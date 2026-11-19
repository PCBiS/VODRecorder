"""플랫폼 요청 창구. 가짜 시계로 시험한다(실제 시간을 기다리지 않는다)."""

import asyncio

import pytest

from live_recorder.core import gate as g


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleepers: list[tuple[float, asyncio.Future]] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.t + max(0.0, delay), fut))
        try:
            await fut
        finally:
            self.sleepers = [(t, f) for t, f in self.sleepers if f is not fut]

    async def run(self, tasks, limit: float = 100000.0) -> None:
        """할 일이 없을 때마다 가장 이른 잠든 시각으로 시계를 옮긴다."""
        while not all(t.done() for t in tasks):
            for _ in range(20):
                await asyncio.sleep(0)
            live = [(t, f) for t, f in self.sleepers if not f.done()]
            if not live or all(t.done() for t in tasks):
                continue
            when = min(t for t, _ in live)
            assert when <= limit, "시계가 너무 멀리 갔다"
            self.t = max(self.t, when)
            for t, f in live:
                if t <= self.t and not f.done():
                    f.set_result(None)
        for t in tasks:
            t.result()


def make(clock, gap=30.0, rand=0.0, **kw):
    return g.Gate("p", lambda: gap, clock=clock.now, sleep=clock.sleep, rand=lambda: rand, **kw)


def run(coro):
    return asyncio.run(coro)


def test_one_at_a_time_with_gap_and_no_burst_at_start():
    """채널 10개가 한꺼번에 줄을 서도 한 번에 하나씩, 간격 30초를 지켜 나간다(시작 때 몰리지 않는다)."""
    async def main():
        clock = FakeClock()
        gate = make(clock)
        starts, active = [], {"n": 0, "max": 0}

        async def check(i):
            async with gate.slot(g.WATCH):
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
                starts.append((clock.t, i))
                await clock.sleep(2.0)  # 요청에 2초 걸린다
                active["n"] -= 1

        tasks = [asyncio.ensure_future(check(i)) for i in range(10)]
        await clock.run(tasks)
        return starts, active["max"]

    starts, most = run(main())
    assert most == 1
    times = [t for t, _ in starts]
    assert times == [i * 32.0 for i in range(10)]  # 앞 요청이 끝난(2초) 뒤 30초
    assert [i for _, i in starts] == list(range(10))  # 줄 선 차례대로


def test_jitter_is_zero_to_ten_percent():
    async def main(rand):
        clock = FakeClock()
        gate = make(clock, rand=rand)
        starts = []

        async def check():
            async with gate.slot():
                starts.append(clock.t)

        tasks = [asyncio.ensure_future(check()) for _ in range(3)]
        await clock.run(tasks)
        return starts

    assert run(main(1.0)) == [0.0, 33.0, 66.0]
    assert run(main(0.5)) == [0.0, 31.5, 63.0]


def test_front_requests_go_first_with_a_third_of_the_gap():
    """앞줄 요청은 감시 확인보다 먼저, 간격의 3분의 1(최소 1초)만 지킨다. 목록 수집·분석은 감시 뒤."""
    async def main():
        clock = FakeClock()
        gate = make(clock)
        order = []

        async def req(name, rank, min_gap=0.0):
            async with gate.slot(rank, min_gap):
                order.append((clock.t, name))

        first = asyncio.ensure_future(req("watch0", g.WATCH))
        await asyncio.sleep(0)
        tasks = [first, asyncio.ensure_future(req("extra", g.EXTRA, 45.0)), asyncio.ensure_future(req("watch1", g.WATCH)),
                 asyncio.ensure_future(req("front", g.FRONT))]
        await clock.run(tasks)
        return order

    order = run(main())
    assert order == [(0.0, "watch0"), (10.0, "front"), (40.0, "watch1"), (85.0, "extra")]


def test_front_floor_one_second():
    async def main():
        clock = FakeClock()
        gate = make(clock, gap=1.5)
        order = []

        async def req():
            async with gate.slot(g.FRONT):
                order.append(clock.t)

        tasks = [asyncio.ensure_future(req()) for _ in range(3)]
        await clock.run(tasks)
        return order

    assert run(main()) == [0.0, 1.0, 2.0]


def test_platform_pause_probe_and_resume():
    """막히면 창구 전체를 멈춘다(5·10·20분). 멈춘 동안 앞줄은 나간다. 끝나면 하나로 찔러 보고, 되면 재개한다."""
    async def main():
        clock = FakeClock()
        events = []
        gate = make(clock, on_pause=lambda s: events.append(("pause", clock.t, s["pause_seconds"])),
                    on_resume=lambda s: events.append(("resume", clock.t)))
        log = []
        answers = {"blocked_once": True}

        async def watch(name):
            async with gate.slot(g.WATCH) as t:
                log.append((clock.t, name))
                if answers.pop("blocked_once", False):
                    t.blocked()

        async def front(name):
            async with gate.slot(g.FRONT):
                log.append((clock.t, name))

        tasks = [asyncio.ensure_future(watch(f"w{i}")) for i in range(3)]
        await asyncio.sleep(0)
        tasks.append(asyncio.ensure_future(front("reconnect")))
        await clock.run(tasks)
        return log, events, gate

    log, events, gate = run(main())
    assert log[0] == (0.0, "w0")  # 막힘
    assert log[1] == (10.0, "reconnect")  # 멈춘 동안 앞줄은 나간다
    assert log[2][0] == 300.0 and log[2][1] == "w1"  # 멈춤(5분)이 끝나고 하나로 찔러 본다
    assert log[3] == (330.0, "w2")  # 되면 나머지를 재개한다
    assert events == [("pause", 0.0, 300.0), ("resume", 300.0)]
    assert gate.level == 0 and not gate.paused()


def test_pause_steps_climb_to_cap():
    async def main():
        clock = FakeClock()
        gate = make(clock, gap=1.0)
        log = []

        async def watch():
            async with gate.slot(g.WATCH) as t:
                log.append((clock.t, gate.level))
                t.blocked()

        for _ in range(4):
            await clock.run([asyncio.ensure_future(watch())])
        return log, gate

    log, gate = run(main())
    assert [t for t, _ in log] == [0.0, 300.0, 900.0, 2100.0]  # 5분, 10분, 20분(상한), 다시 20분
    assert gate.level == 3 and gate.at_cap == 2


def test_platforms_are_independent():
    async def main():
        clock = FakeClock()
        a, b = make(clock), g.Gate("q", lambda: 30.0, clock=clock.now, sleep=clock.sleep, rand=lambda: 0.0)
        log = []

        async def req(gate, name):
            async with gate.slot(g.WATCH) as t:
                log.append((clock.t, name))
                if name == "a0":
                    t.blocked()

        tasks = [asyncio.ensure_future(req(a, "a0")), asyncio.ensure_future(req(b, "b0")),
                 asyncio.ensure_future(req(b, "b1"))]
        await clock.run(tasks)
        return log, a, b

    log, a, b = run(main())
    assert (0.0, "b0") in log and (30.0, "b1") in log and a.paused() and not b.paused()


def test_settings_ranges_and_warnings():
    assert g.validate_scan(10) == 10 and g.validate_gap(1) == 1
    for bad in (9, 3601, "x", True):
        with pytest.raises(ValueError):
            g.validate_scan(bad)
    for bad in (0.5, 601, None):
        with pytest.raises(ValueError):
            g.validate_gap(bad)
    assert g.warnings(30, 5) == {"scan": g.WARNING, "gap": None}
    assert g.warnings(31, 4.9) == {"scan": None, "gap": g.WARNING}
    assert g.cycle_seconds(10, 5) == 50
