"""실패와 재접속 (즉시 실패, 방송 중 끊김, 정체, 디스크 부족 등)."""

import asyncio
import sys
import time
from pathlib import Path

from live_recorder.platforms.chzzk import api as chzzk

from conftest import CHANNEL, Harness, duration, fast_timings, parse_iso


def test_receiver_fails_immediately(tmp_path, server, tools):
    """종료코드 1을 내는 가짜 도구. '녹화 종료'로 기록되지 않고 간격이 늘어난다."""
    stamp = tmp_path / "calls.txt"
    fake = tmp_path / "fake_receiver.py"
    fake.write_text("import sys, time\nopen(sys.argv[1], 'a').write(f'{time.time()}\\n')\nsys.exit(1)\n")
    h = Harness(tmp_path, server, tools, receiver_cmd=(sys.executable, str(fake), str(stamp)))
    h.add_channel()
    server.go_live()

    async def scenario():
        async with h:
            await h.until(lambda: len(h.sessions()) >= 4 and h.state()["state"] == "backoff"
                          and h.state()["error_count"] >= 4, what="네 번 시도 뒤 대기")
            return h.state()

    state = asyncio.run(scenario())
    sessions = h.sessions()
    assert all(s["end_reason"] == "error" and s["end_detail"].startswith("start_failed") for s in sessions), h.brief()
    assert all(s["exit_code"] == 1 and s["finalize_status"] == "skipped" and s["part_path"] is None for s in sessions)
    assert not any(s["end_reason"] == "broadcast_end" for s in sessions)
    assert list((h.root / "ch1").iterdir()) == []  # 빈 파일을 남기지 않는다
    times = [float(x) for x in stamp.read_text().split()]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps[0] >= 0.2 and gaps[1] >= 0.5 and gaps[2] >= 1.0, gaps  # 0.2 → 0.5 → 1.0
    assert gaps[0] < gaps[1] < gaps[2], gaps
    assert state["state"] == "backoff" and state["error_count"] >= 3 and state["error_flag"] == 1  # 정해진 횟수를 넘으면 오류 표시
    assert "start_failed" in state["error"]


def test_disconnect_then_resume_in_new_file(harness, server, tools):
    """방송 중에 끊었다 잇는다. 새 파일로 이어지고 이미 받은 구간은 다시 받지 않는다."""
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 20})
    b = server.go_live(initial=60)

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="첫 녹화")
            await asyncio.sleep(0.8)
            server.down = True
            await harness.until(lambda: any(s["ended_at"] for s in harness.sessions()), what="끊김 감지")
            await asyncio.sleep(0.6)
            server.down = False
            await harness.until(lambda: len([s for s in harness.sessions() if (s["bytes"] or 0) > 0 or harness._part_size(s)]) >= 2,
                                what="새 파일로 재개")
            await asyncio.sleep(0.8)
            b.end()
            await harness.until(lambda: harness.finalized(2) and not harness.recording(), what="두 파일 마무리")

    asyncio.run(scenario())
    data = [s for s in harness.sessions() if (s["bytes"] or 0) > 0]
    assert len(data) == 2, harness.brief()
    first, second = data
    assert first["live_id"] == second["live_id"] == "1001"
    assert first["end_reason"] == "error" and first["exit_code"] in (3, 4), first  # 방송 중 끊김은 '방송 종료'가 아니다
    assert second["end_reason"] == "broadcast_end"
    assert first["output_path"] != second["output_path"]
    assert all(s["finalize_status"] == "done" and Path(s["output_path"]).exists() for s in data), harness.brief()
    # 첫 파일은 되감기(20초)를 적용하고, 둘째 파일은 첫 파일이 끝난 지점부터 받는다
    detected = server.tm_calls[0]  # 감지 시점에 나와 있던 조각 수
    start = parse_iso(first["first_media_time"])
    assert b.pdt(detected - 21) <= start <= b.pdt(detected - 12), (detected, start)
    assert parse_iso(second["first_media_time"]) == parse_iso(first["last_media_time"])
    total = sum(duration(tools, Path(s["output_path"])) for s in data)
    assert abs(total - (b.pdt(b.published()) - start).total_seconds()) <= 3.0  # 겹침도 빈틈도 없다


def test_stall_is_detected_and_reconnected(tmp_path, server, tools):
    """조각을 더 내지 않는 서버. 정해진 시간 뒤 끝내고 다시 붙는다."""
    h = Harness(tmp_path, server, tools, timings=fast_timings(stall_override=1.0, stream_timeout=60.0))
    h.add_channel()
    h.store.set_settings({"rewind_seconds": 0})
    b = server.go_live()

    async def scenario():
        async with h:
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.5)
            b.freeze()  # 재생목록은 열리지만 새 조각이 없다
            t0 = time.monotonic()
            await h.until(lambda: any(s["end_reason"] == "stalled" for s in h.sessions()), what="정체 감지")
            took = time.monotonic() - t0
            await h.until(lambda: len(h.sessions()) >= 2, what="재접속 시도")
            await h.until(lambda: h.finalized(1), what="받은 구간 마무리")
            return took

    took = asyncio.run(scenario())
    first = h.sessions()[0]
    assert 0.9 <= took < 5.0, took
    assert first["end_reason"] == "stalled" and first["bytes"] > 0 and first["finalize_status"] == "done", h.brief()
    assert Path(first["output_path"]).exists()


def test_disk_low_blocks_new_recording(tmp_path, server, tools):
    """여유 공간이 모자라면 새 녹화를 시작하지 않고 오류로 표시한다."""
    free = {"bytes": 1000}
    h = Harness(tmp_path, server, tools, disk_free=lambda path: free["bytes"])
    h.add_channel()
    b = server.go_live()

    async def scenario():
        async with h:
            await h.until(lambda: h.state()["error_flag"] == 1, what="오류 표시")
            await asyncio.sleep(1.0)
            assert h.sessions() == [] and server.count("/hls/") == 0  # 수신을 아예 시작하지 않았다
            state = h.state()
            assert "디스크" in state["error"] and state["state"] == "backoff"
            free["bytes"] = 100 * 1024**3
            await h.until(h.recording, what="공간이 생기면 녹화")
            free["bytes"] = 1000  # 진행 중인 녹화는 멈추지 않는다
            await asyncio.sleep(0.8)
            assert h.recording() and h.state()["state"] == "recording"
            b.end()
            await h.until(lambda: h.finalized(1), what="마무리")

    asyncio.run(scenario())
    assert h.sessions()[0]["end_reason"] == "broadcast_end"


def test_worker_survives_unexpected_exception(harness, server):
    """예상하지 못한 예외가 나도 감시가 죽지 않는다."""
    harness.add_channel()

    async def scenario():
        async with harness:
            real = harness.engine.client_for("chzzk").live_detail
            calls = {"n": 0}

            async def flaky(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise RuntimeError("boom")
                return await real(*args, **kwargs)

            harness.engine.client_for("chzzk").live_detail = flaky
            await harness.until(lambda: calls["n"] >= 2 and harness.state()["error"], what="예외 기록")
            assert "boom" in harness.state()["error"]
            server.go_live()
            await harness.until(harness.recording, what="예외 뒤에도 녹화")

    asyncio.run(scenario())
    assert "worker_exception" in harness.log_text()


def test_api_retry_and_unknown_is_not_offline(harness, server):
    """5xx는 다시 요청하고, 판정 불가는 방송 종료로 취급하지 않는다."""
    harness.add_channel()
    b = server.go_live()

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            # 수신기가 끝난 뒤의 상태 확인이 판정 불가여도 방송이 끝났다고 보지 않는다
            server.detail_queue[:] = [(503, None)] * 6
            b.freeze()
            b.ended = True  # ENDLIST만 붙고 API는 계속 열림
            await harness.until(lambda: any(s["ended_at"] for s in harness.sessions()), what="수신 종료")
            await asyncio.sleep(0.5)
            assert harness.engine.suppress == {}  # 종료 처리(유예)를 하지 않았다
            # 종료 표시 뒤 기다리는 동안과 그 뒤 재확인 중에는 감시로 보인다
            assert harness.state()["state"] in ("backoff", "recording", "watching")

        client = chzzk.ChzzkClient(server.base + "/api", ("127.0.0.1",), allow_http=True,
                                   retry=chzzk.RetryPolicy(first_delay=0.05))
        try:
            server.detail_queue[:] = [(503, None), (429, None)]
            n = server.count("/api/")
            assert (await client.live_detail(CHANNEL)).verdict is chzzk.Verdict.LIVE
            assert server.count("/api/") - n == 3  # 두 번 실패, 세 번째 성공
            server.detail_queue[:] = [(500, None)] * 3
            assert (await client.live_detail(CHANNEL)).verdict is chzzk.Verdict.UNKNOWN
            server.broadcasts.pop(CHANNEL)
            assert (await client.live_detail(CHANNEL)).verdict is chzzk.Verdict.OFFLINE  # 404
            server.broadcasts[CHANNEL] = None
            assert (await client.live_detail(CHANNEL)).verdict is chzzk.Verdict.OFFLINE  # 방송한 적 없는 채널
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_metadata_settles_before_recording(harness, server):
    """방송은 열렸는데 제목과 재생 정보가 아직 비어 있으면 다시 조회해 확정한다."""
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 0})
    b = server.go_live(title="확정된 제목")
    empty = {"code": 200, "content": {"status": "OPEN", "liveId": 1001, "liveTitle": "", "livePlaybackJson": None}}
    server.detail_queue[:] = [(200, empty), (200, empty)]

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            await asyncio.sleep(0.5)
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")

    asyncio.run(scenario())
    s = harness.sessions()[0]
    assert s["title"] == "확정된 제목" and "확정된 제목" in Path(s["output_path"]).name


def test_adult_without_playback_is_a_start_failure(harness, server):
    """재생 정보가 없는 방송(쿠키 없는 성인 방송)은 시작 실패로 남는다."""
    harness.add_channel()
    server.go_live()
    locked = {"code": 200, "content": {"status": "OPEN", "liveId": 1001, "liveTitle": "성인", "adult": True,
                                       "openDate": "2026-11-11 12:00:00", "livePlaybackJson": None}}
    server.detail_queue[:] = [(200, locked)] * 40
    server.tm_fail = True

    async def scenario():
        async with harness:
            await harness.until(lambda: len(harness.sessions()) >= 2, what="시작 실패 기록")

    asyncio.run(scenario())
    s = harness.sessions()[0]
    assert s["end_reason"] == "error" and "start_failed" in s["end_detail"] and "쿠키" in s["end_detail"]


def test_unknown_verdict_is_rechecked_sooner(tmp_path, server, tools):
    """판정 불가면 확인 주기를 다 기다리지 않고 짧은 간격으로 몇 번 더 확인한다."""
    h = Harness(tmp_path, server, tools, timings=fast_timings(check_interval_override=30.0, unknown_recheck_interval=0.2,
                                                              unknown_recheck_max=3))
    h.add_channel()
    server.broadcasts[CHANNEL] = None
    server.detail_queue[:] = [(500, None)] * 9  # 세 번의 확인이 각각 3회 재시도 끝에 판정 불가

    async def scenario():
        async with h:
            h.engine.client_for("chzzk").retry = chzzk.RetryPolicy(first_delay=0.01)
            await h.until(lambda: server.count("/api/") >= 10, timeout=6, what="짧은 간격 재확인")
            assert h.state()["error"] is None and h.state()["state"] == "watching"  # 오류로 세지 않는다
            n = server.count("/api/")
            await asyncio.sleep(1.0)
            assert server.count("/api/") == n  # 판정이 났으면 원래 주기(30초)로 돌아간다

    asyncio.run(scenario())


def test_never_broadcast_channel_is_not_an_error(harness, server):
    """200에 content가 null인 채널은 방송 중이 아닌 것으로 보고 감시를 계속한다."""
    harness.add_channel()
    server.broadcasts[CHANNEL] = None

    async def scenario():
        async with harness:
            await harness.until(lambda: server.count("/api/") >= 3, what="감시 계속")
            state = harness.state()
            assert state["state"] == "watching" and not state["error"] and state["last_check"].startswith("offline")
            server.go_live()
            await harness.until(harness.recording, what="첫 방송 녹화")

    asyncio.run(scenario())
    assert harness.sessions()[0]["time_machine_active"] == 1  # 세션 기록에 남긴다


def test_stale_playback_is_not_confirmed(harness, server):
    """재생 정보의 meta.liveId가 지난 방송의 것이면 다시 조회해 맞는 값으로 녹화한다."""
    import json as _json
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 0})
    b = server.go_live(title="새 방송")
    _, fresh = server.live_detail(CHANNEL)
    stale = _json.loads(_json.dumps(fresh))
    pb = _json.loads(stale["content"]["livePlaybackJson"])
    pb["meta"]["liveId"] = "999"
    stale["content"]["livePlaybackJson"] = _json.dumps(pb)
    stale["content"]["liveTitle"] = "지난 방송 제목"
    server.detail_queue[:] = [(200, stale), (200, stale)]

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            await asyncio.sleep(0.4)
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")

    asyncio.run(scenario())
    assert harness.sessions()[0]["title"] == "새 방송"
