"""다시보기 설정 적용.

저장 경로는 다운로드 기준 폴더(여기서는 녹화 루트) 밖을 거부하고,
쓸 수 있는지 미리 확인하고, 대기·진행 중인 작업이 있으면 변경을 거부한다.
이 도구의 설정은 SQLite 트랜잭션으로 저장되므로, 적용에 실패하면 이전 값으로 다시 연다.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ...platforms import credentials, registry
from . import api
from ...core.config import AppConfig
from ...core.store import ValidationError, resolve_output_dir
from . import admission
from .manager import ReplayError, ReplayManager
from .store import MigrationError

KEYS = ("replay_dir", "replay_min_free_bytes")
STARTUP_ERROR = "다시보기 저장소를 준비하지 못했습니다. 저장 경로와 권한을 확인하십시오"
MIGRATION_ERROR = "다시보기 저장소를 새 형식으로 옮기지 못했습니다. 저장소는 원래대로 두었습니다. 로그를 확인하십시오"
ACTIVE_ERROR = "분석·다운로드 작업이 대기·진행 중이거나 일시정지돼 있습니다(부분 파일이 저장 폴더에 있습니다). 모든 작업이 끝나거나 취소한 뒤 설정을 바꾸십시오"


class ReplayService:
    def __init__(self, cfg: AppConfig, get_settings: Callable[[], Any], *, worker_command: list[str] | None = None,
                 disk_free: Callable[[Path], int] | None = None, analysis_timeout: float = 180,
                 activity: Callable[[], dict[str, int]] | None = None,
                 clock: Callable[[], datetime] | None = None,
                 recording_channels: Callable[[], list[tuple[str, str]]] | None = None,
                 request_gate: Callable[[str], Any] | None = None):
        self.cfg = cfg
        # 녹화 엔진의 상태를 읽기만 하는 함수(admission.engine_activity). 없으면 녹화가 없는 것으로 본다
        self.activity = activity or (lambda: {"recording": 0, "finalizing": 0})
        self.clock = clock or (lambda: datetime.now(timezone.utc))  # 시험은 시계를 바꿔 넣는다
        self._admission: dict[str | None, tuple[float, dict[str, Any]]] = {}  # 대상(None은 전체) → (잰 때, 상태)
        self.get_settings = get_settings
        self.worker_command = worker_command
        self.disk_free = disk_free
        self.analysis_timeout = analysis_timeout
        self.manager: ReplayManager | None = None
        # 공개 접점(api.Downloads). 대기열 저장소를 열고 닫을 때 붙은 일(다시보기 목록 수집 등)을 같이 열고 닫는다
        self.facade = api.Downloads(self)
        self.recording_channels = recording_channels
        self.request_gate = request_gate
        self.values: tuple | None = None
        self.error: str | None = None
        self.lock = asyncio.Lock()

    def root_for(self, settings) -> Path:
        return resolve_output_dir(self.cfg.record_root, settings.replay_dir)

    def _extras(self, platform_id: str | None = None) -> dict:
        settings = self.get_settings()
        extras: dict[str, Any] = {"connections": settings.replay_connections}
        platform = registry.find(platform_id) if platform_id else None
        if platform is not None:  # 작업 종류의 플랫폼. 자격은 플랫폼 자격 저장으로만 읽는다
            extras["cookies"] = credentials.load(self.cfg, platform.id)
            if hasattr(platform, "api_base"):
                extras["api_base"] = platform.api_base(self.cfg)
        if self.cfg.allow_http:
            extras["allow_http"] = True
        return extras

    def _open(self, settings) -> ReplayManager:
        return ReplayManager(self.cfg.data_dir / "replay", self.root_for(settings), settings.replay_min_free_bytes,
                             worker_command=self.worker_command, download_base=self.cfg.record_root,
                             ffprobe=self.cfg.ffprobe, ffmpeg=self.cfg.ffmpeg, task_extras=self._extras,
                             disk_free=self.disk_free, analysis_timeout=self.analysis_timeout,
                             low_priority=lambda: self.get_settings().finalize_low_priority,
                             admission=self.admission, request_gate=self.request_gate)

    def admission(self, target: str | None = None) -> dict[str, Any]:
        """전체 일시정지·녹화 중·시간대로 정한 입장 상태(여유 공간은 관리자가 더한다). 1초 동안 다시 쓴다.
        target(플랫폼:채널 ID)을 주면 그 채널의 작업에 쓸 상태다."""
        moment = time.monotonic()
        cached = self._admission.get(target)
        if cached is not None and moment - cached[0] < 1.0:
            return cached[1]
        settings = self.get_settings()
        activity = None
        if settings.replay_pause_while_recording:
            try:
                activity = self.activity()
            except Exception:  # noqa: BLE001 - 녹화 상태를 모르면 녹화 중으로 본다(녹화를 방해하지 않는 쪽)
                activity = {"recording": 1, "finalizing": 0}
        state = admission.evaluate(settings, self.clock(), activity, target=target)
        if len(self._admission) > 200:
            self._admission.clear()
        self._admission[target] = (moment, state)
        return state

    def poke(self) -> None:
        """설정을 바꾼 뒤: 입장 상태를 바로 다시 따지고 대기열을 깨운다."""
        self._admission.clear()
        if self.manager is not None:
            self.manager.wake["job"].set()

    async def start(self) -> None:
        async with self.lock:
            settings = await asyncio.to_thread(self.get_settings)
            await self._apply(settings, starting=True)

    async def _close_manager(self) -> None:
        if self.manager is not None:
            await self.facade.closing()
            manager, self.manager = self.manager, None
            await manager.close()

    async def _apply(self, settings, starting: bool = False) -> None:
        values = tuple(getattr(settings, k) for k in KEYS)
        await self._close_manager()
        self.values = values
        self.error = None
        try:  # 확장이 켜져 있으면 늘 연다(켜고 끄기는 확장의 몫)
            manager = self._open(settings)
            await manager.start()
            self.manager = manager
            await self.facade.opened(manager.db)  # 붙은 일(다시보기 목록 수집 등)도 같은 저장소로 연다
        except MigrationError:
            # 기동할 때 옮기지 못하면 서버가 뜨지 않는다
            self.error = MIGRATION_ERROR
            if starting:
                raise
        except (OSError, ValueError, ValidationError):
            self.error = STARTUP_ERROR

    async def check_change(self, changes: dict) -> None:
        """저장하기 전에 부른다. 진행 중인 작업이 있거나 새 경로에 쓸 수 없으면 거부한다."""
        if not any(k in changes for k in KEYS):
            return
        current = await asyncio.to_thread(self.get_settings)
        if all(getattr(current, k) == changes.get(k, getattr(current, k)) for k in KEYS):
            return
        if self.manager is not None and self.manager.has_active_work():
            raise ReplayError(409, ACTIVE_ERROR)
        folder = changes.get("replay_dir", current.replay_dir)
        try:
            root = resolve_output_dir(self.cfg.record_root, str(folder))
            await asyncio.to_thread(_preflight, root)
        except (OSError, ValidationError):
            raise ReplayError(400, "다시보기 저장 경로에 쓸 수 없습니다. 녹화 루트 아래 경로와 권한을 확인하십시오") from None

    async def reload(self) -> None:
        """설정을 저장한 뒤 부른다. 값이 바뀌었으면 대기열을 닫고 새 값으로 다시 연다(저장 즉시 적용)."""
        async with self.lock:
            settings = await asyncio.to_thread(self.get_settings)
            if tuple(getattr(settings, k) for k in KEYS) != self.values:
                await self._apply(settings)

    async def close(self) -> None:
        async with self.lock:
            await self._close_manager()

    def active_paths(self) -> list[str]:
        return [str(p) for p in self.manager.active_paths()] if self.manager else []

    def require(self) -> ReplayManager:
        if self.manager is None:
            raise ReplayError(409, self.error or "다시보기가 꺼져 있습니다")
        return self.manager


def _preflight(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".replay-settings-", dir=root)
    os.close(fd)
    os.unlink(name)
