"""접근 키 관리와 API 문서.

- `GET/POST /api/keys`, `DELETE /api/keys/{key_id}`: 관리자(로그인한 세션)만. 키로는 열리지 않는다.
- `GET /api/openapi.json`: 키로 열 수 있는 경로만 담은 기계용 문서. 접근 규칙 표와 경로 정의에서 만든다.
- `GET /api/guide`: 사람과 AI용 안내 글(마크다운). 저장소의 `docs/API.md`와 같은 글이다(시험이 견준다).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, StrictStr

from ... import PROGRAM_NAME, __version__
from ...core import apikeys
from ..access import KEY_NONE, KEY_OPERATE

log = logging.getLogger("live_recorder.web")

GUIDE = Path(__file__).resolve().parent.parent / "api_guide.md"
DISABLED = "접근 키 기능이 꺼져 있습니다"
PERMISSION_TEXT = {"view": "보기", "operate": "운영"}
ERRORS = {"401": "접근 키가 없거나 맞지 않습니다", "403": "권한이 없거나 키로 열 수 없는 경로입니다",
          "404": "찾을 수 없습니다", "409": "지금은 할 수 없습니다(녹화 중, 꺼진 플랫폼 등)", "422": "입력이 맞지 않습니다",
          "429": "요청 제한을 넘었습니다(Retry-After 초 뒤 다시)"}


class KeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    scope: StrictStr
    expiry: StrictStr = "none"


_ID = r"(?<![\w-])[A-Z]{2,3}-\d+(?:[~·]\d+)*(?:,\s*\d+(?:~\d+)?(?![\d.]?\d))*"
_IDS = rf"{_ID}(?:\s*[,·]\s*{_ID})*"


def clean_text(text: str) -> str:
    """경로 설명(docstring)에 든 개발용 요구 번호를 뗀다. 문서에는 뜻만 남긴다."""
    import re

    text = re.sub(rf"\s*\(\s*{_IDS}\s*\)", "", text)
    text = re.sub(rf"(?m)^(\s*){_IDS}\s*(?:보기 경로)?[.:]?\s*", r"\1", text)
    text = re.sub(rf"(?<![\w-]){_IDS}[.:]\s*", "", text)
    return text.strip()


def openapi_document(app, access) -> dict[str, Any]:
    """키로 열 수 있는 경로만. 경로마다 필요한 권한(`x-permission`)·입력·응답·오류."""
    doc = get_openapi(title=f"{PROGRAM_NAME} API", version=__version__, routes=app.routes,
                      description="접근 키(`Authorization: Bearer vr_…`)로 부를 수 있는 경로입니다. 안내 글은 /api/guide 에 있습니다.")
    doc.setdefault("components", {}).setdefault("securitySchemes", {})["bearer"] = {"type": "http", "scheme": "bearer"}
    doc["components"].setdefault("schemas", {})["Error"] = {
        "type": "object", "required": ["detail", "code"],
        "properties": {"detail": {"type": "string"}, "code": {"type": "string"}}}
    doc["security"] = [{"bearer": []}]
    for path, ops in list(doc.get("paths", {}).items()):
        for method in list(ops):
            perm = access.key(method.upper(), path)
            if perm == KEY_NONE:
                del ops[method]
                continue
            op = ops[method]
            op["x-permission"] = perm
            for field in ("summary", "description"):
                if op.get(field):
                    op[field] = clean_text(op[field])
            op["description"] = (op.get("description") or "").strip() + f"\n\n필요한 권한: {PERMISSION_TEXT[perm]}"
            responses = op.setdefault("responses", {})
            for code, text in ERRORS.items():
                if code == "403" or code not in responses:
                    responses[code] = {"description": text,
                                       "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
            responses.pop("400", None)
        if not ops:
            del doc["paths"][path]
    return doc


def build(ctx) -> APIRouter:
    r = APIRouter()

    def need_keys():
        if ctx.keys is None:
            raise HTTPException(404, DISABLED)
        return ctx.keys

    @r.get("/api/keys")
    async def keys_list():
        if ctx.keys is None:
            return {"enabled": False, "keys": [], "max": apikeys.MAX_KEYS}
        return {"enabled": True, "keys": await ctx.db(ctx.keys.list), "max": apikeys.MAX_KEYS,
                "scopes": apikeys.SCOPE_LABELS, "expiries": list(apikeys.EXPIRY_DAYS), "guide": "/api/guide",
                "openapi": "/api/openapi.json"}

    @r.post("/api/keys", status_code=201)
    async def keys_create(body: KeyBody):
        store = need_keys()
        if await ctx.account_generation() is None:
            raise HTTPException(403, "관리자 계정이 없어 접근 키를 만들 수 없습니다")  # 보기 전용 모드
        try:
            value, row = await ctx.db(store.create, body.name, body.scope, body.expiry)
        except apikeys.ApiKeyError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        log.info("접근 키 발급", extra={"event": "api_key_created", "key_name": row["name"], "key_prefix": row["prefix"],
                                        "scope": row["scope"]})
        return {"value": value, "key": row,
                "note": "이 값은 지금만 보입니다. 다시 볼 수 없으니 안전한 곳에 적어 두십시오."}

    @r.delete("/api/keys/{key_id}")
    async def keys_revoke(key_id: str):
        store = need_keys()
        rows = {k["id"]: k for k in await ctx.db(store.list)}
        if key_id not in rows or not await ctx.db(store.revoke, key_id):
            raise HTTPException(404, "없는 접근 키")
        ctx.key_rates.forget(key_id)
        log.info("접근 키 폐기", extra={"event": "api_key_revoked", "key_name": rows[key_id]["name"],
                                        "key_prefix": rows[key_id]["prefix"]})
        return {"ok": True}

    @r.get("/api/openapi.json")
    async def openapi_json(request: Request):
        cache = ctx.app.state.__dict__.setdefault("_openapi", {})
        if "doc" not in cache:
            cache["doc"] = openapi_document(ctx.app, ctx.app.state.access)
        return cache["doc"]

    @r.get("/api/guide")
    async def guide():
        return PlainTextResponse(GUIDE.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")

    return r


__all__ = ["build", "openapi_document", "KEY_OPERATE"]
