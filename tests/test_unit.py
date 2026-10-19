"""Unit tests - no database or network needed."""
from datetime import datetime, timezone

import pytest
import requests

from tests.fake_youtube import FakeResponse, FakeYouTube, api_error
from yt_pipeline.config import ConfigError, Settings, parse_channels
from yt_pipeline.extract import Extractor
from yt_pipeline.youtube_client import NotFoundError, QuotaExceededError, YouTubeAPIError, YouTubeClient

NOW = datetime(2026, 10, 18, 0, 30, tzinfo=timezone.utc)
SECRET = "AIzaSy-SUPER-SECRET-KEY"


# --- configuration --------------------------------------------------------
def test_parse_channels_dedupes_and_trims():
    assert parse_channels(" UC1, @news ,UC1,,\nUC2") == ["UC1", "@news", "UC2"]
    assert parse_channels(None) == []


def test_missing_settings_give_clear_errors(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings.from_env()
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("YOUTUBE_API_KEY", "")
    with pytest.raises(ConfigError, match="YOUTUBE_API_KEY"):
        Settings.from_env()
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    monkeypatch.setenv("YOUTUBE_CHANNELS", "@a")
    monkeypatch.setenv("LOOKBACK_DAYS", "abc")
    with pytest.raises(ConfigError, match="LOOKBACK_DAYS"):
        Settings.from_env()


def test_settings_repr_hides_secrets():
    s = Settings(youtube_api_key=SECRET, database_url="postgresql://u:dbpass@h/db", channels=["@a"])
    assert SECRET not in repr(s) and "dbpass" not in repr(s)


# --- API client -----------------------------------------------------------
class ScriptedSession:
    """Returns (or raises) the given items in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client_for(*responses, retries=4):
    session = ScriptedSession(responses)
    return YouTubeClient(SECRET, session=session, backoff_seconds=0, max_retries=retries), session


def test_api_key_is_sent_as_header_not_in_url():
    client, session = client_for(FakeResponse(200, {"items": []}))
    client.get("videos", {"id": "x"})
    call = session.calls[0]
    assert call["headers"]["X-Goog-Api-Key"] == SECRET
    assert SECRET not in str(call["params"]) and SECRET not in call["url"]


def test_network_error_message_never_contains_key():
    leaky = requests.ConnectionError(f"Max retries exceeded with url: /videos?key={SECRET}")
    client, _ = client_for(leaky, leaky, retries=1)
    with pytest.raises(YouTubeAPIError) as info:
        client.get("videos", {})
    assert SECRET not in str(info.value)
    assert "REDACTED" in str(info.value)


def test_client_retries_transient_errors():
    client, session = client_for(FakeResponse(503, {}), FakeResponse(200, {"items": [1]}))
    assert client.get("videos", {}) == {"items": [1]}
    assert len(session.calls) == 2


def test_client_retries_403_rate_limit_and_invalid_json():
    client, session = client_for(api_error(403, "rateLimitExceeded"), FakeResponse(200, None),
                                 FakeResponse(200, {"ok": True}))
    assert client.get("videos", {}) == {"ok": True}
    assert len(session.calls) == 3


def test_client_honours_retry_after(monkeypatch):
    slept = []
    monkeypatch.setattr("yt_pipeline.youtube_client.time.sleep", slept.append)
    client, _ = client_for(FakeResponse(429, {}, headers={"Retry-After": "7"}), FakeResponse(200, {}))
    client.get("videos", {})
    assert slept == [7.0]


def test_client_gives_up_after_max_retries():
    client, session = client_for(*[FakeResponse(500, {})] * 3, retries=2)
    with pytest.raises(YouTubeAPIError, match="HTTP 500"):
        client.get("videos", {})
    assert len(session.calls) == 3


def test_client_raises_on_quota_without_retry():
    client, session = client_for(api_error(403, "quotaExceeded"))
    with pytest.raises(QuotaExceededError):
        client.get("videos", {})
    assert len(session.calls) == 1


def test_client_does_not_retry_permission_errors():
    client, session = client_for(api_error(403, "forbidden"))
    with pytest.raises(YouTubeAPIError, match="forbidden"):
        client.get("videos", {})
    assert len(session.calls) == 1


def test_client_404_is_not_found_error():
    client, _ = client_for(api_error(404, "playlistNotFound"))
    with pytest.raises(NotFoundError):
        client.get("playlistItems", {})


def test_client_raises_on_bad_request():
    client, _ = client_for(api_error(400, "badRequest"))
    with pytest.raises(YouTubeAPIError):
        client.get("videos", {})


# --- extractor ------------------------------------------------------------
def extractor(fake, **kw):
    client = YouTubeClient(SECRET, session=fake, backoff_seconds=0, max_retries=1)
    return Extractor(client, now=NOW, **kw), client


def test_extractor_respects_lookback_and_counts_quota():
    fake = FakeYouTube(NOW)
    ex, client = extractor(fake, lookback_days=28)
    result = ex.run(["UC_ALPHA", "@betahandle", "UC_MISSING"])

    assert set(result.channel_ids) == {"UC_ALPHA", "UC_BETA"}
    assert result.missing_channels == ["UC_MISSING"] and result.is_partial
    alpha = [v for v in result.video_ids if v.startswith("a")]
    assert len(alpha) == 56                       # one video every 12h -> 56 in 28 days
    assert "b2" not in result.video_ids           # 40 days old
    upload_pages = [c for c in fake.calls if c[0] == "playlistItems" and c[1]["playlistId"] == "UU_ALPHA"]
    assert len(upload_pages) == 2
    assert not any(c[1].get("playlistId") == "PL_ALPHA_EMPTY" for c in fake.calls)  # empty skipped
    assert client.quota_units_used == len(fake.calls)


def test_extractor_short_lookback_stops_early():
    ex, _ = extractor(FakeYouTube(NOW), lookback_days=1, fetch_playlists=False)
    assert ex.run(["UC_ALPHA"]).video_ids == ["a0", "a1"]


def test_channel_without_uploads_is_not_a_failure():
    fake = FakeYouTube(NOW)
    fake.overrides.append(lambda ep, p: api_error(404, "playlistNotFound")
                          if p.get("playlistId") == "UU_BETA" else None)
    result = extractor(fake)[0].run(["@betahandle"])
    assert result.channel_ids == ["UC_BETA"] and result.video_ids == [] and not result.is_partial


def test_one_failing_channel_does_not_stop_the_others():
    fake = FakeYouTube(NOW)
    fake.overrides.append(lambda ep, p: FakeResponse(500, {}) if ep == "videos" and "b0" in p["id"] else None)
    result = extractor(fake)[0].run(["UC_ALPHA", "@betahandle"])
    assert result.channel_ids == ["UC_ALPHA"]
    assert "UC_BETA" in result.failed_channels
    # none of the failed channel's responses are kept (all-or-nothing per channel)
    assert not any(r.context.get("channel_id") == "UC_BETA" for r in result.responses)


def test_broken_playlist_is_only_a_warning():
    fake = FakeYouTube(NOW)
    fake.overrides.append(lambda ep, p: api_error(404, "playlistNotFound")
                          if p.get("playlistId") == "PL_ALPHA_SPORTS" else None)
    result = extractor(fake)[0].run(["UC_ALPHA"])
    assert result.channel_ids == ["UC_ALPHA"] and not result.failed_channels
    assert any("PL_ALPHA_SPORTS" in w for w in result.warnings)


def test_quota_error_stops_the_whole_run():
    fake = FakeYouTube(NOW)
    fake.overrides.append(lambda ep, p: api_error(403, "quotaExceeded") if ep == "videos" else None)
    with pytest.raises(QuotaExceededError):
        extractor(fake)[0].run(["UC_ALPHA", "@betahandle"])


def test_high_volume_channel_truncation_is_flagged():
    # 56 alpha videos in window; cap at 1 page (50) -> must NOT silently drop 6
    ex, _ = extractor(FakeYouTube(NOW), max_upload_pages=1, fetch_playlists=False)
    result = ex.run(["UC_ALPHA"])
    assert len(result.video_ids) == 50
    assert result.truncated_channels == ["UC_ALPHA"] and result.is_partial
    assert any("MAX_UPLOAD_PAGES" in w for w in result.warnings)


def test_no_truncation_flag_when_window_edge_reached():
    ex, _ = extractor(FakeYouTube(NOW), lookback_days=10, max_upload_pages=1, fetch_playlists=False)
    result = ex.run(["UC_ALPHA"])
    assert len(result.video_ids) == 20 and not result.truncated_channels


def test_transaction_pooler_is_detected():
    from yt_pipeline.db import is_transaction_pooler
    assert is_transaction_pooler("ep-cool-123-pooler.ap-southeast-1.aws.neon.tech", 5432)
    assert is_transaction_pooler("aws-0-ap-south-1.pooler.supabase.com", 6543)
    assert not is_transaction_pooler("ep-cool-123.ap-southeast-1.aws.neon.tech", 5432)
    assert not is_transaction_pooler("localhost", 5432)
