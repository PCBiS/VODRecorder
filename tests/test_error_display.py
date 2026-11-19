"""채널의 오류 표시는 지금 상태다. 지난 실패는 세션 기록과 로그에 남고 채널 상태에 붙어 있지 않는다."""

import asyncio

from conftest import CHANNEL, Harness

STALE = "start_failed: 데이터 없이 종료 rc=0 (endlist)"


def stale(h):
    """지난 실행(예전 빌드)이 저장해 둔 낡은 표시."""
    h.store.set_state(CHANNEL, "backoff", platform="chzzk", error=STALE, error_count=3, error_flag=1)


def test_stale_error_is_cleared_by_first_offline_check(harness, server):
    harness.add_channel()
    stale(harness)
    assert harness.state()["error"] == STALE

    async def scenario():
        async with harness:
            await harness.until(lambda: (harness.state()["last_check"] or "").startswith("offline"), what="첫 확인")
            await harness.until(lambda: harness.state()["error"] is None, what="낡은 표시 지움")
            s = harness.state()
            assert s["error_count"] == 0 and s["error_flag"] == 0 and s["state"] == "watching"

    asyncio.run(scenario())


def test_stale_error_is_cleared_when_recording_starts(harness, server):
    """증상: 녹화 중인데 사흘 전 헛시작의 문구가 남아 있었다. 첫 데이터가 오면 지운다(파일이 끝날 때까지 기다리지 않는다)."""
    harness.add_channel()
    stale(harness)
    server.go_live()

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            await harness.until(lambda: harness.state()["error"] is None, what="녹화 중 지움")
            s = harness.state()
            assert s["state"] == "recording" and s["error_flag"] == 0 and s["error_count"] == 0

    asyncio.run(scenario())


def test_failure_then_success_clears(harness, server):
    """감시 중 예외(연속 실패)가 풀려 정상 확인되면 지운다. 지난 실패는 로그에 남는다."""
    harness.add_channel()

    async def scenario():
        async with harness:
            client = harness.engine.client_for("chzzk")
            real = client.live_detail
            calls = {"n": 0}

            async def flaky(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise RuntimeError("boom")
                return await real(*args, **kwargs)

            client.live_detail = flaky
            await harness.until(lambda: "boom" in (harness.state()["error"] or ""), what="실패 표시")
            await harness.until(lambda: calls["n"] >= 3 and harness.state()["error"] is None, what="정상 확인 뒤 지움")

    asyncio.run(scenario())
    assert "worker_exception" in harness.log_text()


def test_valid_problem_stays_until_its_cause_is_gone(tmp_path, server, tools):
    """디스크 부족은 방송이 끝나 방송 없음으로 확인돼도 남고, 여유 공간이 돌아오면 지운다."""
    free = {"bytes": 1000}
    h = Harness(tmp_path, server, tools, disk_free=lambda path: free["bytes"])
    h.add_channel()
    b = server.go_live()

    async def scenario():
        async with h:
            await h.until(lambda: "디스크" in (h.state()["error"] or ""), what="디스크 부족 표시")
            b.end()
            server.broadcasts[CHANNEL] = None
            await h.until(lambda: (h.state()["last_check"] or "").startswith("offline"), what="방송 없음 확인")
            n = server.count("/api/")
            await h.until(lambda: server.count("/api/") >= n + 2, what="확인 두 번 더")
            assert "디스크" in h.state()["error"] and h.state()["error_flag"] == 1  # 원인이 그대로다
            free["bytes"] = 100 * 1024**3
            await h.until(lambda: h.state()["error"] is None, what="공간이 생기면 지움")

    asyncio.run(scenario())


def test_status_cli_and_api_show_the_same_cleared_value(harness, server, capsys):
    """화면·`status` CLI·`/api/channels`는 같은 저장값을 읽는다. 지워지면 CLI 줄에 오류가 없다."""
    from argparse import Namespace

    from live_recorder import cli

    harness.add_channel()
    stale(harness)
    cli.cmd_status(harness.cfg, Namespace(json=False))
    assert STALE in capsys.readouterr().out

    async def scenario():
        async with harness:
            await harness.until(lambda: harness.state()["error"] is None, what="지움")

    asyncio.run(scenario())
    cli.cmd_status(harness.cfg, Namespace(json=False))
    assert "오류" not in capsys.readouterr().out
