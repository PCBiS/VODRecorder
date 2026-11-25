"""HLS 수신기. 코어가 별도 프로세스로 띄운다.

    python -m live_recorder.recording.receiver < job.json

- 작업(job)은 표준 입력의 JSON 한 덩어리로 받는다. 쿠키가 여기 들어 있으므로
  프로세스 인자와 환경변수에는 아무것도 싣지 않는다.
- 표준 출력에는 한 줄에 JSON 하나씩 진행 이벤트를 낸다. 표준 오류는 사람이 읽는 로그다.
  어느 쪽에도 쿠키와 토큰이 든 주소를 쓰지 않는다.
- 조각(fMP4면 초기화 조각 포함)을 받은 순서대로 출력 파일 끝에 붙인다.
- 영상 코덱은 처음 받은 것으로 고정한다. 변형은 작업의 `want_codec`·`codec_prefer`와 `CODECS`로 고르고,
  실제 코덱은 초기화 조각에서 읽는다. 받는 중에 코덱이 달라지면 그 조각을 쓰지 않고 `codec_changed`로 정상 종료한다.
  코어가 새 파일로 이어 받는다. 한 파일에 두 코덱을 이어 붙이지 않는다.
- 마스터 재생목록이 음성을 따로 두면(`EXT-X-MEDIA TYPE=AUDIO`에 URI) 음성 재생목록도 같은 프로세스에서 받아
  `audio_output`(없으면 `<output>`에서 정한 음성 원본 이름)에 쓴다. 합치기는 마무리가 한다.
- 수신기는 HLS만 안다. 주소 갱신, 스트림 요청 머리글, 조각 토큰은 작업에 적힌 플랫폼 모듈(`platform_module`)의
  접점으로 한다. 본체에는 플랫폼별 분기가 없다.

종료코드:
    0  방송이 끝났거나(ENDLIST) 종료 신호를 받아 정상 종료
    1  예상하지 못한 오류
    2  작업 형식 오류
    3  정해진 시간 동안 새 조각이 없음
    4  재생목록을 열 수 없고 주소 갱신도 실패
    5  지원하지 않는 스트림(암호화, 바이트 범위)
    6  파일 쓰기 실패(디스크)
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import httpx

from ..core import codec as codec_mod
from ..core import httpread

MAX_PLAYLIST = 4 * 1024 * 1024  # 재생목록 응답 상한
MAX_SEGMENT = 64 * 1024 * 1024  # 조각 하나의 상한(실시간 조각은 보통 수 MB)

from ..core.naming import audio_part
from ..platforms import registry
from ..platforms.base import ApiError, BroadcastChanged, Platform, host_allowed

EXIT_OK, EXIT_ERROR, EXIT_BAD_JOB, EXIT_STREAM_TIMEOUT, EXIT_PLAYLIST, EXIT_UNSUPPORTED, EXIT_WRITE = 0, 1, 2, 3, 4, 5, 6

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


class Unsupported(Exception):
    pass


class CodecChanged(Exception):
    """받던 것과 다른 영상 코덱을 만났다."""

    def __init__(self, codec: str):
        super().__init__(codec)
        self.codec = codec


class PlaylistHTTPError(Exception):
    def __init__(self, status: int):
        super().__init__(f"playlist http {status}")
        self.status = status


class SegmentAuthError(Exception):
    pass


@dataclass
class Segment:
    seq: int
    uri: str
    duration: float
    pdt: datetime | None
    map_uri: str | None
    discontinuity: bool = False


@dataclass
class MediaPlaylist:
    segments: list[Segment] = field(default_factory=list)
    target_duration: float = 2.0
    endlist: bool = False


@dataclass
class Variant:
    uri: str
    bandwidth: int
    height: int | None
    audio: str | None = None  # EXT-X-STREAM-INF의 AUDIO 묶음 이름
    codec: str = ""  # CODECS에서 읽은 영상 코덱(h264·hevc·av1). 없으면 빈 문자열


@dataclass
class Rendition:
    """`EXT-X-MEDIA TYPE=AUDIO` 한 줄. uri가 없으면 음성이 영상 재생목록에 섞여 있다."""
    group: str
    uri: str | None
    default: bool
    name: str = ""


def _attrs(text: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _ATTR.findall(text)}


def parse_pdt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def parse_master(text: str, base_url: str) -> list[Variant]:
    if httpread.has_control(text):
        raise Unsupported("제어문자가 든 재생목록")
    out: list[Variant] = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = _attrs(line.split(":", 1)[1])
        elif line and not line.startswith("#") and pending is not None:
            height = None
            m = re.match(r"(\d+)x(\d+)", pending.get("RESOLUTION", ""))
            if m:
                height = int(m.group(2))
            try:
                bw = int(pending.get("BANDWIDTH", "0"))
            except ValueError:
                bw = 0
            out.append(Variant(urljoin(base_url, line), bw, height, pending.get("AUDIO") or None,
                               codec_mod.from_codecs_attr(pending.get("CODECS"))))
            pending = None
    return out


def choose_variant(variants: list[Variant], want_height: int | None, want_codec: str = "", prefer: str = "") -> Variant:
    """같은 높이가 있으면 그 가운데서, 없으면 전체에서 고른다.

    먼저 정해진 코덱(`want_codec`)의 변형만 남긴다. 그런 변형이 없으면 우선순위(`prefer`)의 변형만 남기고,
    그것도 없으면 있는 것을 다 본다. 남은 것 가운데 대역폭이 가장 높은 것, 같으면 재생목록에 먼저 나온 것이다."""
    if not variants:
        raise Unsupported("variant가 없는 마스터 재생목록")
    pool = [v for v in variants if want_height and v.height == want_height] or list(variants)
    for key in (want_codec, prefer):
        if key and key != codec_mod.ANY:
            subset = [v for v in pool if v.codec == key]
            if subset:
                pool = subset
                break
    best = pool[0]
    for v in pool[1:]:
        if v.bandwidth > best.bandwidth:
            best = v
    return best


def parse_audio_groups(text: str, base_url: str) -> dict[str, list[Rendition]]:
    """마스터 재생목록의 음성 묶음."""
    groups: dict[str, list[Rendition]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = _attrs(line.split(":", 1)[1])
        if attrs.get("TYPE") != "AUDIO" or not attrs.get("GROUP-ID"):
            continue
        uri = attrs.get("URI")
        groups.setdefault(attrs["GROUP-ID"], []).append(
            Rendition(attrs["GROUP-ID"], urljoin(base_url, uri) if uri else None,
                      attrs.get("DEFAULT", "").upper() == "YES", attrs.get("NAME", "")))
    return groups


def choose_audio(groups: dict[str, list[Rendition]], group: str | None) -> str | None:
    """고른 영상의 음성 묶음에서 DEFAULT=YES, 없으면 첫 항목. 따로 받을 음성이 없으면 None."""
    items = groups.get(group or "") or []
    if not items:
        return None
    pick = next((r for r in items if r.default), items[0])
    return pick.uri


def parse_media(text: str, base_url: str) -> MediaPlaylist:
    if httpread.has_control(text):
        raise Unsupported("제어문자가 든 재생목록")
    pl = MediaPlaylist()
    seq = 0
    duration: float | None = None
    pdt: datetime | None = None
    next_pdt: datetime | None = None
    map_uri: str | None = None
    discontinuity = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            pl.target_duration = float(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pdt = parse_pdt(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MAP:"):
            attrs = _attrs(line.split(":", 1)[1])
            if "BYTERANGE" in attrs:
                raise Unsupported("EXT-X-MAP BYTERANGE")
            map_uri = urljoin(base_url, attrs.get("URI", ""))
        elif line.startswith("#EXT-X-KEY:"):
            if _attrs(line.split(":", 1)[1]).get("METHOD", "NONE") != "NONE":
                raise Unsupported("암호화된 스트림")
        elif line.startswith("#EXT-X-BYTERANGE:"):
            raise Unsupported("EXT-X-BYTERANGE")
        elif line == "#EXT-X-DISCONTINUITY":
            discontinuity = True
        elif line == "#EXT-X-ENDLIST":
            pl.endlist = True
        elif not line.startswith("#"):
            if duration is None:
                continue
            when = pdt or next_pdt
            pl.segments.append(Segment(seq, urljoin(base_url, line), duration, when, map_uri, discontinuity))
            next_pdt = when + timedelta(seconds=duration) if when else None
            seq += 1
            duration, pdt, discontinuity = None, None, False
    return pl


def start_index(segments: list[Segment], *, rewind: float, start_after: datetime | None, live_edge: int) -> int:
    """첫 조각을 정한다.

    - start_after가 있으면 그 시각부터의 첫 조각(이어받기). 조각의 가운데가 그 시각 뒤인 첫 조각이다.
      재생목록의 시각은 밀리초로 적히고 앞 파일의 끝 시각은 조각 길이(예: 2.002초)를 더한 값이라, 다음 조각의 시각이
      끝 시각보다 몇 마이크로초 앞설 수 있다. 시각을 그대로 견주면 그 조각을 건너뛴다.
    - rewind가 있으면 목록 끝에서 그만큼 거슬러 간 조각. 목록이 더 짧으면 목록 처음.
    - 둘 다 없으면 끝에서 live_edge번째 조각.
    """
    if not segments:
        return 0
    if start_after is not None and all(s.pdt for s in segments):
        for i, s in enumerate(segments):
            # start_after는 앞 파일 마지막 조각의 끝 시각
            if s.pdt + timedelta(seconds=s.duration / 2) > start_after:  # type: ignore[operator]
                return i
        return len(segments)
    if rewind > 0:
        total = 0.0
        for i in range(len(segments) - 1, -1, -1):
            total += segments[i].duration
            if total >= rewind:
                return i
        return 0
    return max(0, len(segments) - max(1, live_edge))


def align_index(segments: list[Segment], *, seq: int | None, pdt: datetime | None) -> int | None:
    """음성 첫 조각을 영상 첫 조각(seq, pdt)에 맞춘다. 맞출 근거가 없으면 None.

    시각이 있으면 영상 시작 시각을 덮는 조각(시작 시각이 그 시각 이하인 마지막 조각), 없으면 같은 번호의 조각."""
    if not segments:
        return None
    if pdt is not None and all(s.pdt for s in segments):
        slack = timedelta(milliseconds=50)
        if segments[-1].pdt + timedelta(seconds=segments[-1].duration) <= pdt:  # type: ignore[operator]
            return len(segments)  # 음성이 아직 그 시각까지 오지 않았다. 다음 조각부터
        idx = 0
        for i, s in enumerate(segments):
            if s.pdt <= pdt + slack:  # type: ignore[operator]
                idx = i
        return idx
    if seq is not None:
        for i, s in enumerate(segments):
            if s.seq == seq:
                return i
        if segments[-1].seq < seq:
            return len(segments)
        if segments[0].seq > seq:
            return 0
    return None


class Track:
    """재생목록 하나의 수신 상태. 음성이 섞인 방송은 영상 하나뿐이고, 따로 두는 방송은 음성이 하나 더 있다."""

    def __init__(self, kind: str, output: str):
        self.kind = kind  # "video" | "audio"
        self.output = output
        self.file = None
        self.media_url: str | None = None
        self.last_seq: int | None = None
        self.last_map: str | None = None
        self.bytes = 0
        self.segments = 0
        self.media_seconds = 0.0
        self.first_pdt: datetime | None = None
        self.last_end: datetime | None = None
        self.first_seq: int | None = None  # 처음 받기로 정한 조각(음성 맞추기에 쓴다)
        self.first_pdt_planned: datetime | None = None
        self.anchored = False  # 시작 조각을 정했다
        self.last_new = time.monotonic()
        self.auth_failed_seq: int | None = None
        self.done = False  # ENDLIST까지 받았다

    @property
    def label(self) -> str:
        return "음성 " if self.kind == "audio" else ""


class Receiver:
    def __init__(self, job: dict[str, Any]):
        self.job = job
        self.output = str(job["output"])
        self.audio_output = str(job.get("audio_output") or audio_part(self.output))
        self.master_url = str(job["master_url"])
        self.want_height = job.get("want_height")
        self.want_codec = str(job.get("want_codec") or "")  # 코어가 정한 코덱
        self.codec_prefer = str(job.get("codec_prefer") or "")  # 그 코덱의 변형이 없을 때 볼 우선순위
        self.codec = ""  # 이 파일에 받고 있는 영상 코덱. 처음 받은 것으로 고정한다
        self.codec_next = ""  # 바뀐 코덱(이 값이 있으면 이 회차를 끝낸다)
        self.rewind = float(job.get("rewind_seconds") or 0)
        self.start_after = parse_pdt(job["start_after"]) if job.get("start_after") else None
        self.refresh_spec = job.get("refresh") or None
        self.refresh_first = bool(job.get("refresh_first"))
        module = job.get("platform_module")
        self.platform: Platform = registry.load_module_platform(module) if module else registry.get(None)
        self.cookies = job.get("cookies") or {}
        self.allowed_hosts = tuple(job.get("allowed_hosts") or ())
        self.allow_http = bool(job.get("allow_http"))
        self.segment_timeout = float(job.get("segment_timeout", 15))
        self.segment_attempts = int(job.get("segment_attempts", 12))
        self.stream_timeout = float(job.get("stream_timeout", 60))
        self.reload_interval = job.get("reload_interval")
        self.refresh_after = float(job.get("refresh_after", 5 * 3600))
        self.refresh_attempts = int(job.get("refresh_attempts", 3))
        self.refresh_delay = float(job.get("refresh_delay", 3.0))
        self.live_edge = int(job.get("live_edge", 3))

        self.stop = threading.Event()
        self.client = httpx.Client(
            timeout=httpx.Timeout(self.segment_timeout, connect=min(5.0, self.segment_timeout)),
            follow_redirects=True,
            event_hooks={"request": [self._check_host]},
        )
        self.video = Track("video", self.output)
        self.audio: Track | None = None
        self.audio_url: str | None = None  # 마스터가 음성을 따로 두면 그 재생목록
        self.segment_token: tuple[str, str] | None = None  # (질의 이름, 값). 조각 인증이 거부되면 붙여 본다
        self.use_segment_token = False
        self.url_time = time.monotonic()
        self.variant: Variant | None = None

        self.bytes = 0
        self.gaps = 0
        self.refreshes = 0
        self._last_progress = 0.0
        self._started = False
        self.changed = False  # 주소를 새로 받았더니 다른 방송이었다

    @property
    def media_url(self) -> str | None:
        return self.video.media_url

    def tracks(self) -> list[Track]:
        return [self.video] + ([self.audio] if self.audio else [])

    # ---- 출력 ----
    def emit(self, event: str, **data: Any) -> None:
        data["event"] = event
        try:
            sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, ValueError):
            pass

    def log(self, message: str) -> None:
        message = _redact_urls(message)  # 예외 문장에 든 서명 주소가 로그로 나가지 않게
        try:
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
        except (BrokenPipeError, ValueError):
            pass

    def _snapshot(self) -> dict[str, Any]:
        v = self.video
        snap = {
            "bytes": self.bytes,
            "segments": v.segments,
            "media_seconds": round(v.media_seconds, 3),
            "first_media_time": v.first_pdt.isoformat() if v.first_pdt else None,
            "last_media_time": v.last_end.isoformat() if v.last_end else None,
            "gaps": self.gaps,
            "refreshes": self.refreshes,
        }
        if self.audio:
            snap["audio_segments"] = self.audio.segments
            snap["audio_media_seconds"] = round(self.audio.media_seconds, 3)
        return snap

    # ---- HTTP ----
    def _check_host(self, request: httpx.Request) -> None:
        if not host_allowed(str(request.url), self.allowed_hosts, self.allow_http):
            raise httpx.RequestError("허용하지 않는 호스트", request=request)

    def _get_text(self, url: str) -> str:
        resp = httpread.get(self.client, url, MAX_PLAYLIST, headers=self.platform.request_headers("stream", None))
        if resp.status_code >= 400:
            raise PlaylistHTTPError(resp.status_code)
        return resp.text

    def _open_master(self, master_url: str) -> None:
        text = self._get_text(master_url)
        audio_url: str | None = None
        if is_master(text):
            variant = choose_variant(parse_master(text, master_url), self.want_height,
                                     self.codec or self.want_codec, self.codec_prefer)
            if self.codec and variant.codec and variant.codec != self.codec:
                raise CodecChanged(variant.codec)  # 받던 코덱의 변형이 없어졌다. 이어 붙이지 않는다
            self.variant = variant
            media_url = self.variant.uri
            audio_url = choose_audio(parse_audio_groups(text, master_url), self.variant.audio)
        else:
            self.variant = None
            media_url = master_url
        if audio_url and not host_allowed(audio_url, self.allowed_hosts, self.allow_http):
            raise Unsupported("허용하지 않는 호스트의 음성 재생목록")
        self.video.media_url = media_url
        if self.audio is not None:
            if audio_url:
                self.audio.media_url = audio_url
            else:
                self.log("새 재생 주소에 따로 받을 음성이 없습니다. 지난 음성 주소를 그대로 씁니다")
        elif audio_url and self.video.last_seq is not None:
            self.log("새 재생 주소에 따로 받을 음성이 생겼지만 이번 녹화에서는 받지 않습니다")
            audio_url = None
        self.audio_url = audio_url
        self.segment_token = self.platform.segment_token(master_url)
        self.master_url = master_url
        self.url_time = time.monotonic()

    def _refresh(self, reason: str) -> bool:
        """재생 주소를 새로 받아 같은 파일에 이어 쓴다. 따로 받는 음성도 같이 바뀐다."""
        if not self.refresh_spec:
            return False
        for attempt in range(self.refresh_attempts):
            if attempt and self.stop.wait(self.refresh_delay):
                return False
            try:
                new_url = self.platform.refresh_stream(self.client, self.refresh_spec, self.cookies)
                self._open_master(new_url)
            except BroadcastChanged as e:
                self.log(f"다른 방송으로 바뀌었습니다: {e}")
                self.changed = True
                self.emit("refresh", reason=reason, ok=False)
                return False
            except CodecChanged as e:
                self._note_codec_change(e.codec)
                self.emit("refresh", reason=reason, ok=False)
                return False
            except (httpx.HTTPError, ApiError, PlaylistHTTPError, Unsupported) as e:
                self.log(f"주소 갱신 실패({attempt + 1}/{self.refresh_attempts}): {type(e).__name__}: {e}")
                continue
            self.refreshes += 1
            self.emit("refresh", reason=reason, ok=True, **self._snapshot())
            self.log(f"재생 주소를 갱신했습니다: {reason}")
            return True
        self.emit("refresh", reason=reason, ok=False)
        return False

    def _segment_url(self, url: str) -> str:
        if (self.use_segment_token and self.segment_token and f"{self.segment_token[0]}=" not in url
                and _same_host(url, self.master_url)):  # 토큰은 마스터 주소와 같은 호스트에만 보낸다
            name, value = self.segment_token
            return url + ("&" if "?" in url else "?") + f"{name}={value}"
        return url

    def _download(self, url: str, *, what: str) -> bytes | None:
        """조각 하나를 받는다. 없어진 조각이면 None(건너뜀). 인증 거부면 SegmentAuthError."""
        delay = 0.5
        gone = 0
        for attempt in range(self.segment_attempts):
            if self.stop.is_set():
                return None
            if attempt:
                if self.stop.wait(delay):
                    return None
                delay = min(delay * 1.6, 3.0)
            try:
                headers = dict(self.platform.request_headers("stream", None), **httpread.MEDIA_ENCODINGS)
                with self.client.stream("GET", self._segment_url(url), headers=headers) as resp:
                    status = resp.status_code
                    if status in (401, 403):
                        if self.segment_token and not self.use_segment_token:
                            # 플랫폼이 정한 조각 토큰 인자. 필요할 때만 붙인다
                            self.use_segment_token = True
                            continue
                        raise SegmentAuthError(f"{what} http {status}")
                    if status in (404, 410):
                        gone += 1
                        if gone >= 2:
                            return None
                        continue
                    if status >= 400:
                        continue
                    chunks = []
                    for chunk in httpread.iter_body(resp, MAX_SEGMENT):  # 조각 하나의 상한(풀어 가며 센다)
                        if self.stop.is_set():
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError as e:
                self.log(f"{what} 수신 오류({attempt + 1}/{self.segment_attempts}): {type(e).__name__}")
                continue
        return None

    # ---- 본체 ----
    def _write(self, track: Track, data: bytes) -> None:
        track.file.write(data)  # type: ignore[union-attr]
        track.file.flush()  # type: ignore[union-attr]
        track.bytes += len(data)
        self.bytes += len(data)

    def _progress(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_progress >= 2.0:
            self._last_progress = now
            self.emit("progress", **self._snapshot())

    def run(self) -> int:
        try:
            if not host_allowed(self.master_url, self.allowed_hosts, self.allow_http):
                self.log("허용하지 않는 호스트의 재생 주소")
                return self._exit(EXIT_BAD_JOB, "bad_host")
            return self._loop()
        except Unsupported as e:
            self.log(f"지원하지 않는 스트림: {e}")
            return self._exit(EXIT_UNSUPPORTED, "unsupported")
        except OSError as e:
            self.log(f"파일 쓰기 실패: {e.strerror or type(e).__name__}")
            return self._exit(EXIT_WRITE, "write_error")
        finally:
            for track in self.tracks():
                if track.file is not None:
                    try:
                        track.file.close()
                    except OSError:
                        pass
            self.client.close()

    def _note_codec_change(self, new: str) -> None:
        self.codec_next = new
        self.log(f"영상 코덱이 바뀌었습니다: {codec_mod.label(self.codec)} → {codec_mod.label(new)}. 이 파일은 여기서 끝냅니다")

    def _codec_exit(self) -> int:
        """코덱이 바뀌어 이 회차를 정상으로 끝낸다. 코어가 새 파일로 잇는다."""
        return self._exit(EXIT_OK, "codec_changed", codec=self.codec, next_codec=self.codec_next)

    def _exit(self, code: int, reason: str, **extra: Any) -> int:
        self.emit("exit", code=code, reason=reason, **extra, **self._snapshot())
        extra = f" audio_segments={self.audio.segments}" if self.audio else ""
        self.log(f"수신 종료: code={code} reason={reason} segments={self.video.segments}{extra} bytes={self.bytes}")
        return code

    def _refresh_failed(self, reason: str) -> int:
        """주소 갱신이 실패했을 때의 종료. 다른 방송으로 바뀐 것이면 이 회차를 정상으로 끝낸다."""
        if self.changed:
            return self._exit(EXIT_OK, "broadcast_changed")
        if self.codec_next:
            return self._codec_exit()
        return self._exit(EXIT_PLAYLIST, reason)

    def _todo(self, track: Track, segs: list[Segment]) -> list[Segment] | None:
        """이번에 받을 조각. None이면 아직 시작 조각을 정할 수 없다(음성이 영상 시작을 기다린다)."""
        if track.last_seq is None:
            if track.kind == "audio":
                v = self.video
                if not v.anchored:
                    return None
                idx = align_index(segs, seq=v.first_seq, pdt=v.first_pdt_planned)
                if idx is None:
                    idx = start_index(segs, rewind=self.rewind, start_after=self.start_after, live_edge=self.live_edge)
            else:
                idx = start_index(segs, rewind=self.rewind, start_after=self.start_after, live_edge=self.live_edge)
            todo = segs[idx:]
            track.anchored = True
            if todo:
                track.first_seq = todo[0].seq
                track.first_pdt_planned = todo[0].pdt
            else:
                track.first_seq, track.first_pdt_planned = None, None
            if segs and not todo:
                track.last_seq = segs[-1].seq  # 이어받을 조각이 아직 없다. 다음 조각부터 받는다
            return todo
        if segs and segs[-1].seq < track.last_seq:
            # 번호가 뒤로 갔다. 시각이 앞으로 갔으면 목록이 새로 시작한 것이고, 아니면 묵은 응답이다.
            newer = bool(track.last_end and segs[-1].pdt and segs[-1].pdt >= track.last_end)
            return [s for s in segs if s.pdt and track.last_end and s.pdt >= track.last_end] if newer else []
        todo = [s for s in segs if s.seq > track.last_seq]
        if todo and todo[0].seq > track.last_seq + 1:
            missing = todo[0].seq - track.last_seq - 1
            self.gaps += missing
            self._gap(track, missing=missing)
            self.log(f"{track.label}조각 {missing}개가 재생목록에서 이미 빠졌습니다")
        return todo

    def _gap(self, track: Track, **data: Any) -> None:
        if track.kind == "audio":
            data["track"] = "audio"
        self.emit("gap", **data)

    def _poll(self, track: Track) -> tuple[str, Any]:
        """재생목록 하나를 한 번 읽고 새 조각을 받는다.

        돌려주는 값: ("exit", 종료코드) | ("again", 쉴 초) | ("stop", None) | ("ok", (받을 조각 수, 목표 길이))"""
        try:
            playlist = parse_media(self._get_text(track.media_url), track.media_url)  # type: ignore[arg-type]
        except PlaylistHTTPError as e:
            self.log(f"{track.label}재생목록 요청 실패: http {e.status}")
            if not self._refresh(f"playlist_http_{e.status}"):
                return "exit", self._refresh_failed("playlist_unavailable")
            return "again", 0.0
        except httpx.HTTPError as e:
            self.log(f"{track.label}재생목록 요청 오류: {type(e).__name__}")
            if time.monotonic() - track.last_new > self.stream_timeout:
                return "exit", self._exit(EXIT_STREAM_TIMEOUT, "stream_timeout")
            return "again", 1.0

        todo = self._todo(track, playlist.segments)
        if todo is None:
            track.last_new = time.monotonic()  # 영상이 시작 조각을 정하기 전에는 음성의 정지를 재지 않는다
            return "ok", (0, playlist.target_duration)
        auth_failed = False
        for seg in todo:
            if self.stop.is_set():
                break
            try:
                if seg.map_uri and seg.map_uri != track.last_map:
                    init = self._download(seg.map_uri, what=f"{track.label}초기화 조각")
                    if init is None:
                        if self.stop.is_set():
                            break
                        raise Unsupported("초기화 조각을 받을 수 없습니다")
                    if track.kind == "video":
                        found = codec_mod.from_init(init)
                        if found and self.codec and found != self.codec:
                            raise CodecChanged(found)  # 쓰지 않고 끝낸다
                        if found and not self.codec:
                            if self.variant and self.variant.codec and self.variant.codec != found:
                                self.log(f"재생목록이 밝힌 코덱({codec_mod.label(self.variant.codec)})과 받은 코덱"
                                         f"({codec_mod.label(found)})이 다릅니다. 받은 것을 따릅니다")
                            self.codec = found
                    self._write(track, init)
                    track.last_map = seg.map_uri
                data = self._download(seg.uri, what=f"{track.label}조각 {seg.seq}")
            except CodecChanged as e:
                self._note_codec_change(e.codec)
                return "exit", self._codec_exit()
            except SegmentAuthError:
                if track.auth_failed_seq == seg.seq:
                    data = None  # 갱신해도 거부되면 건너뛴다
                else:
                    track.auth_failed_seq = seg.seq
                    auth_failed = True
                    break
            if self.stop.is_set() and data is None:
                break
            track.last_seq = seg.seq
            if data is None:
                self.gaps += 1
                self._gap(track, missing=1, seq=seg.seq)
                continue
            self._write(track, data)
            track.segments += 1
            track.media_seconds += seg.duration
            if seg.pdt:
                if track.first_pdt is None:
                    track.first_pdt = seg.pdt
                track.last_end = seg.pdt + timedelta(seconds=seg.duration)
            if track.kind == "video" and not self._started:
                self._started = True
                if not self.codec and self.variant:
                    self.codec = self.variant.codec  # 초기화 조각이 없는 스트림(MPEG-TS)은 재생목록이 밝힌 코덱을 따른다
                self.emit(
                    "start",
                    codec=self.codec,
                    start_seq=seg.seq,
                    first_media_time=seg.pdt.isoformat() if seg.pdt else None,
                    height=self.variant.height if self.variant else None,
                    bandwidth=self.variant.bandwidth if self.variant else None,
                    **({"separate_audio": True} if self.audio else {}),
                )
            track.last_new = time.monotonic()
            self._progress()

        if auth_failed:
            if not self._refresh("segment_auth"):
                return "exit", self._refresh_failed("segment_auth")
            return "again", 0.0
        if self.stop.is_set():
            return "stop", None
        if playlist.endlist:
            track.done = True
        elif time.monotonic() - track.last_new > self.stream_timeout:
            # 따로 받는 음성이 멈춰도 녹화 전체가 멈춘 것으로 본다
            if track.kind == "audio":
                self.log("음성 조각이 멈췄습니다")
            return "exit", self._exit(EXIT_STREAM_TIMEOUT, "stream_timeout")
        return "ok", (len(todo), playlist.target_duration)

    def _fresh_master(self) -> None:
        """마스터 주소를 열기 전에 플랫폼의 갱신 방법으로 새 주소를 받는다(갱신 횟수에는 세지 않는다).
        받지 못하면 작업에 적힌 주소를 그대로 쓴다(열다 실패하면 보통의 갱신 규칙을 따른다)."""
        try:
            fresh = self.platform.refresh_stream(self.client, self.refresh_spec, self.cookies)
        except BroadcastChanged:
            self.changed = True
            return
        except (httpx.HTTPError, ApiError) as e:
            self.log(f"처음 주소를 새로 받지 못해 받은 주소를 씁니다: {type(e).__name__}")
            return
        if host_allowed(fresh, self.allowed_hosts, self.allow_http):
            self.master_url = fresh
            self.log("처음 열기 전에 재생 주소를 새로 받았습니다")

    def _loop(self) -> int:
        if self.refresh_first and self.refresh_spec:
            self._fresh_master()
            if self.changed:
                return self._exit(EXIT_OK, "broadcast_changed")
        try:
            self._open_master(self.master_url)
        except (PlaylistHTTPError, httpx.HTTPError) as e:
            self.log(f"재생목록을 열 수 없습니다: {type(e).__name__}: {e}")
            if not self._refresh("open_failed"):
                return self._refresh_failed("playlist_unavailable")
        self.video.file = open(self.output, "ab", buffering=0)
        if self.audio_url:
            self.audio = Track("audio", self.audio_output)
            self.audio.media_url = self.audio_url
            self.audio.file = open(self.audio_output, "ab", buffering=0)
            self.log("음성을 따로 받습니다")
        while not self.stop.is_set():
            if self.refresh_spec and time.monotonic() - self.url_time > self.refresh_after:
                if not self._refresh("scheduled"):
                    if self.changed or self.codec_next:
                        return self._refresh_failed("scheduled")
                    self.url_time = time.monotonic()  # 다음 주기에 다시 해 본다. 지금 주소는 계속 쓴다
            again: float | None = None
            busy = False
            targets: list[float] = []
            stopped = False
            for track in self.tracks():
                if track.done:
                    continue
                kind, value = self._poll(track)
                if kind == "exit":
                    return value
                if kind == "again":
                    again = value
                    break
                if kind == "stop":
                    stopped = True
                    break
                count, target = value
                busy = busy or count > 3
                targets.append(target)
            if again is not None:
                if again:
                    self.stop.wait(again)
                continue
            if stopped or self.stop.is_set():
                break
            if all(t.done for t in self.tracks()):
                return self._exit(EXIT_OK, "endlist")
            if busy:
                continue  # 밀린 구간을 따라잡는 중이면 쉬지 않는다
            target = min(targets) if targets else 1.0
            wait = self.reload_interval if self.reload_interval is not None else max(0.5, target)
            self.stop.wait(float(wait))
        return self._exit(EXIT_OK, "signal")


def _same_host(a: str, b: str | None) -> bool:
    from urllib.parse import urlsplit

    try:
        x, y = urlsplit(a), urlsplit(b or "")
        return bool(x.hostname) and (x.scheme, x.hostname, x.port) == (y.scheme, y.hostname, y.port)
    except ValueError:
        return False


def _redact_urls(text: str) -> str:
    """글 속의 주소는 스킴과 호스트만 남긴다(경로·질의에 서명이 들 수 있다)."""
    return re.sub(r"(?i)\b(https?://[^/\s\"'<>]+)[^\s\"'<>]*", r"\1/…", text)


def _set_parent_death_signal() -> None:
    """코어가 죽으면 같이 끝난다. 고아가 돼서 파일을 계속 쓰지 않게 한다."""
    from ..core.procutil import die_with_parent

    die_with_parent()  # 리눅스는 PR_SET_PDEATHSIG, macOS는 부모 번호를 보는 스레드


def main() -> int:
    os.umask(0o077)  # 녹화 파일은 소유자 전용(코어가 이미 077이면 같은 값)
    _set_parent_death_signal()
    try:
        job = json.loads(sys.stdin.read())
        if not isinstance(job, dict):
            raise ValueError("job은 JSON 객체여야 합니다")
        receiver = Receiver(job)
    except (ValueError, KeyError, TypeError, ImportError, AttributeError) as e:
        sys.stderr.write(f"작업 형식 오류: {type(e).__name__}\n")
        return EXIT_BAD_JOB
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: receiver.stop.set())
    try:
        return receiver.run()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"예상하지 못한 오류: {type(e).__name__}\n")
        receiver.emit("exit", code=EXIT_ERROR, reason="exception")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
