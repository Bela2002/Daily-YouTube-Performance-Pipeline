"""An in-memory stand-in for the YouTube Data API v3, used by the tests.

Responses follow the real API's JSON shapes so the SQL transform is
exercised exactly as in production.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any] | None, headers: dict[str, str] | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self) -> dict[str, Any]:
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def api_error(status: int, reason: str, message: str = "error") -> FakeResponse:
    return FakeResponse(status, {"error": {"code": status, "message": message,
                                           "errors": [{"reason": reason, "message": message}]}})


class FakeYouTube:
    """Two channels:
    * UC_ALPHA (by ID)        - 60 videos, one every 12h, in two playlists
    * @betahandle -> UC_BETA  - 3 videos, no playlists, comments disabled on one
    """

    PAGE = 50

    def __init__(self, now: datetime, view_boost: int = 0):
        self.now = now
        self.view_boost = view_boost
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # functions (endpoint, params) -> FakeResponse | None, used to inject failures
        self.overrides: list = []
        self.channels = {
            "UC_ALPHA": {"title": "Alpha News LK", "handle": "@alphanews", "uploads": "UU_ALPHA"},
            "UC_BETA": {"title": "Beta Cooking", "handle": "@betahandle", "uploads": "UU_BETA"},
        }
        self.videos: dict[str, dict[str, Any]] = {}
        for i in range(60):  # newest first; a1 published 2h ago
            self._add_video(f"a{i}", "UC_ALPHA", now - timedelta(hours=2 + 12 * i), 300 + i, 1000 - i * 10)
        self._add_video("b0", "UC_BETA", now - timedelta(hours=5), 3725, 50, comments=False)
        self._add_video("b1", "UC_BETA", now - timedelta(days=3), 59, 900)
        self._add_video("b2", "UC_BETA", now - timedelta(days=40), 600, 5000)  # outside window
        self.playlists = {
            "PL_ALPHA_HEADLINES": {"channel": "UC_ALPHA", "title": "Headlines",
                                   "items": [("a0", now - timedelta(hours=1)), ("a1", now - timedelta(hours=10)),
                                             ("a5", now - timedelta(days=2))]},
            "PL_ALPHA_SPORTS": {"channel": "UC_ALPHA", "title": "Sports",
                                "items": [("a1", now - timedelta(hours=9)), ("a7", now - timedelta(days=3))]},
            "PL_ALPHA_EMPTY": {"channel": "UC_ALPHA", "title": "Empty", "items": []},
        }

    def _add_video(self, vid, channel, published, duration_s, views, comments=True):
        h, rem = divmod(duration_s, 3600)
        m, s = divmod(rem, 60)
        duration = "PT" + (f"{h}H" if h else "") + (f"{m}M" if m else "") + (f"{s}S" if s or not (h or m) else "")
        self.videos[vid] = {"channel": channel, "published": published, "duration": duration,
                            "views": views, "comments": comments}

    # requests.Session-compatible ------------------------------------------
    def get(self, url: str, params: dict[str, Any], headers: dict[str, str] | None = None,
            timeout: float = 0) -> FakeResponse:
        endpoint = url.rsplit("/", 1)[-1]
        # the API key must travel in a header, never in the URL/query string
        assert "key" not in params, "API key leaked into query parameters"
        assert (headers or {}).get("X-Goog-Api-Key"), "API key header missing"
        self.calls.append((endpoint, dict(params)))
        for override in self.overrides:
            injected = override(endpoint, params)
            if injected is not None:
                return injected
        handler = getattr(self, f"_{endpoint}")
        return FakeResponse(200, handler(params))

    def _page(self, items: list, params: dict[str, Any]) -> dict[str, Any]:
        start = int(params.get("pageToken", "0"))
        page = items[start : start + self.PAGE]
        body: dict[str, Any] = {"items": page, "pageInfo": {"totalResults": len(items)}}
        if start + self.PAGE < len(items):
            body["nextPageToken"] = str(start + self.PAGE)
        return body

    def _channel_item(self, cid):
        ch = self.channels[cid]
        count = sum(v["channel"] == cid for v in self.videos.values())
        return {"kind": "youtube#channel", "id": cid,
                "snippet": {"title": ch["title"], "customUrl": ch["handle"]},
                "statistics": {"videoCount": str(count)},
                "contentDetails": {"relatedPlaylists": {"likes": "", "uploads": ch["uploads"]}}}

    def _channels(self, p):
        if "forHandle" in p:
            ids = [c for c, v in self.channels.items() if v["handle"] == p["forHandle"]]
        else:
            ids = [c for c in p["id"].split(",") if c in self.channels]
        return {"items": [self._channel_item(c) for c in ids]}

    def _playlistItems(self, p):
        pid = p["playlistId"]
        if pid.startswith("UU_"):
            cid = "UC_" + pid[3:]
            vids = sorted((v for v in self.videos.items() if v[1]["channel"] == cid),
                          key=lambda kv: kv[1]["published"], reverse=True)
            items = [{"kind": "youtube#playlistItem", "id": f"{pid}-{vid}",
                      "contentDetails": {"videoId": vid, "videoPublishedAt": iso(v["published"])}}
                     for vid, v in vids]
        else:
            pl = self.playlists[pid]
            items = [{"kind": "youtube#playlistItem", "id": f"{pid}-{vid}",
                      "snippet": {"publishedAt": iso(added), "playlistId": pid, "channelId": pl["channel"],
                                  "resourceId": {"kind": "youtube#video", "videoId": vid}},
                      "contentDetails": {"videoId": vid, "videoPublishedAt": iso(self.videos[vid]["published"])}}
                     for vid, added in pl["items"]]
        return self._page(items, p)

    def _videos(self, p):
        items = []
        for vid in p["id"].split(","):
            v = self.videos.get(vid)
            if not v:
                continue
            stats = {"viewCount": str(v["views"] + self.view_boost), "likeCount": str(v["views"] // 10),
                     "favoriteCount": "0"}
            if v["comments"]:
                stats["commentCount"] = str(v["views"] // 100)
            items.append({"kind": "youtube#video", "id": vid,
                          "snippet": {"publishedAt": iso(v["published"]), "channelId": v["channel"],
                                      "title": f"Video {vid}", "liveBroadcastContent": "none"},
                          "contentDetails": {"duration": v["duration"]},
                          "statistics": stats})
        return {"items": items}

    def _search(self, p):
        after = datetime.fromisoformat(p["publishedAfter"].replace("Z", "+00:00"))
        before = datetime.fromisoformat(p["publishedBefore"].replace("Z", "+00:00"))
        vids = sorted(((vid, v) for vid, v in self.videos.items()
                       if v["channel"] == p["channelId"] and after <= v["published"] < before),
                      key=lambda kv: kv[1]["published"], reverse=True)
        return self._page([{"kind": "youtube#searchResult", "id": {"kind": "youtube#video", "videoId": vid}}
                           for vid, _ in vids], p)

    def _playlists(self, p):
        items = [{"kind": "youtube#playlist", "id": pid,
                  "snippet": {"channelId": pl["channel"], "title": pl["title"]},
                  "contentDetails": {"itemCount": len(pl["items"])}}
                 for pid, pl in self.playlists.items() if pl["channel"] == p["channelId"]]
        return self._page(items, p)
