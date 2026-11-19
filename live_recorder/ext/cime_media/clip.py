"""씨미 클립 — downloads 작업 종류의 handler."""

from __future__ import annotations

from typing import Any

from ..downloads import media
from ..downloads.media import Failure, Video
from . import common

KIND = "cime_clip"


def parse(value: Any) -> dict[str, Any] | None:
    """`ci.me/clips/{숫자}`, `ci.me/@핸들/clips/{숫자}`."""
    _, segs = common.parse_parts(value)
    ident = None
    if len(segs) == 2 and segs[0] == "clips" and common.ID_RE.match(segs[1]):
        ident = segs[1]
    elif len(segs) == 3 and common.HANDLE_RE.match(segs[0]) and segs[1] == "clips" and common.ID_RE.match(segs[2]):
        ident = segs[2]
    if ident is None:
        return None
    return {"url": f"https://ci.me/clips/{ident}", "platform": "cime", "kind": KIND, "video_id": ident}


def key(parsed: dict[str, Any]) -> str:
    return f"cime:clip:{parsed['video_id']}"


thumbnail_ok = common.thumbnail_ok
filename = common.filename


def analyze(url: str, cookies: dict[str, str] | None, api_base: str | None = None, *, context: dict | None = None) -> Video:
    common.setup()
    p = parse(url)
    if p is None:
        raise Failure("invalid_url")
    data = common.get_info("clip", p["video_id"], cookies, p["url"])
    file_url = common.check_playable(data, cookies)
    media.check_url(file_url)
    auth = common.SignedCookies("clip", p["video_id"], cookies, p["url"])
    auth.renew()
    duration = float(data["duration"]) / 1000 if isinstance(data.get("duration"), (int, float)) and data["duration"] > 0 else None
    channel = data.get("channel") if isinstance(data.get("channel"), dict) else {}
    variant = media.Variant("clip", None, None, None, 0, "file", file_url, signed=False, display="원본")
    return Video(url=p["url"], kind=KIND, video_id=p["video_id"], title=str(data.get("title") or p["video_id"]),
                 channel=str(channel.get("name") or ""), open_date=common.kst(data.get("createdAt")), duration=duration,
                 thumbnail=None, adult=bool(data.get("isAdult")), mode="clip", variants=[variant], platform="cime",
                 max_connections=4, auth=auth)
