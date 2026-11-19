"""경로 묶음: 설정. 다른 묶음을 부르지 않는다.
설정 저장에 끼어야 하는 묶음은 `ctx.settings_hooks`에 걸이를 단다."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import StrictStr

from ... import notify
from ...platforms import registry
from ...core import extensions, naming, secretstore
from ...core.config import ConfigError, Settings
from ..auth import Strict

if TYPE_CHECKING:
    from ..context import WebContext

RESTART_NOTE = "확장을 켜고 끄는 것은 다음 서버 시작부터 적용됩니다"


class TelegramBody(Strict):
    bot_token: StrictStr | None = None
    chat_id: StrictStr | None = None


class ExtensionsBody(Strict):
    enabled: list[StrictStr]


def build(ctx: "WebContext") -> APIRouter:
    r = APIRouter()
    cfg, engine = ctx.cfg, ctx.engine

    def settings_payload(current: Settings) -> dict[str, Any]:
        values = {k: getattr(current, k) for k in Settings.keys()}
        # 켜진 확장의 설정 키(예: downloads의 replay_*)도 같은 자리에 보인다. 꺼진 확장의 키는 없다
        values.update({k: list(v) if isinstance(v, tuple) else v for k, v in ctx.extensions.settings_values().items()})
        return {
            "settings": values,
            # 저장된 파일명 패턴을 쓸 수 없어 기본 패턴을 쓰는 중이면 저장된 값과 까닭
            "filename_pattern_problem": (lambda p: {"stored": p[0], "reason": p[1]} if p else None)(engine.store.filename_pattern_problem()),
            "telegram": engine.notifier.configured(),  # 설정 여부만. 값은 내보내지 않는다
            "events": [{"key": f"notify_{k}", "label": label, "required": required}
                       for k, (label, required) in notify.EVENTS.items()],
        }

    @r.get("/api/settings/filename-example")
    async def filename_example(pattern: str = Query(..., max_length=1000), date_format: str = Query(..., max_length=40),
                               time_format: str = Query(..., max_length=40)):
        """고른 모양과 패턴으로 만든 예시 이름. 녹화가 쓰는 naming.render를 그대로 쓴다(저장하지 않는다)."""
        try:
            Settings.validate("filename_date_format", date_format)
            Settings.validate("filename_time_format", time_format)
            Settings.validate("filename_pattern", pattern)
        except ConfigError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        now = datetime.now(naming.KST)
        name = naming.render(pattern, title="방송 제목: 예시", channel_name="채널 이름", quality="1080p", frame_rate="60",
                             extension=".mp4", broadcast_start=now - timedelta(minutes=5), recording_start=now, now=now,
                             date_shape=date_format, time_shape=time_format)
        return {"example": name}

    @r.get("/api/settings/filename-tokens")
    async def filename_tokens(date_format: str = Query(..., max_length=40), time_format: str = Query(..., max_length=40)):
        """쓸 수 있는 토큰과 뜻, 고른 모양으로 만든 예시 값. 토큰 목록은 서버가 받아 주는 목록(naming.TOKEN_HELP)이다."""
        try:
            Settings.validate("filename_date_format", date_format)
            Settings.validate("filename_time_format", time_format)
        except ConfigError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        now = datetime.now(naming.KST)
        values = naming.token_values(title="방송 제목: 예시", channel_name="채널 이름", quality="1080p", frame_rate="60",
                                     extension=".mp4", broadcast_start=now - timedelta(minutes=5), recording_start=now, now=now,
                                     date_shape=date_format, time_shape=time_format)
        return {"tokens": [{"token": name, "meaning": meaning, "example": values[name]} for name, meaning in naming.TOKEN_HELP]}

    @r.get("/api/settings")
    async def settings_get():
        return await ctx.db(lambda: settings_payload(engine.store.get_settings()))

    @r.patch("/api/settings")
    async def settings_patch(body: dict[str, Any]):
        # 정해진 키와 타입만 받는다(검증은 Settings.validate와 켜진 확장의 SettingSpec). 보낸 키만 바꾼다
        list_keys = {"trusted_proxies", "fm_roots"} | ctx.extensions.list_keys()
        for key, value in body.items():
            if value is None or isinstance(value, dict) or (isinstance(value, list) and key not in list_keys):
                raise ConfigError(f"값의 형식이 틀립니다: {key}")
        if "fm_roots" in body:
            for path in Settings.validate("fm_roots", body["fm_roots"]):
                problem = ctx.file_manager.root_problem(path)
                if problem:
                    raise ConfigError(f"허용 루트로 쓸 수 없습니다({problem}): {path}")
        owners = {k: ctx.extensions.setting_owner(k) for k in body}
        ext_changes = {k: v for k, v in body.items() if owners[k] is not None}
        core_changes = {k: v for k, v in body.items() if owners[k] is None}
        for ext in ctx.extensions.active:  # 저장 전 검사(예: downloads는 진행 중인 작업이 있으면 거부한다)
            await ext.settings_check(ctx.extensions.ctx(ext), body)
        clean = {extensions.setting_key(owners[k].name, k): owners[k].settings[k].validate(v) for k, v in ext_changes.items()}
        current = await ctx.db(engine.store.set_settings, core_changes)
        if clean:
            await ctx.db(engine.store.set_extra, clean)
        ctx.forget_settings()
        if current.login_limit_enabled and not ctx.limiter_state["enabled"] or body.get("login_limit_enabled") is False:
            ctx.limiter.clear()
        ctx.limiter_state["enabled"] = current.login_limit_enabled
        for ext in ctx.extensions.active:  # 저장 즉시 적용
            mine = {k: v for k, v in ext_changes.items() if owners[k] is ext}
            if mine:
                await ext.settings_applied(ctx.extensions.ctx(ext), mine)
        engine.events.emit("settings_changed", keys=sorted(body))
        return await ctx.db(settings_payload, current)

    @r.put("/api/notify/secret")
    async def telegram_save(body: TelegramBody):
        changes = body.model_dump(exclude_unset=True)

        def work() -> dict[str, bool]:
            current = secretstore.telegram_load(cfg.telegram_path)
            merged = dict(current)
            for k, v in changes.items():
                if v is not None:
                    merged[k] = v.strip()
            if engine.store.get_settings().notify_enabled and not (merged.get("bot_token") and merged.get("chat_id")):
                raise ConfigError("알림이 켜져 있는 동안에는 봇 토큰과 chat_id를 비울 수 없습니다")
            try:
                secretstore.telegram_save(cfg.telegram_path, changes.get("bot_token"), changes.get("chat_id"))
            except ValueError as e:
                raise ConfigError(str(e)) from None
            return engine.notifier.configured()

        return {"telegram": await ctx.db(work)}

    @r.post("/api/notify/test")
    async def telegram_test():
        """텔레그램 테스트 알림."""
        ok, detail = await engine.notifier.send_test()
        return {"ok": ok, "detail": detail}

    # ---- 확장 켜고 끄기와 확장 설정 ----
    def extensions_payload() -> dict[str, Any]:
        enabled = extensions.enabled_names(engine.store)
        active = [e.name for e in ctx.extensions.active]
        known = extensions.known()
        return {"extensions": [{"name": e.name, "title": e.title or e.name, "description": e.description, "version": e.version,
                                "requires": list(e.requires),
                                # 켜져 있어야 하는 플랫폼의 이름
                                "requires_platforms": [getattr(registry.find(p), "name", p) for p in getattr(e, "requires_platforms", ())],
                                "required_by": [x.name for x in known if e.name in x.requires],  # 끌 때 함께 꺼진다
                                "enabled": e.name in enabled, "active": e.name in active} for e in known],
                "restart_needed": sorted(enabled) != sorted(active), "note": RESTART_NOTE,
                "ui": ctx.extensions.ui_manifest()}

    @r.get("/api/extensions")
    async def extensions_get():
        return await ctx.db(extensions_payload)

    @r.put("/api/extensions")
    async def extensions_put(body: ExtensionsBody):
        clean = extensions.validate_enabled(list(body.enabled))
        for name in clean:  # 켜져 있어야 하는 플랫폼이 꺼져 있으면 그 확장을 켤 수 없다
            ext = extensions.find(name)
            for pid in getattr(ext, "requires_platforms", ()) if ext else ():
                if not await ctx.db(engine.platform_on, pid):
                    platform = registry.find(pid)
                    raise ConfigError(f"{ext.title or name} 확장은 {platform.name if platform else pid}을(를) 먼저 켜야 켤 수 있습니다")
        await ctx.db(engine.store.set_extra, {extensions.ENABLED_KEY: clean})
        return await ctx.db(extensions_payload)

    def active_extension(name: str):
        ext = next((e for e in ctx.extensions.active if e.name == name), None)
        if ext is None:
            raise HTTPException(404, "켜진 확장이 아닙니다")
        return ext

    @r.get("/api/extensions/{name}/settings")
    async def extension_settings_get(name: str):
        ext = active_extension(name)
        return {"settings": {k: await ctx.db(extensions.get_setting, engine.store, name, k) for k in ext.settings}}

    @r.patch("/api/extensions/{name}/settings")
    async def extension_settings_patch(name: str, body: dict[str, Any]):
        ext = active_extension(name)
        clean = {}
        for key, value in body.items():
            spec = ext.settings.get(key)
            if spec is None:
                raise ConfigError(f"모르는 설정입니다: {key}")
            clean[extensions.setting_key(name, key)] = spec.validate(value)
        await ctx.db(engine.store.set_extra, clean)
        engine.events.emit("settings_changed", keys=sorted(clean))
        return await extension_settings_get(name)

    # ---- 외부 도구. 플랫폼 모듈이 밝힌 도구의 상태와 조작. 관리자 설정과 같은 접근 수준·요청 보호 ----
    def tool_owner(name: str):
        for platform in registry.all_platforms():
            if name in platform.tool_names and engine.platform_on(platform.id):  # 꺼진 플랫폼의 도구는 없다
                return platform
        raise HTTPException(404, "모르는 도구입니다")

    @r.get("/api/tools")
    async def tools():
        out = []
        for platform in registry.all_platforms():
            if platform.tool_names and await ctx.db(engine.platform_on, platform.id):
                for item in await ctx.db(platform.tool_status, cfg):
                    out.append({**item, "platform": platform.id})
        return {"tools": out}

    @r.post("/api/tools/{name}/{action}")
    async def tool_action(name: str, action: str):
        platform = tool_owner(name)
        try:
            return await asyncio.to_thread(platform.tool_action, cfg, name, action)
        except KeyError:
            raise HTTPException(404, "모르는 조작입니다") from None
        except PermissionError as e:
            raise HTTPException(403, str(e)[:200]) from None
        except ValueError as e:
            raise HTTPException(409, str(e)[:300]) from None

    return r
