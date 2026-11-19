"""종료 표시(endlist) 뒤의 대기와 빈 재시작.

가짜 서버의 재생목록은 끝났는데 API는 아직 방송 중이라고 답하는 상황을 만든다. 대기 시간은 시험용 값
(`Timings.endlist_wait_override`)으로 줄이고, 5분 안전장치는 기억해 둔 시각을 앞당겨(가짜 시계) 본다.
"""

import asyncio
import time

import pytest

from live_recorder.core import store as st
from live_recorder.core.config import ConfigError, Settings
from live_recorder.recording.engine import Timings

from conftest import CHANNEL, Harness, fast_timings, parse_iso


def _h(tmp_path, server, tools, **timing):
    h = Harness(tmp_path, server, tools, timings=fast_timings(**timing))
    h.add_channel()
    h.store.set_settings({"rewind_seconds": 0})
    return h


def _end_playlist_only(b):
    """재생목록에 종료 표시를 붙이되 API는 방송 중으로 둔다."""
    b.freeze()
    b.ended = True


def test_defaults_and_validation():
    assert Settings().endlist_wait_seconds == 15 and Timings().endlist_grace == 300.0  # 15초, 5분
    for bad in (-1, 601, "x"):
        with pytest.raises(ConfigError):
            Settings.validate("endlist_wait_seconds", bad)
    assert Settings.validate("endlist_wait_seconds", 0) == 0 and Settings.validate("endlist_wait_seconds", "30") == 30


def test_a_api_closes_during_wait_means_no_restart(tmp_path, server, tools):
    """(a) 대기 안에 API가 종료로 바뀌면 다시 여는 시도가 없다."""
    h = _h(tmp_path, server, tools, endlist_wait_override=2.0)
    b = server.go_live()

    async def scenario():
        async with h:
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.8)
            _end_playlist_only(b)
            await h.until(lambda: h.sessions()[0]["ended_at"], what="종료 표시로 끝남")
            await asyncio.sleep(0.5)
            b.api_open = False  # 실측처럼 API가 몇 초 늦게 종료로 바뀐다
            await h.until(lambda: h.finalized(1), what="마무리")
            await asyncio.sleep(2.5)

    asyncio.run(scenario())
    assert len(server.tm_calls) == 1, server.tm_calls  # 타임머신 API(=녹화 시작)는 처음 한 번뿐
    assert [s["end_reason"] for s in h.sessions()] == ["broadcast_end"]


def test_b_still_live_after_wait_resumes_after_last_segment(tmp_path, server, tools):
    """(b) 기다린 뒤에도 방송 중이고 새 조각이 있으면 새 파일로 잇고, 마지막 조각 뒤부터 받는다."""
    h = _h(tmp_path, server, tools, endlist_wait_override=1.0)
    b = server.go_live(initial=10)

    async def scenario():
        async with h:
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.8)
            _end_playlist_only(b)
            await h.until(lambda: h.sessions()[0]["ended_at"], what="첫 파일 끝")
            b.ended, b.frozen = False, None  # 대기하는 동안 방송이 이어진다(조각이 다시 나온다)
            await h.until(lambda: len([s for s in h.sessions() if s["bytes"] or h._part_size(s)]) >= 2, what="이어 받기")
            await asyncio.sleep(0.8)
            b.end()
            await h.until(lambda: h.finalized(2) and not h.recording(), what="두 파일 마무리")

    asyncio.run(scenario())
    first, second = [s for s in h.sessions() if s["bytes"]]
    assert first["end_reason"] == second["end_reason"] == "broadcast_end"
    assert first["live_id"] == second["live_id"]
    # 앞 파일의 마지막 조각 바로 뒤부터다(되감기 없음, 겹침 없음)
    assert parse_iso(second["first_media_time"]) == parse_iso(first["last_media_time"]), (first, second)


def test_c_empty_restarts_after_suppress_are_not_failures(tmp_path, server, tools):
    """(c) 유예가 지난 뒤의 빈 재시작도 실패·알림·오류 세션이 없다."""
    h = _h(tmp_path, server, tools, endlist_wait_override=0.2, post_end_suppress=0.5)
    h.enable_telegram()
    b = server.go_live()
    states = []

    async def scenario():
        async with h:
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.8)
            _end_playlist_only(b)
            await h.until(lambda: h.sessions()[0]["ended_at"], what="첫 파일 끝")
            for _ in range(80):  # 4초 동안 유예가 여러 번 지난다
                states.append((h.state()["state"], h.state()["error_count"]))
                await asyncio.sleep(0.05)
            b.api_open = False
            await h.until(lambda: h.finalized(1), what="마무리")
            await asyncio.sleep(0.5)

    asyncio.run(scenario())
    assert [s["end_reason"] for s in h.sessions()] == ["broadcast_end"], h.brief()
    assert h.log_text().count("endlist_again") >= 3  # 유예가 지난 뒤에도 여러 번 열었고 모두 방송 종료로 봤다
    assert all(state != st.BACKOFF and count == 0 for state, count in states), states
    assert "시작 실패" not in " ".join(m.get("text", "") for m in server.tg_messages)


def test_d_after_five_minutes_empty_restart_is_start_failure(tmp_path, server, tools):
    """(d) 종료 표시 뒤 5분이 지나도 같은 liveId가 방송 중이고 조각이 없으면 시작 실패로 센다."""
    h = _h(tmp_path, server, tools, endlist_wait_override=0.2, post_end_suppress=0.3)
    b = server.go_live()

    async def scenario():
        async with h:
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.8)
            _end_playlist_only(b)
            await h.until(lambda: h.sessions()[0]["ended_at"], what="첫 파일 끝")
            await asyncio.sleep(0.8)
            assert not [s for s in h.sessions() if s["end_reason"] == st.END_ERROR]  # 5분 안에는 실패가 없다
            worker = h.engine.workers[("chzzk", CHANNEL)]
            seen_at, last = worker.endlist_seen["1001"]
            worker.endlist_seen["1001"] = (seen_at - 301.0, last)  # 가짜 시계: 5분 1초가 지났다
            await h.until(lambda: [s for s in h.sessions() if s["end_reason"] == st.END_ERROR], what="시작 실패")
            b.api_open = False

    asyncio.run(scenario())
    failed = [s for s in h.sessions() if s["end_reason"] == st.END_ERROR]
    assert failed and failed[0]["end_detail"].startswith("start_failed"), h.brief()


def test_e_stop_and_shutdown_during_wait_are_immediate(tmp_path, server, tools):
    """(e) 대기 중 사용자 중지와 서버 종료에 바로 반응한다."""
    h = _h(tmp_path, server, tools, endlist_wait_override=60.0)
    b = server.go_live()

    async def scenario():
        async with h:
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.8)
            _end_playlist_only(b)
            await h.until(lambda: h.sessions()[0]["ended_at"], what="첫 파일 끝")
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            assert await h.engine.manual_stop(CHANNEL) == "중지 요청함"
            await h.until(lambda: h.state()["skip_live_id"] == "1001", timeout=3, what="중지 반영")
            stop_took = time.monotonic() - t0
            # 같은 방송은 건너뛰므로 다시 녹화하지 않는다. 새 방송으로 다시 대기에 들어간 뒤 서버를 끈다
            server.go_live(live_id="1002")
            b2 = server.broadcasts[CHANNEL]
            await h.until(h.recording, what="새 방송 녹화")
            await asyncio.sleep(0.8)
            _end_playlist_only(b2)
            await h.until(lambda: len([s for s in h.sessions() if s["ended_at"]]) >= 2, what="둘째 파일 끝")
            await asyncio.sleep(0.3)
            t1 = time.monotonic()
            await h.engine.stop()
            return stop_took, time.monotonic() - t1

    stop_took, shutdown_took = asyncio.run(scenario())
    assert stop_took < 2.0 and shutdown_took < 5.0, (stop_took, shutdown_took)


def test_f_disconnect_reconnects_without_waiting(tmp_path, server, tools):
    """(f) 종료 표시가 아닌 끊김은 기다리지 않고 곧바로 다시 잇는다(대기를 60초로 둬도)."""
    h = _h(tmp_path, server, tools, endlist_wait_override=60.0)
    b = server.go_live(initial=30)

    async def scenario():
        async with h:
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.8)
            server.down = True
            await h.until(lambda: h.sessions()[0]["ended_at"], what="끊김")
            server.down = False
            t0 = time.monotonic()
            await h.until(lambda: len([s for s in h.sessions() if s["bytes"] or h._part_size(s)]) >= 2, timeout=10,
                          what="곧바로 재연결")
            took = time.monotonic() - t0
            b.end()
            return took

    took = asyncio.run(scenario())
    assert took < 10.0 and h.sessions()[0]["end_reason"] == "error"


def test_g_wait_zero_rechecks_immediately(tmp_path, server, tools):
    """(g) 설정 0이면 기다리지 않고 곧바로 다시 확인한다(빈 재시작은 방송 종료로 본다)."""
    h = _h(tmp_path, server, tools, endlist_wait_override=None, post_end_suppress=30.0)
    h.store.set_settings({"endlist_wait_seconds": 0})
    b = server.go_live()

    async def scenario():
        async with h:
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.8)
            _end_playlist_only(b)
            await h.until(lambda: h.sessions()[0]["ended_at"], what="첫 파일 끝")
            t0 = time.monotonic()
            await h.until(lambda: len(server.tm_calls) >= 2, timeout=5, what="곧바로 다시 열기")
            took = time.monotonic() - t0
            await h.until(lambda: "endlist_again" in h.log_text(), what="빈 재시작")
            b.api_open = False
            return took

    took = asyncio.run(scenario())
    assert took < 2.0, took
    assert [s["end_reason"] for s in h.sessions()] == ["broadcast_end"]
