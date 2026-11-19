"""기존 설정 이전 . 한 번 실행하는 가져오기 도구다.

    python -m live_recorder migrate --from <기존 json 폴더> [--map 옛경로=새경로 ...] [--apply]

- 기존 파일은 읽기만 한다. 입력은 `channels.json`, `config.json`, `cookie.json`, `notification.json`과
  아래에 적은 키뿐이다. 다시보기 설정은 기존 다운로드 도구가 쓰던 `config.json`의 세 키를 읽는다.
- 기본은 미리 보기(dry-run)다. `--apply`를 줘야 쓴다.
- 가져온 것, 건너뛴 것(이유), 무시한 키를 표로 출력한다. 쿠키·텔레그램 토큰·chat_id 값은 어디에도 출력하지 않는다.
- 이미 채널·계정·쿠키·텔레그램 값이 있는 데이터 폴더에는 쓰지 않고 멈춘다.
- 다시보기 작업 내역과 계정은 옮기지 않는다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import notify
from .core import secretstore
from .core import extensions
from .platforms import credentials, registry
from .core import quality
from .core import store as st
from .core.config import AppConfig, ConfigError, Settings
from .files import FileManager

CHANNEL_KEYS = ("id", "name", "output_dir", "quality", "extension", "record_enabled", "platform")
CONFIG_KEYS = {  # (+ 다시보기 설정)
    "recheckInterval": "check_interval",
    "filenamePattern": "filename_pattern",
    "autoPostProcessing": "remux_enabled",
    "deleteAfterPostProcessing": "delete_original",
    "plugin_type": "timemachine_enabled",
    "timemachine_time_shift": "rewind_seconds",
    "fileManagerEnabled": "fm_enabled",
    "fileManagerRoots": "fm_roots",
    "fileManagerReadOnly": "fm_readonly",
    "trashEnabled": "fm_trash",
    "replayEnabled": "replay_enabled",
    "replayDownloadRoot": "replay_dir",
    "replayMinFreeGiB": "replay_min_free_bytes",
}
# 옛 이벤트 이름 → 새 이벤트. 새 이름이 그대로 들어와도 받는다.
# 대응이 없는 이름(watchparty_skipped 등)은 건너뛴다. 옛 설정에 없는 timemachine_fallback은 기본값(켬)으로 둔다.
EVENT_ALIASES = {
    "record_started": "record_start",
    "record_finished": "record_end",
    "record_start_failed": "start_failed",
    "record_abnormally_stopped": "abnormal_end",
    "record_user_stopped": "user_stop",
    "postprocess_finished": "finalize_done",
    "postprocess_failed": "finalize_failed",
    "cookie_auth_failed": "cookie_expired",
    "disk_space_low": "disk_low",
}
NOTIFY_KEYS = ("telegram_enabled", "telegram_bot_token", "telegram_chat_id", "events", "limits")
HIDDEN = "설정됨(값 숨김)"


class MigrationError(Exception):
    pass


@dataclass
class Row:
    source: str
    item: str
    result: str  # 가져옴 | 건너뜀 | 무시 | 없음
    detail: str = ""


@dataclass
class Plan:
    settings: dict[str, Any] = field(default_factory=dict)
    channels: list[dict[str, Any]] = field(default_factory=list)
    cookies: dict[str, str] = field(default_factory=dict)
    telegram: dict[str, str] = field(default_factory=dict)
    rows: list[Row] = field(default_factory=list)

    def add(self, source: str, item: str, result: str, detail: object = "") -> None:
        self.rows.append(Row(source, item, result, str(detail)))


def _read(folder: Path, name: str, plan: Plan) -> Any:
    path = folder / name
    if not path.is_file():
        plan.add(name, "(파일)", "없음", "파일이 없습니다")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:  # 읽기만 한다
            return json.load(f)
    except (OSError, ValueError) as e:
        plan.add(name, "(파일)", "건너뜀", f"읽을 수 없습니다: {type(e).__name__}")
        return None


def _bool(value: Any) -> bool:
    return Settings.validate("remux_enabled", value)


class PathMapper:
    """옛 컨테이너의 절대 경로를 새 컨테이너의 경로로 바꾼다(`--map /옛=/새`). 긴 접두사부터 본다."""

    def __init__(self, maps: list[str]):
        pairs = []
        for item in maps:
            if "=" not in item:
                raise MigrationError(f"--map은 옛경로=새경로 형식입니다: {item}")
            old, new = item.split("=", 1)
            if not os.path.isabs(old) or not os.path.isabs(new):
                raise MigrationError(f"--map의 두 경로는 절대 경로여야 합니다: {item}")
            pairs.append((os.path.normpath(old), os.path.normpath(new)))
        self.pairs = sorted(pairs, key=lambda p: len(p[0]), reverse=True)

    def __call__(self, value: str) -> str:
        if not os.path.isabs(value):
            return value
        norm = os.path.normpath(value)
        for old, new in self.pairs:
            if norm == old or norm.startswith(old.rstrip("/") + "/"):
                return os.path.normpath(new + norm[len(old):])
        return norm


def _under_root(record_root: Path, value: str) -> str:
    """녹화 루트 아래면 그 기준 상대 경로를, 아니면 ValidationError."""
    resolved = st.resolve_output_dir(record_root, value)
    root = Path(os.path.realpath(record_root))
    return str(resolved.relative_to(root)) if resolved != root else "."


def build_plan(source: Path, cfg: AppConfig, mapper: PathMapper) -> Plan:
    plan = Plan()
    record_root = cfg.record_root
    files = FileManager(cfg.data_dir, lambda: [], lambda: [])

    # ---- config.json ----
    config = _read(source, "config.json", plan)
    if isinstance(config, dict):
        for key in sorted(config):
            if key not in CONFIG_KEYS:
                plan.add("config.json", key, "무시", "새 도구에 없는 설정")
        for old, new in CONFIG_KEYS.items():
            if old not in config:
                continue
            value = config[old]
            try:
                if old == "plugin_type":
                    converted = value == "timemachine_plus"
                elif old == "fileManagerRoots":
                    roots = value if isinstance(value, list) else [value]
                    kept = []
                    for r in roots:
                        mapped = mapper(str(r))
                        problem = files.root_problem(mapped)
                        if problem:
                            plan.add("config.json", f"{old}: {r}", "건너뜀", f"허용 루트로 쓸 수 없습니다({problem}): {mapped}")
                        else:
                            kept.append(mapped)
                    converted = Settings.validate(new, kept)
                elif old == "replayDownloadRoot":
                    converted = _under_root(record_root, mapper(str(value)))
                elif old == "replayMinFreeGiB":
                    gib = float(value)
                    converted = int(gib * 1024**3)
                else:
                    converted = value
                converted = Settings.validate(new, converted) if new in Settings.VALIDATORS else extensions.validate_legacy(new, converted)
            except (ConfigError, st.ValidationError, TypeError, ValueError) as e:
                # 파일명 패턴을 쓸 수 없으면(모르는 토큰 등) 가져오지 않고 기본 패턴을 쓴다
                tail = f". 기본 패턴({Settings.filename_pattern})을 씁니다" if new == "filename_pattern" else ""
                plan.add("config.json", old, "건너뜀", f"값을 쓸 수 없습니다: {e}{tail}")
                continue
            plan.settings[new] = converted
            shown = list(converted) if isinstance(converted, tuple) else converted
            plan.add("config.json", old, "가져옴", f"{new} = {shown}")

    # ---- notification.json ----
    note = _read(source, "notification.json", plan)
    if isinstance(note, dict):
        for key in sorted(note):
            if key not in NOTIFY_KEYS:
                plan.add("notification.json", key, "무시", "새 도구에 없는 설정")
        for key in ("telegram_bot_token", "telegram_chat_id"):
            value = note.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                plan.telegram[key.removeprefix("telegram_")] = str(value).strip()
                plan.add("notification.json", key, "가져옴", f"비밀 저장소로 옮김. {HIDDEN}")
            elif key in note:
                plan.add("notification.json", key, "건너뜀", "값이 비었습니다")
        events = note.get("events")
        if isinstance(events, dict):
            items = list(events.items())
        elif isinstance(events, list):
            items = [(name, True) for name in events]
        else:
            items = []
        enabled_names = set()
        for name, on in items:
            key = str(name).removeprefix("notify_")
            key = EVENT_ALIASES.get(key, key)
            if key in notify.EVENTS:
                try:
                    plan.settings[f"notify_{key}"] = _bool(on)
                except ConfigError:
                    plan.add("notification.json", f"events.{name}", "건너뜀", "참·거짓이 아닙니다")
                    continue
                enabled_names.add(key)
                plan.add("notification.json", f"events.{name}", "가져옴", f"notify_{key} = {plan.settings[f'notify_{key}']}")
            else:
                plan.add("notification.json", f"events.{name}", "건너뜀", "새 도구에 없는 이벤트")
        limits = note.get("limits")
        if isinstance(limits, dict) and "dedupe_seconds" in limits:
            try:
                plan.settings["notify_dedupe_seconds"] = Settings.validate("notify_dedupe_seconds", limits["dedupe_seconds"])
                plan.add("notification.json", "limits.dedupe_seconds", "가져옴",
                         f"notify_dedupe_seconds = {plan.settings['notify_dedupe_seconds']}")
            except ConfigError as e:
                plan.add("notification.json", "limits.dedupe_seconds", "건너뜀", str(e))
        if isinstance(limits, dict):
            for key in sorted(limits):
                if key != "dedupe_seconds":
                    plan.add("notification.json", f"limits.{key}", "무시", "새 도구에 없는 설정")
        if "telegram_enabled" in note:
            try:
                enabled = _bool(note["telegram_enabled"])
            except ConfigError:
                enabled = None
            if enabled and not (plan.telegram.get("bot_token") and plan.telegram.get("chat_id")):
                plan.add("notification.json", "telegram_enabled", "건너뜀", "토큰이나 chat_id가 없어 알림을 켤 수 없습니다")
            elif enabled is None:
                plan.add("notification.json", "telegram_enabled", "건너뜀", "참·거짓이 아닙니다")
            else:
                plan.settings["notify_enabled"] = enabled
                plan.add("notification.json", "telegram_enabled", "가져옴", f"notify_enabled = {enabled}")

    # ---- cookie.json ----
    cookie = _read(source, "cookie.json", plan)
    # 예전 앱(치지직 전용)의 설정이다. 쿠키·채널은 치지직 플랫폼 몫으로 옮긴다
    chzzk = registry.get("chzzk")
    cookie_keys = chzzk.credential_spec.names
    if isinstance(cookie, dict):
        chz = cookie.get("chzzk") if isinstance(cookie.get("chzzk"), dict) else {}
        for key in sorted(cookie):
            if key != "chzzk":
                plan.add("cookie.json", key, "무시", "치지직 쿠키만 옮깁니다")
        for key in cookie_keys:
            value = chz.get(key)
            if isinstance(value, str) and value.strip():
                plan.cookies[key] = value.strip()
                plan.add("cookie.json", f"chzzk.{key}", "가져옴", f"비밀 저장소로 옮김. {HIDDEN}")
            else:
                plan.add("cookie.json", f"chzzk.{key}", "없음", "값이 없습니다")
        for key in sorted(chz):
            if key not in cookie_keys:
                plan.add("cookie.json", f"chzzk.{key}", "무시", f"{'·'.join(cookie_keys)}만 옮깁니다")
        try:
            _check_cookies(plan.cookies)
        except ValueError as e:
            plan.cookies.clear()
            plan.add("cookie.json", "chzzk", "건너뜀", f"쓸 수 없는 값: {e}")

    # ---- channels.json ----
    channels = _read(source, "channels.json", plan)
    if channels is not None and not isinstance(channels, list):
        plan.add("channels.json", "(파일)", "건너뜀", "배열이 아닙니다")
        channels = None
    seen: set[str] = set()
    remux = plan.settings.get("remux_enabled", Settings().remux_enabled)
    for i, rec in enumerate(channels or []):
        label = f"[{i}]"
        if not isinstance(rec, dict):
            plan.add("channels.json", label, "건너뜀", "객체가 아닙니다")
            continue
        cid = str(rec.get("id") or "").strip().lower()
        label = f"[{i}] {rec.get('name') or cid}"
        if rec.get("platform") != "chzzk":
            plan.add("channels.json", label, "건너뜀", f"치지직 채널이 아닙니다(platform={rec.get('platform')!r})")
            continue
        if rec.get("recordWatchParty"):
            plan.add("channels.json", label, "건너뜀", "같이보기 조건(recordWatchParty)은 범위 밖입니다")
            continue
        if not registry.get("chzzk").valid_channel_id(cid):
            plan.add("channels.json", label, "건너뜀", "채널 ID가 32자리 UID가 아닙니다")
            continue
        if cid in seen:
            plan.add("channels.json", label, "건너뜀", "같은 채널 ID가 이미 있습니다")
            continue
        try:
            folder = _under_root(record_root, mapper(str(rec.get("output_dir") or "")))
            ext = st.normalize_extension(str(rec.get("extension") or ".mp4"))
            if ext == ".ts" and not remux:
                raise st.ValidationError("변환이 꺼져 있으면 .ts를 쓸 수 없습니다")
            auto = _bool(rec.get("record_enabled", True))
        except (st.ValidationError, ConfigError) as e:
            plan.add("channels.json", label, "건너뜀", str(e))
            continue
        name = str(rec.get("name") or "").strip() or cid[:8]
        q = quality.normalize(rec.get("quality"))
        seen.add(cid)
        plan.channels.append({"channel_id": cid, "name": name, "output_dir": folder, "quality": q, "extension": ext,
                              "auto_record": auto})
        plan.add("channels.json", label, "가져옴", f"폴더 {folder}, 화질 {q}, 확장자 {ext}, 자동 녹화 {'켬' if auto else '끔'}")
        for key in sorted(rec):
            if key not in CHANNEL_KEYS and key != "recordWatchParty":
                plan.add("channels.json", f"{label}.{key}", "무시", "범위 밖 값")
    plan.add("(다시보기 작업 내역)", "replay.sqlite3", "건너뜀", "작업 내역은 옮기지 않습니다")
    plan.add("(계정)", "account", "건너뜀", "계정은 옮기지 않습니다. `account create`로 새로 만드십시오")
    return plan


def _check_cookies(values: dict[str, str]) -> None:
    for k, v in values.items():
        if any(c in v for c in ';,\r\n" '):
            raise ValueError(f"{k}에 쓸 수 없는 문자가 있습니다")


def check_target(cfg: AppConfig) -> None:
    """이미 쓰고 있는 데이터 폴더에는 덮어쓰지 않는다."""
    from .recording.engine import InstanceLock

    if InstanceLock.held(cfg.lock_path):
        raise MigrationError("서버(web·serve)가 이 데이터 폴더로 돌고 있습니다. 멈춘 뒤 다시 하십시오")
    problems = []
    if cfg.db_path.exists():
        store = st.Store(cfg.db_path, cfg.record_root)
        try:
            if store.list_channels():
                problems.append("채널")
        finally:
            store.close()
    if secretstore.account_exists(cfg.account_path):
        problems.append("관리자 계정")
    if any(credentials.load(cfg, p.id) for p in registry.all_platforms()):
        problems.append("쿠키")
    if secretstore.telegram_load(cfg.telegram_path):
        problems.append("텔레그램 값")
    if problems:
        raise MigrationError("데이터 폴더에 이미 " + "·".join(problems) + "이(가) 있습니다. 덮어쓰지 않고 멈춥니다")


def apply_plan(plan: Plan, cfg: AppConfig) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if plan.telegram:
        secretstore.telegram_save(cfg.telegram_path, plan.telegram.get("bot_token"), plan.telegram.get("chat_id"))
    if plan.cookies:
        credentials.save(cfg, "chzzk", plan.cookies)
    store = st.Store(cfg.db_path, cfg.record_root, cfg.telegram_path)
    try:
        core = {k: v for k, v in plan.settings.items() if k in Settings.VALIDATORS}
        store.set_settings(core)  # 전부 검증한 뒤 한 트랜잭션
        # 다시보기 설정과 사용 여부는 확장 몫이다. 확장이 정한 대로 넣는다
        extensions.import_legacy_settings(store, {k: v for k, v in plan.settings.items() if k not in core})
        for ch in plan.channels:
            store.add_channel(**ch)
    finally:
        store.close()


def render(plan: Plan, applied: bool) -> str:
    lines = ["| 출처 | 항목 | 결과 | 값·이유 |", "|---|---|---|---|"]
    for r in plan.rows:
        lines.append(f"| {r.source} | {r.item} | {r.result} | {r.detail} |")
    counts = {k: sum(1 for r in plan.rows if r.result == k) for k in ("가져옴", "건너뜀", "무시", "없음")}
    lines.append("")
    lines.append(f"가져옴 {counts['가져옴']} · 건너뜀 {counts['건너뜀']} · 무시 {counts['무시']} · 없음 {counts['없음']}"
                 f" · 채널 {len(plan.channels)}개 · 설정 {len(plan.settings)}개")
    lines.append("적용했습니다." if applied else "미리 보기입니다. 아무것도 쓰지 않았습니다. 쓰려면 --apply를 붙이십시오.")
    return "\n".join(lines)


def run(cfg: AppConfig, source: str, maps: list[str], apply: bool) -> str:
    folder = Path(source)
    if not folder.is_dir():
        raise MigrationError(f"기존 설정 폴더가 없습니다: {source}")
    check_target(cfg)
    plan = build_plan(folder, cfg, PathMapper(maps))
    if apply:
        apply_plan(plan, cfg)
    return render(plan, apply)
