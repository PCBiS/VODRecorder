"""웹 API와 화면 (ADM, SEC). 인증 없는 접근, CSRF, 빠진 폼 필드, 쿠키 노출."""

import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from live_recorder.platforms import credentials
from live_recorder import cli, web
from live_recorder.core import logs, secretstore
from live_recorder.core import store as st

from conftest import CHANNEL, Harness, fast_timings
from webui_files import index_html, all_js

USER, PASSWORD = "admin", "correct horse battery"
AUT, SES, TG = "AUT-s3cr3t-sentinel-0001", "SES-s3cr3t-sentinel-0002", "123456:TG-s3cr3t-sentinel-0003"


class Web:
    def __init__(self, tmp_path, server, tools, extensions=(), **kwargs):
        self.h = Harness(tmp_path, server, tools, **kwargs)
        if extensions:  # 처음 설치는 확장이 모두 꺼져 있다. 다시보기 시험은 켜고 띄운다
            self.h.store.set_extra({"ext:enabled": list(extensions)})
        self.server = server
        secretstore.create_account(self.h.cfg.account_path, USER, PASSWORD)
        logs.setup(self.h.cfg.log_dir, console=False)
        self.app = web.create_app(self.h.cfg, self.h.timings, disk_free=self.h.disk_free)
        self.client = TestClient(self.app, base_url="http://testserver", follow_redirects=False)
        self.csrf = ""

    def __enter__(self):
        self.client.__enter__()
        return self

    def __exit__(self, *exc):
        self.client.__exit__(*exc)
        logs.setup(None, console=False)

    def login(self, client=None):
        c = client or self.client
        r = c.post("/api/login", json={"username": USER, "password": PASSWORD})
        assert r.status_code == 200, r.text
        self.csrf = c.get("/api/session").json()["csrf"]
        return r

    def call(self, method, path, **kwargs):
        headers = kwargs.pop("headers", {})
        headers.setdefault("X-CSRF-Token", self.csrf)
        return self.client.request(method, path, headers=headers, **kwargs)

    def wait(self, pred, timeout=20.0, what="조건"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = pred()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"시간 안에 만족하지 못했다: {what}; {self.h.brief()}")


@pytest.fixture
def site(tmp_path, server, tools):
    with Web(tmp_path, server, tools) as w:
        yield w


def _flat_routes(routes, prefix=""):
    """FastAPI 0.16x는 include_router한 라우터를 따로 품는다. 그 안의 경로까지 펼친다."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _flat_routes(inner.routes, prefix + (inner.prefix or ""))
            continue
        yield prefix, route


def all_routes(app) -> list[tuple[str, str]]:
    out = []
    for prefix, route in _flat_routes(app.routes):
        path = getattr(route, "path", None)
        if path and prefix and not path.startswith(prefix):
            path = prefix + path
        if not path:
            continue
        path = path.replace("{channel_id}", CHANNEL).replace("{path:path}", "app.js")
        for method in sorted(getattr(route, "methods", None) or {"GET"}):
            if method not in ("HEAD", "OPTIONS"):
                out.append((method, path))
    return out


def test_unauthenticated_access_is_denied(site):
    """모든 경로를 세션 없이 호출한다. 허용 목록 밖은 전부 거부된다."""
    public = {("GET", "/login"), ("GET", "/status"), ("POST", "/api/login"), ("GET", "/api/mode")}
    routes = all_routes(site.app)
    assert len(routes) >= 20
    extra = [("GET", p) for p in ("/docs", "/redoc", "/openapi.json", "/admin", "/api/", "/api/nope", "/.env", "/web.py",
                                  "/login/extra", "/statusx", "//status/..")]
    extra += [(m, "/api/settings") for m in ("PUT", "DELETE", "POST")]
    denied = 0
    for method, path in routes + extra:
        r = site.client.request(method, path, json={} if method != "GET" else None)
        if (method, path) in public:
            assert r.status_code in (200, 401, 422), (method, path, r.status_code)  # 닿기는 한다
            continue
        if path.startswith("/static/"):
            assert r.status_code in (200, 404), (method, path, r.status_code)
            continue
        denied += 1
        if method == "GET" and not path.startswith("/api/"):
            assert r.status_code == 303 and r.headers["location"] == "/login", (method, path, r.status_code)
        else:
            assert r.status_code == 401, (method, path, r.status_code, r.text)
        assert "channels" not in r.text and "csrf" not in r.text
    assert denied >= 20
    # 잘못된·만료된 세션 쿠키도 같다
    site.client.cookies.set(web.SESSION_COOKIE, "forged-token")
    assert site.client.get("/api/channels").status_code == 401
    # 정적 자원에 비밀이 없다
    for name in ("app.js", "login.js", "style.css", "login.html", "index.html"):
        assert site.client.get(f"/static/{name}").status_code == 200
    assert site.client.get("/static/../web.py").status_code in (303, 401, 404)


def test_login_logout_and_session_cookie(site):
    """세션 쿠키는 HttpOnly·SameSite이고 HTTPS에서 Secure다."""
    r = site.login()
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" not in cookie and "Path=/" in cookie
    # 표시 이름은 코드 한 곳(PROGRAM_NAME)에서 온다. 화면 문서의 이름 자리는 남지 않는다
    from live_recorder import NAME_SLOT, PROGRAM_NAME
    page = site.client.get("/").text
    assert site.client.get("/").status_code == 200 and f"<title>{PROGRAM_NAME}</title>" in page and NAME_SLOT not in page
    assert site.client.get("/api/session").json()["username"] == USER
    # HTTPS에서는 Secure가 붙는다. 신뢰 프록시가 아닌 곳에서 온 X-Forwarded-Proto는 보지 않는다(신뢰 프록시 경우는
    # test_security_fixes.test_forwarded_proto_only_from_trusted_proxy)
    other = TestClient(site.app, base_url="http://testserver")
    r2 = other.post("/api/login", json={"username": USER, "password": PASSWORD}, headers={"X-Forwarded-Proto": "https"})
    assert "Secure" not in r2.headers["set-cookie"]
    secure = TestClient(site.app, base_url="https://testserver")
    r3 = secure.post("/api/login", json={"username": USER, "password": PASSWORD})
    assert "Secure" in r3.headers["set-cookie"]
    assert site.call("POST", "/api/logout").status_code == 200
    assert site.client.get("/api/session").status_code == 401
    assert site.client.get("/", follow_redirects=False).status_code == 303


def test_login_failures_are_limited_and_password_is_not_logged(site):
    for i in range(5):
        r = site.client.post("/api/login", json={"username": USER, "password": f"wrong-password-{i}"})
        assert r.status_code == 401
    r = site.client.post("/api/login", json={"username": USER, "password": PASSWORD})
    assert r.status_code == 429  # 맞는 비밀번호도 잠금 동안은 안 된다
    assert site.client.get("/api/session").status_code == 401
    log = site.h.log_text()
    assert "login_failed" in log and "login_locked" in log
    assert "wrong-password" not in log and PASSWORD not in log
    account = site.h.cfg.account_path
    assert PASSWORD.encode() not in account.read_bytes() and oct(account.stat().st_mode & 0o777) == "0o600"
    # 계정이 없으면 로그인할 수 없고, 웹에는 계정을 만드는 경로가 없다
    account_routes = {(m, p) for m, p in all_routes(site.app) if "account" in p or "register" in p or "signup" in p}
    assert account_routes == {("POST", "/api/account/password")}  # 만드는 경로는 없고 비밀번호 변경만 있다
    with pytest.raises(secretstore.AccountError):
        secretstore.create_account(account, "second", "another password")  # 계정은 하나다
    with pytest.raises(secretstore.AccountError):
        secretstore.create_account(account.with_name("x.json"), "user", "short")


def test_csrf_is_required_for_state_changes(site):
    """토큰 없는 상태 변경 요청은 거부된다."""
    site.login()
    body = {"channel_id": CHANNEL, "name": "채널", "output_dir": "a"}
    changing = [(m, p) for m, p in all_routes(site.app) if m != "GET" and p != "/api/login"]
    assert len(changing) >= 10
    for method, path in changing:
        for headers in ({}, {"X-CSRF-Token": ""}, {"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": site.csrf[:-1]}):
            r = site.client.request(method, path, json=body, headers=headers)
            assert r.status_code == 403, (method, path, headers, r.status_code)
    assert site.h.store.list_channels() == []  # 아무것도 바뀌지 않았다
    # 토큰이 맞아도 다른 출처에서 온 요청은 거부한다
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://testserver.evil"}):
        assert site.call("POST", "/api/channels", json=body, headers=dict(headers)).status_code == 403
    assert site.client.post("/api/login", json={"username": USER, "password": PASSWORD},
                            headers={"Origin": "https://evil.example"}).status_code == 403
    # 토큰이 있으면 된다. 같은 출처 표시가 있어도 된다
    assert site.call("POST", "/api/channels", json=body, headers={"Origin": "http://testserver"}).status_code == 201
    assert site.client.get("/api/channels").status_code == 200  # 조회에는 토큰이 필요 없다


def test_missing_fields_do_not_erase_values(site):
    """필드를 뺀 저장이 기존 값을 지우거나 끄지 않는다. 모르는 키는 거부한다."""
    site.login()
    assert site.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "채널", "output_dir": "a",
                                                    "quality": "720p", "extension": ".ts"}).status_code == 201
    secretstore.telegram_save(site.h.cfg.telegram_path, TG, "42")
    before = site.client.get("/api/settings").json()["settings"]
    assert before["timemachine_enabled"] is True and before["delete_original"] is True

    r = site.call("PATCH", "/api/settings", json={"rewind_seconds": 600})
    assert r.status_code == 200
    after = r.json()["settings"]
    assert after == {**before, "rewind_seconds": 600}  # 나머지는 그대로
    assert site.call("PATCH", "/api/settings", json={}).json()["settings"] == after
    for bad in ({"nope": 1}, {"rewind_seconds": 99999}, {"check_interval": None}, {"rewind_seconds": [1]},
                {"filename_pattern": "{x}"}, {"rewind_seconds": 30, "bogus": True}):
        assert site.call("PATCH", "/api/settings", json=bad).status_code == 400, bad
    assert site.client.get("/api/settings").json()["settings"] == after  # 일부만 틀려도 아무것도 쓰지 않는다

    # 채널: 이름만 보내면 자동 녹화·화질·확장자가 그대로다
    assert site.call("PATCH", f"/api/channels/{CHANNEL}", json={"name": "새 이름"}).status_code == 200
    ch = site.h.store.get_channel(CHANNEL)
    assert (ch.name, ch.quality, ch.extension, ch.auto_record) == ("새 이름", "720p", ".ts", True)
    assert site.call("PATCH", f"/api/channels/{CHANNEL}", json={"auto_record": False}).status_code == 200
    assert site.h.store.get_channel(CHANNEL).auto_record is False and site.h.store.get_channel(CHANNEL).name == "새 이름"
    for bad in ({"channel_id": "f" * 32}, {"auto_record": "yes"}, {"unknown": 1}, {"output_dir": "../밖"}):
        assert site.call("PATCH", f"/api/channels/{CHANNEL}", json=bad).status_code in (400, 422), bad
    assert site.h.store.get_channel(CHANNEL).auto_record is False

    # 쿠키와 텔레그램 값: 하나만 보내면 다른 하나가 남는다
    saved = site.call("PUT", "/api/cookies", json={"NID_AUT": AUT, "NID_SES": SES}).json()
    assert saved == {"NID_AUT": True, "NID_SES": True, "last_check": None}  # 마지막 확인 시각은 확인 뒤에만
    assert site.call("PUT", "/api/cookies", json={"NID_AUT": "new-aut"}).status_code == 200
    assert credentials.load(site.h.cfg, "chzzk") == {"NID_AUT": "new-aut", "NID_SES": SES}
    assert site.call("PUT", "/api/cookies", json={}).status_code == 200
    assert credentials.load(site.h.cfg, "chzzk")["NID_SES"] == SES
    assert site.call("PUT", "/api/cookies", json={"NID_XYZ": "1"}).status_code == 422
    assert site.call("PUT", "/api/notify/secret", json={"chat_id": "77"}).json() == {"telegram": {"bot_token": True, "chat_id": True}}
    assert secretstore.telegram_load(site.h.cfg.telegram_path) == {"bot_token": TG, "chat_id": "77"}

    # 변환을 끈 채 .ts인 조합은 웹에서도 거부한다
    assert site.call("PATCH", "/api/settings", json={"remux_enabled": False}).status_code == 400
    # 알림이 켜진 동안에는 값을 비울 수 없다
    assert site.call("PATCH", "/api/settings", json={"notify_enabled": True}).status_code == 200
    assert site.call("PUT", "/api/notify/secret", json={"bot_token": ""}).status_code == 400
    assert secretstore.telegram_load(site.h.cfg.telegram_path)["bot_token"] == TG


def test_secrets_never_come_back(site, server):
    """저장한 쿠키·토큰·비밀번호가 어떤 응답, 로그, 저장소에도 없다."""
    site.login()
    site.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "채널", "output_dir": "a"})
    assert site.call("PUT", "/api/cookies", json={"NID_AUT": AUT, "NID_SES": SES}).status_code == 200
    assert site.call("PUT", "/api/notify/secret", json={"bot_token": TG, "chat_id": "-100777"}).status_code == 200
    assert site.call("PATCH", "/api/settings", json={"notify_enabled": True, "rewind_seconds": 0}).status_code == 200
    b = server.go_live()
    site.wait(site.h.recording, what="녹화")
    bodies = []
    for method, path in all_routes(site.app):
        if method == "GET":
            bodies.append(site.client.get(path).text)
    bodies.append(site.call("POST", "/api/cookies/check").text)
    bodies.append(site.call("POST", "/api/notify/test").text)
    b.end()
    site.wait(lambda: site.h.finalized(1), what="마무리")
    bodies.append(site.client.get("/api/sessions").text)
    bodies.append(site.client.get("/api/channels").text)
    blob = "\n".join(bodies) + site.h.log_text()
    for secret in (AUT, SES, TG, "-100777", PASSWORD, "hdnts", "hmac="):
        assert secret not in blob, secret
    state = site.client.get("/api/cookies").json()
    assert {k: state[k] for k in ("NID_AUT", "NID_SES")} == {"NID_AUT": True, "NID_SES": True}
    assert state["last_check"] is None or set(state["last_check"]) == {"result", "label", "at"}  # 값은 넣지 않는다
    assert site.client.get("/api/settings").json()["telegram"] == {"bot_token": True, "chat_id": True}
    assert len(server.tg_messages) >= 2  # 실제로 쓰였다
    for path in site.h.data.rglob("*"):
        if path.is_file() and path.parent.name != "secrets":
            raw = path.read_bytes()
            for secret in (AUT, SES, TG, PASSWORD):
                assert secret.encode() not in raw, (path, secret)
    for path in (site.h.data / "secrets").iterdir():
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    cleared = site.call("DELETE", "/api/cookies").json()
    assert (cleared["NID_AUT"], cleared["NID_SES"]) == (False, False)


def test_status_is_public_and_minimal(site, server):
    """인증 없이 200. 채널별 recording·duration만 있고 파일 이름·제목은 없다."""
    assert site.client.get("/status").json() == {"status": "ok", "channels": []}  # 순번은 채널이 생기면 1부터
    site.h.store.add_channel(CHANNEL, "비밀 채널 이름", "a")
    site.h.store.set_settings({"rewind_seconds": 0})
    server.go_live(title="비밀 방송 제목")
    site.wait(site.h.recording, what="녹화")
    time.sleep(1.2)
    anon = TestClient(site.app, base_url="http://testserver")
    r = anon.get("/status")
    data = r.json()
    assert r.status_code == 200 and set(data) == {"status", "channels"}
    assert [set(c) for c in data["channels"]] == [{"index", "recording", "duration"}] and data["channels"][0]["index"] == 1
    assert data["channels"][0]["recording"] is True and data["channels"][0]["duration"] >= 1
    assert "비밀" not in r.text and CHANNEL not in r.text and ".mp4" not in r.text


def test_security_headers_and_no_api_docs(site):
    site.login()
    for path in ("/login", "/", "/status", "/api/channels", "/static/app.js", "/nope"):
        r = site.client.get(path)
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert site.client.get("/api/channels").headers["cache-control"] == "no-store"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert site.client.get(path).status_code == 404
    # 화면 코드는 서버에서 온 문자열을 HTML로 해석하지 않는다. 인라인 스크립트도 없다
    ui = Path(web.UI_DIR)
    js = all_js() + (ui / "login.js").read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, banned
    for page in ("index.html", "login.html"):
        html = (ui / page).read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", html) and not re.search(r"\son[a-z]+=", html) and "style=" not in html
        assert not re.search(r'(?:src|href)="(?:https?:)?//', html)  # 외부 자원을 부르지 않는다


def test_xss_payloads_are_data_only(site, server):
    """방송 제목과 채널 이름은 사용자 입력이다. JSON 문자열로만 나간다."""
    site.login()
    evil = '<img src=x onerror=alert(1)><script>alert(2)</script>'
    assert site.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": evil, "output_dir": "a"}).status_code == 201
    site.h.store.set_settings({"rewind_seconds": 0})
    server.go_live(title=evil)
    site.wait(site.h.recording, what="녹화")
    r = site.client.get("/api/channels")
    assert r.headers["content-type"].startswith("application/json")
    ch = r.json()["channels"][0]
    assert ch["name"] == evil and ch["live_title"] == evil  # 값은 그대로, 해석은 화면이 textContent로만
    assert "<script>" not in site.client.get("/").text.replace('<script src="/static/app.js"></script>', "")


def test_channel_crud_and_record_through_web(site, server, tools):
    """웹 API로 채널을 추가하고 녹화를 보고 중지하고 지운다."""
    site.login()
    site.call("PATCH", "/api/settings", json={"rewind_seconds": 0})
    r = site.call("POST", "/api/channels", json={"channel_id": CHANNEL.upper(), "name": "웹채널", "output_dir": "web"})
    assert r.status_code == 201
    assert site.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "또", "output_dir": "web"}).status_code == 400
    for bad in ({"channel_id": "https://chzzk.naver.com/live/x", "name": "x", "output_dir": "a"},
                {"channel_id": "e" * 32, "name": "x", "output_dir": "/etc"},
                # 저장 폴더는 빠져도 된다(기본 폴더). 빠진 필수 필드 검사는 표시 이름으로 본다
                {"channel_id": "e" * 32, "output_dir": "a"}):
        assert site.call("POST", "/api/channels", json=bad).status_code in (400, 422)
    view = site.client.get("/api/channels").json()["channels"][0]
    assert view["state"] in ("watching", "stopped") and view["output_dir"] == "web" and view["file"] is None

    server.go_live(title="웹 방송")
    site.wait(lambda: site.client.get("/api/channels").json()["channels"][0]["state"] == "recording", what="화면에 녹화 표시")
    time.sleep(1.1)
    view = site.client.get("/api/channels").json()["channels"][0]
    assert view["state_label"] == "녹화" and view["live_title"] == "웹 방송" and view["elapsed"] >= 1
    assert view["file"].endswith("웹채널 웹 방송 1080p60.mp4") and "/" not in view["file"]
    assert site.call("POST", f"/api/channels/{CHANNEL}/start").json()["result"] == "이미 녹화 중"
    assert site.call("POST", f"/api/channels/{CHANNEL}/stop").json()["result"] == "중지 요청함"
    site.wait(lambda: site.h.finalized(1), what="마무리")
    sessions = site.client.get("/api/sessions").json()["sessions"]
    assert sessions[0]["end_reason"] == "user_stop" and sessions[0]["finalize_status"] == "done"
    assert sessions[0]["file"].endswith(".mp4") and "pid" not in sessions[0] and "part_path" not in sessions[0]
    assert site.client.get("/api/channels").json()["channels"][0]["auto_record"] is True

    assert site.call("DELETE", f"/api/channels/{CHANNEL}").status_code == 200
    assert site.client.get("/api/channels").json()["channels"] == []
    assert site.call("DELETE", f"/api/channels/{CHANNEL}").status_code == 404
    assert site.call("POST", "/api/channels/not-an-id/start").status_code == 404
    assert len(list((site.h.root / "web").glob("*.mp4"))) == 1  # 파일은 남는다


def test_cookie_check(site, server):
    """유효, 만료, 확인 실패, 쿠키 없음을 구분한다. 만료는 알림 대상이다."""
    site.login()
    check = lambda: site.call("POST", "/api/cookies/check").json()["result"]  # noqa: E731
    assert check() == "missing" and server.count("/api/user-status") == 0
    site.call("PUT", "/api/cookies", json={"NID_AUT": AUT, "NID_SES": SES})
    assert check() == "valid"
    assert server.calls[-1] == ("/api/user-status", f"NID_AUT={AUT}; NID_SES={SES}")
    server.user_status = (200, {"code": 200, "content": {"loggedIn": True, "userIdHash": None}})
    assert check() == "failed"
    server.user_status = (500, {})
    assert check() == "failed"
    site.h.enable_telegram()
    for status in ((401, {}), (403, {}), (200, {"code": 200, "content": {"loggedIn": False}})):
        server.user_status = status
        assert check() == "expired"
    site.wait(lambda: any("쿠키 만료" in m["text"] for m in server.tg_messages), what="만료 알림")
    assert sum("쿠키 만료" in m["text"] for m in server.tg_messages) == 1  # 중복 억제


def test_startup_makes_no_external_calls_and_binds_locally(site, server, monkeypatch):
    time.sleep(0.5)
    assert server.calls == [] and server.tg_paths == []  # 채널이 없으면 어디에도 요청하지 않는다
    args = cli.build_parser().parse_args(["web"])
    assert args.host == "127.0.0.1"
    monkeypatch.setenv("LIVE_RECORDER_HOST", "0.0.0.0")
    assert cli.build_parser().parse_args(["web"]).host == "0.0.0.0"  # 컨테이너에서는 설정으로 명시해 연다


def test_account_is_created_outside_the_web(tmp_path, server, tools, monkeypatch):
    """환경변수로는 첫 기동 때만 만들어진다. 계정이 없을 때의 보기 전용 모드는 test_view_only.py가 본다."""
    h = Harness(tmp_path, server, tools)
    monkeypatch.setenv("LIVE_RECORDER_ADMIN_USER", "envadmin")
    monkeypatch.setenv("LIVE_RECORDER_ADMIN_PASSWORD", "env password 1234")
    web.create_app(h.cfg, fast_timings())
    assert secretstore.account_name(h.cfg.account_path) == "envadmin"
    monkeypatch.setenv("LIVE_RECORDER_ADMIN_USER", "other")
    web.create_app(h.cfg, fast_timings())
    assert secretstore.account_name(h.cfg.account_path) == "envadmin"  # 이미 있으면 바꾸지 않는다
    assert secretstore.verify_account(h.cfg.account_path, "envadmin", "env password 1234")
    assert not secretstore.verify_account(h.cfg.account_path, "envadmin", "wrong")
    assert not secretstore.verify_account(h.cfg.account_path, "other", "env password 1234")


def test_forgot_password_hint_shows_after_three_failures():
    """로그인 화면의 비밀번호 분실 안내는 처음에 숨겨 두고, 이 탭에서 3번 틀린 뒤에 보인다. 성공하면 횟수를 지운다.
    (브라우저 동작은 README의 확인 기록. 여기서는 화면 파일의 구성을 본다)"""
    ui = Path(web.UI_DIR)
    html = (ui / "login.html").read_text()
    assert re.search(r'<p id="forgot-hint" class="hint" hidden>', html)
    js = (ui / "login.js").read_text()
    assert "const FORGOT_AFTER = 3;" in js
    assert "if (resp.status === 401) writeFailures(readFailures() + 1);" in js  # 401만 센다
    assert "if (resp.ok) { writeFailures(0);" in js  # 성공하면 지운다
    assert "innerHTML" not in js


def test_channel_field_accepts_address_but_server_takes_only_ids(site):
    """채널 추가 칸은 치지직 주소나 32자리 ID를 받아 화면에서 ID를 뽑는다.
    서버는 지금처럼 ID만 받는다. 브라우저 동작은 README의 확인 기록에 있다."""
    js = all_js()
    assert "function parseChannelId(text, rule = channelRule())" in js
    # 받는 호스트와 경로 앞부분은 화면이 아니라 플랫폼 모듈이 준다
    assert "rule.hosts.includes(url.hostname.toLowerCase())" in js
    assert 'parts[0] === rule.path_prefix ? parts[1] : parts[0]' in js
    site.login()
    chzzk = next(p for p in site.client.get("/api/platforms").json()["platforms"] if p["id"] == "chzzk")
    assert chzzk["channel_rule"]["hosts"] == ["chzzk.naver.com", "www.chzzk.naver.com", "m.chzzk.naver.com"]
    assert chzzk["channel_rule"]["path_prefix"] == "live"
    html = index_html()
    field = next(line for line in html.splitlines() if 'name="channel_id"' in line)
    assert 'maxlength="32"' not in field  # 주소를 붙여 넣을 수 있다
    for value in ("https://chzzk.naver.com/live/" + CHANNEL, "chzzk.naver.com/" + CHANNEL):
        r = site.call("POST", "/api/channels", json={"channel_id": value, "name": "x", "output_dir": "a"})
        assert r.status_code == 400, value  # 서버는 주소를 받지 않는다
    assert site.call("POST", "/api/channels", json={"channel_id": CHANNEL.upper(), "name": "x", "output_dir": "a"}).status_code == 201


def test_channel_card_colors_by_health():
    """채널 카드 색: 녹화 중 녹색, 녹화하지 않음 노란색, 오류(오류 표시·대기) 붉은색."""
    js = all_js()
    assert 'if (ch.error_flag || ch.state === "backoff") return "error";' in js
    assert 'if (ch.state === "recording") return "recording";' in js and 'return "idle";' in js
    css = (Path(web.UI_DIR) / "style.css").read_text()
    for health, var in (("recording", "--live"), ("idle", "--idle-y"), ("error", "--err")):
        # 썸네일 카드에서도 같은 색을 테두리와 옅은 바탕에 쓴다
        assert f'.card-grid .card[data-health="{health}"] {{ border-color: var({var}); background: var({var}-bg); }}' in css


def test_program_name_is_defined_once(tmp_path, server, tools):
    """표시 이름은 PROGRAM_NAME 한 곳. 로그인·첫 설정 화면, 알림 머리말, 로그 묶음·진단 요약이 그 값을 쓴다.
"""
    import re
    from pathlib import Path

    from live_recorder import NAME_SLOT, PROGRAM_NAME, notify

    assert PROGRAM_NAME == "VOD Recorder"
    assert notify.build_text("record_start", "채널").startswith(f"[{PROGRAM_NAME}] ")
    with Web(tmp_path, server, tools) as site:
        setup = site.client.get("/setup").text  # 계정이 있으면 첫 화면으로 보낸다
        site.call("POST", "/api/logout")
        login = site.client.get("/login", follow_redirects=True).text
        assert f"<title>{PROGRAM_NAME} 로그인</title>" in login and NAME_SLOT not in login and setup is not None
