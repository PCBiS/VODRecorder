"""확장 접점. 확장 하나가 중심부에 등록하는 것을 이 파일의 형식으로만 적는다.

확장은 `Extension`을 이어받아 아래를 채운다(쓰지 않는 것은 기본값 그대로 둔다).
- `name`·`title`·`version`·`requires`: 이름(설정·저장소·경로의 앞부분), 화면 이름, 버전, 필요한 다른 확장
- `routes(ctx)`와 `access_rules`: API 경로(FastAPI APIRouter)와 그 접근 규칙(PUBLIC·VIEW·LOGIN). 규칙이 없는 경로,
  다른 확장·중심부와 겹치는 경로가 있으면 서버가 뜨지 않는다. 로그인·CSRF·보기 전용 판정은 중심부 관문이 한다
- `tabs`·`settings_groups`·`scripts`·`styles`·`static_dir`: 위쪽 탭, 설정 하위 탭 안의 묶음, 화면 파일
  (`/static/ext/<이름>/<파일>`로 나간다). 화면 뼈대가 서버가 내려 준 순서대로 불러온다
- `settings`: 설정 키와 기본값·검증(`SettingSpec`). 값은 중심부 저장소의 설정 표에 `ext:<이름>:<키>`로 둔다
- `open_storage(ctx)`: 자기 저장소를 열고 옮긴다. 켜진 확장만 서버가 뜰 때 한 번 부른다
- `listeners()`: 듣는 사건(`core.events.EVENTS`의 이름 → 함수). 사건 스레드에서 차례로 불린다
- `start(ctx)`·`stop(ctx)`: 서버 시작·종료 때 할 일(비동기)
- `log_export(ctx)`·`log_fields`·`diagnostics(ctx)`: 로그 내려받기 묶음에 넣을 파일과 줄, 줄의 필드 가리기 규칙, 진단 요약
- `lock_targets(ctx)`: 파일 관리자가 건드리지 못하게 할 경로(쓰는 중인 파일)
- `channel_info(ctx, channel)`: 채널 화면 줄에 더할 정보(`ext.<이름>` 아래에 들어간다)

꺼진 확장은 아무것도 등록하지 않는다: 경로·탭·설정 묶음·사건 듣기·백그라운드 일·저장소가 없다.
켜고 끄기는 설정 > 일반의 "확장" 묶음에서 하고 다음 서버 시작부터 먹는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .config import ConfigError

if TYPE_CHECKING:  # 형식 표시만. 실제로 불러오지 않는다
    from .events import Event, EventBus

log = logging.getLogger("live_recorder.extensions")

PUBLIC, VIEW, LOGIN = "public", "view", "login"
KEY_NONE, KEY_VIEW, KEY_OPERATE = "none", "view", "operate"  # 접근 키 권한
SUBTABS = ("record", "notify", "account", "general")  # 중심부의 설정 하위 탭. 확장은 자기 하위 탭도 더한다
ENABLED_KEY = "ext:enabled"  # 켠 확장 이름 목록(설정 표)


class ExtensionError(Exception):
    """확장 등록이 규칙에 어긋난다. 서버를 띄우지 않는다."""


@dataclass(frozen=True)
class AccessRule:
    method: str
    path: str  # 경로, 접두사(*로 끝남), 또는 경로 꼴(`{이름}`이 한 마디)
    level: str  # PUBLIC | VIEW | LOGIN
    # 접근 키로 열 수 있는지와 필요한 권한: KEY_VIEW | KEY_OPERATE | KEY_NONE(키로는 열리지 않는다, 기본)
    key: str = "none"


@dataclass(frozen=True)
class Tab:
    view: str  # 화면 이름(주소 #<view>, 요소 view-<view>)
    label: str


@dataclass(frozen=True)
class SettingsGroup:
    subtab: str  # SUBTABS 중 하나
    id: str  # 요소 ID(ext-group-<id>)
    title: str


@dataclass(frozen=True)
class SettingSpec:
    default: Any
    validate: Callable[[Any], Any] = lambda v: v  # 틀리면 ConfigError. 고친 값을 돌려준다
    label: str = ""
    list_value: bool = False  # 값이 목록이다(설정 API가 목록을 받는다)
    path: bool = False  # 경로다. 진단 요약에는 설정 여부만 적는다


@dataclass
class LogFile:
    """로그 내려받기 묶음에 넣을 줄. 기간(`time_key`)으로 고르고 크기 한도에서 오래된 줄부터 뺀다.
    `fields`가 있으면 그 필드만 남기고 정해진 문장으로 다룬다(문장 안의 비밀만 가린다). 없으면 로그 줄과 같은 규칙이다."""

    name: str  # 묶음 안의 파일 이름
    rows: list[dict[str, Any]]
    time_key: str = "ts"
    fields: tuple[str, ...] | None = None

@dataclass
class ExtensionContext:
    """확장이 쓰는 중심부의 공개 접점."""

    name: str
    cfg: Any
    store: Any  # core.store.Store
    events: "EventBus"
    data_dir: Path  # <데이터 폴더>/ext/<이름>. 자기 저장소는 여기에 둔다
    log: logging.Logger
    disk_free: Callable[[Path], int] | None = None  # 여유 공간(시험이 바꿔 넣는다)
    gate_provider: Callable[[str], Any] | None = None  # 플랫폼 요청 창구(중심부가 넣는다)

    def request_gate(self, platform_id: str):
        """그 플랫폼의 요청 창구(core.gate.Gate). 목록 수집·분석은 EXTRA 줄로 쓴다. 없으면 None."""
        return self.gate_provider(platform_id) if self.gate_provider else None

    def setting(self, key: str) -> Any:
        return get_setting(self.store, self.name, key)

    def settings(self) -> dict[str, Any]:
        """이 확장의 설정 값 전부."""
        ext = find(self.name)
        return {k: get_setting(self.store, self.name, k) for k in (ext.settings if ext else {})}

    def core_settings(self):
        """중심부 설정(core.config.Settings). 읽기만 한다."""
        return self.store.get_settings()


class Extension:
    name: str = ""
    title: str = ""
    description: str = ""  # 설정 > 일반의 "확장" 묶음에 보이는 설명
    version: str = "0"
    requires: tuple[str, ...] = ()
    # 켜져 있어야 하는 플랫폼(켜야 쓰는 플랫폼). 꺼져 있으면 이 확장을 켤 수 없다
    requires_platforms: tuple[str, ...] = ()
    # 이전 빌드에서 올라올 때 한 번 읽는 것: 켜고 끄기 키와 그 키가 없을 때의 값, Settings에 있던 설정 키
    legacy_enabled_key: str | None = None
    legacy_enabled_default: bool = False
    legacy_settings: tuple[str, ...] = ()
    http_errors: tuple[type, ...] = ()  # 이 예외(status·detail·extra)는 {**extra, "error": detail}로 응답한다
    log_not_included: tuple[str, ...] = ()  # 로그 묶음 안내(manifest.not_included)에 더할 것
    access_rules: tuple[AccessRule, ...] = ()
    tabs: tuple[Tab, ...] = ()
    settings_groups: tuple[SettingsGroup, ...] = ()
    scripts: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    static_dir: Path | None = None
    # 화면 문서(index.html)의 자리(`<!-- lr-slot:<이름> -->`)에 넣을 HTML 조각: {자리: ui_dir 안의 파일}. 켜진 확장만 넣는다
    html: dict[str, str] = {}
    ui_dir: Path | None = None
    settings: dict[str, SettingSpec] = {}
    log_fields: dict[str, str] = {}  # 로그 줄 필드 → 가리기 규칙(logmask.FIELDS와 같은 값)

    def routes(self, ctx: ExtensionContext):  # -> fastapi.APIRouter | None
        return None

    def open_storage(self, ctx: ExtensionContext) -> None:
        return None

    def listeners(self, ctx: ExtensionContext) -> dict[str, Callable[["Event"], None]]:
        return {}

    async def start(self, ctx: ExtensionContext) -> None:
        return None

    async def stop(self, ctx: ExtensionContext) -> None:
        return None

    def log_export(self, ctx: ExtensionContext) -> list:
        """로그 묶음에 넣을 줄: `LogFile` 목록(또는 (파일 이름, 줄 목록)). 이벤트 루프에서 부른다."""
        return []

    def log_names(self, ctx: ExtensionContext) -> dict[str, list]:
        """로그 가리기가 알아야 할 이름: {"titles": [제목], "channels": [(채널 ID, 이름)]}."""
        return {}

    async def settings_check(self, ctx: ExtensionContext, changes: dict[str, Any]) -> None:
        """설정 저장 전(설정 API 본문 전체). 거부하려면 예외."""
        return None

    async def settings_applied(self, ctx: ExtensionContext, changes: dict[str, Any]) -> None:
        """설정 저장 뒤(이 확장의 키가 바뀌었을 때)."""
        return None

    def file_roots(self, ctx: ExtensionContext) -> list[tuple[str, str, str]]:
        """파일 관리자의 허용 폴더(실제 경로, 표시 이름, 출처)."""
        return []

    def diagnostics(self, ctx: ExtensionContext) -> dict[str, Any]:
        return {}

    def lock_targets(self, ctx: ExtensionContext) -> list[Path]:
        return []

    def channel_info(self, ctx: ExtensionContext, channel: dict[str, Any]) -> dict[str, Any] | None:
        return None


# ---- 등록 ----
# 제품에 들어 있는 확장(이름 → 모듈, 이 순서로 켠다). 중심부는 이 표로만 안다(이름으로 불러 쓰지 않는다)
BUILTIN: dict[str, str] = {
    "downloads": "live_recorder.ext.downloads.extension",
    "replay": "live_recorder.ext.replay.extension",
    "clips": "live_recorder.ext.clips.extension",
    "youtube_video": "live_recorder.ext.youtube_video.extension",
    "cime_media": "live_recorder.ext.cime_media.extension",
}
_known: dict[str, Extension] = {}


def register(ext: Extension) -> None:
    if not ext.name or not ext.name.replace("_", "").isalnum() or ext.name != ext.name.lower():
        raise ExtensionError(f"확장 이름이 틀립니다: {ext.name!r}")
    _known[ext.name] = ext


def unregister(name: str) -> None:
    _known.pop(name, None)


def known() -> list[Extension]:
    """들어 있는 확장(BUILTIN 순서) 다음에 시험 등이 등록한 확장."""
    import importlib

    for name, module in BUILTIN.items():
        if name not in _known:
            register(importlib.import_module(module).EXTENSION)
    builtin = [_known[n] for n in BUILTIN if n in _known]
    return builtin + [e for n, e in _known.items() if n not in BUILTIN]


def find(name: str) -> Extension | None:
    return next((e for e in known() if e.name == name), None)


# ---- 켜고 끄기(설정 표) ----
def read_enabled(db_path: Path) -> list[str]:
    """서버가 뜰 때(저장소를 열기 전) 켠 확장 목록을 읽는다. 읽기만 하고 아무것도 고치지 않는다. 없으면 빈 목록."""
    import json
    import sqlite3

    if not Path(db_path).exists():
        return []
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = db.execute("SELECT value FROM settings WHERE key=?", (ENABLED_KEY,)).fetchone()
        finally:
            db.close()
    except sqlite3.DatabaseError:
        return []  # 깨진 저장소는 엔진이 열 때 알린다
    try:
        value = json.loads(row[0]) if row else []
    except ValueError:
        return []
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def enabled_names(store) -> list[str]:
    value = store.get_extra(ENABLED_KEY, [])
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def validate_enabled(names: list[str]) -> list[str]:
    """켤 확장 목록을 검사한다. 모르는 확장, 필요한 확장이 꺼진 확장은 켤 수 없다."""
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ConfigError("확장 목록의 형식이 틀립니다")
    clean: list[str] = []
    for name in names:
        ext = find(name)
        if ext is None:
            raise ConfigError(f"모르는 확장입니다: {name}")
        missing = [r for r in ext.requires if r not in names]
        if missing:
            raise ConfigError(f"{ext.title or name} 확장은 {', '.join(missing)} 확장이 켜져 있어야 켤 수 있습니다")
        if name not in clean:
            clean.append(name)
    return clean


def dependents(name: str, enabled: list[str]) -> list[str]:
    """이 확장을 끄면 함께 꺼지는 확장(켜진 것 가운데 이것이 필요한 것, 줄줄이)."""
    out: list[str] = []
    todo = [name]
    while todo:
        cur = todo.pop()
        for ext in known():
            if ext.name in enabled and cur in ext.requires and ext.name not in out:
                out.append(ext.name)
                todo.append(ext.name)
    return out


# ---- 이전 빌드에서 올라올 때 ----
def _legacy_moves(enabled_value_of: Callable[[str, Any], Any]) -> tuple[list[str], dict[str, tuple[str, str]]]:
    enabled: list[str] = []
    moves: dict[str, tuple[str, str]] = {}  # 옛 키 → (확장, 키)
    for ext in known():
        if ext.legacy_enabled_key:
            if enabled_value_of(ext.legacy_enabled_key, ext.legacy_enabled_default) is True:
                if all(r in enabled for r in ext.requires):
                    enabled.append(ext.name)
        for key in ext.legacy_settings:
            moves.setdefault(key, (ext.name, key))
    return enabled, moves


def upgrade_legacy_settings(db, from_version: int) -> None:
    """저장소를 옮길 때 한 번(이전 빌드에서 올라온 경우). 켜고 끄기 키가 켜져 있었으면(없으면 그 빌드의 기본값) 그 확장을 켜고,
    Settings에 있던 확장 몫의 설정을 `ext:<확장>:<키>`로 옮긴 뒤 옛 키를 지운다. 확장이 정한 것만 옮긴다(중심부는 이름을 모른다)."""
    import json

    # 아주 옛 저장소에는 설정 표가 없을 수 있다(그 빌드의 기본값으로 옮긴다)
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if db.execute("SELECT 1 FROM settings WHERE key=?", (ENABLED_KEY,)).fetchone():
        return
    stored = {r[0]: r[1] for r in db.execute("SELECT key, value FROM settings")}

    def value_of(key: str, default: Any) -> Any:
        try:
            return json.loads(stored[key]) if key in stored else default
        except ValueError:
            return default

    enabled, moves = _legacy_moves(value_of)
    upsert = "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    db.execute(upsert, (ENABLED_KEY, json.dumps(enabled)))
    moved = []
    for old, (ext_name, key) in moves.items():
        if old in stored:
            db.execute(upsert, (setting_key(ext_name, key), stored[old]))
            moved.append(old)
    legacy_keys = {e.legacy_enabled_key for e in known() if e.legacy_enabled_key} | set(moves)
    for key in legacy_keys:
        db.execute("DELETE FROM settings WHERE key=?", (key,))
    log.info("이전 빌드의 설정을 확장 설정으로 옮겼다", extra={"event": "extensions_migrated", "from_version": from_version,
                                                             "extension": ",".join(enabled) or None, "keys": len(moved)})


def validate_legacy(key: str, value: Any) -> Any:
    """옛 설정 키(확장 몫)의 값 검사. 확장이 정한 규칙을 쓴다. 모르는 키면 ConfigError."""
    for ext in known():
        if key == ext.legacy_enabled_key:
            from .config import bool_value

            return bool_value(value)
        if key in ext.legacy_settings:
            return ext.settings[key].validate(value)
    raise ConfigError(f"모르는 설정 키: {key}")


def import_legacy_settings(store, values: dict[str, Any]) -> list[str]:
    """이전 도구(migrate)가 옛 설정 키로 읽은 값을 확장 설정으로 넣는다. 켠 확장 목록을 돌려준다.
    켜고 끄기 키가 없으면 그 빌드의 기본값을 따른다."""
    enabled, moves = _legacy_moves(lambda key, default: values.get(key, default))
    out: dict[str, Any] = {ENABLED_KEY: enabled}
    for old, (ext_name, key) in moves.items():
        if old in values:
            spec = find(ext_name).settings[key]
            out[setting_key(ext_name, key)] = spec.validate(values[old])
    store.set_extra(out)
    return enabled


def settings_snapshot(store) -> dict[str, dict[str, Any]]:
    """켠 확장의 설정 값(진단 요약용). {확장: {키: 값}}. 경로 설정은 설정 여부만 적는다."""
    out: dict[str, dict[str, Any]] = {}
    for name in enabled_names(store):
        ext = find(name)
        if ext is None:
            continue
        values = {}
        for key, spec in ext.settings.items():
            value = get_setting(store, name, key)
            values[key] = {"set": bool(value)} if spec.path else (list(value) if isinstance(value, tuple) else value)
        out[name] = values
    return out


def setting_key(ext_name: str, key: str) -> str:
    return f"ext:{ext_name}:{key}"


def get_setting(store, ext_name: str, key: str) -> Any:
    ext = find(ext_name)
    spec = ext.settings.get(key) if ext else None
    if spec is None:
        raise KeyError(key)
    value = store.get_extra(setting_key(ext_name, key), spec.default)
    try:
        return spec.validate(value)
    except ConfigError:
        return spec.default


@dataclass
class ExtensionHost:
    """서버 한 번(시작부터 종료까지)의 켜진 확장들. 서버가 뜰 때 정하고 도는 동안 바꾸지 않는다."""

    cfg: Any
    events: "EventBus"
    active: list[Extension] = field(default_factory=list)
    contexts: dict[str, ExtensionContext] = field(default_factory=dict)

    @classmethod
    def load(cls, cfg, wanted: list[str], events, disk_free=None) -> "ExtensionHost":
        """켠 확장 가운데 알려진 것을 고른다. 필요한 확장이 꺼졌거나 없는 확장은 켜지 않는다."""
        host = cls(cfg, events)
        for ext in known():
            if ext.name not in wanted:
                continue
            missing = [r for r in ext.requires if r not in wanted or find(r) is None]
            if missing:
                log.warning("필요한 확장이 꺼져 있어 켜지 않았다: %s", ext.name,
                            extra={"event": "extension_skipped", "extension": ext.name})
                continue
            host.active.append(ext)
            host.contexts[ext.name] = ExtensionContext(ext.name, cfg, None, events, cfg.data_dir / "ext" / ext.name,
                                                       logging.getLogger(f"live_recorder.ext.{ext.name}"), disk_free)
        return host

    def bind(self, store, gate_provider=None) -> None:
        """엔진이 저장소를 연 뒤 부른다. gate_provider는 플랫폼 ID → 요청 창구."""
        for ctx in self.contexts.values():
            ctx.store = store
            ctx.gate_provider = gate_provider

    def ctx(self, ext: Extension) -> ExtensionContext:
        return self.contexts[ext.name]

    def open(self) -> None:
        """저장소를 열고 사건 듣기를 건다. 서버가 뜰 때 한 번."""
        for ext in self.active:
            ctx = self.ctx(ext)
            ctx.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            ext.open_storage(ctx)
            for name, fn in ext.listeners(ctx).items():
                self.events.subscribe(name, fn, owner=ext.name)

    async def start(self) -> None:
        for ext in self.active:
            await ext.start(self.ctx(ext))

    async def stop(self) -> None:
        for ext in reversed(self.active):
            try:
                await ext.stop(self.ctx(ext))
            except Exception:  # noqa: BLE001 - 한 확장의 종료 오류로 다른 확장·녹화 종료를 막지 않는다
                log.exception("확장 종료 중 오류", extra={"event": "extension_stop_failed", "extension": ext.name})
            self.events.unsubscribe_owner(ext.name)

    # ---- 화면·내려받기·파일 관리자가 묻는 것 ----
    def html_fragments(self) -> dict[str, str]:
        """자리 이름 → 켜진 확장들이 넣을 HTML(켜는 순서대로 이어 붙임)."""
        out: dict[str, str] = {}
        seen: set[tuple[str, Path]] = set()
        for ext in self.active:
            for slot, name in ext.html.items():
                path = (Path(ext.ui_dir) / name).resolve()
                if (slot, path) in seen:
                    continue  # 같은 조각 파일을 여러 확장이 밝히면 한 번만 넣는다(예: 공통 화면 조각)
                seen.add((slot, path))
                out[slot] = out.get(slot, "") + path.read_text(encoding="utf-8")
        return out

    def ui_manifest(self) -> dict[str, Any]:
        scripts, styles = [], []
        for ext in self.active:
            scripts += [f"/static/ext/{ext.name}/{s}" for s in ext.scripts]
            styles += [f"/static/ext/{ext.name}/{s}" for s in ext.styles]
        return {"scripts": scripts, "styles": styles,
                "tabs": [{"ext": e.name, "view": t.view, "label": t.label} for e in self.active for t in e.tabs],
                "groups": [{"ext": e.name, "subtab": g.subtab, "id": g.id, "title": g.title}
                           for e in self.active for g in e.settings_groups]}

    def log_export(self) -> dict[str, Any]:
        """로그 내려받기에 넣을 것. 이벤트 루프에서 부른다(확장의 DB 연결을 루프에서만 쓰는 경우가 있다).
        {"files": [LogFile], "diagnostics": {확장: 요약}, "fields": {필드: 규칙}, "titles": [...], "channels": [...],
         "not_included": [...]}"""
        out: dict[str, Any] = {"files": [], "diagnostics": {}, "fields": {}, "titles": [], "channels": [], "not_included": []}
        for ext in self.active:
            ctx = self.ctx(ext)
            try:
                for item in ext.log_export(ctx):
                    if isinstance(item, LogFile):
                        out["files"].append(item)
                    else:
                        name, rows = item
                        out["files"].append(LogFile(f"ext/{ext.name}/{name}", list(rows)))
                diag = ext.diagnostics(ctx)
                if diag:  # 진단 요약을 내놓는 확장만(없으면 diagnostics/extensions.json도 없다)
                    out["diagnostics"][ext.name] = {"version": ext.version, **diag}
                out["fields"].update(ext.log_fields)
                names = ext.log_names(ctx)
                out["titles"] += list(names.get("titles", []))
                out["channels"] += list(names.get("channels", []))
                out["not_included"] += list(ext.log_not_included)
            except Exception:  # noqa: BLE001 - 한 확장의 오류로 묶음을 못 만들게 하지 않는다
                log.exception("확장 로그 내보내기 오류", extra={"event": "extension_failed", "extension": ext.name})
        return out

    # ---- 설정: 켜진 확장의 설정 키는 설정 API에서 중심부 설정과 함께 보인다 ----
    def setting_owner(self, key: str) -> Extension | None:
        return next((e for e in self.active if key in e.settings), None)

    def settings_values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for ext in self.active:
            out.update(self.ctx(ext).settings())
        return out

    def list_keys(self) -> set[str]:
        return {k for e in self.active for k, spec in e.settings.items() if spec.list_value}

    def file_roots(self) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        for ext in self.active:
            try:
                out += list(ext.file_roots(self.ctx(ext)))
            except Exception:  # noqa: BLE001
                log.exception("확장 허용 폴더 조회 오류", extra={"event": "extension_failed", "extension": ext.name})
        return out

    def lock_targets(self) -> list[Path]:
        out: list[Path] = []
        for ext in self.active:
            try:
                out += [Path(p) for p in ext.lock_targets(self.ctx(ext))]
            except Exception:  # noqa: BLE001
                log.exception("확장 잠금 대상 조회 오류", extra={"event": "extension_failed", "extension": ext.name})
        return out

    def channel_info(self, channel: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for ext in self.active:
            try:
                info = ext.channel_info(self.ctx(ext), channel)
            except Exception:  # noqa: BLE001
                log.exception("확장 채널 정보 오류", extra={"event": "extension_failed", "extension": ext.name})
                continue
            if info:
                out[ext.name] = info
        return out
