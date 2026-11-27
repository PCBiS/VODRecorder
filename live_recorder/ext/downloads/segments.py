"""HLS형 다시보기를 조각 단위로 받고 이어받는다.

예전에는 ffmpeg가 서명된 재생목록을 읽어 조각을 받으면서 바로 MP4로 썼다. 그러면 중간에 멈춘 MP4는 쓸 수 없어 이어받을 수 없다.
이제는 이 프로세스가 조각을 하나씩 받아 작업 폴더의 `seg/`에 두고, 다 받으면 ffmpeg 복사 모드로 MP4 하나로 담는다.

- 재생목록은 hls_transport로 받고 검사·변환한다. 서명된 주소는 이 프로세스 메모리에만 있고,
  ffmpeg에는 표준 입력으로만 넘긴다. 디스크에는 조각 데이터와 이어받기 기록(주소 없음)만 남는다.
- 조각 파일 이름은 `<번호 6자리>-<크기>.<ts|m4s>`다. 데이터를 fsync한 뒤 이 이름으로 바꾸므로, 이름이 있고 크기가 맞는
  조각은 다 받은 조각이다. 받다 만 조각은 `.tmp`로 남아 다음 시도에서 지운다.
- 이어받기 전에 확인한다: 같은 화질·구간이고, 재생목록 지문(조각 이름과 길이, 태그. 서명·주소 앞부분은 뺀다)이 같고,
  마지막으로 받은 조각을 서버에서 다시 받아 같은지 본다. 초기화 조각(EXT-X-MAP)도 다시 받아 견준다. 다르면 처음부터 받는다.
- 구간 받기(DASH의 HLS형)는 구간과 겹치는 조각만 받고, 담을 때 ffmpeg가 구간을 자른다.
- 바이트 범위 조각(EXT-X-BYTERANGE)과 비어 있는 조각(EXT-X-GAP)은 이 방식으로 받지 않는다(`Unsupported`).
  그때는 예전처럼 ffmpeg가 바로 받는다. 그 방식은 이어받을 수 없다.

한계: 다 받은 조각과 담은 MP4가 잠깐 함께 있어 작업 폴더가 영상 크기의 두 배까지 커진다.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from ...core import httpread

from . import hls_transport, resume
from .errors import ReplayFailure, classify_exception, record_diagnostic

SEG_DIR = "seg"
NAME = re.compile(r"(\d{6})-(\d{1,15})\.(ts|m4s)\Z")
MAP_NAME = re.compile(r"map-(\d{3})\.mp4\Z")
SEGMENT_TAGS = ("#EXTINF:", "#EXT-X-DISCONTINUITY", "#EXT-X-KEY:", "#EXT-X-MAP:", "#EXT-X-PROGRAM-DATE-TIME:",
                "#EXT-X-BITRATE:", "#EXT-X-DATERANGE:")
MAX_SEGMENT = 512 * 1024 * 1024


class Unsupported(Exception):
    """이 방식으로 받을 수 없는 재생목록."""


def _basename(uri: str) -> str:
    return urlsplit(uri).path.rsplit("/", 1)[-1]


def _uri(line: str) -> re.Match | None:
    """다시 조립한(URI가 하나뿐인) KEY·MAP 줄의 URI. 두 번 이상 있으면 없는 것으로 본다."""
    found = list(re.finditer(r'[:,]URI="([^"]*)"(?=,|$)', line))
    return found[0] if len(found) == 1 else None


# 로컬 재생목록(ffmpeg에 넘긴다)에 다시 적는 태그. 그 밖의 태그는 넣지 않는다
LOCAL_TAGS = ("#EXTINF:", "#EXT-X-DISCONTINUITY", "#EXT-X-PROGRAM-DATE-TIME:", "#EXT-X-TARGETDURATION:", "#EXT-X-VERSION:",
              "#EXT-X-PLAYLIST-TYPE:", "#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-DISCONTINUITY-SEQUENCE:")


class Playlist:
    """변환을 마친(주소가 절대 주소인) 미디어 재생목록."""

    def __init__(self, payload: bytes):
        text = payload.decode("utf-8")
        if httpread.has_control(text):
            raise ReplayFailure("invalid_media")
        lines = [line.strip() for line in text.splitlines()]
        self.header: list[str] = []
        self.sequence = 0
        self.segments: list[dict] = []
        tags: list[str] = []
        fingerprint = hashlib.sha256()
        for line in lines:
            if not line:
                continue
            if line.startswith("#EXT-X-BYTERANGE") or line.startswith("#EXT-X-GAP") or (
                    line.startswith("#EXT-X-MAP:") and "BYTERANGE" in line):
                raise Unsupported(line.split(":", 1)[0])
            if line.startswith("#"):
                m = _uri(line)
                fingerprint.update((line[:m.start(1)] + _basename(m.group(1)) + line[m.end(1):] if m else line).encode() + b"\n")
                if line == "#EXT-X-ENDLIST" or line == "#EXTM3U":
                    continue
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    try:
                        self.sequence = int(line.split(":", 1)[1])
                    except ValueError:
                        raise Unsupported("sequence") from None
                    continue
                if line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:") and not self.segments and not tags:
                    self.header.append(line)
                elif line.startswith(SEGMENT_TAGS) or self.segments or tags:
                    tags.append(line)
                else:
                    self.header.append(line)
                continue
            fingerprint.update(_basename(line).encode() + b"\n")
            info = next((t for t in tags if t.startswith("#EXTINF:")), None)
            try:
                duration = float(info.split(":", 1)[1].split(",", 1)[0]) if info else None
            except ValueError:
                duration = None
            if duration is None or duration < 0:
                raise Unsupported("extinf")
            self.segments.append({"tags": tags, "uri": line, "duration": duration})
            tags = []
        if not self.segments:
            raise ReplayFailure("invalid_media")
        self.fingerprint = fingerprint.hexdigest()
        # 조각마다 그때 걸린 KEY·MAP 줄(구간 받기에서 앞 조각을 뺄 때 다시 넣는다)
        key = mapping = None
        self.maps: list[str] = []
        start = 0.0
        for seg in self.segments:
            for tag in seg["tags"]:
                if tag.startswith("#EXT-X-KEY:"):
                    key = tag
                elif tag.startswith("#EXT-X-MAP:"):
                    mapping = tag
                    uri = _uri(tag)
                    if not uri:
                        raise Unsupported("map")
                    if uri.group(1) not in self.maps:
                        self.maps.append(uri.group(1))
            seg.update(key=key, map=mapping, start=start, ext="m4s" if mapping else "ts")
            start += seg["duration"]

    def select(self, section: tuple[float, float] | None) -> tuple[list[int], float]:
        """구간과 겹치는 조각 번호와, 첫 조각 시작에서 구간 시작까지의 초."""
        if not section:
            return list(range(len(self.segments))), 0.0
        start, end = section
        chosen = [i for i, s in enumerate(self.segments) if s["start"] < end and s["start"] + s["duration"] > start]
        if not chosen:
            raise ReplayFailure("invalid_section")
        return chosen, max(0.0, start - self.segments[chosen[0]]["start"])


def rewrite_plain(content: bytes, manifest_url: str, check_url: Callable[[str], str]) -> bytes:
    """서명 질의가 없는(요청 자격을 머리글로 싣는) 다시보기 재생목록: 조각·MAP 주소를 절대 주소로 바꾼다.
    끝이 있는(ENDLIST) 미디어 재생목록만 받고, 모든 주소는 허용 호스트인지 본다."""
    from urllib.parse import urljoin

    if len(content) > hls_transport.MAX_MANIFEST_BYTES:
        raise ReplayFailure("invalid_media")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        raise ReplayFailure("invalid_media") from None
    if httpread.has_control(text):
        raise ReplayFailure("invalid_media")  # 제어문자가 든 재생목록은 통째로 받지 않는다
    lines = [line.strip() for line in text.splitlines()]
    if not lines or lines[0] != "#EXTM3U" or "#EXT-X-ENDLIST" not in lines:
        raise ReplayFailure("invalid_media")
    if any(line.startswith(("#EXT-X-STREAM-INF:", "#EXT-X-MEDIA:", "#EXT-X-KEY:")) for line in lines):
        raise ReplayFailure("invalid_media")  # 암호화와 마스터 재생목록은 이 방식으로 받지 않는다
    out = []
    for line in lines:
        if line and not line.startswith("#"):
            line = check_url(urljoin(manifest_url, line))
        elif line.startswith("#EXT-X-MAP:"):
            line = hls_transport.safe_tag(line, lambda uri: check_url(urljoin(manifest_url, uri)))
        out.append(line)
    return ("\n".join(out) + "\n").encode()


class SegmentDownloader:
    def __init__(self, url: str, work: Path, ffmpeg: str, section: tuple[float, float] | None, signed: bool,
                 variant_id: str, headers: dict[str, str], *, on_progress: Callable[[float | None], None] | None = None,
                 on_event: Callable[..., None] | None = None, on_bytes: Callable[[int, int | None], None] | None = None,
                 on_stage: Callable[[str], None] | None = None, attempts: int = 8, timeout: float = 30.0,
                 check_url: Callable[[str], str] | None = None, auth=None):
        self.url = url
        self.work = Path(work)
        self.seg_dir = self.work / SEG_DIR
        self.ffmpeg = ffmpeg
        self.section = section
        self.signed = signed
        self.variant_id = variant_id
        self.headers = headers
        self.on_progress = on_progress
        self.on_event = on_event
        self.on_bytes = on_bytes
        self.on_stage = on_stage
        self.attempts = attempts
        self.timeout = timeout
        self.check_url = check_url or (lambda u: u)
        self.host_check = check_url  # 주지 않으면 None: 서명 없는 재생목록도 hls_transport의 기본 호스트 검사를 쓴다
        self.fetched = 0  # 이 시도에서 받은 조각 수(시험이 본다)
        # 받는 중에 갈아 주는 요청 자격(예: 서명 쿠키). 있으면 재생목록을 이 모듈이 직접 받아 절대 주소로 바꾸고,
        # 요청마다 auth.headers(주소)를 더하며, 403이면 auth.renew() 뒤 한 번 다시 받는다. 없으면 예전 그대로다
        self.auth = auth

    # ---- 받기 ----
    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(self.timeout, connect=8.0), follow_redirects=False, trust_env=False)

    def _get(self, client: httpx.Client, url: str, sink) -> int:
        """조각 하나를 sink에 쓴다. 네트워크 오류는 처음부터 다시 받는다(조각은 작다)."""
        self.check_url(url)
        renewed = False
        for attempt in range(self.attempts):
            try:
                sink(None)  # 비운다
                size = 0
                headers = dict(self.headers, **(self.auth.headers(url) if self.auth else {}), **httpread.MEDIA_ENCODINGS)
                with client.stream("GET", url, headers=headers) as r:
                    if r.status_code == 403 and self.auth is not None and not renewed:
                        renewed = True
                        self.auth.renew()  # 거부되면 자격을 한 번 새로 받아 다시 시도한다
                        continue
                    if r.status_code != 200:
                        raise ReplayFailure({401: "http_401", 403: "http_403", 400: "http_400", 404: "not_found"}.get(
                            r.status_code, "engine_operation_failed"))
                    encoded = r.headers.get("content-encoding", "").strip().lower() not in ("", "identity")
                    expected = None if encoded else r.headers.get("content-length")
                    try:
                        for chunk in httpread.iter_body(r, MAX_SEGMENT, 1 << 17):
                            size += len(chunk)
                            sink(chunk)
                    except httpread.TooLarge:
                        raise ReplayFailure("invalid_media") from None
                if expected and expected.isdigit() and int(expected) != size:
                    raise httpx.ReadError("short segment")
                return size
            except httpx.HTTPError as e:
                if attempt == self.attempts - 1:
                    raise ReplayFailure(classify_exception(e)) from None
                time.sleep(min(10.0, 0.5 * 2 ** attempt))
        raise ReplayFailure("network_timeout")

    def _fetch_bytes(self, client: httpx.Client, url: str) -> bytes:
        parts: list[bytes] = []

        def sink(chunk):
            if chunk is None:
                parts.clear()
            else:
                parts.append(chunk)

        self._get(client, url, sink)
        return b"".join(parts)

    def _fetch_file(self, client: httpx.Client, url: str, index: int, ext: str) -> tuple[Path, int]:
        tmp = self.seg_dir / f"{index:06d}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            def sink(chunk):
                if chunk is None:
                    os.ftruncate(fd, 0)
                    os.lseek(fd, 0, os.SEEK_SET)
                else:
                    os.write(fd, chunk)

            size = self._get(client, url, sink)
            os.fsync(fd)
        finally:
            os.close(fd)
        final = self.seg_dir / f"{index:06d}-{size}.{ext}"
        os.replace(tmp, final)
        return final, size

    # ---- 이어받기 ----
    def _existing(self, playlist: Playlist) -> dict[int, Path]:
        """다 받은 조각. 이름과 크기가 맞지 않는 것과 받다 만 것은 지운다."""
        out: dict[int, Path] = {}
        for entry in os.scandir(self.seg_dir):
            path = Path(entry.path)
            m = NAME.fullmatch(entry.name)
            ok = False
            if m and entry.is_file(follow_symlinks=False):
                index = int(m.group(1))
                ok = (index < len(playlist.segments) and playlist.segments[index]["ext"] == m.group(3)
                      and entry.stat(follow_symlinks=False).st_size == int(m.group(2)) and index not in out)
            if ok:
                out[index] = path
            elif not MAP_NAME.fullmatch(entry.name):
                if entry.is_dir(follow_symlinks=False):
                    raise ReplayFailure("storage_unavailable")
                path.unlink()
        return out

    def _prepare(self, client: httpx.Client, playlist: Playlist) -> dict[int, Path]:
        plan = {"kind": "hls", "variant": self.variant_id, "section": list(self.section) if self.section else None,
                "playlist": playlist.fingerprint, "count": len(playlist.segments)}
        existing: dict[int, Path] = {}
        if resume.has_prior(self.work):
            state = resume.load(self.work)
            reason = None
            if state is None or not self.seg_dir.is_dir():
                reason = "no_state"
            elif any(state.get(k) != v for k, v in plan.items()):
                reason = "changed"
            else:
                existing = self._existing(playlist)
                reason = self._verify(client, playlist, existing)
            if reason:
                resume.restart(self.work, self.on_event, reason)
                existing = {}
        self.seg_dir.mkdir(mode=0o700, exist_ok=True)
        resume.save(self.work, plan)
        return existing

    def _verify(self, client: httpx.Client, playlist: Playlist, existing: dict[int, Path]) -> str | None:
        """초기화 조각과 마지막으로 받은 조각을 서버의 것과 견준다."""
        try:
            for k, uri in enumerate(playlist.maps):
                path = self.seg_dir / f"map-{k:03d}.mp4"
                if path.exists() and path.read_bytes() != self._fetch_bytes(client, uri):
                    return "mismatch"
            if existing:
                last = max(existing)
                if existing[last].read_bytes() != self._fetch_bytes(client, playlist.segments[last]["uri"]):
                    return "mismatch"
        except ReplayFailure as e:
            if e.code in ("http_401", "http_403", "http_400"):
                raise
            return "mismatch"
        return None

    # ---- 실행 ----
    def run(self, out: str) -> None:
        try:
            if self.auth is not None:
                with self._client() as client:
                    raw = self._fetch_bytes(client, self.url)
                payload = rewrite_plain(raw, self.url, self.check_url)
            else:
                raw = hls_transport._fetch_manifest(self.url, self.headers)
                payload = hls_transport.rewrite_media_playlist(raw, self.url, self.signed, self.host_check)
        except ReplayFailure:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ReplayFailure(classify_exception(exc)) from None
        playlist = Playlist(payload)  # Unsupported면 작업 폴더를 건드리기 전에 나간다
        chosen, offset = playlist.select(self.section)
        total_seconds = sum(playlist.segments[i]["duration"] for i in chosen) or 1.0
        with self._client() as client:
            existing = self._prepare(client, playlist)
            for k, uri in enumerate(playlist.maps):
                path = self.seg_dir / f"map-{k:03d}.mp4"
                if not path.exists():
                    data = self._fetch_bytes(client, uri)
                    tmp = self.seg_dir / f"map-{k:03d}.tmp"
                    tmp.write_bytes(data)
                    os.replace(tmp, path)
            have = {i: existing[i] for i in chosen if i in existing}
            done_seconds = sum(playlist.segments[i]["duration"] for i in have)
            done_bytes = sum(p.stat().st_size for p in have.values())
            if have and self.on_event:
                self.on_event("resume", mode="segments", count=len(have), bytes=done_bytes)
            if have:
                self._estimate(done_bytes, done_seconds, total_seconds)
            last_report = 0.0
            for i in chosen:
                if i in have:
                    continue
                path, size = self._fetch_file(client, playlist.segments[i]["uri"], i, playlist.segments[i]["ext"])
                have[i] = path
                self.fetched += 1
                done_seconds += playlist.segments[i]["duration"]
                done_bytes += size
                if self.on_progress:
                    self.on_progress(min(99.0, done_seconds * 100.0 / total_seconds))
                now = time.monotonic()
                if now - last_report >= 0.5:
                    last_report = now
                    if self.on_bytes:
                        self.on_bytes(done_bytes, None)
                    self._estimate(done_bytes, done_seconds, total_seconds)
            if self.on_bytes:
                self.on_bytes(done_bytes, None)
        if self.on_stage:
            self.on_stage("finalizing")
        self._remux(playlist, chosen, have, offset, out)

    def _estimate(self, done_bytes: int, done_seconds: float, total_seconds: float) -> None:
        """받은 조각의 초당 크기로 영상 전체 크기를 추정해 알린다(관리자가 두 배를 여유 공간에 넣는다)."""
        if self.on_event and done_seconds > 0 and done_bytes > 0:
            self.on_event("space", mode="hls", estimate=int(done_bytes * total_seconds / done_seconds))

    def local_playlist(self, playlist: Playlist, chosen: list[int], have: dict[int, Path]) -> bytes:
        """받은 조각 파일을 가리키는 재생목록. KEY 주소는 서명된 원래 주소 그대로다(메모리에만 있다)."""
        lines = ["#EXTM3U", *playlist.header, f"#EXT-X-MEDIA-SEQUENCE:{playlist.sequence + chosen[0]}"]
        # 표준 입력으로 읽은 재생목록의 상대 주소는 pipe: 기준이 되므로 `file:` 절대 경로로 적는다
        maps = {uri: "file:" + str(self.seg_dir / f"map-{k:03d}.mp4") for k, uri in enumerate(playlist.maps)}

        def local_tag(tag: str) -> str | None:
            """허용한 태그만 다시 만든다. KEY는 원래 주소, MAP은 받아 둔 파일로(속성은 다시 조립한다)."""
            if tag.startswith("#EXT-X-MAP:"):
                return hls_transport.safe_tag(tag, lambda uri: maps[uri])
            if tag.startswith("#EXT-X-KEY:"):
                return hls_transport.safe_tag(tag, lambda uri: uri)
            return tag if tag.startswith(LOCAL_TAGS) and "\n" not in tag else None

        lines = ["#EXTM3U"] + [t for t in (local_tag(h) for h in playlist.header) if t] + lines[1 + len(playlist.header):]

        first = playlist.segments[chosen[0]]
        carried = []
        if first["key"] and first["key"] not in first["tags"]:
            carried.append(first["key"])
        if first["map"] and first["map"] not in first["tags"]:
            carried.append(first["map"])
        for n, i in enumerate(chosen):
            seg = playlist.segments[i]
            tags = (carried if n == 0 else []) + [t for t in seg["tags"]]
            for tag in tags:
                rebuilt = local_tag(tag)
                if rebuilt:
                    lines.append(rebuilt)
            lines.append("file:" + str(have[i]))
        lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode()

    def _remux(self, playlist: Playlist, chosen: list[int], have: dict[int, Path], offset: float, out: str) -> None:
        payload = self.local_playlist(playlist, chosen, have)
        before = ["-ss", f"{offset:.3f}"] if self.section and offset > 0 else []
        after = ["-t", f"{self.section[1] - self.section[0]:.3f}"] if self.section else []
        protocols = "file,pipe,https,tls,tcp,crypto" + (",http" if hls_transport.ALLOW_LOCAL_HTTP else "")
        picky = ["-extension_picky", "0"] if hls_transport._supports_extension_picky(str(self.ffmpeg)) else []
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *before, "-protocol_whitelist", protocols,
               "-allowed_extensions", "ALL", *picky, "-f", "hls", "-i", "pipe:0", *after,
               "-c", "copy", "-movflags", "+faststart", "-f", "mp4", "-y", out]
        diagnostics: dict[str, str] = {}
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def drain() -> None:
            for chunk in iter(lambda: proc.stderr.read(4096), b""):
                record_diagnostic(diagnostics, chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except OSError:
            pass
        rc = proc.wait()
        reader.join(timeout=5)
        if rc != 0:
            raise ReplayFailure(diagnostics.get("code", "engine_operation_failed"))
        if not Path(out).is_file() or Path(out).stat().st_size < 1024:
            raise ReplayFailure("invalid_media")
