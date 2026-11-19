"""채널 다시보기 목록의 한 쪽 읽기.

수집은 `catalog.py`가 맡는다. 여기서는 응답 한 쪽을 검사하고 저장할 필드만 고른다.
저장하는 것은 화면에 보이는 값(제목·종류·게시 날짜·길이·성인·썸네일)과 영상 번호뿐이다.
썸네일은 `https://*.pstatic.net` 주소만 남기고 성인 영상은 남기지 않는다.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ...core.naming import seoul
from ..downloads import api

PAGE_SIZE = 30
THUMB_RE = re.compile(r"https://[a-z0-9.-]+\.pstatic\.net/[^\s\"<>]{1,500}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
# 목록 응답의 publishDate(`YYYY-MM-DD HH:MM:SS`)에는 시간대가 없다. 치지직 화면의 날짜와 같은 한국 시각으로 본다.
# 다른 시간대로 밝혀지면 이 값만 바꾼다. 달은 이 시각을 Asia/Seoul로 바꿔 정한다
SOURCE_TZ = seoul()


ListError = api.catalog.ListError  # 쪽 전체를 읽을 수 없다. 문장에 응답 본문을 넣지 않는다(공통 수집기의 형식)


def video_list_url(api_base: str, channel_id: str, page: int) -> str:
    return (f"{api_base.rstrip('/')}/service/v1/channels/{channel_id}/videos"
            f"?sortType=LATEST&pagingType=PAGE&page={page}&size={PAGE_SIZE}")


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


def _text(value: Any, length: int) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    return text[:length]


def publish_fields(value: Any) -> tuple[str | None, str | None]:
    """게시 날짜를 Asia/Seoul 시각 문자열과 달(`YYYY-MM`)로 바꾼다. 읽을 수 없으면 둘 다 비운다."""
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return None, None
    try:
        moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SOURCE_TZ).astimezone(seoul())
    except ValueError:
        return None, None
    return moment.strftime("%Y-%m-%d %H:%M:%S"), moment.strftime("%Y-%m")


def summary(item: dict) -> dict | None:
    """한 줄. 영상 번호가 없으면 저장할 수 없어 None. 나머지 값이 빠지면 그 값만 비운다."""
    no = _int(item.get("videoNo"))
    if no is None or no < 0:
        return None
    adult = item.get("adult") is True
    thumb = item.get("thumbnailImageUrl")
    publish, month = publish_fields(item.get("publishDate"))
    duration = _int(item.get("duration"))
    return {
        "video_no": str(no),
        "title": _text(item.get("videoTitle") or f"영상 {no}", 300),
        "video_type": "replay" if item.get("videoType") == "REPLAY" else "upload",
        "publish_date": publish,
        "month": month,
        "duration": duration if duration is not None and duration >= 0 else None,
        "adult": adult,
        "thumbnail": thumb if (not adult and isinstance(thumb, str) and THUMB_RE.fullmatch(thumb)) else None,
    }


def channel_url(api_base: str, channel_id: str) -> str:
    """채널 이름을 받는 주소."""
    return f"{api_base.rstrip('/')}/service/v1/channels/{channel_id}"


ChannelMissing = api.catalog.ChannelMissing  # 없는 채널


def parse_channel(status: int, payload: Any, channel_id: str) -> str:
    """채널 정보 응답에서 이름만 꺼낸다. 없는 채널이면 ChannelMissing, 이름을 읽을 수 없으면 ListError."""
    if status == 404:
        raise ChannelMissing("치지직에 없는 채널입니다")
    if status != 200 or not isinstance(payload, dict) or payload.get("code") != 200:
        raise ListError("채널 정보를 받지 못했습니다")
    content = payload.get("content")
    if content is None or (isinstance(content, dict) and content.get("channelId") in (None, "")):
        raise ChannelMissing("치지직에 없는 채널입니다")
    if not isinstance(content, dict) or content.get("channelId") != channel_id:
        raise ListError("채널 정보 응답 형식이 다릅니다")
    name = _text(content.get("channelName"), 100).strip()
    if not name:
        raise ListError("채널 이름을 받지 못했습니다")
    return name


def item_channel_name(item: dict, channel_id: str) -> str | None:
    """목록 줄에 그 채널의 이름이 있으면 꺼낸다."""
    channel = item.get("channel")
    if isinstance(channel, dict) and channel.get("channelId") == channel_id:
        name = _text(channel.get("channelName"), 100).strip()
        return name or None
    return None


def parse_page(status: int, payload: Any, page: int, channel_id: str | None = None) -> dict:
    """응답을 검사한다: code 200, page 일치, 쪽 정보가 모두 정수."""
    if status != 200 or not isinstance(payload, dict) or payload.get("code") != 200:
        raise ListError("목록 응답이 정상이 아닙니다")
    content = payload.get("content")
    if not isinstance(content, dict):
        raise ListError("목록 응답 형식이 다릅니다")
    data = content.get("data")
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ListError("목록 응답 형식이 다릅니다")
    total_pages, total_count = _int(content.get("totalPages")), _int(content.get("totalCount"))
    if _int(content.get("page")) != page or total_pages is None or total_count is None or total_pages < 0 or total_count < 0:
        raise ListError("목록 쪽 정보가 다릅니다")
    videos = [v for v in (summary(i) for i in data if isinstance(i, dict)) if v]
    names = [n for n in (item_channel_name(i, channel_id) for i in data if isinstance(i, dict) and channel_id) if n]
    return {"page": page, "total_pages": total_pages, "total_count": total_count, "videos": videos, "skipped": len(data) - len(videos),
            "channel_name": names[0] if names else None}
