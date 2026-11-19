"""받는 방법: 분석 결과의 형식(Video·Variant)과 화질 고르기, DASH·HLS 해석, 파일 받기(이어받기 포함), ffmpeg 구간·HLS 받기.

downloads 확장의 공개 접점이다. 작업 종류(다시보기·클립)의 분석 모듈이 `Video`를 만들어 넘기고, 이 모듈이 받는다.
어느 호스트로 요청해도 되는지와 영상 요청 머리글은 분석 모듈이 `configure`로 정한다(이 모듈은 플랫폼을 모른다).
이 모듈은 수신 프로세스(worker.py) 안에서만 돈다. 쿠키와 서명된 주소는 그 프로세스 메모리에만 있다.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from ...core import httpread

from . import hls_transport, resume, segments
from .errors import ReplayFailure, classify_exception, record_diagnostic

MPD_NS = "urn:mpeg:dash:schema:mpd:2011"
NAVER_NS = "urn:naver:vod:2020"
MAX_ITEMS = 50
MAX_JSON = 4 * 1024 * 1024
MAX_MPD = 16 * 1024 * 1024
QUALITY_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
SECTION = re.compile(r"(\d{2}):([0-5]\d):([0-5]\d)~(\d{2}):([0-5]\d):([0-5]\d)\Z")

ALLOW_LOCAL_HTTP = False  # 시험 전용(수신 프로세스 안에서만 켠다)
# 분석 모듈이 정하는 요청 규칙(configure). 수신 프로세스 하나는 작업 하나만 다룬다
ALLOWED_HOSTS: tuple[str, ...] = ()
MEDIA_HEADERS: dict[str, str] = {}


def configure(allowed_hosts: tuple[str, ...], headers: dict[str, str]) -> None:
    """이 프로세스에서 요청해도 되는 호스트(접미사 일치)와 영상 요청 머리글을 정한다."""
    global ALLOWED_HOSTS, MEDIA_HEADERS
    ALLOWED_HOSTS = tuple(allowed_hosts)
    MEDIA_HEADERS = dict(headers)


def media_headers() -> dict[str, str]:
    """영상 CDN 요청. 주소의 서명으로 인증되므로 쿠키를 싣지 않는다."""
    return dict(MEDIA_HEADERS)


# ---- 주소 ----
def parse_section(value: Any) -> tuple[float, float] | None:
    """`HH:MM:SS~HH:MM:SS`, 끝이 시작보다 늦어야 한다."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ReplayFailure("invalid_section")
    m = SECTION.fullmatch(value.strip())
    if not m:
        raise ReplayFailure("invalid_section")
    a, b, c, d, e, f = map(int, m.groups())
    start, end = a * 3600 + b * 60 + c, d * 3600 + e * 60 + f
    if end <= start:
        raise ReplayFailure("invalid_section")
    return float(start), float(end)


def _host_ok(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if ALLOW_LOCAL_HTTP and parts.scheme == "http" and host == "127.0.0.1":
        return True
    return (parts.scheme == "https" and parts.username is None and parts.password is None
            and any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS))


MAX_FILE_BYTES = 1 << 40  # 영상 파일 하나의 상한(풀어 받을 때도 한 번에 STEP만 메모리에 둔다)


def _check_url(url: str) -> str:
    if not isinstance(url, str) or httpread.url_has_bad_chars(url) or not _host_ok(url):
        raise ReplayFailure("protocol_denied")
    return url


# ---- HTTP ----
def _client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=8.0), follow_redirects=False, trust_env=False)


def _get(client: httpx.Client, url: str, headers: dict[str, str], limit: int) -> tuple[int, bytes]:
    _check_url(url)
    try:
        with client.stream("GET", url, headers=dict(headers, **httpread.TEXT_ENCODINGS)) as resp:
            body = b"".join(httpread.iter_body(resp, limit))  # 풀어 가며 센다(압축 폭탄)
            return resp.status_code, body
    except ReplayFailure:
        raise
    except httpread.TooLarge:
        raise ReplayFailure("invalid_media") from None
    except httpx.HTTPError as e:
        raise ReplayFailure(classify_exception(e)) from None


# ---- 분석 결과 ----
@dataclass
class Variant:
    id: str
    height: int | None
    width: int | None
    fps: float | None
    bandwidth: int
    kind: str  # file | hls
    url: str = field(repr=False)
    signed: bool = True  # hls일 때: 질의 서명(_lsu_sa_)을 조각에 물려줄지
    # 음성이 따로인 파일 받기: 영상 파일(이 Variant)과 음성 파일(audio)을 각각 받아 다시 인코딩 없이 합친다
    audio: "Variant | None" = field(default=None, repr=False)
    size: int | None = None  # 미리 아는 파일 크기(바이트). 모르면 None
    display: str = ""  # 분석 모듈이 정한 화면 이름(예: 높이·코덱·예상 크기). 비면 높이로 만든다

    def label(self, show_kind: bool = False) -> str:
        if self.display:
            return self.display
        if not self.height:
            return self.id
        fps = f"{round(self.fps)}" if self.fps and round(self.fps) > 30 else ""
        kind = " (HLS)" if show_kind and self.kind == "hls" else ""
        return f"{self.height}p{fps}{kind}"


@dataclass
class Video:
    url: str
    kind: str  # video | clip
    video_id: str
    title: str
    channel: str
    open_date: str | None  # liveOpenDate, `YYYY-MM-DD HH:MM:SS`
    duration: float | None
    thumbnail: str | None
    adult: bool
    mode: str  # dash | hls | clip
    variants: list[Variant]
    platform: str = ""  # 분석 모듈이 정한다
    # 받는 규칙(분석 모듈이 정한다): 한 범위 요청의 최대 크기, 연결 수 상한, 주소가 만료되는 시각(epoch 초)
    max_request: int | None = None
    max_connections: int | None = None
    expires_at: float | None = None
    # 받는 중에 갈아 주는 요청 자격(예: 서명 쿠키). `headers(주소) -> dict`와 `renew()`를 가진 객체.
    # 수신 프로세스 메모리에만 있다. 없으면(다른 종류) 예전 그대로다
    auth: Any = field(default=None, repr=False, compare=False)

    def public(self) -> dict[str, Any]:
        """화면과 저장소로 나가는 값. 서명된 주소는 넣지 않는다."""
        ordered = sort_variants(self.variants)
        return {
            "url": self.url, "platform": self.platform, "kind": self.kind, "video_id": self.video_id,
            "title": self.title, "channel": self.channel, "open_date": self.open_date,
            "duration_seconds": self.duration, "adult": self.adult, "mode": self.mode,
            "thumbnail": self.thumbnail if self.thumbnail and _host_ok(self.thumbnail) else None,
            "qualities": [{"id": v.id, "label": v.label(self.mode == "dash"), "height": v.height} for v in ordered],
            "supports": {"section": self.mode == "dash"},  # 빠른 다시보기와 클립은 구간이 없다
        }


def sort_variants(variants: list[Variant]) -> list[Variant]:
    """높이, 그다음 대역폭의 내림차순."""
    return sorted(variants, key=lambda v: (v.height or 0, v.bandwidth), reverse=True)


def pick_variant(variants: list[Variant], wanted: str) -> Variant:
    """분석 때 고른 화질 ID를 그대로 찾는다. 다시 분석한 목록에 없으면 quality_changed."""
    for v in variants:
        if v.id == wanted:
            return v
    raise ReplayFailure("quality_changed")


def select_by_rule(variants: list[Variant], wanted: str) -> Variant | None:
    """선택 규칙은 녹화 화질 규칙과 같다(ID → best/worst → 이름 → 목표 높이 이하 → best)."""
    from ...core.quality import Track, select

    tracks = [Track(v.id, v.height, v.fps, v.bandwidth) for v in variants]
    chosen = select(tracks, wanted)
    return next((v for v in variants if chosen and v.id == chosen.track_id), None)


# ---- 분석 ----
def _num(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) and n > 0 else None


def _frame_rate(value: Any) -> float | None:
    if isinstance(value, str) and "/" in value:
        a, _, b = value.partition("/")
        try:
            return float(a) / float(b) if float(b) else None
        except ValueError:
            return None
    return _num(value)


def _attr(element: ET.Element, name: str) -> str | None:
    """네임스페이스와 상관없이 속성을 찾는다."""
    for key, value in element.attrib.items():
        if key == name or key.endswith("}" + name):
            return value
    return None


def parse_mpd(xml_bytes: bytes, base_url: str) -> list[Variant]:
    """영상 AdaptationSet의 Representation을 읽는다. 오디오는 뺀다."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raise ReplayFailure("invalid_media") from None
    q = lambda tag: f"{{{MPD_NS}}}{tag}"  # noqa: E731
    out: list[Variant] = []
    seen: set[str] = set()
    for adaptation in root.iter(q("AdaptationSet")):
        as_mime = adaptation.get("mimeType") or ""
        if as_mime.startswith("audio/"):
            continue
        for rep in adaptation.findall(q("Representation")):
            mime = rep.get("mimeType") or as_mime
            if mime.startswith("audio/"):
                continue
            rid = rep.get("id") or ""
            if not QUALITY_ID.fullmatch(rid) or rid in seen:
                continue
            labels = {lab.get("kind"): (lab.text or "").strip() for lab in rep.findall(f"{{{NAVER_NS}}}Label")}
            height = _num(rep.get("height")) or _num(labels.get("resolution"))
            width = _num(rep.get("width"))
            fps = _frame_rate(rep.get("frameRate") or adaptation.get("frameRate")) or _num(labels.get("fps"))
            bandwidth = int(_num(rep.get("bandwidth")) or 0)
            base = rep.find(q("BaseURL"))
            base_url_text = (base.text or "").strip() if base is not None else ""
            # 서명된 재생목록(m3u)이 있으면 그것이 정본이다. BaseURL은 조각 폴더일 수 있다
            m3u = _attr(rep, "m3u") or _attr(adaptation, "m3u")  # 실측: 네임스페이스가 붙은 속성(`nvod:m3u`)이다
            target = m3u or base_url_text
            if not target:
                continue
            target = urljoin(base_url, target)
            is_hls = mime == "video/mp2t" or urlsplit(target).path.lower().endswith(".m3u8")
            if not _host_ok(target):
                continue
            seen.add(rid)
            out.append(Variant(rid, int(height) if height else None, int(width) if width else None, fps, bandwidth,
                               "hls" if is_hls else "file", target,
                               signed=hls_transport.is_signed_naver_hls(target) if is_hls else True))
    return out


def prefer_files(variants: list[Variant]) -> list[Variant]:
    """같은 높이·fps에 단일 파일과 HLS형이 둘 다 있으면 단일 파일을 남긴다(여러 연결로 받을 수 있다)."""
    best: dict[tuple, Variant] = {}
    for v in variants:
        key = (v.height, round(v.fps or 0))
        cur = best.get(key)
        if cur is None or (cur.kind == "hls" and v.kind == "file"):
            best[key] = v
    return [v for v in variants if best.get((v.height, round(v.fps or 0))) is v]


def parse_master(text: str, base_url: str) -> list[Variant]:
    """빠른 다시보기의 마스터 재생목록에서 화질 목록을 만든다."""
    out: list[Variant] = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[1]))
        elif line and not line.startswith("#") and pending is not None:
            m = re.match(r"(\d+)x(\d+)", pending.get("RESOLUTION", ""))
            height = int(m.group(2)) if m else None
            bandwidth = int(pending.get("BANDWIDTH", "0") or 0)
            url = urljoin(base_url, line)
            if _host_ok(url):
                vid = f"hls-{height or 0}p-{bandwidth}"
                out.append(Variant(vid, height, int(m.group(1)) if m else None, _num(pending.get("FRAME-RATE")),
                                   bandwidth, "hls", url, signed=hls_transport.is_signed_naver_hls(url)))
            pending = None
    return out



# ---- 다운로드 ----
Progress = Callable[[float | None], None]


class RangeDownloader:
    """단일 파일을 여러 연결로 나눠 받는다. 각 연결이 자기 구간을 이어받고, os.pwrite로 제자리에 쓴다.

    주소는 이 프로세스 메모리에만 있다. 서버가 범위 요청을 받지 않으면 한 연결로 받는다.

    범위로 받을 때는 받은 구간을 작업 폴더의 기록(resume.json)에 적어 두고, 다음 시도에서 받지 않은 구간만 받는다.
    - 데이터를 fsync한 뒤에만 그 구간을 기록에 넣는다(1초마다). 강제로 끝나도 기록은 실제보다 적을 뿐이다.
    - 이어받기 전에 확인한다: 같은 화질, 같은 전체 크기, 서버 검증값(ETag·Last-Modified)이 그대로이고,
      받아 둔 구간의 처음과 끝 64 KiB를 서버에서 다시 받아 같은지 본다. 하나라도 다르면 처음부터 받는다.
    - 범위 요청을 받지 않는 서버는 이어받을 수 없어 처음부터 받는다.
    """

    MIN_PART = 1024 * 1024  # 한 번에 맡기는 조각의 크기 범위(원본 aria2c 설정의 조각 1M을 하한으로 둔다)
    MAX_PART = 8 * 1024 * 1024
    SAMPLE = 64 * 1024
    FLUSH_SECONDS = 1.0

    def __init__(self, url: str, path: str, connections: int, on_progress: Progress | None = None,
                 attempts: int = 8, timeout: float = 30.0, *, variant_id: str = "", on_event: Callable[..., None] | None = None,
                 on_bytes: Callable[[int, int | None], None] | None = None, max_part: int | None = None,
                 deadline: float | None = None, auth: Any = None):
        self.url = _check_url(url)
        self.path = path
        self.work = Path(path).parent
        self.variant_id = variant_id
        self.connections = max(1, min(16, int(connections)))
        self.on_progress = on_progress
        self.on_event = on_event
        self.on_bytes = on_bytes
        self.attempts = attempts
        self.timeout = timeout
        self.done = 0
        self.total: int | None = None
        self._lock = threading.Lock()
        self._error: BaseException | None = None
        self._tasks: list[tuple[int, int]] = []
        self.part = self.MIN_PART
        self._pending: list[list[int]] = []  # 써 두었지만 아직 fsync하지 않은 구간
        self._durable: list[list[int]] = []  # fsync하고 기록에 넣은 구간
        self._state: dict[str, Any] | None = None
        self._last_bytes = 0.0
        self.max_part = max_part  # 한 범위 요청의 최대 크기
        self.deadline = deadline  # 이 시각(epoch 초)이 지나면 새 조각을 맡기지 않고 url_expired로 끝낸다
        self.auth = auth  # 요청마다 더하는 자격(서명 쿠키). 403이면 한 번 새로 받아 다시 시도한다

    def _headers(self, **extra: str) -> dict[str, str]:
        # 영상 파일은 압축하지 말라고 청한다. 그래도 압축해 오면 httpread가 상한을 두고 풀어 받는다
        return dict(media_headers(), **(self.auth.headers(self.url) if self.auth else {}), **httpread.MEDIA_ENCODINGS, **extra)

    def _renewed(self, status: int, done: list) -> bool:
        """403이고 자격을 아직 새로 받지 않았으면 새로 받고 참(다시 시도한다)."""
        if status == 403 and self.auth is not None and not done:
            done.append(True)
            self.auth.renew()
            return True
        return False

    def _probe(self, client: httpx.Client) -> tuple[int | None, bool, dict[str, str]]:
        renewed: list = []
        while True:
            with client.stream("GET", self.url, headers=self._headers(Range="bytes=0-0")) as r:
                if self._renewed(r.status_code, renewed):
                    continue
                return self._probe_answer(r)

    def _probe_answer(self, r) -> tuple[int | None, bool, dict[str, str]]:
        validators = {k: r.headers[k] for k in ("etag", "last-modified") if r.headers.get(k)}
        if r.status_code == 206:
            m = re.search(r"/(\d+)$", r.headers.get("content-range", ""))
            return (int(m.group(1)) if m else None), bool(m), validators
        if r.status_code == 200:
            size = r.headers.get("content-length")
            return (int(size) if size and size.isdigit() else None), False, validators
        raise ReplayFailure({401: "http_401", 403: "http_403", 400: "http_400", 410: "http_410"}.get(
            r.status_code, "engine_operation_failed"))

    def _report(self, pos: int, n: int) -> None:
        with self._lock:
            self.done += n
            if self._state is not None:
                self._pending.append([pos, pos + n])
            if self.on_progress and self.total:
                self.on_progress(min(99.9, self.done * 100.0 / self.total))
            now = time.monotonic()
            if self.on_bytes and now - self._last_bytes >= 0.5:
                self._last_bytes = now
                self.on_bytes(self.done, self.total)

    def _take(self) -> tuple[int, int] | None:
        with self._lock:
            if self._error or not self._tasks:
                return None
            if self.deadline is not None and time.time() >= self.deadline:
                self._error = ReplayFailure("url_expired")
                return None
            start, end = self._tasks.pop(0)
            return start, end - 1

    def _plan(self, gaps: list[tuple[int, int]]) -> None:
        """받지 않은 구간을 조각으로 나눠 맡길 목록을 만든다."""
        tasks = []
        for start, end in gaps:
            pos = start
            while pos < end:
                tasks.append((pos, min(end, pos + self.part)))
                pos += self.part
        self._tasks = tasks

    def _fetch_range(self, client: httpx.Client, fd: int, start: int, end: int) -> None:
        pos = start
        for attempt in range(self.attempts):
            if pos > end:
                return
            try:
                headers = self._headers(Range=f"bytes={pos}-{end}")
                with client.stream("GET", self.url, headers=headers) as r:
                    if r.status_code == 403 and self.auth is not None and attempt < self.attempts - 1:
                        self.auth.renew()  # 서명이 끝났거나 거부됐다: 새로 받아 받은 자리부터 다시
                        continue
                    if r.status_code != 206:
                        raise ReplayFailure({401: "http_401", 403: "http_403", 410: "http_410"}.get(
                            r.status_code, "engine_operation_failed"))
                    for chunk in httpread.iter_body(r, MAX_FILE_BYTES, 1 << 17):
                        if pos + len(chunk) > end + 1:
                            chunk = chunk[: end + 1 - pos]
                        os.pwrite(fd, chunk, pos)
                        self._report(pos, len(chunk))
                        pos += len(chunk)
                if pos > end:
                    return
            except httpx.HTTPError as e:  # 네트워크 오류는 받은 자리부터 다시 받는다. 디스크 오류(OSError)는 그대로 올린다
                if attempt == self.attempts - 1:
                    raise ReplayFailure(classify_exception(e)) from None
                time.sleep(min(10.0, 0.5 * 2 ** attempt))
        raise ReplayFailure("network_timeout")

    def _worker(self, fd: int) -> None:
        try:
            with _client(self.timeout) as client:
                while True:
                    task = self._take()
                    if task is None:
                        return
                    self._fetch_range(client, fd, *task)
        except BaseException as e:  # noqa: BLE001
            with self._lock:
                self._error = self._error or e

    # ---- 이어받기 ----
    def _flush(self, fd: int) -> None:
        """fsync 앞에 모은 구간만 기록에 넣는다(fsync 뒤에 쓴 구간은 다음 차례)."""
        with self._lock:
            snap, self._pending = self._pending, []
        if not snap or self._state is None:
            return
        os.fsync(fd)
        self._durable = resume.merge(self._durable + snap)
        resume.save(self.work, dict(self._state, done=self._durable))

    def _flusher(self, fd: int, stop: threading.Event) -> None:
        while not stop.wait(self.FLUSH_SECONDS):
            try:
                self._flush(fd)
            except OSError:
                pass  # 기록을 못 쓰면 다음에 다시 받을 뿐이다

    def _matches(self, client: httpx.Client, fd: int, done: list[list[int]]) -> bool:
        """받아 둔 구간의 처음과 끝을 서버에서 다시 받아 견준다."""
        spots = {(done[0][0], min(done[0][1], done[0][0] + self.SAMPLE)),
                 (max(done[-1][0], done[-1][1] - self.SAMPLE), done[-1][1])}
        for start, end in sorted(spots):
            with client.stream("GET", self.url, headers=self._headers(Range=f"bytes={start}-{end - 1}")) as r:
                if r.status_code != 206:
                    return False
                body = b""
                try:
                    for chunk in httpread.iter_body(r, end - start):
                        body += chunk
                except httpread.TooLarge:
                    return False  # 청한 범위보다 많이 오면 같은 파일이 아니다
            if body != os.pread(fd, end - start, start):
                return False
        return True

    def _resume(self, client: httpx.Client, ranged: bool, validators: dict[str, str]) -> list[list[int]] | None:
        """이어받을 구간 목록. 이어받을 수 없으면 남은 것을 지우고 None."""
        if not resume.has_prior(self.work):
            return None
        state = resume.load(self.work)
        reason = None
        if not ranged or not self.total:
            reason = "no_range"
        elif state is None or state.get("kind") != "range" or not isinstance(state.get("done"), list):
            reason = "no_state"
        elif (state.get("variant") != self.variant_id or state.get("size") != self.total
              or any(state.get("validators", {}).get(k) not in (None, v) for k, v in validators.items())):
            reason = "changed"
        if reason is None:
            try:
                done = resume.merge([[int(a), int(b)] for a, b in state["done"] if 0 <= int(a) < int(b) <= self.total])
                st = os.stat(self.path, follow_symlinks=False)
                if st.st_size != self.total:
                    reason = "mismatch"
            except (OSError, TypeError, ValueError):
                reason = "mismatch"
        if reason is None and not done:
            reason = "no_state"  # 부분 파일은 있으나 확실히 받았다고 적힌 구간이 없다
        if reason is None:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                if not self._matches(client, fd, done):
                    reason = "mismatch"
            except httpx.HTTPError:
                reason = "mismatch"
            finally:
                os.close(fd)
        if reason is not None:
            resume.restart(self.work, self.on_event, reason)
            return None
        return done

    def run(self) -> None:
        with _client(self.timeout) as client:
            self.total, ranged, validators = self._probe(client)
            done = self._resume(client, ranged, validators)
        if done is not None:
            fd = os.open(self.path, os.O_WRONLY | os.O_NOFOLLOW)
        else:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        stop = threading.Event()
        flusher = None
        try:
            if not ranged or not self.total:
                self._single(fd)
                return
            if done is None:
                os.ftruncate(fd, self.total)
                done = []
            self._durable = done
            self.done = resume.covered(done)
            self._state = {"kind": "range", "variant": self.variant_id, "size": self.total, "validators": validators}
            resume.save(self.work, dict(self._state, done=self._durable))
            if done and self.on_event:
                self.on_event("resume", mode="range", bytes=self.done, total=self.total)
            # 연결마다 조각이 몇 개씩 돌아가게 나눈다. 느린 연결이 끝을 붙잡지 않도록 조각을 너무 크게 하지 않는다
            self.part = max(self.MIN_PART, min(self.MAX_PART, self.total // (self.connections * 4) or 1))
            if self.max_part:
                self.part = max(1, min(self.part, int(self.max_part)))
            self._plan(resume.gaps(done, self.total))
            flusher = threading.Thread(target=self._flusher, args=(fd, stop), daemon=True)
            flusher.start()
            threads = [threading.Thread(target=self._worker, args=(fd,), daemon=True)
                       for _ in range(min(self.connections, max(1, len(self._tasks))))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stop.set()
            flusher.join()
            self._flush(fd)
            if self._error:
                if isinstance(self._error, ReplayFailure):
                    raise self._error
                raise ReplayFailure(classify_exception(self._error)) from None
            if self.done != self.total or resume.covered(self._durable) != self.total:
                raise ReplayFailure("invalid_media")
        finally:
            stop.set()
            if flusher is not None and flusher.is_alive():
                flusher.join(timeout=5)
            os.close(fd)

    def _single(self, fd: int) -> None:
        with _client(self.timeout) as client, client.stream("GET", self.url, headers=self._headers()) as r:
            if r.status_code != 200:
                raise ReplayFailure({401: "http_401", 403: "http_403"}.get(r.status_code, "engine_operation_failed"))
            pos = 0
            for chunk in httpread.iter_body(r, MAX_FILE_BYTES, 1 << 17):
                os.pwrite(fd, chunk, pos)
                self._report(pos, len(chunk))
                pos += len(chunk)


class FFmpegJob:
    """ffmpeg 복사 모드. 서명된 주소는 표준 입력으로만 넘긴다."""

    def __init__(self, ffmpeg: str, on_progress: Progress | None, expected: float | None):
        self.ffmpeg = ffmpeg
        self.on_progress = on_progress
        self.expected = expected
        self.processes: list[subprocess.Popen] = []

    def progress(self, seconds: float) -> None:
        if self.on_progress and self.expected:
            self.on_progress(min(99.9, seconds * 100.0 / self.expected))

    def section_from_file(self, url: str, out: str, start: float, end: float) -> None:
        """단일 파일의 구간. concat 스크립트(inpoint/outpoint)를 표준 입력으로 준다."""
        _check_url(url)
        if "'" in url or "\n" in url:
            raise ReplayFailure("protocol_denied")
        script = f"ffconcat version 1.0\nfile '{url}'\ninpoint {start:.3f}\noutpoint {end:.3f}\n".encode()
        # 원격 파일 하나만 연다. 로컬 파일(file)은 허용하지 않는다. 안쪽 형식은 mp4 계열과 MPEG-TS만
        protocols = "pipe,https,tls,tcp,crypto" + (",http" if ALLOW_LOCAL_HTTP else "")
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-progress", "pipe:1", "-nostats",
               "-protocol_whitelist", protocols, "-format_whitelist", "concat,mov,mpegts",
               "-safe", "0", "-f", "concat", "-i", "pipe:0",
               "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", "-f", "mp4", "-y", out]
        diagnostics: dict[str, str] = {}
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(proc)

        def drain() -> None:
            for chunk in iter(lambda: proc.stderr.read(4096), b""):
                record_diagnostic(diagnostics, chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            proc.stdin.write(script)
            proc.stdin.close()
        except OSError:
            pass
        for raw in proc.stdout:
            line = raw.decode("ascii", "replace").strip()
            if line.startswith("out_time_us=") and line[12:].lstrip("-").isdigit():
                self.progress(max(0, int(line[12:])) / 1e6)
        rc = proc.wait()
        reader.join(timeout=5)
        if rc != 0:
            raise ReplayFailure(diagnostics.get("code", "engine_operation_failed"))

    def hls(self, url: str, out: str, section: tuple[float, float] | None, signed: bool) -> None:
        """HLS형은 서명 전송(hls_transport)으로 받는다."""
        text = None
        if section:
            s, e = section
            text = f"{_hms(s)}~{_hms(e)}"
        hls_transport.run_signed_hls(
            url, out, text, self.ffmpeg, media_headers(), subprocess,
            lambda procs: self.processes.extend(procs), lambda: None,
            inherit_query=signed, on_progress=self.progress, output_format="mp4")


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def resolve_media_playlist(url: str, height: int | None, signed: bool, auth: Any = None) -> str:
    """HLS형의 주소가 마스터 재생목록이면 같은 높이(없으면 가장 높은 대역폭)의 미디어 재생목록을 고른다."""
    with _client() as client:
        status, body = _get(client, url, dict(media_headers(), **(auth.headers(url) if auth else {})), MAX_JSON)
        if status == 403 and auth is not None:
            auth.renew()
            status, body = _get(client, url, dict(media_headers(), **auth.headers(url)), MAX_JSON)
    if status != 200:
        raise ReplayFailure({401: "http_401", 403: "http_403", 400: "http_400"}.get(status, "engine_operation_failed"))
    text = body.decode("utf-8", "replace")
    if httpread.has_control(text):
        raise ReplayFailure("invalid_media")
    if "#EXT-X-STREAM-INF" not in text:
        return url
    variants = parse_master(text, url)
    if not variants:
        raise ReplayFailure("invalid_media")
    same = [v for v in variants if height and v.height == height]
    choice = max(same or variants, key=lambda v: v.bandwidth)
    if signed and hls_transport.is_signed_naver_hls(url) and not hls_transport.is_signed_naver_hls(choice.url):
        # 마스터의 서명을 미디어 재생목록에 물려준다(같은 출처 규칙)
        return hls_transport._media_url(url, choice.url)
    return choice.url


SPLIT_PARTS = ("video", "audio")
DONE_MARK = "done.json"
REFRESH_CODES = ("http_403", "http_410", "url_expired")  # 다시 분석해 새 주소로 잇는 실패
MAX_REANALYZE = 3


def _done_mark(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / DONE_MARK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _part_complete(folder: Path, variant_id: str) -> int | None:
    """다 받아 둔 파일이면 그 크기(합치기 실패 뒤 재시도: 다시 받지 않는다)."""
    mark = _done_mark(folder)
    if not mark or mark.get("variant") != variant_id or type(mark.get("size")) is not int:
        return None
    try:
        st = os.stat(folder / "part", follow_symlinks=False)
    except OSError:
        return None
    return mark["size"] if st.st_size == mark["size"] else None


def merge_av(ffmpeg: str, video_path: str, audio_path: str, out: str) -> None:
    """영상 파일과 음성 파일을 다시 인코딩하지 않고 mp4 하나로 합친다. 실패하면 merge_failed(두 파일은 그대로)."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", video_path, "-i", audio_path,
           "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", "-f", "mp4", "-y", out]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=6 * 3600)
    except (OSError, subprocess.TimeoutExpired):
        raise ReplayFailure("merge_failed") from None
    if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        try:
            os.unlink(out)
        except OSError:
            pass
        raise ReplayFailure("merge_failed")


def download_split(video: Video, variant: Variant, out: str, ffmpeg: str, connections: int,
                   on_progress: Progress | None = None, on_stage: Callable[[str], None] | None = None, *,
                   on_event: Callable[..., None] | None = None, on_bytes: Callable[[int, int | None], None] | None = None,
                   reanalyze: Callable[[], Video] | None = None) -> None:
    """영상·음성 파일을 각각 단일 파일 받기(여러 연결·이어받기)로 작업 폴더의 `video/`·`audio/`에 받고 합친다.

    - 진행률은 두 파일의 받은 바이트를 두 크기의 합과 견준다.
    - 주소가 만료됐거나(expires_at) 403·410으로 거부되면 reanalyze로 새 주소를 받아 잇는다(작업당 MAX_REANALYZE번).
      같은 형식이고 전체 크기가 같으면 이어받고(RangeDownloader의 이어받기 확인), 다르면 그 파일을 처음부터 받는다.
    - 다 받은 파일에는 `done.json`을 남긴다. 합치기에 실패해 다시 시도하면 받지 않고 합치기부터 한다."""
    work = Path(out).parent
    conns = max(1, min(int(connections), int(video.max_connections or 16)))
    parts = {"video": variant, "audio": variant.audio}
    done = {name: 0 for name in SPLIT_PARTS}
    sizes: dict[str, int | None] = {name: (v.size if v else None) for name, v in parts.items()}
    lock = threading.Lock()
    refreshed = {"n": 0}

    def report() -> None:
        amount = sum(done.values())
        total = sum(sizes.values()) if all(sizes.values()) else None  # type: ignore[arg-type]
        if on_bytes:
            on_bytes(amount, total)
        if on_progress and total:
            on_progress(min(99.9, amount * 100.0 / total))

    def counter(name: str) -> Callable[[int, int | None], None]:
        def cb(amount: int, total: int | None) -> None:
            with lock:
                done[name] = amount
                if total:
                    sizes[name] = total
                report()
        return cb

    if on_stage:
        on_stage("downloading")
    for name in SPLIT_PARTS:
        folder = work / name
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / "part"
        while True:
            current = parts[name]
            if current is None:
                raise ReplayFailure("invalid_media")
            have = _part_complete(folder, current.id)
            if have is not None and current.size and current.size != have:
                have = None  # 다시 분석했더니 전체 크기가 달라졌다: 처음부터 받는다
                (folder / DONE_MARK).unlink(missing_ok=True)
                resume.restart(folder, on_event, "changed")
            if have is not None:
                done[name] = sizes[name] = have
                report()
                if on_event:
                    on_event("resume", mode="range", bytes=sum(done.values()), total=None)  # 다 받아 둔 파일은 다시 받지 않는다
                break
            try:
                (folder / DONE_MARK).unlink(missing_ok=True)
                rd = RangeDownloader(current.url, str(path), conns, None, variant_id=current.id, on_event=on_event,
                                     on_bytes=counter(name), max_part=video.max_request, deadline=video.expires_at)
                rd.run()
                (folder / DONE_MARK).write_text(json.dumps({"variant": current.id, "size": rd.total}), encoding="utf-8")
                done[name] = sizes[name] = rd.total or done[name]
                report()
                break
            except ReplayFailure as e:
                if e.code not in REFRESH_CODES or reanalyze is None or refreshed["n"] >= MAX_REANALYZE:
                    raise
                refreshed["n"] += 1
                if on_event:
                    on_event("refresh", reason=e.code, count=refreshed["n"])
                fresh = reanalyze()
                new = pick_variant(fresh.variants, variant.id)
                video.expires_at = fresh.expires_at
                video.max_request, video.max_connections = fresh.max_request or video.max_request, fresh.max_connections or video.max_connections
                parts.update(video=new, audio=new.audio)
    if on_stage:
        on_stage("finalizing")
    merge_av(ffmpeg, str(work / "video" / "part"), str(work / "audio" / "part"), out)


def download(video: Video, quality_id: str, section: tuple[float, float] | None, out: str, ffmpeg: str,
             connections: int, on_progress: Progress | None = None, on_stage: Callable[[str], None] | None = None,
             *, on_event: Callable[..., None] | None = None, on_bytes: Callable[[int, int | None], None] | None = None,
             reanalyze: Callable[[], Video] | None = None) -> None:
    """선택한 화질을 out(임시 파일)에 받는다. 검증과 이름 붙이기는 관리자(manager)가 한다.

    out이 있는 작업 폴더에 지난 시도의 부분 파일이 있으면 이어받는다. 이어받을 수 없는 방식이거나
    부분 파일이 맞지 않으면 지우고 처음부터 받으며, on_event("resume", mode="restart", reason=…)로 알린다.
    """
    work = Path(out).parent
    variant = pick_variant(video.variants, quality_id)
    if section and video.mode != "dash":
        raise ReplayFailure("invalid_section")  # 빠른 다시보기·클립은 구간이 없다
    if section and video.duration and section[1] > video.duration + 1:
        raise ReplayFailure("invalid_section")
    expected = (section[1] - section[0]) if section else video.duration
    if variant.audio is not None:
        if section:
            raise ReplayFailure("invalid_section")  # 음성이 따로인 받기에는 구간이 없다
        download_split(video, variant, out, ffmpeg, connections, on_progress, on_stage, on_event=on_event,
                       on_bytes=on_bytes, reanalyze=reanalyze)
        return
    if on_stage:
        on_stage("downloading")
    if variant.kind == "file" and not section:
        RangeDownloader(variant.url, out, max(1, min(int(connections), int(video.max_connections or 16))), on_progress,
                        variant_id=variant.id, on_event=on_event, on_bytes=on_bytes, max_part=video.max_request,
                        auth=video.auth).run()
        return
    job = FFmpegJob(ffmpeg, on_progress, expected)
    if variant.kind == "file":
        resume.restart(work, on_event, "not_resumable")  # ffmpeg 구간 받기는 이어받을 수 없다
        job.section_from_file(variant.url, out, *section)  # type: ignore[misc]
        return
    media = resolve_media_playlist(variant.url, variant.height, variant.signed, auth=video.auth)
    try:
        segments.SegmentDownloader(media, work, ffmpeg, section, variant.signed, variant.id, media_headers(),
                                   on_progress=on_progress, on_event=on_event, on_bytes=on_bytes, on_stage=on_stage,
                                   check_url=_check_url, auth=video.auth).run(out)
    except segments.Unsupported:
        if video.auth is not None:
            raise ReplayFailure("invalid_media") from None  # 자격을 가는 받기는 ffmpeg에 주소만 넘기는 방식으로 대신하지 않는다
        resume.restart(work, on_event, "unsupported")
        job.hls(media, out, section, variant.signed)


# ---- 작업 종류의 분석 모듈이 쓰는 이름(공개 접점) ----
http_client = _client
http_get = _get
host_ok = _host_ok
check_url = _check_url
number = _num
frame_rate = _frame_rate
is_signed_naver_hls = hls_transport.is_signed_naver_hls
Failure = ReplayFailure
classify = classify_exception  # 예외를 공개 오류 코드로(작업 종류의 분석 모듈이 쓴다)
