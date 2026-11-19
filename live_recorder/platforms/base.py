"""플랫폼 접점. 녹화 중심부(감시 엔진·수신기·마무리)와 웹 서버·화면은 플랫폼을 이 파일의 형식으로만 안다.

플랫폼 모듈 하나가 내놓는 것:
- `id`, `name`: 식별자(채널 식별의 앞쪽 반, 파일 이름 토큰 `{platform}`)와 표시 이름
- `parse_channel(text)`: 주소나 ID에서 채널 ID를 뽑는다. 못 뽑으면 `ChannelParseError`(사람이 읽는 까닭)
- `valid_channel_id(value)`: 저장·API에 들어온 채널 ID가 이 플랫폼의 꼴인지
- `client(cfg)`: 방송 확인·재생 정보·자격 확인을 하는 비동기 클라이언트(`PlatformClient`)
- `features`: 선택 기능(타임머신·되감기)
- `credential_spec`: 필요한 자격의 이름·모양·확인 방법
- `allowed_hosts(cfg)`: 요청을 허용할 호스트(API·스트림). `thumbnail_hosts`: 화면이 그림을 불러도 되는 호스트
- `request_headers(kind, credentials)`: API(`"api"`)·스트림(`"stream"`) 요청 머리글
- `thumbnail_url(raw)`: 방송 확인 결과의 썸네일 주소를 화면이 쓸 주소로(허용 밖이면 None)
- `log_mask`: 로그 내보내기가 가릴 이 플랫폼의 이름(쿠키 이름 꼴, 서명 질의 이름, 방송 ID 키)
- `min_check_interval`: 이 플랫폼 채널의 가장 짧은 확인 간격(초). 엔진은 전역 확인 주기와 이 값 중 큰 쪽을 쓴다
- `url_refresh_max`: 재생 주소를 늦어도 이만큼 지나면 새로 받는다(초, 0이면 전역 설정만 따른다)
- 채널 조회: `parse_channel`이 `ChannelLookupNeeded`를 내면 웹 서버가 클라이언트의 `lookup_channel`로 한 번 조회해
  채널 ID와 이름으로 바꾼다
- 자격 "파일 글 전체" 꼴: `credential_spec.kind == "file"`이면 `clean_credential_file(text)`로 검사·정리하고
  `credential_summary(values)`로 화면에 보일 요약(값 없음)을 만든다
- 수신기(별도 프로세스) 쪽: `refresh_stream(http, spec, credentials)`, `segment_token(master_url)`.
  수신기는 작업에 적힌 플랫폼 모듈 이름으로 이 객체를 불러와 쓴다. 수신기 본체에는 플랫폼별 분기가 없다

플랫폼 모듈은 `PLATFORM`이라는 이름으로 이 접점을 만족하는 객체를 내놓고 `registry.register`로 등록한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from ..core.quality import Track


class Verdict(Enum):
    LIVE = "live"
    OFFLINE = "offline"
    UNKNOWN = "unknown"  # 판정 불가. 방송 종료로 취급하지 않는다


class ApiError(Exception):
    """플랫폼 API를 쓸 수 없다(연결 실패, 모르는 응답). 문장은 로그·상세에 쓰인다."""


class TimeMachineUnavailable(ApiError):
    """타임머신 재생 정보를 받지 못했다. 엔진은 일반 수신으로 바꾼다."""


class ChannelParseError(ValueError):
    """주소나 ID에서 채널을 뽑지 못했다. 문장은 사람이 읽는다."""


class ChannelLookupNeeded(ChannelParseError):
    """채널 ID가 아니라 이름(`@이름` 등)이다. 플랫폼 API로 한 번 조회해야 채널 ID를 안다."""

    def __init__(self, query: str, message: str = "채널을 조회해야 합니다"):
        super().__init__(message)
        self.query = query


def clean_text(value: Any) -> Any:
    """플랫폼에서 받은 글의 짝 없는 서러게이트(\\ud800 꼴)를 바꿈표(U+FFFD)로 바꾼다. 그대로 두면 저장·파일 이름에서 막힌다."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return "".join("\ufffd" if "\ud800" <= ch <= "\udfff" else ch for ch in value)
    return value


class BroadcastChanged(ApiError):
    """주소를 새로 받았더니 다른 방송이다. 수신기는 이 회차를 끝낸다(새 방송은 엔진이 새로 시작한다)."""


@dataclass
class LiveInfo:
    """방송 확인 결과의 방송 정보. 값의 뜻은 플랫폼과 상관없이 같다."""

    live_id: str
    title: str
    category: str
    adult: bool
    open_date: str
    image_url: str
    started_at: datetime | None
    tracks: list[Track] = field(default_factory=list)
    hls_url: str | None = None
    time_machine_active: bool | None = None
    playback_live_id: str | None = None  # 재생 정보가 가리키는 방송 ID

    def __post_init__(self) -> None:
        # 플랫폼에서 받은 글은 여기서 정리한다(경계)
        for name in ("live_id", "title", "category", "open_date", "image_url"):
            setattr(self, name, clean_text(getattr(self, name)))

    def complete(self) -> bool:
        """값이 모두 채워져 있고 재생 정보가 같은 방송의 것이다."""
        has_video = any(t.height and not t.audio_only for t in self.tracks)
        return bool(self.title and self.started_at and self.hls_url and has_video
                    and self.playback_live_id == self.live_id)


@dataclass
class CheckResult:
    verdict: Verdict
    info: LiveInfo | None = None
    detail: str = ""
    # 플랫폼이 확인을 막았다(봇 확인 요구, HTTP 429). 엔진이 그 채널의 확인을 늦춘다
    throttled: bool = False
    # 로그인이 필요한 방송: "needed"(쿠키 없음) | "rejected"(쿠키가 있는데 거부). 녹화 실패가 아니다
    credential_problem: str | None = None


@dataclass(frozen=True)
class Features:
    """선택 기능. 지원하지 않는 기능의 설정과 표시는 화면이 감춘다."""

    timemachine: bool = False  # 방송 앞부분부터 받기
    rewind: bool = False  # 되감기 초를 쓰는지
    # 타임머신 없이 일반 재생목록의 창으로 되감는다(되감기·이어받기를 일반 수신에도 적용)
    live_rewind: bool = False
    # 되감기는 없지만 다시 붙을 때 앞 파일의 마지막 조각 뒤부터 잇는다(재생목록에 남아 있으면)
    live_resume: bool = False
    # 마스터 재생목록 주소의 서명이 짧게 끝나므로 수신기가 처음 열 때도 갱신 방법으로 새 주소를 받는다
    fresh_master: bool = False


@dataclass(frozen=True)
class CredentialSpec:
    """필요한 자격. 값은 플랫폼별 0600 파일에만 둔다."""

    names: tuple[str, ...] = ()  # 자격 값의 이름(쿠키 이름 등). 비어 있으면 자격을 쓰지 않는다
    title: str = "쿠키"  # 화면의 묶음 이름
    help: str = ""  # 화면의 안내(경어체)
    forbidden: str = ';,\r\n" '  # 값에 쓸 수 없는 글자
    checkable: bool = False  # 확인 기능이 있는지
    # "values": 이름 붙은 값 몇 개, "file": `names[0]` 하나에 파일 글 전체(예: Netscape cookies.txt)
    kind: str = "values"
    max_bytes: int = 0  # kind="file"일 때 글의 최대 크기(바이트)
    # 값 꼴 자격도 저장 시각과 플랫폼이 만든 요약(credential_summary, 예: 만료 예상일)을 화면에 보인다
    summary: bool = False


@dataclass
class Playback:
    """받을 재생목록과 그 주소를 새로 받는 방법.

    `refresh`는 수신기에 그대로 넘기는 JSON 값이다. 뜻은 그 플랫폼의 `refresh_stream`만 안다.
    """

    master_url: str
    source: str  # 화면·세션 기록용: "live" 또는 "timemachine"
    refresh: dict[str, Any] | None = None


@runtime_checkable
class PlatformClient(Protocol):
    """코어용 비동기 클라이언트. 이벤트 루프를 막지 않는다."""

    async def check_live(self, channel_id: str, credentials: dict[str, str] | None) -> CheckResult: ...

    async def playback(self, channel_id: str, info: LiveInfo, credentials: dict[str, str] | None, *,
                       timemachine: bool) -> Playback:
        """재생 정보. `timemachine`이 참인데 받지 못하면 `TimeMachineUnavailable`."""
        ...

    async def check_credentials(self, credentials: dict[str, str] | None) -> str:
        """'missing' | 'valid' | 'expired' | 'failed'."""
        ...

    async def lookup_channel(self, query: str, credentials: dict[str, str] | None) -> tuple[str, str]:
        """`ChannelLookupNeeded.query`를 (채널 ID, 채널 이름)으로. 찾지 못하면 ChannelParseError(까닭)."""
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class Platform(Protocol):
    id: str
    name: str
    features: Features
    credential_spec: CredentialSpec
    thumbnail_hosts: tuple[str, ...]
    channel_hint: str  # 채널 칸의 안내(예: 주소나 ID 꼴)
    channel_id_error: str  # 저장·API가 채널 ID의 꼴이 틀렸을 때 내는 문장
    channel_rule: dict[str, Any]  # 화면이 주소를 해석하는 규칙(id_pattern, hosts, path_prefix, error)
    # 로그 내보내기의 가리기에 더할 이름(정규식 조각): cookie_patterns, query_params, live_id_keys,
    # path_params(주소 경로의 `/이름/값/` 꼴 서명 값), path_signatures(주소 경로의 `이름=값` 꼴 서명 조각),
    # live_id_patterns(더 가리기에서 방송 ID로 바꿀 값의 꼴)
    log_mask: dict[str, tuple[str, ...]]
    min_check_interval: float  # 0이면 전역 확인 주기만 따른다
    # 켜야 쓰는 플랫폼(외부 도구가 필요하다). 처음에는 꺼져 있고 관리자가 고지를 읽고 켠 뒤에만 쓴다
    opt_in: bool
    tool_names: tuple[str, ...]  # 이 플랫폼이 쓰는 외부 도구 이름. 비어 있으면 tool_versions를 부르지 않는다
    url_refresh_max: float  # 0이면 전역 설정만 따른다

    def parse_channel(self, text: str) -> str: ...

    def valid_channel_id(self, value: str) -> bool: ...

    def allowed_hosts(self, cfg: Any) -> tuple[str, ...]: ...

    def request_headers(self, kind: str, credentials: dict[str, str] | None) -> dict[str, str]: ...

    def thumbnail_url(self, raw: str | None) -> str | None: ...

    def client(self, cfg: Any) -> PlatformClient: ...

    def clean_credential_file(self, text: str) -> str:
        """kind="file"인 자격의 글을 검사해 저장할 글로 만든다. 받을 수 없으면 ValueError(사람이 읽는 까닭)."""
        ...

    def credential_summary(self, values: dict[str, str]) -> dict[str, Any]:
        """화면에 보일 자격 요약(값은 넣지 않는다). 값으로 하는 자격이면 빈 dict."""
        ...

    def pick_credentials(self, text: str) -> tuple[dict[str, str], dict[str, Any]]:
        """올린 쿠키 파일(Netscape cookies.txt·JSON 배열)에서 이 플랫폼의 자격을 고른다.
        돌려주는 값: (자격 저장에 넣을 값, 화면용 결과 {"names": 찾은 쿠키 이름, "expires": 파일에 적힌 만료일}).
        필요한 쿠키가 없거나 만료됐거나 파일을 읽지 못하면 ValueError(까닭. 값·내용은 넣지 않는다)."""
        ...

    def tool_versions(self) -> dict[str, str]:
        """이 플랫폼이 쓰는 외부 도구의 "판 (출처)". 시작 로그와 진단 요약에 남긴다. 없으면 빈 dict."""
        ...

    def enable_blocked(self) -> str | None:
        """환경변수 등으로 켤 수 없게 했으면 그 까닭, 아니면 None. 켜야 쓰는 플랫폼이 아니면 늘 None."""
        ...

    def opt_in_notice(self, cfg: Any) -> dict[str, Any]:
        """켜기 고지와 화면 문장(경어체): title, lines, enable_label, disable_label, off_label, need_enable,
        disable_confirm. 도구를 불러오거나 실행하지 않고 만든다(판은 묶음 정보에서 읽는다)."""
        ...

    def prepare_enable(self, cfg: Any) -> None:
        """켜기 전 자체 점검(따로 띄운 프로세스). 통과하지 못하면 ValueError(까닭). 통과하면 쓸 도구 판을 고른다."""
        ...

    def startup(self, cfg: Any) -> None:
        """서버가 시작할 때 한 번(동기, 스레드에서 부른다). 예: 쓸 외부 도구 판 고르기. 할 일이 없으면 아무것도 안 한다."""
        ...

    def tool_status(self, cfg: Any) -> list[dict[str, Any]]:
        """외부 도구의 상태(화면·진단 요약용, 비밀 없음). 각 항목: name, version, source, actions 등."""
        ...

    def tool_action(self, cfg: Any, name: str, action: str) -> dict[str, Any]:
        """외부 도구 조작(예: 갱신, 되돌리기). 모르는 도구·조작이면 KeyError, 꺼져 있으면 PermissionError,
        할 수 없으면 ValueError(사람이 읽는 까닭). 오래 걸릴 수 있으므로 스레드에서 부른다."""
        ...

    # ---- 수신기 프로세스 쪽(동기) ----
    def refresh_stream(self, http: httpx.Client, spec: dict[str, Any], credentials: dict[str, str] | None) -> str:
        """새 마스터 재생목록 주소. 받지 못하면 ApiError."""
        ...

    def segment_token(self, master_url: str) -> tuple[str, str] | None:
        """조각 요청이 인증을 거부할 때 붙여 볼 (질의 이름, 값). 없으면 None."""
        ...


# ---- 플랫폼 모듈이 같이 쓰는 도우미 ----
def host_allowed(url: str, allowed: tuple[str, ...] | list[str], allow_http: bool = False) -> bool:
    """정해진 호스트(접미사 일치)로만 요청한다. 제어문자·공백이 든 주소는 받지 않는다."""
    if not isinstance(url, str) or any(ord(c) <= 0x20 or ord(c) == 0x7f for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
        return False
    host = (parts.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in allowed)
