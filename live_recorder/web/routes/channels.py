"""경로 묶음: 채널·녹화. 다른 묶음을 부르지 않는다."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr

from ...core import gate as gate_mod
from ...core import store as st
from ...platforms import registry
from ...platforms.base import ChannelLookupNeeded
from ..auth import Strict

if TYPE_CHECKING:
    from ..context import WebContext

# 보기 전용 응답에 넣는 필드. 채널 ID·저장 폴더·화질 설정·자동 녹화·오류 문장은 넣지 않는다
VIEW_CHANNEL_KEYS = ("name", "state", "state_label", "live_title", "elapsed", "file", "error_flag", "error_count",
                     "bytes", "file_started_at", "thumb_url", "adult", "last_started_at", "last_seconds")


class ChannelCreate(Strict):
    channel_id: StrictStr
    platform: StrictStr | None = None  # 없으면 기본 플랫폼(치지직)
    name: StrictStr
    output_dir: StrictStr | None = None  # 없거나 비면 <녹화 루트>/<플랫폼 식별자>
    quality: StrictStr = "best"
    extension: StrictStr = ".mp4"
    auto_record: StrictBool = True


class GateBody(Strict):
    scan: StrictInt | StrictFloat | None = None
    gap: StrictInt | StrictFloat | None = None


class PlatformEnable(Strict):
    agree: StrictBool = False


class ChannelResolve(Strict):
    text: StrictStr
    platform: StrictStr | None = None


class ChannelPatch(Strict):
    name: StrictStr | None = None
    output_dir: StrictStr | None = None
    quality: StrictStr | None = None
    extension: StrictStr | None = None
    auto_record: StrictBool | None = None


def coarse_error(text: str) -> str:
    """보기 전용 화면의 오류 사유. 문장 대신 종류만 준다(문장에 경로가 들어갈 수 있다)."""
    if text.startswith("디스크 여유 부족"):
        return "디스크 여유 부족"
    if "start_failed" in text or "재생 정보" in text:
        return "녹화 시작 실패"
    return "녹화 실패"


def needs_lookup(platform, text: str) -> bool:
    """채널 칸의 글이 조회가 필요한 이름(`@이름`)인가."""
    value = (text or "").strip()
    if not value.startswith("@"):
        return False
    try:
        platform.parse_channel(value)
    except ChannelLookupNeeded:
        return True
    except ValueError:
        return False
    return False


def platform_info(platform) -> dict[str, Any]:
    spec = platform.credential_spec
    return {"id": platform.id, "name": platform.name,
            "features": {"timemachine": platform.features.timemachine, "rewind": platform.features.rewind},
            "live_rewind": platform.features.live_rewind, "min_check_interval": platform.min_check_interval,
            "channel_hint": platform.channel_hint, "channel_rule": platform.channel_rule,
            "credentials": {"names": list(spec.names), "title": spec.title, "help": spec.help,
                            "checkable": spec.checkable, "kind": spec.kind, "max_bytes": spec.max_bytes,
                            "summary": spec.summary}}


def build(ctx: "WebContext") -> APIRouter:
    r = APIRouter()
    engine = ctx.engine

    @r.get("/api/view/channels")
    async def view_channels():
        """보기 경로. 채널 이름, 상태, 방송 제목, 녹화 경과 시간, 현재 파일 이름과 카드 표시만 돌려준다.
        채널 ID·저장 폴더·화질·자동 녹화·오류 문장(경로가 들어갈 수 있다)은 넣지 않는다. 오류는 종류만 준다."""
        out = []
        for v in await engine.channel_views():
            item = {k: v[k] for k in VIEW_CHANNEL_KEYS if k in v}
            item["error_reason"] = coarse_error(v["error"]) if v["error"] else None
            out.append(item)
        return {"channels": out, "disk": await ctx.disk_info()}

    @r.get("/status")
    async def status():
        """살아 있는지와 채널별 순번(등록 순서)·recording·duration만 돌려준다."""
        views = await engine.channel_views()
        return {"status": "ok", "channels": [{"index": i, "recording": v["state"] == st.RECORDING,
                                              "duration": v["elapsed"] or 0} for i, v in enumerate(views, 1)]}

    @r.get("/api/platforms")
    async def platforms():
        """화면이 쓰는 플랫폼 정보(이름, 선택 기능, 채널 주소 규칙, 자격 이름). 비밀 값은 없다."""
        enabled = await ctx.db(engine.store.enabled_platforms)
        out = []
        for p in registry.all_platforms():
            info = platform_info(p)
            info["gate"] = await ctx.db(engine.gate_info, p.id)  # 스캔 주기·채널 사이 간격·한 바퀴
            info["gate_warning"] = gate_mod.WARNING
            if p.opt_in:
                # 꺼져 있어도 알린다(화면이 켜기 단추를 보인다). 고지 문장은 도구를 불러오지 않고 만든다
                info.update(opt_in=True, enabled=engine._platform_on(p, enabled), notice=p.opt_in_notice(ctx.cfg))
            else:
                info.update(opt_in=False, enabled=True)
            out.append(info)
        return {"platforms": out, "default": registry.DEFAULT}

    @r.put("/api/platforms/{platform_id}/gate")
    async def platform_gate(platform_id: str, body: GateBody):
        """그 플랫폼의 스캔 주기·채널 사이 간격. 저장하면 다음 바퀴부터 적용한다."""
        platform = await ctx.need_usable_platform(platform_id)
        try:
            await ctx.db(engine.set_gate_values, platform.id, body.scan, body.gap)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        log_change = await ctx.db(engine.gate_info, platform.id)
        return log_change

    @r.post("/api/platforms/{platform_id}/enable")
    async def platform_enable(platform_id: str, body: PlatformEnable):
        """고지에 동의(agree)해야 한다. 자체 점검을 통과하면 바로 켜진다."""
        platform = ctx.need_platform(platform_id)
        if not body.agree:
            raise HTTPException(422, "고지에 동의해야 켤 수 있습니다")
        try:
            await engine.enable_platform(platform.id)
        except PermissionError as e:
            raise HTTPException(403, str(e)[:200]) from None
        except ValueError as e:
            raise HTTPException(409, str(e)[:400]) from None
        return {"platform": platform.id, "enabled": True}

    @r.post("/api/platforms/{platform_id}/disable")
    async def platform_disable(platform_id: str):
        """녹화 중인 채널이 있으면 409. 끄면 바로 먹는다."""
        platform = ctx.need_platform(platform_id)
        try:
            await engine.disable_platform(platform.id)
        except ValueError as e:
            raise HTTPException(409, str(e)[:300]) from None
        return {"platform": platform.id, "enabled": False}

    @r.get("/api/channels")
    async def channels():
        rows = await engine.channel_views()
        # 주기가 늘어난 플랫폼의 안내("채널 N개를 간격 M초로 확인해 한 바퀴에 약 T초가 걸립니다")
        gates = {}
        for pid in sorted({r["platform"] for r in rows}):
            if registry.find(pid) is not None:
                info = await ctx.db(engine.gate_info, pid)
                if info["note"] or info["paused"]:
                    gates[pid] = info
        if ctx.extensions.active:  # 켜진 확장이 채널 화면에 더할 정보(확장이 없으면 예전과 같은 모양)
            for row in rows:
                info = await ctx.db(ctx.extensions.channel_info, row)
                if info:
                    row["ext"] = info
        return {"channels": rows, "disk": await ctx.disk_info(), "gates": gates}

    @r.post("/api/channels/resolve")
    async def channel_resolve(body: ChannelResolve):
        """채널 칸의 글을 채널 ID로. `@이름`처럼 조회가 필요하면 한 번 조회해 이름도 준다."""
        platform = await ctx.need_usable_platform(body.platform)
        cid, name = await engine.resolve_channel(platform.id, body.text)
        return {"platform": platform.id, "channel_id": cid, "name": name}

    @r.post("/api/channels", status_code=201)
    async def channel_add(body: ChannelCreate):
        """채널을 더합니다. `output_dir`(녹화 루트 아래의 저장 폴더)을 생략하거나 비우면 `<플랫폼 식별자>/<채널 이름>`에 저장합니다.
        이름에서 파일 이름에 못 쓰는 글자는 `_`로 바뀌고, 같은 폴더를 다른 채널이 쓰면 끝에 `_<채널 ID 앞 8자>`가 붙습니다."""
        fields = body.model_dump()
        platform = await ctx.need_usable_platform(fields.pop("platform"))
        rule = platform.channel_rule or {}
        if rule.get("lookup_only") and not needs_lookup(platform, fields["channel_id"]):
            # 조회로만 추가하는 플랫폼(채널 ID만으로는 쓸 수 없다)
            raise HTTPException(400, rule.get("lookup_only_error") or rule.get("error") or "주소로 추가해 주십시오")
        if needs_lookup(platform, fields["channel_id"]):
            # `@이름`이면 추가할 때 한 번 조회해 채널 ID와 이름으로 바꿔 저장한다(주소는 화면이 ID로 바꿔 보낸다)
            cid, name = await engine.resolve_channel(platform.id, fields["channel_id"])
            fields["channel_id"] = cid
            if name and not fields["name"].strip():
                fields["name"] = name
        ch = await engine.add_channel(**fields, platform=body.platform)
        return {"channel_id": ch.channel_id, "platform": ch.platform}

    @r.patch("/api/channels/{channel_id}")
    async def channel_patch(channel_id: str, body: ChannelPatch, platform: str | None = None):
        # 보낸 필드만 바꾼다. 빠진 필드를 빈 값이나 꺼짐으로 읽지 않는다
        changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
        await engine.update_channel(ctx.need_channel_id(channel_id, platform), platform_id=platform, **changes)
        return {"ok": True}

    @r.delete("/api/channels/{channel_id}")
    async def channel_delete(channel_id: str, platform: str | None = None):
        ch = await ctx.db(engine.store.get_channel, ctx.need_channel_id(channel_id, platform), platform)
        if ch is None:
            raise HTTPException(404, "없는 채널")
        return {"result": await engine.remove_channel(channel_id, ch.platform)}

    @r.post("/api/channels/{channel_id}/start")
    async def channel_start(channel_id: str, platform: str | None = None):
        return {"result": await engine.manual_start(ctx.need_channel_id(channel_id, platform), platform)}

    @r.post("/api/channels/{channel_id}/stop")
    async def channel_stop(channel_id: str, platform: str | None = None):
        return {"result": await engine.manual_stop(ctx.need_channel_id(channel_id, platform), platform)}

    return r
