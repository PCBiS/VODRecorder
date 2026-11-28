"""downloads 확장의 경로 (VOD). 화면의 "다시보기" 탭이 쓰는 API다.

중심부의 웹 서버를 부르지 않는다. 확장 접점(ExtensionContext)과 사건만 쓴다.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from ... import files
from ...core.extensions import ExtensionContext
from . import api
from .extension import settings_view
from .manager import OWNER, ReplayError


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 정해진 키만 받는다


class AnalyzeBody(Strict):
    urls: list[StrictStr]


class JobItem(Strict):
    url: StrictStr
    quality: StrictStr
    section: StrictStr | None = None


class JobsBody(Strict):
    analysis_id: StrictStr
    items: list[JobItem]
    key: StrictStr
    confirm: StrictBool = False  # 중복을 확인했다는 표시


class ControlBody(Strict):
    paused: StrictBool  # 전체 일시정지


def stream_file(fd: int, size: int, name: str) -> StreamingResponse:
    from urllib.parse import quote

    disposition = "attachment; filename*=UTF-8''" + quote(name, safe="")
    return StreamingResponse(files.iter_fd(fd), media_type="application/octet-stream",
                             headers={"Content-Disposition": disposition, "Content-Length": str(size)})


def build(ctx: ExtensionContext) -> APIRouter:
    r = APIRouter()
    replay = ctx.service

    # ---- 경로 ----
    @r.get("/api/replay/kinds")
    async def replay_kinds():
        """켜진 작업 종류(화면이 탭 이름과 주소 칸을 맞춘다)."""
        return {"kinds": [{"kind": k.kind, "title": k.title, "platform": k.platform, "example": k.example} for k in api.kinds()]}

    @r.get("/api/replay/status")
    async def replay_status():
        settings = await asyncio.to_thread(settings_view, ctx)
        manager = replay.manager
        return {"enabled": True, "ready": manager is not None, "error": replay.error,
                "folder": settings.replay_dir, "connections": settings.replay_connections,
                "busy": bool(manager and manager.has_active_work()),
                "active": manager.active_count() if manager else 0,  # 배지
                "paused_jobs": manager.paused_count() if manager else 0,
                "admission": admission_view(manager.gate() if manager else replay.admission())}  # 위쪽 안내

    def admission_view(state: dict[str, Any]) -> dict[str, Any]:
        keys = ("open", "reason", "reason_label", "reasons", "suspend", "paused", "window_enabled", "end_action",
                "pause_while_recording", "recording", "finalizing", "closes_at", "next_open_at", "error", "space_need")
        return {k: state.get(k) for k in keys}

    @r.put("/api/replay/control")
    async def replay_control(body: ControlBody):
        """전체 일시정지 스위치. 설정(replay_paused)에 두므로 재시작 뒤에도 유지한다."""
        await asyncio.to_thread(ctx.store.set_extra, {"ext:downloads:replay_paused": body.paused})
        replay.poke()
        return await replay_status()

    @r.post("/api/replay/cancel-active")
    async def replay_cancel_active():
        """대기·진행 전체 취소."""
        return await replay.require().cancel_active(OWNER)

    @r.post("/api/replay/analyze")
    async def replay_analyze(body: AnalyzeBody):
        return await replay.require().analyze(OWNER, body.urls)

    @r.get("/api/replay/analyses/{ident}")
    async def replay_analysis(ident: str):
        return replay.require().get_analysis(OWNER, ident)

    @r.post("/api/replay/jobs")
    async def replay_create(body: JobsBody):
        items = [item.model_dump() for item in body.items]
        return await replay.require().create_jobs(OWNER, body.analysis_id, items, body.key, body.confirm)

    @r.get("/api/replay/jobs")
    async def replay_jobs(status: str | None = Query(None, max_length=20)):
        return replay.require().list_jobs(OWNER, status)

    @r.get("/api/replay/jobs/{ident}")
    async def replay_job(ident: str):
        return replay.require().get_job(OWNER, ident)

    @r.get("/api/replay/jobs/{ident}/logs")
    async def replay_logs(ident: str, after: int = Query(0, ge=0)):
        return replay.require().logs(OWNER, ident, after)

    @r.post("/api/replay/jobs/{ident}/cancel")
    async def replay_cancel(ident: str):
        return await replay.require().cancel(OWNER, ident)

    @r.post("/api/replay/jobs/remove-unfinished")
    async def replay_remove_unfinished():
        """실패·취소·중단한 작업의 내역을 모두 지운다."""
        return replay.require().remove_unfinished(OWNER)

    @r.delete("/api/replay/jobs/{ident}")
    async def replay_remove(ident: str):
        """실패·취소·중단한 작업 하나의 내역을 지운다."""
        return replay.require().remove(OWNER, ident)

    @r.post("/api/replay/jobs/{ident}/retry")
    async def replay_retry(ident: str):
        return await replay.require().retry(OWNER, ident)

    @r.post("/api/replay/jobs/{ident}/pause")
    async def replay_pause(ident: str):
        """작업 일시정지. 부분 파일을 남긴다."""
        return await replay.require().pause(OWNER, ident)

    @r.post("/api/replay/jobs/{ident}/resume")
    async def replay_resume(ident: str):
        """일시정지한 작업 재개. 이어받는다."""
        return await replay.require().resume(OWNER, ident)

    @r.get("/api/replay/jobs/{ident}/file")
    async def replay_file(ident: str):
        path = replay.require().resolve_file(OWNER, ident)
        fd = await asyncio.to_thread(os.open, path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        return stream_file(fd, os.fstat(fd).st_size, path.name)

    from .lists import build as build_lists

    r.include_router(build_lists(ctx, ctx.lists))  # 채널 다시보기 목록
    return r
