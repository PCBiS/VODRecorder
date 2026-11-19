"""플랫폼 접점 더하기. 가짜 플랫폼(tests/fake_platform.py)으로 시험한다."""

import asyncio
import dataclasses
import os
from pathlib import Path

import pytest

import fake_platform
from conftest import Harness, fast_timings, parse_iso
from live_recorder.core import store as st
from live_recorder.platforms import base, credentials, registry

FAKE_ID = "fp000123"


@pytest.fixture
def fake(server, monkeypatch):
    seen = fake_platform.serve(server)
    monkeypatch.setattr(fake_platform.PLATFORM, "base", server.base)
    tests_dir = str(Path(__file__).resolve().parent)
    monkeypatch.setenv("PYTHONPATH", tests_dir + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    registry.register(fake_platform.PLATFORM)
    yield seen
    registry.unregister(fake_platform.PLATFORM.id)


def test_interface_has_new_members():
    """접점의 새 항목: 최소 확인 간격, 주소 수명, 조회 단계, 파일 글 자격, 일반 재생목록 되감기."""
    chzzk = registry.get("chzzk")
    assert chzzk.min_check_interval == 0 and chzzk.url_refresh_max == 0  # 치지직은 그대로
    assert chzzk.credential_spec.kind == "values" and not chzzk.features.live_rewind
    assert issubclass(base.ChannelLookupNeeded, base.ChannelParseError) and issubclass(base.BroadcastChanged, base.ApiError)
    assert base.CheckResult(base.Verdict.UNKNOWN).throttled is False


def test_handle_lookup_on_add(tmp_path, server, tools, fake):
    """`@이름`은 추가할 때 한 번 조회해 채널 ID와 이름으로 저장한다. 찾지 못하면 추가하지 않는다."""
    from test_web import Web

    with Web(tmp_path, server, tools) as site:
        site.login()
        r = site.call("POST", "/api/channels/resolve", json={"text": "@FakeOne", "platform": "fakeplat"})
        assert r.status_code == 200 and r.json() == {"platform": "fakeplat", "channel_id": "fp000777", "name": "가짜 이름표 채널"}
        r = site.call("POST", "/api/channels/resolve", json={"text": FAKE_ID, "platform": "fakeplat"})
        assert r.json() == {"platform": "fakeplat", "channel_id": FAKE_ID, "name": None}  # ID는 조회하지 않는다
        r = site.call("POST", "/api/channels/resolve", json={"text": "@nobody", "platform": "fakeplat"})
        assert r.status_code == 400 and "찾지 못했습니다" in r.text
        r = site.call("POST", "/api/channels", json={"channel_id": "@nobody", "name": "", "output_dir": "a",
                                                    "platform": "fakeplat", "auto_record": False})
        assert r.status_code == 400 and site.h.store.list_channels() == []
        r = site.call("POST", "/api/channels", json={"channel_id": "@fakeone", "name": "", "output_dir": "a",
                                                    "platform": "fakeplat", "auto_record": False})
        assert r.status_code == 201 and r.json() == {"channel_id": "fp000777", "platform": "fakeplat"}
        assert site.h.store.get_channel("fp000777", "fakeplat").name == "가짜 이름표 채널"
        # 치지직은 조회 단계가 없고 요청도 그대로다(주소를 채널 ID 자리에 받지 않는다)
        assert site.call("POST", "/api/channels", json={"channel_id": "@someone", "name": "x", "output_dir": "b"}).status_code == 400


def _scripted(monkeypatch, results):
    """가짜 플랫폼의 방송 확인이 정해 둔 결과를 차례로 낸다(마지막 결과를 되풀이)."""
    calls = []

    async def check_live(self, channel_id, creds):
        calls.append(len(calls))
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(fake_platform.FakeClient, "check_live", check_live)
    return calls


def test_platform_minimum_check_interval(tmp_path, server, tools, fake, monkeypatch):
    """확인 주기는 플랫폼마다의 스캔 주기다. 이전 빌드의 전역 확인 주기(check_interval)는 저장한 값이
    없는 플랫폼의 초깃값이 된다. 플랫폼의 최소 확인 간격은 강제하지 않는다. 채널별 어긋남(±15%)도 없다."""
    h = Harness(tmp_path, server, tools, timings=fast_timings(check_interval_override=None, gate_interval_override=None))
    h.store.set_settings({"check_interval": 15})
    monkeypatch.setattr(fake_platform.PLATFORM, "min_check_interval", 60.0)  # 강제하지 않는다
    h.store.set_gate_settings("fakeplat", scan=90)

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=FAKE_ID, name="가짜", output_dir="f", platform="fakeplat", auto_record=False)
            await h.engine.add_channel(channel_id="0123456789abcdef0123456789abcdef", name="치지직", output_dir="c",
                                       auto_record=False)
            settings = h.store.get_settings()
            fake_w = h.engine.worker(FAKE_ID, "fakeplat")
            chzzk_w = h.engine.worker("0123456789abcdef0123456789abcdef", "chzzk")
            return fake_w.interval(settings), chzzk_w.interval(settings)

    fake_wait, chzzk_wait = asyncio.run(scenario())
    assert fake_wait == 90 and chzzk_wait == 15  # 치지직은 옮겨 온 전역 값(15초)


def test_throttle_backoff_and_restore(tmp_path, server, tools, fake, monkeypatch):
    """막히면 플랫폼 창구 전체를 5·10·20분(시험에서는 줄인 값) 멈추고, 찔러 본 확인이 되면 되돌린다.
    멈춘 사실은 상태에 보이고 알림은 플랫폼당 한 번."""
    blocked = base.CheckResult(base.Verdict.UNKNOWN, detail="봇 확인 요구", throttled=True)
    calls = _scripted(monkeypatch, [blocked, blocked, blocked, blocked, base.CheckResult(base.Verdict.OFFLINE)])
    h = Harness(tmp_path, server, tools, timings=fast_timings(throttle_steps=(0.4, 0.8, 1.2)))
    h.enable_telegram(notify_abnormal_end=True)
    errors = []

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=FAKE_ID, name="가짜", output_dir="f", platform="fakeplat")
            w = h.engine.gate_for("fakeplat")  # 단계는 채널이 아니라 플랫폼 창구에 있다
            levels = []
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            while len(calls) < 6 and loop.time() - t0 < 15:
                levels.append(w.level)
                e = h.store.get_state(FAKE_ID, "fakeplat").get("error")
                if e:
                    errors.append(e)
                await asyncio.sleep(0.05)
            await h.until(lambda: not h.store.get_state(FAKE_ID, "fakeplat").get("error"), what="되돌림")
            return levels, loop.time() - t0

    levels, took = asyncio.run(scenario())
    assert max(levels) == 3 and levels[-1] == 0, levels  # 상한에서 멈추고, 성공하면 0
    assert took >= 0.4 + 0.8 + 1.2 + 1.2 - 0.3, took  # 늦춘 만큼 기다렸다
    assert any("다시 확인합니다" in e for e in errors), errors
    throttled = [m for m in server.tg_messages if "다시 확인합니다" in m.get("text", "")]
    assert len(throttled) == 1, server.tg_messages  # 알림은 처음 한 번만
    assert '"event": "gate_resumed"' in h.log_text()  # 채널별 늦추기 풀기(check_throttle_end) 대신 창구 다시 열기


def test_credential_problem_is_shown_not_failed(tmp_path, server, tools, fake, monkeypatch):
    """쿠키 없이 로그인이 필요한 방송은 녹화 실패가 아니라 "쿠키가 필요합니다". 거부면 따로 구분한다."""
    needed = base.CheckResult(base.Verdict.UNKNOWN, detail="로그인 필요", credential_problem="needed")
    rejected = base.CheckResult(base.Verdict.UNKNOWN, detail="로그인 필요", credential_problem="rejected")
    calls = _scripted(monkeypatch, [needed] * 4 + [rejected] * 3 + [base.CheckResult(base.Verdict.OFFLINE)])
    h = Harness(tmp_path, server, tools)
    h.enable_telegram(notify_start_failed=True, notify_cookie_expired=True)
    seen = []

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=FAKE_ID, name="가짜", output_dir="f", platform="fakeplat")
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            while len(calls) < 9 and loop.time() - t0 < 15:
                state = h.store.get_state(FAKE_ID, "fakeplat")
                if state.get("error"):
                    seen.append((state["error"], state["error_flag"], state["state"]))
                await asyncio.sleep(0.03)
            await h.until(lambda: not h.store.get_state(FAKE_ID, "fakeplat").get("error"), what="되돌림")

    asyncio.run(scenario())
    texts = {e for e, _, _ in seen}
    assert texts == {"쿠키가 필요합니다", "쿠키가 만료됐거나 권한이 없습니다"}, texts
    assert all(flag == 1 for _, flag, _ in seen)
    assert h.sessions() == []  # 녹화 실패 기록이 없다
    msgs = [m["text"] for m in server.tg_messages]
    assert sum("쿠키가 필요합니다" in t for t in msgs) == 1 and sum("만료됐거나" in t for t in msgs) == 1, msgs
    assert '"event": "failure"' not in h.log_text()


class FilePlatform(fake_platform.FakePlatform):
    """시험용: 자격이 파일 글 전체인 가짜 플랫폼."""

    id = "fakefile"
    name = "파일 자격 플랫폼"
    credential_spec = base.CredentialSpec(names=("cookies_txt",), title="쿠키 파일", kind="file", max_bytes=1024)

    def clean_credential_file(self, text: str) -> str:
        lines = [x for x in text.splitlines() if x.strip() and not x.startswith("#")]
        if not lines or not all(x.count("\t") == 6 for x in lines):
            raise ValueError("쿠키 파일 꼴이 아닙니다")
        return "\n".join(lines) + "\n"

    def credential_summary(self, values):
        return {"count": len(values["cookies_txt"].splitlines())}


def test_file_credentials(tmp_path, server, tools):
    from test_web import Web

    plat = FilePlatform()
    registry.register(plat)
    try:
        with Web(tmp_path, server, tools) as site:
            site.login()
            info = next(p for p in site.client.get("/api/platforms").json()["platforms"] if p["id"] == "fakefile")
            assert info["credentials"]["kind"] == "file" and info["credentials"]["max_bytes"] == 1024
            assert site.client.get("/api/cookies?platform=fakefile").json() == {"cookies_txt": False, "last_check": None,
                                                                                 "summary": None}
            r = site.call("PUT", "/api/cookies?platform=fakefile", json={"cookies_txt": "아무 글"})
            assert r.status_code == 400 and "꼴이 아닙니다" in r.text
            r = site.call("PUT", "/api/cookies?platform=fakefile", json={"cookies_txt": "x" * 2000})
            assert r.status_code == 400 and "너무 큽니다" in r.text
            text = "# Netscape\n.example.com\tTRUE\t/\tTRUE\t0\tA\tsecret-value-1\n.example.com\tTRUE\t/\tTRUE\t0\tB\tv2\n"
            r = site.call("PUT", "/api/cookies?platform=fakefile", json={"cookies_txt": text})
            body = r.json()
            assert r.status_code == 200 and body["cookies_txt"] is True and body["summary"]["count"] == 2
            assert body["summary"]["saved_at"] and "secret-value-1" not in r.text
            assert credentials.load(site.h.cfg, "fakefile")["cookies_txt"].startswith(".example.com")
            assert oct(os.stat(credentials.path(site.h.cfg, "fakefile")).st_mode & 0o777) == "0o600"
    finally:
        registry.unregister("fakefile")


def test_live_rewind_without_timemachine(tmp_path, server, tools, fake, monkeypatch):
    """타임머신이 없어도 플랫폼이 밝히면 일반 재생목록의 창으로 되감는다."""
    monkeypatch.setattr(fake_platform.PLATFORM, "features", base.Features(timemachine=False, rewind=True, live_rewind=True))
    server.live_window = 60
    h = Harness(tmp_path, server, tools)
    h.store.set_settings({"rewind_seconds": 20})
    b = server.go_live(FAKE_ID, live_id="7001", initial=50, rate=2)

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=FAKE_ID, name="가짜", output_dir="f", platform="fakeplat")
            rec = await h.until(lambda: next((s for s in h.sessions() if s.get("first_media_time")), None), what="녹화")
            published = b.published()
            b.end()
            await h.until(lambda: h.finalized(1), timeout=40, what="마무리")
            return rec, published

    rec, published = asyncio.run(scenario())
    start = parse_iso(rec["first_media_time"])
    assert b.pdt(published - 24) <= start <= b.pdt(published - 17), (start, published)


def test_broadcast_change_on_refresh_ends_session(tmp_path, server, tools, fake, monkeypatch):
    """주소를 새로 받았더니 다른 방송이면 그 회차를 끝낸다(정상 종료)."""
    monkeypatch.setenv("FAKE_PLATFORM_CHANGE", "1")
    h = Harness(tmp_path, server, tools)
    h.store.set_settings({"rewind_seconds": 0})
    server.go_live(FAKE_ID, live_id="7001", initial=20, rate=10)

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=FAKE_ID, name="가짜", output_dir="f", platform="fakeplat")
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.5)
            server.expire_tokens()
            return await h.until(lambda: next((s for s in h.sessions() if s["ended_at"]), None), what="끝")

    s = asyncio.run(scenario())
    assert s["exit_code"] == 0 and "broadcast_changed" in s["end_detail"], s


def test_throttle_cap_hint_without_cookies(tmp_path, server, tools, fake, monkeypatch):
    """쿠키 없이 상한에서 세 번 이어 막히면 상태 문장에 "쿠키를 넣으면 풀릴 수 있습니다"를 덧붙인다."""
    blocked = base.CheckResult(base.Verdict.UNKNOWN, detail="봇 확인 요구", throttled=True)
    calls = _scripted(monkeypatch, [blocked])
    h = Harness(tmp_path, server, tools, timings=fast_timings(throttle_steps=(0.1, 0.1, 0.1)))
    seen = []

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=FAKE_ID, name="가짜", output_dir="f", platform="fakeplat")
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            while len(calls) < 8 and loop.time() - t0 < 15:
                seen.append((len(calls), h.store.get_state(FAKE_ID, "fakeplat").get("error") or ""))
                await asyncio.sleep(0.02)

    asyncio.run(scenario())
    hinted = [n for n, e in seen if "쿠키를 넣으면 풀릴 수 있습니다" in e]
    assert hinted and min(hinted) >= 5, seen[-5:]  # 상한(셋째)에서 세 번째 막힘(다섯째 확인)부터
    assert not [e for n, e in seen if n <= 4 and "쿠키를 넣으면" in e]


def test_throttle_cap_hint_not_shown_with_cookies(tmp_path, server, tools, fake, monkeypatch):
    blocked = base.CheckResult(base.Verdict.UNKNOWN, detail="봇 확인 요구", throttled=True)
    calls = _scripted(monkeypatch, [blocked])
    h = Harness(tmp_path, server, tools, timings=fast_timings(throttle_steps=(0.1, 0.1, 0.1)))
    credentials.save(h.cfg, "fakeplat", {"FP_TOKEN": "tok-1"})
    errors = []

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=FAKE_ID, name="가짜", output_dir="f", platform="fakeplat")
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            while len(calls) < 8 and loop.time() - t0 < 15:
                errors.append(h.store.get_state(FAKE_ID, "fakeplat").get("error") or "")
                await asyncio.sleep(0.02)

    asyncio.run(scenario())
    assert any("다시 확인합니다" in e for e in errors) and not any("쿠키를 넣으면" in e for e in errors)
