"""설정 코드 끄기. 환경변수 LIVE_RECORDER_SETUP_CODE=off면 첫 설정에서 코드를 받지 않는다. 기본은 받는다(test_setup.py)."""

import re
from pathlib import Path

import pytest

from live_recorder import cli
from live_recorder.core import secretstore

from test_view_only import ViewSite

USER, PASSWORD = "first-admin", "eight888"
WEBUI = Path(__file__).resolve().parents[1] / "live_recorder" / "webui"


@pytest.fixture
def off(tmp_path, server, tools, monkeypatch):
    monkeypatch.setenv(secretstore.SETUP_CODE_ENV, "OFF")  # 대소문자 무시
    with ViewSite(tmp_path, server, tools) as v:
        yield v


def test_switch_values(monkeypatch):
    for value, enabled in (("off", False), ("Off", False), (" OFF ", False), ("", True), ("on", True), ("0", True), ("false", True)):
        assert secretstore.setup_code_enabled({secretstore.SETUP_CODE_ENV: value}) is enabled, value
    assert secretstore.setup_code_enabled({}) is True


def test_no_code_is_made_and_account_is_created_without_one(capfd, off):
    path = off.h.cfg.setup_code_path
    assert not path.exists()
    err = capfd.readouterr().err
    assert "첫 설정 코드" not in err and secretstore.SETUP_CODE_WARNING in err
    log = off.h.log_text()
    assert '"event": "setup_code_disabled"' in log and "setup_code_created" not in log
    mode = off.client.get("/api/mode").json()
    assert mode["setup_code"] is False and mode["setup_warning"] == secretstore.SETUP_CODE_WARNING and "설정 코드" not in mode["guide"]
    assert off.client.get("/setup").status_code == 200 and not path.exists()
    # 코드 없이, 또는 아무 코드 값이 와도(보지 않는다) 계정을 만든다
    r = off.client.post("/api/setup", json={"username": USER, "password": PASSWORD, "password_confirm": PASSWORD, "code": "WRONG"})
    assert r.status_code == 201, r.text
    assert secretstore.account_exists(off.h.cfg.account_path) and not path.exists()
    assert off.client.post("/api/setup", json={"username": "x", "password": PASSWORD, "password_confirm": PASSWORD}).status_code in (403, 409)  # 계정이 생기면 닫힌다


def test_other_protections_stay(off):
    """비밀번호 규칙과 형식 검사는 그대로다."""
    r = off.client.post("/api/setup", json={"username": USER, "password": "short", "password_confirm": "short"})
    assert r.status_code == 400
    r = off.client.post("/api/setup", json={"username": USER, "password": PASSWORD, "password_confirm": PASSWORD + "x"})
    assert r.status_code == 400 and "다릅니다" in r.json()["error"]


def test_existing_code_file_is_removed(tmp_path, server, tools, monkeypatch):
    with ViewSite(tmp_path / "on", server, tools) as v:
        path = v.h.cfg.setup_code_path
        assert path.exists()  # 기본은 코드를 만든다
        monkeypatch.setenv(secretstore.SETUP_CODE_ENV, "off")
        v.client.get("/api/mode")
        assert not path.exists()  # 끈 뒤 다음 요청에서 지운다


def test_cli_setup_code_says_disabled(off, capsys, monkeypatch):
    code = cli.main(["--data-dir", str(off.h.cfg.data_dir), "--record-root", str(off.h.root), "account", "setup-code"])
    assert code == 1 and secretstore.SETUP_CODE_DISABLED in capsys.readouterr().err
    assert not off.h.cfg.setup_code_path.exists()


def test_screen_hides_the_code_field():
    html = (WEBUI / "setup.html").read_text(encoding="utf-8")
    js = (WEBUI / "setup.js").read_text(encoding="utf-8")
    assert re.search(r'<label data-code-part>설정 코드<input id="code"', html) and 'id="setup-warning"' in html
    assert "mode.setup_code === false" in js and 'querySelectorAll("[data-code-part]")' in js and "if (useCode) body.code" in js
