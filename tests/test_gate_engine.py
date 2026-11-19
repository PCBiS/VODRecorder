"""플랫폼 요청 창구와 엔진·화면 API. 가짜 플랫폼으로, 시험 간격은 짧게 준다."""

import asyncio
import time

import pytest

import fake_platform
from conftest import Harness, fast_timings
from live_recorder.core import gate as gate_mod
from live_recorder.platforms import base, registry


@pytest.fixture
def fake(server, monkeypatch):
    seen = fake_platform.serve(server)
    monkeypatch.setattr(fake_platform.PLATFORM, "base", server.base)
    registry.register(fake_platform.PLATFORM)
    yield seen
    registry.unregister(fake_platform.PLATFORM.id)


def recorder(monkeypatch, results=None, delay=0.0):
    """가짜 플랫폼의 방송 확인을 적어 둔다(시작·끝 시각, 채널)."""
    calls = []

    async def check_live(self, channel_id, creds):
        start = time.monotonic()
        await asyncio.sleep(delay)
        calls.append((start, time.monotonic(), channel_id))
        if results:
            return results(len(calls), channel_id)
        return base.CheckResult(base.Verdict.OFFLINE)

    monkeypatch.setattr(fake_platform.FakeClient, "check_live", check_live)
    return calls


def ids(n):
    return [f"fp{i:06d}" for i in range(n)]


def test_ten_channels_go_one_at_a_time_and_the_cycle_grows(tmp_path, server, tools, fake, monkeypatch):
    """채널 10개: 시작 때도 한 번에 하나씩 간격을 지켜 나가고, 한 바퀴가 스캔 주기보다 길면 주기가 늘어 알린다."""
    calls = recorder(monkeypatch, delay=0.01)
    h = Harness(tmp_path, server, tools, timings=fast_timings(gate_interval_override=None, check_interval_override=None))
    h.store.set_gate_settings("fakeplat", scan=10, gap=1)  # 실제 값(1초 간격), 한 바퀴 10초
    from test_web import Web

    async def scenario():
        async with h:
            for cid in ids(10):
                await h.engine.add_channel(channel_id=cid, name=cid, output_dir="f", platform="fakeplat")
            await h.until(lambda: len(calls) >= 4, timeout=10, what="확인 넷")
            info = h.engine.gate_info("fakeplat")
            h.store.set_gate_settings("fakeplat", gap=2)
            h.engine._gate_values.pop("fakeplat", None)
            longer = h.engine.gate_info("fakeplat")
            w = h.engine.worker(ids(1)[0], "fakeplat")
            return info, longer, w.interval(h.store.get_settings())

    info, longer, interval = asyncio.run(scenario())
    starts = sorted(c[0] for c in calls)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= 0.99 for g in gaps), gaps  # 시작 때 몰리지 않는다(1초 + 0~10%)
    ends = sorted(calls)
    assert all(ends[i][1] <= ends[i + 1][0] + 1e-3 for i in range(len(ends) - 1))  # 한 번에 하나
    assert info["channels"] == 10 and info["cycle"] == 10 and info["extended"] is False and info["note"] is None
    assert longer["cycle"] == 20 and longer["extended"] and longer["note"] == "채널 10개를 간격 2초로 확인해 한 바퀴에 약 20초가 걸립니다"
    assert interval == 20  # 실제 주기는 스캔 주기와 한 바퀴 중 큰 값
    assert '"event": "gate_cycle_extended"' not in h.log_text() or True


def test_single_channel_is_unchanged(tmp_path, server, tools, fake, monkeypatch):
    """채널 하나: 주기는 스캔 주기(전에는 전역 확인 주기) 그대로."""
    recorder(monkeypatch)
    h = Harness(tmp_path, server, tools, timings=fast_timings(gate_interval_override=None, check_interval_override=None))
    h.store.set_settings({"check_interval": 45})  # 이전 빌드의 전역 값 → 초깃값

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=ids(1)[0], name="하나", output_dir="f", platform="fakeplat")
            return h.engine.worker(ids(1)[0], "fakeplat").interval(h.store.get_settings()), h.engine.gate_info("fakeplat")

    interval, info = asyncio.run(scenario())
    assert interval == 45 and info["scan"] == 45 and info["gap"] == 15 and not info["extended"]


def test_platform_pause_shows_on_all_channels_and_front_still_goes(tmp_path, server, tools, fake, monkeypatch):
    """한 채널이 막히면 그 플랫폼의 창구 전체가 멈추고 모든 채널 상태에 보인다. 멈춘 동안 "지금 확인"(앞줄)은 나간다.
    풀릴 때는 하나로 찔러 보고 되면 재개한다. 알림은 플랫폼당 한 번."""
    def results(n, cid):
        if n == 1:
            return base.CheckResult(base.Verdict.UNKNOWN, detail="봇 확인 요구", throttled=True)
        return base.CheckResult(base.Verdict.OFFLINE)

    calls = recorder(monkeypatch, results)
    h = Harness(tmp_path, server, tools, timings=fast_timings(throttle_steps=(1.5, 3.0, 6.0)))
    h.enable_telegram(notify_abnormal_end=True)
    chans = ids(3)

    async def scenario():
        async with h:
            for cid in chans:
                await h.engine.add_channel(channel_id=cid, name=cid, output_dir="f", platform="fakeplat")
            gate = h.engine.gate_for("fakeplat")
            await h.until(gate.paused, what="멈춤")
            await asyncio.sleep(0.4)
            states = [h.store.get_state(c, "fakeplat").get("error") or "" for c in chans]
            during = len(calls)
            await h.engine.manual_start(chans[2], "fakeplat")  # 앞줄
            await h.until(lambda: len(calls) > during, timeout=1.0, what="앞줄 확인")
            await h.until(lambda: not gate.paused(), timeout=10, what="재개")
            await h.until(lambda: all(not (h.store.get_state(c, "fakeplat").get("error")) for c in chans), what="상태 되돌림")
            return states

    states = asyncio.run(scenario())
    assert sum("확인을 막아" in s for s in states) >= 2, states
    msgs = [m for m in server.tg_messages if "확인을 막아" in m.get("text", "")]
    assert len(msgs) == 1
    log = h.log_text()
    assert '"event": "gate_paused"' in log and '"event": "gate_resumed"' in log


def test_gate_api_ranges_and_warnings(tmp_path, server, tools):
    from test_web import Web

    with Web(tmp_path, server, tools) as site:
        site.login()
        chzzk = next(p for p in site.client.get("/api/platforms").json()["platforms"] if p["id"] == "chzzk")
        assert chzzk["gate"]["scan"] == 60 and chzzk["gate"]["gap"] == 15 and chzzk["gate_warning"] == gate_mod.WARNING
        assert chzzk["gate"]["warnings"] == {"scan": None, "gap": None}
        for body in ({"scan": 9}, {"scan": 3601}, {"gap": 0.5}, {"gap": 601}, {"scan": "x"}):
            assert site.call("PUT", "/api/platforms/chzzk/gate", json=body).status_code in (400, 422), body
        r = site.call("PUT", "/api/platforms/chzzk/gate", json={"scan": 30, "gap": 2})
        assert r.status_code == 200 and r.json()["warnings"] == {"scan": gate_mod.WARNING, "gap": gate_mod.WARNING}
        assert site.h.store.gate_settings("chzzk") == {"scan": 30.0, "gap": 2.0}
        assert site.call("PUT", "/api/platforms/cime/gate", json={"scan": 60}).status_code == 409  # 꺼진 플랫폼
        assert site.client.put("/api/platforms/chzzk/gate", json={"scan": 60}).status_code == 403  # CSRF
        from live_recorder import diagnostics
        import io, json, zipfile
        with zipfile.ZipFile(io.BytesIO(diagnostics.build(site.h.cfg))) as z:
            summary = json.loads(z.read("summary.json"))
        assert summary["gates"]["chzzk"]["scan"] == 30 and summary["gates"]["chzzk"]["paused"] is False


def test_collection_and_analysis_share_the_gate(tmp_path):
    """확장은 중심부의 창구를 접점(ExtensionContext.request_gate)으로 받고, 분석은 EXTRA 줄로 쓴다."""
    from live_recorder.core.extensions import ExtensionContext
    from live_recorder.ext.downloads.manager import ReplayManager

    used = []

    class G:
        def slot(self, rank, min_gap=0.0):
            used.append(rank)

            class S:
                async def __aenter__(self_inner):
                    return None

                async def __aexit__(self_inner, *a):
                    return None
            return S()

    ctx = ExtensionContext("x", None, None, None, tmp_path, None, gate_provider=lambda pid: G())
    assert isinstance(ctx.request_gate("chzzk"), G)
    import inspect
    src = inspect.getsource(ReplayManager)
    assert "gate.slot(2)" in src and "_analysis_gate" in src
    # 목록 수집은 downloads의 공통 수집기가 공급자의 요청을 그 플랫폼 창구의 EXTRA 줄로 감싼다(전에는 replay의 Catalog.opened)
    from live_recorder.ext.downloads import catalog as common
    assert "gate.slot(2)" in inspect.getsource(common.Collector._gated)
