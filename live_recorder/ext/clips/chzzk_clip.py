"""치지직 클립의 분석 — clips 확장의 치지직 전용 모듈.

클립 주소 인식, clips.resolve_clip으로 해석, 받은 파일 이름이 여기에 있다. 받는 일은 downloads의 `media`가 한다.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from ...core import naming
from ..downloads import media
from ..downloads.media import QUALITY_ID, Variant, Video, parse_master, parse_mpd, prefer_files
from ..downloads.media import Failure as ReplayFailure
from . import clips

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
SITE = "https://chzzk.naver.com"
ALLOWED_HOSTS = ("naver.com", "pstatic.net", "akamaized.net", "navercorp.com")


def _check_site(value: Any) -> str:
    """치지직 주소의 공통 검사(https, 사용자 정보·다른 포트 없음, 치지직 호스트). 끝의 `/`를 뗀 경로."""
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\\]", value.strip()):
        raise ReplayFailure("invalid_url")
    parts = urlsplit(value.strip())
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443) or parts.hostname not in ("chzzk.naver.com", "www.chzzk.naver.com")):
        raise ReplayFailure("invalid_url")
    return parts.path.rstrip("/")


def parse(value: Any) -> dict[str, Any] | None:
    """클립 주소면 정규화한 값, 치지직 주소지만 클립이 아니면 None. 치지직 주소가 아니면 invalid_url."""
    m = re.fullmatch(r"/clips/([A-Za-z0-9]{1,100})", _check_site(value))
    if not m:
        return None
    return {"url": f"{SITE}/clips/{m.group(1)}", "platform": "chzzk", "kind": "clip", "video_id": m.group(1)}


def clip_url(clip_id: str) -> str:
    return f"{SITE}/clips/{clip_id}"


def key(parsed: dict[str, Any]) -> str:
    """중복 판단 열쇠: `clip:` 접두사를 붙인 클립 ID."""
    return "clip:" + parsed["video_id"]


def thumbnail_ok(url: str) -> bool:
    return bool(re.fullmatch(r"https://[a-z0-9.-]+\.pstatic\.net/[^\s\"<>]{1,500}", url or ""))


def setup() -> None:
    media.configure(ALLOWED_HOSTS, {"User-Agent": USER_AGENT, "Referer": SITE + "/"})


def analyze_clip(clip_id: str) -> Video:
    """공개 클립. clips.resolve_clip으로 해석한다."""
    try:
        info = clips.resolve_clip(clip_id)
    except clips.ClipUnavailableError:
        raise ReplayFailure("not_found") from None
    variants = [Variant(f["id"], f["height"], f["width"], media.frame_rate(f.get("fps")), f["width"] * f["height"], "file", f["url"])
                for f in info["formats"]]
    return Video(platform="chzzk", url=f"{SITE}/clips/{clip_id}", kind="clip", video_id=clip_id, title=str(info["title"]), channel="",
                 open_date=None, duration=float(info["duration"]), thumbnail=None, adult=False, mode="clip",
                 variants=variants)


def analyze(url: str, cookies: dict[str, str] | None, api_base: str = "") -> Video:
    setup()
    p = parse(url)
    if p is None:
        raise ReplayFailure("invalid_url")
    return analyze_clip(p["video_id"])


def filename(video: dict[str, Any]) -> str:
    """클립은 `{제목} [{클립ID}].mp4`."""
    title = video.get("title") or video.get("video_id") or "replay"
    now = datetime.now(naming.KST)
    return naming.render("{safe_live_title} [{channel_name}]{file_extension}", title=title,
                         channel_name=str(video.get("video_id") or ""), quality="", frame_rate="", extension=".mp4",
                         broadcast_start=now, recording_start=now, now=now)
