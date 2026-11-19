"""첫 설정 화면 . 계정이 없을 때 설정 코드로 웹에서 관리자 계정을 하나 만든다."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import zipfile

import httpx
import pytest

from live_recorder import diagnostics, web
from live_recorder.core import secretstore

from conftest import ROOT
from test_view_only import ViewSite
from test_web import all_routes

USER, PASSWORD = "first-admin", "eight888"  # 8자: 최소 길이


@pytest.fixture
def view(tmp_path, server, tools):
    with ViewSite(tmp_path, server, tools) as v:
        yield v


def code_of(view) -> str:
    code = secretstore.read_setup_code(view.h.cfg.setup_code_path)
    assert code
    return code


def submit(client, code, username=USER, password=PASSWORD, confirm=None, **headers):
    return client.post("/api/setup", json={"username": username, "password": password,
                                           "password_confirm": password if confirm is None else confirm, "code": code},
                       headers=headers)


def test_code_is_created_at_start_and_shown_only_on_stderr(capfd, view):
    """기동할 때 계정이 없으면 코드를 만든다(160비트, 0600). 표준 오류에만 한 번 찍고 로그 파일에는 코드 없이 남긴다."""
    path = view.h.cfg.setup_code_path
    code = code_of(view)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert len(secretstore.normalize_code(code)) == 32  # base32 32자 = 160비트
    err = capfd.readouterr().err
    assert err.count(code) == 1 and "첫 설정 코드" in err
    log = view.h.log_text()
    assert "setup_code_created" in log and code not in log and secretstore.normalize_code(code) not in log


def test_submit_rejections(view):
    """코드 없이, 틀린 코드로, 형식이 틀린 값으로는 만들지 않는다. 계정이 이미 있으면 거부한다."""
    c = view.client
    assert submit(c, "").status_code == 403
    assert submit(c, "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-GGGG-HHHH").status_code == 403
    assert c.post("/api/setup", json={"username": USER, "password": PASSWORD, "password_confirm": PASSWORD}).status_code == 422
    r = submit(c, code_of(view), password="seven77")
    assert r.status_code == 400 and "8자" in r.json()["error"]  # 규칙을 알려 준다
    assert submit(c, code_of(view), confirm=PASSWORD + "x").status_code == 400
    assert submit(c, code_of(view), username="has space").status_code == 400
    assert not secretstore.account_exists(view.h.cfg.account_path)
    # 출처 검사는 그대로 거친다
    assert submit(c, code_of(view), **{"Origin": "https://evil.example"}).status_code == 403
    assert not secretstore.account_exists(view.h.cfg.account_path)
    # 맞는 코드면 만든다(8자 비밀번호, 코드의 대소문자·하이픈은 상관없다)
    code = code_of(view)
    r = submit(c, code.lower().replace("-", " "))
    assert r.status_code == 201 and "lr_session" in r.headers["set-cookie"]
    assert secretstore.verify_account(view.h.cfg.account_path, USER, PASSWORD)
    assert not view.h.cfg.setup_code_path.exists()  # 만든 뒤 코드 파일이 없다
    assert c.get("/api/session").json()["username"] == USER  # 바로 로그인된다
    # 계정이 생긴 뒤: 다른 클라이언트는 로그인 화면으로, 로그인한 클라이언트는 409·첫 화면으로
    other = web_client(view)
    assert other.get("/setup").headers["location"] == "/login"
    assert submit(other, code).status_code == 401
    csrf = c.get("/api/session").json()["csrf"]
    assert c.post("/api/setup", json={"username": "x", "password": PASSWORD, "password_confirm": PASSWORD, "code": code},
                  headers={"X-CSRF-Token": csrf}).status_code == 409
    assert c.get("/setup").headers["location"] == "/"


def web_client(view):
    from fastapi.testclient import TestClient

    return TestClient(view.app, base_url="http://testserver", follow_redirects=False)


def test_concurrent_submits_create_exactly_one_account(view):
    """맞는 코드로 동시에 여러 번 제출해도 계정은 하나만 생긴다."""
    code = code_of(view)

    async def scenario():
        transport = httpx.ASGITransport(app=view.app, client=("127.0.0.1", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            reqs = [client.post("/api/setup", json={"username": f"user{i}", "password": PASSWORD,
                                                    "password_confirm": PASSWORD, "code": code}) for i in range(8)]
            return [r.status_code for r in await asyncio.gather(*reqs)]

    statuses = asyncio.run(scenario())
    assert statuses.count(201) == 1, statuses
    assert all(s in (201, 401, 403, 409) for s in statuses), statuses
    name = secretstore.account_name(view.h.cfg.account_path)
    assert name in {f"user{i}" for i in range(8)}
    assert [p.name for p in view.h.cfg.account_path.parent.iterdir() if p.name.startswith("account")] == ["account.json"]


def test_store_guarantees_single_account(tmp_path):
    """저장소 수준: 여러 스레드가 동시에 만들어도 하나만 성공한다(link로 이름을 붙인다)."""
    path = tmp_path / "secrets" / "account.json"
    results = []
    barrier = threading.Barrier(6)

    def make(i):
        barrier.wait()
        try:
            secretstore.create_account(path, f"u{i}", PASSWORD)
            results.append(i)
        except secretstore.AccountError:
            pass

    threads = [threading.Thread(target=make, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 1 and secretstore.account_name(path) == f"u{results[0]}"
    assert sorted(p.name for p in path.parent.iterdir()) == ["account.json"]


def test_deleting_account_creates_a_new_code(view, capfd):
    """계정을 지우면 다음 요청에 새 코드를 만든다. 재시작 없이 된다."""
    old = code_of(view)
    assert submit(view.client, old).status_code == 201
    capfd.readouterr()
    secretstore.remove(view.h.cfg.account_path)
    assert view.client.get("/api/mode").json()["mode"] == "view"
    new = code_of(view)
    assert new != old and capfd.readouterr().err.count(new) == 1
    assert submit(web_client(view), old).status_code == 403  # 옛 코드는 듣지 않는다
    assert submit(web_client(view), new, username="second").status_code == 201


def test_failures_are_limited_even_when_login_limit_is_off(view):
    """코드 실패는 제한에 센다. 로그인 제한을 꺼 두어도 첫 설정은 따로 제한한다."""
    c = view.client
    for _ in range(5):
        assert submit(c, "WRONG").status_code == 403
    assert submit(c, code_of(view)).status_code == 429  # 맞는 코드도 잠금 동안은 안 된다
    assert view.app.state.limiter._fails  # 로그인 제한에도 들어갔다
    assert not secretstore.account_exists(view.h.cfg.account_path)
    # 로그인 제한을 끈 상태
    view.h.store.set_settings({"login_limit_enabled": False})
    time.sleep(1.1)

    async def scenario():
        transport = httpx.ASGITransport(app=view.app, client=("192.0.2.40", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            out = []
            for _ in range(6):
                r = await client.post("/api/setup", json={"username": USER, "password": PASSWORD,
                                                          "password_confirm": PASSWORD, "code": "WRONG"})
                out.append(r.status_code)
            return out

    assert asyncio.run(scenario()) == [403] * 5 + [429]
    # 형식 오류는 세지 않는다
    other = web_client(view)
    for _ in range(10):
        assert submit(other, "x", password="short").status_code in (400, 429)


def test_code_never_leaves_through_api_or_diagnostics(view):
    """코드가 API 응답·화면·진단팩·로그 파일에 없다."""
    code = code_of(view)
    plain = secretstore.normalize_code(code)
    blobs = []
    for method, path in all_routes(view.app):
        if method == "GET" and "{" not in path:
            blobs.append(view.client.get(path, params={"view": "1"}).text)
    blobs.append(submit(view.client, "WRONG").text)
    blobs.append(submit(view.client, code, password="short").text)
    pack = diagnostics.create(view.h.cfg, "start")
    with zipfile.ZipFile(pack) as z:
        blobs.extend(z.read(n).decode() for n in z.namelist())
        assert json.loads(z.read("settings.json"))["secrets"]["setup_code"] is True
    blobs.append(view.h.log_text())
    blob = "\n".join(blobs)
    assert code not in blob and plain not in blob


def test_cli_rules_and_setup_code(tmp_path):
    """CLI는 입력을 받기 전에 비밀번호 규칙을 알린다. setup-code는 파일의 코드를 보여 주고, 계정을 만들면 코드가 지워진다."""
    data, root = tmp_path / "data", tmp_path / "rec"
    root.mkdir()
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(data), LIVE_RECORDER_RECORD_ROOT=str(root), PYTHONPATH=str(ROOT))
    cli = lambda *a, inp=None: subprocess.run([sys.executable, "-m", "live_recorder", "account", *a], cwd=ROOT, env=env,  # noqa: E731
                                              input=inp, capture_output=True, text=True, timeout=60)
    r = cli("setup-code")
    assert r.returncode == 0
    code = secretstore.read_setup_code(data / "secrets" / "setup-code")
    assert code in r.stdout
    assert cli("setup-code").stdout == r.stdout  # 같은 코드
    r = cli("create", "admin", inp="seven77\n")
    assert r.returncode == 1 and "8자 이상" in r.stderr and r.stderr.index("8자 이상") < r.stderr.index("거부")
    r = cli("create", "admin", inp=PASSWORD + "\n")
    assert r.returncode == 0 and "8자 이상" in r.stderr
    assert not (data / "secrets" / "setup-code").exists()
    r = cli("setup-code")
    assert r.returncode == 1 and "이미 있어" in r.stderr
    r = cli("passwd", inp="short\n")
    assert r.returncode == 1 and "8자 이상" in r.stderr
    assert cli("create", "other", inp=PASSWORD + "\n").returncode == 1  # 계정은 하나
