"""보기 전용 모드. "인증 없는 접근"의 계정 없는 경우.

계정이 없으면 채널 목록·상태·방송 제목·녹화 경과 시간·현재 파일 이름만 보인다. 허용 목록 밖은 전부 거부한다.
녹화 엔진은 모드와 상관없이 녹화한다. 계정을 만들거나 지우면 재시작 없이 바로 바뀐다.
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from live_recorder.platforms import credentials
from live_recorder.core import logs, secretstore
from live_recorder import web

from conftest import CHANNEL, Harness
from test_web import all_routes
from webui_files import index_html, all_js

AUT, SES, TG = "AUT-view-sentinel-0001", "SES-view-sentinel-0002", "123456:TG-view-sentinel-0003"
PATTERN = "{channel_name} SENTINELPATTERN {safe_live_title}{file_extension}"
USER, PASSWORD = "admin", "correct horse battery"


class ViewSite:
    def __init__(self, tmp_path, server, tools):
        self.h = Harness(tmp_path, server, tools)
        logs.setup(self.h.cfg.log_dir, console=False)
        self.app = web.create_app(self.h.cfg, self.h.timings)
        self.client = TestClient(self.app, base_url="http://testserver", follow_redirects=False)

    def __enter__(self):
        self.client.__enter__()
        return self

    def __exit__(self, *exc):
        self.client.__exit__(*exc)
        logs.setup(None, console=False)

    def wait(self, pred, timeout=20.0, what="조건"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = pred()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"시간 안에 만족하지 못했다: {what}")


@pytest.fixture
def view(tmp_path, server, tools):
    with ViewSite(tmp_path, server, tools) as v:
        yield v


ALLOWED = {(m, p) for m, p, level in web.ACCESS_RULES if level in (web.PUBLIC, web.VIEW) and "*" not in p}


def body_for(method):
    return None if method in ("GET", "DELETE") else {
        "channel_id": CHANNEL, "name": "x", "output_dir": "a", "auto_record": False, "NID_AUT": "x", "bot_token": "x",
        "chat_id": "1", "urls": ["https://chzzk.naver.com/video/1"], "root": "x", "path": "a", "new_name": "b",
        "paths": ["a"], "dest_root": "x", "dest_path": "", "mode": "permanent", "current": "x", "new": "y" * 12,
        "analysis_id": "0" * 32, "items": [], "key": "k" * 10, "username": "a", "password": "b"}


def test_every_route_outside_allow_list_is_refused(view):
    """계정이 없을 때 등록된 모든 경로를 돈다. 허용 목록(PUBLIC·VIEW) 밖은 전부 403과 안내다."""
    routes = all_routes(view.app)
    assert len(routes) >= 40
    assert ("GET", "/api/view/channels") in routes and ("GET", "/") in routes
    refused = 0
    for method, path in routes + [("GET", "/api/nope"), ("GET", "/docs"), ("POST", "/api/view/channels"),
                                  ("DELETE", "/api/view/channels"), ("GET", "/api/view/other")]:
        if (method, path) in ALLOWED or path.startswith("/static/"):
            continue
        r = view.client.request(method, path, json=body_for(method),
                                params={"root": "x", "path": ""} if method == "GET" else None)
        assert r.status_code == 403, (method, path, r.status_code, r.text[:200])
        data = r.json()
        assert data["view_only"] is True and "/setup" in data["error"] and "setup-code" in data["error"], (method, path)
        refused += 1
    assert refused >= 35
    # 상태를 바꾸려던 요청이 아무것도 바꾸지 않았다
    assert view.h.store.list_channels() == [] and credentials.load(view.h.cfg, "chzzk") == {}
    assert not view.h.cfg.telegram_path.exists()
    # 허용된 경로는 열린다. 계정이 없으면 첫 화면은 첫 설정이다. 보기 전용 화면은 /?view=1
    r = view.client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert view.client.get("/?view=1").status_code == 200
    assert view.client.get("/setup").status_code == 200
    assert view.client.get("/api/view/channels").json()["channels"] == []
    assert view.client.get("/api/view/sessions").json()["sessions"] == []
    assert view.client.get("/status").status_code == 200
    assert view.client.get("/api/mode").json()["mode"] == "view"
    r = view.client.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/setup"  # 로그인할 계정이 없다. 첫 설정으로
    assert view.client.post("/api/login", json={"username": "admin", "password": "anything"}).status_code == 401


def test_allow_list_is_one_place_and_small():
    """보기 전용에서 열리는 경로는 ACCESS_RULES 한 곳에 있다. 보기 경로 둘과 첫 설정의 두 경로뿐이다."""
    view_rules = {(m, p) for m, p, level in web.ACCESS_RULES if level == web.VIEW}
    assert view_rules == {("GET", "/"), ("GET", "/api/view/channels"), ("GET", "/api/view/sessions"), ("GET", "/setup"),
                          ("POST", "/api/setup")}  # 기록 화면의 보기가 더해졌다
    assert web.access_level("GET", "/api/brand-new-route") == web.LOGIN  # 새 경로는 기본이 거부다
    assert web.access_level("POST", "/") == web.LOGIN


def test_engine_records_and_view_shows_only_allowed_fields(view, server):
    """녹화는 모드와 상관없이 하고, 보기 경로 응답에는 쿠키·토큰·설정값이 없다."""
    view.h.store.add_channel(CHANNEL, "보기채널", "SENTINELFOLDER", quality="720p")
    view.h.store.set_settings({"rewind_seconds": 0, "filename_pattern": PATTERN, "fm_roots": ["/SENTINELROOT"],
                               "trusted_proxies": ["198.51.100.77"]})
    view.h.store.set_extra({"ext:downloads:replay_dir": "SENTINELREPLAY"})  # 다운로드 저장 폴더는 확장 설정
    credentials.save(view.h.cfg, "chzzk", {"NID_AUT": AUT, "NID_SES": SES})
    secretstore.telegram_save(view.h.cfg.telegram_path, TG, "-100555")
    server.go_live(title="보기 방송")
    view.wait(lambda: view.client.get("/api/view/channels").json()["channels"]
              and view.client.get("/api/view/channels").json()["channels"][0]["state"] == "recording", what="녹화")
    time.sleep(1.2)
    r = view.client.get("/api/view/channels")
    ch = r.json()["channels"][0]
    assert set(ch) == {"name", "state", "state_label", "live_title", "elapsed", "file", "error_flag", "error_count", "bytes",
                       "file_started_at", "thumb_url", "adult", "last_started_at", "last_seconds", "error_reason"}
    assert ch["name"] == "보기채널" and ch["state_label"] == "녹화" and ch["live_title"] == "보기 방송" and ch["elapsed"] >= 1
    assert ch["file"].endswith(".mp4") and "/" not in ch["file"]
    sessions = view.client.get("/api/view/sessions")
    assert sessions.status_code == 200
    row = sessions.json()["sessions"][0]
    assert row["channel_name"] == "보기채널" and row["title"] == "보기 방송" and row["ch"] == 1 and row["group"] == 1
    assert not {"channel_id", "live_id", "end_detail", "finalize_detail", "note", "quality_label"} & set(row)
    blob = r.text + sessions.text + view.client.get("/?view=1").text + view.client.get("/setup").text + view.client.get("/api/mode").text + view.client.get("/status").text
    for secret in (AUT, SES, TG, "-100555", "SENTINELFOLDER", "SENTINELROOT", "SENTINELREPLAY", "198.51.100.77",
                   "{channel_name}", CHANNEL, "720p", str(view.h.root)):
        assert secret not in blob, secret
    # 쿠키는 녹화 API 요청에는 쓰였다(엔진은 그대로 돈다)
    assert any(c == f"NID_AUT={AUT}; NID_SES={SES}" for p, c in server.calls if p.startswith("/api/"))


def test_mode_switches_without_restart(view):
    """계정을 만드는 순간 보기 경로도 로그인을 요구하고, 지우면 다시 보기 전용이 된다."""
    assert view.client.get("/api/view/channels").status_code == 200
    secretstore.create_account(view.h.cfg.account_path, USER, PASSWORD)
    assert view.client.get("/api/view/channels").status_code == 401
    assert view.client.get("/").status_code == 303 and view.client.get("/").headers["location"] == "/login"
    assert view.client.get("/api/mode").json()["mode"] == "login"
    assert view.client.get("/login").status_code == 200
    r = view.client.post("/api/login", json={"username": USER, "password": PASSWORD})
    assert r.status_code == 200
    csrf = view.client.get("/api/session").json()["csrf"]
    assert view.client.post("/api/channels", json={"channel_id": CHANNEL, "name": "x", "output_dir": "a"},
                            headers={"X-CSRF-Token": csrf}).status_code == 201
    # 계정을 지우면 열려 있던 세션도 쓸 수 없고 보기 전용이 된다
    secretstore.remove(view.h.cfg.account_path)
    r = view.client.post("/api/channels/" + CHANNEL + "/stop", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 403 and r.json()["view_only"] is True
    assert view.client.get("/api/session").status_code == 403
    assert view.client.get("/api/view/channels").json()["channels"][0]["name"] == "x"
    # 다시 만들면 예전 세션은 되살아나지 않는다(세대 값이 다르다)
    secretstore.create_account(view.h.cfg.account_path, USER, PASSWORD)
    assert view.client.get("/api/session").status_code == 401


def test_screen_hides_controls_and_shows_guide():
    """화면도 보기 전용이면 조작을 숨기고 CLI 안내를 보여 준다(서버가 같은 경로를 거부하는 것은 위에서 본다)."""
    js = all_js()
    assert "/api/mode" in js and "bootViewOnly" in js and "/api/view/channels" in js
    html = index_html()
    assert 'id="view-only-banner"' in html
    assert "/setup" in web.VIEW_ONLY_GUIDE and "account setup-code" in web.VIEW_ONLY_GUIDE
    assert 'href="/setup"' in html


def test_cli_web_starts_in_view_mode(tmp_path, tools):
    """계정 없이 `web`이 보기 전용으로 뜨고 녹화 엔진이 돈다."""
    import os
    import signal
    import subprocess
    import sys
    from conftest import ROOT

    data, root = tmp_path / "data", tmp_path / "rec"
    root.mkdir()
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(data), LIVE_RECORDER_RECORD_ROOT=str(root),
               LIVE_RECORDER_FFMPEG=tools[0], LIVE_RECORDER_FFPROBE=tools[1], PYTHONPATH=str(ROOT))
    proc = subprocess.Popen([sys.executable, "-m", "live_recorder", "web", "--port", "18093", "--quiet"], cwd=ROOT, env=env,
                            stderr=subprocess.PIPE, text=True)
    try:
        import httpx
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                r = httpx.get("http://127.0.0.1:18093/api/mode", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert r.json()["mode"] == "view"
        assert httpx.get("http://127.0.0.1:18093/api/settings").status_code == 403
        assert (data / "instance.lock").exists()  # 엔진이 떴다
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
    assert "보기 전용" in proc.stderr.read()
