"""비밀번호 규칙: 최소 8자, 처리 부담을 막는 1024자 제한만 둔다.
계정 생성(CLI, 첫 설정 화면)과 비밀번호 변경(CLI, 웹)이 같은 상수 하나를 쓰고, 화면·CLI 안내의 숫자도 그 상수에서 온다."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from live_recorder.core import secretstore
from live_recorder import web

from conftest import ROOT
from test_web import PASSWORD, USER, Web

SEVEN, EIGHT = "abcdefg", "abcdefgh"


def test_constant_values():
    assert secretstore.MIN_PASSWORD == 8 and secretstore.MAX_PASSWORD == 1024
    assert "8자 이상" in secretstore.PASSWORD_RULE


def test_store_create_and_change(tmp_path):
    path = tmp_path / "account.json"
    with pytest.raises(secretstore.AccountError, match="8자 이상"):
        secretstore.create_account(path, "a", SEVEN)
    secretstore.create_account(path, "a", EIGHT)
    assert secretstore.verify_account(path, "a", EIGHT)
    with pytest.raises(secretstore.AccountError):
        secretstore.change_password(path, SEVEN)
    secretstore.change_password(path, "x" * 1024)  # 긴 비밀번호는 막지 않는다(1024자까지)
    with pytest.raises(secretstore.AccountError):
        secretstore.change_password(path, "x" * 1025)
    secretstore.change_password(path, EIGHT)
    assert secretstore.verify_account(path, "a", EIGHT)


def test_cli_create_and_passwd(tmp_path):
    data, root = tmp_path / "data", tmp_path / "rec"
    root.mkdir()
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(data), LIVE_RECORDER_RECORD_ROOT=str(root), PYTHONPATH=str(ROOT))
    cli = lambda *a, inp: subprocess.run([sys.executable, "-m", "live_recorder", "account", *a], cwd=ROOT, env=env,  # noqa: E731
                                         input=inp, capture_output=True, text=True, timeout=60)
    r = cli("create", "admin", inp=SEVEN + "\n")
    assert r.returncode == 1 and r.stderr.startswith(secretstore.PASSWORD_RULE)  # 입력 전에 규칙을 먼저 알린다
    assert cli("create", "admin", inp=EIGHT + "\n").returncode == 0
    r = cli("passwd", inp=SEVEN + "\n")
    assert r.returncode == 1 and r.stderr.startswith(secretstore.PASSWORD_RULE)
    assert cli("passwd", inp="12345678\n").returncode == 0
    assert secretstore.verify_account(data / "secrets" / "account.json", "admin", "12345678")


def test_web_change(tmp_path, server, tools):
    with Web(tmp_path, server, tools) as w:
        w.login()
        r = w.call("POST", "/api/account/password", json={"current": PASSWORD, "new": SEVEN})
        assert r.status_code == 400 and "8자 이상" in r.json()["error"]
        assert w.call("POST", "/api/account/password", json={"current": PASSWORD, "new": EIGHT}).status_code == 200
        assert secretstore.verify_account(w.h.cfg.account_path, USER, EIGHT)
        rule = w.client.get("/api/mode").json()["password"]
        assert rule == {"min": 8, "max": 1024, "text": secretstore.PASSWORD_RULE}


def test_screens_take_numbers_from_the_server():
    """화면(html·js)에 비밀번호 길이 숫자를 적지 않는다. 규칙은 /api/mode의 password에서 받는다."""
    ui = Path(web.UI_DIR)
    from webui_files import index_html, script_paths

    for path in [ui / n for n in ("setup.html", "index.html", "login.html", "setup.js", "login.js")] + script_paths():
        text = path.read_text()
        name = path.name
        assert not re.search(r"\d+\s*자 이상", text), name
    index = index_html()
    pw_form = index[index.index('id="password-form"'):index.index("</form>", index.index('id="password-form"'))]
    assert "minlength" not in pw_form and "data-pw-input" in pw_form and "data-pw-rule" in pw_form
    setup = (ui / "setup.html").read_text()
    for field in ('id="password"', 'id="password_confirm"'):
        line = next(l for l in setup.splitlines() if field in l)
        assert "minlength" not in line and "maxlength" not in line and "data-pw-input" in line
    assert "data-pw-rule" in setup
    for js in ("setup.js", "app.js"):
        assert "data-pw-rule" in (ui / js).read_text() and "/api/mode" in (ui / js).read_text()
