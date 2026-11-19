"""씨미(ci.me) 플랫폼 모듈의 접점 객체. 14절의 요구는 이 모듈의 요구다.

- 방송 감지는 공식 공개 API(`live-status`), 재생 주소는 방송 화면에서 읽는다.
- 채널은 숫자 채널 ID로 저장하고, 방송 화면 주소에 필요한 핸들은 이 모듈이 데이터 폴더의 작은 표(`cime-channels.json`)에
  함께 둔다. 방송 화면을 읽을 때마다 새 값으로 고친다.
- 쿠키는 ci.me로 가는 화면 요청(방송 화면·채널 화면)에만 싣는다. 상태 API와 재생목록·조각에는 싣지 않는다.
밖에서는 `PLATFORM`으로만 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from ...core import httpread

from ...core.quality import Track
from ..base import (ApiError, BroadcastChanged, ChannelLookupNeeded, ChannelParseError, CheckResult, CredentialSpec,
                    Features, LiveInfo, Playback, Verdict, host_allowed)
from .. import cookiefile
from . import page

DEFAULT_BASE = "https://ci.me"
# 시험 전용: 가짜 서버 주소(수신기 프로세스도 같은 환경변수를 받는다)
BASE_ENV = "LIVE_RECORDER_CIME_BASE"
# 이 환경변수가 off(또는 0·false·no)면 씨미를 켤 수 없다
ENV_FORBID = "LIVE_RECORDER_CIME"
STATUS_PATH = "/api/openapi/v1/{}/live-status"
PAGE_PATH = "/@{}/live"
SITE_HOSTS = ("ci.me", "www.ci.me")
STREAM_HOSTS = ("live-video.net",)  # 마스터 `*.playback.live-video.net`, 조각 `*.hls.live-video.net`
THUMB_HOSTS = ("cf.ci.me",)  # 썸네일 streaming.cf.ci.me(CSP의 *.cf.ci.me가 그 호스트를 덮는다)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
CHANNEL_ID_RE = re.compile(r"^[0-9]{1,20}$")
HANDLE_RE = re.compile(r"^@[A-Za-z0-9_.-]{1,64}$")
MAX_JSON = 256 * 1024
MAX_MASTER = 256 * 1024
COOKIE_NAME = "mauth-authorization-code"  # 이 값 하나로 연령 제한 방송의 주소가 채워졌다
NOT_FOUND_PAGE = "재생 정보를 찾지 못했습니다(씨미 화면이 바뀐 것으로 보입니다)"


def base() -> str:
    return (os.environ.get(BASE_ENV) or DEFAULT_BASE).rstrip("/")


def site_hosts() -> tuple[str, ...]:
    host = (urlsplit(base()).hostname or "").lower()
    return SITE_HOSTS + ((host,) if host and host not in SITE_HOSTS else ())


# ---- 채널 ----
def parse_channel(text: str) -> str:
    """`ci.me/@핸들`, `ci.me/@핸들/live`, `@핸들`. 핸들은 조회해야 채널 ID를 안다(ChannelLookupNeeded)."""
    value = str(text or "").strip()
    if HANDLE_RE.match(value):
        raise ChannelLookupNeeded(value)
    candidate = value if re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I) else "https://" + value
    try:
        parts = urlsplit(candidate)
    except ValueError:
        raise ChannelParseError(PLATFORM.channel_rule["error"]) from None
    if parts.scheme.lower() not in ("https", "http") or (parts.hostname or "").lower() not in SITE_HOSTS:
        raise ChannelParseError(PLATFORM.channel_rule["error"])
    segs = [unquote(s) for s in parts.path.split("/") if s]
    if segs and HANDLE_RE.match(segs[0]) and (len(segs) == 1 or segs[1:] == ["live"]):
        raise ChannelLookupNeeded(segs[0])
    raise ChannelParseError(PLATFORM.channel_rule["error"])


class Handles:
    """채널 ID → 핸들 표. 데이터 폴더의 0600 파일(비밀이 아니다)."""

    def __init__(self, data_dir: Path | None):
        self.path = Path(data_dir) / "cime-channels.json" if data_dir else None

    def read(self) -> dict[str, str]:
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def get(self, channel_id: str) -> str | None:
        return self.read().get(str(channel_id))

    def set(self, channel_id: str, handle: str) -> None:
        if self.path is None or not handle:
            return
        data = self.read()
        if data.get(str(channel_id)) == handle:
            return
        data[str(channel_id)] = handle
        tmp = self.path.with_name(self.path.name + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.path)


# ---- 자격 ----
def auth_value(credentials: dict[str, str] | None) -> str | None:
    value = (credentials or {}).get(COOKIE_NAME)
    return value if value and not any(c in value for c in ';,\r\n" ') else None


def cookie_header(credentials: dict[str, str] | None) -> str | None:
    """ci.me로 가는 화면 요청의 Cookie 머리글(값 하나)."""
    value = auth_value(credentials)
    return f"{COOKIE_NAME}={value}" if value else None


def token_expiry(value: str) -> str | None:
    """JWT 꼴 값의 내용에서 만료 예상일: `iat`(밀리초) + `duration.days`. 서명은 검증하지 않는다. 꼴이 다르면 None."""
    import base64

    try:
        part = value.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        iat = float(data["iat"])
        days = float(data["duration"]["days"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None
    if iat <= 0 or days <= 0:
        return None
    try:
        return datetime.fromtimestamp(iat / 1000 + days * 86400, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# ---- 응답 해석 ----
def page_headers(credentials: dict[str, str] | None) -> dict[str, str]:
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}
    cookie = cookie_header(credentials)
    if cookie:
        headers["Cookie"] = cookie  # 화면 요청에만
    return headers


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_master(text: str) -> list[Track]:
    """마스터 재생목록의 화질(높이·대역폭·프레임 수). 본문은 로그에 남기지 않는다(USER-IP)."""
    tracks = []
    for n, line in enumerate(text.splitlines()):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        bw = re.search(r"(?<![-A-Z])BANDWIDTH=(\d+)", line)
        fr = re.search(r"FRAME-RATE=([0-9.]+)", line)
        if res:
            tracks.append(Track(f"v{n}", int(res.group(2)), float(fr.group(1)) if fr else None,
                                int(bw.group(1)) if bw else None))
    return tracks


def playback_problem(obj: dict[str, Any], has_cookies: bool) -> str | None:
    """방송 객체는 있는데 주소가 비었거나 볼 권한이 막혔다: "needed" | "rejected"."""
    pb = obj.get("playback") if isinstance(obj.get("playback"), dict) else {}
    url = pb.get("url")
    access = obj.get("viewAccess")
    blocked = isinstance(access, dict) and access.get("canView") is False or access in (False, "DENIED", "BLOCKED")
    if url and not blocked:
        return None
    return "rejected" if has_cookies else "needed"


class CimeClient:
    """코어용 비동기 클라이언트."""

    def __init__(self, cfg: Any):
        self.handles = Handles(getattr(cfg, "data_dir", None))
        self.allow_http = bool(getattr(cfg, "allow_http", False))
        self.hosts = PLATFORM.allowed_hosts(cfg)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False)

    async def _get(self, url: str, headers: dict[str, str], limit: int) -> tuple[int, bytes]:
        if not host_allowed(url, self.hosts, self.allow_http):
            raise ApiError("허용하지 않는 호스트")
        try:
            resp = await httpread.aget(self._client, url, limit, headers=headers)  # 풀어 가며 센다
        except httpread.TooLarge:
            raise ApiError("응답이 너무 큽니다") from None
        return resp.status_code, resp.content

    async def read_page(self, handle: str, credentials: dict[str, str] | None) -> str:
        status, body = await self._get(base() + PAGE_PATH.format(handle.lstrip("@")), page_headers(credentials), page.MAX_PAGE)
        if status == 404:
            raise ChannelParseError("씨미 채널을 찾지 못했습니다")
        if status != 200:
            raise ApiError(f"씨미 화면 HTTP {status}")
        return body.decode("utf-8", "replace")

    async def check_live(self, channel_id: str, credentials: dict[str, str] | None) -> CheckResult:
        try:
            status, body = await self._get(base() + STATUS_PATH.format(channel_id), {"User-Agent": UA, "Accept": "application/json"},
                                           MAX_JSON)  # 상태 API에는 쿠키를 싣지 않는다
        except (httpx.HTTPError, ApiError) as e:
            return CheckResult(Verdict.UNKNOWN, detail=f"씨미: {type(e).__name__}")
        if status == 404:
            return CheckResult(Verdict.UNKNOWN, detail="씨미: 채널이 없습니다(404)")
        if status == 429:
            return CheckResult(Verdict.UNKNOWN, detail="씨미: 요청이 너무 많습니다(HTTP 429)", throttled=True)
        if status != 200:
            return CheckResult(Verdict.UNKNOWN, detail=f"씨미: 상태 API HTTP {status}")
        try:
            content = json.loads(body).get("content") or {}
        except (ValueError, AttributeError):
            return CheckResult(Verdict.UNKNOWN, detail="씨미: 상태 응답을 읽지 못했습니다")
        if not isinstance(content, dict) or not isinstance(content.get("isLive"), bool):
            return CheckResult(Verdict.UNKNOWN, detail="씨미: 상태 응답을 읽지 못했습니다")
        if not content["isLive"]:
            return CheckResult(Verdict.OFFLINE)
        handle = self.handles.get(channel_id)
        if not handle:
            return CheckResult(Verdict.UNKNOWN, detail="씨미: 채널 핸들을 몰라 방송 화면을 읽지 못했습니다(채널을 다시 추가해 주십시오)")
        try:
            text = await self.read_page(handle, credentials)
        except (httpx.HTTPError, ApiError, ChannelParseError) as e:
            return CheckResult(Verdict.UNKNOWN, detail=f"씨미: {e}"[:200])
        obj = await asyncio.to_thread(page.find_live, text, channel_id)  # 해석은 이벤트 루프 밖에서
        if obj is None or obj.get("state") != "ACTIVE":
            return CheckResult(Verdict.UNKNOWN, detail=NOT_FOUND_PAGE)
        slug = (obj.get("channel") or {}).get("slug")
        if isinstance(slug, str) and slug:
            await asyncio.to_thread(self.handles.set, channel_id, slug)  # 핸들이 바뀌었으면 고친다
        problem = playback_problem(obj, bool(cookie_header(credentials)))
        if problem:
            return CheckResult(Verdict.UNKNOWN, detail="씨미: 로그인이 필요한 방송", credential_problem=problem)
        url = obj["playback"]["url"]
        tracks: list[Track] = []
        try:
            status, master = await self._get(url, PLATFORM.request_headers("stream", None), MAX_MASTER)
            if status == 200:
                tracks = parse_master(master.decode("utf-8", "replace"))
        except (httpx.HTTPError, ApiError):
            tracks = []
        started = _parse_time(obj.get("openedAt")) or _parse_time(content.get("openedAt")) or datetime.now(timezone.utc)
        live_id = str(obj.get("id") or "")
        info = LiveInfo(live_id=live_id, title=str(obj.get("title") or content.get("title") or ""), category="",
                        adult=bool(obj.get("isAdult")), open_date=started.isoformat(), image_url=str(obj.get("imageUrl") or ""),
                        started_at=started, tracks=tracks or [Track("best", 1080, None, None)], hls_url=url,
                        playback_live_id=live_id)
        return CheckResult(Verdict.LIVE, info)

    async def playback(self, channel_id: str, info: LiveInfo, credentials: dict[str, str] | None, *,
                       timemachine: bool) -> Playback:
        handle = self.handles.get(channel_id) or ""
        return Playback(info.hls_url or "", "live", {"channel_id": channel_id, "handle": handle, "live_id": info.live_id})

    async def check_credentials(self, credentials: dict[str, str] | None) -> str:
        return "valid" if cookie_header(credentials) else "missing"

    async def lookup_channel(self, query: str, credentials: dict[str, str] | None) -> tuple[str, str]:
        """채널 화면을 한 번 읽어 채널 ID와 이름으로 바꾼다."""
        handle = query.lstrip("@")
        try:
            text = await self.read_page(handle, credentials)
        except httpx.HTTPError as e:
            raise ApiError(f"씨미: {type(e).__name__}") from None
        ch = await asyncio.to_thread(page.find_channel, text, handle)
        if ch is None or not CHANNEL_ID_RE.match(str(ch.get("id"))):
            raise ChannelParseError(f"@{handle} 씨미 채널을 찾지 못했습니다")
        cid = str(ch["id"])
        await asyncio.to_thread(self.handles.set, cid, str(ch.get("slug") or handle))
        return cid, str(ch.get("name") or handle)

    async def aclose(self) -> None:
        await self._client.aclose()


NOTICE_LINES = [
    "씨미 서비스 이용약관 제11조는 서비스 이용 중 녹화를 하거나 시도하는 것을 부정 이용행위로 보고, 사전 통지 없이 "
    "계약 해지나 이용 제한을 할 수 있다고 정하고 있습니다. 이 기능은 약관이 금지하는 이용에 해당할 수 있습니다.",
    "로그인 쿠키를 넣어 쓰면 그 계정이 제재될 수 있습니다. 약관 제50조에 따라 같은 명의의 모든 계정에 일괄 조치될 수 있습니다.",
    "이 기능을 써서 생기는 책임은 쓰는 사람에게 있습니다.",
    "방송 확인은 씨미의 공식 공개 API로 하고, 재생 주소는 방송 화면에서 읽습니다. 외부 도구는 쓰지 않으며 동의하면 바로 켜집니다.",
]


class CimePlatform:
    id = "cime"
    name = "씨미"
    features = Features(timemachine=False, rewind=False, live_rewind=False, live_resume=True,
                        fresh_master=True)
    credential_spec = CredentialSpec(
        names=(COOKIE_NAME,), title="씨미 쿠키", kind="values", checkable=False, summary=True,
        help=("연령 제한 방송처럼 로그인이 필요한 방송에만 씁니다. 씨미에 로그인한 브라우저의 개발자 도구에서 ci.me의 "
              "mauth-authorization-code 쿠키 값을 복사해 넣으십시오. 로그인 쿠키를 쓰면 그 계정이 제재될 수 있습니다. "
              "저장한 값은 화면에 다시 나오지 않고 저장 시각과 만료 예상일만 보입니다."))
    thumbnail_hosts = THUMB_HOSTS
    channel_hint = "https://ci.me/@핸들 또는 @핸들"
    channel_id_error = "씨미 채널 ID는 숫자입니다"
    channel_rule = {"id_pattern": "^(?!)$", "hosts": list(SITE_HOSTS), "path_prefix": "", "case_sensitive": True,
                    "lookup_pattern": "^@[A-Za-z0-9_.-]{1,64}$",
                    # 채널 ID만으로는 추가하지 않는다(방송 화면 주소에 핸들이 필요하다)
                    "lookup_only": True, "lookup_only_error": "씨미 채널은 주소나 @핸들로 추가해 주십시오",
                    "error": "씨미 채널 주소(ci.me/@핸들)나 @핸들이 아닙니다"}
    # 재생목록의 USER-IP 값, 재생 주소의 경로 값(호스트와 꼴만), 방송 ID(더 가리기에서만, liveId·live_id 키)
    # 다시보기·클립의 서명 쿠키(CloudFront-Policy·Signature·Key-Pair-Id)
    log_mask = {"cookie_patterns": (r"CloudFront-(?:Policy|Signature|Key-Pair-Id)", r"mauth-authorization-code"), "query_params": ("token",),
                "attr_params": ("USER-IP",), "path_hosts_full": STREAM_HOSTS, "live_id_keys": ()}
    min_check_interval = 30.0
    url_refresh_max = 0.0
    tool_names: tuple[str, ...] = ()  # 외부 도구 없음(자체 점검 없음)
    opt_in = True

    def parse_channel(self, text: str) -> str:
        return parse_channel(text)

    def valid_channel_id(self, value: str) -> bool:
        return isinstance(value, str) and bool(CHANNEL_ID_RE.match(value))

    def allowed_hosts(self, cfg: Any) -> tuple[str, ...]:
        return site_hosts() + STREAM_HOSTS

    def request_headers(self, kind: str, credentials: dict[str, str] | None) -> dict[str, str]:
        if kind == "stream":
            return {"User-Agent": UA, "Origin": "https://ci.me", "Referer": "https://ci.me/"}  # 쿠키 없음
        return {"User-Agent": UA}

    def thumbnail_url(self, raw: str | None) -> str | None:
        return raw if raw and host_allowed(raw, THUMB_HOSTS) else None

    def client(self, cfg: Any) -> CimeClient:
        return CimeClient(cfg)

    def clean_credential_file(self, text: str) -> str:
        raise ValueError("씨미 쿠키는 이름 붙은 값으로 넣습니다")

    def credential_summary(self, values: dict[str, str]) -> dict[str, Any]:
        value = (values or {}).get(COOKIE_NAME)
        if not value:
            return {}
        expires = token_expiry(value)
        return {"expires": expires, "expires_note": None if expires else "만료일을 알 수 없습니다"}

    def tool_versions(self) -> dict[str, str]:
        return {}

    def pick_credentials(self, text: str) -> tuple[dict[str, str], dict[str, Any]]:
        """ci.me 도메인의 mauth-authorization-code. 값 검사는 직접 넣을 때와 같다(auth_value)."""
        chosen = cookiefile.pick(cookiefile.parse(text), ("ci.me",), (COOKIE_NAME,))
        value = chosen[COOKIE_NAME].value
        if not auth_value({COOKIE_NAME: value}):
            raise ValueError(f"파일의 {COOKIE_NAME} 값에 쓸 수 없는 문자가 있습니다")
        return {COOKIE_NAME: value}, {"names": [COOKIE_NAME], "expires": cookiefile.earliest_expiry(chosen.values())}

    def enable_blocked(self) -> str | None:
        if os.environ.get(ENV_FORBID, "").strip().lower() in ("off", "0", "false", "no"):
            return "이 서버에서는 씨미를 켤 수 없게 해 두었습니다(환경변수)"
        return None

    def opt_in_notice(self, cfg: Any) -> dict[str, Any]:
        return {
            "title": "씨미 사용", "enable_label": "씨미 사용", "disable_label": "씨미 끄기",
            "off_label": "씨미가 꺼져 있습니다", "need_enable": "씨미를 쓰려면 설정에서 먼저 켜 주십시오",
            "lines": list(NOTICE_LINES), "agree_label": "약관을 읽었고 동의하고 켭니다",
            "disable_confirm": "씨미를 끕니다. 씨미 채널의 감시를 멈추고 채널은 남겨 둡니다. 쿠키는 지우지 않습니다. 끌까요?",
            "blocked": self.enable_blocked(),
        }

    def prepare_enable(self, cfg: Any) -> None:
        why = self.enable_blocked()
        if why:
            raise ValueError(why)  # 외부 도구가 없으므로 자체 점검은 없다

    def startup(self, cfg: Any) -> None:
        pass

    def tool_status(self, cfg: Any) -> list[dict[str, Any]]:
        return []

    def tool_action(self, cfg: Any, name: str, action: str) -> dict[str, Any]:
        raise KeyError(name)

    # ---- 수신기 프로세스 쪽 ----
    def refresh_stream(self, http: httpx.Client, spec: dict[str, Any], credentials: dict[str, str] | None) -> str:
        """방송 화면을 다시 읽어 새 주소를 받는다. 방송 ID가 바뀌었으면 BroadcastChanged."""
        handle = str(spec.get("handle") or "")
        if not handle:
            raise ApiError("씨미: 핸들을 모릅니다")
        resp = httpread.get(http, base() + PAGE_PATH.format(handle), page.MAX_PAGE, headers=page_headers(credentials))
        if resp.status_code != 200:
            raise ApiError(f"씨미 화면 HTTP {resp.status_code}")
        obj = page.find_live(resp.text, str(spec.get("channel_id")))
        if obj is None or obj.get("state") != "ACTIVE":
            raise ApiError("씨미: 방송 중이 아닙니다")
        if spec.get("live_id") and str(obj.get("id")) != str(spec["live_id"]):
            raise BroadcastChanged("씨미: 방송 ID가 바뀌었습니다")
        url = (obj.get("playback") or {}).get("url")
        if not url:
            raise ApiError("씨미: 재생 주소가 비었습니다")
        return url

    def segment_token(self, master_url: str) -> tuple[str, str] | None:
        return None


PLATFORM = CimePlatform()
