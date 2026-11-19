"""웹 보완: 비밀번호 변경, 세션 유지 시간, 로그인 제한 끄기, 신뢰할 프록시, /status 순번, 쿠키 확인 시점."""

import asyncio
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from live_recorder.platforms import credentials
from live_recorder.core import secretstore
from live_recorder import web
from live_recorder.web import auth as web_auth
from live_recorder.core.config import ConfigError, Settings

from conftest import CHANNEL, ROOT, Harness, fast_timings
from test_web import PASSWORD, USER, Web, all_routes

NEW_PASSWORD = "brand new password 42"


@pytest.fixture
def site(tmp_path, server, tools):
    with Web(tmp_path, server, tools) as w:
        yield w


def other_client(site, **headers) -> TestClient:
    return TestClient(site.app, base_url="http://testserver", follow_redirects=False, headers=headers)


def login(client, password=PASSWORD, **headers):
    return client.post("/api/login", json={"username": USER, "password": password}, headers=headers)


def test_web_password_change_drops_all_sessions(site):
    """현재 비밀번호를 확인하고, 바꾸면 열린 세션이 모두 끊긴다."""
    site.login()
    second = other_client(site)
    assert login(second).status_code == 200
    csrf = site.csrf
    # 현재 비밀번호가 틀리면 바뀌지 않고, 실패는 로그인 제한에 들어간다
    r = site.call("POST", "/api/account/password", json={"current": "wrong", "new": NEW_PASSWORD})
    assert r.status_code == 401 and site.app.state.limiter._fails
    assert secretstore.verify_account(site.h.cfg.account_path, USER, PASSWORD)
    assert site.call("POST", "/api/account/password", json={"current": PASSWORD, "new": "short"}).status_code == 400
    assert site.call("POST", "/api/account/password", json={"current": PASSWORD, "new": NEW_PASSWORD, "x": 1}).status_code == 422
    r = site.call("POST", "/api/account/password", json={"current": PASSWORD, "new": NEW_PASSWORD})
    assert r.status_code == 200
    assert site.client.get("/api/session").status_code == 401  # 이 세션도
    assert second.get("/api/session").status_code == 401  # 다른 세션도
    assert login(second).status_code == 401 and login(second, NEW_PASSWORD).status_code == 200
    assert secretstore.account_name(site.h.cfg.account_path) == USER
    log = site.h.log_text()
    assert NEW_PASSWORD not in log and PASSWORD not in log and "password_changed" in log
    assert csrf  # 비밀번호 변경도 CSRF 토큰을 요구한다(test_web의 모든 상태 변경 경로 시험에 포함)


def test_password_checks_count_toward_limit(site):
    """비밀번호 변경의 현재 비밀번호 실패가 잠금에 들어간다."""
    site.login()
    for _ in range(5):
        assert site.call("POST", "/api/account/password", json={"current": "nope", "new": NEW_PASSWORD}).status_code == 401
    assert site.call("POST", "/api/account/password", json={"current": PASSWORD, "new": NEW_PASSWORD}).status_code == 429
    assert login(other_client(site)).status_code == 429  # 같은 주소의 로그인도 잠겼다


def test_cli_password_change_ends_web_sessions(site):
    """CLI(다른 프로세스)에서 바꿔도 웹 세션이 끊긴다. 비밀번호는 인자로 받지 않는다."""
    site.login()
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(site.h.data), LIVE_RECORDER_RECORD_ROOT=str(site.h.root),
               PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-m", "live_recorder", "account", "passwd"], cwd=ROOT, env=env,
                       input=NEW_PASSWORD + "\n", capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert NEW_PASSWORD not in r.stdout + r.stderr
    assert site.client.get("/api/session").status_code == 401
    assert login(other_client(site), NEW_PASSWORD).status_code == 200
    r = subprocess.run([sys.executable, "-m", "live_recorder", "account", "passwd", NEW_PASSWORD], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0  # 인자로는 받지 않는다


def test_session_idle_timeout(site, monkeypatch):
    """쓰지 않은 시간이 정한 분을 넘으면 끝나고, 쓰면 늘어난다. 0이면 제한이 없다."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(web_auth.time, "monotonic", lambda: clock["now"])
    site.h.store.set_settings({"session_minutes": 10})
    site.login()
    for _ in range(3):
        clock["now"] += 9 * 60
        assert site.client.get("/api/session").status_code == 200  # 쓸 때마다 늘어난다
    clock["now"] += 10 * 60 + 2
    assert site.client.get("/api/session").status_code == 401
    site.h.store.set_settings({"session_minutes": 0})
    clock["now"] += 2
    site.login()
    clock["now"] += 365 * 24 * 3600
    assert site.client.get("/api/session").status_code == 200
    for bad in (-1, "abc", 1.5, None):
        with pytest.raises(ConfigError):
            Settings.validate("session_minutes", bad)
    assert Settings().session_minutes == 60


def test_login_limit_can_be_disabled(site):
    """끄면 잠그지 않고 세지도 않는다. 다시 켜면 그때까지의 기록을 지우고 새로 센다."""
    site.login()
    c = other_client(site)
    for _ in range(4):
        assert login(c, "wrong").status_code == 401
    assert site.call("PATCH", "/api/settings", json={"login_limit_enabled": False}).status_code == 200
    for _ in range(8):
        assert login(c, "wrong").status_code == 401
    assert login(c).status_code == 200
    assert not site.app.state.limiter._fails and not site.app.state.limiter._all  # 꺼 둔 동안 세지 않았다
    # 다시 켜면(웹이든 CLI든) 지난 기록 없이 새로 센다
    site.h.store.set_settings({"login_limit_enabled": True})
    time.sleep(1.1)  # 설정 캐시
    for _ in range(4):
        assert login(c, "wrong").status_code == 401
    assert login(c).status_code == 200  # 4번은 잠기지 않는다(꺼 두기 전의 4번은 지워졌다)
    for _ in range(5):
        login(c, "wrong")
    assert login(c).status_code == 429


def test_origin_check_behind_trusted_proxy(site):
    """프록시가 Host를 바꿔도, 신뢰할 프록시에서 온 요청이면 X-Forwarded-Host와 Origin을 견준다.
    신뢰하지 않는 상대가 보낸 X-Forwarded-Host는 읽지 않는다."""
    import asyncio

    import httpx

    site.login()
    csrf = site.csrf
    cookie = site.client.cookies.get(web.SESSION_COOKIE)

    async def call(peer: str, headers: dict) -> int:
        transport = httpx.ASGITransport(app=site.app, client=(peer, 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://recorder:8080") as client:
            client.cookies.set(web.SESSION_COOKIE, cookie)
            r = await client.post("/api/channels/" + CHANNEL + "/start",
                                  headers={"X-CSRF-Token": csrf, "Origin": "https://rec.example.lan", **headers})
            return r.status_code

    async def scenario():
        # 프록시를 신뢰하지 않으면: Host(recorder:8080)와 Origin이 달라 거부
        assert await call("127.0.0.1", {"X-Forwarded-Host": "rec.example.lan"}) == 403
        site.h.store.set_settings({"trusted_proxies": ["127.0.0.1"]})
        await asyncio.sleep(1.1)
        assert await call("127.0.0.1", {"X-Forwarded-Host": "rec.example.lan"}) == 200
        assert await call("127.0.0.1", {"X-Forwarded-Host": "evil.example, rec.example.lan"}) == 200  # 마지막 값
        assert await call("127.0.0.1", {"X-Forwarded-Host": "evil.example"}) == 403
        assert await call("127.0.0.1", {}) == 403  # 헤더가 없으면 Host와 견준다
        # 신뢰하지 않는 상대가 지어낸 헤더는 읽지 않는다
        assert await call("192.0.2.9", {"X-Forwarded-Host": "rec.example.lan"}) == 403

    asyncio.run(scenario())


def test_forwarded_header_is_ignored_without_trusted_proxy(site):
    """신뢰할 프록시가 비어 있으면 X-Forwarded-For를 읽지 않는다. 헤더를 지어내 제한을 피하지 못한다."""
    for i in range(5):
        assert login(other_client(site), "wrong", **{"X-Forwarded-For": f"10.0.0.{i}"}).status_code == 401
    assert login(other_client(site), **{"X-Forwarded-For": "10.9.9.9"}).status_code == 429


def test_trusted_proxy_uses_last_forwarded_value(site):
    """신뢰할 프록시에서 온 요청만 마지막 X-Forwarded-For 값을 접속자로 본다."""
    site.h.store.set_settings({"trusted_proxies": ["127.0.0.1"]})
    # TestClient의 상대 주소는 "testclient"라서 IP가 아니다. 실제 주소로 시험하려고 ASGI 범위를 바꾼다
    app = site.app

    async def call(peer: str, xff: str | None, password: str) -> int:
        import httpx
        transport = httpx.ASGITransport(app=app, client=(peer, 50000))
        headers = {"X-Forwarded-For": xff} if xff else {}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.post("/api/login", json={"username": USER, "password": password}, headers=headers)
            return r.status_code

    async def scenario():
        time.sleep(1.1)
        # 프록시(127.0.0.1)를 거친 접속자 203.0.113.5가 5번 틀리면 그 접속자만 잠긴다
        for _ in range(5):
            assert await call("127.0.0.1", "198.51.100.1, 203.0.113.5", "wrong") == 401
        assert await call("127.0.0.1", "203.0.113.5", PASSWORD) == 429
        assert await call("127.0.0.1", "203.0.113.6", PASSWORD) == 200  # 다른 접속자는 된다
        # 앞쪽 값은 클라이언트가 지어낼 수 있어서 쓰지 않는다
        assert await call("127.0.0.1", "203.0.113.6, 203.0.113.5", PASSWORD) == 429
        # 신뢰하지 않는 상대가 보낸 헤더는 무시한다. 이 상대 자신의 주소로 센다
        for _ in range(5):
            assert await call("192.0.2.50", "203.0.113.77", "wrong") == 401
        assert await call("192.0.2.50", "203.0.113.78", PASSWORD) == 429
        assert await call("192.0.2.51", None, PASSWORD) == 200
        # 깨진 헤더면 프록시 주소로 센다
        assert await call("127.0.0.1", "not-an-ip", PASSWORD) == 200

    asyncio.run(scenario())
    for bad in ("not-an-ip", ["10.0.0.0/33"], 5):
        with pytest.raises(ConfigError):
            Settings.validate("trusted_proxies", bad)
    assert Settings.validate("trusted_proxies", "10.0.0.1\n172.16.0.0/12") == ("10.0.0.1/32", "172.16.0.0/12")


def test_access_policy_is_in_one_place():
    """로그인을 요구하지 않는 경로는 정책 표 하나에만 있다."""
    assert web.access_level("GET", "/status") == web.PUBLIC
    assert web.access_level("POST", "/status") == web.LOGIN
    assert web.access_level("GET", "/static/app.js") == web.PUBLIC
    assert web.access_level("GET", "/api/channels") == web.LOGIN
    assert web.access_level("GET", "/anything-new") == web.LOGIN  # 새 경로는 기본으로 로그인을 요구한다
    assert {r[1] for r in web.ACCESS_RULES if r[2] == web.PUBLIC} == {"/login", "/status", "/api/login", "/api/mode", "/static/*"}
    assert web.access_level("GET", "/api/view/channels") == web.VIEW  # 계정이 있으면 로그인을 요구한다(test_view_only)


def test_cookie_check_daily_and_after_adult_failure(tmp_path, server, tools):
    """하루 한 번(쿠키가 있을 때만), 그리고 성인 방송이 재생 정보 없음으로 실패했을 때 확인한다.
    만료만 알리고 '확인 실패'는 알리지 않는다."""
    h = Harness(tmp_path, server, tools, timings=fast_timings(cookie_check_interval=0.5, cookie_check_min_gap=0.0))
    h.enable_telegram(notify_dedupe_seconds=0)

    async def scenario():
        async with h:
            from live_recorder.platforms.chzzk import api as chzzk
            h.engine.client_for("chzzk").retry = chzzk.RetryPolicy(first_delay=0.01)  # 500 재시도가 다음 단계와 겹치지 않게
            await asyncio.sleep(1.3)
            assert server.count("/api/user-status") == 0  # 쿠키가 없으면 하루 확인도 하지 않는다
            credentials.save(h.cfg, "chzzk", {"NID_AUT": "a", "NID_SES": "b"})
            await h.until(lambda: server.count("/api/user-status") >= 1, what="하루 한 번 확인")
            assert h.engine.last_cookie_check["reason"] == "daily"
            server.user_status = (500, {})
            n = server.count("/api/user-status")
            await h.until(lambda: server.count("/api/user-status") > n, what="다음 확인")
            await asyncio.sleep(0.3)
            assert not any("쿠키" in m["text"] for m in server.tg_messages)  # 확인 실패는 알리지 않는다
            # 성인 방송: 재생 정보 없이 시작 실패하면 곧바로 확인한다
            h.engine.t.cookie_check_interval = 3600
            await asyncio.sleep(0.8)  # 이미 시작한 하루 확인이 끝나기를 기다린다
            server.user_status = (401, {})
            h.add_channel()
            server.go_live()
            locked = {"code": 200, "content": {"status": "OPEN", "liveId": 1001, "liveTitle": "성인", "adult": True,
                                               "openDate": "2026-11-11 12:00:00", "livePlaybackJson": None}}
            server.detail_queue[:] = [(200, locked)] * 40
            server.tm_fail = True
            await h.until(lambda: any("쿠키 만료" in m["text"] for m in server.tg_messages), what="만료 알림")
            assert h.engine.last_cookie_check["reason"] == "adult_start_failed"

    asyncio.run(scenario())


def test_reconnect_is_not_notified(harness, server):
    """방송 중 끊겼다가 다시 연결에 성공한 것은 알리지 않고 세션 기록에만 남긴다."""
    harness.add_channel()
    harness.enable_telegram(rewind_seconds=0, notify_dedupe_seconds=0, notify_record_start=False,
                            notify_record_end=False, notify_finalize_done=False)
    b = server.go_live(initial=30)

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화")
            await asyncio.sleep(0.6)
            server.down = True
            await harness.until(lambda: any(s["ended_at"] for s in harness.sessions()), what="끊김")
            server.down = False
            await harness.until(lambda: len([s for s in harness.sessions() if s["bytes"] or harness._part_size(s)]) >= 2,
                                what="다시 연결")
            await asyncio.sleep(0.5)
            b.end()
            await harness.until(lambda: harness.finalized(2), what="마무리")

    asyncio.run(scenario())
    assert server.tg_messages == [], [m["text"] for m in server.tg_messages]
    assert "다시 연결" in harness.sessions()[0]["note"]


def test_trusted_proxies_from_environment(tmp_path, server, tools, monkeypatch):
    """컨테이너 환경변수 LIVE_RECORDER_TRUSTED_PROXIES가 기동할 때 설정값을 정한다. 틀린 값이면 뜨지 않는다."""
    monkeypatch.setenv("LIVE_RECORDER_TRUSTED_PROXIES", "192.0.2.10, 172.16.0.0/12")
    with Web(tmp_path, server, tools) as w:
        assert w.h.store.get_settings().trusted_proxies == ("192.0.2.10/32", "172.16.0.0/12")
    monkeypatch.setenv("LIVE_RECORDER_TRUSTED_PROXIES", "not-an-ip")
    w2 = Web(tmp_path / "second", server, tools)
    with pytest.raises(ConfigError):
        with w2:
            pass


def test_healthcheck_module(monkeypatch):
    """이미지 HEALTHCHECK가 부르는 모듈. /status가 200이면 0, 아니면 1."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from live_recorder import healthcheck

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200 if self.path == "/status" else 404)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("LIVE_RECORDER_PORT", str(httpd.server_address[1]))
        assert healthcheck.main() == 0
    finally:
        httpd.shutdown()
    monkeypatch.setenv("LIVE_RECORDER_PORT", "1")
    assert healthcheck.main() == 1
