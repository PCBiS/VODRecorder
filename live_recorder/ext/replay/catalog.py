"""치지직 채널 다시보기 목록의 공급자.

모으기 일정·재시도·저장·년월 조회는 downloads의 공통 수집기(`api.catalog`)가 한다. 여기에는 치지직에만 있는 것이 남았다:
목록 API 요청(`http_fetcher`), 한 쪽 읽기(`channel_list`), 채널 이름 조회.
쪽 번호 0부터 totalPages까지 받고, 도중에 totalPages가 바뀌면 바뀐 값까지 받는다.

`Collector(db, fetch, …)`는 치지직 공급자 하나를 단 공통 수집기다(예전 호출 꼴 그대로. 시험이 쓴다).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx

from ...core import httpread

from ...platforms import credentials
from ...platforms.base import host_allowed
from ...platforms.chzzk import api as chzzk  # 치지직 목록 API(이 확장은 치지직 전용이다)
from ..downloads import api
from . import chzzk_video
from .channel_list import ChannelMissing, ListError, channel_url, parse_channel, parse_page, video_list_url

_common = api.catalog
# 수집 규칙의 값은 공통 수집기의 것이다(같은 이름으로 다시 내보낸다)
_values = api.catalog

PAGE_PAUSE, PAGE_JITTER, BACKOFF, AUTO_LATEST_AFTER = _values.PAGE_PAUSE, _values.PAGE_JITTER, _values.BACKOFF, _values.AUTO_LATEST_AFTER
SECONDS_PER_PAGE, MAX_CHANNELS, MAX_PAGES, SEARCH_LIMIT = _values.SECONDS_PER_PAGE, _values.MAX_CHANNELS, _values.MAX_PAGES, _values.SEARCH_LIMIT
MONTH_LIMIT, NO_MONTH, ACTIVE, STATUS_LABELS = _values.MONTH_LIMIT, _values.NO_MONTH, _values.ACTIVE, _values.STATUS_LABELS
RESTART_ERROR, STOP_ERROR, CANCEL_ERROR, COLUMNS = _values.RESTART_ERROR, _values.STOP_ERROR, _values.CANCEL_ERROR, _values.COLUMNS

Fetch = Callable[[str, int], Awaitable[tuple[int, Any]]]
ChannelFetch = Callable[[str], Awaitable[tuple[int, Any]]]


FetchError, CollectFailed, ChannelError = _common.FetchError, _common.CollectFailed, _common.ChannelError


def now() -> datetime:
    return datetime.now(timezone.utc)


def http_fetcher(api_base: str, allowed_hosts: tuple[str, ...], allow_http: bool, cfg) -> tuple[Fetch, ChannelFetch, Callable[[], Awaitable[None]]]:
    """한 번만 부르는 목록·채널 정보 요청. 다시 부르기는 수집기가 정한다. 쿠키는 영상 분석과 같이 저장한 값이 있으면 쓴다."""
    client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)

    async def get(url: str) -> tuple[int, Any]:
        # httpx는 `params=`를 주면 주소의 기존 쿼리(page·size)를 버린다. 캐시 회피 값은 주소에 직접 붙인다
        url += ("&" if "?" in url else "?") + urlencode(chzzk.cache_buster())
        if not host_allowed(url, allowed_hosts, allow_http):
            raise CollectFailed("허용하지 않는 주소입니다")
        try:
            values = await asyncio.to_thread(credentials.load, cfg, "chzzk")
        except (OSError, ValueError):
            values = {}
        try:
            resp = await httpread.aget(client, url, chzzk.MAX_RESPONSE, headers=chzzk.api_headers(values))
        except httpread.TooLarge:
            raise CollectFailed("목록 응답이 너무 큽니다") from None
        except httpx.HTTPError as e:
            raise FetchError(type(e).__name__) from None
        try:
            body = resp.json() if resp.status_code == 200 else None
        except ValueError:
            body = None
        return resp.status_code, body

    async def fetch(channel_id: str, page: int) -> tuple[int, Any]:
        return await get(video_list_url(api_base, channel_id, page))

    async def channel(channel_id: str) -> tuple[int, Any]:
        return await get(channel_url(api_base, channel_id))

    return fetch, channel, client.aclose


def chzzk_provider(fetch: Fetch, channel_fetch: ChannelFetch | None = None, **extra: Any) -> api.ListProvider:
    """치지직 공급자: 쪽 번호(0부터)로 한 쪽씩, 응답의 totalPages까지."""
    from ...platforms import registry

    def parse(status: int, payload: Any, page: Any, channel: str) -> dict:
        data = parse_page(status, payload, int(page), channel)
        return dict(data, next=int(page) + 1 if int(page) + 1 < data["total_pages"] else None)

    async def lookup(channel: str) -> str:
        status, payload = await channel_fetch(channel)
        return parse_channel(status, payload, channel)

    return api.ListProvider(
        platform="chzzk", extension="replay", name="치지직", subject="치지직이", fetch=fetch, parse=parse,
        video_url=lambda video_no, channel="": chzzk_video.video_url(video_no), first=0,
        lookup=lookup if channel_fetch is not None else None,
        valid_channel=lambda value: registry.get("chzzk").valid_channel_id(value),
        add_hint="치지직 채널 주소나 32자리 ID를 넣으십시오", video_types=True, **extra)


class Collector(_common.Collector):
    """치지직 공급자 하나를 단 공통 수집기(예전 호출 꼴: `Collector(db, fetch, sleep=…, channel_fetch=…)`)."""

    def __init__(self, db, fetch: Fetch, *, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 jitter: Callable[[], float] | None = None, clock: Callable[[], datetime] = now,
                 aclose: Callable[[], Awaitable[None]] | None = None, channel_fetch: ChannelFetch | None = None):
        super().__init__(db, {"chzzk": chzzk_provider(fetch, channel_fetch)}, sleep=sleep, jitter=jitter, clock=clock,
                         aclose=aclose, default="chzzk")
