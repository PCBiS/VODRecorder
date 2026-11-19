"""경로 묶음이 같이 쓰는 것. 묶음끼리는 서로 부르지 않고 이 객체만 쓴다.

다른 일에 끼는 기능(다시보기·클립 등)은 확장이다. 확장은 확장 접점(core.extensions)으로만 끼어든다.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from .. import files
from ..core import secretstore
from ..core.config import AppConfig, Settings
from ..core.extensions import ExtensionHost
from ..platforms import registry
from ..recording.engine import Engine
from ..core import apikeys
from .auth import LoginLimiter, Sessions, VerifyQueue
from .keyauth import RateLimiter


def stream_file(fd: int, size: int, name: str) -> StreamingResponse:
    """열린 파일을 내려받기로 보낸다(파일 관리자)."""
    disposition = "attachment; filename*=UTF-8''" + quote(name, safe="")
    return StreamingResponse(files.iter_fd(fd), media_type="application/octet-stream",
                             headers={"Content-Disposition": disposition, "Content-Length": str(size)})


@dataclass
class WebContext:
    cfg: AppConfig
    engine: Engine
    app: FastAPI
    extensions: ExtensionHost
    sessions: Sessions = field(default_factory=Sessions)
    limiter: LoginLimiter = field(default_factory=LoginLimiter)
    # 첫 설정의 코드 실패는 로그인 제한을 꺼 두어도 따로 제한한다(같은 주소 10분 5번, 전체 10분 20번)
    setup_limiter: LoginLimiter = field(default_factory=lambda: LoginLimiter(limit=5, window=600.0, global_limit=20))
    limiter_state: dict[str, bool] = field(default_factory=lambda: {"enabled": True})
    # 틀린 접근 키는 로그인과 같은 주소별 규칙(주소 10분 5번)으로 따로 센다(서로를 막지 않는다).
    # 접근 키는 256비트라 전체 한도를 두지 않는다. 실패했을 때만 센다
    key_limiter: LoginLimiter = field(default_factory=lambda: LoginLimiter(global_limit=None))
    verify_queue: VerifyQueue = field(default_factory=VerifyQueue)  # 비밀번호·설정 코드 검증은 한 번에 하나씩(대기 수 상한)
    key_limiter_state: dict[str, bool] = field(default_factory=lambda: {"enabled": True})
    file_manager: Any = None
    keys: Any = None  # 접근 키 저장소(core.apikeys.KeyStore). 기능을 끄면 None
    key_rates: RateLimiter = field(default_factory=RateLimiter)
    _settings_cache: dict[str, Any] = field(default_factory=lambda: {"at": 0.0, "value": None})
    _account_cache: dict[str, Any] = field(default_factory=lambda: {"mtime": None, "generation": None})

    @property
    def store(self):
        return self.engine.store

    @staticmethod
    def db(fn: Callable, *args: Any, **kwargs: Any):
        return asyncio.to_thread(fn, *args, **kwargs)

    async def current_settings(self) -> Settings:
        """요청마다 저장소를 읽지 않도록 1초 동안 재사용한다."""
        cache = self._settings_cache
        now = time.monotonic()
        if cache["value"] is None or now - cache["at"] > 1.0:
            cache["value"] = await asyncio.to_thread(self.engine.store.get_settings)
            cache["at"] = now
        return cache["value"]

    def forget_settings(self) -> None:
        self._settings_cache["value"] = None

    async def account_generation(self) -> str | None:
        """CLI가 다른 프로세스에서 비밀번호를 바꿔도 알아챈다(파일 수정 시각으로 다시 읽음)."""
        import os

        cache = self._account_cache
        try:
            mtime = (await asyncio.to_thread(os.stat, self.cfg.account_path)).st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if mtime != cache["mtime"]:
            cache["generation"] = await asyncio.to_thread(secretstore.account_generation, self.cfg.account_path)
            cache["mtime"] = mtime
        return cache["generation"]

    def forget_account(self) -> None:
        self._account_cache["mtime"] = None

    async def disk_info(self) -> dict[str, int] | None:
        """녹화 디스크의 여유 공간."""
        def work() -> dict[str, int]:
            total = shutil.disk_usage(self.cfg.record_root).total
            return {"free": int(self.engine.disk_free(self.cfg.record_root)), "total": int(total)}
        try:
            return await asyncio.to_thread(work)
        except OSError:
            return None

    @staticmethod
    def need_platform(platform: str | None):
        found = registry.find(platform or registry.DEFAULT)
        if found is None:
            raise HTTPException(404, "없는 플랫폼")
        return found

    async def need_usable_platform(self, platform: str | None):
        """켜야 쓰는 플랫폼이 꺼져 있으면 409와 "먼저 켜 주십시오" 안내."""
        found = self.need_platform(platform)
        if found.opt_in and not await asyncio.to_thread(self.engine.platform_on, found.id):
            raise HTTPException(409, found.opt_in_notice(self.cfg).get("need_enable") or "먼저 이 플랫폼을 켜 주십시오")
        return found

    def need_channel_id(self, channel_id: str, platform: str | None = None) -> str:
        """채널은 (플랫폼, 채널 ID)다. 주소의 채널 ID와 `?platform=`(없으면 기본 플랫폼)으로 가리킨다."""
        if not self.need_platform(platform).valid_channel_id(channel_id):
            raise HTTPException(404, "없는 채널")
        return channel_id
