"""씨미 다시보기·클립의 공통 부분 — cime_media 확장의 씨미 전용 모듈.

정보 API, 서명 쿠키 받기와 갱신, 주소 인식, 파일 이름. 받는 일은 downloads 확장의 `media`가 한다.
이 모듈은 수신 프로세스(downloads의 worker) 안에서 불린다. 로그인 쿠키(`mauth-authorization-code`)는 ci.me의 API 요청에만,
서명 쿠키(`CloudFront-*`)는 그 영상의 파일 호스트·경로 요청에만 싣고 메모리에만 둔다.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from ...core import httpread

from ...core import naming
from ...platforms.cime import platform as cmp
from ..downloads import media
from ..downloads.media import Failure

FILE_HOSTS = ("streaming.cf.ci.me",)
INFO_PATH = {"vod": "/api/app/videos/{}", "clip": "/api/app/clips/{}"}
SIGN_PATH = "/api/app/streams/sign-cookie"
SIGN_TYPE = {"vod": ("VIDEO", "videoId"), "clip": ("CLIP", "clipId")}
SIGN_NAMES = ("CloudFront-Policy", "CloudFront-Signature", "CloudFront-Key-Pair-Id")
MAX_JSON = 2 * 1024 * 1024
ID_RE = re.compile(r"[0-9]{1,20}\Z")
HANDLE_RE = re.compile(r"@[A-Za-z0-9_.-]{1,64}\Z")


def parse_parts(value: Any) -> tuple[str, list[str]] | None:
    """ci.me 주소면 (원래 글, 경로 조각). 아니면 invalid_url."""
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\x00-\x20\\]", value.strip()):
        raise Failure("invalid_url")
    text = value.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        raise Failure("invalid_url") from None
    if parts.scheme.lower() not in ("https", "http") or parts.username or parts.password \
            or (parts.hostname or "").lower() not in cmp.SITE_HOSTS:
        raise Failure("invalid_url")
    return text, [s for s in parts.path.split("/") if s]


def site_headers(cookies: dict[str, str] | None, referer: str) -> dict[str, str]:
    """ci.me API 요청 머리글. 로그인 쿠키는 여기에만 싣는다."""
    headers = {"User-Agent": cmp.UA, "Origin": "https://ci.me", "Referer": referer, "Accept": "application/json"}
    cookie = cmp.cookie_header(cookies)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _api_url(path: str) -> str:
    url = cmp.base() + path
    if not media.host_ok(url) and not _site_ok(url):
        raise Failure("protocol_denied")
    return url


def _site_ok(url: str) -> bool:
    return cmp.host_allowed(url, cmp.site_hosts(), media.ALLOW_LOCAL_HTTP)


def setup() -> None:
    """이 프로세스의 요청 규칙: 파일 호스트(streaming.cf.ci.me)와 머리글(서명 쿠키는 따로 더한다)."""
    extra = tuple(h for h in cmp.site_hosts() if h not in cmp.SITE_HOSTS)  # 시험의 가짜 서버
    media.configure(FILE_HOSTS + extra, {"User-Agent": cmp.UA, "Origin": "https://ci.me", "Referer": "https://ci.me/"})


def get_info(kind: str, ident: str, cookies: dict[str, str] | None, referer: str) -> dict[str, Any]:
    """정보 API. 404는 not_found, 로그인이 필요하면 login_required/login_rejected."""
    url = _api_url(INFO_PATH[kind].format(ident))
    with media.http_client(20.0) as client:
        try:
            resp = httpread.get(client, url, MAX_JSON, headers=site_headers(cookies, referer))
        except httpread.TooLarge:
            raise Failure("engine_operation_failed") from None
        except httpx.HTTPError as e:
            raise Failure(media.classify(e)) from None
    if resp.status_code == 404:
        raise Failure("not_found")
    if resp.status_code in (401, 403):
        raise Failure("login_rejected" if cmp.cookie_header(cookies) else "login_required")
    if resp.status_code != 200 or len(resp.content) > MAX_JSON:
        raise Failure("engine_operation_failed")
    try:
        body = resp.json()
    except ValueError:
        raise Failure("engine_operation_failed") from None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise Failure("not_found")
    return data


def check_playable(data: dict[str, Any], cookies: dict[str, str] | None) -> str:
    """받을 수 있으면 재생 주소. 상태가 ACTIVE가 아니거나 주소가 없으면 받지 않는다."""
    pb = data.get("playback") if isinstance(data.get("playback"), dict) else {}
    url = pb.get("url")
    blocked = cmp.playback_problem({"playback": pb, "viewAccess": data.get("viewAccess")}, bool(cmp.cookie_header(cookies)))
    if blocked or (not url and data.get("isAdult")):
        raise Failure("login_rejected" if cmp.cookie_header(cookies) else "login_required")
    if data.get("state") not in (None, "ACTIVE") or not url:
        raise Failure("not_ready")
    return str(url)


class SignedCookies:
    """서명 쿠키. 받기 직전에 받고, `refreshAfterSec`(없으면 유효 시간의 60%)가 지나면 받는 중에 새로 받는다.
    media의 받기가 요청마다 `headers(주소)`를 부르고 403이면 `renew()`를 부른다. 값은 메모리에만 있다.
    수신 프로세스 안의 갱신은 요청 창구 밖이다(수신기 프로세스의 갱신과 같은 취급)."""

    def __init__(self, kind: str, ident: str, cookies: dict[str, str] | None, referer: str, clock=time.monotonic):
        self.kind, self.ident, self.cookies, self.referer = kind, ident, cookies, referer
        self.clock = clock
        self.lock = threading.Lock()
        self.values: dict[str, str] = {}
        self.path = "/"
        self.refresh_at = 0.0
        self.renewals = 0

    def renew(self) -> None:
        kind_name, key = SIGN_TYPE[self.kind]
        url = _api_url(SIGN_PATH)
        with media.http_client(20.0) as client:
            try:
                resp = httpread.get(client, url, MAX_JSON, method="POST",
                                    headers=dict(site_headers(self.cookies, self.referer), **{"Content-Type": "application/json"}),
                                    content=json.dumps({"type": kind_name, key: self.ident}))  # 응답 본문도 상한을 두고 읽는다
            except httpread.TooLarge:
                raise Failure("engine_operation_failed") from None
            except httpx.HTTPError as e:
                raise Failure(media.classify(e)) from None
        if resp.status_code in (401, 403):
            raise Failure("login_rejected" if cmp.cookie_header(self.cookies) else "login_required")
        if resp.status_code != 200:
            raise Failure("engine_operation_failed")
        values = {}
        for raw in resp.headers.get_list("set-cookie"):
            name, _, rest = raw.partition("=")
            if name.strip() in SIGN_NAMES:
                values[name.strip()] = rest.split(";", 1)[0].strip()
        try:
            data = (resp.json() or {}).get("data") or {}
        except ValueError:
            data = {}
        if set(values) != set(SIGN_NAMES):
            raise Failure("engine_operation_failed")
        ttl = float(data.get("ttlSec") or 900)
        after = float(data.get("refreshAfterSec") or ttl * 0.6)
        with self.lock:
            self.values = values
            self.path = str(data.get("cookiePath") or "/")
            self.refresh_at = self.clock() + max(1.0, after)
            self.renewals += 1

    def headers(self, url: str) -> dict[str, str]:
        """그 영상의 파일 호스트·경로 요청에만 서명 쿠키를 싣는다."""
        if not media.host_ok(url):
            return {}
        if not self.values or self.clock() >= self.refresh_at:
            self.renew()
        with self.lock:
            path = urlsplit(url).path
            if not path.startswith(self.path.rstrip("*")):
                return {}
            return {"Cookie": "; ".join(f"{k}={v}" for k, v in self.values.items())}


def kst(value: Any) -> str | None:
    """ISO 8601 시각을 한국 시각 `YYYY-MM-DD HH:MM:SS`로."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(naming.KST).strftime("%Y-%m-%d %H:%M:%S")


def filename(video: dict[str, Any]) -> str:
    """`[YYMMDD_hhmmss] {채널명} {제목}.mp4`(한국 시각)."""
    title = video.get("title") or video.get("video_id") or "cime"
    when = None
    if video.get("open_date"):
        try:
            when = datetime.strptime(video["open_date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=naming.KST)
        except ValueError:
            when = None
    when = when or datetime.now(naming.KST)
    return naming.render("[{broadcast_time}] {channel_name} {safe_live_title}{file_extension}", title=title,
                         channel_name=str(video.get("channel") or ""), quality="", frame_rate="", extension=".mp4",
                         broadcast_start=when, recording_start=when, now=when)


def thumbnail_ok(url: str) -> bool:
    return False
