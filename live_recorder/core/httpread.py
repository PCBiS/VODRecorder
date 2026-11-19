"""크기 상한을 두고 응답을 읽는다. 다 받은 뒤 크기를 보지 않고, 받는 동안(압축을 푼 양으로) 센다.

    body = httpread.get(client, url, limit, headers=...)          # httpx.Client
    body = await httpread.aget(client, url, limit, headers=...)   # httpx.AsyncClient

넘으면 `TooLarge`(httpx.HTTPError의 하위)다. 부르는 쪽은 연결 실패와 같이 다룬다.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterator

import httpx


# 탭·줄바꿈(CR·LF) 밖의 제어문자(NUL 포함, C0·DEL). 재생목록에 있으면 도구마다 다르게 끊어 읽으므로 전체를 받지 않는다
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_BAD = re.compile(r"[\x00-\x20\x7f]")


def has_control(text: str) -> bool:
    return bool(_CONTROL.search(text))


def url_has_bad_chars(url: str) -> bool:
    """주소에 제어문자나 공백이 있는가."""
    return bool(_URL_BAD.search(url))


class TooLarge(httpx.HTTPError):
    def __init__(self, limit: int):
        super().__init__(f"응답이 {limit}바이트를 넘습니다")


class BadEncoding(httpx.HTTPError):
    def __init__(self, encoding: str):
        super().__init__(f"받지 않는 응답 인코딩: {encoding[:40]}")


# 글(재생목록·JSON·화면)을 받을 때 보내는 머리글. 풀 수 있는 gzip·deflate만 청한다(br·zstd는 청하지 않는다)
TEXT_ENCODINGS = {"Accept-Encoding": "gzip, deflate"}
# 영상·조각·파일을 받을 때. 압축해 보낼 이유가 없다. 그래도 압축해 오면 아래 상한 있는 풀기로 받는다
MEDIA_ENCODINGS = {"Accept-Encoding": "identity"}
STEP = 65536  # 한 번에 풀어 내는 최대 바이트


def _new_decoder(encoding: str):
    """gzip·deflate 한 겹만. 그 밖(br, zstd, 여러 겹)은 받지 않는다."""
    enc = (encoding or "").strip().lower()
    if enc in ("", "identity"):
        return None
    if enc in ("gzip", "x-gzip"):
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if enc == "deflate":
        return _Deflate()
    raise BadEncoding(enc)


class _Deflate:
    """deflate는 zlib 머리가 있는 꼴과 없는 꼴이 섞여 쓰인다. 첫 덩어리로 고른다."""

    def __init__(self):
        self._d = None

    def decompress(self, data: bytes, max_length: int = 0) -> bytes:
        if self._d is None:
            self._d = zlib.decompressobj()
            try:
                return self._d.decompress(data, max_length)
            except zlib.error:
                self._d = zlib.decompressobj(-zlib.MAX_WBITS)
        return self._d.decompress(data, max_length)

    @property
    def unconsumed_tail(self) -> bytes:
        return self._d.unconsumed_tail if self._d is not None else b""

    def flush(self) -> bytes:
        return self._d.flush() if self._d is not None else b""


class _Counter:
    """원본(전송) 바이트를 받아 풀어 낸 조각을 내놓는다. 풀어 낸 양이 상한을 넘으면 TooLarge.
    한 번에 STEP보다 많이 풀지 않으므로 순간 메모리도 상한 + STEP 아래다."""

    def __init__(self, encoding: str, limit: int):
        self.limit, self.total = limit, 0
        try:
            self.decoder = _new_decoder(encoding)
        except BadEncoding:
            raise

    def _take(self, piece: bytes) -> bytes:
        self.total += len(piece)
        if self.total > self.limit:
            raise TooLarge(self.limit)
        return piece

    def feed(self, raw: bytes):
        if self.decoder is None:
            if raw:
                yield self._take(raw)
            return
        data = raw
        while True:
            try:
                piece = self.decoder.decompress(data, STEP)
            except zlib.error:
                raise httpx.DecodingError("응답을 풀 수 없습니다") from None
            if piece:
                yield self._take(piece)
            data = self.decoder.unconsumed_tail
            if not data:
                return

    def finish(self):
        if self.decoder is not None:
            try:
                tail = self.decoder.flush()
            except zlib.error:
                raise httpx.DecodingError("응답을 풀 수 없습니다") from None
            if tail:
                yield self._take(tail)


def _check_length(resp: httpx.Response, limit: int) -> str:
    encoding = resp.headers.get("content-encoding", "")
    if encoding.strip().lower() in ("", "identity"):
        try:
            if int(resp.headers.get("content-length") or 0) > limit:
                raise TooLarge(limit)
        except ValueError:
            pass
    return encoding


def _already_read(resp: httpx.Response) -> bytes | None:
    """이미 메모리에 다 읽힌 응답(시험의 가짜 응답 등)이면 그 본문."""
    content = getattr(resp, "_content", None)
    return content if isinstance(content, bytes) else None


def iter_body(resp: httpx.Response, limit: int, chunk: int = STEP) -> Iterator[bytes]:
    """응답 본문을 풀어 가며 내놓는다(httpx가 통째로 풀지 않게 원본 바이트를 읽는다)."""
    encoding = _check_length(resp, limit)
    done = _already_read(resp)
    if done is not None:
        yield _Counter("", limit)._take(done)
        return
    counter = _Counter(encoding, limit)
    for raw in resp.iter_raw(chunk):
        yield from counter.feed(raw)
    yield from counter.finish()


async def aiter_body(resp: httpx.Response, limit: int, chunk: int = STEP) -> AsyncIterator[bytes]:
    encoding = _check_length(resp, limit)
    done = _already_read(resp)
    if done is not None:
        yield _Counter("", limit)._take(done)
        return
    counter = _Counter(encoding, limit)
    async for raw in resp.aiter_raw(chunk):
        for piece in counter.feed(raw):
            yield piece
    for piece in counter.finish():
        yield piece


@dataclass
class Body:
    status_code: int
    content: bytes
    headers: httpx.Headers

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        """너무 깊이 겹친 JSON(RecursionError)도 ValueError로 알린다."""
        try:
            return json.loads(self.content)
        except RecursionError:
            raise ValueError("JSON이 너무 깊습니다") from None


def _headers(kwargs: dict[str, Any]) -> dict[str, Any]:
    headers = dict(kwargs.pop("headers", None) or {})
    if not any(k.lower() == "accept-encoding" for k in headers):
        headers.update(TEXT_ENCODINGS)
    return headers


def get(client: httpx.Client, url: str, limit: int, method: str = "GET", **kwargs: Any) -> Body:
    headers = _headers(kwargs)
    with client.stream(method, url, headers=headers, **kwargs) as resp:
        return Body(resp.status_code, b"".join(iter_body(resp, limit)), resp.headers)


async def aget(client: httpx.AsyncClient, url: str, limit: int, method: str = "GET", **kwargs: Any) -> Body:
    headers = _headers(kwargs)
    async with client.stream(method, url, headers=headers, **kwargs) as resp:
        return Body(resp.status_code, b"".join([p async for p in aiter_body(resp, limit)]), resp.headers)
