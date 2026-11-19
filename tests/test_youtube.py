"""유튜브 플랫폼 모듈. 실제 유튜브는 부르지 않는다. yt-dlp 파일을 가짜(tests/fake_ytdlp.py)로 바꿔 끼운다."""

import asyncio
import json
import os
from pathlib import Path

import pytest

import fake_ytdlp
from conftest import CHANNEL, Harness, duration, fast_timings, parse_iso, probe
from live_recorder.platforms import base, credentials, registry
from live_recorder.platforms.youtube import platform as yt

YT_CH = "UCabcdefghijklmnopqrstuv"
ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "live_recorder"

COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tsid-secret-value-0001\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1861920000\t__Secure-3PAPISID\tpapisid-secret-0002\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1900000000\tLOGIN_INFO\tlogin-info-secret-0003\n"
    ".example.org\tTRUE\t/\tFALSE\t1900000000\tOTHER\tother-site-secret-0004\n"
)


@pytest.fixture
def ytb(server, split_media, monkeypatch):
    """가짜 yt-dlp 파일을 끼우고 가짜 서버가 음성이 따로인 HLS를 내게 한다. 수신기 프로세스도 같은 가짜를 쓴다."""
    tests_dir = str(Path(__file__).resolve().parent)
    monkeypatch.setenv("PYTHONPATH", tests_dir + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    monkeypatch.setenv(yt.BACKEND_ENV, "fake_ytdlp")
    monkeypatch.setenv("FAKE_YTDLP_BASE", server.base)
    server.split = split_media
    return fake_ytdlp.serve(server, handles={"@ytone": (YT_CH, "유튜브 시험 채널")})


def yt_harness(tmp_path, server, tools, **timings):
    """유튜브를 켠 상태의 하네스."""
    h = Harness(tmp_path, server, tools, timings=fast_timings(min_interval_override=0.0, **timings))
    h.store.set_platform_enabled("youtube", True)
    return h


# ---- 등록과 채널 인식 ----
def test_registered_and_parses_channels():
    p = registry.get("youtube")
    assert isinstance(p, base.Platform) and p.id == "youtube" and p.name == "유튜브"
    assert p.min_check_interval == 60 and p.url_refresh_max == 4 * 3600 and p.features.live_rewind and not p.features.timemachine
    assert p.allowed_hosts(None)[0] == "googlevideo.com" and p.thumbnail_url("https://i.ytimg.com/vi/x/a.jpg")
    assert p.thumbnail_url("https://evil.example/a.jpg") is None
    for text in (YT_CH, f"https://www.youtube.com/channel/{YT_CH}", f"youtube.com/channel/{YT_CH}/live",
                 f"https://m.youtube.com/channel/{YT_CH}?si=x"):
        assert p.parse_channel(text) == YT_CH, text
    for text, query in (("@SomeOne", "@SomeOne"), ("https://www.youtube.com/@SomeOne", "@SomeOne"),
                        ("youtube.com/@SomeOne/live", "@SomeOne"), ("https://www.youtube.com/@%ED%95%9C%EA%B8%80", "@한글")):
        with pytest.raises(base.ChannelLookupNeeded) as e:
            p.parse_channel(text)
        assert e.value.query == query
    for bad in ("UCshort", "https://evil.example/channel/" + YT_CH, "https://www.youtube.com/watch?v=abc", CHANNEL):
        with pytest.raises(base.ChannelParseError):
            p.parse_channel(bad)
    assert p.valid_channel_id(YT_CH) and not p.valid_channel_id(YT_CH.lower()) and not p.valid_channel_id(YT_CH + "x")


def test_handle_lookup_through_web(tmp_path, server, tools, ytb):
    from test_web import Web

    with Web(tmp_path, server, tools) as site:
        site.login()
        site.h.store.set_platform_enabled("youtube", True)  # 켠 상태로 시험한다
        r = site.call("POST", "/api/channels/resolve", json={"text": "https://www.youtube.com/@YtOne", "platform": "youtube"})
        assert r.json() == {"platform": "youtube", "channel_id": YT_CH, "name": "유튜브 시험 채널"}, r.text
        r = site.call("POST", "/api/channels/resolve", json={"text": "@nobody", "platform": "youtube"})
        assert r.status_code == 400 and "찾지 못했습니다" in r.text
        r = site.call("POST", "/api/channels", json={"channel_id": "@ytone", "name": "", "output_dir": "y",
                                                    "platform": "youtube", "auto_record": False})
        assert r.status_code == 201 and r.json() == {"channel_id": YT_CH, "platform": "youtube"}
        assert site.h.store.get_channel(YT_CH, "youtube").name == "유튜브 시험 채널"
        info = next(p for p in site.client.get("/api/platforms").json()["platforms"] if p["id"] == "youtube")
        assert info["credentials"]["kind"] == "file" and info["credentials"]["checkable"] is False
        assert info["channel_rule"]["case_sensitive"] is True and info["min_check_interval"] == 60


# ---- 방송 확인 ----
def test_check_live_verdicts(server, ytb):
    server.go_live(YT_CH, live_id="9001", title="유튜브 방송")
    client = yt.PLATFORM.client(None)

    async def check(creds=None):
        return await client.check_live(YT_CH, creds)

    async def scenario():
        out = {}
        out["live"] = await check()
        server.yt_modes[YT_CH] = "scheduled"
        out["scheduled"] = await check()
        server.yt_modes[YT_CH] = "error"
        out["error"] = await check()
        server.yt_modes[YT_CH] = "bot"
        out["bot"] = await check()
        server.yt_modes[YT_CH] = "rate"
        out["rate"] = await check()
        server.yt_modes[YT_CH] = "login"
        out["login"] = await check()
        server.yt_cookie_ok = False
        out["rejected"] = await check({"cookies_txt": yt.clean_cookie_file(COOKIES)})
        server.yt_cookie_ok = True
        out["member"] = await check({"cookies_txt": yt.clean_cookie_file(COOKIES)})
        server.yt_modes.clear()
        server.broadcasts[YT_CH] = None
        out["offline"] = await check()
        return out

    r = asyncio.run(scenario())
    live = r["live"]
    assert live.verdict is base.Verdict.LIVE and live.info.live_id == fake_ytdlp.video_id("9001")
    assert live.info.title == "유튜브 방송" and live.info.complete() and {t.height for t in live.info.tracks} == {720, 1080}
    assert r["scheduled"].verdict is base.Verdict.OFFLINE and r["offline"].verdict is base.Verdict.OFFLINE
    assert r["error"].verdict is base.Verdict.UNKNOWN and not r["error"].throttled and r["error"].credential_problem is None
    assert r["bot"].throttled and r["rate"].throttled
    assert r["login"].credential_problem == "needed" and r["rejected"].credential_problem == "rejected"
    assert r["member"].verdict is base.Verdict.LIVE
    assert [x["cookie_lines"] for x in ytb][-3:] == [3, 3, 0]  # 쿠키는 yt-dlp 호출에만 넘긴다(유튜브·구글 줄만)


def test_credential_needed_state(tmp_path, server, tools, ytb):
    """쿠키 없이 멤버십 방송을 만나면 채널 상태에 "쿠키가 필요합니다". 녹화 실패가 아니다."""
    h = yt_harness(tmp_path, server, tools)
    server.go_live(YT_CH, live_id="9002")
    server.yt_modes[YT_CH] = "login"

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=YT_CH, name="유튜브", output_dir="y", platform="youtube")
            return await h.until(lambda: h.store.get_state(YT_CH, "youtube").get("error"), what="상태")

    assert asyncio.run(scenario()) == "쿠키가 필요합니다"
    assert h.sessions() == []


# ---- 녹화 ----
def test_record_split_audio_with_cookies_rewind_and_refresh(tmp_path, server, tools, ytb):
    """음성이 따로인 HLS를 받아 합친다. 되감기는 일반 재생목록의 창으로, 주소 갱신 뒤에는 조각 번호로 잇는다.
    쿠키는 yt-dlp에만 가고 재생목록·조각 요청, 프로세스 인자, 로그에는 없다."""
    h = yt_harness(tmp_path, server, tools)
    credentials.save(h.cfg, "youtube", {"cookies_txt": COOKIES})
    h.store.set_settings({"rewind_seconds": 15, "filename_pattern": "{platform} {channel_name} {safe_live_title}"})
    server.live_window = 60
    b = server.go_live(YT_CH, live_id="9003", title="합치기 시험", initial=40, rate=4, total=110)
    cmdlines = []

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=YT_CH, name="유튜브채널", output_dir="y", platform="youtube", quality="1080p")
            rec = await h.until(lambda: next((s for s in h.sessions() if s.get("first_media_time")), None), what="녹화")
            published = b.published()
            try:
                cmdlines.append(Path(f"/proc/{rec['pid']}/cmdline").read_bytes())
            except OSError:
                pass
            await asyncio.sleep(1.0)
            server.expire_tokens()  # 조각·재생목록이 403 → 수신기가 가짜 yt-dlp로 새 주소를 받는다
            await h.until(lambda: '"event": "url_refresh"' in h.log_text(), what="주소 갱신")
            await asyncio.sleep(1.0)
            b.end()
            done = await h.until(lambda: h.finalized(1), timeout=60, what="마무리")
            return done[0], published

    s, published = asyncio.run(scenario())
    assert s["platform"] == "youtube" and s["finalize_status"] == "done" and s["url_refreshes"] >= 1, h.brief()
    out = Path(s["output_path"])
    assert out.name.startswith("youtube 유튜브채널 합치기 시험")  # {platform}
    kinds = sorted(x["codec_type"] for x in probe(tools, out)["streams"])
    assert kinds == ["audio", "video"] and "음성 합침" in s["finalize_detail"]
    # 되감기 15초(재생목록 창 안)
    start = parse_iso(s["first_media_time"])
    assert b.pdt(published - 19) <= start <= b.pdt(published - 12), (start, published)
    # 갱신 뒤 빈틈 없이 이었다(조각 번호 기준)
    total = (parse_iso(s["last_media_time"]) - start).total_seconds()
    assert abs(duration(tools, out) - total) <= 2.0 and abs(s["media_seconds"] - total) <= 1.0, s
    # 쿠키는 yt-dlp 호출에만
    assert any(x["cookie_lines"] == 3 for x in ytb) and any(x["pid"] != os.getpid() for x in ytb)  # 수신기도 쿠키로 갱신
    hls = [r for r in server.requests if r["path"].startswith("/hls/")]
    assert hls and all("cookie" not in r["headers"] for r in hls)
    secrets = ("sid-secret-value-0001", "papisid-secret-0002", "login-info-secret-0003")
    log = h.log_text()
    assert not any(x in log for x in secrets) and "other-site-secret-0004" not in credentials.load(h.cfg, "youtube")["cookies_txt"]
    assert cmdlines and not any(x.encode() in c for x in secrets for c in cmdlines)
    assert any(json.loads(x).get("msg") == "yt-dlp: 0.0.fake (설치된 기본 판)" for x in log.splitlines())  # 판 + 출처


def test_youtube_and_chzzk_together(tmp_path, server, tools, ytb):
    """치지직과 유튜브 채널을 함께 감시하고 녹화한다. 치지직 요청은 그대로다."""
    h = yt_harness(tmp_path, server, tools)
    h.store.set_settings({"rewind_seconds": 0})
    by = server.go_live(YT_CH, live_id="9004", title="유튜브 함께", initial=20, rate=10, total=110)
    bc = server.go_live(CHANNEL, live_id="1001", title="치지직 함께", initial=20, rate=10, total=110)

    async def scenario():
        async with h:
            await h.engine.add_channel(channel_id=YT_CH, name="유튜브", output_dir="y", platform="youtube")
            await h.engine.add_channel(channel_id=CHANNEL, name="치지직", output_dir="c")
            await h.until(lambda: len([s for s in h.sessions() if s.get("first_media_time")]) >= 2, what="둘 다 녹화")
            await asyncio.sleep(0.5)
            by.end()
            bc.end()
            return await h.until(lambda: h.finalized(2), timeout=60, what="둘 다 마무리")

    done = {s["platform"]: s for s in asyncio.run(scenario())}
    assert set(done) == {"youtube", "chzzk"} and all(s["finalize_status"] == "done" for s in done.values()), h.brief()
    assert any(r["path"].startswith("/api/") and "live-detail" in r["path"] for r in server.requests)  # 치지직 API


# ---- 쿠키 파일 ----
def test_cookie_file_checks(tmp_path, server, tools):
    from test_web import Web

    clean = yt.clean_cookie_file(COOKIES)
    assert "other-site" not in clean and clean.startswith("# Netscape") and "#HttpOnly_.youtube.com" in clean
    summary = yt.cookie_summary(clean)
    assert summary == {"count": 3, "login_expires": "2029-01-01T00:00:00+00:00"}
    with pytest.raises(ValueError, match="꼴이 아닙니다"):
        yt.clean_cookie_file('{"SID": "x"}')
    with pytest.raises(ValueError, match="로그인한 쿠키가 아닙니다"):
        yt.clean_cookie_file(".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tx\n")
    with pytest.raises(ValueError, match="로그인한 쿠키가 아닙니다"):  # 다른 도메인의 로그인 쿠키는 세지 않는다
        yt.clean_cookie_file(".example.org\tTRUE\t/\tTRUE\t0\tSID\tx\n.example.org\tTRUE\t/\tTRUE\t0\tSAPISID\ty\n")
    with pytest.raises(ValueError, match="너무 큽니다"):
        yt.clean_cookie_file(COOKIES + "#" * (256 * 1024))
    with Web(tmp_path, server, tools) as site:
        site.login()
        site.h.store.set_platform_enabled("youtube", True)  # 켠 상태로 시험한다
        assert site.client.get("/api/cookies?platform=youtube").json() == {"cookies_txt": False, "last_check": None,
                                                                          "summary": None}
        r = site.call("PUT", "/api/cookies?platform=youtube", json={"cookies_txt": "SID=abc; SAPISID=def"})
        assert r.status_code == 400 and "꼴이 아닙니다" in r.text
        r = site.call("PUT", "/api/cookies?platform=youtube", json={"cookies_txt": "x" * (300 * 1024)})
        assert r.status_code == 400 and "너무 큽니다" in r.text
        r = site.call("PUT", "/api/cookies?platform=youtube", json={"cookies_txt": COOKIES})
        body = r.json()
        assert r.status_code == 200 and body["cookies_txt"] is True and body["summary"]["count"] == 3
        assert body["summary"]["login_expires"].startswith("2029-01-01") and body["summary"]["saved_at"]
        assert "secret" not in r.text and site.call("POST", "/api/cookies/check?platform=youtube").status_code == 404
        assert oct(os.stat(credentials.path(site.h.cfg, "youtube")).st_mode & 0o777) == "0o600"


# ---- 유튜브 낱말은 플랫폼 모듈 안에만 ----
YOUTUBE_STRINGS = ("youtube.com", "googlevideo", "ytimg", "SAPISID", "__Secure-3P", "yt_dlp", "yt-dlp", "youtube")


def test_no_youtube_strings_outside_the_platform_module():
    from test_platform_module import product_files

    bad = []
    # 유튜브 영상 확장(유튜브 전용 확장, 치지직 전용 모듈과 같은 자리)은 유튜브를 안다
    youtube_ext = PKG / "ext" / "youtube_video"
    for path in product_files((".py", ".js", ".html", ".css")):
        if path.is_relative_to(youtube_ext):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.replace("youtube_video", "")  # 확장 이름(BUILTIN 표, 설정 키 이름)은 낱말이 아니라 식별자다
            hits = [s for s in YOUTUBE_STRINGS if s in line]
            if hits:
                bad.append(f"{path.relative_to(ROOT)}:{n} {hits}")
    assert bad == [], "\n".join(bad)
    inside = (PKG / "platforms" / "youtube" / "platform.py").read_text(encoding="utf-8")
    assert all(s in inside for s in ("youtube.com", "googlevideo", "ytimg", "SAPISID", "__Secure-3P"))
    # yt-dlp를 불러오는 곳은 한 파일뿐이고, 모듈 맨 위에서 불러오지 않는다
    import re

    loads = re.compile(r"(?:^|\s)(?:import\s+yt_dlp|from\s+yt_dlp\b)|import_module\(\s*[\"']yt_dlp")
    users = [p for p in PKG.rglob("*.py") if loads.search(p.read_text(encoding="utf-8"))]
    assert users == [PKG / "platforms" / "youtube" / "ytdlp.py"], users
    top = [line for line in users[0].read_text(encoding="utf-8").splitlines()
           if line.startswith(("import yt_dlp", "from yt_dlp"))]  # 들여 쓴(함수 안의) 불러오기만 있다
    assert top == []


def test_real_backend_is_lazy_and_pinned():
    """판은 requirements.txt에 고정. 시작 로그용 판 읽기는 yt-dlp를 불러오지 않는다."""
    import subprocess
    import sys

    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "yt-dlp==" in req
    code = ("import sys; from live_recorder.platforms.youtube import ytdlp; print(ytdlp.describe()); "
            "print('yt_dlp' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    pinned = req.split("yt-dlp==", 1)[1].split()[0]
    assert out[0] == pinned and out[-1] == "False", out
    assert "yt-dlp" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
