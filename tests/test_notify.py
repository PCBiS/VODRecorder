"""텔레그램 알림. 가짜 서버로만 보낸다. 알림 지연, 알림 로그."""

import asyncio
import dataclasses
import time
from pathlib import Path

import pytest

from live_recorder import notify
from live_recorder.core import secretstore
from live_recorder.core.config import AppConfig, ConfigError, Settings

from conftest import CHANNEL, Harness, fast_timings

TOKEN = "123456:TEST-TOKEN-sentinel"


def texts(server) -> list[str]:
    return [m["text"] for m in server.tg_messages]


def test_events_are_delivered_with_safe_content(harness, server):
    """녹화 시작·종료·마무리 알림. 쿠키·토큰·재생 주소는 들어가지 않는다."""
    harness.add_channel()
    harness.enable_telegram(TOKEN, rewind_seconds=0)
    b = server.go_live(title="알림 방송")

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            await asyncio.sleep(0.6)
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")
            await harness.until(lambda: len(server.tg_messages) >= 3, what="알림 3건")

    asyncio.run(scenario())
    got = texts(server)
    assert any("녹화 시작" in t for t in got) and any("녹화 종료" in t and "녹화 시간" in t for t in got)
    assert any("마무리 완료" in t for t in got)
    for t in got:
        assert "채널: 시험채널" in t
        assert "http" not in t and "hdnts" not in t and "tok" not in t.split("파일:")[0] and TOKEN not in t
    assert any("파일: " in t and "알림 방송 1080p60.mp4" in t for t in got)
    assert all(m["chat_id"] == "-1009999" for m in server.tg_messages)
    assert all(p == f"/tg/bot{TOKEN}/sendMessage" for p in server.tg_paths)


def test_slow_telegram_does_not_delay_recording(harness, server):
    """응답하지 않는 텔레그램 서버. 그동안 녹화 시작과 마무리가 늦어지지 않는다."""
    harness.add_channel()
    harness.enable_telegram(rewind_seconds=0)
    server.tg_hold.clear()  # 응답을 붙잡는다(시험이 끝날 때까지. 30초짜리 지연과 같다)
    b = server.go_live()
    marks = {}

    async def scenario():
        async with harness:
            t0 = time.monotonic()
            await harness.until(harness.recording, timeout=5, what="알림이 막혀도 녹화 시작")
            marks["start"] = time.monotonic() - t0
            await asyncio.sleep(0.5)
            assert len(server.tg_paths) >= 1 and server.tg_messages == []  # 첫 알림이 붙잡혀 있다
            b.end()
            t1 = time.monotonic()
            await harness.until(lambda: harness.finalized(1), timeout=8, what="알림이 막혀도 마무리")
            marks["finalize"] = time.monotonic() - t1
            marks["queued"] = harness.engine.notifier._queue.qsize()
            t2 = time.monotonic()
        marks["stop"] = time.monotonic() - t2

    asyncio.run(scenario())
    assert marks["start"] < 3.0 and marks["finalize"] < 6.0, marks
    assert marks["queued"] >= 1  # 뒤따르는 알림은 대기열에서 기다린다
    assert marks["stop"] < 8.0, marks  # 종료도 전송을 기다리지 않는다
    assert harness.sessions()[0]["finalize_status"] == "done"


def test_failed_send_is_logged_without_token(harness, server):
    """전송 실패는 로그에 남고, 로그에 토큰이 든 주소가 없다."""
    harness.add_channel()
    harness.enable_telegram(TOKEN, rewind_seconds=0)
    server.tg_status = 500
    b = server.go_live()

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            await harness.until(lambda: harness.engine.notifier.failed >= 1, what="전송 실패")
            # 연결조차 안 되는 경우
            harness.engine.notifier.cfg = dataclasses.replace(harness.cfg, telegram_api_base="http://127.0.0.1:9/tg")
            ok, detail = await harness.engine.notifier.send_test()
            assert not ok and TOKEN not in detail and "127.0.0.1" not in detail
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")
            await harness.until(lambda: harness.engine.notifier.failed >= 2, what="두 번째 실패")

    asyncio.run(scenario())
    log = harness.log_text()
    assert '"event": "notify_failed"' in log and "HTTP 500" in log
    assert TOKEN not in log and "TEST-TOKEN" not in log and "/bot" not in log and "sendMessage" not in log
    for path in harness.data.rglob("*"):
        if path.is_file() and path != harness.cfg.telegram_path:
            assert TOKEN.encode() not in path.read_bytes(), path
    assert oct(harness.cfg.telegram_path.stat().st_mode & 0o777) == "0o600"


def test_switches_and_dedupe(tmp_path, server):
    """이벤트마다 켜고 끌 수 있고, 같은 이벤트·같은 채널은 정한 시간 안에 다시 보내지 않는다."""
    cfg = AppConfig(data_dir=tmp_path, record_root=tmp_path, telegram_api_base=server.base + "/tg", allow_http=True)
    secretstore.telegram_save(cfg.telegram_path, TOKEN, "1")

    async def scenario():
        n = notify.Notifier(cfg)
        n.start()
        on = Settings(notify_enabled=True, notify_dedupe_seconds=300)
        assert n.notify(on, "record_start", "ch1", "채널1") is True
        assert n.notify(on, "record_start", "ch1", "채널1") is False  # 중복 억제
        assert n.notify(on, "record_start", "ch2", "채널2") is True  # 다른 채널
        assert n.notify(on, "record_end", "ch1", "채널1") is True  # 다른 이벤트
        assert n.notify(Settings(notify_enabled=True, notify_dedupe_seconds=0), "record_start", "ch1", "채널1") is True  # 0이면 끔
        assert n.notify(Settings(notify_enabled=False), "start_failed", "ch3", "x") is False  # 전체 끔
        assert n.notify(Settings(notify_enabled=True, notify_start_failed=False), "start_failed", "ch3", "x") is False
        assert n.notify(on, "no_such_event", "ch3", "x") is False
        for _ in range(100):
            if len(server.tg_messages) >= 4:
                break
            await asyncio.sleep(0.05)
        # 테스트 전송은 스위치와 중복 억제를 무시한다
        for _ in range(2):
            ok, detail = await n.send_test()
            assert ok, detail
        await n.stop()

    asyncio.run(scenario())
    assert len(server.tg_messages) == 6
    assert sum("테스트 알림" in t for t in texts(server)) == 2


def test_enable_requires_token_and_chat_id(tmp_path, server, tools):
    """사용을 켰는데 둘 중 하나가 비면 저장을 거부한다."""
    h = Harness(tmp_path, server, tools)
    with pytest.raises(ConfigError):
        h.store.set_settings({"notify_enabled": True})
    secretstore.telegram_save(h.cfg.telegram_path, TOKEN, None)
    with pytest.raises(ConfigError):
        h.store.set_settings({"notify_enabled": True})
    assert h.store.get_settings().notify_enabled is False
    secretstore.telegram_save(h.cfg.telegram_path, None, "42")
    assert h.store.set_settings({"notify_enabled": True}).notify_enabled is True
    with pytest.raises(ValueError):
        secretstore.telegram_save(h.cfg.telegram_path, "bad/../token", None)


def test_only_telegram_host_is_contacted(tmp_path, server):
    """알림은 정해진 호스트로만 보낸다."""
    cfg = AppConfig(data_dir=tmp_path, record_root=tmp_path, telegram_api_base=server.base + "/tg", allow_http=False)
    secretstore.telegram_save(cfg.telegram_path, TOKEN, "1")

    async def scenario():
        out = []
        for base in (server.base + "/tg", "https://evil.example", "http://api.telegram.org"):
            n = notify.Notifier(dataclasses.replace(cfg, telegram_api_base=base))
            n.start()
            out.append(await n.send_test())
            await n.stop()
        return out

    results = asyncio.run(scenario())
    assert all(not ok and "허용하지 않는" in detail for ok, detail in results)
    assert server.tg_paths == []


def test_failures_notify(tmp_path, server, tools):
    """시작 실패, 지원하지 않는 형식, 타임머신 전환, 디스크 부족, 연속 실패, 마무리 실패."""
    free = {"bytes": 1000}
    fake_ffmpeg = tmp_path / "ffmpeg-fail"
    fake_ffmpeg.write_text('#!/bin/sh\ncase "$1" in -version) echo ffmpeg fake; exit 0;; esac\nexit 1\n')
    fake_ffmpeg.chmod(0o755)
    h = Harness(tmp_path, server, tools, disk_free=lambda p: free["bytes"], ffmpeg=str(fake_ffmpeg))
    h.add_channel()
    h.enable_telegram(rewind_seconds=0, notify_dedupe_seconds=0)
    b = server.go_live()

    async def scenario():
        async with h:
            await h.until(lambda: any("디스크 부족" in t for t in texts(server)), what="디스크 부족 알림")
            free["bytes"] = 10**12
            server.encrypted = True
            await h.until(lambda: any("녹화 시작 실패" in t and "지원하지 않는 스트림 형식" in t for t in texts(server)),
                          what="지원하지 않는 형식 알림")
            await h.until(lambda: any("연속 실패 3회" in t for t in texts(server)), what="연속 실패 알림")
            server.encrypted = False
            server.tm_fail = True
            await h.until(lambda: any("타임머신 실패" in t for t in texts(server)), what="타임머신 전환 알림")
            await h.until(h.recording, what="일반 수신 녹화")
            await asyncio.sleep(0.5)
            b.end()
            await h.until(lambda: any("마무리 실패" in t and "원본 보존" in t for t in texts(server)), what="마무리 실패 알림")

    asyncio.run(scenario())
    assert not any("http://" in t or "hdnts" in t for t in texts(server))
