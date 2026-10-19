"""Thin, dependency-light client for the YouTube Data API v3.

Why not google-api-python-client? Plain `requests` keeps the code easy to
read, lets us count quota precisely, and makes retries explicit.

Security: the API key is sent in the `X-Goog-Api-Key` header, never in the
URL, so it cannot appear in URLs, proxy logs or exception messages. As a
second line of defence every error message is passed through `redact()`.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import requests

log = logging.getLogger(__name__)
# urllib3 logs full request lines at DEBUG level; keep it quiet.
logging.getLogger("urllib3").setLevel(logging.WARNING)

BASE_URL = "https://www.googleapis.com/youtube/v3"

# Quota cost per call (https://developers.google.com/youtube/v3/determine_quota_cost).
# The pipeline only uses `list` calls (1 unit). `search` (100 units) is used
# only by the optional `verify --deep` completeness check.
QUOTA_COST = {"channels": 1, "playlistItems": 1, "playlists": 1, "videos": 1, "search": 100}

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 403s that are temporary throttling, not permission problems
RETRYABLE_403_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
MAX_RETRY_AFTER_SECONDS = 60


class YouTubeAPIError(RuntimeError):
    """Non-retryable error returned by the API (or retries exhausted)."""


class QuotaExceededError(YouTubeAPIError):
    """Daily quota is used up - retrying today will not help."""


class NotFoundError(YouTubeAPIError):
    """HTTP 404, e.g. a playlist that was deleted or a channel with no uploads."""


class YouTubeClient:
    def __init__(
        self,
        api_key: str,
        session: requests.Session | None = None,
        max_retries: int = 4,
        timeout: float = 30.0,
        backoff_seconds: float = 1.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._timeout = timeout
        self._backoff = backoff_seconds
        self.requests_made = 0
        self.quota_units_used = 0

    def redact(self, text: str) -> str:
        """Remove the API key from any text before it is logged or stored."""
        return text.replace(self._api_key, "***REDACTED***") if text else text

    # ------------------------------------------------------------------
    def get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET one page from an endpoint, with retries for transient errors."""
        url = f"{BASE_URL}/{endpoint}"
        headers = {"X-Goog-Api-Key": self._api_key, "Accept": "application/json"}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._session.get(url, params=params, headers=headers, timeout=self._timeout)
            except requests.RequestException as exc:
                msg = self.redact(f"{type(exc).__name__}: {exc}")
                if attempt > self._max_retries:
                    raise YouTubeAPIError(f"{endpoint}: network error after {attempt} attempts: {msg}") from None
                self._sleep(attempt, msg)
                continue

            self.requests_made += 1
            self.quota_units_used += QUOTA_COST.get(endpoint, 1)

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    if attempt > self._max_retries:
                        raise YouTubeAPIError(f"{endpoint}: invalid JSON in response") from None
                    self._sleep(attempt, "invalid JSON in response")
                    continue

            reason, message = _error_details(resp)
            message = self.redact(message)
            if resp.status_code == 403 and reason in QUOTA_REASONS:
                raise QuotaExceededError(f"YouTube API quota exceeded: {message}")
            retryable = resp.status_code in RETRYABLE_STATUS or (
                resp.status_code == 403 and reason in RETRYABLE_403_REASONS
            )
            if retryable and attempt <= self._max_retries:
                self._sleep(attempt, f"HTTP {resp.status_code} {reason}", _retry_after(resp))
                continue
            error_cls = NotFoundError if resp.status_code == 404 else YouTubeAPIError
            raise error_cls(f"{endpoint}: HTTP {resp.status_code} ({reason}): {message}")

    def paginate(
        self, endpoint: str, params: dict[str, Any], max_pages: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield successive pages, following nextPageToken."""
        page_params = dict(params)
        pages = 0
        while True:
            page = self.get(endpoint, page_params)
            pages += 1
            yield page
            token = page.get("nextPageToken")
            if not token or (max_pages is not None and pages >= max_pages):
                return
            page_params = {**params, "pageToken": token}

    # ------------------------------------------------------------------
    def _sleep(self, attempt: int, why: str, retry_after: float | None = None) -> None:
        delay = self._backoff * (2 ** (attempt - 1))
        if retry_after is not None:
            delay = max(delay, min(retry_after, MAX_RETRY_AFTER_SECONDS))
        log.warning("Transient YouTube API problem (%s); retry %d in %.1fs", why, attempt, delay)
        time.sleep(delay)


def _retry_after(resp: requests.Response) -> float | None:
    value = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _error_details(resp: requests.Response) -> tuple[str, str]:
    try:
        err = resp.json().get("error", {})
        reasons = err.get("errors") or [{}]
        return reasons[0].get("reason", "unknown"), str(err.get("message", ""))[:300]
    except (ValueError, AttributeError):
        return "unknown", (resp.text or "")[:300]
