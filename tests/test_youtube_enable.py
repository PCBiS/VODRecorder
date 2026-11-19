"""켜야 쓰는 플랫폼으로서의 유튜브와 이미지의 도구.

켜기 자체 점검은 개발 환경에 깔린 실제 yt-dlp·deno를 따로 띄운 프로세스에서 돌린다(네트워크 요청 없음).
방송 확인·녹화는 가짜 yt-dlp 파일(tests/fake_ytdlp.py)로 한다.
"""

import json
import os
import re
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

import fake_ytdlp
from conftest import Harness, fast_timings
from live_recorder.core import store as st
from live_recorder.platforms import registry
from live_recorder.platforms.youtube import platform as yt
from live_recorder.platforms.youtube import vendor, ytdlp

ROOT = Path(__file__).resolve().parents[1]
YT_CH = "UCabcdefghijklmnopqrstuv"
TESTS = str(Path(__file__).resolve().parent)


@pytest.fixture
def fake_backend(server, split_media, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", TESTS + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    monkeypatch.setenv(yt.BACKEND_ENV, "fake_ytdlp")
    monkeypatch.setenv("FAKE_YTDLP_BASE", server.base)
    monkeypatch.delenv(yt.ENV_FORBID, raising=False)
    server.split = split_media
    return fake_ytdlp.serve(server, handles={"@ytone": (YT_CH, "유튜브 시험 채널")})


def web(tmp_path, server, tools):
    from test_web import Web

    return Web(tmp_path, server, tools, timings=fast_timings(min_interval_override=0.0))


def yt_info(site) -> dict:
    return next(p for p in site.client.get("/api/platforms").json()["platforms"] if p["id"] == "youtube")


# ---- 처음 설치·옮겨 온 DB에서 꺼져 있음 ----
def test_off_on_fresh_install_and_b12_db(tmp_path, server, tools):
    from test_platform_module import load_fixture

    fresh = st.Store(tmp_path / "fresh.sqlite3", tmp_path)
    assert fresh.enabled_platforms() == []
    rec = tmp_path / "rec"
    rec.mkdir()
    db = tmp_path / "b12" / "recorder.sqlite3"
    db.parent.mkdir()
    load_fixture("b12_recorder.sql", db, rec)
    assert st.Store(db, rec).enabled_platforms() == []
    p = registry.get("youtube")
    assert p.opt_in and not registry.get("chzzk").opt_in
    with web(tmp_path / "w", server, tools) as site:
        site.login()
        info = yt_info(site)
        assert info["opt_in"] is True and info["enabled"] is False and info["notice"]["blocked"] is None
        text = " ".join(info["notice"]["lines"])
        for word in ("yt-dlp", "yt-dlp-ejs", "deno", "스크립트를 실행", "권한을 주지 않습니다", "제한할 수 있습니다", "쿠키 없이"):
            assert word in text, word
        chzzk = next(x for x in site.client.get("/api/platforms").json()["platforms"] if x["id"] == "chzzk")
        assert chzzk["opt_in"] is False and chzzk["enabled"] is True


# ---- 꺼진 동안 yt-dlp를 불러오지 않음(하위 프로세스) ----
OFF_SCRIPT = r'''
import json, sys, asyncio
sys.path.insert(0, TESTS)
from pathlib import Path
from conftest import FakeChzzk, fast_timings
from test_web import Web
from live_recorder import diagnostics
from live_recorder.platforms.youtube import vendor

tmp = Path(TMP)
media = tmp / "media"; media.mkdir()
server = FakeChzzk(media)
# 교체 폴더가 있어도 읽지 않는다: 깨진 표시 파일을 둔다(읽으면 고르기 로그가 남는다)
root = tmp / "w" / "data" / "vendor" / "yt-dlp"
root.mkdir(parents=True, mode=0o700)
(root / "current.json").write_text('{"version": "2099.1.1"}')
site = Web(tmp / "w", server, TOOLS, timings=fast_timings(min_interval_override=0.0))
site.h.store.add_channel("UCabcdefghijklmnopqrstuv", "유튜브", "y", platform="youtube")
out = {}
with site:
    site.login()
    out["platforms"] = site.client.get("/api/platforms").status_code
    out["tools"] = [t["name"] for t in site.client.get("/api/tools").json()["tools"]]
    out["cookies"] = site.client.get("/api/cookies?platform=youtube").status_code
    out["channels"] = [c.get("platform_off") for c in site.client.get("/api/channels").json()["channels"]]
    import time; time.sleep(0.5)
    out["state"] = site.h.store.get_state("UCabcdefghijklmnopqrstuv", "youtube")["state"]
    diagnostics.build(site.h.cfg)
    out["log_selected"] = "ytdlp_selected" in site.h.log_text()
out["yt_dlp"] = sorted(m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp."))
out["deno"] = "deno" in sys.modules
out["chosen_note"] = vendor.chosen()["note"]
server.close()
print("RESULT " + json.dumps(out, ensure_ascii=False))
'''


def test_off_never_loads_tools_in_a_fresh_process(tmp_path, tools):
    code = OFF_SCRIPT.replace("TESTS", repr(TESTS)).replace("TMP", repr(str(tmp_path))).replace("TOOLS", repr(tuple(tools)))
    env = {k: v for k, v in os.environ.items() if k not in (yt.BACKEND_ENV, yt.ENV_FORBID)}
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
    line = next((x for x in proc.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, proc.stderr[-2000:]
    out = json.loads(line[7:])
    assert out["platforms"] == 200 and out["tools"] == [] and out["cookies"] == 409
    assert out["channels"] == [True] and out["state"] == "stopped"
    assert out["yt_dlp"] == [] and out["deno"] is False and out["log_selected"] is False, out
    assert out["chosen_note"] == ""  # 교체 폴더를 읽지 않았다


# ---- 켜기 고지 뒤 자체 점검 통과·실패, 켠 뒤 채널 추가와 감시 ----
def test_enable_runs_self_check_then_channels_work(tmp_path, server, tools, fake_backend, monkeypatch):
    monkeypatch.setattr(vendor, "_chosen", dict(vendor._chosen))
    server.go_live(YT_CH, live_id="9100", title="켠 뒤 방송", initial=20, rate=10, total=110)
    with web(tmp_path, server, tools) as site:
        site.login()
        r = site.call("POST", "/api/channels", json={"channel_id": "@ytone", "name": "", "output_dir": "y", "platform": "youtube"})
        assert r.status_code == 409 and "먼저 켜 주십시오" in r.json()["detail"]
        assert site.call("POST", "/api/platforms/youtube/enable", json={}).status_code == 422  # 동의 없음
        assert site.client.post("/api/platforms/youtube/enable", json={"agree": True}).status_code == 403  # CSRF
        # 자체 점검 실패: 꺼진 채로 이유를 보인다
        real = vendor.self_check
        monkeypatch.setattr(vendor, "self_check", lambda *a, **k: (False, "deno 실행 결과가 다릅니다"))
        r = site.call("POST", "/api/platforms/youtube/enable", json={"agree": True})
        assert r.status_code == 409 and "deno 실행 결과가 다릅니다" in r.json()["detail"]
        assert yt_info(site)["enabled"] is False
        # 통과(실제 yt-dlp와 deno를 따로 띄운 프로세스에서 돌린다)
        monkeypatch.setattr(vendor, "self_check", real)
        r = site.call("POST", "/api/platforms/youtube/enable", json={"agree": True})
        assert r.status_code == 200 and r.json() == {"platform": "youtube", "enabled": True}, r.text
        assert yt_info(site)["enabled"] is True and site.h.store.enabled_platforms() == ["youtube"]
        log = site.h.log_text()
        assert '"event": "platform_enable_failed"' in log and '"event": "platform_enabled"' in log
        # 다시 시작하지 않아도 바로 쓴다: 채널 추가(조회)와 감시·녹화
        r = site.call("POST", "/api/channels", json={"channel_id": "@ytone", "name": "", "output_dir": "y", "platform": "youtube"})
        assert r.status_code == 201, r.text
        import time
        end = time.monotonic() + 30
        while time.monotonic() < end and not any(s.get("first_media_time") for s in site.h.sessions()):
            time.sleep(0.1)
        assert any(s["platform"] == "youtube" and s.get("first_media_time") for s in site.h.sessions()), site.h.brief()


def test_real_self_check_passes_with_deno():
    """실제 점검: 이미지(개발 환경)의 yt-dlp를 불러오고 deno가 권한 없이 짧은 글을 실행한다."""
    ok, why = vendor.self_check(None, vendor.default_version(), deno=True)
    assert ok, why


# ---- 꺼진 동안의 안내와 API 거부, 녹화 중 끄기 거부, 끈 뒤 채널 유지와 다시 켜기 ----
def test_disable_rules_and_reenable(tmp_path, server, tools, fake_backend, monkeypatch):
    monkeypatch.setattr(vendor, "_chosen", dict(vendor._chosen))
    b = server.go_live(YT_CH, live_id="9200", title="끄기 시험", initial=20, rate=4, total=110)
    with web(tmp_path, server, tools) as site:
        site.login()
        site.h.store.set_platform_enabled("youtube", True)
        from live_recorder.platforms import credentials
        credentials.save(site.h.cfg, "youtube", {"cookies_txt": (
            ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\ts1\n.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\ts2\n")})
        assert site.call("POST", "/api/channels", json={"channel_id": YT_CH, "name": "유튜브", "output_dir": "y",
                                                       "platform": "youtube"}).status_code == 201
        import time
        end = time.monotonic() + 30
        while time.monotonic() < end and not site.h.recording():
            time.sleep(0.1)
        assert site.h.recording(), site.h.brief()
        r = site.call("POST", "/api/platforms/youtube/disable")
        assert r.status_code == 409 and "녹화 중인" in r.json()["detail"]
        b.end()
        end = time.monotonic() + 40
        while time.monotonic() < end and not site.h.finalized(1):
            time.sleep(0.1)
        time.sleep(0.5)
        r = site.call("POST", "/api/platforms/youtube/disable")
        assert r.status_code == 200 and r.json()["enabled"] is False, r.text
        # 채널은 남고 "유튜브가 꺼져 있습니다". 쿠키와 교체 폴더는 그대로
        row = next(c for c in site.client.get("/api/channels").json()["channels"] if c["platform"] == "youtube")
        assert row["platform_off"] is True and row["platform_note"] == "유튜브가 꺼져 있습니다"
        assert credentials.load(site.h.cfg, "youtube")
        assert site.call("GET", "/api/cookies?platform=youtube").status_code == 409
        assert site.call("PUT", "/api/cookies?platform=youtube", json={"cookies_txt": "x"}).status_code == 409
        assert site.call("DELETE", "/api/cookies?platform=youtube").status_code == 409
        assert site.call("POST", "/api/channels/resolve", json={"text": "@ytone", "platform": "youtube"}).status_code == 409
        assert [t for t in site.client.get("/api/tools").json()["tools"] if t["name"] == "yt-dlp"] == []
        assert site.call("POST", "/api/tools/yt-dlp/update").status_code == 404
        calls = len(fake_backend)
        time.sleep(0.6)
        assert len(fake_backend) == calls  # 감시를 멈췄다
        assert site.h.store.get_state(YT_CH, "youtube")["state"] == "stopped"
        # 다시 켜면 이어서 감시한다
        server.go_live(YT_CH, live_id="9201", title="다시 켬", initial=20, rate=10, total=110)
        assert site.call("POST", "/api/platforms/youtube/enable", json={"agree": True}).status_code == 200
        end = time.monotonic() + 30
        while time.monotonic() < end and len(fake_backend) == calls:
            time.sleep(0.1)
        assert len(fake_backend) > calls


# ---- 켤 수 없게 한 환경변수 ----
def test_forbid_env(tmp_path, server, tools, monkeypatch):
    monkeypatch.setenv(yt.ENV_FORBID, "off")
    with web(tmp_path, server, tools) as site:
        site.login()
        site.h.store.set_platform_enabled("youtube", True)  # 켠 기록이 있어도 꺼진 것으로 본다
        info = yt_info(site)
        assert info["enabled"] is False and "켤 수 없게" in info["notice"]["blocked"]
        r = site.call("POST", "/api/platforms/youtube/enable", json={"agree": True})
        assert r.status_code == 403 and "환경변수" in r.json()["detail"]
        assert site.call("GET", "/api/cookies?platform=youtube").status_code == 409
        assert site.call("POST", "/api/channels", json={"channel_id": YT_CH, "name": "x", "output_dir": "y",
                                                       "platform": "youtube"}).status_code == 409


def test_chzzk_cannot_be_toggled(tmp_path, server, tools):
    with web(tmp_path, server, tools) as site:
        site.login()
        assert site.call("POST", "/api/platforms/chzzk/disable").status_code == 409
        assert site.call("POST", "/api/platforms/nope/enable", json={"agree": True}).status_code == 404


# ---- deno 경로 전달과 허용 옵션 없음, 이미지 고정 ----
def test_deno_path_is_given_without_permissions():
    path = os.path.join(sysconfig.get_path("scripts"), "deno")
    assert ytdlp.deno_path() == path and os.access(path, os.X_OK)
    opts = ytdlp.build_opts("x", flat=True)
    assert opts["js_runtimes"] == {"deno": {"path": path}}  # 경로만. 허용 옵션·인자를 더하지 않는다
    src = (ROOT / "live_recorder" / "platforms" / "youtube" / "ytdlp.py").read_text(encoding="utf-8")
    assert not re.search(r"--allow|allow-all|\"-A\"|DENO_DIR|DENO_AUTH", src)
    assert '"--no-prompt", "--no-remote"' in src  # 점검도 yt-dlp가 띄우는 꼴과 같다


def test_no_js_runtime_warning_only_without_deno(monkeypatch, caplog):
    monkeypatch.setattr(ytdlp, "_js_noted", False)
    with caplog.at_level("WARNING", logger="live_recorder.youtube"):
        ytdlp.note_startup()
    assert not [r for r in caplog.records if getattr(r, "event", "") == "ytdlp_no_js_runtime"]
    monkeypatch.setattr(ytdlp, "deno_path", lambda: None)
    assert ytdlp.js_runtimes() == {}  # PATH를 뒤지지 않게 빈 설정
    with caplog.at_level("WARNING", logger="live_recorder.youtube"):
        ytdlp.note_startup()
        ytdlp.note_startup()
    assert len([r for r in caplog.records if getattr(r, "event", "") == "ytdlp_no_js_runtime"]) == 1


def test_image_pins_match():
    """requirements.txt와 Dockerfile의 해시 고정 설치가 같은 판이고, yt-dlp가 지정한 짝과 맞는다."""
    req = dict(re.findall(r"^([a-z0-9-]+)==(\S+)$", (ROOT / "requirements.txt").read_text(encoding="utf-8"), re.M))
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pins = dict(re.findall(r"^ARG (YTDLP_VERSION|YTDLP_EJS_VERSION|DENO_VERSION)=(\S+)$", docker, re.M))
    assert pins == {"YTDLP_VERSION": req["yt-dlp"], "YTDLP_EJS_VERSION": req["yt-dlp-ejs"], "DENO_VERSION": req["deno"]}
    for name in ("YTDLP_SHA256", "YTDLP_EJS_SHA256", "DENO_SHA256_X86_64", "DENO_SHA256_AARCH64"):
        assert re.search(rf"^ARG {name}=[0-9a-f]{{64}}$", docker, re.M), name
    assert "--require-hashes" in docker and "--no-deps" in docker
    from importlib import metadata
    from email.parser import Parser

    meta = Parser().parsestr(metadata.distribution("yt-dlp").read_text("METADATA"), headersonly=True)
    reqs = meta.get_all("Requires-Dist")
    assert f"yt-dlp-ejs=={req['yt-dlp-ejs']}; extra == 'pin'" in reqs and f"deno=={req['deno']}; extra == 'pin-deno'" in reqs
    assert metadata.version("deno") == req["deno"] and metadata.version("yt-dlp-ejs") == req["yt-dlp-ejs"]
