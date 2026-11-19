"""경로 묶음: 쿠키. 자격은 플랫폼별이다. `?platform=`이 없으면 기본 플랫폼(치지직)이고
응답 모양은 예전과 같다. 다른 묶음을 부르지 않는다."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import StrictStr

from ...core.config import ConfigError
from ...platforms import cookiefile, credentials
from ..auth import Strict

if TYPE_CHECKING:
    from ..context import WebContext

class CookieUpload(Strict):
    text: StrictStr  # 브라우저가 고른 파일의 글(또는 붙여 넣은 글)


COOKIE_LABELS = {"valid": "유효", "expired": "만료", "failed": "확인 실패", "missing": "쿠키 없음"}


def build(ctx: "WebContext") -> APIRouter:
    r = APIRouter()
    cfg, engine = ctx.cfg, ctx.engine

    @r.get("/api/cookies")
    async def cookie_status(platform: str | None = None):
        pid = (await ctx.need_usable_platform(platform)).id
        current = await ctx.db(credentials.load, cfg, pid)
        out: dict[str, Any] = {k: bool(current.get(k)) for k in credentials.names(pid)}  # 설정 여부만
        platform = ctx.need_platform(pid)
        if platform.credential_spec.kind == "file" or platform.credential_spec.summary:
            # 값 대신 저장 시각, 쿠키 수, 로그인 쿠키의 가장 이른 만료일만
            summary = platform.credential_summary(current) if current else {}
            out["summary"] = {**summary, "saved_at": await ctx.db(credentials.saved_at, cfg, pid)} if current else None
        last = engine.last_cookie_checks.get(pid)  # 마지막 확인 시각(엔진의 상태를 읽기만 한다)
        out["last_check"] = ({"result": last["result"], "label": COOKIE_LABELS.get(last["result"], last["result"]),
                              "at": datetime.fromtimestamp(last["at"], timezone.utc).isoformat()} if last else None)
        return out

    @r.put("/api/cookies")
    async def cookie_save(body: dict[str, Any], platform: str | None = None):
        pid = (await ctx.need_usable_platform(platform)).id
        names = credentials.names(pid)
        # 그 플랫폼의 자격 이름과 문자열 값만 받는다(모르는 키·다른 타입은 422)
        for key, value in body.items():
            if key not in names or not (value is None or isinstance(value, str)):
                raise HTTPException(422, f"받지 않는 값입니다: {key}"[:120])
        current = await ctx.db(credentials.load, cfg, pid)
        for key, value in body.items():
            if value is not None:
                current[key] = value
        try:
            await ctx.db(credentials.save, cfg, pid, current)
        except ValueError as e:
            raise ConfigError(str(e)) from None
        return await cookie_status(pid)

    @r.post("/api/cookies/upload")
    async def cookie_upload(body: CookieUpload, platform: str | None = None):
        """올린 쿠키 파일에서 그 플랫폼의 자격만 골라 저장한다. 파일 자체는 저장하지 않는다."""
        found = await ctx.need_usable_platform(platform)
        if len(body.text.encode("utf-8")) > cookiefile.MAX_BYTES:
            raise ConfigError(f"쿠키 파일이 너무 큽니다({cookiefile.MAX_BYTES // 1024}KB 이하만 받습니다)")
        try:
            values, info = await ctx.db(found.pick_credentials, body.text)
            await ctx.db(credentials.save, cfg, found.id, values)
        except ValueError as e:
            raise ConfigError(str(e)[:300]) from None  # 까닭만. 파일 내용과 값은 넣지 않는다
        except RecursionError:
            raise ConfigError("쿠키 파일을 읽을 수 없습니다(구조가 너무 깊습니다)") from None
        del values
        state = await cookie_status(found.id)
        return {"found": info.get("names") or [], "expires": info.get("expires"),
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "state": state}

    @r.delete("/api/cookies")
    async def cookie_clear(platform: str | None = None):
        pid = (await ctx.need_usable_platform(platform)).id
        await ctx.db(credentials.clear, cfg, pid)
        return await cookie_status(pid)

    @r.post("/api/cookies/check")
    async def cookie_check(platform: str | None = None):
        """저장한 쿠키가 아직 유효한지 확인한다."""
        found = await ctx.need_usable_platform(platform)
        if not found.credential_spec.checkable:
            raise HTTPException(404, "확인 기능이 없는 플랫폼")
        result = await engine.check_cookies("button", found.id)
        return {"result": result, "label": COOKIE_LABELS[result]}

    return r
