"""사용자 조작."""

import asyncio
from pathlib import Path

from conftest import CHANNEL


def test_auto_record_off_then_on(harness, server):
    """끄면 진행 중인 녹화는 끝까지 하고 다음 감시를 하지 않는다. 다시 켜면 곧바로 재개한다."""
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 0})
    b = server.go_live(live_id="1001")

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            harness.store.update_channel(CHANNEL, auto_record=False)
            await asyncio.sleep(0.8)
            assert harness.recording() and harness.state()["state"] == "recording"  # 끝까지 한다
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")
            await harness.until(lambda: harness.state()["state"] == "stopped", what="정지 상태")
            server.go_live(live_id="1002")  # 꺼진 동안의 새 방송
            n = server.count("/api/")
            await asyncio.sleep(1.2)
            assert server.count("/api/") == n and len(harness.sessions()) == 1  # 감시하지 않는다
            harness.store.update_channel(CHANNEL, auto_record=True)
            await harness.until(lambda: server.count("/api/") > n, timeout=1.0, what="켠 직후 감시 재개")
            await harness.until(lambda: len(harness.sessions()) == 2 and harness.recording(), what="새 방송 녹화")

    asyncio.run(scenario())
    assert harness.sessions()[1]["live_id"] == "1002"


def test_manual_stop_then_next_broadcast(harness, server):
    """수동 중지 뒤에도 자동 녹화는 켜져 있고, 같은 방송은 건너뛰고 새 liveId에서 녹화한다."""
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 0})
    server.go_live(live_id="1001")

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            await asyncio.sleep(0.6)
            assert await harness.engine.manual_stop(CHANNEL) == "중지 요청함"
            done = await harness.until(lambda: harness.finalized(1), what="받은 데까지 마무리")
            assert done[0]["end_reason"] == "user_stop" and done[0]["finalize_status"] == "done"
            assert Path(done[0]["output_path"]).exists()
            assert harness.store.get_channel(CHANNEL).auto_record is True  # 설정은 그대로
            n = server.count("/api/")
            await asyncio.sleep(1.5)
            assert server.count("/api/") > n  # 감시는 계속 돈다
            assert len(harness.sessions()) == 1 and harness.state()["skip_live_id"] == "1001"  # 같은 방송은 다시 안 한다
            assert await harness.engine.manual_stop(CHANNEL) == "녹화 중이 아님"
            server.go_live(live_id="1002")
            await harness.until(lambda: len(harness.sessions()) == 2 and harness.recording(), what="다음 방송 녹화")

    asyncio.run(scenario())
    assert harness.sessions()[1]["live_id"] == "1002" and harness.state()["skip_live_id"] is None


def test_manual_start_when_auto_is_off(harness, server):
    """자동 녹화가 꺼진 채널도 방송 중이면 한 번 녹화한다. 녹화 중의 시작 요청은 아무것도 바꾸지 않는다."""
    harness.add_channel(auto_record=False)
    harness.store.set_settings({"rewind_seconds": 0})
    b = server.go_live(live_id="1001")

    async def scenario():
        async with harness:
            await asyncio.sleep(0.8)
            assert harness.sessions() == [] and harness.state()["state"] == "stopped"
            assert await harness.engine.manual_start(CHANNEL) == "확인 요청함"
            rec = await harness.until(harness.recording, what="수동 녹화")
            assert await harness.engine.manual_start(CHANNEL) == "이미 녹화 중"
            await asyncio.sleep(0.5)
            assert len(harness.sessions()) == 1 and harness.recording()["pid"] == rec["pid"]
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")
            server.go_live(live_id="1002")
            await asyncio.sleep(1.0)
            assert len(harness.sessions()) == 1  # 한 번만. 자동 녹화는 여전히 꺼져 있다
            assert harness.store.get_channel(CHANNEL).auto_record is False

    asyncio.run(scenario())


def test_remove_channel_stops_and_finalizes(harness, server):
    """채널을 지우면 진행 중인 녹화를 먼저 중지·마무리한다."""
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 0})
    server.go_live()

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            await asyncio.sleep(0.6)
            cid = harness.store.push_control(CHANNEL, "remove")  # CLI가 쓰는 경로
            await harness.until(lambda: harness.store.get_control(cid)["done_at"], what="삭제 처리")
            assert harness.store.get_channel(CHANNEL) is None
            done = await harness.until(lambda: harness.finalized(1), what="마무리")
            assert done[0]["end_reason"] == "user_stop" and Path(done[0]["output_path"]).exists()
            n = server.count("/api/")
            await asyncio.sleep(0.8)
            assert server.count("/api/") == n  # 감시가 끝났다

    asyncio.run(scenario())


def test_same_live_id_is_not_rerecorded_right_after_end(harness, server):
    """방송이 끝난 직후 같은 liveId가 다시 열려 보여도 유예 동안 녹화하지 않는다. liveId가 바뀌면 즉시 푼다."""
    harness.timings.post_end_suppress = 2.0
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 0})
    b = server.go_live(live_id="1001")

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            await asyncio.sleep(0.5)
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")
            b.api_open = True  # API가 잠깐 다시 OPEN으로 보인다
            await asyncio.sleep(1.0)
            assert len(harness.sessions()) == 1
            server.go_live(live_id="1002")
            await harness.until(lambda: len(harness.sessions()) == 2, timeout=1.5, what="새 liveId는 즉시 녹화")

    asyncio.run(scenario())


def test_channel_added_while_running_and_settings_apply(harness, server):
    """기동 뒤 추가한 채널도 감시하고, 설정은 재시작 없이 다음 녹화에 반영된다."""
    other = "f" * 32
    b = server.go_live(channel_id=other, live_id="2001", title="나중 채널")

    async def scenario():
        async with harness:
            harness.store.set_settings({"filename_pattern": "{channel_name}-{safe_live_title}", "rewind_seconds": 0})
            harness.store.add_channel(other, "새채널", "ch2", extension=".ts")
            await harness.until(lambda: harness.sessions(other) and harness.recording(), what="추가한 채널 녹화")
            await asyncio.sleep(0.5)
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")

    asyncio.run(scenario())
    out = Path(harness.sessions(other)[0]["output_path"])
    assert out.name == "새채널-나중 채널.ts" and out.parent.name == "ch2"
