"""플랫폼 요청 창구. 중심부의 일반 기능이다. 플랫폼 이름으로 가르지 않는다.

플랫폼마다 창구 하나. 그 플랫폼으로 가는 확인 요청은 모두 창구를 거쳐 한 번에 하나씩 나가고,
앞 요청이 끝난 뒤 "요청 사이 간격"(+0~10% 무작위)이 지나야 다음 요청이 나간다.

줄의 순서:
- FRONT(앞줄): 녹화 중인 방송의 주소 갱신, 끊긴 뒤 같은 방송에 다시 붙기 위한 확인, "지금 확인", 채널 추가 때의 조회,
  같은 채널의 이어지는 요청(빠른 재확인, 짧은 재조회). 간격의 3분의 1(최소 1초)만 지킨다
- WATCH: 감시 확인
- EXTRA: 같은 플랫폼의 다른 기능(목록 수집, 분석). 감시 뒤에 서고 각자의 더 긴 간격도 지킨다

멈춤: 요청 제한·봇 확인을 받으면 창구 전체를 5·10·20분(상한) 멈춘다. 멈춘 동안에도 앞줄은 나간다.
멈춤이 끝나면 감시 요청 하나만 먼저 내 보고(찔러 보기), 되면 나머지를 재개하고 단계를 되돌린다.

시각(clock)과 기다리기(sleep)는 바꿔 넣을 수 있다(가짜 시계 시험).
"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

FRONT, WATCH, EXTRA = 0, 1, 2
RANK_NAMES = {FRONT: "front", WATCH: "watch", EXTRA: "extra"}
# 플랫폼마다 둘: 스캔 주기(한 바퀴를 시작하는 간격)와 채널 사이 간격(요청 사이 간격)
DEFAULT_SCAN, SCAN_RANGE, SCAN_WARN_AT_OR_BELOW = 60.0, (10.0, 3600.0), 30.0
DEFAULT_GAP, GAP_RANGE, GAP_WARN_BELOW = 15.0, (1.0, 600.0), 5.0  # 기본 15초, 경고는 5초 미만
WARNING = "이 값은 플랫폼이 이 IP를 차단할 위험이 있습니다. 채널이 많을수록 위험이 커집니다."
PAUSE_STEPS = (300.0, 600.0, 1200.0)  # (단계)

# 진단 요약이 읽는 창구 상태(같은 프로세스 안). 플랫폼 ID → 상태
STATE: dict[str, dict[str, Any]] = {}


def _number(value: Any, label: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}는 숫자(초)입니다")
    v = float(value)
    if not lo <= v <= hi:
        raise ValueError(f"{label}는 {lo:g}~{hi:g}초입니다")
    return v


def validate_scan(value: Any) -> float:
    return _number(value, "스캔 주기", *SCAN_RANGE)


def validate_gap(value: Any) -> float:
    return _number(value, "채널 사이 간격", *GAP_RANGE)


def warnings(scan: float, gap: float) -> dict[str, str | None]:
    """경고 범위의 값이면 그 칸의 경고 문장."""
    return {"scan": WARNING if scan <= SCAN_WARN_AT_OR_BELOW else None, "gap": WARNING if gap < GAP_WARN_BELOW else None}


@dataclass(eq=False)
class _Waiter:
    rank: int
    seq: int
    min_gap: float
    jitter: float
    probe: bool = False


@dataclass
class Ticket:
    gate: "Gate"
    waiter: _Waiter
    outcome: str = "ok"  # ok | blocked

    def blocked(self) -> None:
        self.outcome = "blocked"


class Gate:
    def __init__(self, platform_id: str, interval: Callable[[], float], *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, rand: Callable[[], float] = random.random,
                 steps: tuple[float, ...] = PAUSE_STEPS, front_floor: float = 1.0,
                 on_pause: Callable[[dict], None] | None = None, on_resume: Callable[[dict], None] | None = None):
        self.platform_id = platform_id
        self.interval = interval
        self.clock = clock
        self._sleep = sleep
        self.rand = rand
        self.steps = steps
        self.front_floor = front_floor
        self.on_pause = on_pause
        self.on_resume = on_resume
        self._seq = itertools.count()
        self._waiters: list[_Waiter] = []
        self._busy = False
        self._last_end: float | None = None
        self._event = asyncio.Event()
        self.level = 0  # 멈춤 단계(0이면 멈추지 않음)
        self.pause_until: float | None = None
        self.probing = False  # 멈춤이 끝나 찔러 보는 중
        self.at_cap = 0  # 상한에서 이어 막힌 횟수(쿠키 안내)
        self.sent = 0
        self._publish()

    # ---- 상태 ----
    def paused(self) -> bool:
        return self.level > 0

    def pause_left(self) -> float:
        if self.pause_until is None:
            return 0.0
        return max(0.0, self.pause_until - self.clock())

    def _publish(self) -> None:
        STATE[self.platform_id] = {"interval": self.interval(), "paused": self.paused(), "level": self.level,
                                   "pause_left": round(self.pause_left(), 1), "sent": self.sent}

    def _kick(self) -> None:
        event, self._event = self._event, asyncio.Event()
        event.set()

    def _gap(self, w: _Waiter) -> float:
        interval = max(0.0, float(self.interval()))
        if w.rank == FRONT:
            return max(self.front_floor, interval / 3.0) if interval > 0 else 0.0
        gap = interval * (1.0 + 0.1 * w.jitter)
        return max(gap, w.min_gap)

    def _eligible(self, w: _Waiter, now: float) -> bool:
        if w.rank == FRONT or not self.paused():
            return True
        # 멈춘 동안은 앞줄만. 멈춤이 끝났으면 감시 하나만 찔러 본다(찔러 보는 중이면 기다린다)
        if self.pause_until is not None and now < self.pause_until:
            return False
        return w.rank == WATCH and not self.probing

    def _ready(self, me: _Waiter) -> float | None:
        """0이면 지금 나간다, 양수면 그만큼 기다린 뒤 다시 본다, None이면 깨울 때까지 기다린다."""
        if self._busy:
            return None
        now = self.clock()
        eligible = [w for w in self._waiters if self._eligible(w, now)]
        if not eligible:
            if self.paused() and self.pause_until is not None and now < self.pause_until:
                return self.pause_until - now
            return None
        head = min(eligible, key=lambda w: (w.rank, w.seq))
        if head is not me:
            return None
        start = (self._last_end + self._gap(me)) if self._last_end is not None else now
        return max(0.0, start - now)

    async def _wait(self, delay: float | None) -> None:
        event = self._event
        waiter = asyncio.ensure_future(event.wait())
        tasks = {waiter}
        timer = None
        if delay is not None:
            timer = asyncio.ensure_future(self._sleep(delay))
            tasks.add(timer)
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()

    async def acquire(self, rank: int = WATCH, min_gap: float = 0.0) -> Ticket:
        me = _Waiter(rank, next(self._seq), float(min_gap), self.rand())
        self._waiters.append(me)
        self._kick()
        try:
            while True:
                delay = self._ready(me)
                if delay == 0:
                    break
                await self._wait(delay)
        except BaseException:
            if me in self._waiters:
                self._waiters.remove(me)
            self._kick()
            raise
        self._waiters.remove(me)
        self._busy = True
        if self.paused() and rank == WATCH and (self.pause_until is None or self.clock() >= self.pause_until):
            me.probe = True
            self.probing = True
        return Ticket(self, me)

    def release(self, ticket: Ticket) -> None:
        now = self.clock()
        self._busy = False
        self._last_end = now
        self.sent += 1
        w = ticket.waiter
        if ticket.outcome == "blocked":
            self.probing = False
            self.level = min(self.level + 1, len(self.steps))
            self.at_cap = self.at_cap + 1 if self.level == len(self.steps) else 0
            self.pause_until = now + self.steps[self.level - 1]
            if self.on_pause:
                self.on_pause(self.status())
        elif w.probe:
            # 찔러 본 요청이 됐다: 나머지를 재개하고 단계를 되돌린다
            self.probing = False
            self.level, self.pause_until, self.at_cap = 0, None, 0
            if self.on_resume:
                self.on_resume(self.status())
        self._publish()
        self._kick()

    def slot(self, rank: int = WATCH, min_gap: float = 0.0) -> "_Slot":
        """`async with gate.slot(...) as t:` 요청 하나. 막혔으면 `t.blocked()`를 부른다."""
        return _Slot(self, rank, min_gap)

    def status(self) -> dict[str, Any]:
        return {"platform": self.platform_id, "interval": self.interval(), "paused": self.paused(), "level": self.level,
                "pause_left": round(self.pause_left(), 1), "pause_seconds": self.steps[self.level - 1] if self.level else 0,
                "at_cap": self.at_cap}


class _Slot:
    def __init__(self, gate: Gate, rank: int, min_gap: float):
        self.gate, self.rank, self.min_gap = gate, rank, min_gap
        self.ticket: Ticket | None = None

    async def __aenter__(self) -> Ticket:
        self.ticket = await self.gate.acquire(self.rank, self.min_gap)
        return self.ticket

    async def __aexit__(self, *exc) -> None:
        if self.ticket is not None:
            self.gate.release(self.ticket)


def cycle_seconds(channels: int, gap: float) -> float:
    """한 바퀴 시간(감시 중인 채널 수 × 채널 사이 간격)."""
    return max(0, int(channels)) * float(gap)
