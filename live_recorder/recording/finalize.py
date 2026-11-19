"""마무리: 검증, 컨테이너 변환, 이름 바꾸기.

파일 이름 규칙:
    <최종이름>.part         녹화 중인 원본(수신기가 쓴다)
    <최종이름>.remux.part   변환 결과의 임시 이름
    <최종이름>              완성본
    <줄기>.raw.<ts|mp4>     남겨 둔 원본(변환 실패, 검증 실패, 원본 삭제 꺼짐)

음성을 따로 받은 녹화:
    <최종이름>.audio.part   녹화 중인 음성 원본
    <줄기>.raw.audio.<aac|ts|mp4>  남겨 둔 음성 원본(영상 원본 이름 뒤에 `.audio`)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import lowprio
from ..core import childenv
from ..core import store as st
from ..core.config import AppConfig
from ..core.naming import PART_SUFFIX, audio_part

log = logging.getLogger("live_recorder.finalize")

REMUX_SUFFIX = ".remux" + PART_SUFFIX
FORMATS = {".mp4": "mp4", ".ts": "mpegts"}
PROBE_TIMEOUT = 120
SCAN_TIMEOUT = 6 * 3600
REMUX_TIMEOUT = 12 * 3600
# 원본 검증은 앞부분만 읽는다. 조각 MP4는 색인이 없어 전체를 읽으면 수십 GB에서 몇 시간이 걸린다
# (예: 34GB 원본의 -show_format이 3분에 0.9GB를 읽었다). 영상 스트림이 읽히는지만 본다.
HEAD_BYTES = 64 * 1024 * 1024
GIB = 1024 ** 3
SCAN_PER_GIB = 600.0  # 전체를 읽는 길이 측정. 실측 속도(약 200초/GB)의 세 배쯤
REMUX_PER_GIB = 600.0
RESULT_PROBE_PER_GIB = 60.0  # 결과 파일은 색인(faststart mp4)이나 앞뒤 시각(MPEG-TS)으로 길이를 읽는다


def scaled_timeout(size: int, base: float, per_gib: float) -> float:
    """파일 크기에 비례한 시간 제한. 고정값(base)보다 짧아지지는 않는다."""
    return max(base, base + per_gib * (max(0, size) / GIB))


@dataclass
class Probe:
    ok: bool
    has_video: bool = False
    duration: float | None = None
    format_name: str = ""
    detail: str = ""
    has_audio: bool = False
    video_duration: float | None = None
    audio_duration: float | None = None


def within_tolerance(actual: float, expected: float) -> bool:
    """2초 또는 1% 가운데 큰 쪽."""
    return abs(actual - expected) <= max(2.0, expected * 0.01)


def sniff_container(path: Path) -> str:
    """원본이 MPEG-TS인지 (조각난) MP4인지 앞머리로 본다."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return ".ts"
    if len(head) >= 8 and head[4:8] in (b"ftyp", b"styp", b"moov", b"moof", b"sidx"):
        return ".mp4"
    return ".ts"


def sniff_audio(path: Path) -> str:
    """따로 받은 음성 원본의 확장자. ID3 머리말이나 ADTS 동기 비트면 AAC, 아니면 컨테이너를 본다."""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return ".aac"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xF0 == 0xF0):
        return ".aac"
    return sniff_container(path)


AV_GAP_WARN = 2.0  # 영상과 음성 길이가 이보다 더 다르면 기록에 경고를 남긴다


def raw_audio_of(raw: Path) -> Path | None:
    """남겨 둔 영상 원본(`<줄기>.raw.ts`) 옆의 음성 원본(`<줄기>.raw.audio.*`)."""
    stem = os.path.splitext(raw.name)[0]
    try:
        for cand in sorted(raw.parent.iterdir()):
            name = cand.name
            if name.startswith(stem + ".audio.") and cand.is_file() and cand.stat().st_size > 0:
                if os.path.splitext(name)[0] == stem + ".audio":
                    return cand
    except OSError:
        return None
    return None


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _link_exclusive(src: Path, dst: Path) -> None:
    """dst가 이미 있으면 실패한다. 덮어쓰는 경로를 만들지 않는다."""
    os.link(src, dst)
    os.unlink(src)


def free_name(path: Path) -> Path:
    if not path.exists():
        return path
    stem, ext = os.path.splitext(path.name)
    n = 1
    while True:
        cand = path.with_name(f"{stem} ({n}){ext}")
        if not cand.exists():
            return cand
        n += 1


RAW_MARK = ".raw"


def retry_source(session: dict) -> Path | None:
    """마무리 실패로 남은 원본(`<줄기>.raw.<ts|mp4>`)이 그대로 있으면 그 경로."""
    if session.get("finalize_status") != "failed" or not session.get("output_path"):
        return None
    path = Path(session["output_path"])
    stem, ext = os.path.splitext(path.name)
    if ext.lower() not in FORMATS or not (stem.endswith(RAW_MARK) or RAW_MARK + " (" in stem):
        return None
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return None
    except OSError:
        return None
    return path


class Finalizer:
    def __init__(self, cfg: AppConfig, store: st.Store, notifier=None, events=None):
        self.cfg = cfg
        self.events = events  # 없으면(명령줄의 다시 마무리) 알리지 않는다
        self.store = store
        self.notifier = notifier
        self._procs: set[asyncio.subprocess.Process] = set()
        self._sem: asyncio.Semaphore | None = None
        self._sem_size = 0
        self._channel_locks: dict[str, asyncio.Lock] = {}
        self.closing = False

    # ---- 외부 도구 ----
    async def _run(self, args: list[str], timeout: float, *, line_sink=None, low_priority: bool = False,
                   feed: tuple[Path, int] | None = None) -> tuple[int, str, str]:
        """인자 목록으로 실행한다. 셸을 거치지 않는다. low_priority면 낮은 우선순위 실행기를 앞에 붙인다.
        feed=(파일, 바이트 수)면 그 파일의 앞부분만 표준 입력으로 넣는다."""
        if low_priority:
            args = lowprio.wrap(args)
        proc = await asyncio.create_subprocess_exec(
            *args,
            env=childenv.child_env(),  # 필요한 변수만 넘긴다
            stdin=asyncio.subprocess.PIPE if feed else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._procs.add(proc)
        feeder: asyncio.Task | None = None
        if feed:
            feeder = asyncio.ensure_future(self._feed(proc, *feed))
        try:
            async def pump() -> tuple[str, str]:
                if line_sink is None and feeder is None:
                    out, err = await proc.communicate()
                    return out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
                if line_sink is None:
                    # communicate()는 표준 입력을 바로 닫으므로 넣는 중에는 쓰지 않는다
                    assert proc.stdout and proc.stderr
                    out_b, err_b = await asyncio.gather(proc.stdout.read(), proc.stderr.read())
                    await proc.wait()
                    return out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace")
                assert proc.stdout and proc.stderr
                err_task = asyncio.ensure_future(proc.stderr.read())
                async for raw in proc.stdout:
                    line_sink(raw.decode("utf-8", "replace"))
                err = await err_task
                await proc.wait()
                return "", err.decode("utf-8", "replace")

            try:
                out, err = await asyncio.wait_for(pump(), timeout)
            except asyncio.TimeoutError:
                self._kill(proc)
                await proc.wait()
                return -1, "", "timeout"
            if low_priority:
                kept = []
                for line in err.splitlines(keepends=True):
                    if line.startswith(lowprio.WARNING_PREFIX):
                        log.warning("변환 우선순위를 낮추지 못했다. 그대로 변환한다: %s",
                                    line[len(lowprio.WARNING_PREFIX):].strip()[:200], extra={"event": "lowprio_failed"})
                    else:
                        kept.append(line)
                err = "".join(kept)
            return proc.returncode if proc.returncode is not None else -1, out, err
        except asyncio.CancelledError:
            self._kill(proc)
            raise
        finally:
            if feeder is not None:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
            self._procs.discard(proc)

    @staticmethod
    async def _feed(proc: asyncio.subprocess.Process, path: Path, limit: int) -> None:
        """파일 앞부분을 표준 입력으로 넣고 닫는다. 도구가 먼저 끝나 파이프가 닫혀도 괜찮다."""
        assert proc.stdin
        left = limit
        try:
            with open(path, "rb") as f:
                while left > 0:
                    chunk = await asyncio.to_thread(f.read, min(1024 * 1024, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def kill_all(self) -> None:
        self.closing = True
        for proc in list(self._procs):
            self._kill(proc)

    async def probe(self, path: Path, *, head: bool = False, timeout: float = PROBE_TIMEOUT) -> Probe:
        """영상 스트림과 길이가 있는지 본다.

        head면 파일 앞부분(HEAD_BYTES)만 표준 입력으로 넣어 영상 스트림이 읽히는지만 본다.
        이때 길이는 알 수 없을 수 있다. 원본 길이는 수신기 보고나 scan_duration으로 구한다."""
        source = "pipe:0" if head else str(path)
        rc, out, err = await self._run(
            [self.cfg.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-i", source],
            timeout, feed=(path, HEAD_BYTES) if head else None,
        )
        if rc != 0:
            return Probe(False, detail=f"ffprobe rc={rc}: {err.strip()[-200:]}")
        try:
            data = json.loads(out)
        except ValueError:
            return Probe(False, detail="ffprobe 출력이 JSON이 아닙니다")
        streams = data.get("streams") or []
        has_video = any(s.get("codec_type") == "video" for s in streams)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        fmt = data.get("format") or {}

        def stream_duration(kind: str) -> float | None:
            for s in streams:
                if s.get("codec_type") != kind:
                    continue
                try:
                    value = float(s.get("duration"))
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    return value
            return None
        duration = None
        for cand in [fmt.get("duration")] + [s.get("duration") for s in streams if s.get("codec_type") == "video"]:
            try:
                value = float(cand)
            except (TypeError, ValueError):
                continue
            if value > 0:
                duration = value
                break
        return Probe(True, has_video, duration, str(fmt.get("format_name") or ""), has_audio=has_audio,
                     video_duration=stream_duration("video"), audio_duration=stream_duration("audio"))

    def _low(self) -> bool:
        try:
            return self.store.get_settings().finalize_low_priority
        except Exception:  # noqa: BLE001
            return True

    async def scan_duration(self, path: Path, size: int | None = None) -> float | None:
        """영상 패킷을 끝까지 읽어 실제 길이를 잰다. 조각난 MP4처럼 머리말에 길이가 없는 원본에 쓴다."""
        lo: float | None = None
        hi: float | None = None

        def sink(line: str) -> None:
            nonlocal lo, hi
            parts = line.strip().split(",")
            if len(parts) < 2:
                return
            try:
                pts = float(parts[0])
            except ValueError:
                return
            try:
                dur = float(parts[1])
            except ValueError:
                dur = 0.0
            if lo is None or pts < lo:
                lo = pts
            if hi is None or pts + dur > hi:
                hi = pts + dur

        rc, _, err = await self._run(
            [self.cfg.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,duration_time",
             "-of", "csv=p=0", "-i", str(path)],
            max(SCAN_TIMEOUT, scaled_timeout(size if size is not None else _size(path), 3600.0, SCAN_PER_GIB)),
            line_sink=sink,
            low_priority=await asyncio.to_thread(self._low),
        )
        if lo is None or hi is None:
            log.warning("길이 측정 실패 rc=%s %s", rc, err.strip()[-200:])
            return None
        return hi - lo

    # ---- 동시 실행 제한 ----
    def _semaphore(self, size: int) -> asyncio.Semaphore:
        if self._sem is None or self._sem_size != size:
            self._sem = asyncio.Semaphore(size)
            self._sem_size = size
        return self._sem

    async def finalize_session(self, session_id: int, *, retry: bool = False) -> str:
        """세션 하나를 마무리한다. 결과 상태('done'/'failed'/'skipped'/'pending')를 돌려준다.

        retry면 마무리 실패로 남은 원본(`.raw.*`)을 다시 마무리한다. 다시 실패하면 원본을 그대로 둔다."""
        session = await asyncio.to_thread(self.store.get_session, session_id)
        if session is None:
            return "skipped"
        if retry:
            raw = retry_source(session)
            if raw is None:
                return "skipped"
            await asyncio.to_thread(self.store.update_session, session_id,
                                    finalize_retries=int(session.get("finalize_retries") or 0) + 1)
        channel_id = session["channel_id"]
        platform_id = session.get("platform")
        settings = await asyncio.to_thread(self.store.get_settings)
        lock = self._channel_locks.setdefault((platform_id, channel_id), asyncio.Lock())
        sem = self._semaphore(settings.finalize_concurrency)
        async with lock, sem:
            if self.closing:
                return "pending"
            await asyncio.to_thread(self.store.add_finalizing, channel_id, 1, platform_id)
            await asyncio.to_thread(self.store.update_session, session_id, finalize_status="running")
            if self.events:
                self.events.emit("finalize_started", platform_id, channel_id, session_id=session_id)
            try:
                if retry:
                    status, detail, output = await self._refinalize(session, settings)
                else:
                    status, detail, output = await self._finalize(session, settings)
            except asyncio.CancelledError:
                # 다시 마무리는 원본이 `.raw.*` 그대로이므로 실패 기록을 되돌려 둔다
                back = dict(finalize_status="failed") if retry else dict(finalize_status="pending")
                await asyncio.shield(asyncio.to_thread(self.store.update_session, session_id, **back))
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("마무리 중 예외 session=%s", session_id)
                status, detail, output = "failed", f"exception: {type(e).__name__}: {e}", None
            finally:
                await asyncio.shield(asyncio.to_thread(self.store.add_finalizing, channel_id, -1, platform_id))
            if self.closing and status == "failed":
                # 서버 종료로 중단. 처음 마무리면 `.part`가 남아 다음 기동에 처리된다.
                # 다시 마무리면 원본은 `.raw.*` 그대로이고 실패로 남긴다(다음 기동에 다시 시도하지 않는다)
                status, detail = ("failed", "다시 마무리 중 서버 종료. 원본은 그대로입니다") if retry else ("pending", "서버 종료로 중단")
            if retry and status in ("pending", "failed") and output is None:
                status, output = "failed", raw  # 원본 위치를 기록에 남긴다(다시 마무리할 수 있게)
                detail = detail or "다시 마무리 실패"
            await asyncio.to_thread(self.store.update_session, session_id, finalize_status=status,
                                    finalize_detail=detail, output_path=str(output) if output else None)
            if self.notifier and status in ("done", "failed"):
                ch = await asyncio.to_thread(self.store.get_channel, channel_id, platform_id)
                self.notifier.notify(settings, "finalize_done" if status == "done" else "finalize_failed", channel_id,
                                     ch.name if ch else channel_id, "" if status == "done" else (detail or ""),
                                     Path(output).name if output else "")
            level = logging.INFO if status in ("done", "skipped") else logging.ERROR
            log.log(level, "마무리 %s session=%s %s", status, session_id, detail,
                    extra={"event": "finalize", "session_id": session_id, "channel_id": channel_id, "platform": platform_id,
                           "status": status})
            if self.events:
                self.events.emit("finalize_ended", platform_id, channel_id, session_id=session_id, status=status,
                                 path=str(output) if output else None)
            return status

    async def _finalize(self, session: dict, settings) -> tuple[str, str, Path | None]:
        part = Path(session["part_path"]) if session.get("part_path") else None
        final = Path(session["final_path"]) if session.get("final_path") else None
        if part is None or final is None:
            return "skipped", "파일 없음", None
        if not part.exists():
            if final.exists():
                return "done", "이미 완성본이 있습니다", final
            return "skipped", "원본 파일이 없습니다", None
        audio = audio_part(part)
        return await self.finalize_file(part, final, settings, expected=self._expected(session),
                                        audio=audio if _size(audio) > 0 else None)

    async def _refinalize(self, session: dict, settings) -> tuple[str, str, Path | None]:
        """남은 원본에서 다시 마무리한다. 실패하면 원본은 같은 이름으로 그대로 남는다."""
        raw = retry_source(session)
        final = Path(session["final_path"]) if session.get("final_path") else None
        if raw is None or final is None:
            return "failed", "다시 마무리할 원본이 없습니다", None
        status, detail, out = await self.finalize_file(raw, final, settings, expected=self._expected(session),
                                                       source_is_raw=True,
                                                       audio=await asyncio.to_thread(raw_audio_of, raw))
        return status, "다시 마무리: " + detail, out

    @staticmethod
    def _expected(session: dict) -> float | None:
        """수신기가 정상 종료하며 보고한 길이만 믿는다."""
        if not session.get("clean_exit"):
            return None
        first, last = session.get("first_media_time"), session.get("last_media_time")
        if first and last:
            try:
                span = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds()
            except ValueError:
                span = 0
            if span > 0:
                return span
        return session.get("media_seconds") or None

    async def finalize_file(self, part: Path, final: Path, settings, expected: float | None = None,
                            source_is_raw: bool = False, audio: Path | None = None) -> tuple[str, str, Path | None]:
        """source_is_raw면 part는 이미 남겨 둔 원본(`.raw.*`)이다. 실패해도 이름을 바꾸지 않는다.
        audio가 있으면 따로 받은 음성 원본이다. 다시 인코딩하지 않고 영상과 합친다."""
        size = part.stat().st_size
        if size == 0:
            if source_is_raw:
                return "failed", "원본이 빈 파일입니다", part
            part.unlink()
            if audio is not None:
                audio.unlink(missing_ok=True)
            return "skipped", "빈 파일을 지웠습니다", None
        stem = os.path.splitext(final.name)[0]
        raw_ext = await asyncio.to_thread(sniff_container, part)
        audio_ext = await asyncio.to_thread(sniff_audio, audio) if audio is not None else ""
        kept_audio: list[Path] = []

        def keep_raw() -> Path:
            """원본을 `.part`에서 빼서 남긴다. 다음 기동 때 다시 마무리 대상이 되지 않는다."""
            if source_is_raw:
                return part
            dst = free_name(part.with_name(f"{stem}.raw{raw_ext}"))
            _link_exclusive(part, dst)
            if audio is not None and audio.exists():
                a_dst = free_name(part.with_name(f"{os.path.splitext(dst.name)[0]}.audio{audio_ext}"))
                _link_exclusive(audio, a_dst)
                kept_audio.append(a_dst)
            return dst

        def kept_text(kept: Path) -> str:
            return kept.name + (f", {kept_audio[0].name}" if kept_audio else "")

        src = await self.probe(part, head=True)  # 앞부분만
        if not src.ok or not src.has_video:
            kept = await asyncio.to_thread(keep_raw)
            return "failed", f"원본 검증 실패({src.detail or '영상 스트림 없음'}). 원본 보존: {kept_text(kept)}", kept
        if audio is not None:
            a_src = await self.probe(audio, head=True)
            if not a_src.ok or not a_src.has_audio:
                kept = await asyncio.to_thread(keep_raw)
                return "failed", f"음성 원본 검증 실패({a_src.detail or '음성 스트림 없음'}). 원본 보존: {kept_text(kept)}", kept

        target_ext = final.suffix.lower()
        if not settings.remux_enabled and audio is None:
            # 변환을 끄면 원본 그대로 둔다. 확장자는 실제 컨테이너를 따른다.
            dst = free_name(final if target_ext == raw_ext else final.with_suffix(raw_ext))
            await asyncio.to_thread(_link_exclusive, part, dst)
            return "done", "변환 없이 이름만 바꿨습니다", dst

        tmp = Path(str(final) + REMUX_SUFFIX)
        if tmp.exists():
            tmp.unlink()
        if audio is None:
            args = [self.cfg.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
                    "-fflags", "+genpts", "-i", str(part),
                    "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero"]
        else:
            # 변환을 꺼 두었어도 음성을 붙이려면 합쳐야 한다. 다시 인코딩하지 않는다
            args = [self.cfg.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
                    "-fflags", "+genpts", "-i", str(part), "-fflags", "+genpts", "-i", str(audio),
                    "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-avoid_negative_ts", "make_zero"]
            size += _size(audio)
        if target_ext == ".mp4":
            args += ["-movflags", "+faststart"]
        args += ["-f", FORMATS.get(target_ext, "mp4"), str(tmp)]
        remux_limit = max(REMUX_TIMEOUT, scaled_timeout(size, 3600.0, REMUX_PER_GIB))
        rc, _, err = await self._run(args, remux_limit, low_priority=settings.finalize_low_priority)

        def fail(reason: str) -> tuple[str, str, Path | None]:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            if self.closing:
                return "pending", "서버 종료로 중단", None  # 원본은 `.part`로 남아 다음 기동에 처리된다
            kept = keep_raw()
            return "failed", f"{reason}. 원본 보존: {kept_text(kept)}", kept

        if rc != 0:
            return await asyncio.to_thread(fail, f"변환 실패 rc={rc}: {err.strip()[-200:]}")
        if not tmp.exists():
            return await asyncio.to_thread(fail, "변환 결과 파일이 없습니다")
        out = await self.probe(tmp, timeout=scaled_timeout(size, PROBE_TIMEOUT, RESULT_PROBE_PER_GIB))
        if not out.ok or not out.has_video or not out.duration:
            return await asyncio.to_thread(fail, f"변환 결과 검증 실패({out.detail or '영상·길이 없음'})")
        if audio is not None and not out.has_audio:
            return await asyncio.to_thread(fail, "변환 결과 검증 실패(음성 스트림 없음)")
        ok = expected is not None and within_tolerance(out.duration, expected)
        measured = None
        if not ok:
            measured = await self.scan_duration(part, size)
            if measured is None:
                measured = src.duration
            ok = measured is not None and within_tolerance(out.duration, measured)
        if not ok:
            want = measured if measured is not None else expected
            return await asyncio.to_thread(
                fail, f"길이 불일치: 결과 {out.duration:.1f}초, 원본 {want:.1f}초" if want is not None
                else "원본 길이를 잴 수 없습니다")

        warn = ""
        if audio is not None and out.video_duration and out.audio_duration \
                and abs(out.video_duration - out.audio_duration) > AV_GAP_WARN:
            # 길이가 다르면 마무리 기록에 경고를 남긴다(실패로 보지는 않는다)
            warn = f". 경고: 영상 {out.video_duration:.1f}초와 음성 {out.audio_duration:.1f}초의 길이가 다릅니다"
            log.warning("영상과 음성 길이가 다르다: 영상 %.1f초, 음성 %.1f초", out.video_duration, out.audio_duration,
                        extra={"event": "av_length_mismatch"})
        merged = ", 음성 합침" if audio is not None else ""
        dst = free_name(final)
        await asyncio.to_thread(_link_exclusive, tmp, dst)
        if settings.delete_original:
            await asyncio.to_thread(part.unlink)
            if audio is not None:
                await asyncio.to_thread(audio.unlink, True)
            return "done", f"변환 완료 {out.duration:.1f}초{merged}, 원본 삭제{warn}", dst
        kept = await asyncio.to_thread(keep_raw)
        return "done", f"변환 완료 {out.duration:.1f}초{merged}, 원본 보존: {kept_text(kept)}{warn}", dst
