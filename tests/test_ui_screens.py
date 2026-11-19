"""화면 개편: 카드 정보, 썸네일 주소와 CSP, 기록 구간 조회, 보기 전용 기록, 색 구성."""

import re
from pathlib import Path

from live_recorder.platforms.chzzk import platform as eng
from live_recorder import web
from live_recorder.core import store as st

from conftest import CHANNEL
from test_web import Web, site  # noqa: F401  (fixture)
from webui_files import all_js

UI = Path(web.UI_DIR)


def test_thumbnail_url_rules(monkeypatch):
    """`{type}`에는 480을 넣는다. 허용한 CDN·https만 준다."""
    raw = "https://livecloud-thumb.akamaized.net/chzzk/livecloud/KR/stream/1/live/2/record/3/thumbnail/image_{type}.jpg"
    assert eng.THUMB_TYPE == "480" and eng.thumbnail_url(raw) == raw.replace("{type}", "480")
    monkeypatch.setattr(eng, "THUMB_TYPE", None)  # 값이 없으면 자리표시 주소는 쓰지 않는다
    assert eng.thumbnail_url(raw) is None
    for bad in ("http://livecloud-thumb.akamaized.net/a.jpg", "https://evil.example/a.jpg",
                "https://x.akamaized.net/{size}/a.jpg", "https://akamaized.net.evil.example/a.jpg", "", None):
        assert eng.thumbnail_url(bad) is None, bad
    assert eng.thumbnail_url("https://x.akamaized.net/a.jpg") == "https://x.akamaized.net/a.jpg"


def test_csp_opens_only_the_video_cdn_for_images(site):
    site.login()
    csp = site.client.get("/").headers["content-security-policy"]
    img = next(part for part in csp.split(";") if part.strip().startswith("img-src")).split()[1:]
    # 유튜브 썸네일 호스트(ytimg.com)가 더해졌다
    # 씨미 썸네일 호스트(streaming.cf.ci.me를 덮는 *.cf.ci.me)가 더해졌다
    assert img == ["'self'", "data:", "https://*.pstatic.net", "https://*.akamaized.net", "https://*.ytimg.com",
                   "https://*.cf.ci.me"]
    assert "script-src 'self';" in csp and "connect-src 'self';" in csp
    for page in ("index.html", "login.html", "setup.html"):  # 외부 CDN·폰트·라이브러리를 부르지 않는다
        html = (UI / page).read_text()
        assert not re.search(r'(?:src|href)="(?:https?:)?//', html), page
        assert '<script src="/static/theme.js"></script>' in html
    assert site.client.get("/static/theme.js").status_code == 200


def test_cards_get_quality_size_and_thumbnail_fields(site, server, monkeypatch):
    """녹화 중 카드: 화질(1080p60), 받은 용량, 파일 시작 시각. 성인 방송은 주소 없이 표시만."""
    site.login()
    assert site.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "카드", "output_dir": "a"}).status_code == 201
    site.h.store.set_settings({"rewind_seconds": 0})
    b = server.go_live(title="카드 방송")
    site.wait(site.h.recording, what="녹화")
    data = site.wait(lambda: (d := site.client.get("/api/channels").json())["channels"][0]["bytes"] and d, what="용량")
    ch = data["channels"][0]
    assert ch["state"] == "recording" and ch["quality_label"] == "1080p60" and ch["bytes"] > 0 and ch["file_started_at"]
    assert ch["thumb_url"] is None and ch["adult"] is False  # 시험 서버 주소(127.0.0.1)는 허용 CDN이 아니다
    assert data["disk"]["free"] > 0 and data["disk"]["total"] >= data["disk"]["free"]
    worker = site.app.state.engine.workers[("chzzk", CHANNEL)]
    worker.live_image = "https://livecloud-thumb.akamaized.net/t/image_{type}.jpg"
    assert site.client.get("/api/channels").json()["channels"][0]["thumb_url"].endswith("image_480.jpg")
    worker.live_adult = True
    ch = site.client.get("/api/channels").json()["channels"][0]
    assert ch["thumb_url"] is None and ch["adult"] is True
    b.end()
    site.wait(lambda: site.h.finalized(1), what="마무리")
    ch = site.wait(lambda: (c := site.client.get("/api/channels").json()["channels"][0])["state"] == "watching" and c, what="감시")
    assert ch["last_started_at"] and ch["last_seconds"] > 0 and ch["quality_label"] is None and ch["thumb_url"] is None
    s = site.h.sessions()[0]
    assert s["quality_label"] == "1080p60" and s["title"] == "카드 방송" and s["live_id"] == "1001"


def test_session_range_query_and_grouping(site):
    """구간과 겹치는 세션만, 같은 방송은 같은 묶음. 잘못된 구간은 거부한다."""
    site.login()
    store = site.h.store
    store.add_channel(CHANNEL, "기록", "a", auto_record=False)
    old = store.create_session(channel_id=CHANNEL, live_id="7", title="지난 방송", started_at="2026-11-01T10:00:00.000+00:00",
                               ended_at="2026-11-01T12:00:00.000+00:00", end_reason=st.END_BROADCAST)
    a = store.create_session(channel_id=CHANNEL, live_id="9", title="이번 방송", started_at="2026-11-10T10:00:00.000+00:00",
                             ended_at="2026-11-10T11:00:00.000+00:00", end_reason=st.END_ERROR, note="방송 중 끊김. 새 파일로 다시 연결",
                             quality_label="1080p60", url_refreshes=2)
    b = store.create_session(channel_id=CHANNEL, live_id="9", title="이번 방송", started_at="2026-11-10T11:00:01.000+00:00",
                             ended_at="2026-11-10T13:00:00.000+00:00", end_reason=st.END_BROADCAST)
    live = store.create_session(channel_id=CHANNEL, live_id="10", title="녹화 중", started_at="2026-11-11T09:00:00.000+00:00")
    r = site.client.get("/api/sessions", params={"start": "2026-11-09T00:00:00+09:00", "end": "2026-11-12T00:00:00+09:00"})
    rows = r.json()["sessions"]
    assert [x["id"] for x in rows] == [live, b, a]  # 최신이 앞. 지난 방송은 구간 밖
    by = {x["id"]: x for x in rows}
    assert by[a]["group"] == by[b]["group"] != by[live]["group"]
    assert by[a]["ch"] == 1 and by[a]["channel_name"] == "기록" and by[a]["url_refreshes"] == 2
    assert by[live]["ended_at"] is None  # 진행 중인 것도 구간에 든다
    assert old in [x["id"] for x in site.client.get("/api/sessions", params={"start": "2026-10-31T00:00:00Z",
                                                                              "end": "2026-11-02T00:00:00Z"}).json()["sessions"]]
    for bad in ({"start": "어제"}, {"start": "2026-11-10T00:00:00Z", "end": "2026-11-09T00:00:00Z"},
                {"start": "2026-01-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"}):
        assert site.client.get("/api/sessions", params=bad).status_code == 400, bad
    # 옛 기록(제목·liveId가 빈 행)도 깨지지 않는다
    store.create_session(channel_id=CHANNEL, started_at="2026-11-10T14:00:00.000+00:00", ended_at="2026-11-10T14:10:00.000+00:00")
    rows = site.client.get("/api/sessions", params={"start": "2026-11-10T00:00:00Z", "end": "2026-11-11T00:00:00Z"}).json()["sessions"]
    blank = next(x for x in rows if x["title"] is None)
    assert blank["group"] is None and blank["live_id"] is None


def test_view_sessions_needs_login_when_account_exists(site):
    """보기 전용 기록 경로는 계정이 있으면 로그인을 요구한다."""
    assert web.access_level("GET", "/api/view/sessions") == web.VIEW
    assert web.access_level("POST", "/api/view/sessions") == web.LOGIN
    assert site.client.get("/api/view/sessions").status_code == 401
    site.login()
    assert site.client.get("/api/view/sessions").status_code == 200


def test_screen_code_follows_ui_rules():
    """화면 코드: 썸네일 1분 제한, 색 구성은 브라우저에만 저장, 보기 전용은 채널·기록 탭만, 정오 기준 줄."""
    js = all_js()
    theme = (UI / "theme.js").read_text()
    css = (UI / "style.css").read_text()
    assert "const THUMB_MIN_MS = 60000;" in js
    assert 'localStorage.setItem("lr-theme", theme)' in js and 'localStorage.getItem("lr-theme")' in theme
    assert ':root[data-theme="dark"]' in css and ':root:not([data-theme="light"])' in css
    assert 'const VIEW_ONLY_TABS = ["channels", "history"];' in js
    assert "const NOON_MS = 12 * 3600000;" in js and "const TIMELINE_DAYS = 7;" in js
    assert '"끊겨 재접속"' in js and '"이어 받음"' in js
    assert "calc((100% - 3 * var(--gap)) / 4)" in css  # 한 줄 최대 4개
    assert "@media (max-width: 640px)" in css and ".card-grid { grid-template-columns: minmax(0, 1fr); }" in css
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"):
        assert banned not in js + theme
    from webui_files import script_paths

    for path in [UI / "theme.js", UI / "index.html", UI / "style.css"] + script_paths():
        assert path.stat().st_mode & 0o044 == 0o044, path  # 컨테이너 사용자가 읽을 수 있다


# --- 정적 파일 캐시 -------------------------------------------------------------

FIXED_MTIME = 1767225600  # 2026-01-01T00:00:00Z. 소스 묶음이 시각을 이렇게 고정한다


def _static_client(directory, cls=web.UIStaticFiles):
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.testclient import TestClient

    return TestClient(Starlette(routes=[Mount("/static", cls(directory=directory))]))


def _write_fixed(path, text):
    import os

    path.write_text(text)
    os.utime(path, (FIXED_MTIME, FIXED_MTIME))


def test_static_files_are_revalidated_and_pages_are_not_stored(site):
    """/static/은 no-cache와 내용 ETag, 같으면 304. 화면 문서와 API는 지금처럼 no-store."""
    import hashlib

    r = site.client.get("/static/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    body = (UI / "app.js").read_bytes()
    assert r.content == body and r.headers["etag"] == f'"{hashlib.sha256(body).hexdigest()[:32]}"'
    again = site.client.get("/static/app.js", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["cache-control"] == "no-cache" and again.headers["etag"] == r.headers["etag"]
    assert site.client.get("/static/app.js", headers={"If-None-Match": '"other"'}).status_code == 200
    # 시각만 맞는 요청(If-Modified-Since)에는 304를 주지 않는다. 시각은 빌드마다 같다
    assert site.client.get("/static/app.js", headers={"If-Modified-Since": r.headers["last-modified"]}).status_code == 200
    assert site.client.head("/static/style.css").headers["cache-control"] == "no-cache"
    for path in ("/login", "/setup", "/"):
        assert site.client.get(path).headers["cache-control"] == "no-store", path
    site.login()
    assert site.client.get("/").headers["cache-control"] == "no-store"
    assert site.client.get("/api/channels").headers["cache-control"] == "no-store"


def test_static_change_with_same_size_and_time_is_sent_again(tmp_path):
    """크기와 수정 시각이 같고 내용만 다른 파일도 새로 내려간다(Starlette 기본 ETag는 이 경우 304를 준다)."""
    from fastapi.staticfiles import StaticFiles

    target = tmp_path / "app.js"
    _write_fixed(target, 'console.log("build-a");\n')
    client = _static_client(tmp_path)
    first = client.get("/static/app.js")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-cache"
    stat_a = target.stat()
    old_default = _static_client(tmp_path, StaticFiles).get("/static/app.js").headers["etag"]

    _write_fixed(target, 'console.log("build-b");\n')  # 같은 크기, 같은 시각
    stat_b = target.stat()
    assert (stat_a.st_size, stat_a.st_mtime) == (stat_b.st_size, stat_b.st_mtime)
    # 전제: 기본 StaticFiles는 같은 ETag를 내서 이전 내용을 계속 쓰게 한다
    default = _static_client(tmp_path, StaticFiles)
    assert default.get("/static/app.js", headers={"If-None-Match": old_default}).status_code == 304

    second = client.get("/static/app.js", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 200 and second.text == 'console.log("build-b");\n'
    assert second.headers["etag"] != first.headers["etag"] and second.headers["cache-control"] == "no-cache"
    assert second.headers["last-modified"] == first.headers["last-modified"]
    same = client.get("/static/app.js", headers={"If-None-Match": f'W/{second.headers["etag"]}, "x"'})
    assert same.status_code == 304 and same.headers["cache-control"] == "no-cache"


# --- 시작 실패 안내 -------------------------------------------------------------

BOOT_GUIDE = "화면을 불러오지 못했습니다. 새로고침(Ctrl+Shift+R)하십시오"
HARNESS = Path(__file__).resolve().parent / "ui_boot_harness.js"
MODE_ADMIN = {"body": {"mode": "login", "password": {"min": 8, "max": 1024, "text": "8자 이상"}}}
SESSION_OK = {"body": {"csrf": "x", "username": "admin"}}
CHANNELS_OK = {"body": {"channels": [], "disk": {"free": 1, "total": 2}}}
SCREENS = {"index": ("index.html", "app.js"), "login": ("login.html", "login.js"), "setup": ("setup.html", "setup.js")}


def boot_screen(screen, responses=None, remove=(), scripts=None, **extra):
    """화면 HTML과 스크립트를 가짜 DOM에서 돌린다(tests/ui_boot_harness.js). 브라우저 대신 Node를 쓴다."""
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node가 없다")
    page, script = SCREENS[screen]
    cfg = {"html": str(UI / page), "remove": list(remove), "responses": responses or {},
           "scripts": scripts or [str(UI / "theme.js"), str(UI / "boot.js"), str(UI / script)], **extra}
    out = subprocess.run([node, str(HARNESS), json.dumps(cfg)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def assert_failed(result, secret_hint=None):
    assert result["panelHidden"] is False and result["retryFocused"]
    assert all(x["hidden"] for x in result["hideTargets"])
    assert result["consoleErrors"]  # 오류 내용은 콘솔에만
    assert result["toast"] == ""  # 오류 문장을 화면(알림)에 내보내지 않는다
    if secret_hint:
        assert any(secret_hint in e for e in result["consoleErrors"])


def assert_ok(result):
    assert result["panelHidden"] is True and result["consoleErrors"] == [], result["consoleErrors"]


def test_boot_guide_markup_and_code_rules():
    """세 화면에 같은 고정 안내와 다시 불러오기 버튼이 있고, 인라인 스크립트·스타일이 없다."""
    for page, script in SCREENS.values():
        html = (UI / page).read_text()
        assert html.index('<script src="/static/theme.js"></script>\n<script src="/static/boot.js"></script>') < html.index("<body")
        assert f'<script src="/static/{script}"></script>' in html
        panel = html[html.index('<section id="boot-failed"'):html.index("</section>", html.index('id="boot-failed"'))]
        assert 'class="boot-failed" role="alert" hidden>' in panel and BOOT_GUIDE in panel
        assert '<button id="boot-retry" class="btn primary" type="button">다시 불러오기</button>' in panel
        assert not re.search(r"<script(?![^>]*\bsrc=)", html) and not re.search(r"\son[a-z]+=", html) and "style=" not in html
        assert "data-boot-hide" in html
        js = (UI / script).read_text()
        assert "window.lrBoot.run(" in js and "boot().catch((e) => toast(" not in js
    boot = (UI / "boot.js").read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "textContent"):
        assert banned not in boot, banned  # 안내 문구는 HTML에 고정. 오류 내용을 화면에 넣지 않는다
    assert 'console.error("화면을 시작하지 못했다", err)' in boot
    assert ".boot-failed:not([hidden])" in (UI / "style.css").read_text()  # hidden이 배치 규칙에 지지 않는다
    assert (UI / "boot.js").stat().st_mode & 0o044 == 0o044


def test_main_screen_boot_failures_show_the_guide():
    """첫 요청 실패(거부된 Promise)와 화면에서 요소가 빠진 경우(이전 빌드 스크립트) 모두 안내가 보인다."""
    assert_failed(boot_screen("index", {"/api/mode": "reject"}), "/secret/internal/path")
    missing = boot_screen("index", {"/api/mode": MODE_ADMIN, "/api/session": SESSION_OK}, remove=["channel-form"])
    assert_failed(missing, "reading 'quality'")
    assert all(v["hidden"] for v in missing["views"])
    clicked = boot_screen("index", {"/api/mode": "reject"}, clickRetry=True)
    assert clicked["reloadsAfterClick"] == 1  # 다시 불러오기 버튼


def test_main_screen_normal_boot_and_login_redirect_stay_as_before():
    """정상 시작은 안내 없이 채널 탭을 연다. 세션이 없으면(401) 지금처럼 로그인으로 보내고 안내를 띄우지 않는다."""
    ok = boot_screen("index", {"/api/mode": MODE_ADMIN, "/api/session": SESSION_OK, "/api/channels": CHANNELS_OK})
    assert_ok(ok)
    assert [v["id"] for v in ok["views"] if not v["hidden"]] == ["view-channels"]
    expired = boot_screen("index", {"/api/mode": MODE_ADMIN, "/api/session": {"status": 401, "body": {"error": "x"}}})
    assert expired["replaced"] == ["/login"] and expired["panelHidden"] is True
    view = boot_screen("index", {"/api/mode": {"body": {"mode": "view", "guide": "보기 전용"}}, "/api/view/channels": CHANNELS_OK})
    assert_ok(view)
    assert [v["id"] for v in view["views"] if not v["hidden"]] == ["view-channels"]


def test_login_and_setup_boot_failures_show_the_guide(tmp_path):
    """로그인(동기 예외)과 첫 설정(규칙 요청 실패) 화면도 같다. 정상이면 안내가 없다."""
    assert_ok(boot_screen("login"))
    assert_failed(boot_screen("login", remove=["login-form"]), "addEventListener")
    ok = boot_screen("setup", {"/api/mode": MODE_ADMIN})
    assert_ok(ok)
    assert ok["pwRule"] == "8자 이상"
    assert_failed(boot_screen("setup", {"/api/mode": "reject"}), "/secret/internal/path")
    assert_failed(boot_screen("setup", {"/api/mode": {"status": 500, "body": {"error": "x"}}}))
    # 화면 스크립트를 받지 못했거나(404) 맨 위에서 던진 경우(이전 빌드 스크립트)
    assert_failed(boot_screen("login", scripts=[str(UI / "theme.js"), str(UI / "boot.js")]))
    broken = tmp_path / "old.js"
    broken.write_text('"use strict";\ndocument.getElementById("gone").addEventListener("click", () => {});\n')
    assert_failed(boot_screen("login", scripts=[str(UI / "theme.js"), str(UI / "boot.js"), str(broken)]), "addEventListener")
