"""치지직 다시보기의 분석 — replay 확장의 치지직 전용 모듈.

영상 API 주소, 응답 해석, 주소 인식, 받은 파일 이름이 여기에 있다. 받는 일은 downloads 확장의 `media`가 한다.
이 모듈은 수신 프로세스(downloads의 worker) 안에서 불린다(작업 종류의 handler).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from ...core import naming
from ..downloads import media
from ..downloads.media import QUALITY_ID, Variant, Video, parse_master, parse_mpd, prefer_files
from ..downloads.media import Failure as ReplayFailure

USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
SITE = "https://chzzk.naver.com"
API_BASE = "https://api.chzzk.naver.com"
PLAYBACK_URL = "https://apis.naver.com/neonplayer/vodplay/v2/playback/{video_id}?key={in_key}"
ALLOWED_HOSTS = ("naver.com", "pstatic.net", "akamaized.net", "navercorp.com")
MAX_JSON, MAX_MPD = media.MAX_JSON, media.MAX_MPD


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
    """다시보기 주소면 정규화한 값, 치지직 주소지만 다시보기가 아니면 None. 치지직 주소가 아니면 invalid_url."""
    m = re.fullmatch(r"/video/(\d{1,20})", _check_site(value))
    if not m:
        return None
    return {"url": f"{SITE}/video/{m.group(1)}", "platform": "chzzk", "kind": "video", "video_id": m.group(1)}


def key(parsed: dict[str, Any]) -> str:
    """중복 판단 열쇠: 영상 번호."""
    return parsed["video_id"]


def video_url(video_no: str) -> str:
    """목록에서 고른 영상 번호의 주소."""
    return f"{SITE}/video/{video_no}"


def thumbnail_ok(url: str) -> bool:
    return bool(re.fullmatch(r"https://[a-z0-9.-]+\.pstatic\.net/[^\s\"<>]{1,500}", url or ""))


def api_headers(cookies: dict[str, str] | None) -> dict[str, str]:
    """API 요청에만 쿠키를 싣는다."""
    headers = {"User-Agent": USER_AGENT, "Referer": SITE + "/", "Origin": SITE, "Accept": "application/json"}
    cookie = "; ".join(f"{k}={v}" for k, v in (cookies or {}).items() if k in ("NID_AUT", "NID_SES") and v)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def setup() -> None:
    """이 프로세스의 요청 규칙: 치지직·네이버 CDN 호스트와 영상 요청 머리글."""
    media.configure(ALLOWED_HOSTS, {"User-Agent": USER_AGENT, "Referer": SITE + "/"})


def _tracks_variants(rewind_media: dict, url: str) -> list[Variant]:
    """마스터 재생목록을 읽지 못하면 encodingTrack[]에서 만든다(재생 주소는 마스터 하나)."""
    out = []
    for t in rewind_media.get("encodingTrack") or []:
        if not isinstance(t, dict) or t.get("audioOnly"):
            continue
        tid = str(t.get("encodingTrackId") or "")
        if QUALITY_ID.fullmatch(tid):
            out.append(Variant(f"track-{tid}", int(media.number(t.get("videoHeight")) or 0) or None, None,
                               media.number(t.get("videoFrameRate")), int(media.number(t.get("videoBitRate")) or 0), "hls", url,
                               signed=media.is_signed_naver_hls(url)))
    return out


def _as_json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def analyze_video(video_no: str, cookies: dict[str, str] | None, api_base: str = API_BASE,
                  client: httpx.Client | None = None) -> Video:
    own = client is None
    client = client or media.http_client()
    try:
        status, body = media.http_get(client, f"{api_base}/service/v2/videos/{video_no}", api_headers(cookies), MAX_JSON)
        if status in (401, 403):
            raise ReplayFailure("login_required")
        if status == 404:
            raise ReplayFailure("not_found")
        if status != 200:
            raise ReplayFailure("http_400" if status == 400 else "engine_operation_failed")
        try:
            payload = json.loads(body)
        except ValueError:
            raise ReplayFailure("engine_operation_failed") from None
        if not isinstance(payload, dict) or payload.get("code") != 200 or not isinstance(payload.get("content"), dict):
            raise ReplayFailure("not_found")  # code가 200이 아니면 오류
        c = payload["content"]
        title = str(c.get("videoTitle") or "").strip()
        channel = str((c.get("channel") or {}).get("channelName") or "").strip()
        open_date = c.get("liveOpenDate") if isinstance(c.get("liveOpenDate"), str) else None
        duration = media.number(c.get("duration"))
        adult = c.get("adult") is True
        in_key, vid = c.get("inKey"), c.get("videoId")
        rewind = _as_json(c.get("liveRewindPlaybackJson"))
        common = dict(url=f"{SITE}/video/{video_no}", kind="video", video_id=str(video_no), title=title,
                      channel=channel, open_date=open_date, duration=duration,
                      thumbnail=c.get("thumbnailImageUrl") if isinstance(c.get("thumbnailImageUrl"), str) else None,
                      adult=adult)
        if in_key and vid:  # 인코딩이 끝난 영상(DASH)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", str(vid)):
                raise ReplayFailure("engine_operation_failed")
            mpd_url = PLAYBACK_URL.format(video_id=vid, in_key=in_key)
            if api_base != API_BASE:  # 시험용 가짜 서버
                mpd_url = f"{api_base}/neonplayer/vodplay/v2/playback/{vid}?key={in_key}"
            headers = dict(api_headers(cookies), Accept="application/dash+xml")
            mstatus, xml_bytes = media.http_get(client, mpd_url, headers, MAX_MPD)
            if mstatus in (401, 403):
                raise ReplayFailure("login_required")
            if mstatus != 200:
                raise ReplayFailure("not_ready" if mstatus == 404 else "engine_operation_failed")
            variants = prefer_files(parse_mpd(xml_bytes, mpd_url))
            if not variants:
                raise ReplayFailure("not_ready")
            return Video(**common, platform="chzzk", mode="dash", variants=variants)
        rewind_media = next((m for m in rewind.get("media") or [] if isinstance(m, dict) and m.get("mediaId") == "HLS"), None)
        if rewind_media and rewind_media.get("path"):  # 방송 직후의 빠른 다시보기(HLS)
            master = media.check_url(str(rewind_media["path"]))
            try:
                mstatus, text = media.http_get(client, master, media.media_headers(), MAX_JSON)
                variants = parse_master(text.decode("utf-8", "replace"), master) if mstatus == 200 else []
            except ReplayFailure:
                variants = []
            if not variants:
                variants = _tracks_variants(rewind_media, master)
            if not variants:
                raise ReplayFailure("not_ready")
            return Video(**common, platform="chzzk", mode="hls", variants=variants)
        # 로그인·성인 인증이 필요한데 쿠키가 없거나 만료됐다
        if adult or c.get("userAdultStatus") or c.get("paidProduct") or c.get("membershipBenefitType"):
            raise ReplayFailure("login_required")
        raise ReplayFailure("not_ready")
    finally:
        if own:
            client.close()


def analyze(url: str, cookies: dict[str, str] | None, api_base: str = API_BASE) -> Video:
    setup()
    p = parse(url)
    if p is None:
        raise ReplayFailure("invalid_url")
    video = analyze_video(p["video_id"], cookies, api_base)
    if video.thumbnail and not media.host_ok(video.thumbnail):
        video.thumbnail = None
    return video


def filename(video: dict[str, Any]) -> str:
    """`[YYMMDD_hhmmss] {채널명} {영상제목}.mp4`. 시각은 liveOpenDate다."""
    title = video.get("title") or video.get("video_id") or "replay"
    stamp = None
    if video.get("open_date"):
        try:
            stamp = datetime.strptime(video["open_date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=naming.KST)
        except ValueError:
            stamp = None
    when = stamp or datetime.now(naming.KST)
    return naming.render("[{broadcast_time}] {channel_name} {safe_live_title}{file_extension}", title=title,
                         channel_name=str(video.get("channel") or ""), quality="", frame_rate="", extension=".mp4",
                         broadcast_start=when, recording_start=when, now=when)
