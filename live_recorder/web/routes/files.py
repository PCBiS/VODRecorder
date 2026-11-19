"""경로 묶음: 파일 관리자. 허용 루트와 잠금 대상은 중심부와 다른 묶음·확장이 `ctx`에 단 원천에서 모은다.
다른 묶음을 부르지 않는다."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import StrictStr

from ... import files
from ...core.config import Settings
from ..auth import Strict
from ..context import stream_file

if TYPE_CHECKING:
    from ..context import WebContext


# 아래 모델의 필드 이름은 files.FIELDS와 같아야 한다(시험이 확인한다)
class MkdirBody(Strict):
    root: StrictStr
    path: StrictStr = ""
    name: StrictStr


class RenameBody(Strict):
    root: StrictStr
    path: StrictStr
    new_name: StrictStr


class MoveBody(Strict):
    root: StrictStr
    paths: list[StrictStr]
    dest_root: StrictStr
    dest_path: StrictStr = ""


class DeleteBody(Strict):
    root: StrictStr
    paths: list[StrictStr]
    mode: StrictStr


def make_manager(ctx: "WebContext", after_check=None) -> files.FileManager:
    engine = ctx.engine

    def file_roots() -> list[tuple[str, str, str]]:
        """설정한 목록, 채널 저장 폴더, 켜진 확장이 내놓은 폴더."""
        settings = engine.store.get_settings()
        out = [(path, path, "setting") for path in settings.fm_roots]
        for ch in engine.store.list_channels():
            out.append((ch.output_dir, f"채널: {ch.name}", "channel"))
        return out + ctx.extensions.file_roots()  # 켜진 확장의 허용 폴더(다시보기 저장 폴더 등)

    def locked() -> list:
        return list(engine.locked_paths()) + ctx.extensions.lock_targets()

    return files.FileManager(ctx.cfg.data_dir, file_roots, locked, after_check=after_check, log_dir=ctx.cfg.log_dir)


def build(ctx: "WebContext") -> APIRouter:
    file_manager = ctx.file_manager

    @ctx.app.exception_handler(files.FileError)
    async def file_error(request: Request, exc: files.FileError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    async def require_files() -> Settings:
        """꺼져 있으면 파일 관련 경로가 모두 거부한다. 이 라우터의 모든 경로에 걸린다."""
        settings = await ctx.current_settings()
        if not settings.fm_enabled:
            raise files.FileError(403, "파일 관리자가 꺼져 있습니다")
        return settings

    def writable(settings: Settings) -> None:
        if settings.fm_readonly:
            raise files.FileError(403, "읽기 전용입니다")

    fm = APIRouter(prefix="/api/files", dependencies=[Depends(require_files)])

    @fm.get("/roots")
    async def files_roots(settings: Settings = Depends(require_files)):
        roots = await ctx.db(file_manager.roots)
        usage = {u["key"]: u for u in await ctx.db(file_manager.usage)}
        return {"roots": [{"key": r.key, "label": r.label, "source": r.source, "usage": usage.get(r.key)} for r in roots],
                "readonly": settings.fm_readonly, "delete_mode": "trash" if settings.fm_trash else "permanent",
                "show_hidden": settings.fm_show_hidden, "fields": files.FIELDS}

    @fm.get("/list")
    async def files_list(root: str, path: str = "", settings: Settings = Depends(require_files)):
        return await ctx.db(file_manager.list_dir, root, path, settings.fm_show_hidden)

    @fm.get("/download")
    async def files_download(root: str, path: str):
        settings = await ctx.db(ctx.engine.store.get_settings)
        fd, size, name = await ctx.db(file_manager.open_file, root, path, settings.fm_show_hidden)
        return stream_file(fd, size, name)

    @fm.post("/mkdir")
    async def files_mkdir(body: MkdirBody, settings: Settings = Depends(require_files)):
        writable(settings)
        await ctx.db(file_manager.mkdir, body.root, body.path, body.name)
        return {"ok": True}

    @fm.post("/rename")
    async def files_rename(body: RenameBody, settings: Settings = Depends(require_files)):
        writable(settings)
        return {"path": await ctx.db(file_manager.rename, body.root, body.path, body.new_name)}

    @fm.post("/move")
    async def files_move(body: MoveBody, settings: Settings = Depends(require_files)):
        writable(settings)
        return {"moved": await ctx.db(file_manager.move, body.root, body.paths, body.dest_root, body.dest_path)}

    @fm.post("/delete")
    async def files_delete(body: DeleteBody, settings: Settings = Depends(require_files)):
        writable(settings)
        return {"deleted": await ctx.db(file_manager.delete, body.root, body.paths, body.mode, settings.fm_trash)}

    return fm
