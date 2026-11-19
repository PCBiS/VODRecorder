"""replay 확장: 치지직 다시보기 분석과 받기와 치지직 채널 다시보기 목록의 공급자.

downloads 확장이 필요하다. downloads는 공개 접점(api: 작업 종류 등록·작업 만들기·저장소에 붙기, media: 받는 방법)으로만 쓴다.
치지직 전용 코드는 `chzzk_video`(영상 API·주소 인식·파일 이름)와 `catalog`·`channel_list`(채널 목록 API)에 있다.
"""

from __future__ import annotations

import asyncio
from typing import Any

import random

from ...core.extensions import Extension, ExtensionContext
from ...platforms import registry
from ..downloads import api
from . import catalog, chzzk_video

DownloadError = api.DownloadError
VIDEO_KIND = api.JobKind("video", "replay", "다시보기", "live_recorder.ext.replay.chzzk_video", platform="chzzk",
                         example=chzzk_video.video_url("12345678"))


def replay_platform():
    """다시보기는 치지직만 다룬다."""
    return registry.get("chzzk")


class Catalog:
    """치지직 목록 공급자. 공통 수집기(downloads)에 `api.ListProvider`로 등록한다.
    시험은 collect_fetch·collect_channel_fetch·collect_sleep·collect_jitter를 바꿔 넣는다. 부를 때마다 읽는다."""

    def __init__(self, ctx: ExtensionContext):
        self.ctx = ctx
        self.collect_fetch = None
        self.collect_channel_fetch = None
        self.collect_sleep = None
        self.collect_jitter = None
        self._http = None

    @property
    def collector(self):
        """공통 수집기(downloads가 대기열 저장소와 함께 연다)."""
        downloads = api.current()
        return downloads.list_collector() if downloads else None

    def provider(self) -> api.ListProvider:
        chzzk = replay_platform()
        self._http = catalog.http_fetcher(chzzk.api_base(self.ctx.cfg), chzzk.allowed_hosts(self.ctx.cfg),
                                          self.ctx.cfg.allow_http, self.ctx.cfg)
        fetch, channel_fetch, _ = self._http

        async def page(channel: str, cursor) -> tuple[int, Any]:
            return await (self.collect_fetch or fetch)(channel, cursor)

        async def channel_info(channel: str) -> tuple[int, Any]:
            return await (self.collect_channel_fetch or channel_fetch)(channel)

        async def sleep(seconds: float) -> None:
            await (self.collect_sleep or asyncio.sleep)(seconds)

        def jitter() -> float:
            return (self.collect_jitter or (lambda: random.uniform(0.0, catalog.PAGE_JITTER)))()

        return catalog.chzzk_provider(page, channel_info, sleep=sleep, jitter=jitter)

    async def aclose(self) -> None:
        if self._http is not None:
            http, self._http = self._http, None
            await http[2]()


class ReplayExtension(Extension):
    name = "replay"
    title = "다시보기"
    description = "치지직 다시보기를 받습니다. 채널의 다시보기 목록을 모아 년·월로 보고 골라 받을 수 있습니다."
    version = "1.0"
    requires = ("downloads",)
    legacy_enabled_key = "replay_enabled"
    legacy_enabled_default = True
    job_kinds = (VIDEO_KIND,)
    log_mask = {"video_paths": ("/video/",), "video_keys": ("videoNo",)}  # 로그 내려받기가 영상 번호를 가린다
    http_errors = (DownloadError,)
    # 채널 다시보기 목록의 화면·경로·수집은 downloads의 공통 부분이 맡고, 이 확장은 치지직 공급자를 등록한다.
    # 화면 조각(채널 목록 방식)은 공급자가 있을 때만 들어가도록 공급자를 내는 확장이 밝힌다
    ui_dir = api.LIST_FRAGMENT.parent
    html = {"downloads-modes": api.LIST_FRAGMENT.name}

    def routes(self, ctx: ExtensionContext):
        ctx.catalog = Catalog(ctx)
        return None

    def open_storage(self, ctx: ExtensionContext) -> None:
        api.register_kind(VIDEO_KIND)
        api.register_list_provider(ctx.catalog.provider())  # 치지직 채널 다시보기 목록

    async def stop(self, ctx: ExtensionContext) -> None:
        api.unregister_kind(VIDEO_KIND.kind)
        api.unregister_list_provider("chzzk")
        await ctx.catalog.aclose()


EXTENSION = ReplayExtension()
