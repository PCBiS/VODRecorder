"""씨미 채널의 다시보기 목록 공급자. 모으기 일정·재시도·저장·조회는 downloads의 공통 수집기가 한다.

- 한 쪽: `GET https://ci.me/api/app/channels/{핸들}/videos?sort=RECENT&limit=30[&continuation=…]`.
  응답 `data.section.items`(종류 VIDEO)와 `data.continuation`·`hasMoreData`. 다음 쪽은 받은 `continuation` 값을 그대로 보낸다.
  `GET /api/app/videos?…&channelId=`는 채널을 가리지 않는 전체 목록이라 쓰지 않는다.
- 채널은 숫자 채널 ID로 저장하고, 목록 요청에 필요한 핸들은 씨미 플랫폼 모듈의 핸들 표(`Handles`)에서 찾는다.
- 로그인 쿠키(`mauth-authorization-code`)가 있으면 싣는다. 요청은 씨미의 요청 창구를 거친다(공통 수집기가 감싼다).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote

import httpx

from ...core import httpread

from ...platforms import credentials
from ...platforms.base import ApiError, ChannelLookupNeeded, ChannelParseError
from ...platforms.cime import platform as cmp
from ..downloads import api
from . import common, vod

LIST_PATH = "/api/app/channels/{}/videos"
PAGE_SIZE = 30  # 실측: limit=30을 그대로 받는다(주지 않으면 10)
THUMB_RE = re.compile(r"https://([a-z0-9-]+\.)*cf\.ci\.me/[^\s\"<>]{1,500}")
MISSING_HANDLE = "씨미 채널의 핸들을 몰라 목록을 받을 수 없습니다. 채널을 지우고 주소나 @핸들로 다시 추가하십시오"


def _text(value: Any, length: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:length]


def summary(item: dict) -> dict | None:
    """한 줄: 영상 ID, 제목, 길이, 공개 시각(liveOpenedAt 또는 openedAt, 한국 시각), 썸네일, 연령 제한."""
    ident = str(item.get("id") or "")
    if not re.fullmatch(r"[0-9]{1,20}", ident) or item.get("state") not in (None, "ACTIVE"):
        return None
    adult = item.get("isAdult") is True
    when = common.kst(item.get("liveOpenedAt") or item.get("openedAt"))
    duration = item.get("duration")
    thumb = item.get("imageUrl")
    return {
        "video_no": ident,
        "title": _text(item.get("title") or f"영상 {ident}", 300),
        "video_type": "replay",
        "publish_date": when,
        "month": when[:7] if when else None,
        "duration": int(duration // 1000) if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0 else None,
        "adult": adult,
        "thumbnail": thumb if (not adult and isinstance(thumb, str) and THUMB_RE.fullmatch(thumb)) else None,
    }


def parse_page(status: int, payload: Any, cursor: Any, channel_id: str, handles: cmp.Handles | None = None) -> dict:
    if status != 200 or not isinstance(payload, dict) or payload.get("code") not in (200, None):
        raise api.catalog.ListError("씨미 목록 응답이 정상이 아닙니다")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise api.catalog.ListError("씨미 목록 응답 형식이 다릅니다")
    section = data.get("section")
    if not isinstance(section, dict):
        section = next((s for s in data.get("sections") or [] if isinstance(s, dict) and s.get("type") == "VIDEO"), {})
    items = section.get("items") if isinstance(section, dict) else None
    if items is None:
        items = []
    if not isinstance(items, list):
        raise api.catalog.ListError("씨미 목록 응답 형식이 다릅니다")
    rows = [i for i in items if isinstance(i, dict)]
    mine = [i for i in rows if str((i.get("channel") or {}).get("id") or "") in ("", str(channel_id))]  # 다른 채널 줄은 버린다
    videos = [v for v in (summary(i) for i in mine) if v]
    name = next((_text(i["channel"].get("name"), 100).strip() for i in mine if isinstance(i.get("channel"), dict)
                 and str(i["channel"].get("id")) == str(channel_id) and i["channel"].get("name")), None)
    slug = next((str(i["channel"].get("slug")) for i in mine if isinstance(i.get("channel"), dict)
                 and str(i["channel"].get("id")) == str(channel_id) and i["channel"].get("slug")), None)
    if handles is not None and slug and cmp.HANDLE_RE.match("@" + slug):
        handles.set(str(channel_id), slug)  # 핸들이 바뀌었으면 고친다
    token = data.get("continuation")
    more = data.get("hasMoreData") is True and isinstance(token, str) and bool(token)
    return {"videos": videos, "next": token if more else None, "total_pages": None, "channel_name": name or None,
            "skipped": len(items) - len(videos)}


class CimeLists:
    """씨미 공급자. 확장의 접점(cfg)만 쓴다. 요청 창구는 공통 수집기가 씌운다."""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self.handles = cmp.Handles(getattr(cfg, "data_dir", None))
        self._client: httpx.AsyncClient | None = None
        self._lookup: Any = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)
        return self._client

    async def fetch(self, channel_id: str, cursor: Any) -> tuple[int, Any]:
        handle = await asyncio.to_thread(self.handles.get, channel_id)
        if not handle:
            raise api.catalog.CollectFailed(MISSING_HANDLE)
        url = cmp.base() + LIST_PATH.format(quote(handle.lstrip("@"), safe="")) + f"?sort=RECENT&limit={PAGE_SIZE}"
        if cursor:
            url += "&continuation=" + quote(str(cursor), safe="")
        if not cmp.host_allowed(url, cmp.site_hosts(), bool(getattr(self.cfg, "allow_http", False))):
            raise api.catalog.CollectFailed("허용하지 않는 주소입니다")
        try:
            cookies = await asyncio.to_thread(credentials.load, self.cfg, "cime")
        except (OSError, ValueError):
            cookies = {}
        headers = common.site_headers(cookies, f"https://ci.me/@{handle.lstrip('@')}/vods")
        try:
            resp = await httpread.aget(self.client(), url, common.MAX_JSON, headers=headers)
        except httpread.TooLarge:
            raise api.catalog.CollectFailed("씨미 목록 응답이 너무 큽니다") from None
        except httpx.HTTPError as e:
            raise api.catalog.FetchError(type(e).__name__) from None
        try:
            body = resp.json() if resp.status_code == 200 else None
        except ValueError:
            body = None
        return resp.status_code, body

    def parse(self, status: int, payload: Any, cursor: Any, channel_id: str) -> dict:
        return parse_page(status, payload, cursor, channel_id, self.handles)

    def _platform_client(self):
        if self._lookup is None:
            self._lookup = cmp.PLATFORM.client(self.cfg)
        return self._lookup

    async def resolve(self, text: str) -> tuple[str, str] | None:
        """주소나 @핸들을 (채널 ID, 이름)으로(채널 추가와 같은 조회). 씨미 주소가 아니면 None."""
        try:
            cmp.parse_channel(text)
        except ChannelLookupNeeded as need:
            try:
                cookies = await asyncio.to_thread(credentials.load, self.cfg, "cime")
                return await self._platform_client().lookup_channel(need.query, cookies)
            except ChannelParseError as e:
                raise api.catalog.ChannelError(404, str(e)) from None
            except (ApiError, httpx.HTTPError):
                raise api.catalog.FetchError("lookup") from None
        except ChannelParseError:
            return None
        return None

    async def lookup(self, channel_id: str) -> str:
        handle = await asyncio.to_thread(self.handles.get, channel_id)
        if not handle:
            raise api.catalog.ChannelMissing(MISSING_HANDLE)
        found = await self.resolve("@" + handle.lstrip("@"))
        if not found or found[0] != str(channel_id):
            raise api.catalog.ChannelMissing("씨미에 없는 채널입니다")
        return found[1]

    def video_url(self, video_no: str, channel_id: str = "") -> str:
        handle = (self.handles.get(channel_id) if channel_id else None) or channel_id or "channel"
        return f"https://ci.me/@{handle.lstrip('@')}/vods/{video_no}"

    async def aclose(self) -> None:
        for client in (self._client, self._lookup):
            if client is not None:
                await client.aclose()
        self._client = self._lookup = None

    def provider(self, available) -> api.ListProvider:
        return api.ListProvider(
            platform="cime", extension="cime_media", name="씨미", subject="씨미가", fetch=self.fetch, parse=self.parse,
            video_url=self.video_url, first=None, lookup=self.lookup, resolve=self.resolve,
            valid_channel=lambda value: cmp.PLATFORM.valid_channel_id(value),
            state_key=lambda video_no: vod.key({"video_id": video_no}),
            add_hint="씨미 채널 주소(ci.me/@핸들)나 @핸들을 넣으십시오", video_types=False, aclose=self.aclose,
            available=available)

