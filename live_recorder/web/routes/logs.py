"""경로 묶음: 로그 보기와 로그 내려받기. 로그 파일 밖의 줄은 켜진 확장(`log_export`)에서 받는다. 다른 묶음을 부르지 않는다."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import StrictBool

from ... import logexport, logview
from ..auth import Strict

if TYPE_CHECKING:
    from ..context import WebContext

log = logging.getLogger("live_recorder.web")


class ExportBody(Strict):
    range: Literal["1", "3", "7", "all"] = "3"
    strict: StrictBool = False  # 더 가리기(기본 꺼짐)


def build(ctx: "WebContext") -> APIRouter:
    r = APIRouter()
    export_lock = asyncio.Lock()

    @r.get("/api/logs")
    async def logs_view(limit: int = Query(logview.PAGE_DEFAULT, ge=1, le=logview.PAGE_MAX),
                        level: str = Query("", max_length=10), channel: str = Query("", max_length=200),
                        q: str = Query("", max_length=200), before: str = Query("", max_length=60),
                        after: str = Query("", max_length=60)):
        """로그 보기. 새것부터 한 쪽(`before`: 더 오래된 쪽) 또는 `after` 뒤에 새로 쓰인 줄. 읽기만 하고 스레드에서 읽는다."""
        lv = level.strip().upper()
        if lv and lv not in logview.LEVELS:
            raise HTTPException(400, "수준은 DEBUG·INFO·WARNING·ERROR·CRITICAL 가운데 하나입니다")
        if before and after:
            raise HTTPException(400, "before와 after는 함께 쓸 수 없습니다")
        flt = logview.Filter(level=logview.LEVELS.get(lv, 0), channel=channel.strip() or None, text=q.strip() or None)
        secrets = ctx.sessions.secrets()
        try:
            if after:
                return await asyncio.to_thread(logview.read_newer, ctx.cfg, after=after, limit=limit, flt=flt,
                                               session_secrets=secrets)
            return await asyncio.to_thread(logview.read_page, ctx.cfg, before=before or None, limit=limit, flt=flt,
                                           session_secrets=secrets)
        except logview.CursorError as e:
            raise HTTPException(400, str(e)) from None

    @r.post("/api/logs/export")
    async def logs_export(body: ExportBody):
        """로그를 zip 하나로 묶어 내려 준다. 메모리에서만 만들고 서버에 남기지 않는다. 한 번에 하나만 만든다."""
        if export_lock.locked():
            raise HTTPException(409, "로그 묶음을 만드는 중입니다. 끝난 뒤 다시 누르십시오")
        async with export_lock:
            # 확장의 DB 연결은 이벤트 루프에서만 쓴다. 줄을 먼저 읽어 두고 묶음은 스레드에서 만든다
            ext = ctx.extensions.log_export()
            name, data, _ = await asyncio.to_thread(logexport.build, ctx.cfg, ctx.engine.store, range_key=body.range,
                                                    strict=body.strict, session_secrets=ctx.sessions.secrets(),
                                                    extensions=ext)
        log.info("로그 묶음을 내려 줬다", extra={"event": "log_export"})
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    return r
