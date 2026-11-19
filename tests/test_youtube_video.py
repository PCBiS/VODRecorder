"""유튜브 영상 주소 다운로드. 실제 유튜브는 부르지 않는다.

가짜 yt-dlp 파일(tests/fake_ytdlp.py)이 형식 목록을 내고, 가짜 서버의 `/ytfile/`이 범위 요청을 받는 googlevideo 흉내다.
"""

import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

import fake_ytdlp
from conftest import probe
from live_recorder.core import extensions
from live_recorder.ext.downloads import api, media
from live_recorder.ext.youtube_video import extension as yv_ext
from live_recorder.ext.youtube_video import handler
from live_recorder.platforms import credentials
from live_recorder.platforms.youtube import platform as yt

ROOT = Path(__file__).resolve().parents[1]
TESTS = str(Path(__file__).resolve().parent)
VID, VID2 = "aqz-KE-bpKQ", "Bbbbbbbbbb2"
URL = f"https://www.youtube.com/watch?v={VID}"
EXTS = ["downloads", "youtube_video"]
COOKIES = ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tsid-yv-secret-0001\n" \
          ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tsapisid-yv-secret-0002\n"


@pytest.fixture(scope="session")
def yt_files(tools, tmp_path_factory):
    """영상만 든 mp4(약 6MB, 20초)와 음성만 든 m4a."""
    out = tmp_path_factory.mktemp("ytfiles")
    common = [tools[0], "-nostdin", "-hide_banner", "-loglevel", "error"]
    subprocess.run(common + ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "20", "-c:v", "libx264",
                             "-preset", "ultrafast", "-b:v", "2400k", "-maxrate", "2400k", "-bufsize", "1M", "-pix_fmt", "yuv420p",
                             "-an", "-movflags", "+faststart", str(out / "v.mp4")], check=True)
    subprocess.run(common + ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "20", "-c:a", "aac",
                             "-b:a", "96k", "-ac", "2", str(out / "a.m4a")], check=True)
    return {"video": (out / "v.mp4").read_bytes(), "audio": (out / "a.m4a").read_bytes()}


def video_spec(files, **extra):
    v, a = files["video"], files["audio"]
    spec = {"title": "유튜브 시험 영상", "channel": "가짜 채널", "ts": 1763000000, "duration": 20, "files": {
        "137": {"data": v, "vcodec": "avc1.640028", "height": 1080, "width": 1920, "fps": 30, "tbr": 2400},
        "136": {"data": v, "vcodec": "avc1.4d401f", "height": 720, "width": 1280, "fps": 30, "tbr": 1200},
        "248": {"data": v, "vcodec": "vp09.00.40.08", "height": 1080, "width": 1920, "fps": 30, "tbr": 2000},
        "271": {"data": v, "vcodec": "vp09.00.50.08", "height": 1440, "width": 2560, "fps": 60, "tbr": 5000},
        "400": {"data": v, "vcodec": "av01.0.12M.08", "height": 1440, "width": 2560, "fps": 60, "tbr": 4500},
        "140": {"data": a, "acodec": "mp4a.40.2", "channels": 2, "tbr": 128},
        "139": {"data": a, "acodec": "mp4a.40.5", "channels": 2, "tbr": 48},
        "251": {"data": a, "acodec": "opus", "channels": 2, "tbr": 160},
    }}
    spec.update(extra)
    return spec


@pytest.fixture
def yv(tmp_path, server, tools, split_media, yt_files, monkeypatch):
    from test_web import Web

    monkeypatch.setenv("PYTHONPATH", TESTS + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    monkeypatch.setenv(yt.BACKEND_ENV, "fake_ytdlp")
    monkeypatch.setenv("FAKE_YTDLP_BASE", server.base)
    monkeypatch.delenv(yt.ENV_FORBID, raising=False)
    monkeypatch.setattr(yv_ext, "KIND", replace(yv_ext.KIND, analysis_gap=0.0))  # 분석 사이 5초 간격은 시험에서 줄인다
    fake_ytdlp.serve(server)
    server.yt_videos[VID] = video_spec(yt_files)
    w = Web(tmp_path, server, tools, extensions=EXTS)
    w.h.store.set_platform_enabled("youtube", True)
    with w:
        w.login()
        yield w


def analyze(w, urls):
    r = w.call("POST", "/api/replay/analyze", json={"urls": urls})
    assert r.status_code == 200, r.text
    ident = r.json()["id"]
    return w.wait(lambda: (lambda a: a if a["status"] in ("completed", "failed") else None)(
        w.client.get(f"/api/replay/analyses/{ident}").json()), timeout=60, what="분석")


def enqueue(w, analysis, quality=None, confirm=True, key="yt-request-0001"):
    item = analysis["items"][0]
    body = {"analysis_id": analysis["id"], "items": [{"url": item["url"], "quality": quality or item["qualities"][0]["id"]}],
            "key": key, "confirm": confirm}
    return w.call("POST", "/api/replay/jobs", json=body)


def logs(w, ident):
    return w.client.get(f"/api/replay/jobs/{ident}/logs").json()["logs"]


def job(w, ident):
    return w.client.get(f"/api/replay/jobs/{ident}").json()


def wait_job(w, ident, statuses=("completed", "failed", "cancelled"), timeout=90):
    return w.wait(lambda: (lambda j: j if j["status"] in statuses else None)(job(w, ident)), timeout=timeout, what=f"작업 {statuses}")


# ---- 주소 인식과 거절 ----
def test_parse_urls():
    want = handler.parse(URL)
    assert want == {"url": URL, "platform": "youtube", "kind": "youtube", "video_id": VID}
    for text in (f"https://youtu.be/{VID}?t=10", f"youtube.com/shorts/{VID}", f"https://m.youtube.com/live/{VID}",
                 f"https://www.youtube.com/watch?v={VID}&list=PL123&t=30s", f"www.youtube.com/watch?feature=x&v={VID}"):
        assert handler.parse(text) == want, text
    for text in ("https://www.youtube.com/playlist?list=PL123", "https://www.youtube.com/@someone",
                 "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv", "https://www.youtube.com/watch?v=short"):
        assert handler.parse(text)["rejected"] is True, text
    for text in ("https://evil.example/watch?v=" + VID, "https://chzzk.naver.com/video/1", "ftp://youtube.com/watch?v=" + VID):
        with pytest.raises(media.Failure):
            handler.parse(text)
    assert handler.key(want) == f"youtube:{VID}"


def test_filename_rules():
    base = {"title": "제목: 이상한/글자", "channel": "채널", "video_id": VID}
    assert handler.filename(dict(base, open_date="2026-11-13 21:04:05")).startswith("[261113_210405] 채널 제목")
    assert handler.filename(dict(base, open_date="2026-11-13")).startswith("[261113] 채널 제목")
    assert "/" not in handler.filename(dict(base, open_date="2026-11-13"))


# ---- 분석, 코덱 설정에 따른 목록 ----
def test_analysis_lists_heights_and_codecs(yv, server):
    a = analyze(yv, [URL])
    item = a["items"][0]
    assert item["error"] is None and item["kind"] == "youtube" and item["mode"] == "split", item
    assert item["title"] == "유튜브 시험 영상" and item["channel"] == "가짜 채널" and item["duration_seconds"] == 20
    assert item["supports"] == {"section": False} and item["thumbnail"] is None
    labels = [q["label"] for q in item["qualities"]]
    assert [q["height"] for q in item["qualities"]] == [1440, 1080, 720]  # 높이마다 한 줄
    assert labels[0].startswith("1440p60 VP9") and labels[1].startswith("1080p H.264") and "약" in labels[1]
    assert [q["id"] for q in item["qualities"]] == ["271-140", "137-140", "136-140"]  # 음성은 AAC 2채널 중 가장 높은 것
    r = yv.call("PATCH", "/api/settings", json={"youtube_video_codec": "av1"})
    assert r.status_code == 200, r.text
    a = analyze(yv, [URL])
    assert a["items"][0]["qualities"][0]["label"].startswith("1440p60 AV1")
    assert yv.call("PATCH", "/api/settings", json={"youtube_video_codec": "h265"}).status_code == 400
    kinds = yv.client.get("/api/replay/kinds").json()["kinds"]
    assert [k["kind"] for k in kinds] == ["youtube"] and kinds[0]["title"] == "유튜브 영상"


def test_live_upcoming_login_rate_and_playlist(yv, server, yt_files):
    server.yt_videos["Livelivelv1"] = video_spec(yt_files, mode="live")
    server.yt_videos["Upcomingvv1"] = video_spec(yt_files, mode="upcoming")
    server.yt_videos["Loginloginv"] = video_spec(yt_files, mode="login")
    server.yt_videos["Ratelimitv1"] = video_spec(yt_files, mode="rate")
    a = analyze(yv, [f"https://youtu.be/{v}" for v in ("Livelivelv1", "Upcomingvv1", "Loginloginv", "Ratelimitv1")]
                + ["https://www.youtube.com/playlist?list=PL1"])
    errors = [i["error"] for i in a["items"]]
    assert errors[0] == errors[1] == "방송 중인 영상은 채널을 등록해 녹화해 주십시오."
    assert "쿠키 화면에서 유튜브 쿠키를 넣어 주십시오" in errors[2]
    assert errors[3] == "유튜브가 요청을 제한했습니다. 잠시 뒤 다시 시도해 주십시오."
    assert "재생목록·채널 주소는 받지 않습니다" in errors[4]
    # 쿠키가 거부되면 구분한다
    credentials.save(yv.h.cfg, "youtube", {"cookies_txt": COOKIES})
    server.yt_videos["Loginloginv"]["cookie_ok"] = False
    a = analyze(yv, ["https://youtu.be/Loginloginv"])
    assert "쿠키가 거부됐습니다" in a["items"][0]["error"]
    server.yt_videos["Loginloginv"]["cookie_ok"] = True
    a = analyze(yv, ["https://youtu.be/Loginloginv"])
    assert a["items"][0]["error"] is None


# ---- 두 파일 받아 합치기, 받은 영상 기억, 머리글·쿠키 ----
def test_download_two_files_and_merge(yv, server, tools, yt_files):
    credentials.save(yv.h.cfg, "youtube", {"cookies_txt": COOKIES})
    a = analyze(yv, [URL])
    r = enqueue(yv, a, quality="137-140")
    assert r.status_code == 200, r.text
    j = wait_job(yv, r.json()["jobs"][0]["id"])
    assert j["status"] == "completed", j
    assert j["filename"].startswith("[251113_") and "가짜 채널 유튜브 시험 영상" in j["filename"]
    out = yv.h.root / "replay" / j["filename"]
    kinds = sorted(s["codec_type"] for s in probe(tools, out)["streams"])
    assert kinds == ["audio", "video"]
    reqs = [x for x in server.yt_file_requests]
    assert reqs and all(x["cookie"] is None and x["agent"] == "fake-yt-agent" for x in reqs)
    sizes = []
    for x in reqs:
        a_, b_ = x["range"][6:].split("-")
        sizes.append(int(b_) - int(a_) + 1)
    assert max(sizes) <= 10 * 1024 * 1024
    log = yv.h.log_text()
    assert "sid-yv-secret-0001" not in log and "sapisid-yv-secret-0002" not in log
    # 받은 영상 기억(플랫폼과 영상 ID). 다시 넣으려 하면 확인을 받는다
    a = analyze(yv, [f"https://youtu.be/{VID}"])
    r = enqueue(yv, a, confirm=False, key="yt-request-0002")
    assert r.status_code == 409 and r.json()["duplicates"] == ["유튜브 시험 영상"]


# ---- 403 뒤 다시 분석해 잇기, 크기가 달라지면 처음부터 ----
def test_forbidden_then_reanalyze_and_continue(yv, server):
    a = analyze(yv, [URL])
    calls = len(server.yt_watch_calls)
    server.yt_fail[f"{VID}-137"] = [403]
    j = wait_job(yv, enqueue(yv, a, quality="137-140").json()["jobs"][0]["id"])
    assert j["status"] == "completed", j
    assert len(server.yt_watch_calls) - calls >= 2  # 작업의 분석 + 다시 분석


def test_pause_resume_and_size_change_restarts(yv, server, yt_files):
    server.yt_delay = 0.25
    a = analyze(yv, [URL])
    ident = enqueue(yv, a, quality="137-140").json()["jobs"][0]["id"]
    yv.wait(lambda: (job(yv, ident).get("downloaded_bytes") or 0) > 1024 * 1024, timeout=60, what="받기 시작")
    running = job(yv, ident)
    assert running["total_bytes"] == len(yt_files["video"]) + len(yt_files["audio"])  # 두 파일의 받은 양을 두 크기의 합과 견준다
    assert yv.call("POST", f"/api/replay/jobs/{ident}/pause").status_code == 200
    yv.wait(lambda: job(yv, ident)["status"] == "paused", timeout=30, what="일시정지")
    server.yt_delay = 0.0
    assert yv.call("POST", f"/api/replay/jobs/{ident}/resume").status_code == 200
    j = wait_job(yv, ident)
    assert j["status"] == "completed", j
    assert any("이어받습니다" in e["message"] for e in logs(yv, ident)), logs(yv, ident)
    # 크기가 바뀐 판: 일시정지 뒤 영상 파일이 달라지면 그 파일을 처음부터 받는다
    server.yt_delay = 0.25
    a = analyze(yv, [URL])
    ident = enqueue(yv, a, quality="137-140", key="yt-request-0003").json()["jobs"][0]["id"]
    yv.wait(lambda: (job(yv, ident).get("downloaded_bytes") or 0) > 1024 * 1024, timeout=60, what="받기 시작")
    yv.call("POST", f"/api/replay/jobs/{ident}/pause")
    yv.wait(lambda: job(yv, ident)["status"] == "paused", timeout=30, what="일시정지")
    server.yt_delay = 0.0
    server.yt_videos[VID]["files"]["137"]["data"] = yt_files["video"] + b"\x00" * 4096  # 크기가 다르다
    server.yt_generation += 1
    yv.call("POST", f"/api/replay/jobs/{ident}/resume")
    j = wait_job(yv, ident)
    assert j["status"] == "completed", j
    assert any("영상 정보가 바뀌어 처음부터" in e["message"] for e in logs(yv, ident)), logs(yv, ident)


# ---- 합치기 실패 뒤 재시도(다시 받지 않고 합치기부터) ----
def test_merge_failure_keeps_parts_then_retry_merges_only(tmp_path, server, tools, split_media, yt_files, monkeypatch):
    from test_web import Web

    flag = tmp_path / "failed-once"
    wrapper = tmp_path / "ffmpeg-once"
    wrapper.write_text(f"#!/bin/sh\ncase \"$*\" in *\"-map 1:a:0\"*) if [ ! -e {flag} ]; then : > {flag}; exit 1; fi;; esac\n"
                       f"exec {tools[0]} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PYTHONPATH", TESTS + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    monkeypatch.setenv(yt.BACKEND_ENV, "fake_ytdlp")
    monkeypatch.setenv("FAKE_YTDLP_BASE", server.base)
    monkeypatch.setattr(yv_ext, "KIND", replace(yv_ext.KIND, analysis_gap=0.0))
    fake_ytdlp.serve(server)
    server.yt_videos[VID] = video_spec(yt_files)
    w = Web(tmp_path / "w", server, tools, extensions=EXTS, ffmpeg=str(wrapper))
    w.h.store.set_platform_enabled("youtube", True)
    with w:
        w.login()
        a = analyze(w, [URL])
        ident = enqueue(w, a, quality="136-140").json()["jobs"][0]["id"]
        j = wait_job(w, ident)
        assert j["status"] == "failed" and "합치지 못했습니다" in j["error"], j
        work = w.h.root / "replay" / ".work" / ident
        assert (work / "video" / "part").stat().st_size == len(yt_files["video"])
        assert (work / "audio" / "part").stat().st_size == len(yt_files["audio"])
        before = len([x for x in server.yt_file_requests if x["range"] != "bytes=0-0"])
        assert w.call("POST", f"/api/replay/jobs/{ident}/retry").status_code == 200
        j = wait_job(w, ident)
        assert j["status"] == "completed", j
        assert len([x for x in server.yt_file_requests if x["range"] != "bytes=0-0"]) == before  # 다시 받지 않았다


# ---- 확장 조합·유튜브가 꺼졌을 때 ----
def test_platform_off_blocks_kind(yv, server):
    assert yv.call("POST", "/api/platforms/youtube/disable").status_code == 200
    assert yv.client.get("/api/replay/kinds").json()["kinds"] == []
    r = yv.call("POST", "/api/replay/analyze", json={"urls": [URL]})
    assert r.status_code == 400 and "유튜브를 쓰려면 설정에서 먼저 켜 주십시오" in r.text
    # 켜야 확장을 켤 수 있다
    r = yv.call("PUT", "/api/extensions", json={"enabled": ["downloads", "youtube_video"]})
    assert r.status_code == 400 and "먼저 켜야" in r.text
    assert yv.call("PUT", "/api/extensions", json={"enabled": ["downloads"]}).status_code == 200
    assert yv.call("POST", "/api/platforms/youtube/enable", json={"agree": True}).status_code == 200
    assert yv.call("PUT", "/api/extensions", json={"enabled": ["downloads", "youtube_video"]}).status_code == 200
    assert [k["kind"] for k in yv.client.get("/api/replay/kinds").json()["kinds"]] == ["youtube"]


def test_tab_title_rule_and_settings_slot(yv):
    from webui_files import all_js

    js = all_js()
    assert 'return downloadKinds.length === 1 ? downloadKinds[0].title : "다운로드";' in js
    assert 'const subtab = $("stab-replay");' in js
    html = yv.client.get("/").text
    assert 'name="youtube_video_codec"' in html and 'data-ext-setting="youtube_video_codec"' in html


OFF_SCRIPT = r'''
import json, sys
sys.path.insert(0, TESTS)
from pathlib import Path
from conftest import FakeChzzk, fast_timings
from test_web import Web
tmp = Path(TMP); media = tmp / "media"; media.mkdir()
server = FakeChzzk(media)
site = Web(tmp / "w", server, TOOLS, extensions=["downloads", "youtube_video"])
with site:
    site.login()
    kinds = site.client.get("/api/replay/kinds").json()["kinds"]
    r = site.call("POST", "/api/replay/analyze", json={"urls": ["https://www.youtube.com/watch?v=aqz-KE-bpKQ"]})
out = {"kinds": kinds, "analyze": r.status_code, "yt_dlp": sorted(m for m in sys.modules if m.split(".")[0] == "yt_dlp")}
server.close()
print("RESULT " + json.dumps(out))
'''


def test_extension_on_but_platform_off_never_loads_yt_dlp(tmp_path, tools):
    code = OFF_SCRIPT.replace("TESTS", repr(TESTS)).replace("TMP", repr(str(tmp_path))).replace("TOOLS", repr(tuple(tools)))
    env = {k: v for k, v in os.environ.items() if k not in (yt.BACKEND_ENV, yt.ENV_FORBID)}
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
    line = next((x for x in proc.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, proc.stderr[-2000:]
    out = json.loads(line[7:])
    assert out == {"kinds": [], "analyze": 400, "yt_dlp": []}, out


def test_unknown_url_message_follows_enabled_kinds(yv, server):
    """알아보지 못한 주소의 안내는 켜진 종류에 맞춘다. 치지직 종류만 켜져 있으면 예전 문장 그대로."""
    from live_recorder.ext.downloads.errors import ERROR_MESSAGES
    from polite_scan import plain_endings

    r = yv.call("POST", "/api/replay/analyze", json={"urls": ["https://example.com/x"]})
    assert r.status_code == 400 and r.json()["error"] == "지원하는 주소가 아닙니다. 받을 수 있는 주소: 유튜브 영상", r.text
    api.register_kind(api.JobKind("video", "replay", "다시보기", "live_recorder.ext.replay.chzzk_video", platform="chzzk"))
    try:
        assert api.unknown_url_message() == "지원하는 주소가 아닙니다. 받을 수 있는 주소: 유튜브 영상, 치지직 다시보기"
        assert plain_endings(api.unknown_url_message()) == []
        assert yv.call("POST", "/api/platforms/youtube/disable").status_code == 200
        assert api.unknown_url_message() == ERROR_MESSAGES["invalid_url"]  # 치지직 종류만
    finally:
        api.unregister_kind("video")
