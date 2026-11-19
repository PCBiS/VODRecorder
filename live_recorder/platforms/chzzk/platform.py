"""치지직 플랫폼 모듈의 접점 객체. 3절의 요구(REC-)는 이 모듈의 요구다.

채널 주소 인식, 썸네일 주소 규칙, 쿠키 이름, 쿠키 확인 주소, 주소 갱신과
조각 토큰 인자가 모두 여기 있다. 밖에서는 `PLATFORM`으로만 쓴다.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ...core import httpread

from ..base import ApiError, ChannelParseError, CredentialSpec, Features, Verdict, host_allowed
from .. import cookiefile
from . import api

# 썸네일 주소의 `{type}` 자리에 넣을 크기 값(480이 200·image/jpeg로 답한다)
THUMB_TYPE: str | None = "480"
THUMB_HOSTS = ("pstatic.net", "akamaized.net")  # 실측: 라이브 썸네일은 *.akamaized.net. 다시보기 썸네일은 *.pstatic.net

# 채널 주소로 받는 호스트. `/live/<ID>`와 `/<ID>`
CHANNEL_HOSTS = ("chzzk.naver.com", "www.chzzk.naver.com", "m.chzzk.naver.com")
# 공개 문서(streamlink 치지직 플러그인)에 있는 조각 토큰 인자. 마스터 주소의 `hdnts` 값을 조각 요청에 `__bgda__`로 붙인다
MASTER_TOKEN_PARAM = "hdnts"
SEGMENT_TOKEN_PARAM = "__bgda__"


def thumbnail_url(raw: str | None) -> str | None:
    if not raw:
        return None
    url = raw
    if "{type}" in url:
        if THUMB_TYPE is None:
            return None
        url = url.replace("{type}", THUMB_TYPE)
    if "{" in url or "}" in url or not host_allowed(url, THUMB_HOSTS):
        return None  # 모르는 자리표시나 허용하지 않은 호스트
    return url


def parse_channel(text: str) -> str:
    """32자리 채널 ID와 치지직 주소(`https://` 없어도 됨, `www.`·`m.`, `/live/<ID>`와 `/<ID>`)를 받는다."""
    value = str(text or "").strip()
    if api.CHANNEL_ID_RE.match(value.lower()):
        return value.lower()
    candidate = value if re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I) else "https://" + value
    try:
        parts = urlsplit(candidate)
    except ValueError:
        raise ChannelParseError("치지직 채널 주소나 32자리 채널 ID가 아닙니다") from None
    if parts.scheme.lower() not in ("https", "http") or (parts.hostname or "").lower() not in CHANNEL_HOSTS:
        raise ChannelParseError("치지직 채널 주소나 32자리 채널 ID가 아닙니다")
    segs = [s for s in parts.path.split("/") if s]
    cid = segs[1] if segs and segs[0] == "live" and len(segs) > 1 else (segs[0] if segs else "")
    if not api.CHANNEL_ID_RE.match(cid.lower()):
        raise ChannelParseError("치지직 채널 주소나 32자리 채널 ID가 아닙니다")
    return cid.lower()


class ChzzkPlatform:
    id = "chzzk"
    name = "치지직"
    features = Features(timemachine=True, rewind=True)
    credential_spec = CredentialSpec(
        names=api.COOKIE_NAMES, title="치지직 쿠키",
        help="성인 방송처럼 로그인이 필요한 방송에 씁니다. 저장한 값은 화면에 다시 나오지 않습니다.", checkable=True)
    thumbnail_hosts = THUMB_HOSTS
    channel_hint = "https://chzzk.naver.com/live/… 또는 32자리 ID"
    channel_id_error = "채널 ID는 치지직 채널 주소의 32자리 UID입니다"
    # 화면이 서버에 묻지 않고 주소를 해석할 수 있게 내놓는 규칙
    channel_rule = {"id_pattern": "^[0-9a-f]{32}$", "hosts": list(CHANNEL_HOSTS), "path_prefix": "live",
                    "error": "치지직 채널 주소(https://chzzk.naver.com/live/…)나 32자리 채널 ID가 아닙니다"}
    # 로그 내보내기의 가리기: 쿠키 이름 꼴, 주소의 서명 질의 이름(CDN 토큰), 방송 ID 키(정규식 조각)
    log_mask = {"cookie_patterns": (r"NID_[A-Z]+",), "query_params": ("__gda__", MASTER_TOKEN_PARAM, "hdntl"),
                "live_id_keys": ("liveId",),
                # 주소 경로에 든 서명 조각(`/hdntl=…/`)
                "path_signatures": ("hdntl", "hdnts", "__gda__", "__bgda__")}

    min_check_interval = 0.0  # 전역 확인 주기를 그대로 쓴다
    tool_names: tuple[str, ...] = ()  # 외부 도구를 쓰지 않는다
    opt_in = False  # 늘 켜져 있다
    url_refresh_max = 0.0  # 전역 설정을 그대로 쓴다

    def parse_channel(self, text: str) -> str:
        return parse_channel(text)

    def clean_credential_file(self, text: str) -> str:
        raise ValueError("치지직 쿠키는 이름 붙은 값으로 넣습니다")

    def credential_summary(self, values: dict[str, str]) -> dict[str, Any]:
        return {}

    def tool_versions(self) -> dict[str, str]:
        return {}

    def pick_credentials(self, text: str) -> tuple[dict[str, str], dict[str, Any]]:
        """네이버 도메인의 NID_AUT·NID_SES."""
        cookies = cookiefile.parse(text)
        chosen = cookiefile.pick(cookies, ("naver.com",), api.COOKIE_NAMES)
        return ({k: c.value for k, c in chosen.items()},
                {"names": list(chosen), "expires": cookiefile.earliest_expiry(chosen.values())})

    def enable_blocked(self) -> str | None:
        return None

    def opt_in_notice(self, cfg: Any) -> dict[str, Any]:
        return {}

    def prepare_enable(self, cfg: Any) -> None:
        pass

    def startup(self, cfg: Any) -> None:
        pass

    def tool_status(self, cfg: Any) -> list[dict[str, Any]]:
        return []

    def tool_action(self, cfg: Any, name: str, action: str) -> dict[str, Any]:
        raise KeyError(name)

    def valid_channel_id(self, value: str) -> bool:
        return api.valid_channel_id(value)

    def allowed_hosts(self, cfg: Any) -> tuple[str, ...]:
        return tuple(getattr(cfg, "allowed_hosts", None) or api.DEFAULT_ALLOWED_HOSTS)

    def api_base(self, cfg: Any) -> str:
        return (getattr(cfg, "api_base", None) or api.DEFAULT_API_BASE).rstrip("/")

    def request_headers(self, kind: str, credentials: dict[str, str] | None) -> dict[str, str]:
        return api.api_headers(credentials) if kind == "api" else api.stream_headers()

    def thumbnail_url(self, raw: str | None) -> str | None:
        return thumbnail_url(raw)

    def client(self, cfg: Any) -> api.ChzzkClient:
        return api.ChzzkClient(self.api_base(cfg), self.allowed_hosts(cfg), bool(getattr(cfg, "allow_http", False)),
                               user_status_url=getattr(cfg, "user_status_url", None) or api.DEFAULT_USER_STATUS_URL)

    # ---- 수신기 프로세스 쪽 ----
    def refresh_stream(self, http: httpx.Client, spec: dict[str, Any], credentials: dict[str, str] | None) -> str:
        """타임머신이면 타임머신 API, 아니면 방송 확인 API를 다시 불러 새 마스터 주소를 받는다."""
        kind, url = spec.get("kind"), spec.get("url")
        resp = httpread.get(http, url, api.MAX_RESPONSE, params=api.cache_buster(), headers=api.api_headers(credentials))
        try:
            body = resp.json()
        except ValueError:
            body = None
        if kind == "timemachine":
            return api.parse_time_machine(resp.status_code, body)
        result = api.parse_live_detail(resp.status_code, body)
        if result.verdict is not Verdict.LIVE or not result.info or not result.info.hls_url:
            raise ApiError(f"live-detail: {result.verdict.value} {result.detail}")
        return result.info.hls_url

    def segment_token(self, master_url: str) -> tuple[str, str] | None:
        token = parse_qs(urlsplit(master_url).query).get(MASTER_TOKEN_PARAM)
        return (SEGMENT_TOKEN_PARAM, token[0]) if token else None


PLATFORM = ChzzkPlatform()
