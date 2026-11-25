"""치지직 API. 치지직 플랫폼 모듈의 안쪽이다. 밖에서는 `PLATFORM`(접점)으로만 쓴다.

주소와 필드는 실제 응답으로 확인한 값이다.
응답 해석은 순수 함수로 두어 수신기(동기)와 코어(비동기)가 같이 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from ...core import codec as codec_mod
from ...core import httpread

MAX_RESPONSE = 4 * 1024 * 1024  # API 응답 상한(압축을 푼 양)

from ...core.naming import KST
from ...core.quality import Track
from ..base import ApiError, ChannelParseError, CheckResult, LiveInfo, Playback, TimeMachineUnavailable, Verdict, host_allowed
from ..credentials import cookie_header

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
SITE = "https://chzzk.naver.com"
CHANNEL_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# 기본값. 환경변수 LIVE_RECORDER_API_BASE·LIVE_RECORDER_ALLOWED_HOSTS·LIVE_RECORDER_USER_STATUS_URL이 이름 그대로 덮는다
DEFAULT_API_BASE = "https://api.chzzk.naver.com"
DEFAULT_ALLOWED_HOSTS = ("naver.com", "pstatic.net", "akamaized.net")
DEFAULT_USER_STATUS_URL = "https://comm-api.game.naver.com/nng_main/v1/user/getUserStatus"
COOKIE_NAMES = ("NID_AUT", "NID_SES")

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def valid_channel_id(value: str) -> bool:
    """채널 ID 자리에 URL이나 경로를 받지 않는다."""
    return bool(CHANNEL_ID_RE.match(value or ""))


def api_headers(cookie_values: dict[str, str] | None) -> dict[str, str]:
    """방송 상세 API 주소."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": SITE + "/",
        "Origin": SITE,
    }
    cookie = cookie_header(COOKIE_NAMES, cookie_values)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def stream_headers() -> dict[str, str]:
    """쿠키는 API 호스트에만 보낸다."""
    return {"User-Agent": USER_AGENT, "Referer": SITE + "/", "Origin": SITE}


def live_detail_url(api_base: str, channel_id: str) -> str:
    return f"{api_base}/service/v3/channels/{channel_id}/live-detail"


def time_machine_url(api_base: str, live_id: str) -> str:
    return f"{api_base}/service/v1/live/{live_id}/playback/time-machine"


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=KST)  # 실측: 시간대 없는 KST


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def pick_hls_path(media: Any) -> str | None:
    """`mediaId`가 HLS인 항목, 없으면 `protocol`이 HLS인 첫 항목."""
    if not isinstance(media, list):
        return None
    items = [m for m in media if isinstance(m, dict) and m.get("path")]
    for m in items:
        if m.get("mediaId") == "HLS":
            return str(m["path"])
    for m in items:
        if m.get("protocol") == "HLS":
            return str(m["path"])
    return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_tracks(media: Any) -> list[Track]:
    out: list[Track] = []
    if not isinstance(media, list):
        return out
    chosen = None
    for m in media:
        if isinstance(m, dict) and m.get("mediaId") == "HLS":
            chosen = m
            break
    if chosen is None:
        chosen = next((m for m in media if isinstance(m, dict) and m.get("protocol") == "HLS"), None)
    for t in (chosen or {}).get("encodingTrack") or []:
        if not isinstance(t, dict):
            continue
        out.append(
            Track(
                track_id=str(t.get("encodingTrackId") or ""),
                height=_int(t.get("videoHeight")),
                fps=_float(t.get("videoFrameRate")),
                bitrate=_int(t.get("videoBitRate")),
                audio_only=bool(t.get("audioOnly")),
                codec=codec_mod.from_name(t.get("videoCodec")),
            )
        )
    return out


def parse_live_detail(status_code: int, payload: Any) -> CheckResult:
    """방송 상세 응답을 확인 결과로 바꾼다."""
    if status_code in (404, 410):
        return CheckResult(Verdict.OFFLINE, detail=f"http {status_code}")
    if status_code != 200 or not isinstance(payload, dict):
        return CheckResult(Verdict.UNKNOWN, detail=f"http {status_code}")
    content = payload.get("content")
    if content is None and "content" in payload:
        # 방송한 적 없는 채널. 방송 중이 아닌 것으로 보고 오류로 세지 않는다
        return CheckResult(Verdict.OFFLINE, detail="no broadcast yet")
    if not isinstance(content, dict) or not content:
        return CheckResult(Verdict.UNKNOWN, detail="empty content")
    status = content.get("status")
    if status != "OPEN":
        if status == "CLOSE":
            return CheckResult(Verdict.OFFLINE, detail="status CLOSE")
        return CheckResult(Verdict.UNKNOWN, detail=f"status {status!r}")
    playback = _as_dict(content.get("livePlaybackJson"))
    live = playback.get("live") if isinstance(playback.get("live"), dict) else {}
    started = _parse_dt(live.get("open")) or _parse_dt(live.get("start")) or _parse_dt(content.get("openDate"))
    live_id = content.get("liveId")
    if live_id in (None, ""):
        return CheckResult(Verdict.UNKNOWN, detail="OPEN without liveId")
    tma = content.get("timeMachineActive")
    meta = playback.get("meta") if isinstance(playback.get("meta"), dict) else {}
    meta_live_id = meta.get("liveId")
    info = LiveInfo(
        live_id=str(live_id),
        title=str(content.get("liveTitle") or "").strip(),
        category=str(content.get("liveCategoryValue") or ""),
        adult=bool(content.get("adult")),
        open_date=str(content.get("openDate") or ""),
        image_url=str(content.get("liveImageUrl") or ""),
        started_at=started,
        tracks=parse_tracks(playback.get("media")),
        hls_url=pick_hls_path(playback.get("media")),
        time_machine_active=tma if isinstance(tma, bool) else None,
        playback_live_id=str(meta_live_id) if meta_live_id not in (None, "") else None,
    )
    return CheckResult(Verdict.LIVE, info=info)


def parse_time_machine(status_code: int, payload: Any) -> str:
    """마스터 재생목록 주소를 돌려준다. 없으면 ApiError."""
    if status_code != 200 or not isinstance(payload, dict):
        raise ApiError(f"time-machine http {status_code}")
    content = payload.get("content")
    if not isinstance(content, dict):
        raise ApiError("time-machine: empty content")
    playback = _as_dict(content.get("playback"))
    path = pick_hls_path(playback.get("media"))
    if not path:
        raise ApiError("time-machine: no HLS media")
    return path


def cache_buster() -> dict[str, str]:
    return {"_": str(int(time.time() * 1000))}


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    first_delay: float = 0.7
    factor: float = 1.8
    connect_timeout: float = 3.0
    read_timeout: float = 8.0


class ChzzkClient:
    """코어용 비동기 클라이언트. 이벤트 루프를 막지 않는다."""

    def __init__(self, api_base: str, allowed_hosts: tuple[str, ...], allow_http: bool = False, retry: RetryPolicy | None = None,
                 user_status_url: str = DEFAULT_USER_STATUS_URL):
        self.api_base = api_base.rstrip("/")
        self.user_status_url = user_status_url
        self.allowed_hosts = allowed_hosts
        self.allow_http = allow_http
        self.retry = retry or RetryPolicy()
        timeout = httpx.Timeout(self.retry.read_timeout, connect=self.retry.connect_timeout)
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def lookup_channel(self, query: str, credentials: dict[str, str] | None) -> tuple[str, str]:
        """접점. 치지직 채널은 주소나 ID로만 받으므로 조회할 일이 없다."""
        raise ChannelParseError("치지직 채널 주소나 32자리 채널 ID가 아닙니다")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(self, url: str, cookie_values: dict[str, str] | None) -> tuple[int, Any]:
        if not host_allowed(url, self.allowed_hosts, self.allow_http):
            raise ApiError("허용하지 않는 호스트")
        delay = self.retry.first_delay
        last: tuple[int, Any] | None = None
        error: Exception | None = None
        for attempt in range(self.retry.attempts):
            if attempt:
                await asyncio.sleep(delay)
                delay *= self.retry.factor
            try:
                resp = await httpread.aget(self._client, url, MAX_RESPONSE, params=cache_buster(), headers=api_headers(cookie_values))
            except httpx.HTTPError as e:
                error = e
                continue
            try:
                body = resp.json()
            except ValueError:
                body = None
            last = (resp.status_code, body)
            error = None
            if resp.status_code not in RETRY_STATUS:
                break
        if last is None:
            raise ApiError(f"network: {type(error).__name__}")
        return last

    async def get_json(self, url: str) -> tuple[int, Any]:
        """쿠키 없이 공개 API를 부른다(채널 다시보기 목록)."""
        return await self._get_json(url, None)

    async def live_detail(self, channel_id: str, cookie_values: dict[str, str] | None = None) -> CheckResult:
        if not valid_channel_id(channel_id):
            return CheckResult(Verdict.UNKNOWN, detail="bad channel id")
        try:
            status, body = await self._get_json(live_detail_url(self.api_base, channel_id), cookie_values)
        except ApiError as e:
            return CheckResult(Verdict.UNKNOWN, detail=str(e))
        return parse_live_detail(status, body)

    async def user_status(self, url: str, cookie_values: dict[str, str] | None) -> str:
        """쿠키 확인. 'missing' | 'valid' | 'expired' | 'failed'."""
        if not cookie_header(COOKIE_NAMES, cookie_values):
            return "missing"
        try:
            status, body = await self._get_json(url, cookie_values)
        except ApiError:
            return "failed"
        if status in (401, 403):
            return "expired"
        content = body.get("content") if isinstance(body, dict) else None
        if status == 200 and isinstance(content, dict):
            if content.get("loggedIn") is True and content.get("userIdHash"):
                return "valid"
            if content.get("loggedIn") is False:
                return "expired"  # 200이지만 로그인 상태가 아니다
        return "failed"

    # ---- 접점(PlatformClient) ----
    async def check_live(self, channel_id: str, credentials: dict[str, str] | None) -> CheckResult:
        return await self.live_detail(channel_id, credentials)

    async def check_credentials(self, credentials: dict[str, str] | None) -> str:
        return await self.user_status(self.user_status_url, credentials)

    async def playback(self, channel_id: str, info: LiveInfo, credentials: dict[str, str] | None, *,
                       timemachine: bool) -> Playback:
        """타임머신이면 타임머신 재생 정보, 아니면 방송 확인 결과의 재생 주소. 갱신 방법은 수신기가 같은 API를 다시 부른다."""
        if timemachine:
            try:
                master = await self.time_machine(info.live_id, credentials)
            except ApiError as e:
                raise TimeMachineUnavailable(str(e)) from None
            return Playback(master, "timemachine", {"kind": "timemachine", "url": time_machine_url(self.api_base, info.live_id)})
        return Playback(info.hls_url or "", "live", {"kind": "live", "url": live_detail_url(self.api_base, channel_id)})

    async def time_machine(self, live_id: str, cookie_values: dict[str, str] | None = None) -> str:
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", live_id or ""):
            raise ApiError("bad live id")
        status, body = await self._get_json(time_machine_url(self.api_base, live_id), cookie_values)
        url = parse_time_machine(status, body)
        if not host_allowed(url, self.allowed_hosts, self.allow_http):
            raise ApiError("time-machine: 허용하지 않는 호스트")
        return url
