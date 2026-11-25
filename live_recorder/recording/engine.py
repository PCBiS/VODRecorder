"""녹화 코어: 채널 감시, 수신기 실행, 재접속, 종료 처리.

채널마다 작업자(ChannelWorker) 하나가 돈다. 상태는 다섯 가지다.
"""

from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import json
import logging
import os
import shutil
import signal
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..core import childenv
from ..core import gate as gate_mod
from ..core import naming
from ..core import codec as codec_mod
from ..core import quality
from ..core import store as st
from ..core.config import AppConfig, Settings
from ..core.events import EventBus
from ..platforms import credentials, registry
from ..platforms.base import (ApiError, clean_text, ChannelLookupNeeded, ChannelParseError, LiveInfo, Platform, PlatformClient,
                              TimeMachineUnavailable, Verdict, host_allowed)
from .finalize import REMUX_SUFFIX, Finalizer, retry_source
from ..core.naming import AUDIO_PART_SUFFIX, KST, PART_SUFFIX, audio_part
from ..notify import Notifier
from .receiver import EXIT_UNSUPPORTED

log = logging.getLogger("live_recorder.engine")
rlog = logging.getLogger("live_recorder.receiver")


class StartupError(RuntimeError):
    pass


class AlreadyRunning(StartupError):
    pass


@dataclass
class Timings:
    """시간 상수. 시험에서 줄여 쓴다."""

    backoff_steps: tuple[float, ...] = (5.0, 15.0, 60.0)  # 그 뒤는 확인 주기
    max_same_failures: int = 5  # 넘으면 오류 표시
    stop_grace: float = 10.0  # 종료 신호 뒤 기다리는 시간
    post_end_suppress: float = 60.0
    unknown_recheck_interval: float = 10.0  # 판정 불가면 주기를 다 기다리지 않고 다시 본다
    unknown_recheck_max: int = 3
    meta_retry_interval: float = 1.0
    meta_retry_max: int = 5
    control_poll: float = 1.0
    idle_poll: float = 1.0
    monitor_poll: float = 1.0
    progress_save: float = 10.0
    shutdown_deadline: float = 25.0
    # 수신기에 넘기는 값
    segment_timeout: float = 15.0
    segment_attempts: int = 12
    stream_timeout: float = 60.0
    refresh_attempts: int = 3
    refresh_delay: float = 3.0
    reload_interval: float | None = None
    cookie_check_interval: float = 24 * 3600.0  # 하루 한 번
    cookie_check_min_gap: float = 600.0  # 성인 방송 실패로 인한 확인은 10분에 한 번까지
    # 시험용. 설정 검증의 하한(확인 주기 10초, 정체 5초)보다 짧게 돌릴 때만 쓴다
    check_interval_override: float | None = None
    stall_override: float | None = None
    endlist_wait_override: float | None = None  # 설정값 대신 쓸 대기(초)
    # 플랫폼이 확인을 막으면(봇 확인 요구, HTTP 429) 그 채널의 확인을 이만큼 늦춘다(마지막 값이 상한)
    throttle_steps: tuple[float, ...] = (300.0, 600.0, 1200.0)
    # 시험용. 플랫폼의 최소 확인 간격 대신 쓸 값. None이면 플랫폼 값을 따른다
    min_interval_override: float | None = None
    # 시험용. 채널 사이 간격 대신 쓸 값과 앞줄 간격의 하한(원래 1초)
    gate_interval_override: float | None = None
    gate_front_floor: float = 1.0
    endlist_grace: float = 300.0  # 5분. 종료 표시 뒤 이 시간이 지나도 같은 방송이 빈 채로 열리면 시작 실패로 센다


class InstanceLock:
    """같은 데이터 폴더로 두 인스턴스가 뜨지 못하게 한다."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise AlreadyRunning(f"이미 실행 중인 인스턴스가 있습니다: {self.path}") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    @staticmethod
    def held(path: Path) -> bool:
        """다른 프로세스가 잠그고 있는지 본다(CLI용)."""
        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)


def jitter_factor(channel_id: str) -> float:
    """채널마다 고정된 ±15% 어긋남."""
    return 1.0 + ((zlib.crc32(channel_id.encode()) % 3001) / 10000.0 - 0.15)


@dataclass
class RecResult:
    kind: str  # ended | disconnected | start_failed | stalled | user_stop | shutdown
    got_data: bool = False
    last_media_time: str | None = None
    session_id: int | None = None
    detail: str = ""
    file: str = ""
    seconds: float = 0.0
    exit_code: int | None = None
    codec: str = ""  # 이 파일에 받은 영상 코덱
    next_codec: str = ""  # 코덱이 바뀌어 끝났을 때 새 코덱


class ChannelWorker:
    def __init__(self, engine: "Engine", platform_id: str, channel_id: str):
        self.engine = engine
        self.platform_id = platform_id  # 채널은 (플랫폼, 채널 ID)로 식별한다
        self.platform: Platform = registry.get(platform_id)
        self.channel_id = channel_id
        self.wake = asyncio.Event()
        self.manual_once = False
        self.stop_requested = False
        self.remove_requested = False
        self.recording = False
        self.in_broadcast = False
        self.task: asyncio.Task | None = None
        # 마지막으로 본 방송의 썸네일 주소와 성인 여부(화면용. 녹화 판단에는 쓰지 않는다)
        self.live_image: str | None = None
        self.live_adult = False
        # 종료 표시로 끝난 방송: 방송 ID(live_id) → (처음 종료 표시를 본 시각(monotonic), 마지막 조각 시각).
        # 메모리에만 둔다. 서버를 다시 띄우면 잊는다(그때는 시작 실패로 센다)
        self.endlist_seen: dict[str, tuple[float, str | None]] = {}
        self.gate_flagged = False  # 이 채널 상태에 창구 멈춤을 보였다
        self.credential_problem: str | None = None  # 지금 알린 자격 문제("needed" | "rejected")
        self.disk_low = False  # 이 채널 상태에 디스크 부족을 보였다(원인이 풀릴 때 지운다)

    # ---- 도우미 ----
    @property
    def store(self) -> st.Store:
        return self.engine.store

    async def db(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def set_state(self, state: str | None = None, **fields: Any) -> None:
        await self.db(self.store.set_state, self.channel_id, state, platform=self.platform_id, **fields)

    async def check_live(self, rank: int = gate_mod.FRONT):
        """방송 확인. 플랫폼 접점으로 부르되 그 플랫폼의 요청 창구를 거친다.
        감시 확인은 WATCH, 다시 붙기·지금 확인·이어지는 재확인은 FRONT."""
        gate = self.engine.gate_for(self.platform_id)
        creds = await self.engine.load_credentials(self.platform_id)
        async with gate.slot(rank) as ticket:
            result = await self.engine.client_for(self.platform_id).check_live(self.channel_id, creds)
            if result.throttled:
                ticket.blocked()  # 플랫폼 창구 전체를 멈춘다
        return result

    def halted(self) -> bool:
        return self.engine.stopping or self.remove_requested

    async def sleep(self, seconds: float) -> None:
        """깨우면 일찍 돌아오는 잠."""
        if seconds <= 0 or self.halted():
            return
        self.wake.clear()
        try:
            await asyncio.wait_for(self.wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def sleep_watching(self, seconds: float, auto_was: bool) -> None:
        """확인 주기 동안 자되, 자동 녹화 설정이 바뀌거나 조작이 오면 바로 깬다."""
        deadline = time.monotonic() + seconds
        while not self.halted():
            left = deadline - time.monotonic()
            if left <= 0 or self.manual_once:
                return
            await self.sleep(min(left, self.engine.t.idle_poll))
            if self.wake.is_set():
                return
            ch = await self.db(self.store.get_channel, self.channel_id, self.platform_id)
            if ch is None or ch.auto_record != auto_was:
                return

    def interval(self, settings: Settings) -> float:
        """그 플랫폼의 스캔 주기와 `감시 중인 채널 수 × 채널 사이 간격` 가운데 큰 값(바퀴가 겹치지 않는다).
        채널별 어긋남(±15%)은 창구가 대신하므로 없다. 플랫폼의 최소 확인 간격은 강제하지 않는다."""
        scan = self.engine.t.check_interval_override or self.engine.gate_values(self.platform_id)["scan"]
        return max(scan, self.engine.cycle_of(self.platform_id))

    async def note_gate(self, result, settings: Settings) -> None:
        """플랫폼 창구가 멈춰 있으면 이 채널의 상태에도 보인다. 풀리면 되돌린다."""
        gate = self.engine.gate_for(self.platform_id)
        if gate.paused():
            msg = await self.engine.pause_message(self.platform_id, getattr(result, "detail", "") if result else "")
            await self.set_state(st.BACKOFF, error=msg, error_flag=0)
            self.gate_flagged = True
        elif self.gate_flagged:
            self.gate_flagged = False
            await self.set_state(None, error=None, error_flag=0)

    async def clear_error(self, settings: Settings | None = None) -> None:
        """채널의 오류 표시는 지금 상태다. 정상 확인(방송 없음)이나 녹화 시작(첫 데이터) 때 앞선 실패의 표시를 지운다.
        지난 실패는 세션 기록과 로그에 남는다. 지금도 유효한 문제(창구 멈춤, 자격 문제, 디스크 부족)가 있으면 지우지 않는다.
        올라올 때 저장소에 남아 있던 낡은 표시도 여기서 지워진다(메모리의 표시 상태와 무관하게 저장값을 본다)."""
        if self.gate_flagged or self.credential_problem is not None or self.engine.gate_for(self.platform_id).paused():
            return
        if self.disk_low:
            if settings is None:
                return
            ch = await self.db(self.store.get_channel, self.channel_id, self.platform_id)
            free = await asyncio.to_thread(self.engine.disk_free, Path(ch.output_dir)) if ch is not None else 0
            if free < settings.min_free_bytes:
                return
            self.disk_low = False
        state = await self.db(self.store.get_state, self.channel_id, self.platform_id)
        if state.get("error") or state.get("error_count") or state.get("error_flag"):
            await self.set_state(None, error=None, error_count=0, error_flag=0)

    async def note_credential_problem(self, result, settings: Settings, name: str) -> bool:
        """로그인이 필요한 방송이면 녹화 실패로 세지 않고 채널 상태에 보이고 한 번 알린다."""
        problem = result.credential_problem
        if problem is None:
            if self.credential_problem is not None:
                self.credential_problem = None
                await self.set_state(None, error=None, error_flag=0)
            return False
        msg = "쿠키가 필요합니다" if problem == "needed" else "쿠키가 만료됐거나 권한이 없습니다"
        await self.set_state(st.WATCHING, error=msg, error_flag=1)
        if self.credential_problem != problem:
            self.credential_problem = problem
            log.warning("로그인이 필요한 방송: %s", problem,
                        extra={"event": "credential_needed", "channel_id": self.channel_id, "platform": self.platform_id,
                               "reason": problem})
            self.notify(settings, "start_failed" if problem == "needed" else "cookie_expired", name, msg)
        return True

    def backoff(self, fails: int, settings: Settings) -> float:
        steps = self.engine.t.backoff_steps
        if fails <= len(steps):
            return steps[max(0, fails - 1)]
        return self.interval(settings)

    def notify(self, settings: Settings, event: str, name: str, reason: str = "", file: str = "") -> None:
        """대기열에 넣기만 한다."""
        self.engine.notifier.notify(settings, event, self.channel_id, name, reason, file)

    async def note_failure(self, fails: int, message: str, settings: Settings | None = None, name: str = "") -> None:
        flag = 1 if fails >= self.engine.t.max_same_failures else 0
        await self.set_state(st.BACKOFF, error=message, error_count=fails, error_flag=flag)
        if settings is not None and fails == self.engine.t.max_same_failures:
            self.notify(settings, "abnormal_end", name, f"연속 실패 {fails}회: {message}")
        log.warning("실패 %d회: %s", fails, message,
                    extra={"event": "failure", "channel_id": self.channel_id, "platform": self.platform_id, "fails": fails, "error_flag": flag})

    # ---- 감시 루프 ----
    async def run(self) -> None:
        fails = 0
        unknown_streak = 0
        while not self.halted():
            try:
                ch = await self.db(self.store.get_channel, self.channel_id, self.platform_id)
                if ch is None:
                    return
                settings = await self.db(self.store.get_settings)
                if not await asyncio.to_thread(self.engine.platform_on, self.platform_id):
                    # 꺼진 플랫폼의 채널은 감시하지 않는다(채널은 남는다). 다시 켜면 이어서 감시한다
                    self.manual_once = False
                    await self.set_state(st.STOPPED, session_id=None)
                    await self.sleep(self.engine.t.idle_poll)
                    continue
                if not ch.auto_record and not self.manual_once:
                    await self.set_state(st.STOPPED, session_id=None)
                    await self.sleep(self.engine.t.idle_poll)  # 다시 켜면 곧 알아챈다
                    continue
                manual, self.manual_once = self.manual_once, False
                await self.set_state(st.WATCHING, session_id=None)
                gate = self.engine.gate_for(self.platform_id)
                if gate.paused():
                    await self.note_gate(None, settings)  # 멈춘 동안 기다린다는 것을 보인다
                    if not manual and gate.pause_left() > 0:
                        # 창구 안에서 기다리지 않고 여기서 기다린다("지금 확인"·설정 변경에 바로 깬다)
                        self.manual_once = manual
                        await self.sleep_watching(gate.pause_left(), ch.auto_record)
                        continue
                # "지금 확인"과 판정 불가 뒤 빠른 재확인은 앞줄, 나머지는 감시 줄
                rank = gate_mod.FRONT if manual or unknown_streak else gate_mod.WATCH
                result = await self.check_live(rank)
                await self.set_state(None, last_check_at=st.now_iso(),
                                     last_check=f"{result.verdict.value} {result.detail}".strip())
                await self.note_gate(result, settings)
                blocked = await self.note_credential_problem(result, settings, ch.name)
                if result.verdict is Verdict.LIVE and result.info:
                    self.live_image, self.live_adult = result.info.image_url or None, result.info.adult
                elif result.verdict is Verdict.OFFLINE:
                    self.live_image, self.live_adult = None, False
                    if not blocked and not result.throttled:
                        await self.clear_error(settings)  # 방송 없음으로 정상 확인됐다
                if result.verdict is Verdict.LIVE and result.info:
                    info = result.info
                    state = await self.db(self.store.get_state, self.channel_id, self.platform_id)
                    skip = state.get("skip_live_id")
                    if skip and skip != info.live_id:
                        await self.set_state(None, skip_live_id=None)  # 새 방송이다
                        skip = None
                    suppressed = self.engine.suppress.get(info.live_id, 0) > time.monotonic()
                    if manual or (not skip and not suppressed):
                        self.stop_requested = False
                        self.in_broadcast = True
                        try:
                            await self.record_broadcast(ch, info, manual=manual)
                        finally:
                            self.in_broadcast = False
                            self.recording = False
                        fails = 0
                        if self.halted():
                            break
                        await self.sleep(min(1.0, self.engine.t.idle_poll))
                        continue
                fails = 0
                wait = self.interval(settings)
                if result.throttled:
                    unknown_streak = 0  # 창구가 멈췄다. 멈춤이 풀리면 창구가 하나씩 찔러 본다
                elif blocked:
                    unknown_streak = 0  # 다시 봐도 같은 답이다. 확인 주기대로 본다
                elif result.verdict is Verdict.UNKNOWN and unknown_streak < self.engine.t.unknown_recheck_max:
                    unknown_streak += 1
                    wait = min(wait, self.engine.t.unknown_recheck_interval)
                else:
                    unknown_streak = 0
                await self.sleep_watching(wait, ch.auto_record)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 감시가 죽지 않는다
                fails += 1
                log.exception("채널 감시 중 예외", extra={"event": "worker_exception", "channel_id": self.channel_id, "platform": self.platform_id})
                try:
                    settings = await self.db(self.store.get_settings)
                    await self.note_failure(fails, f"예외: {type(e).__name__}: {e}")
                    await self.sleep(self.backoff(fails, settings))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(self.engine.t.backoff_steps[0])

    async def stabilize(self, info: LiveInfo) -> LiveInfo:
        """값이 다 채워지고 재생 정보의 방송 ID(live_id)가 맞을 때까지 짧게 다시 본다."""
        if info.complete():
            return info
        prev = info
        for _ in range(self.engine.t.meta_retry_max):
            await self.sleep(self.engine.t.meta_retry_interval)
            if self.halted():
                break
            res = await self.check_live()
            if res.verdict is not Verdict.LIVE or not res.info or res.info.live_id != info.live_id:
                continue
            cur = res.info
            if cur.complete():
                return cur  # 빈 값은 몇 번 같아도 확정하지 않는다
            prev = cur
        return prev

    async def record_broadcast(self, ch: st.Channel, info: LiveInfo, *, manual: bool) -> None:
        """방송 하나를 끝날 때까지 녹화한다. 끊기면 새 파일로 잇는다."""
        fails = 0
        first_file = True
        resume_after: str | None = None
        resume_exact = False  # 코덱이 바뀌어 나눈 다음 파일은 앞 파일의 마지막 조각 바로 뒤부터 받는다
        prev_codec = ""  # 이 방송에서 앞 파일에 받은 코덱
        announced = ""  # 이미 알린 새 코덱(같은 전환을 두 번 알리지 않는다)
        live_id = info.live_id
        for other in [k for k in self.endlist_seen if k != live_id]:
            del self.endlist_seen[other]  # 방송 ID(live_id)가 바뀌면 잊는다
        if live_id in self.endlist_seen:
            # 종료 표시 뒤 같은 방송을 다시 연다: 되감기 없이 마지막 조각 뒤부터
            first_file, resume_after = False, self.endlist_seen[live_id][1]
        while not self.halted():
            ch_now = await self.db(self.store.get_channel, self.channel_id, self.platform_id)
            if ch_now is None:
                return
            ch = ch_now  # 설정은 시도할 때마다 다시 읽는다
            settings = await self.db(self.store.get_settings)

            # 여유 공간이 모자라면 새 녹화를 시작하지 않는다
            folder = Path(ch.output_dir)
            await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
            free = await asyncio.to_thread(self.engine.disk_free, folder)
            if free < settings.min_free_bytes:
                msg = f"디스크 여유 부족: {free // 1024**2} MiB < {settings.min_free_bytes // 1024**2} MiB"
                self.disk_low = True
                await self.set_state(st.BACKOFF, error=msg, error_flag=1, live_id=live_id, live_title=info.title)
                log.error(msg, extra={"event": "disk_low", "channel_id": self.channel_id, "platform": self.platform_id})
                self.notify(settings, "disk_low", ch.name, msg)
                await self.sleep(self.interval(settings))
                res = await self.check_live()
                if res.verdict is Verdict.OFFLINE or (res.info and res.info.live_id != live_id):
                    return
                if res.info:
                    info = res.info
                continue
            self.disk_low = False  # 여유 공간이 돌아왔다(표시는 녹화가 시작될 때 지운다)

            info = await self.stabilize(info)
            if self.halted():
                return
            result = await self.record_once(
                ch, info, settings,
                rewind=settings.rewind_seconds if first_file else 0,
                resume_after=None if first_file else resume_after,
                after_endlist=self.within_endlist_grace(live_id),
                resume_exact=resume_exact, prev_codec=prev_codec, announced_codec=announced,
            )
            resume_exact = False
            if result.got_data and result.codec:
                prev_codec = result.codec
            announced = ""
            if result.kind == "shutdown":
                return
            if result.kind == "endlist_again":
                # 실패로 세지 않고 알리지 않는다. 같은 방송 ID(live_id)는 유예로 넘긴다
                self.engine.suppress[live_id] = time.monotonic() + self.engine.t.post_end_suppress
                await self.set_state(st.WATCHING, session_id=None)
                return
            if result.kind == "user_stop":
                # 자동 녹화 설정은 그대로 두고, 같은 방송만 건너뛴다
                await self.set_state(st.WATCHING, skip_live_id=live_id, session_id=None)
                return
            if result.got_data:
                first_file = False
                resume_after = result.last_media_time or resume_after
                fails = 0
                await self.clear_error(settings)  # 앞선 실패의 문구까지 지운다(전에는 횟수·표시만 지웠다)
            if not result.got_data:
                fails += 1  # 시작 실패, 또는 한 조각도 못 받고 정체로 끝남
            else:
                self.endlist_seen.pop(live_id, None)  # 조각을 받았으면 잊는다
            if result.kind == "ended" and result.detail == "codec_changed":
                # 방송은 이어지고 있다. 상태를 다시 묻지 않고 곧바로 새 파일로 잇는다
                text = (f"녹화 중 영상 코덱이 바뀌었습니다({codec_mod.label(result.codec)} → {codec_mod.label(result.next_codec)})."
                        " 새 파일로 이어서 녹화합니다")
                log.warning("녹화 중 영상 코덱이 바뀌었다 %s → %s", result.codec or "?", result.next_codec or "?",
                            extra={"event": "codec_changed", "channel_id": self.channel_id, "platform": self.platform_id,
                                   "session_id": result.session_id})
                self.notify(settings, "codec_changed", ch.name, text, result.file)
                announced = result.next_codec
                if result.session_id:
                    await self.db(self.store.update_session, result.session_id, note=text)
                if self.stop_requested:
                    await self.set_state(st.WATCHING, skip_live_id=live_id, session_id=None)
                    return
                resume_exact = True
                continue
            if result.kind == "ended" and result.detail == "endlist":
                self.endlist_seen[live_id] = (time.monotonic(), resume_after)
                # 종료 표시로 끝났으면 상태를 곧바로 다시 보지 않고 기다린다. 중지·종료에는 바로 깬다
                override = self.engine.t.endlist_wait_override
                wait = override if override is not None else float(settings.endlist_wait_seconds)
                if wait > 0:
                    await self.set_state(st.WATCHING, session_id=None)
                    await self.sleep(wait)
                    if self.halted():
                        return
                    if self.stop_requested:
                        await self.set_state(st.WATCHING, skip_live_id=live_id, session_id=None)
                        return

            # 수신기가 끝나면 방송 상태를 다시 본다
            res = await self.check_live()
            if res.verdict is Verdict.OFFLINE or (res.verdict is Verdict.LIVE and res.info and res.info.live_id != live_id):
                if result.kind == "disconnected" and result.session_id:
                    await self.db(self.store.update_session, result.session_id, end_reason=st.END_BROADCAST)
                if result.kind in ("ended", "disconnected"):
                    self.notify(settings, "record_end", ch.name, f"녹화 시간 {_hms(result.seconds)}", result.file)
                self.engine.suppress[live_id] = time.monotonic() + self.engine.t.post_end_suppress
                await self.set_state(st.WATCHING, session_id=None)
                return
            if result.kind == "ended":
                self.notify(settings, "record_end", ch.name, f"녹화 시간 {_hms(result.seconds)}", result.file)
            elif result.kind == "disconnected":
                if result.exit_code == EXIT_UNSUPPORTED:
                    self.notify(settings, "abnormal_end", ch.name, f"지원하지 않는 스트림 형식 ({result.detail})", result.file)
                elif result.session_id:
                    # 끊겼다가 다시 연결하는 것은 알리지 않고 세션 기록에만 남긴다.
                    # 다시 연결하지 못하면 시작 실패·연속 실패 알림이 따로 간다.
                    await self.db(self.store.update_session, result.session_id, note="방송 중 끊김. 새 파일로 다시 연결")
            if res.verdict is Verdict.LIVE and res.info:
                info = res.info
            elif result.got_data:
                fails += 1  # 판정 불가. 조금 기다렸다가 다시 시도한다
            if self.stop_requested:
                await self.set_state(st.WATCHING, skip_live_id=live_id, session_id=None)
                return
            if fails:
                await self.note_failure(fails, result.detail or f"방송 상태 판정 불가({res.detail})", settings, ch.name)
                await self.sleep(self.backoff(fails, settings))
                if self.stop_requested:
                    await self.set_state(st.WATCHING, skip_live_id=live_id, session_id=None)
                    return

    def within_endlist_grace(self, live_id: str) -> bool:
        """이 방송이 종료 표시로 끝난 뒤 정한 시간(5분) 안인가."""
        seen = self.endlist_seen.get(live_id)
        return seen is not None and time.monotonic() - seen[0] < self.engine.t.endlist_grace

    # ---- 녹화 한 번 ----
    async def record_once(self, ch: st.Channel, info: LiveInfo, settings: Settings, *, rewind: int,
                          resume_after: str | None, after_endlist: bool = False, resume_exact: bool = False,
                          prev_codec: str = "", announced_codec: str = "") -> RecResult:
        eng = self.engine
        folder = Path(ch.output_dir)
        track = quality.select(info.tracks, ch.quality, ch.video_codec)
        q_label, fps = quality.label(track, ch.quality)
        now = datetime.now(KST)
        name = naming.render(
            settings.filename_pattern,
            title=info.title or ch.name,  # 끝내 비면 채널 이름
            channel_name=ch.name, quality=q_label, frame_rate=fps, extension=ch.extension,
            broadcast_start=info.started_at or now, recording_start=now, now=now,
            date_shape=settings.filename_date_format, time_shape=settings.filename_time_format,
            platform=self.platform_id,
        )
        final = await asyncio.to_thread(naming.unique_path, folder, name)  # 한 번만 만든다
        part = Path(str(final) + PART_SUFFIX)
        audio = audio_part(part)  # 음성을 따로 받는 방송만 쓴다

        cookie_values = await eng.load_credentials(self.platform_id)
        client = eng.client_for(self.platform_id)
        note: str | None = None
        # 타임머신은 플랫폼이 지원하고 설정이 켜져 있을 때만. 받지 못하면 일반 수신으로 바꾼다
        use_tm = settings.timemachine_enabled and self.platform.features.timemachine
        try:
            pb = await client.playback(self.channel_id, info, cookie_values, timemachine=use_tm)
        except TimeMachineUnavailable as e:
            note = f"타임머신 실패({e}). 일반 수신으로 전환"
            log.warning(note, extra={"event": "timemachine_fallback", "channel_id": self.channel_id, "platform": self.platform_id})
            self.notify(settings, "timemachine_fallback", ch.name, note)
            pb = await client.playback(self.channel_id, info, cookie_values, timemachine=False)
        master, source, refresh = pb.master_url or None, pb.source, pb.refresh
        base_fields = dict(platform=self.platform_id, channel_id=self.channel_id, live_id=info.live_id, title=info.title or ch.name,
                           source=source, note=note, quality_label=f"{q_label}{fps}",
                           time_machine_active=None if info.time_machine_active is None else int(info.time_machine_active))
        allowed = self.platform.allowed_hosts(eng.cfg)
        if not master or not host_allowed(master, allowed, eng.cfg.allow_http):
            detail = "재생 정보가 없습니다" + (" (성인 방송이면 쿠키가 필요합니다)" if info.adult else "")
            sid = await self.db(self.store.create_session, **base_fields, ended_at=st.now_iso(), end_reason=st.END_ERROR,
                                end_detail="start_failed: " + detail, finalize_status="skipped")
            self.notify(settings, "start_failed", ch.name, detail)
            if info.adult:
                eng.request_cookie_check("adult_start_failed", self.platform_id)
            return RecResult("start_failed", session_id=sid, detail=detail)

        job = {
            "output": str(part),
            "audio_output": str(audio),
            "master_url": master,
            "mode": source,
            # 타임머신이 없어도 플랫폼이 일반 재생목록의 창으로 되감기를 밝히면 같은 규칙을 쓴다
            "rewind_seconds": rewind if source == "timemachine" or self.platform.features.live_rewind else 0,
            "start_after": resume_after if (source == "timemachine" or self.platform.features.live_rewind
                                            or self.platform.features.live_resume or resume_exact) else None,
            "want_height": track.height if track else None,
            "want_codec": track.codec if track else "",  # 수신기가 CODECS로 같은 코덱의 변형을 고른다
            "codec_prefer": ch.video_codec,
            "refresh": refresh,
            "refresh_first": bool(self.platform.features.fresh_master),  # 열 때도 새 주소를 받는다
            "platform_module": registry.module_name(self.platform),  # 수신기가 주소 갱신·머리글을 이 모듈에서 얻는다
            "cookies": cookie_values,
            "allowed_hosts": list(allowed),
            "allow_http": eng.cfg.allow_http,
            "segment_timeout": eng.t.segment_timeout,
            "segment_attempts": eng.t.segment_attempts,
            "stream_timeout": eng.t.stream_timeout,
            # 플랫폼이 밝힌 주소 수명이 더 짧으면 그 안에 새로 받는다
            "refresh_after": min(settings.url_refresh_seconds, self.platform.url_refresh_max or settings.url_refresh_seconds),
            "refresh_attempts": eng.t.refresh_attempts,
            "refresh_delay": eng.t.refresh_delay,
            "reload_interval": eng.t.reload_interval,
        }
        env = childenv.child_env(str(Path(__file__).resolve().parent.parent))  # 필요한 변수만 넘긴다
        try:
            # 별도 프로세스 그룹. 인자에는 아무 비밀도 없다.
            proc = await asyncio.create_subprocess_exec(
                *eng.cfg.receiver_cmd,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True, env=env,
            )
        except OSError as e:
            detail = f"수신 도구를 실행할 수 없습니다: {e.strerror or type(e).__name__}"
            sid = await self.db(self.store.create_session, **base_fields, ended_at=st.now_iso(), end_reason=st.END_ERROR,
                                end_detail="start_failed: " + detail, finalize_status="skipped")
            self.notify(settings, "start_failed", ch.name, detail)
            return RecResult("start_failed", session_id=sid, detail=detail)

        # 뜨자마자 시작 시각과 파일 경로를 기록한다
        sid = await self.db(self.store.create_session, **base_fields, pid=proc.pid, part_path=str(part),
                            final_path=str(final))
        self.recording = True
        await self.set_state(st.RECORDING, live_id=info.live_id, live_title=info.title, session_id=sid)
        log.info("녹화 시작 %s", final.name, extra={"event": "record_start", "channel_id": self.channel_id, "platform": self.platform_id,
                                                  "session_id": sid, "source": source, "pid": proc.pid, "rewind": job["rewind_seconds"]})
        self.notify(settings, "record_start", ch.name, f"수신 {'타임머신' if source == 'timemachine' else '일반'}", final.name)
        eng.events.emit("record_started", self.platform_id, self.channel_id, session_id=sid, live_id=info.live_id,
                        source=source, path=str(part))
        try:
            assert proc.stdin
            proc.stdin.write(json.dumps(job).encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

        last: dict[str, Any] = {}
        exit_event: dict[str, Any] = {}
        refreshes = {"n": 0}
        saved_at = 0.0

        async def read_events() -> None:
            nonlocal saved_at
            assert proc.stdout
            async for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                kind = ev.get("event")
                if kind in ("progress", "exit", "refresh"):
                    last.update({k: ev[k] for k in ("bytes", "media_seconds", "first_media_time", "last_media_time") if k in ev})
                if kind == "start":
                    last["first_media_time"] = ev.get("first_media_time")
                    got = str(ev.get("codec") or "")
                    codec_fields: dict[str, Any] = {}
                    if got:  # 실제로 받은 코덱을 기록한다. H.264가 아니면 화질 표시에도 적는다
                        last["codec"] = got
                        codec_fields["video_codec"] = got
                        if got != codec_mod.H264:
                            codec_fields["quality_label"] = f"{q_label}{fps} {codec_mod.label(got)}"
                        if prev_codec and got != prev_codec:
                            # 같은 방송의 앞 파일과 코덱이 다르다. 어떤 까닭으로 새 파일이 됐든(끊김·정체 포함) 남기고 알린다
                            change = f"{codec_mod.label(prev_codec)} → {codec_mod.label(got)}"
                            codec_fields["note"] = f"앞 파일에서 이어짐. 영상 코덱이 바뀌었습니다({change})"
                            if got != announced_codec:
                                log.warning("녹화 중 영상 코덱이 바뀌었다 %s → %s", prev_codec, got,
                                            extra={"event": "codec_changed", "channel_id": self.channel_id,
                                                   "platform": self.platform_id, "session_id": sid})
                                self.notify(settings, "codec_changed", ch.name,
                                            f"녹화 중 영상 코덱이 바뀌었습니다({change}). 새 파일로 이어서 녹화합니다", final.name)
                        if resume_exact and resume_after and ev.get("first_media_time"):
                            # 일반 수신은 재생목록이 짧아(치지직은 조각 3개) 새 파일을 여는 사이에 조각이 지나갈 수 있다
                            try:
                                hole = (datetime.fromisoformat(ev["first_media_time"])
                                        - datetime.fromisoformat(resume_after)).total_seconds()
                            except (TypeError, ValueError):
                                hole = 0.0
                            if hole > 0.5:
                                codec_fields["note"] = (codec_fields.get("note") or "앞 파일에서 이어짐") + f". 앞 파일과 {hole:.1f}초가 비었습니다"
                                log.warning("코덱이 바뀌어 나눈 파일 사이가 %.1f초 비었다", hole,
                                            extra={"event": "codec_change_gap", "channel_id": self.channel_id,
                                                   "platform": self.platform_id, "session_id": sid})
                        want = track.codec if track else ""
                        if want and got != want:
                            log.warning("정한 코덱(%s)과 다른 코덱(%s)을 받는다", want, got,
                                        extra={"event": "codec_differs", "channel_id": self.channel_id,
                                               "platform": self.platform_id, "session_id": sid})
                    await self.db(self.store.update_session, sid, first_media_time=ev.get("first_media_time"), **codec_fields)
                    await self.clear_error(settings)  # 녹화가 시작됐다(첫 데이터)
                elif kind == "exit":
                    exit_event.update(ev)
                elif kind == "refresh":
                    refreshes["n"] += 1  # 주소 갱신 횟수
                    await self.db(self.store.update_session, sid, url_refreshes=refreshes["n"])
                    log.info("재생 주소 갱신 %s ok=%s", ev.get("reason"), ev.get("ok"),
                             extra={"event": "url_refresh", "channel_id": self.channel_id, "platform": self.platform_id, "session_id": sid})
                elif kind == "gap":
                    log.warning("조각 누락 %s개", ev.get("missing"),
                                extra={"event": "segment_gap", "channel_id": self.channel_id, "platform": self.platform_id, "session_id": sid})
                if kind == "progress" and time.monotonic() - saved_at > eng.t.progress_save:
                    saved_at = time.monotonic()
                    await self.db(self.store.update_session, sid, media_seconds=last.get("media_seconds"),
                                  last_media_time=last.get("last_media_time"), bytes=last.get("bytes"))

        async def read_stderr() -> None:
            assert proc.stderr
            async for raw in proc.stderr:
                rlog.info(raw.decode("utf-8", "replace").rstrip()[:500],
                          extra={"channel_id": self.channel_id, "platform": self.platform_id, "session_id": sid})

        readers = [asyncio.create_task(read_events()), asyncio.create_task(read_stderr())]
        waiter = asyncio.create_task(proc.wait())
        reason: str | None = None
        stall_limit = eng.t.stall_override or float(settings.stall_seconds)
        last_size = -1
        last_growth = time.monotonic()
        audio_size = -1
        audio_growth: float | None = None  # 음성 원본이 생긴 뒤에만 잰다
        try:
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=eng.t.monitor_poll)
                if done:
                    break
                size = await asyncio.to_thread(_size, part)
                if size > last_size:
                    last_size, last_growth = size, time.monotonic()
                a_size = await asyncio.to_thread(_size, audio)
                if a_size > 0 and a_size > audio_size:
                    audio_size, audio_growth = a_size, time.monotonic()
                if eng.stopping:
                    reason = "shutdown"
                elif self.stop_requested or self.remove_requested:
                    reason = "user_stop"
                elif time.monotonic() - last_growth > stall_limit:
                    reason = "stalled"
                elif audio_growth is not None and time.monotonic() - audio_growth > stall_limit:
                    reason = "stalled"  # 한쪽만 멈춰도 녹화 전체가 멈춘 것
                if reason:
                    await self._terminate(proc, waiter)
                    break
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate(proc, waiter))
            reason = "shutdown"
        rc = proc.returncode
        try:
            await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), 3)
        except asyncio.TimeoutError:
            for r in readers:
                r.cancel()
        self.recording = False

        size = await asyncio.to_thread(_size, part)
        got_data = size > 0
        size += await asyncio.to_thread(_size, audio)
        tool_reason = exit_event.get("reason")
        if reason == "shutdown":
            kind, end_reason, detail = "shutdown", st.END_SHUTDOWN, "서버 종료"
        elif reason == "user_stop":
            kind, end_reason, detail = "user_stop", st.END_USER, "채널 삭제" if self.remove_requested else "사용자 중지"
        elif reason == "stalled":
            kind, end_reason, detail = "stalled", st.END_STALL, f"{stall_limit:g}초 동안 파일이 늘지 않았습니다"
        elif not got_data and after_endlist and rc == 0 and tool_reason == "endlist":
            # 종료 표시로 끝난 직후 같은 방송을 다시 열었는데 조각 없이 또 종료 표시다. 방송 종료로 본다
            kind, end_reason, detail = "endlist_again", st.END_BROADCAST, "종료 표시 직후 다시 열었지만 조각이 없습니다"
        elif not got_data:
            # 데이터 없이 끝났으면 "녹화 종료"가 아니라 "시작 실패"다
            kind, end_reason = "start_failed", st.END_ERROR
            detail = f"start_failed: 데이터 없이 종료 rc={rc}" + (f" ({tool_reason})" if tool_reason else "")
        elif rc == 0:
            kind, end_reason, detail = "ended", st.END_BROADCAST, tool_reason or "수신 도구 정상 종료"
        else:
            kind, end_reason = "disconnected", st.END_ERROR
            detail = f"수신 도구 종료 rc={rc}" + (f" ({tool_reason})" if tool_reason else "")

        fields: dict[str, Any] = dict(
            ended_at=st.now_iso(), end_reason=end_reason, end_detail=detail, exit_code=rc, bytes=size,
            media_seconds=last.get("media_seconds"), last_media_time=last.get("last_media_time"),
            clean_exit=int(bool(exit_event) and exit_event.get("code") == 0 and rc == 0),
        )
        if last.get("first_media_time"):
            fields["first_media_time"] = last["first_media_time"]
        if got_data:
            fields["finalize_status"] = "pending"
        else:
            fields["finalize_status"] = "skipped"
            fields["part_path"] = None
            await asyncio.to_thread(_unlink, part)
            await asyncio.to_thread(_unlink, audio)
        if kind == "endlist_again":
            # 오류 세션으로 남기지 않는다. 받은 것이 없으므로 세션 기록을 지우고 로그만 남긴다
            await self.db(self.store.delete_session, sid)
            log.info("방송 종료로 본다: %s", detail, extra={"event": "endlist_again", "channel_id": self.channel_id, "platform": self.platform_id,
                                                         "live_id": info.live_id})
            return RecResult(kind, False, None, None, detail, final.name, 0.0, rc)
        await self.db(self.store.update_session, sid, **fields)
        log.log(logging.INFO if kind in ("ended", "user_stop", "shutdown") else logging.WARNING,
                "녹화 끝 %s: %s", kind, detail,
                extra={"event": "record_end", "channel_id": self.channel_id, "platform": self.platform_id, "session_id": sid, "kind": kind,
                       "exit_code": rc, "bytes": size, "end_reason": end_reason})
        eng.events.emit("record_ended", self.platform_id, self.channel_id, session_id=sid, kind=kind,
                        end_reason=end_reason, bytes=size)
        if kind == "start_failed":
            why = "지원하지 않는 스트림 형식" if rc == EXIT_UNSUPPORTED else detail
            self.notify(settings, "start_failed", ch.name, why, final.name)
        elif kind == "stalled":
            self.notify(settings, "abnormal_end", ch.name, detail, final.name)
        elif kind == "user_stop":
            self.notify(settings, "user_stop", ch.name, detail, final.name if got_data else "")
        if got_data and not eng.stopping:
            eng.schedule_finalize(sid)  # 감시를 막지 않는다
        return RecResult(kind, got_data, last.get("last_media_time"), sid, detail, final.name,
                         float(last.get("media_seconds") or 0.0), rc,
                         codec=str(exit_event.get("codec") or last.get("codec") or ""),
                         next_codec=str(exit_event.get("next_codec") or ""))

    async def _terminate(self, proc: asyncio.subprocess.Process, waiter: asyncio.Task) -> None:
        """프로세스 그룹에 종료 신호를 보내고, 안 끝나면 강제 종료한다."""
        _killpg(proc.pid, signal.SIGTERM)
        done, _ = await asyncio.wait({waiter}, timeout=self.engine.t.stop_grace)
        if not done:
            _killpg(proc.pid, signal.SIGKILL)
            await asyncio.wait({waiter}, timeout=5)


def session_seconds(s: dict[str, Any]) -> float | None:
    """세션의 길이. 받은 길이(수신기 보고)가 있으면 그것, 없으면 시작~끝 시각."""
    if s.get("media_seconds"):
        return float(s["media_seconds"])
    if s.get("started_at") and s.get("ended_at"):
        try:
            return max(0.0, (datetime.fromisoformat(s["ended_at"]) - datetime.fromisoformat(s["started_at"])).total_seconds())
        except ValueError:
            return None
    return None


def _hms(seconds: float) -> str:
    total = int(seconds or 0)
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _is_receiver(pid: int, receiver_cmd: tuple[str, ...]) -> bool:
    """그 프로세스가 우리 수신기 명령인가. 리눅스는 /proc, 그 밖(macOS)은 ps로 본다."""
    mine = [c.encode() for c in receiver_cmd]
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return cmdline[: len(mine)] == mine
    except OSError:
        if Path("/proc/self").exists():
            return False  # /proc가 있는데 없는 프로세스
    import subprocess

    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))], capture_output=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip().startswith(b" ".join(mine))


START_SLACK = 120.0  # 세션 기록 시각과 프로세스 시작 시각의 허용 차(초)


def _process_start(pid: int) -> float | None:
    """프로세스가 시작한 시각(유닉스 시각). 알 수 없으면 None. 리눅스는 /proc, 그 밖은 ps."""
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text()
        fields = stat_line[stat_line.rindex(")") + 2:].split()
        ticks = int(fields[19])  # starttime(부팅 뒤 클록 틱)
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        if Path("/proc/self").exists():
            return None
    import subprocess

    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, timeout=5, check=False,
                             env={"LC_ALL": "C", "PATH": os.environ.get("PATH", "/bin:/usr/bin")}).stdout.decode().strip()
        return datetime.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y").timestamp() if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _same_run(pid: int, started_at: str | None) -> bool:
    """PID 재사용 구분: 그 프로세스가 세션을 기록한 무렵에 시작했는가. 어느 한쪽을 모르면 True(명령 비교만 한다)."""
    if not started_at:
        return True
    began = _process_start(pid)
    if began is None:
        return True
    try:
        recorded = datetime.fromisoformat(started_at).timestamp()
    except ValueError:
        return True
    return abs(recorded - began) <= START_SLACK


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


class Engine:
    def __init__(self, cfg: AppConfig, timings: Timings | None = None,
                 disk_free: Callable[[Path], int] | None = None):
        self.cfg = cfg
        self.t = timings or Timings()
        self.disk_free = disk_free or _disk_free
        self.stopping = False
        self.suppress: dict[str, float] = {}
        self.workers: dict[tuple[str, str], ChannelWorker] = {}  # (플랫폼, 채널 ID) → 작업자
        self.store: st.Store = None  # type: ignore[assignment]
        self.clients: dict[str, PlatformClient] = {}  # 플랫폼별 API 클라이언트(처음 쓸 때 만든다)
        self.gates: dict[str, gate_mod.Gate] = {}  # 플랫폼별 요청 창구
        self._gate_values: dict[str, dict[str, float]] = {}
        self._cycle_logged: dict[str, Any] = {}
        self._gate_notified: set[str] = set()
        self.finalizer: Finalizer = None  # type: ignore[assignment]
        self.notifier = Notifier(cfg)
        self.events = EventBus()  # 확장이 듣는 사건. 듣는 쪽은 별도 스레드에서 돈다
        # 저장소를 연 뒤, 감시를 시작하고 server_started를 알리기 전에 부르는 일(웹 서버가 켜진 확장의 저장소를 열고 사건 듣기를 건다)
        self.store_ready: list[Callable[[st.Store], None]] = []
        self._lock = InstanceLock(cfg.lock_path)
        self._finalize_tasks: set[asyncio.Task] = set()
        self._control_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._cookie_task: asyncio.Task | None = None
        self._cookie_wake = asyncio.Event()
        self._cookie_reasons: list[tuple[str, str]] = []
        self.last_cookie_checks: dict[str, dict[str, Any]] = {}  # 플랫폼별 마지막 자격 확인
        self.started = False

    @property
    def last_cookie_check(self) -> dict[str, Any] | None:
        """기본 플랫폼의 마지막 자격 확인."""
        return self.last_cookie_checks.get(registry.DEFAULT)

    def client_for(self, platform_id: str) -> PlatformClient:
        client = self.clients.get(platform_id)
        if client is None:
            client = self.clients[platform_id] = registry.get(platform_id).client(self.cfg)
        return client

    async def load_credentials(self, platform_id: str | None = None) -> dict[str, str]:
        """자격(쿠키)은 쓸 때마다 플랫폼별 파일에서 읽는다. 바꾸면 다음 주기부터 반영된다."""
        return await asyncio.to_thread(credentials.load, self.cfg, platform_id or registry.DEFAULT)

    # ---- 기동 ----
    async def check_tools(self) -> None:
        """ffmpeg, ffprobe, 수신 도구가 없으면 기동을 멈춘다."""
        for label, exe in (("ffmpeg", self.cfg.ffmpeg), ("ffprobe", self.cfg.ffprobe)):
            if shutil.which(exe) is None:
                raise StartupError(f"{label}를 찾을 수 없습니다: {exe}")
            try:
                proc = await asyncio.create_subprocess_exec(
                    exe, "-version", stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                out, _ = await asyncio.wait_for(proc.communicate(), 15)
            except (OSError, asyncio.TimeoutError) as e:
                raise StartupError(f"{label}를 실행할 수 없습니다: {type(e).__name__}") from None
            if proc.returncode != 0:
                raise StartupError(f"{label} -version 실패 rc={proc.returncode}")
            log.info("%s: %s", label, out.decode("utf-8", "replace").splitlines()[0][:120] if out else "?",
                     extra={"event": "tool_version"})
        enabled = await asyncio.to_thread(self.store.enabled_platforms) if self.store else []
        for platform in registry.all_platforms():
            if not platform.tool_names or not self._platform_on(platform, enabled):
                continue  # 꺼진 플랫폼의 도구는 고르지도 점검하지도 않는다
            await asyncio.to_thread(platform.startup, self.cfg)  # 쓸 도구 판을 시작할 때 한 번 고른다
            for label, text in platform.tool_versions().items():  # 플랫폼이 쓰는 도구의 판 + 출처
                log.info("%s: %s", label, text, extra={"event": "tool_version"})
        cmd = self.cfg.receiver_cmd
        if not cmd or shutil.which(cmd[0]) is None:
            raise StartupError(f"수신 도구를 찾을 수 없습니다: {cmd[0] if cmd else ''}")
        if cmd[0] == sys.executable and "live_recorder.recording.receiver" in cmd:
            if importlib.util.find_spec("live_recorder.recording.receiver") is None or importlib.util.find_spec("httpx") is None:
                raise StartupError("수신 도구 모듈(live_recorder.recording.receiver, httpx)을 불러올 수 없습니다")

    async def start(self) -> None:
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.cfg.record_root.is_dir():
            raise StartupError(f"녹화 루트가 없습니다: {self.cfg.record_root}")
        self._lock.acquire()
        try:
            self.store = st.Store(self.cfg.db_path, self.cfg.record_root, self.cfg.telegram_path)  # 깨졌으면 여기서 멈춘다
            await self.check_tools()
            self.notifier.start()
            self.finalizer = Finalizer(self.cfg, self.store, self.notifier, events=self.events)
            self.events.start()
            for hook in self.store_ready:
                await asyncio.to_thread(hook, self.store)
            await self.recover()
        except BaseException:
            await self.notifier.stop()
            await asyncio.to_thread(self.events.stop, 1.0)
            self._lock.release()
            raise
        for ch in await asyncio.to_thread(self.store.list_channels):
            self._spawn_worker(ch.platform, ch.channel_id)  # 자동 녹화가 켜진 채널은 곧 감시를 시작한다
        self._control_task = asyncio.create_task(self._control_loop())
        self._cookie_task = asyncio.create_task(self._cookie_loop())
        self.started = True
        log.info("기동 완료. 채널 %d개", len(self.workers), extra={"event": "started"})
        self.events.emit("server_started", channels=len(self.workers))

    def _spawn_worker(self, platform_id: str, channel_id: str) -> ChannelWorker:
        worker = ChannelWorker(self, platform_id, channel_id)
        worker.task = asyncio.create_task(worker.run(), name=f"worker-{platform_id}-{channel_id[:8]}")
        self.workers[(platform_id, channel_id)] = worker
        return worker

    def worker(self, channel_id: str, platform_id: str | None = None) -> "ChannelWorker | None":
        """채널 ID로 작업자를 찾는다. 플랫폼을 적지 않으면 그 ID를 가진 채널(기본 플랫폼 먼저)."""
        if platform_id:
            return self.workers.get((platform_id, channel_id))
        found = [w for (pid, cid), w in self.workers.items() if cid == channel_id]
        found.sort(key=lambda w: w.platform_id != registry.DEFAULT)
        return found[0] if found else None

    async def recover(self) -> None:
        """지난 실행이 닫지 못한 세션과 남은 `.part`를 마무리한다."""
        store = self.store
        await asyncio.to_thread(store.reset_runtime_state)
        for s in await asyncio.to_thread(store.open_sessions):
            await asyncio.to_thread(self._reap_receiver, s.get("pid"), s.get("started_at"))
            part = Path(s["part_path"]) if s.get("part_path") else None
            size = _size(part) if part else 0
            fields: dict[str, Any] = dict(ended_at=st.now_iso(), end_reason=st.END_SHUTDOWN,
                                          end_detail="지난 실행이 닫지 못한 세션", bytes=size, clean_exit=0)
            if size > 0:
                fields["finalize_status"] = "pending"
            else:
                fields["finalize_status"] = "skipped"
                fields["part_path"] = None
                if part:
                    _unlink(part)
                    _unlink(audio_part(part))
            await asyncio.to_thread(store.update_session, s["id"], **fields)
            log.warning("닫히지 않은 세션을 정리했다", extra={"event": "recover_session", "session_id": s["id"], "bytes": size})

        channels = await asyncio.to_thread(store.list_channels)
        known = await asyncio.to_thread(store.known_part_paths)
        known_finals = await asyncio.to_thread(store.known_final_paths)
        settings = await asyncio.to_thread(store.get_settings)
        for ch in channels:
            folder = Path(ch.output_dir)
            if not folder.is_dir():
                continue
            for p in sorted(folder.glob("*" + PART_SUFFIX)):
                if p.name.endswith(REMUX_SUFFIX) and str(p)[: -len(REMUX_SUFFIX)] in known_finals:
                    _unlink(p)  # 기록이 있는 변환 임시 파일은 원본에서 다시 만든다
                elif p.name.endswith(AUDIO_PART_SUFFIX) and (
                        str(p)[: -len(AUDIO_PART_SUFFIX)] + PART_SUFFIX in known
                        or str(p)[: -len(AUDIO_PART_SUFFIX)] in known_finals):
                    continue  # 기록이 있는 녹화의 음성 원본. 마무리가 영상과 함께 처리한다
                elif str(p) not in known:
                    # 저장소에 세션 기록이 없는 .part는 이 도구가 쓴 파일이라는 근거가 없다.
                    # 변환하거나 지우지 않고 그대로 두며, 로그와 알림만 남긴다
                    log.warning("기록 없는 .part를 찾았다. 손대지 않았다: %s", p.name,
                                extra={"event": "recover_orphan", "channel_id": ch.channel_id, "platform": ch.platform})
                    self.notifier.notify(settings, "finalize_failed", ch.channel_id, ch.name,
                                         "기록 없는 임시 파일을 찾았습니다. 변환·삭제하지 않고 그대로 두었습니다", p.name)
        for s in await asyncio.to_thread(store.unfinalized_sessions):
            self.schedule_finalize(s["id"])
        # 마무리 실패로 남은 원본이 그대로 있으면 한 번 다시 마무리한다. 다시 실패하면 더 시도하지 않는다
        for s in await asyncio.to_thread(store.retryable_sessions):
            if await asyncio.to_thread(retry_source, s) is not None:
                log.info("마무리 실패로 남은 원본을 다시 마무리한다", extra={"event": "refinalize", "session_id": s["id"]})
                self.schedule_finalize(s["id"], retry=True)

    def _reap_receiver(self, pid: int | None, started_at: str | None = None) -> None:
        """지난 실행의 수신기가 아직 살아 있으면 끝낸다. 다른 프로세스는 건드리지 않는다
        (명령이 같고, 세션을 기록한 무렵에 시작한 프로세스일 때만)."""
        if not pid:
            return
        if not _is_receiver(pid, self.cfg.receiver_cmd) or not _same_run(pid, started_at):
            return
        _killpg(pid, signal.SIGTERM)
        for _ in range(50):
            if not _alive(pid):
                return
            time.sleep(0.1)
        _killpg(pid, signal.SIGKILL)

    def schedule_finalize(self, session_id: int, *, retry: bool = False) -> asyncio.Task:
        task = asyncio.create_task(self.finalizer.finalize_session(session_id, retry=retry), name=f"finalize-{session_id}")
        self._finalize_tasks.add(task)
        task.add_done_callback(self._finalize_done)
        return task

    def _finalize_done(self, task: asyncio.Task) -> None:
        self._finalize_tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("마무리 작업 예외: %r", task.exception(), extra={"event": "finalize_exception"})

    async def wait_finalizers(self) -> None:
        while self._finalize_tasks:
            await asyncio.gather(*list(self._finalize_tasks), return_exceptions=True)

    # ---- 조회와 채널 관리 (웹 API가 직접 부른다) ----
    def thumbnail(self, channel_id: str, shown: str, platform_id: str | None = None) -> tuple[str | None, bool]:
        """방송 중일 때만 썸네일 주소를 준다. 성인 방송은 주소를 주지 않는다(화면은 아이콘과 '연령 제한')."""
        worker = self.worker(channel_id, platform_id)
        if worker is None or shown not in (st.RECORDING, st.WATCHING, st.FINALIZING) or not worker.live_image:
            return None, False
        if worker.live_adult:
            return None, True
        return worker.platform.thumbnail_url(worker.live_image), False

    def _views(self) -> list[dict[str, Any]]:
        now = datetime.now().astimezone()
        rows = []
        for ch in self.store.list_channels():
            state = self.store.get_state(ch.channel_id, ch.platform)
            session = self.store.get_session(state["session_id"]) if state.get("session_id") else None
            shown = self.store.display_state(state)
            thumb, adult = self.thumbnail(ch.channel_id, shown, ch.platform)
            platform = registry.find(ch.platform)
            off = bool(platform and platform.opt_in and not self._platform_on(platform, self.store.enabled_platforms()))
            last = self.store.last_recorded_session(ch.channel_id, ch.platform)
            last_parts = self.store.sessions_of_live(ch.channel_id, last["live_id"], ch.platform) if last and last.get("live_id") else []
            last_parts = [p for p in last_parts if p.get("ended_at") and p.get("bytes")] or ([last] if last else [])
            elapsed = None
            if session and shown == st.RECORDING:
                elapsed = max(0, int((now - datetime.fromisoformat(session["started_at"])).total_seconds()))
            try:
                folder = str(Path(ch.output_dir).relative_to(self.cfg.record_root)) or "."
            except ValueError:
                folder = ch.output_dir
            rows.append({
                "channel_id": ch.channel_id, "platform": ch.platform, "name": ch.name,
                # 지원하지 않는 선택 기능의 표시는 화면이 감춘다
                "platform_name": platform.name if platform else ch.platform,
                "timemachine": bool(platform and platform.features.timemachine), "output_dir": folder, "quality": ch.quality,
                # 꺼진 플랫폼의 채널: 화면이 "…가 꺼져 있습니다"를 보인다
                "platform_off": off, "platform_note": (platform.opt_in_notice(self.cfg).get("off_label") if off else None),
                "extension": ch.extension, "auto_record": ch.auto_record, "video_codec": ch.video_codec,
                "state": shown, "state_label": st.STATE_LABELS.get(shown, shown),
                "live_title": state.get("live_title") if session else None,
                "file": Path(session["final_path"]).name if session and session.get("final_path") else None,
                "elapsed": elapsed, "source": session["source"] if session else None,
                "error": state.get("error"), "error_flag": bool(state.get("error_flag")),
                "last_check_at": state.get("last_check_at"),
                # 카드 정보
                "error_count": int(state.get("error_count") or 0),
                "quality_label": session.get("quality_label") if session and shown == st.RECORDING else None,
                "bytes": session.get("bytes") if session and shown == st.RECORDING else None,
                "file_started_at": (session.get("first_media_time") or session["started_at"]) if session else None,
                "thumb_url": thumb, "adult": adult,
                # 마지막 녹화: 같은 방송(방송 ID)에서 나뉜 파일은 합친다
                "last_started_at": min((p.get("first_media_time") or p["started_at"]) for p in last_parts) if last_parts else None,
                "last_seconds": sum(session_seconds(p) or 0.0 for p in last_parts) if last_parts else None,
            })
        return rows

    async def channel_views(self) -> list[dict[str, Any]]:
        """채널 목록과 화면에 보일 상태."""
        return await asyncio.to_thread(self._views)

    # ---- 플랫폼 요청 창구 ----
    def gate_values(self, platform_id: str) -> dict[str, float]:
        values = self._gate_values.get(platform_id)
        if values is None:
            values = self._gate_values[platform_id] = (self.store.gate_settings(platform_id) if self.store
                                                      else {"scan": gate_mod.DEFAULT_SCAN, "gap": gate_mod.DEFAULT_GAP})
        return values

    def set_gate_values(self, platform_id: str, scan: float | None = None, gap: float | None = None) -> dict[str, float]:
        """저장하면 다음 바퀴부터 적용한다."""
        values = self.store.set_gate_settings(platform_id, scan, gap)
        self._gate_values[platform_id] = values
        self._note_cycle(platform_id)
        return values

    def gap_of(self, platform_id: str) -> float:
        if self.t.gate_interval_override is not None:
            return self.t.gate_interval_override
        return self.gate_values(platform_id)["gap"]

    def gate_for(self, platform_id: str) -> gate_mod.Gate:
        gate = self.gates.get(platform_id)
        if gate is None:
            gate = self.gates[platform_id] = gate_mod.Gate(
                platform_id, lambda pid=platform_id: self.gap_of(pid), steps=self.t.throttle_steps,
                front_floor=self.t.gate_front_floor, on_pause=self._gate_paused, on_resume=self._gate_resumed)
        return gate

    def watched_count(self, platform_id: str) -> int:
        """감시 중인 채널 수(자동 녹화가 켜진 그 플랫폼의 채널)."""
        if self.store is None:
            return 0
        return sum(1 for ch in self.store.list_channels() if ch.platform == platform_id and ch.auto_record)

    def cycle_of(self, platform_id: str) -> float:
        """한 바퀴 시간(감시 중인 채널 수 × 채널 사이 간격)."""
        return gate_mod.cycle_seconds(self.watched_count(platform_id), self.gap_of(platform_id))

    def gate_info(self, platform_id: str) -> dict[str, Any]:
        """화면·진단용: 스캔 주기, 채널 사이 간격, 채널 수, 한 바퀴 시간, 늘어났는지, 멈춤 상태, 경고."""
        values = self.gate_values(platform_id)
        channels = self.watched_count(platform_id)
        cycle = gate_mod.cycle_seconds(channels, values["gap"])
        gate = self.gates.get(platform_id)
        info = {"scan": values["scan"], "gap": values["gap"], "channels": channels, "cycle": cycle,
                "extended": cycle > values["scan"], "warnings": gate_mod.warnings(values["scan"], values["gap"]),
                "paused": bool(gate and gate.paused()), "pause_left": gate.pause_left() if gate else 0.0}
        info["note"] = (f"채널 {channels}개를 간격 {values['gap']:g}초로 확인해 한 바퀴에 약 {cycle:g}초가 걸립니다"
                        if info["extended"] else None)
        return info

    def _note_cycle(self, platform_id: str) -> None:
        """주기가 늘어났으면(또는 바뀌었으면) 한 번 남긴다."""
        info = self.gate_info(platform_id)
        key = (info["channels"], info["gap"], info["scan"]) if info["extended"] else None
        if self._cycle_logged.get(platform_id) == key:
            return
        self._cycle_logged[platform_id] = key
        if key is not None:
            log.info("한 바퀴가 스캔 주기보다 길어 주기를 늘렸다: 채널 %d개, 간격 %g초, 한 바퀴 %g초", info["channels"],
                     info["gap"], info["cycle"], extra={"event": "gate_cycle_extended", "platform": platform_id,
                                                        "channels": info["channels"], "interval": info["gap"],
                                                        "cycle": info["cycle"]})

    async def pause_message(self, platform_id: str, detail: str = "") -> str:
        gate = self.gate_for(platform_id)
        minutes = (gate.status()["pause_seconds"] or 0) / 60
        msg = f"플랫폼이 확인을 막아 이 플랫폼의 확인을 {minutes:g}분 멈췄습니다. 그 뒤에 다시 확인합니다"
        if detail:
            msg += f"({detail})"
        platform = registry.find(platform_id)
        if gate.at_cap >= 3 and platform and platform.credential_spec.names and not await self.load_credentials(platform_id):
            msg += ". 쿠키를 넣으면 풀릴 수 있습니다"  # 쿠키 없이 상한에서 세 번 이어 막혔다
        return msg

    def _gate_paused(self, status: dict) -> None:
        pid = status["platform"]
        info = self.gate_info(pid)
        log.warning("요청 창구를 멈춘다 %g초", status["pause_seconds"],
                    extra={"event": "gate_paused", "platform": pid, "channels": info["channels"], "interval": info["gap"],
                           "cycle": info["cycle"]})
        for (wpid, _), w in self.workers.items():
            if wpid == pid:
                w.wake.set()
        if pid not in self._gate_notified:
            self._gate_notified.add(pid)  # 알림은 플랫폼당 한 번
            try:
                settings = self.store.get_settings()
                platform = registry.find(pid)
                self.notifier.notify(settings, "abnormal_end", "", platform.name if platform else pid,
                                     f"플랫폼이 확인을 막아 {status['pause_seconds'] / 60:g}분 동안 이 플랫폼의 확인을 멈췄습니다. "
                                     "그 뒤에 채널 하나로 먼저 다시 확인합니다")
            except Exception:  # noqa: BLE001 - 알림 실패가 창구를 막지 않는다
                pass

    def _gate_resumed(self, status: dict) -> None:
        pid = status["platform"]
        self._gate_notified.discard(pid)
        info = self.gate_info(pid)
        log.info("요청 창구를 다시 연다", extra={"event": "gate_resumed", "platform": pid, "channels": info["channels"],
                                            "interval": info["gap"], "cycle": info["cycle"]})
        for (wpid, _), w in self.workers.items():
            if wpid == pid:
                w.wake.set()

    # ---- 켜야 쓰는 플랫폼 ----
    @staticmethod
    def _platform_on(platform: Platform, enabled: list[str]) -> bool:
        if not platform.opt_in:
            return True
        return platform.id in enabled and not platform.enable_blocked()

    def platform_on(self, platform_id: str | None) -> bool:
        """그 플랫폼을 지금 쓸 수 있는가. 켜야 쓰는 플랫폼은 켠 기록이 있고 막혀 있지 않을 때만(동기, 저장소를 읽는다)."""
        platform = registry.find(platform_id)
        if platform is None:
            return False
        if not platform.opt_in:
            return True
        return self._platform_on(platform, self.store.enabled_platforms())

    async def enable_platform(self, platform_id: str) -> None:
        """자체 점검을 통과해야 켜진다. 실패하면 ValueError(까닭), 꺼진 채로 둔다. 다시 시작하지 않아도 바로 쓴다."""
        platform = registry.get(platform_id)
        if not platform.opt_in:
            raise ValueError("켜고 끌 수 있는 플랫폼이 아닙니다")
        why = platform.enable_blocked()
        if why:
            raise PermissionError(why)
        try:
            await asyncio.to_thread(platform.prepare_enable, self.cfg)
        except ValueError as e:
            log.warning("플랫폼 켜기 실패: %s", e, extra={"event": "platform_enable_failed", "platform": platform.id})
            raise
        await asyncio.to_thread(self.store.set_platform_enabled, platform.id, True)
        self.clients.pop(platform.id, None)  # 고른 도구 판으로 새로 만든다
        log.info("플랫폼을 켰다", extra={"event": "platform_enabled", "platform": platform.id})
        for label, text in platform.tool_versions().items():
            log.info("%s: %s", label, text, extra={"event": "tool_version"})
        self._wake_platform(platform.id)

    async def disable_platform(self, platform_id: str) -> None:
        """녹화 중인 채널이 있으면 끌 수 없다(ValueError). 끄면 감시를 멈추고 채널·쿠키·받아 둔 판은 그대로 둔다."""
        platform = registry.get(platform_id)
        if not platform.opt_in:
            raise ValueError("켜고 끌 수 있는 플랫폼이 아닙니다")
        busy = [w for (pid, _), w in self.workers.items() if pid == platform.id and (w.recording or w.in_broadcast)]
        if busy:
            raise ValueError(f"녹화 중인 {platform.name} 채널이 있어 끌 수 없습니다. 녹화를 멈춘 뒤 다시 해 주십시오")
        await asyncio.to_thread(self.store.set_platform_enabled, platform.id, False)
        log.info("플랫폼을 껐다", extra={"event": "platform_disabled", "platform": platform.id})
        self._wake_platform(platform.id)

    def _wake_platform(self, platform_id: str) -> None:
        for (pid, _), w in self.workers.items():
            if pid == platform_id:
                w.wake.set()

    async def resolve_channel(self, platform_id: str | None, text: str) -> tuple[str, str | None]:
        """채널 칸의 글(주소·ID·`@이름`)을 (채널 ID, 조회로 안 이름)으로. 못 찾으면 store.ValidationError."""
        platform = registry.get(platform_id)
        try:
            return platform.parse_channel(text), None
        except ChannelLookupNeeded as need:
            client = self.client_for(platform.id)
            try:
                async with self.gate_for(platform.id).slot(gate_mod.FRONT):  # 채널 추가 때의 조회는 앞줄
                    cid, name = await client.lookup_channel(need.query, await self.load_credentials(platform.id))
            except ChannelParseError as e:
                raise st.ValidationError(str(e)) from None
            except ApiError as e:
                log.warning("채널 조회 실패: %s", e, extra={"event": "channel_lookup_failed", "platform": platform.id})
                raise st.ValidationError("채널을 조회하지 못했습니다. 잠시 뒤 다시 해 주십시오") from None
            if not platform.valid_channel_id(cid):
                raise st.ValidationError(platform.channel_id_error)
            log.info("채널 조회 %s", cid, extra={"event": "channel_lookup", "platform": platform.id, "channel_id": cid})
            return cid, clean_text(name)
        except ChannelParseError as e:
            raise st.ValidationError(str(e)) from None

    async def add_channel(self, **fields: Any) -> st.Channel:
        ch = await asyncio.to_thread(lambda: self.store.add_channel(**fields))
        if (ch.platform, ch.channel_id) not in self.workers:
            self._spawn_worker(ch.platform, ch.channel_id)
        self._note_cycle(ch.platform)
        self.events.emit("channel_added", ch.platform, ch.channel_id, name=ch.name, output_dir=ch.output_dir,
                         quality=ch.quality, extension=ch.extension, auto_record=ch.auto_record, video_codec=ch.video_codec)
        return ch

    async def update_channel(self, channel_id: str, platform_id: str | None = None, **changes: Any) -> st.Channel:
        ch = await asyncio.to_thread(lambda: self.store.update_channel(channel_id, platform=platform_id, **changes))
        worker = self.worker(ch.channel_id, ch.platform)
        if worker:
            worker.wake.set()  # 다음 확인부터 반영된다
        self.events.emit("channel_updated", ch.platform, ch.channel_id, changes=dict(changes))
        return ch

    # ---- 조작 ----
    async def manual_start(self, channel_id: str, platform_id: str | None = None) -> str:
        """지금 확인(방송 중이면 녹화를 시작한다)."""
        worker = self.worker(channel_id, platform_id)
        if worker is None:
            return "없는 채널"
        if worker.recording or worker.in_broadcast:
            return "이미 녹화 중"  # 아무것도 바꾸지 않는다
        worker.manual_once = True
        worker.wake.set()
        return "확인 요청함"

    async def manual_stop(self, channel_id: str, platform_id: str | None = None) -> str:
        """녹화 중지(같은 방송은 건너뛴다)."""
        worker = self.worker(channel_id, platform_id)
        if worker is None:
            return "없는 채널"
        if not (worker.recording or worker.in_broadcast):
            return "녹화 중이 아님"
        worker.stop_requested = True
        worker.wake.set()
        return "중지 요청함"

    async def remove_channel(self, channel_id: str, platform_id: str | None = None) -> str:
        """진행 중인 녹화를 먼저 중지·마무리한다."""
        worker = self.worker(channel_id, platform_id)
        if worker is not None:
            platform_id = worker.platform_id
            self.workers.pop((worker.platform_id, worker.channel_id), None)
            worker.remove_requested = True
            worker.wake.set()
            if worker.task:
                await asyncio.gather(worker.task, return_exceptions=True)
        found = await asyncio.to_thread(self.store.get_channel, channel_id, platform_id)
        await asyncio.to_thread(self.store.delete_channel, channel_id, platform_id)
        if found is not None:
            self.events.emit("channel_removed", found.platform, found.channel_id, result="삭제함")
        return "삭제함"

    async def refinalize(self, session_id: int) -> str:
        """CLI가 세션 번호로 요청한다. 횟수와 상관없이 한 번 다시 마무리한다(끝나기를 기다리지 않는다)."""
        session = await asyncio.to_thread(self.store.get_session, session_id)
        if session is None:
            return "없는 세션"
        if await asyncio.to_thread(retry_source, session) is None:
            return "다시 마무리할 원본이 없습니다(마무리 실패 기록과 .raw 원본이 있어야 합니다)"
        self.schedule_finalize(session_id, retry=True)
        return "다시 마무리를 시작했습니다. 결과는 sessions 명령으로 확인하십시오"

    # ---- 파일 관리자 잠금 ----
    def locked_paths(self) -> list[str]:
        """녹화 중인 파일과 마무리할(하고 있는) 파일. 아직 없는 최종 파일은 그 폴더를 잠그는 데 쓴다."""
        from .finalize import REMUX_SUFFIX

        out: list[str] = []
        for s in self.store.open_sessions() + self.store.unfinalized_sessions():
            for key in ("part_path", "final_path"):
                if s.get(key):
                    out.append(s[key])
            if s.get("finalize_status") == "running" and s.get("output_path"):
                out.append(s["output_path"])  # 다시 마무리 중인 `.raw.*` 원본
            if s.get("final_path"):
                out.append(s["final_path"] + REMUX_SUFFIX)
        return out

    # ---- 쿠키 확인 ----
    def request_cookie_check(self, reason: str, platform_id: str | None = None) -> None:
        """성인 방송 시작 실패처럼 엔진 안에서 확인을 요청한다. 실제 확인은 따로 도는 작업이 한다."""
        pid = platform_id or registry.DEFAULT
        if registry.get(pid).credential_spec.checkable:
            self._cookie_reasons.append((reason, pid))
        self._cookie_wake.set()

    async def check_cookies(self, reason: str, platform_id: str | None = None) -> str:
        """확인하고 결과를 남긴다. 만료면 알린다. '확인 실패'는 알리지 않는다."""
        pid = platform_id or registry.DEFAULT
        values = await self.load_credentials(pid)
        result = await self.client_for(pid).check_credentials(values)
        self.last_cookie_checks[pid] = {"result": result, "reason": reason, "at": time.time()}
        log.info("쿠키 확인 %s (%s)", result, reason,
                 extra={"event": "cookie_check", "result": result, "reason": reason, "platform": pid})
        if result == "expired":
            settings = await asyncio.to_thread(self.store.get_settings)
            self.notifier.notify(settings, "cookie_expired", "", "", f"{registry.get(pid).name} 쿠키가 만료됐습니다")
        return result

    def _checkable(self) -> list[str]:
        return [p.id for p in registry.all_platforms() if p.credential_spec.checkable]

    async def _cookie_loop(self) -> None:
        """하루 한 번(쿠키가 있을 때만), 그리고 요청이 올 때 확인한다. 성인 방송 실패가 잇따라도 10분에 한 번만."""
        last_daily = time.monotonic()
        last_requested = -1e9
        while not self.stopping:
            try:
                await asyncio.wait_for(self._cookie_wake.wait(), timeout=min(self.t.cookie_check_interval, 3600))
            except asyncio.TimeoutError:
                pass
            self._cookie_wake.clear()
            if self.stopping:
                return
            try:
                now = time.monotonic()
                if self._cookie_reasons:
                    reason, pid = self._cookie_reasons[-1]
                    self._cookie_reasons.clear()
                    if now - last_requested >= self.t.cookie_check_min_gap and await self.load_credentials(pid):
                        last_requested = now
                        await self.check_cookies(reason, pid)
                elif now - last_daily >= self.t.cookie_check_interval:
                    last_daily = now
                    for pid in self._checkable():
                        if await self.load_credentials(pid):
                            await self.check_cookies("daily", pid)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("쿠키 확인 중 예외", extra={"event": "cookie_check_exception"})

    async def _control_loop(self) -> None:
        while not self.stopping:
            try:
                for row in await asyncio.to_thread(self.store.pending_controls):
                    action, cid, pid = row["action"], row["channel_id"], row["platform"]
                    if action == "start":
                        result = await self.manual_start(cid, pid)
                    elif action == "stop":
                        result = await self.manual_stop(cid, pid)
                    elif action == "remove":
                        result = await self.remove_channel(cid, pid)
                    elif action.startswith("refinalize:"):
                        result = await self.refinalize(int(action.split(":", 1)[1]))
                    else:
                        result = "모르는 조작"
                    await asyncio.to_thread(self.store.finish_control, row["id"], result)
                    log.info("조작 %s: %s", action, result, extra={"event": "control", "channel_id": cid, "platform": pid})
                # CLI가 다른 프로세스에서 추가한 채널을 알아챈다
                for ch in await asyncio.to_thread(self.store.list_channels):
                    if (ch.platform, ch.channel_id) not in self.workers:
                        self._spawn_worker(ch.platform, ch.channel_id)
                for key, worker in list(self.workers.items()):
                    if worker.task and worker.task.done():
                        self.workers.pop(key, None)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("조작 처리 중 예외", extra={"event": "control_exception"})
            await asyncio.sleep(self.t.control_poll)

    # ---- 종료 ----
    def request_stop(self) -> None:
        self._stop_event.set()

    async def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        log.info("종료 시작", extra={"event": "stopping"})
        self.events.emit("server_stopping")
        if self._control_task:
            self._control_task.cancel()
        if self._cookie_task:
            self._cookie_task.cancel()
        for worker in self.workers.values():
            worker.wake.set()
        tasks = [w.task for w in self.workers.values() if w.task]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.t.shutdown_deadline)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=self.t.stop_grace + 6)
        # 마무리 중인 변환은 끊는다. 원본 `.part`가 남아 다음 기동에 처리된다
        if self.finalizer:
            self.finalizer.kill_all()
        if self._finalize_tasks:
            _, pending = await asyncio.wait(list(self._finalize_tasks), timeout=10)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=5)
        if self._control_task:
            await asyncio.gather(self._control_task, return_exceptions=True)
        if self._cookie_task:
            await asyncio.gather(self._cookie_task, return_exceptions=True)
        for client in self.clients.values():
            await client.aclose()
        await self.notifier.stop()
        await asyncio.to_thread(self.events.stop)  # 남은 사건을 전한다(듣는 쪽이 붙잡으면 기다리지 않는다)
        if self.store:
            await asyncio.to_thread(self.store.reset_runtime_state)
            self.store.close()
        self._lock.release()
        log.info("종료 완료", extra={"event": "stopped"})

    async def serve(self) -> None:
        await self.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop)
        try:
            await self._stop_event.wait()
        finally:
            await self.stop()
