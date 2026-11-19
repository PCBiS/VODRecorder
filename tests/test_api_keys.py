"""접근 키와 API 문서. 키를 하나도 발급하지 않으면 예전과 같다(기존 시험이 지킨다)."""

import json
import os
import re
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fake_extension
from conftest import CHANNEL
from live_recorder import logmask
from live_recorder.core import apikeys, extensions, secretstore
from live_recorder.platforms import credentials
from live_recorder.web import access as acc
from live_recorder.web import keyauth
from test_web import AUT, SES, TG, Web

ROOT = Path(__file__).resolve().parents[1]
ALL_EXTS = ("downloads", "replay", "clips", "youtube_video", "cime_media")
OWN_403 = (keyauth.NOT_FOR_KEYS, keyauth.NEED_OPERATE)


@pytest.fixture
def site(tmp_path, server, tools, monkeypatch):
    monkeypatch.delenv(apikeys.ENV_SWITCH, raising=False)
    with Web(tmp_path, server, tools, extensions=ALL_EXTS) as w:
        w.login()
        w.bare = TestClient(w.app, base_url="http://testserver", follow_redirects=False)  # 쿠키 없는 프로그램
        yield w


def issue(w, scope="operate", name=None, expiry="none"):
    r = w.call("POST", "/api/keys", json={"name": name or f"{scope}-key", "scope": scope, "expiry": expiry})
    assert r.status_code == 201, r.text
    return r.json()


def kcall(w, key, method, path, **kwargs):
    """키로 부른다. 바꾸는 요청은 서버를 띄운 클라이언트로 보낸다(엔진의 이벤트 루프 안에서 돌아야 한다).
    그 클라이언트는 세션 쿠키도 갖고 있지만 키 머리글이 있으면 관문이 쿠키를 보지 않는다(CSRF 머리글도 붙이지 않는다)."""
    headers = dict(kwargs.pop("headers", {}), Authorization=f"Bearer {key}")
    client = w.bare if method in ("GET", "HEAD") else w.client
    return client.request(method, path, headers=headers, **kwargs)


def all_routes(app):
    return keyauth.flat_routes(app)


def concrete(path):
    return re.sub(r"\{[a-z_]+\}", "zz0", path)


# ---- 발급: 값은 한 번만, 저장은 해시 ----
def test_issue_shows_value_once_and_stores_hash(site):
    got = issue(site, "view", name="대시보드")
    value, row = got["value"], got["key"]
    assert value.startswith("vr_") and len(value) >= 3 + 43  # 256비트
    assert row["prefix"] == value[:8] and row["scope"] == "view" and row["expires_at"] is None
    listed = site.client.get("/api/keys").json()
    assert listed["enabled"] and listed["max"] == 10
    assert value not in json.dumps(listed) and "hash" not in json.dumps(listed)
    path = site.h.cfg.api_keys_path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    raw = path.read_text()
    assert value not in raw and apikeys._digest(value) in raw
    for i in range(9):
        issue(site, "view", name=f"k{i}")
    r = site.call("POST", "/api/keys", json={"name": "열한째", "scope": "view"})
    assert r.status_code == 400 and "10개" in r.json()["error"]
    assert site.call("POST", "/api/keys", json={"name": "x", "scope": "admin"}).status_code == 400
    exp = site.call("DELETE", f"/api/keys/{row['id']}")
    assert exp.status_code == 200
    assert kcall(site, value, "GET", "/api/channels").status_code == 401  # 폐기는 곧바로 먹는다


def test_keys_cannot_be_managed_with_keys_and_need_an_account(site, tmp_path):
    op = issue(site)["value"]
    for method, path in (("GET", "/api/keys"), ("POST", "/api/keys"), ("DELETE", "/api/keys/abc")):
        r = kcall(site, op, method, path, json={"name": "x", "scope": "view"} if method == "POST" else None)
        assert r.status_code == 403 and r.json()["detail"] == keyauth.NOT_FOR_KEYS, (method, path)
    os.remove(site.h.cfg.account_path)  # 보기 전용 모드
    site.app.state.sessions.drop_all()
    r = site.bare.post("/api/keys", json={"name": "x", "scope": "view"})
    assert r.status_code == 403  # 계정이 없으면 만들 수 없다


# ---- 인증: 만료·폐기·틀린 키, 늦추기, CSRF, 쿠키 ----
def test_wrong_expired_and_missing_keys_are_401_alike(site):
    good = issue(site, "view")["value"]
    store = site.app.state.keys
    expired = issue(site, "view", expiry="30d")
    import datetime as dt
    store.clock = lambda: dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=31)
    try:
        bodies = [kcall(site, v, "GET", "/api/channels") for v in (expired["value"], "vr_wrong" + "x" * 40, "")]
    finally:
        store.clock = apikeys._now
    assert all(r.status_code == 401 for r in bodies)
    assert len({r.text for r in bodies}) == 1 and bodies[0].json() == {"detail": keyauth.UNAUTHORIZED, "code": "unauthorized"}
    r = site.bare.get("/api/channels", headers={"Authorization": "Basic abc"})
    assert r.status_code == 401
    ok = kcall(site, good, "GET", "/api/channels")
    assert ok.status_code == 200 and "set-cookie" not in ok.headers


def test_wrong_keys_are_slowed_like_login_failures(site):
    good = issue(site, "view")["value"]
    for _ in range(5):
        assert kcall(site, "vr_" + "w" * 43, "GET", "/api/channels").status_code == 401
    r = kcall(site, "vr_" + "w" * 43, "GET", "/api/channels")
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    assert kcall(site, good, "GET", "/api/channels").status_code == 429  # 같은 주소는 맞는 키도 잠시 막힌다
    assert "api_key_failed" in site.h.log_text()
    # 셈은 로그인 실패와 따로다: 틀린 키가 많아도 같은 주소의 화면 로그인은 된다
    from test_web import PASSWORD, USER
    login = site.bare.post("/api/login", json={"username": USER, "password": PASSWORD})
    assert login.status_code == 200, login.text


def test_login_failures_do_not_block_keys(site):
    """반대 방향: 로그인 실패로 그 주소가 잠겨도 맞는 키는 된다(그리고 키 실패로 로그인 잠금이 풀리지도 않는다)."""
    from test_web import USER

    good = issue(site, "view")["value"]
    for _ in range(5):
        assert site.bare.post("/api/login", json={"username": USER, "password": "wrong password!!"}).status_code == 401
    assert site.bare.post("/api/login", json={"username": USER, "password": "wrong password!!"}).status_code == 429
    assert kcall(site, good, "GET", "/api/channels").status_code == 200
    assert site.app.state.limiter.blocked("testclient")


def test_programs_without_origin_or_referer_can_read_and_change(site):
    """브라우저가 아닌 프로그램(curl·스크립트)은 Origin·Referer·Sec-Fetch-* 머리글을 보내지 않는다. 키 요청은 읽기·바꾸기 모두 된다.
    docs/API.md의 curl 예시 순서(채널 칸의 글 바꾸기 → 채널 더하기 → 상태 → 지금 확인 → 중지)를 그대로 따른다."""
    op = issue(site)["value"]

    def call(method, path, body=None):
        headers = {"Authorization": f"Bearer {op}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        client = site.bare if method == "GET" else site.client
        req = client.build_request(method, path, headers=headers, content=json.dumps(body) if body is not None else None)
        for name in ("origin", "referer", "sec-fetch-site", "cookie", "x-csrf-token"):
            if name in req.headers:
                del req.headers[name]
        assert not any(h in req.headers for h in ("origin", "referer", "cookie")), req.headers
        return client.send(req)

    r = call("POST", "/api/channels/resolve", {"text": f"https://chzzk.naver.com/live/{CHANNEL}"})
    assert r.status_code == 200 and r.json()["channel_id"] == CHANNEL, r.text
    r = call("POST", "/api/channels", {"channel_id": CHANNEL, "name": "채널 이름", "output_dir": "channel1"})
    assert r.status_code == 201, r.text
    r = call("GET", "/api/channels")
    assert r.status_code == 200 and r.json()["channels"][0]["channel_id"] == CHANNEL
    assert call("POST", f"/api/channels/{CHANNEL}/start").status_code == 200
    assert call("POST", f"/api/channels/{CHANNEL}/stop").status_code == 200
    # 쿠키 세션의 출처 검사는 그대로다: 다른 출처의 바꾸는 요청은 키가 있어도 막힌다
    r = site.client.post("/api/channels/zz/stop", headers={"Authorization": f"Bearer {op}", "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_nested_routers_are_seen_by_the_overlap_check():
    """겹침 검사는 묶음 안에 또 넣은 묶음(include_router)의 경로까지 편다."""
    from fastapi import APIRouter

    from live_recorder.core.extensions import ExtensionError, LOGIN, AccessRule
    from live_recorder.web import app as appmod

    inner = APIRouter(prefix="/api/files")

    @inner.get("/list")
    async def clash():
        return {}

    outer = APIRouter()
    outer.include_router(inner)
    assert ("GET", "/api/files/list") in appmod._route_keys(outer)

    class Ext:
        name = "nested"
        access_rules = (AccessRule("GET", "/api/files/*", LOGIN),)

        def routes(self, ctx):
            return outer

    class Host:
        active = [Ext()]

        def ctx(self, ext):
            return None

    with pytest.raises(ExtensionError, match="겹칩니다"):
        appmod._check_extension_routes(Host(), [("GET", "/api/files/list")])


def test_key_requests_skip_csrf_but_cookie_sessions_still_need_it(site):
    op = issue(site)["value"]
    r = kcall(site, op, "POST", "/api/channels", json={"channel_id": CHANNEL, "name": "키로 더함", "output_dir": "k1"})
    assert r.status_code == 201, r.text
    assert "set-cookie" not in r.headers
    r = site.client.post("/api/channels", json={"channel_id": "b" * 32, "name": "세션", "output_dir": "s1"})
    assert r.status_code == 403 and "CSRF" in r.json()["error"]  # 쿠키 세션에는 여전히 CSRF가 필요하다
    # 기존 화면용 오류 꼴은 그대로다
    assert site.client.get("/api/keys").status_code == 200


def test_feature_off_and_no_keys(tmp_path, server, tools, monkeypatch):
    monkeypatch.setenv(apikeys.ENV_SWITCH, "off")
    with Web(tmp_path / "off", server, tools) as w:
        w.login()
        assert w.client.get("/api/keys").json()["enabled"] is False
        assert w.call("POST", "/api/keys", json={"name": "x", "scope": "view"}).status_code == 404
        bare = TestClient(w.app, base_url="http://testserver")
        r = bare.get("/api/channels", headers={"Authorization": "Bearer vr_" + "a" * 43})
        assert r.status_code == 401 and r.json() == {"error": "로그인이 필요합니다"}  # 키 인증을 받지 않는다(예전 그대로)
    monkeypatch.delenv(apikeys.ENV_SWITCH)
    with Web(tmp_path / "on", server, tools) as w:
        bare = TestClient(w.app, base_url="http://testserver")
        r = bare.get("/api/channels", headers={"Authorization": "Bearer vr_" + "a" * 43})
        assert r.status_code == 401 and r.json()["code"] == "unauthorized"  # 켠 채 키가 없으면 401
        assert bare.get("/status").status_code == 200  # 머리글이 없는 요청은 예전 그대로


# ---- 권한 표와 기본 거부: 경로 전체를 돌며 표와 대조 ----
def test_every_core_route_states_its_key_permission(site):
    access = site.app.state.access
    missing = [(m, p) for m, p in all_routes(site.app) if not access.key_explicit(m, p)]
    assert missing == [], missing  # 새 경로를 더하면서 키 권한을 안 적으면 여기서 걸린다


def test_permissions_match_the_table_for_every_route(site):
    access = site.app.state.access
    view, op = issue(site, "view")["value"], issue(site, "operate")["value"]
    rates = site.app.state.key_rates
    rates.limits = {"read": 10_000, "write": 10_000}
    checked = {"view": 0, "operate": 0, "none": 0}
    for method, path in all_routes(site.app):
        if path in ("/api/keys/{key_id}",):
            continue
        perm = access.key(method, path)
        url = concrete(path)
        body = {} if method in ("POST", "PUT", "PATCH") else None
        rv = kcall(site, view, method, url, json=body)
        ro = kcall(site, op, method, url, json=body)
        what = f"{method} {path}: {rv.status_code}/{ro.status_code} {rv.text[:120]}"
        if perm == acc.KEY_NONE:
            assert rv.status_code == ro.status_code == 403 and rv.json()["detail"] == keyauth.NOT_FOR_KEYS, what
        elif perm == acc.KEY_VIEW:
            assert rv.status_code not in (401, 429) and not (rv.status_code == 403 and rv.json().get("detail") in OWN_403), what
            assert ro.status_code not in (401, 429) and not (ro.status_code == 403 and ro.json().get("detail") in OWN_403), what
        else:
            assert rv.status_code == 403 and rv.json()["detail"] == keyauth.NEED_OPERATE, what
            assert ro.status_code not in (401, 429) and not (ro.status_code == 403 and ro.json().get("detail") in OWN_403), what
        checked[perm] += 1
    assert all(checked.values()), checked
    # 표에 없는 경로: 있는 경로인데 키 권한이 없으면 403, 없는 경로는 404(꺼진 확장의 경로도 같다)
    assert kcall(site, op, "GET", "/static/app.js").status_code == 403
    assert kcall(site, op, "GET", "/api/nope").status_code == 404


def test_extension_routes_declare_their_own_key_permission(tmp_path, server, tools, monkeypatch):
    """확장의 경로는 확장이 밝힌다. 밝히지 않은 확장 경로(가짜 확장)는 키로 열리지 않는다."""
    fake_extension.EXTENSION.reset()
    extensions.register(fake_extension.EXTENSION)
    extensions.register(fake_extension.CHILD)
    try:
        with Web(tmp_path, server, tools, extensions=("fakeext",)) as w:
            w.login()
            key = issue(w)["value"]
            bare = TestClient(w.app, base_url="http://testserver")
            r = bare.get("/api/fakeext/ping", headers={"Authorization": f"Bearer {key}"})
            assert r.status_code == 403 and r.json()["detail"] == keyauth.NOT_FOR_KEYS
            assert w.client.get("/api/fakeext/ping").status_code == 200  # 세션으로는 그대로
    finally:
        extensions.unregister("fakeext")
        extensions.unregister("fakechild")
    # 다운로드 확장이 꺼져 있으면 그 경로는 키로도 404다
    with Web(tmp_path / "plain", server, tools) as w:
        w.login()
        key = issue(w)["value"]
        bare = TestClient(w.app, base_url="http://testserver")
        r = bare.get("/api/replay/jobs", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_disabled_platform_is_409_for_keys_too(site):
    op = issue(site)["value"]
    r = kcall(site, op, "POST", "/api/channels", json={"channel_id": "@someone", "platform": "youtube", "name": "", "output_dir": "y"})
    assert r.status_code == 409 and r.json()["code"] == "conflict" and "켜 주십시오" in r.json()["detail"]
    r = kcall(site, op, "POST", "/api/channels", json={"channel_id": CHANNEL})
    assert r.status_code == 422 and r.json()["code"] == "invalid_input" and r.json()["errors"]


# ---- 응답에 비밀값이 없다 ----
def test_no_stored_secret_in_any_key_get_response(site):
    credentials.save(site.h.cfg, "chzzk", {"NID_AUT": AUT, "NID_SES": SES})
    secretstore.telegram_save(site.h.cfg.telegram_path, bot_token=TG, chat_id="12345")
    other = issue(site, "view")["value"]
    key = issue(site, "operate")["value"]
    site.app.state.key_rates.limits = {"read": 10_000, "write": 10_000}
    site.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "채널", "output_dir": "c1"})
    access = site.app.state.access
    secrets_ = (AUT, SES, TG, other, key, site.csrf)
    for method, path in all_routes(site.app):
        if method == "GET" and access.key(method, path) != acc.KEY_NONE:
            r = kcall(site, key, "GET", concrete(path).replace("zz0", CHANNEL))
            assert not any(s in r.text for s in secrets_), path


# ---- 요청 제한 ----
def test_rate_limits_per_key(site):
    key = issue(site)["value"]
    other = issue(site, "view")["value"]
    site.app.state.key_rates.limits = {"read": 3, "write": 2}
    assert [kcall(site, key, "GET", "/api/channels").status_code for _ in range(3)] == [200] * 3
    r = kcall(site, key, "GET", "/api/channels")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1 and r.json()["code"] == "rate_limited"
    assert kcall(site, other, "GET", "/api/channels").status_code == 200  # 키마다 따로 센다
    codes = [kcall(site, key, "POST", f"/api/channels/{CHANNEL}/stop").status_code for _ in range(3)]
    assert codes[:2] != [429, 429] and codes[2] == 429
    assert keyauth.READ_LIMIT == 60 and keyauth.WRITE_LIMIT == 20


# ---- 기록과 가리기 ----
def test_mutations_are_logged_without_values_and_masked(site):
    got = issue(site, name="자동화")
    key, prefix = got["value"], got["key"]["prefix"]
    kcall(site, key, "POST", "/api/channels", json={"channel_id": CHANNEL, "name": "키 채널", "output_dir": "k2"})
    kcall(site, key, "POST", f"/api/channels/{CHANNEL}/stop")
    kcall(site, key, "GET", "/api/channels")
    site.app.state.keys.flush()
    text = site.h.log_text()
    events = [json.loads(line) for line in text.splitlines() if '"api_key_request"' in line]
    assert len(events) == 2, events
    assert {e["route"] for e in events} == {"/api/channels", "/api/channels/{channel_id}/stop"}
    assert all(e["key_name"] == "자동화" and e["key_prefix"] == prefix and e["method"] == "POST" for e in events)
    assert key not in text and CHANNEL not in json.dumps([e["route"] for e in events])
    row = next(k for k in site.client.get("/api/keys").json()["keys"] if k["name"] == "자동화")
    assert row["uses"] == 3 and row["last_used_at"]
    masked = logmask.Masker(False).text(f"Authorization: Bearer {key}\nkey={key} {prefix}")
    assert key not in masked and key[8:20] not in masked and prefix in masked


# ---- 문서 ----
def test_openapi_lists_exactly_the_key_routes(site):
    assert site.bare.get("/api/openapi.json").status_code == 401  # 로그인이나 키가 있어야 받는다
    doc = site.client.get("/api/openapi.json").json()
    access = site.app.state.access
    want = {(m, p) for m, p in all_routes(site.app) if access.key(m, p) != acc.KEY_NONE}
    got = {(m.upper(), p) for p, ops in doc["paths"].items() for m in ops}
    assert got == want
    for p, ops in doc["paths"].items():
        for m, op in ops.items():
            assert op["x-permission"] == access.key(m.upper(), p)
            assert {"401", "403", "429"} <= set(op["responses"])
    key = issue(site, "view")["value"]
    assert kcall(site, key, "GET", "/api/openapi.json").json()["paths"] == doc["paths"]


def test_guide_matches_routes_and_has_no_internal_details(site):
    guide = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
    assert site.client.get("/api/guide").text == guide
    assert (ROOT / "live_recorder" / "web" / "api_guide.md").read_text(encoding="utf-8") == guide
    rows = set(re.findall(r"^\| (GET|POST|PUT|PATCH|DELETE) \| (\S+) \| (보기|운영) \|$", guide, re.M))
    access = site.app.state.access
    label = {"view": "보기", "operate": "운영"}
    want = {(m, p, label[access.key(m, p)]) for m, p in all_routes(site.app) if access.key(m, p) != acc.KEY_NONE}
    assert rows == want
    assert not re.search(r"\b(?:API|SEC|REC|VOD|MOD|CM|YT|RL|ADM|UI)-\d+|\bb\d{1,2}\b", guide)
    for word in ("Authorization: Bearer", "curl", "429", "Retry-After", "키로 할 수 없는 일"):
        assert word in guide


# ---- 화면 ----
def test_screen_has_the_access_key_group():
    from webui_files import all_js, index_html

    html, js = index_html(), all_js()
    assert 'id="apikeys-group"' in html and 'href="/api/guide"' in html
    assert "화면 없이 녹화를 시작·중지하고 채널을 더하거나 지울 수 있습니다" in js
    assert "/api/keys" in js


def test_openapi_descriptions_have_no_internal_numbers(site):
    """경로 설명은 docstring에서 오지만 문서에는 개발용 요구 번호가 나가지 않는다."""
    doc = site.client.get("/api/openapi.json").json()
    text = json.dumps(doc, ensure_ascii=False)
    assert not re.search(r"\b(?:ADM|API|CM|FM|MIG|MOD|NTF|PKG|REC|REL|RL|SEC|UI|VOD|YT)-\d+", text)
    from live_recorder.web.routes.apikeys import clean_text
    assert clean_text("full(전체)·latest") == "full(전체)·latest"
    assert clean_text("수집 취소. 받은 줄") == "수집 취소. 받은 줄"
