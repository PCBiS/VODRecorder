"""채널 다시보기 목록 보기의 공통 부분. 플랫폼을 모른다.

목록을 모으는 공급자(`api.ListProvider`)는 확장이 등록한다. 여기서는 수집기(`catalog.Collector`)를 대기열 저장소와 함께 열고 닫고,
녹화 채널을 다시보기 채널에 넣고, 화면이 쓰는 경로(`/api/replay/channels…`, `/api/replay/select`)를 낸다.
공급자가 하나도 없으면 목록 경로는 빈 목록·404로 답하고 화면은 목록 보기를 감춘다.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from ...core.events import Event
from ...core.extensions import ExtensionContext
from ...platforms import registry
from . import api, catalog
from .manager import OWNER, ReplayError

MAX_SELECT = 30


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 정해진 키만 받는다


class SelectItem(Strict):
    video_no: StrictStr
    title: StrictStr | None = None
    platform: StrictStr | None = None  # 없으면 기본 공급자
    channel_id: StrictStr | None = None


class SelectBody(Strict):
    items: list[SelectItem]
    confirm: StrictBool = False


class ReplayChannelBody(Strict):
    channel_id: StrictStr  # 공급자가 글을 풀 수 있으면 주소나 @핸들도 된다
    platform: StrictStr | None = None


class CollectBody(Strict):
    mode: Literal["full", "latest", "auto"]


class Lists:
    """수집기와 녹화 채널 동기화. downloads의 대기열 저장소에 붙어 같이 열고 닫는다."""

    def __init__(self, ctx: ExtensionContext):
        self.ctx = ctx
        self.collector: catalog.Collector | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def providers() -> dict[str, api.ListProvider]:
        return api.list_providers()

    def default(self) -> str | None:
        found = self.providers()
        if registry.DEFAULT in found:
            return registry.DEFAULT
        return next(iter(found), None)

    async def opened(self, db) -> None:
        self.collector = catalog.Collector(db, self.providers, default=self.default(), gate=self.ctx.request_gate)
        self.collector.start()
        await self.sync_recording()  # 이 기능이 없던 빌드에서 올라와 처음 뜰 때 녹화 채널을 넣는다

    async def closing(self) -> None:
        if self.collector is not None:
            collector, self.collector = self.collector, None
            await collector.close()

    def recording_channels(self, platform: str) -> list[tuple[str, str]]:
        return [(ch.channel_id, ch.name) for ch in self.ctx.store.list_channels() if ch.platform == platform]

    async def sync_recording(self) -> None:
        if self.collector is None:
            return
        self.collector.default = self.default()
        for pid in self.providers():
            recording = await asyncio.to_thread(self.recording_channels, pid)
            if self.collector is not None:
                self.collector.sync_recording(recording, pid)

    def require(self) -> catalog.Collector:
        if self.collector is None:
            downloads = api.current()
            raise ReplayError(409, (downloads.error if downloads else None) or "다시보기가 꺼져 있습니다")
        self.collector.default = self.default()
        return self.collector

    # ---- 녹화 채널 사건 ----
    def on_channel_event(self, event: Event) -> None:
        """사건 스레드에서 불린다. 저장소는 이벤트 루프에서만 쓰므로 루프로 넘긴다(순서는 그대로다)."""
        loop = self.loop
        if loop is not None and event.platform in self.providers():
            loop.call_soon_threadsafe(self._apply, event)

    def _apply(self, event: Event) -> None:
        collector = self.collector
        if collector is None:
            return
        if event.name == "channel_added":
            collector.add_recording(event.channel_id, event.values.get("name") or event.channel_id, event.platform)
        elif event.name == "channel_removed":
            # 녹화 채널을 지워도 다시보기 채널과 수집 목록은 그대로다. 다시 추가하면 넣게 기억만 푼다
            collector.recording_deleted(event.channel_id, event.platform)


def build(ctx: ExtensionContext, lists: Lists) -> APIRouter:
    r = APIRouter()

    async def settle() -> None:
        """녹화 채널 사건을 다 받아 적용한 뒤 읽는다(추가·삭제 직후의 다시보기 목록에 바로 보이게)."""
        await asyncio.to_thread(ctx.events.drain, 5.0)
        await asyncio.sleep(0)

    def provider_of(platform: str | None) -> api.ListProvider:
        if not lists.providers():
            raise ReplayError(404, "채널 목록을 내는 확장이 꺼져 있습니다")
        pid = platform or lists.default()
        found = lists.providers().get(pid or "")
        if found is None:
            raise ReplayError(404, "목록을 볼 수 없는 플랫폼입니다")
        return found

    async def registered(channel_id: str, platform: str | None) -> tuple[str, str]:
        """다시보기 채널만 받는다."""
        provider = provider_of(platform)
        if not provider.valid_channel(channel_id):
            raise ReplayError(404, "다시보기 채널이 아닙니다")
        await settle()
        if not lists.require().has_channel(channel_id, provider.platform):
            await lists.sync_recording()  # CLI로 더한 녹화 채널일 수 있다
            if not lists.require().has_channel(channel_id, provider.platform):
                raise ReplayError(404, "다시보기 채널이 아닙니다")
        return channel_id, provider.platform

    def channel_error(e: catalog.ChannelError) -> ReplayError:
        return ReplayError(e.status, e.message)

    def with_states(videos: list[dict[str, Any]], provider: api.ListProvider, channel: str) -> list[dict[str, Any]]:
        """줄마다 받은 상태(플랫폼·종류·ID로 센 열쇠)와 받을 주소를 붙인다."""
        downloads = api.current()
        keys = {v["video_no"]: provider.state_key(v["video_no"]) for v in videos}
        states = downloads.video_states(keys.values()) if downloads else {}
        return [dict(v, state=states.get(keys[v["video_no"]]), url=provider.video_url(v["video_no"], channel)) for v in videos]

    @r.get("/api/replay/channels")
    async def replay_channels():
        """다시보기 채널(플랫폼을 가린다)과 채널마다 녹화 채널 여부·수집한 영상 수·수집 상태."""
        providers = lists.providers()
        if not providers:
            raise ReplayError(404, "채널 목록을 내는 확장이 꺼져 있습니다")  # 목록 보기가 없다
        info = [{"platform": p.platform, "name": p.name, "add_hint": p.add_hint, "video_types": p.video_types,
                 "resolve": p.resolve is not None}
                for p in providers.values()]
        lists.require()
        await settle()
        await lists.sync_recording()  # CLI로 더한 녹화 채널도 넣는다(관리자가 지운 채널은 빼고)
        collector = lists.require()
        recording = {(ch.platform, ch.channel_id) for ch in await asyncio.to_thread(ctx.store.list_channels)}
        out = []
        for c in collector.channels(every=True):
            pid = c["platform"]
            row = {k: v for k, v in c.items() if k != "platform"}
            out.append(dict(row, platform=pid, platform_name=providers[pid].name, recording=(pid, c["channel_id"]) in recording,
                            collect=collector.summary(c["channel_id"], pid)))
        return {"channels": out, "limit": catalog.MAX_CHANNELS, "providers": info}

    @r.post("/api/replay/channels", status_code=201)
    async def replay_channel_add(body: ReplayChannelBody):
        """공급자가 글(주소·@핸들)을 풀 수 있으면 풀어 이름과 함께 넣고, 아니면 채널 ID만 받는다."""
        provider = provider_of(body.platform)
        await settle()
        collector = lists.require()
        name = None
        channel = body.channel_id.strip()
        if provider.resolve is not None:
            try:
                found = await collector._gated(provider.platform, provider.resolve, channel)
            except catalog.ChannelError as e:
                raise channel_error(e) from None
            except (catalog.FetchError, catalog.ListError):
                raise ReplayError(502, f"{provider.name}에서 채널을 찾지 못해 추가하지 않았습니다. 잠시 뒤 다시 하십시오") from None
            if found is None:
                raise ReplayError(400, provider.add_hint or "채널 주소를 보내십시오")
            channel, name = found
        if not provider.valid_channel(channel):
            raise ReplayError(400, provider.add_hint or "채널 ID를 보내십시오")
        try:
            return await collector.add_channel(channel, provider.platform, name)
        except catalog.ChannelError as e:
            raise channel_error(e) from None

    @r.delete("/api/replay/channels/{channel_id}")
    async def replay_channel_remove(channel_id: str, platform: str | None = Query(None, max_length=20)):
        """수집 중이면 취소하고 수집 목록과 함께 지운다. 받은 파일·받은 기록·작업은 남긴다."""
        cid, pid = await registered(channel_id, platform)
        try:
            await lists.require().remove_channel(cid, pid)
        except catalog.ChannelError as e:
            raise channel_error(e) from None
        return {"ok": True}

    @r.post("/api/replay/channels/{channel_id}/collect")
    async def replay_collect(channel_id: str, body: CollectBody, platform: str | None = Query(None, max_length=20)):
        """full(전체 다시 수집)·latest(새 영상 확인)·auto(화면을 열 때. 처음이면 전체, 30분 지났으면 새 영상 확인)."""
        cid, pid = await registered(channel_id, platform)
        collector = lists.require()
        if body.mode == "auto":
            started = collector.auto(cid, pid)
        else:
            started = body.mode if collector.request(cid, body.mode, pid) else None
        return {"started": started, "collect": collector.summary(cid, pid)}

    @r.post("/api/replay/channels/{channel_id}/collect/cancel")
    async def replay_collect_cancel(channel_id: str, platform: str | None = Query(None, max_length=20)):
        """수집 취소. 받은 줄은 남긴다."""
        cid, pid = await registered(channel_id, platform)
        collector = lists.require()
        return {"cancelled": collector.cancel(cid, pid), "collect": collector.summary(cid, pid)}

    @r.get("/api/replay/channels/{channel_id}/months")
    async def replay_channel_months(channel_id: str, type: str | None = Query(None, max_length=10),
                                    platform: str | None = Query(None, max_length=20)):
        """영상이 있는 달과 달마다 영상 수. 종류 필터가 수에 함께 걸린다."""
        cid, pid = await registered(channel_id, platform)
        collector = lists.require()
        return {"months": collector.months(cid, type, pid), "collect": collector.summary(cid, pid)}

    @r.get("/api/replay/channels/{channel_id}/videos")
    async def replay_channel_videos(channel_id: str, month: str | None = Query(None, max_length=7),
                                    q: str | None = Query(None, max_length=100), type: str | None = Query(None, max_length=10),
                                    platform: str | None = Query(None, max_length=20)):
        """수집한 목록만 읽는다. month(YYYY-MM 또는 none)의 영상 전부, 또는 제목 검색(q) 결과 200개까지."""
        cid, pid = await registered(channel_id, platform)
        provider = provider_of(pid)
        collector = lists.require()
        query = (q or "").strip()
        if query:
            videos, more = collector.search(cid, query, type, pid)
            return {"videos": with_states(videos, provider, cid), "truncated": more, "limit": catalog.SEARCH_LIMIT}
        if month is None or not (month == catalog.NO_MONTH or re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month)):
            raise ReplayError(400, "달(YYYY-MM)이나 검색어를 주십시오")
        return {"videos": with_states(collector.videos(cid, month, type, pid), provider, cid), "truncated": False}

    @r.post("/api/replay/select")
    async def replay_select(body: SelectBody):
        """고른 영상을 기본 화질로 받는다(30개까지). 영상은 그 플랫폼의 작업 종류로 들어간다."""
        if not 1 <= len(body.items) <= MAX_SELECT:
            raise ReplayError(400, f"한 번에 1~{MAX_SELECT}개를 고르십시오.")
        entries = []
        for item in body.items:
            provider = provider_of(item.platform)
            if not re.fullmatch(r"\d{1,20}", item.video_no):
                raise ReplayError(400, "영상 번호가 올바르지 않습니다.")
            entries.append((provider.video_url(item.video_no, item.channel_id or ""), item.title or f"영상 {item.video_no}"))
        downloads = api.current()
        if downloads is None:
            raise ReplayError(409, "다운로드를 준비하지 못했습니다")
        return await downloads.select(OWNER, entries, body.confirm)

    return r
