"""downloads 확장: 작업 대기열과 작업 화면, 일시정지·이어받기, 다운로드 시간대, 녹화 중 멈춤, 여유 공간 판단,
작업 저장소(replay.sqlite3)와 받은 기록.

중심부와는 확장 접점과 사건으로만 이어진다:
- 녹화 중 멈춤: 녹화·마무리 사건(`record_*`·`finalize_*`)과 저장소의 열린 세션으로 판단한다
- 설정: 이 확장의 설정 키(`replay_*`)는 설정 API에서 중심부 설정과 함께 보이고, 저장 전후에 `settings_check`·`settings_applied`
- 파일 관리자: 다시보기 저장 폴더(`file_roots`), 받는 중인 파일(`lock_targets`)
- 로그 내려받기: 작업 로그(`replay/job-logs.jsonl`), 가릴 제목·채널, 진단 요약
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ...core import config
from ...core.config import ConfigError
from ...core.events import Event
from ...core.extensions import KEY_OPERATE, KEY_VIEW, LOGIN, AccessRule, Extension, ExtensionContext, LogFile, SettingSpec
from ...core.store import ValidationError, resolve_output_dir
from . import api, schedule
from .manager import ReplayError

JOB_LOG_FILE = "replay/job-logs.jsonl"
JOB_LOG_FIELDS = ("job", "at", "level", "message")
STATIC = Path(__file__).resolve().parent / "static"


def _window_rules(value: Any) -> tuple[dict[str, Any], ...]:
    """다운로드 허용 시간대 규칙 목록."""
    try:
        rules = schedule.parse_rules(value)
    except ValueError as e:
        raise ConfigError(f"시간대 규칙: {e}") from None
    return tuple(schedule.rules_to_json(rules))


def _end_action(value: Any) -> str:
    """시간대가 끝날 때 받는 중인 작업: 멈추고 다음 시간대에 이어받기(pause), 끝까지 받기(finish)."""
    if value not in ("pause", "finish"):
        raise ConfigError("시간대 끝 동작은 pause나 finish입니다")
    return value


# 이전 빌드의 Settings에 있던 키 그대로(설정 API의 키 이름도 같다)
SETTINGS = {
    "replay_dir": SettingSpec("replay", config.rel_dir, "저장 폴더", path=True),
    "replay_min_free_bytes": SettingSpec(5 * 1024**3, config.int_range(0, None), "최소 여유 공간"),
    "replay_connections": SettingSpec(16, config.int_range(1, 16), "동시 연결 수"),
    # HLS형(조각으로 받는 다시보기)에서 함께 받는 조각 수. 조각마다 fsync하므로 저장소가 느리면 늘려도 빨라지지 않는다
    "replay_segment_connections": SettingSpec(4, config.int_range(1, 16), "조각 동시 받기 수"),
    "replay_paused": SettingSpec(False, config.bool_value, "전체 일시정지"),
    "replay_window_enabled": SettingSpec(False, config.bool_value, "다운로드 시간대"),
    "replay_window_rules": SettingSpec((), _window_rules, "시간대 규칙", list_value=True),
    "replay_window_end_action": SettingSpec("pause", _end_action, "시간대 끝 동작"),
    "replay_pause_while_recording": SettingSpec(True, config.bool_value, "녹화 중 멈춤"),
}


class Activity:
    """녹화 중인 채널 수와 마무리할 녹화 수. 사건(녹화 시작·끝)과 저장소의 열린 세션으로 센다."""

    def __init__(self) -> None:
        self.recording: set[tuple] = set()

    def on_event(self, event: Event) -> None:
        key = (event.platform, event.channel_id, event.values.get("session_id"))
        if event.name == "record_started":
            self.recording.add(key)
        elif event.name == "record_ended":
            self.recording.discard(key)

    def read(self, store) -> dict[str, int]:
        if store is None:
            return {"recording": len(self.recording), "finalizing": 0}
        return {"recording": max(len(self.recording), len(store.open_sessions())),
                "finalizing": len(store.unfinalized_sessions())}


def settings_view(ctx: ExtensionContext) -> SimpleNamespace:
    """작업 대기열이 읽는 설정: 이 확장의 설정과 중심부의 변환 우선순위."""
    values = ctx.settings()
    core = ctx.core_settings()
    return SimpleNamespace(**values, finalize_low_priority=core.finalize_low_priority)


class DownloadsExtension(Extension):
    name = "downloads"
    title = "다운로드"
    description = "다시보기·클립을 받는 작업 대기열입니다. 일시정지·이어받기, 다운로드 시간대, 녹화 중 멈춤을 다룹니다."
    version = "1.0"
    legacy_enabled_key = "replay_enabled"
    legacy_enabled_default = True  # 확장 구조 전의 저장소에서 올라오면 다시보기를 켠다
    legacy_settings = tuple(SETTINGS)
    # 키 권한은 경로마다 밝힌다. 밝히지 않은 /api/replay/ 경로는 키로 열리지 않는다(아래 묶음 규칙)
    access_rules = tuple(AccessRule(m, p, LOGIN, KEY_VIEW) for m, p in (
        ("GET", "/api/replay/kinds"), ("GET", "/api/replay/status"), ("GET", "/api/replay/analyses/{ident}"),
        ("GET", "/api/replay/jobs"), ("GET", "/api/replay/jobs/{ident}"), ("GET", "/api/replay/jobs/{ident}/logs"),
        ("GET", "/api/replay/jobs/{ident}/file"))) + tuple(AccessRule(m, p, LOGIN, KEY_OPERATE) for m, p in (
        ("PUT", "/api/replay/control"), ("POST", "/api/replay/cancel-active"), ("POST", "/api/replay/analyze"),
        ("POST", "/api/replay/jobs"), ("POST", "/api/replay/jobs/{ident}/cancel"), ("POST", "/api/replay/jobs/{ident}/retry"),
        ("POST", "/api/replay/jobs/{ident}/pause"), ("POST", "/api/replay/jobs/{ident}/resume"),
        ("POST", "/api/replay/jobs/remove-unfinished"), ("DELETE", "/api/replay/jobs/{ident}"))) + (
        # 채널 다시보기 목록(공급자는 확장이 등록한다). 수집 목록은 보기, 수집 시작·채널 등록·고른 영상 받기는 운영
        AccessRule("GET", "/api/replay/channels", LOGIN, KEY_VIEW),
        AccessRule("GET", "/api/replay/channels/{channel_id}/months", LOGIN, KEY_VIEW),
        AccessRule("GET", "/api/replay/channels/{channel_id}/videos", LOGIN, KEY_VIEW),
        AccessRule("POST", "/api/replay/channels", LOGIN, KEY_OPERATE),
        AccessRule("POST", "/api/replay/channels/{channel_id}/collect", LOGIN, KEY_OPERATE),
        AccessRule("POST", "/api/replay/channels/{channel_id}/collect/cancel", LOGIN, KEY_OPERATE),
        AccessRule("DELETE", "/api/replay/channels/{channel_id}", LOGIN, KEY_OPERATE),
        AccessRule("POST", "/api/replay/select", LOGIN, KEY_OPERATE)) + \
        tuple(AccessRule(m, "/api/replay/*", LOGIN) for m in ("GET", "POST", "PUT", "DELETE"))
    settings = SETTINGS
    http_errors = (ReplayError,)
    log_not_included = ("db:replay.sqlite3", "media:다시보기")
    log_fields: dict[str, str] = {}
    static_dir = STATIC
    scripts = ("downloads.js", "catalog.js")  # catalog.js: 채널 다시보기 목록(공급자가 있을 때만 보인다)
    ui_dir = Path(__file__).resolve().parent / "ui"
    # 화면 문서의 자리에 넣는 조각: 위쪽 탭, 다시보기 화면, 설정의 하위 탭과 그 내용, 중심부 문구에 덧붙이는 말
    html = {"nav": "nav.html", "views": "view.html", "settings-tabs": "settings-tab.html",
            "settings-panels": "settings-panel.html", "fm-roots": "fm-roots.html", "export-files": "export-files.html"}

    # ---- 등록 ----
    def routes(self, ctx: ExtensionContext):
        from .lists import Lists
        from .routes import build
        from .service import ReplayService

        ctx.activity = Activity()
        ctx.service = ReplayService(ctx.cfg, lambda: settings_view(ctx), disk_free=ctx.disk_free,
                                    request_gate=lambda pid: ctx.request_gate(pid),  # 중심부의 창구를 접점으로
                                    activity=lambda: ctx.activity.read(ctx.store))  # 사건과 저장소로 센다
        ctx.lists = Lists(ctx)  # 채널 다시보기 목록의 공통 부분
        return build(ctx)

    def open_storage(self, ctx: ExtensionContext) -> None:
        api._set_current(ctx.service.facade)  # replay·clips가 이 뒤에 열리며 붙는다(켜는 순서)
        ctx.service.facade.attach(ctx.lists.opened, ctx.lists.closing)  # 목록 수집기도 대기열 저장소로 연다
        ctx.service.facade.lists = ctx.lists  # 공급자를 낸 확장이 수집기를 볼 수 있게(시험·로그)

    def listeners(self, ctx: ExtensionContext) -> dict:
        return {"record_started": ctx.activity.on_event, "record_ended": ctx.activity.on_event,
                "channel_added": ctx.lists.on_channel_event, "channel_removed": ctx.lists.on_channel_event}

    async def start(self, ctx: ExtensionContext) -> None:
        import asyncio

        ctx.lists.loop = asyncio.get_running_loop()
        await ctx.service.start()  # 저장소를 옮기지 못하면(MigrationError) 서버가 뜨지 않는다

    async def stop(self, ctx: ExtensionContext) -> None:
        ctx.lists.loop = None
        await ctx.service.close()
        ctx.service.facade.detach_all()
        if api.current() is ctx.service.facade:
            api._set_current(None)

    # ---- 설정 ----
    async def settings_check(self, ctx: ExtensionContext, changes: dict[str, Any]) -> None:
        await ctx.service.check_change(changes)  # 진행 중인 작업이 있으면 거부
        mine = {k: SETTINGS[k].validate(v) for k, v in changes.items() if k in SETTINGS}
        if "replay_dir" in mine:
            try:
                resolve_output_dir(ctx.cfg.record_root, mine["replay_dir"])  # 녹화 루트 밖이면 거부
            except ValidationError as e:
                raise ConfigError(str(e)) from None
        if "replay_window_enabled" in mine or "replay_window_rules" in mine:
            enabled = mine.get("replay_window_enabled", ctx.setting("replay_window_enabled"))
            rules = mine.get("replay_window_rules", ctx.setting("replay_window_rules"))
            if enabled and not any(not rule.get("target") for rule in rules):  # 채널을 지정한 규칙은 그 채널에만 쓴다
                raise ConfigError("다운로드 시간대를 켜려면 대상이 전체인 규칙이 하나 이상 있어야 합니다")

    async def settings_applied(self, ctx: ExtensionContext, changes: dict[str, Any]) -> None:
        await ctx.service.reload()  # 저장 즉시 적용
        ctx.service.poke()  # 일시정지·시간대 설정도 바로 따진다

    # ---- 파일 관리자·로그 내려받기 ----
    def file_roots(self, ctx: ExtensionContext) -> list[tuple[str, str, str]]:
        try:
            return [(str(ctx.service.root_for(settings_view(ctx))), "다시보기", "replay")]
        except ValidationError:
            return []

    def lock_targets(self, ctx: ExtensionContext) -> list[Path]:
        return [Path(p) for p in ctx.service.active_paths()]

    def log_export(self, ctx: ExtensionContext) -> list:
        manager = ctx.service.manager
        rows = [dict(r) for r in manager.db.execute("SELECT job, at, level, message FROM logs ORDER BY seq")] if manager else []
        return [LogFile(JOB_LOG_FILE, rows, time_key="at", fields=JOB_LOG_FIELDS)]

    def log_names(self, ctx: ExtensionContext) -> dict[str, list]:
        manager = ctx.service.manager
        if manager is None:
            return {}
        # 작업의 제목(공유용에서 글 속의 제목을 [제목]으로 바꾸는 데 쓴다)와 다시보기 채널(목록 수집)
        collector = ctx.lists.collector
        channels = [(c["channel_id"], c["name"]) for c in collector.channels(every=True)] if collector else []
        return {"channels": channels, "titles": [json.loads(r[0]).get("title") for r in manager.db.execute("SELECT payload FROM records WHERE kind='job'")]}


EXTENSION = DownloadsExtension()
