"""유튜브 영상의 분석 — youtube_video 확장의 유튜브 전용 모듈(downloads의 작업 종류 handler).

주소 인식, 분석(yt-dlp는 플랫폼 모듈의 `ytdlp.py`를 통해서만 부른다. 서버가 고른 판을 그대로 쓴다), 화질·코덱 고르기,
받은 파일 이름이 여기 있다. 받는 일은 downloads 확장의 `media`(음성이 따로인 파일 받기)가 한다.
이 모듈은 수신 프로세스(downloads의 worker) 안에서 불린다. 쿠키는 yt-dlp 호출에만 쓰고 파일 요청에는 싣지 않는다.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ...core import naming
from ...platforms.youtube import platform as ytp
from ..downloads import media
from ..downloads.media import Variant, Video
from ..downloads.media import Failure

HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com")
SHORT_HOSTS = ("youtu.be", "www.youtu.be")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
STREAM_HOSTS = ("googlevideo.com",)
MAX_REQUEST = 10 * 1024 * 1024  # 한 범위 요청(yt-dlp가 알려 주는 http_chunk_size와 같은 상한)
MAX_CONNECTIONS = 4
URL_LIFETIME = 5 * 3600.0  # 받은 지 5시간이면 다시 분석한다(실측 유효 6시간)
CODECS = {"avc1": "H.264", "vp09": "VP9", "vp9": "VP9", "av01": "AV1"}


def _canonical(video_id: str) -> dict[str, Any]:
    return {"url": f"https://www.youtube.com/watch?v={video_id}", "platform": "youtube", "kind": "youtube",
            "video_id": video_id}


def parse(value: Any) -> dict[str, Any] | None:
    """영상 하나의 주소면 정규화한 값. 재생목록·채널처럼 받지 않는 유튜브 주소는 `rejected`를 단 값(분석에서
    까닭을 알린다). 유튜브 주소가 아니면 invalid_url."""
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\\]", value.strip()):
        raise Failure("invalid_url")
    text = value.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        raise Failure("invalid_url") from None
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() not in ("https", "http") or parts.username or parts.password or host not in HOSTS + SHORT_HOSTS:
        raise Failure("invalid_url")
    segs = [s for s in parts.path.split("/") if s]
    vid = None
    if host in SHORT_HOSTS:
        vid = segs[0] if len(segs) == 1 else None
    elif segs == ["watch"]:
        vid = (parse_qs(parts.query).get("v") or [None])[0]
    elif len(segs) == 2 and segs[0] in ("shorts", "live", "embed"):
        vid = segs[1]
    if vid and VIDEO_ID.match(vid):
        return _canonical(vid)
    # 재생목록·채널·검색 등: 영상 하나가 아니다
    return {"url": text[:300], "platform": "youtube", "kind": "youtube", "video_id": "", "rejected": True}


def key(parsed: dict[str, Any]) -> str:
    """받은 영상 기억: 플랫폼과 영상 ID."""
    return f"youtube:{parsed['video_id']}"


def thumbnail_ok(url: str) -> bool:
    return False  # 썸네일은 받지도 보이지도 않는다


# ---- 분석 ----
def _codec(vcodec: str) -> str:
    head = (vcodec or "").split(".", 1)[0].lower()
    return CODECS.get(head, head.upper() or "?")


def _size(f: dict[str, Any]) -> int | None:
    value = f.get("filesize") or f.get("filesize_approx")
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


def _file_formats(info: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """영상만 든 파일과 음성만 든 파일(https, googlevideo). 재생목록형(HLS)과 합쳐진 형식은 쓰지 않는다."""
    videos, audios = [], []
    for f in info.get("formats") or []:
        if not isinstance(f, dict) or f.get("protocol") != "https" or not f.get("url"):
            continue
        if not media.host_ok(str(f["url"])):
            continue
        v, a = f.get("vcodec") or "none", f.get("acodec") or "none"
        if v != "none" and a == "none" and f.get("height"):
            videos.append(f)
        elif v == "none" and a != "none":
            audios.append(f)
    return videos, audios


def _rate(f: dict[str, Any]) -> float:
    return float(f.get("tbr") or f.get("abr") or 0)


def pick_audio(audios: list[dict]) -> dict | None:
    """AAC 가운데 대역폭이 가장 높은 2채널. 없으면 AAC 아무거나, 그것도 없으면 가장 높은 것."""
    aac = [f for f in audios if str(f.get("acodec") or "").startswith("mp4a")]
    stereo = [f for f in aac if f.get("audio_channels") in (2, None)]
    pool = stereo or aac or audios
    return max(pool, key=_rate) if pool else None


def pick_videos(videos: list[dict], high_codec: str) -> list[dict]:
    """높이마다 하나: 1080p 이하는 H.264, 그보다 높으면 설정한 코덱(vp9·av1). 없으면 있는 것."""
    want_high = "av01" if high_codec == "av1" else "vp09"
    out = []
    for height in sorted({int(f["height"]) for f in videos}, reverse=True):
        same = [f for f in videos if int(f["height"]) == height]
        want = "avc1" if height <= 1080 else want_high
        match = [f for f in same if str(f.get("vcodec") or "").lower().startswith(want)
                 or (want == "vp09" and str(f.get("vcodec") or "").lower().startswith("vp9"))]
        out.append(max(match or same, key=lambda f: (_rate(f), f.get("fps") or 0)))
    return out


def _stamp(info: dict[str, Any]) -> str | None:
    """공개 시각(한국 시각 `YYYY-MM-DD HH:MM:SS`), 없으면 올린 날짜(`YYYY-MM-DD`)."""
    ts = info.get("release_timestamp") or info.get("timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, timezone.utc).astimezone(naming.KST).strftime("%Y-%m-%d %H:%M:%S")
    day = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", day):
        return f"{day[:4]}-{day[4:6]}-{day[6:]}"
    return None


def _mb(n: int | None) -> str:
    return f" · 약 {n / 1024 ** 2:.0f}MB" if n else ""


def setup(headers: dict[str, str] | None = None) -> None:
    """이 프로세스의 요청 규칙: googlevideo.com 아래만, 머리글은 yt-dlp가 알려 준 것(쿠키는 싣지 않는다)."""
    clean = {k: v for k, v in (headers or {}).items() if isinstance(k, str) and isinstance(v, str) and k.lower() != "cookie"}
    media.configure(STREAM_HOSTS + tuple(ytp.backend().extra_hosts()), clean)


def analyze(url: str, cookies: dict[str, str] | None, api_base: str | None = None, *, context: dict | None = None) -> Video:
    setup()
    p = parse(url)
    if p is None or p.get("rejected"):
        raise Failure("unsupported_url")
    context = context or {}
    backend = ytp.backend()
    source = backend.Source.from_token(context.get("ytdlp"))
    outcome = backend.extract(p["url"], ytp.cookie_text(cookies), source=source)
    if outcome.kind != "ok":
        if outcome.kind == "login":
            raise Failure("login_rejected" if ytp.cookie_text(cookies) else "login_required")
        raise Failure({"bot": "rate_limited", "rate": "rate_limited", "notfound": "not_found",
                       "timeout": "network_timeout", "offline": "not_found"}.get(outcome.kind, "engine_operation_failed"))
    info = outcome.info or {}
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise Failure("live_video")
    if info.get("live_status") == "post_live":
        raise Failure("not_ready")
    videos, audios = _file_formats(info)
    audio = pick_audio(audios)
    if not videos or audio is None:
        raise Failure("not_ready")
    headers = next((f.get("http_headers") for f in videos if isinstance(f.get("http_headers"), dict)), None)
    setup(headers)
    a_size = _size(audio)
    a_var = Variant(str(audio["format_id"]), None, None, None, int(_rate(audio) * 1000), "file", str(audio["url"]),
                    size=a_size)
    variants = []
    for f in pick_videos(videos, str(context.get("codec") or "vp9")):
        fps = float(f["fps"]) if f.get("fps") else None
        v_size = _size(f)
        total = (v_size + a_size) if v_size and a_size else None
        label = f"{int(f['height'])}p{round(fps) if fps and round(fps) > 30 else ''} {_codec(str(f.get('vcodec')))}{_mb(total)}"
        variants.append(Variant(f"{f['format_id']}-{audio['format_id']}", int(f["height"]),
                                int(f["width"]) if f.get("width") else None, fps, int(_rate(f) * 1000), "file",
                                str(f["url"]), audio=a_var, size=v_size, display=label))
    duration = info.get("duration")
    return Video(url=p["url"], kind="youtube", video_id=p["video_id"], title=str(info.get("title") or p["video_id"]),
                 channel=str(info.get("channel") or info.get("uploader") or ""), open_date=_stamp(info),
                 duration=float(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
                 thumbnail=None, adult=int(info.get("age_limit") or 0) >= 18, mode="split", variants=variants,
                 platform="youtube", max_request=MAX_REQUEST, max_connections=MAX_CONNECTIONS,
                 expires_at=time.time() + URL_LIFETIME)


def filename(video: dict[str, Any]) -> str:
    """`[YYMMDD_hhmmss] {채널명} {영상제목}.mp4`(한국 시각). 공개 시각이 없고 날짜만 있으면 `[YYMMDD]`."""
    title = video.get("title") or video.get("video_id") or "youtube"
    channel = str(video.get("channel") or "")
    stamp = str(video.get("open_date") or "")
    when = None
    date_only = False
    try:
        when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=naming.KST)
    except ValueError:
        try:
            when = datetime.strptime(stamp, "%Y-%m-%d").replace(tzinfo=naming.KST)
            date_only = True
        except ValueError:
            when = None
    when = when or datetime.now(naming.KST)
    name = naming.render("[{broadcast_time}] {channel_name} {safe_live_title}{file_extension}", title=title,
                         channel_name=channel, quality="", frame_rate="", extension=".mp4",
                         broadcast_start=when, recording_start=when, now=when)
    if date_only:
        name = re.sub(r"^\[(\d{6})_\d{6}\]", r"[\1]", name)
    return name
