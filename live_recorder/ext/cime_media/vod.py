"""씨미 다시보기 — downloads 작업 종류의 handler."""

from __future__ import annotations

from typing import Any

from ..downloads import media
from ..downloads.media import Failure, Video
from . import common

KIND = "cime_vod"


def parse(value: Any) -> dict[str, Any] | None:
    """`ci.me/@핸들/vods/{숫자}`. 방송 주소와 채널·목록 주소는 받지 않는 까닭을 단 값(rejected)."""
    text, segs = common.parse_parts(value)
    base = {"platform": "cime", "kind": KIND}
    if len(segs) == 3 and common.HANDLE_RE.match(segs[0]) and segs[1] == "vods" and common.ID_RE.match(segs[2]):
        return dict(base, url=f"https://ci.me/{segs[0]}/vods/{segs[2]}", video_id=segs[2])
    if len(segs) == 2 and common.HANDLE_RE.match(segs[0]) and segs[1] == "live":
        return dict(base, url=text[:300], video_id="", rejected="live_video")
    if segs and common.HANDLE_RE.match(segs[0]) and (len(segs) == 1 or segs[1:] in (["vods"], ["clips"])):
        return dict(base, url=text[:300], video_id="", rejected="unsupported_url")
    return None


def key(parsed: dict[str, Any]) -> str:
    return f"cime:vod:{parsed['video_id']}"  # 플랫폼·종류·ID


thumbnail_ok = common.thumbnail_ok
filename = common.filename


def _label(height: int | None, bandwidth: int, duration: float | None) -> str:
    size = f" · 약 {bandwidth * duration / 8 / 1024 ** 2:.0f}MB" if bandwidth and duration else ""
    return f"{height}p{size}" if height else f"화질{size}"


def analyze(url: str, cookies: dict[str, str] | None, api_base: str | None = None, *, context: dict | None = None) -> Video:
    common.setup()
    p = parse(url)
    if p is None:
        raise Failure("invalid_url")
    if p.get("rejected"):
        raise Failure(p["rejected"])
    data = common.get_info("vod", p["video_id"], cookies, p["url"])
    master = common.check_playable(data, cookies)
    media.check_url(master)
    auth = common.SignedCookies("vod", p["video_id"], cookies, p["url"])
    auth.renew()
    duration = float(data["duration"]) / 1000 if isinstance(data.get("duration"), (int, float)) and data["duration"] > 0 else None
    with media.http_client() as client:
        status, body = media.http_get(client, master, dict(media.media_headers(), **auth.headers(master)), media.MAX_JSON)
    if status != 200:
        raise Failure({403: "http_403", 404: "not_found"}.get(status, "engine_operation_failed"))
    text = body.decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in text:
        variants = media.parse_master(text, master)
    else:
        variants = [media.Variant("hls-0p-0", None, None, None, 0, "hls", master, signed=False)]
    for v in variants:
        v.signed = False
        v.display = _label(v.height, v.bandwidth, duration)
    if not variants:
        raise Failure("not_ready")
    channel = data.get("channel") if isinstance(data.get("channel"), dict) else {}
    return Video(url=p["url"], kind=KIND, video_id=p["video_id"], title=str(data.get("title") or p["video_id"]),
                 channel=str(channel.get("name") or ""), open_date=common.kst(data.get("liveOpenedAt") or data.get("openedAt")),
                 duration=duration, thumbnail=None, adult=bool(data.get("isAdult")), mode="hls", variants=variants,
                 platform="cime", max_connections=4, auth=auth)
