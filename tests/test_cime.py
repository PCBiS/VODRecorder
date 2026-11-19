"""씨미 실시간 녹화. 실제 씨미는 부르지 않는다. 가짜 씨미(tests/fake_cime.py)와 가짜 fMP4 HLS로 시험한다."""

import asyncio
import json
import os
import re
from pathlib import Path

import pytest

import fake_cime
import fake_ytdlp
from conftest import CHANNEL, Harness, duration, fast_timings, parse_iso, probe
from live_recorder.platforms import base, credentials, registry
from live_recorder.platforms.cime import page as cpage
from live_recorder.platforms.cime import platform as cm

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "live_recorder"
TESTS = str(Path(__file__).resolve().parent)
CID, OTHER = "1007346", "2000001"


@pytest.fixture
def cime(server, monkeypatch):
    monkeypatch.setenv(cm.BASE_ENV, server.base + fake_cime.PREFIX)
    monkeypatch.delenv(cm.ENV_FORBID, raising=False)
    fake_cime.serve(server)
    server.cime_channels[CID] = {"slug": "jeongnae", "name": "정나은양"}
    server.cime_channels[OTHER] = {"slug": "other", "name": "다른 채널"}
    server.go_live(OTHER, live_id="999001", title="다른 방송")  # 방송 화면에 함께 든다
    return server


def harness(tmp_path, server, tools, **timings):
    h = Harness(tmp_path, server, tools, timings=fast_timings(min_interval_override=0.0, **timings))
    h.store.set_platform_enabled("cime", True)
    cm.Handles(h.cfg.data_dir).set(CID, "jeongnae")
    return h


def test_registered_and_parses():
    p = registry.get("cime")
    assert isinstance(p, base.Platform) and p.name == "씨미" and p.opt_in and p.min_check_interval == 30
    assert not p.features.rewind and not p.features.timemachine and p.features.live_resume
    for text, q in (("https://ci.me/@jeongnae", "@jeongnae"), ("ci.me/@jeongnae/live", "@jeongnae"), ("@jeongnae", "@jeongnae")):
        with pytest.raises(base.ChannelLookupNeeded) as e:
            p.parse_channel(text)
        assert e.value.query == q
    for bad in ("1007346", "https://evil.example/@x", "https://ci.me/@x/videos", CHANNEL):
        with pytest.raises(base.ChannelParseError):
            p.parse_channel(bad)
    assert p.valid_channel_id(CID) and not p.valid_channel_id("abc")
    assert set(p.allowed_hosts(None)) >= {"ci.me", "live-video.net"} and p.thumbnail_url("https://streaming.cf.ci.me/a.jpg")
    assert p.thumbnail_url("https://evil.example/a.jpg") is None


def test_page_picks_the_channel_by_id():
    items = [{"id": "1", "state": "ACTIVE", "playback": {"url": "u1"}, "channel": {"id": "9", "slug": "a", "name": "A"}},
             {"id": "2", "state": "ACTIVE", "playback": {"url": "u2"}, "channel": {"id": CID, "slug": "jeongnae", "name": "B"}}]
    body = "<script>x=\"" + json.dumps({"lives": items}).replace('"', "&quot;") + "\"</script>"
    assert cpage.find_live(body, CID)["playback"]["url"] == "u2"
    assert cpage.find_live(body, "12345") is None
    assert cpage.find_channel(body, "@JeongNae")["id"] == CID


# ---- 방송 확인 ----
def test_check_verdicts_and_credentials(tmp_path, cime):
    cfg = type("C", (), {"data_dir": tmp_path, "allow_http": True})()
    cm.Handles(tmp_path).set(CID, "jeongnae")
    client = cm.PLATFORM.client(cfg)

    async def run():
        out = {}
        out["offline"] = await client.check_live(CID, None)
        cime.go_live(CID, live_id="5001", title="씨미 방송")
        out["live"] = await client.check_live(CID, None)
        cime.cime_status[CID] = 500
        out["error"] = await client.check_live(CID, None)
        cime.cime_status[CID] = 429
        out["rate"] = await client.check_live(CID, None)
        cime.cime_status.clear()
        out["missing"] = await client.check_live("4040404", None)
        cime.cime_page_broken = True
        out["broken"] = await client.check_live(CID, None)
        cime.cime_page_broken = False
        cime.cime_channels[CID]["adult"] = True
        out["needed"] = await client.check_live(CID, None)
        out["rejected"] = await client.check_live(CID, {"mauth-authorization-code": "wrong-value"})
        out["adult_ok"] = await client.check_live(CID, {"mauth-authorization-code": fake_cime.GOOD_AUTH})
        await client.aclose()
        return out

    r = asyncio.run(run())
    assert r["offline"].verdict is base.Verdict.OFFLINE
    live = r["live"]
    assert live.verdict is base.Verdict.LIVE and live.info.live_id == "5001" and live.info.title == "씨미 방송"
    assert live.info.complete() and {t.height for t in live.info.tracks} == {720, 1080}
    assert r["error"].verdict is base.Verdict.UNKNOWN and not r["error"].throttled
    assert r["rate"].throttled
    assert "채널이 없습니다" in r["missing"].detail
    assert r["broken"].detail == cm.NOT_FOUND_PAGE
    assert r["needed"].credential_problem == "needed" and r["rejected"].credential_problem == "rejected"
    assert r["adult_ok"].verdict is base.Verdict.LIVE
    # 쿠키는 방송 화면 요청에만
    with_cookie = [x for x in cime.cime_requests if x["cookie"]]
    assert with_cookie and all(x["path"].startswith("/@") for x in with_cookie)
    assert all("cookie" not in r_["headers"] for r_ in cime.requests if r_["path"].startswith("/hls/"))


def test_credential_value_and_summary(tmp_path, server, tools, cime):
    from test_web import Web

    with Web(tmp_path, server, tools) as site:
        site.login()
        assert site.client.get("/api/cookies?platform=cime").status_code == 409  # 꺼져 있다
        site.h.store.set_platform_enabled("cime", True)
        info = next(p for p in site.client.get("/api/platforms").json()["platforms"] if p["id"] == "cime")
        assert info["credentials"] == {"names": ["mauth-authorization-code"], "title": "씨미 쿠키", "help": info["credentials"]["help"],
                                       "checkable": False, "kind": "values", "max_bytes": 0, "summary": True}
        assert site.call("PUT", "/api/cookies?platform=cime", json={"mauth-authorization-code": "a;b"}).status_code == 400
        body = site.call("PUT", "/api/cookies?platform=cime", json={"mauth-authorization-code": fake_cime.GOOD_AUTH}).json()
        assert body["mauth-authorization-code"] is True and body["summary"]["saved_at"]
        assert body["summary"]["expires"].startswith("2025-11-27"), body  # iat(밀리초) + 14일
        body = site.call("PUT", "/api/cookies?platform=cime", json={"mauth-authorization-code": "not-a-jwt"}).json()
        assert body["summary"]["expires"] is None and body["summary"]["expires_note"] == "만료일을 알 수 없습니다"
        assert "not-a-jwt" not in json.dumps(body)


# ---- fMP4 녹화에서 마무리까지, 되감기 무시, 주소 갱신, 방송 ID 바뀜 ----
def test_record_fmp4_ignores_rewind_and_refreshes(tmp_path, server, tools, cime):
    h = harness(tmp_path, server, tools)
    h.store.set_settings({"rewind_seconds": 60, "filename_pattern": "{platform} {channel_name} {safe_live_title}"})
    b = cime.go_live(CID, live_id="5002", title="씨미 녹화", initial=60, rate=10, total=390)
    credentials.save(h.cfg, "cime", {"mauth-authorization-code": fake_cime.GOOD_AUTH})

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=CID, name="씨미채널", output_dir="c", platform="cime")
            rec = await h.until(lambda: next((s for s in h.sessions() if s.get("first_media_time")), None), what="녹화")
            published = b.published()
            await asyncio.sleep(0.5)
            server.expire_tokens()  # 재생목록·조각 403 → 방송 화면을 다시 읽어 새 주소
            await h.until(lambda: '"event": "url_refresh"' in h.log_text(), what="주소 갱신")
            await asyncio.sleep(0.5)
            b.end()
            done = await h.until(lambda: h.finalized(1), timeout=60, what="마무리")
            return rec, published, done[0]

    rec, published, s = asyncio.run(scenario())
    assert s["platform"] == "cime" and s["finalize_status"] == "done" and s["url_refreshes"] >= 1, h.brief()
    start = parse_iso(rec["first_media_time"])
    assert start >= b.pdt(published - 8), (start, published)  # 되감기 60초를 적용하지 않고 라이브 끝에서 시작
    out = Path(s["output_path"])
    assert out.name.startswith("cime 씨미채널 씨미 녹화")
    assert any(x["codec_type"] == "video" for x in probe(tools, out)["streams"])
    assert fake_cime.GOOD_AUTH not in h.log_text()
    assert all("cookie" not in r["headers"] for r in server.requests if r["path"].startswith("/hls/"))


def test_broadcast_id_change_ends_session(tmp_path, server, tools, cime):
    h = harness(tmp_path, server, tools)
    h.store.set_settings({"rewind_seconds": 0})
    cime.go_live(CID, live_id="5003", initial=20, rate=10, total=390)

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=CID, name="씨미채널", output_dir="c", platform="cime")
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.5)
            cime.cime_live_id[CID] = "6000"  # 화면이 다른 방송 ID를 낸다
            server.expire_tokens()
            return await h.until(lambda: next((x for x in h.sessions() if x["ended_at"]), None), what="끝")

    s = asyncio.run(scenario())
    assert s["exit_code"] == 0 and "broadcast_changed" in s["end_detail"], s


def test_credential_problem_shown(tmp_path, server, tools, cime):
    h = harness(tmp_path, server, tools)
    cime.cime_channels[CID]["adult"] = True
    cime.go_live(CID, live_id="5004")

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=CID, name="씨미채널", output_dir="c", platform="cime")
            return await h.until(lambda: h.store.get_state(CID, "cime").get("error"), what="상태")

    assert asyncio.run(scenario()) == "쿠키가 필요합니다"
    assert h.sessions() == []


# ---- 조회, 켜기·끄기, 꺼진 상태 ----
def test_lookup_enable_disable_and_off_state(tmp_path, server, tools, cime, monkeypatch):
    from test_web import Web

    with Web(tmp_path, server, tools) as site:
        site.login()
        info = next(p for p in site.client.get("/api/platforms").json()["platforms"] if p["id"] == "cime")
        assert info["opt_in"] and info["enabled"] is False
        assert "제11조" in " ".join(info["notice"]["lines"]) and "책임" in " ".join(info["notice"]["lines"])
        r = site.call("POST", "/api/channels", json={"channel_id": "@jeongnae", "name": "", "output_dir": "c", "platform": "cime"})
        assert r.status_code == 409 and "씨미를 쓰려면 설정에서 먼저 켜 주십시오" in r.text
        assert not [x for x in cime.cime_requests if x["path"].startswith("/@")]  # 꺼진 동안 씨미를 부르지 않았다
        r = site.call("POST", "/api/platforms/cime/enable", json={"agree": True})  # 자체 점검 없이 바로 켜진다
        assert r.status_code == 200, r.text
        r = site.call("POST", "/api/channels/resolve", json={"text": "https://ci.me/@jeongnae/live", "platform": "cime"})
        assert r.json() == {"platform": "cime", "channel_id": CID, "name": "정나은양"}, r.text
        assert site.call("POST", "/api/channels/resolve", json={"text": "@nobody", "platform": "cime"}).status_code == 400
        r = site.call("POST", "/api/channels", json={"channel_id": "@jeongnae", "name": "", "output_dir": "c", "platform": "cime",
                                                    "auto_record": False})
        assert r.status_code == 201 and site.h.store.get_channel(CID, "cime").name == "정나은양"
        assert cm.Handles(site.h.cfg.data_dir).get(CID) == "jeongnae"
        # 숫자 채널 ID만으로는 추가하지 않는다(핸들을 알 수 없다)
        r = site.call("POST", "/api/channels", json={"channel_id": "3000003", "name": "x", "output_dir": "c", "platform": "cime"})
        assert r.status_code == 400 and "씨미 채널은 주소나 @핸들로 추가해 주십시오" in r.text
        assert site.call("POST", "/api/platforms/cime/disable").status_code == 200
        row = next(c for c in site.client.get("/api/channels").json()["channels"] if c["platform"] == "cime")
        assert row["platform_off"] and row["platform_note"] == "씨미가 꺼져 있습니다"
        monkeypatch.setenv(cm.ENV_FORBID, "off")
        assert site.call("POST", "/api/platforms/cime/enable", json={"agree": True}).status_code == 403


# ---- 세 플랫폼 채널을 함께 감시 ----
def test_three_platforms_together(tmp_path, server, tools, cime, split_media, monkeypatch):
    from live_recorder.platforms.youtube import platform as yt

    monkeypatch.setenv("PYTHONPATH", TESTS + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    monkeypatch.setenv(yt.BACKEND_ENV, "fake_ytdlp")
    monkeypatch.setenv("FAKE_YTDLP_BASE", server.base)
    fake_ytdlp.serve(server)
    yt_ch = "UCabcdefghijklmnopqrstuv"
    h = harness(tmp_path, server, tools)
    h.store.set_platform_enabled("youtube", True)
    h.store.set_settings({"rewind_seconds": 0})
    bs = [server.go_live(CID, live_id="5005", initial=20, rate=10, total=390),
          server.go_live(yt_ch, live_id="9301", initial=20, rate=10, total=390),
          server.go_live(CHANNEL, live_id="1001", initial=20, rate=10, total=390)]

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=CID, name="씨미", output_dir="c", platform="cime")
            await h.engine.add_channel(channel_id=yt_ch, name="유튜브", output_dir="y", platform="youtube")
            await h.engine.add_channel(channel_id=CHANNEL, name="치지직", output_dir="z")
            await h.until(lambda: len({s["platform"] for s in h.sessions() if s.get("first_media_time")}) == 3, timeout=30,
                          what="세 플랫폼 녹화")
            await asyncio.sleep(0.5)
            for b in bs:
                b.end()
            return await h.until(lambda: h.finalized(3), timeout=60, what="마무리")

    done = asyncio.run(scenario())
    assert {s["platform"] for s in done} == {"cime", "youtube", "chzzk"} and all(s["finalize_status"] == "done" for s in done)


# ---- 나가는 요청의 호스트 제한, 가리기, 낱말 ----
def test_host_limit_and_masking(tmp_path):
    from live_recorder import logmask

    cfg = type("C", (), {"data_dir": tmp_path, "allow_http": False})()
    client = cm.PLATFORM.client(cfg)

    async def run():
        try:
            with pytest.raises(base.ApiError):
                await client._get("https://evil.example/x", {}, 10)
            with pytest.raises(base.ApiError):
                await client._get("http://ci.me/x", {}, 10)  # https만
        finally:
            await client.aclose()

    asyncio.run(run())
    text = logmask.Masker(False).text(
        "GET https://abc.ap-northeast-2.playback.live-video.net/api/video/v1/ap.123.channel.Xyz.m3u8?token=eyJabc.def.ghi1234 "
        '#EXT-X-SESSION-DATA:DATA-ID="NODE",USER-IP="203.0.113.7"')
    assert "Xyz" not in text and "eyJabc" not in text and "203.0.113.7" not in text and "live-video.net/" in text
    assert ".m3u8?token=[가림]" in text


CIME_STRINGS = ("ci.me", "live-video.net", "viewAccess", "isAdult", "live-status", "mauth-authorization-code", "openedAt")


def test_no_cime_strings_outside_the_platform_module():
    from test_platform_module import product_files

    bad = []
    for path in product_files((".py", ".js", ".html", ".css")):
        if path.is_relative_to(PKG / "ext" / "cime_media"):
            continue  # 씨미 주소·필드 이름은 씨미 플랫폼 모듈과 씨미 다시보기·클립 확장에만 둔다
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hits = [s for s in CIME_STRINGS if s in line]
            if hits:
                bad.append(f"{path.relative_to(ROOT)}:{n} {hits}")
    assert bad == [], "\n".join(bad)


def test_receiver_takes_fresh_master_before_opening(tmp_path, server, tools, cime):
    """마스터 재생목록을 다시 받아야 할 때(새 파일·재접속) 저장해 둔 옛 주소를 쓰지 않고 방송 화면을 다시 읽는다.
    여기서는 엔진이 확인한 주소의 토큰을 수신기가 열기 전에 없앤다. 수신기는 새 주소로 연다(갱신 횟수에는 세지 않는다)."""
    h = harness(tmp_path, server, tools)
    h.store.set_settings({"rewind_seconds": 0})
    b = cime.go_live(CID, live_id="5006", initial=20, rate=10, total=390)
    original = server._new_token
    issued = []

    def tracking(broadcast):
        token = original(broadcast)
        issued.append(token)
        if len(issued) == 2:  # 엔진의 방송 확인이 받은 주소(둘째 토큰, 첫째는 다른 채널)는 바로 만료된다
            broadcast.tokens.discard(token)
        return token

    server._new_token = tracking

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=CID, name="씨미채널", output_dir="c", platform="cime")
            await h.until(lambda: next((s for s in h.sessions() if s.get("first_media_time")), None), what="녹화")
            b.end()
            return (await h.until(lambda: h.finalized(1), timeout=60, what="마무리"))[0]

    s = asyncio.run(scenario())
    assert s["finalize_status"] == "done" and not s["url_refreshes"], h.brief()
    assert "처음 열기 전에 재생 주소를 새로 받았습니다" in h.log_text()
    pages = [x for x in cime.cime_requests if x["path"].startswith("/@")]
    assert len(pages) >= 2  # 엔진의 확인 + 수신기가 열기 전
