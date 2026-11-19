"""타임머신 (늦은 감지, 재생 주소 만료, 타임머신 API 실패)."""

import asyncio
from pathlib import Path

from conftest import CHANNEL, duration, parse_iso


def _record(harness, server, b, seconds=0.8):
    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            await asyncio.sleep(seconds)
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")

    asyncio.run(scenario())
    return harness.sessions()[0]


def test_late_detection_starts_before_detection(harness, server, tools):
    """방송 시작 뒤 한참 지나 감시를 켠다. 파일이 감지 시점보다 앞에서 시작한다."""
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 60})
    b = server.go_live(initial=150, rate=5)  # 이미 150초가 지난 방송
    s = _record(harness, server, b)
    detected = server.tm_calls[0]
    start = parse_iso(s["first_media_time"])
    assert s["source"] == "timemachine"
    # 감지 시점에서 60초 앞. API 응답과 재생목록 요청 사이에 나온 조각 몇 개만큼은 늦을 수 있다
    assert b.pdt(detected - 61) <= start <= b.pdt(detected - 54), (detected, start)
    assert start < b.pdt(detected)
    out = Path(s["output_path"])
    assert duration(tools, out) >= 60.0  # 감지 전 60초가 파일에 들어 있다
    assert abs(duration(tools, out) - (b.pdt(b.published()) - start).total_seconds()) <= 2.0


def test_rewind_longer_than_broadcast_starts_at_beginning(harness, server, tools):
    """되감기가 방송 길이보다 길면 방송 처음부터 받는다."""
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 3600})
    b = server.go_live(initial=40, rate=5)
    s = _record(harness, server, b)
    assert parse_iso(s["first_media_time"]) == b.pdt(0)
    assert abs(duration(tools, Path(s["output_path"])) - b.published()) <= 2.0


def test_rewind_zero_and_timemachine_off_start_at_live_edge(harness, server):
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 0})
    b = server.go_live(initial=100, rate=5)
    s = _record(harness, server, b)
    assert parse_iso(s["first_media_time"]) >= b.pdt(server.tm_calls[0] - 4)
    assert s["source"] == "timemachine"


def test_timemachine_disabled_uses_live(harness, server):
    harness.add_channel()
    harness.store.set_settings({"timemachine_enabled": False})
    b = server.go_live(initial=100, rate=5)
    s = _record(harness, server, b)
    assert s["source"] == "live" and s["note"] is None and server.tm_calls == []
    assert parse_iso(s["first_media_time"]) >= b.pdt(96)


def test_playback_url_expiry_is_refreshed_without_splitting(harness, server, tools):
    """재생목록이 403을 낸다. 새 주소로 바뀌고 같은 파일에 빈틈 없이 이어진다."""
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 10})
    b = server.go_live(initial=30, rate=10)

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            await asyncio.sleep(0.6)
            server.expire_tokens()  # 지금 쓰는 주소의 재생목록과 조각이 전부 403
            calls = len(server.tm_calls)
            await harness.until(lambda: len(server.tm_calls) > calls, what="주소 갱신 호출")
            await asyncio.sleep(1.0)
            server.expire_tokens()  # 한 번 더
            await asyncio.sleep(1.0)
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")

    asyncio.run(scenario())
    sessions = harness.sessions()
    assert len(sessions) == 1, harness.brief()  # 파일이 나뉘지 않는다
    s = sessions[0]
    assert s["end_reason"] == "broadcast_end" and s["finalize_status"] == "done", harness.brief()
    span = (parse_iso(s["last_media_time"]) - parse_iso(s["first_media_time"])).total_seconds()
    assert span == s["media_seconds"]  # 받은 조각 길이의 합 = 처음부터 끝까지의 시간. 빠진 구간이 없다
    assert parse_iso(s["last_media_time"]) == b.pdt(b.published())  # 방송 끝까지 받았다
    assert abs(duration(tools, Path(s["output_path"])) - span) <= 2.0
    log = harness.log_text()
    assert log.count('"event": "url_refresh"') >= 2 and "segment_gap" not in log


def test_scheduled_refresh_keeps_same_file(harness, server):
    """정해진 시간이 지나면 끊기기 전에 주소를 바꾼다."""
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 0})
    b = server.go_live(rate=10)

    async def scenario():
        async with harness:
            # 설정 하한(60초)보다 짧게 시험하려고 수신기에 넘기는 값을 직접 줄인다
            real = harness.store.get_settings

            def patched():
                import dataclasses
                return dataclasses.replace(real(), url_refresh_seconds=1)

            harness.engine.store.get_settings = patched
            await harness.until(harness.recording, what="녹화")
            await harness.until(lambda: len(server.tm_calls) >= 3, what="주기 갱신")
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")

    asyncio.run(scenario())
    s = harness.sessions()
    assert len(s) == 1 and s[0]["finalize_status"] == "done"
    span = (parse_iso(s[0]["last_media_time"]) - parse_iso(s[0]["first_media_time"])).total_seconds()
    assert span == s[0]["media_seconds"]


def test_timemachine_api_failure_falls_back_to_live(harness, server, tools):
    """타임머신 API가 오류를 낸다. 일반 수신으로 녹화를 시작하고 세션에 남긴다."""
    harness.add_channel()
    server.tm_fail = True
    b = server.go_live(initial=100, rate=5)
    s = _record(harness, server, b)
    assert s["source"] == "live" and "타임머신 실패" in s["note"], s
    assert s["end_reason"] == "broadcast_end" and s["finalize_status"] == "done"
    assert Path(s["output_path"]).exists() and duration(tools, Path(s["output_path"])) > 2
    assert "timemachine_fallback" in harness.log_text()
