"""씨미 방송 화면 읽기 — 화면(HTML)에 든 자료에서 방송 객체·채널 객체를 꺼낸다.

방송 화면에는 여러 채널의 방송 객체가 함께 들어 있으므로 반드시 채널 ID(또는 핸들)로 고른다. HTML 엔티티를 먼저 푼다.
재생목록 본문과 화면 본문은 로그에 남기지 않는다.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, Iterator

MAX_PAGE = 4 * 1024 * 1024
_DECODER = json.JSONDecoder()
# 해석의 상한: 화면이 이상하게 커도 시간이 끝없이 걸리지 않게 한다(키 자리 수, 전체 풀기 시도 수, 걸린 시간)
MAX_KEYS = 500
MAX_TRIES = 4000
MAX_SECONDS = 2.0


def _candidates(text: str, key: str) -> Iterator[dict[str, Any]]:
    """key를 가진 가장 안쪽 JSON 객체들(앞으로 거슬러 여는 중괄호를 찾아 하나씩 풀어 본다). 상한을 넘으면 멈춘다."""
    import time

    deadline = time.monotonic() + MAX_SECONDS
    tries = 0
    for n, m in enumerate(re.finditer(re.escape(key), text)):
        if n >= MAX_KEYS:
            return
        j = m.start()
        for _ in range(40):
            j = text.rfind("{", 0, j)
            if j < 0:
                break
            tries += 1
            if tries > MAX_TRIES or time.monotonic() > deadline:
                return
            try:
                obj, _end = _DECODER.raw_decode(text, j)
            except (ValueError, RecursionError):  # 너무 깊이 겹친 객체도 건너뛴다
                continue
            if isinstance(obj, dict) and key.strip('":') in obj:
                yield obj
                break


def _texts(body: str) -> list[str]:
    text = html.unescape(body)
    out = [text]
    if '\\"' in text:
        out.append(text.replace('\\"', '"'))  # 스크립트 문자열 안에 든 자료
    return out


def live_objects(body: str) -> list[dict[str, Any]]:
    """`playback`을 가진 방송 객체들."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in _texts(body):
        for obj in _candidates(text, '"playback":'):
            key = json.dumps([obj.get("id"), (obj.get("channel") or {}).get("id") if isinstance(obj.get("channel"), dict) else None])
            if key not in seen and isinstance(obj.get("playback"), dict):
                seen.add(key)
                found.append(obj)
    return found


def find_live(body: str, channel_id: str) -> dict[str, Any] | None:
    """그 채널의 방송 객체(채널 ID 일치). 방송 중(ACTIVE)인 것을 먼저 고른다."""
    mine = [o for o in live_objects(body)
            if isinstance(o.get("channel"), dict) and str(o["channel"].get("id")) == str(channel_id)]
    active = [o for o in mine if o.get("state") == "ACTIVE"]
    return (active or mine or [None])[0]


def find_channel(body: str, handle: str) -> dict[str, Any] | None:
    """핸들(slug)이 같은 채널 객체(`id`·`slug`·`name`)."""
    want = handle.lstrip("@").lower()
    for text in _texts(body):
        for obj in _candidates(text, '"slug":'):
            stack = [obj]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    if str(cur.get("slug") or "").lower() == want and cur.get("id") is not None and cur.get("name") is not None:
                        return cur
                    stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
                elif isinstance(cur, list):
                    stack.extend(cur)
    return None
