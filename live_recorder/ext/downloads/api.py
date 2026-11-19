"""downloads 확장의 공개 접점. replay·clips 확장은 이 모듈과 `media`(받는 방법)만 쓴다.

- 작업 종류 등록: `JobKind`를 `register_kind`로 등록한다(그 확장이 켜졌을 때, 저장소를 열 때). 종류는 주소를 알아보는 법과
  수신 프로세스에서 분석할 모듈(handler)을 넘긴다. handler 모듈은 `parse(url)`, `analyze(url, cookies, api_base) -> media.Video`,
  `filename(meta)`, `key(parsed)`, `thumbnail_ok(url)`를 내놓는다
- 작업 만들기: `current()`가 돌려주는 `Downloads`의 `select(owner, entries, confirm)`(분석 뒤 기본 화질로 작업), 상태 읽기
  (`video_states`), 대기열 저장소에 붙는 일(`attach`: 대기열을 열고 닫을 때 같이 열고 닫는다. 다시보기 목록 수집이 쓴다)
- 목록 공급자: `ListProvider`를 `register_list_provider`로 등록하면 채널 다시보기 목록 보기에 그 플랫폼이 들어간다
- 꺼진 확장의 주소: 확장 목록(core.extensions.known)에서 그 확장이 밝힌 종류(`job_kinds`)로 알아보고
  "<이름> 확장이 꺼져 있습니다"로 거절한다
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ...core import extensions

from .errors import ReplayError as DownloadError  # noqa: E402  (공개: 확장이 내는 오류 형식)
from . import catalog  # noqa: E402  (공개: 목록 공급자가 쓰는 오류 형식과 수집기)

BLOCKED = "확장이 꺼져 있어 진행할 수 없습니다"


# ---- 목록 공급자 ----
@dataclass
class ListProvider:
    """채널 다시보기 목록 공급자. 확장이 `register_list_provider`로 등록한다(치지직은 replay, 씨미는 cime_media).
    공급자는 한 쪽을 받고(`fetch`) 읽어(`parse`) 영상 줄과 다음 쪽 표시를 돌려줄 뿐이다. 일정·재시도·저장·조회는 downloads가 한다.

    - fetch(channel, cursor) -> (HTTP 상태, 본문): 연결 실패면 `catalog.FetchError`
    - parse(status, payload, cursor, channel) -> {"videos": [줄…], "next": 다음 쪽 표시|None, "total_pages": int|None,
      "channel_name": str|None, "skipped": 버린 줄 수}. 읽을 수 없으면 `catalog.ListError`. 줄은 video_no·title·video_type
      (replay|upload)·publish_date(`YYYY-MM-DD HH:MM:SS` 한국 시각)·month(`YYYY-MM`)·duration(초)·adult·thumbnail
    - lookup(channel) -> 채널 이름: 없으면 `catalog.ChannelMissing`
    - resolve(text) -> (채널 ID, 이름) | None: 화면의 글(주소·@핸들)을 채널로. None이면 `valid_channel`로 ID만 받는다
    - video_url(video_no, channel): 고른 영상을 받을 주소(작업 종류가 알아보는 주소). state_key(video_no): 받은 기록의 열쇠
    """
    platform: str
    extension: str
    name: str  # 화면·오류 문장의 플랫폼 이름("씨미")
    subject: str  # 주격 조사를 붙인 이름("씨미가")
    fetch: Callable[[str, Any], Awaitable[tuple[int, Any]]]
    parse: Callable[[int, Any, Any, str], dict]
    video_url: Callable[[str, str], str]  # (영상 번호, 채널 ID) -> 받을 주소
    first: Any = None  # 첫 쪽의 표시
    lookup: Callable[[str], Awaitable[str]] | None = None
    resolve: Callable[[str], Awaitable[tuple[str, str] | None]] | None = None
    valid_channel: Callable[[str], bool] = lambda value: bool(value)
    state_key: Callable[[str], str] = lambda video_no: video_no
    add_hint: str = ""  # 채널 추가 칸의 안내
    video_types: bool = False  # 다시보기·일반 업로드를 가르는가(종류 필터)
    sleep: Callable[[float], Awaitable[Any]] | None = None  # 시험이 바꿔 넣는다
    jitter: Callable[[], float] | None = None
    aclose: Callable[[], Awaitable[None]] | None = None
    available: Callable[[], bool] | None = None  # 지금 쓸 수 있는가(예: 그 플랫폼이 켜져 있어야 한다). 없으면 늘 참

    def usable(self) -> bool:
        if self.available is None:
            return True
        try:
            return bool(self.available())
        except Exception:  # noqa: BLE001 - 판단할 수 없으면 쓰지 않는다
            return False


_providers: dict[str, ListProvider] = {}
# 채널 목록 방식의 화면 조각. 공급자를 내는 확장이 `html = {"downloads-modes": LIST_FRAGMENT.name}`, `ui_dir = LIST_FRAGMENT.parent`로
# 밝힌다(공급자가 있는 조합에서만 화면에 들어간다. 여러 확장이 밝혀도 한 번만 들어간다)
from pathlib import Path as _Path  # noqa: E402

LIST_FRAGMENT = _Path(__file__).resolve().parent / "ui" / "modes.html"


def register_list_provider(provider: ListProvider) -> None:
    _providers[provider.platform] = provider


def unregister_list_provider(platform: str) -> ListProvider | None:
    return _providers.pop(platform, None)


def list_providers() -> dict[str, ListProvider]:
    """지금 쓸 수 있는 공급자(플랫폼 ID → 공급자). 등록 순서대로."""
    return {pid: p for pid, p in _providers.items() if p.usable()}


@dataclass(frozen=True)
class JobKind:
    kind: str  # 작업의 kind 값(video·clip). 저장소 형식 그대로
    extension: str  # 이 종류를 내놓은 확장
    title: str  # 사람이 읽는 이름("다시보기", "클립"). 꺼져 있다는 안내에 쓴다
    handler: str  # 수신 프로세스가 불러올 분석 모듈
    platform: str = ""  # 이 종류의 플랫폼. 수신 프로세스에 넘길 자격·API 주소를 고른다
    example: str = ""  # 화면의 주소 칸 예시
    # 종류마다 더할 수 있는 것: 지금 쓸 수 있는지(예: 플랫폼이 켜져 있어야 한다)와 그렇지 않을 때의 안내,
    # 수신 프로세스에 넘길 값(context), 오류 코드별 문장 바꾸기, 분석과 분석 사이의 간격(초)
    available: Callable[[], bool] | None = field(default=None, compare=False)
    unavailable_message: str = ""
    context: Callable[[], dict] | None = field(default=None, compare=False)
    messages: dict = field(default_factory=dict, compare=False, hash=False)
    analysis_gap: float = 0.0

    def usable(self) -> bool:
        if self.available is None:
            return True
        try:
            return bool(self.available())
        except Exception:  # noqa: BLE001 - 판단할 수 없으면 쓰지 않는다
            return False

    def message(self, code: str, default: str) -> str:
        return self.messages.get(code) or default

    def module(self):
        return importlib.import_module(self.handler)


_kinds: dict[str, JobKind] = {}


def register_kind(kind: JobKind) -> None:
    _kinds[kind.kind] = kind


def unregister_kind(name: str) -> None:
    _kinds.pop(name, None)


def kinds() -> list[JobKind]:
    """지금 쓸 수 있는 종류(등록됐고 usable)."""
    return [k for k in _kinds.values() if k.usable()]


def find_kind(name: str | None) -> JobKind | None:
    kind = _kinds.get(name or "")
    return kind if kind is not None and kind.usable() else None


def registered_kind(name: str | None) -> JobKind | None:
    """쓸 수 없는 상태(예: 플랫폼이 꺼짐)여도 등록된 종류."""
    return _kinds.get(name or "")


def parse(url: Any) -> tuple[dict[str, Any], JobKind]:
    """켜진 종류로 주소를 알아본다. 꺼진 확장의 주소면 DisabledKind, 모르는 주소면 media.Failure("invalid_url")."""
    from .media import Failure

    for kind in _kinds.values():
        try:
            parsed = kind.module().parse(url)
        except Failure:
            continue
        if parsed is not None:
            if not kind.usable():
                raise DisabledKind(kind)
            return parsed, kind
    for ext in extensions.known():
        for kind in getattr(ext, "job_kinds", ()):
            if kind.kind in _kinds:
                continue
            try:
                if kind.module().parse(url) is not None:
                    raise DisabledKind(kind)
            except Failure:
                continue
    raise Failure("invalid_url")


def unknown_url_message() -> str:
    """알아보지 못한 주소의 안내. 켜진 종류가 모두 치지직이면 예전 문장 그대로, 아니면 받을 수 있는 종류를 적는다."""
    from ...platforms import registry
    from .errors import ERROR_MESSAGES

    active = kinds()
    if active and all(k.platform == registry.DEFAULT for k in active):  # 기본 플랫폼(치지직)의 종류만 켜져 있다
        return ERROR_MESSAGES["invalid_url"]
    names = []
    for k in active:
        platform = registry.find(k.platform) if k.platform else None
        names.append(f"{platform.name} {k.title}" if platform and platform.name not in k.title else k.title)
    return "지원하는 주소가 아닙니다." + (f" 받을 수 있는 주소: {', '.join(names)}" if names else "")


def all_kinds() -> list[JobKind]:
    """켜진 종류와, 꺼진 확장이 밝힌 종류(`job_kinds`)까지."""
    out = list(_kinds.values())
    for ext in extensions.known():
        out += [k for k in getattr(ext, "job_kinds", ()) if k.kind not in _kinds and k not in out]
    return out


def parse_any(url: Any) -> tuple[dict[str, Any], JobKind]:
    """꺼진 확장의 종류까지 주소를 알아본다(받은 기록·지난 작업의 열쇠를 셀 때). 모르면 media.Failure."""
    from .media import Failure

    for kind in all_kinds():
        try:
            parsed = kind.module().parse(url)
        except Failure:
            continue
        if parsed is not None:
            return parsed, kind
    raise Failure("invalid_url")


class DisabledKind(ValueError):
    """꺼진 확장의 작업 종류."""

    def __init__(self, kind: JobKind):
        super().__init__(kind.kind)
        self.kind = kind
        if kind.kind in _kinds and kind.unavailable_message:
            self.message = kind.unavailable_message  # 확장은 켜져 있지만 지금 쓸 수 없다(예: 플랫폼이 꺼짐)
        else:
            self.message = f"{kind.title} 확장이 꺼져 있습니다"


# ---- 돌고 있는 대기열(downloads 확장이 켜졌을 때) ----
class Downloads:
    """replay·clips가 쓰는 대기열의 공개 면."""

    def __init__(self, service):
        self._service = service
        self._attachments: list[tuple[Callable[[Any], Awaitable[None]], Callable[[], Awaitable[None]]]] = []
        self.lists = None  # 채널 다시보기 목록(lists.Lists). downloads 확장이 넣는다

    def list_collector(self):
        """목록 수집기(catalog.Collector). 대기열 저장소가 열려 있지 않으면 None."""
        return self.lists.collector if self.lists is not None else None

    @property
    def ready(self) -> bool:
        return self._service.manager is not None

    @property
    def error(self) -> str | None:
        return self._service.error

    def attach(self, on_open: Callable[[Any], Awaitable[None]], on_close: Callable[[], Awaitable[None]]) -> None:
        """대기열 저장소(sqlite 연결)를 열고 닫을 때 같이 부른다. on_open(db)은 이벤트 루프에서 불린다."""
        self._attachments.append((on_open, on_close))

    def detach_all(self) -> None:
        self._attachments.clear()

    async def opened(self, db) -> None:
        for on_open, _ in self._attachments:
            await on_open(db)

    async def closing(self) -> None:
        for _, on_close in reversed(self._attachments):
            await on_close()

    def require(self):
        return self._service.require()

    def video_states(self, keys) -> dict[str, str]:
        manager = self._service.manager
        return manager.video_states(keys) if manager else {}

    async def select(self, owner: str, entries: list[tuple[str, str]], confirm: bool = False) -> dict[str, Any]:
        """(주소, 제목) 목록을 분석한 뒤 기본 화질로 작업을 만든다."""
        return await self._service.require().select_download(owner, entries, confirm)

    def recording_channels(self) -> list[tuple[str, str]]:
        return self._service.recording_channels() if self._service.recording_channels else []


_current: Downloads | None = None


def current() -> Downloads | None:
    """켜진 downloads 확장의 대기열. 꺼져 있거나 아직 뜨지 않았으면 None."""
    return _current


def _set_current(value: Downloads | None) -> None:
    global _current
    _current = value


def owner_title(name: str) -> str:
    ext = extensions.find(name)
    return (ext.title or name) if ext else name
