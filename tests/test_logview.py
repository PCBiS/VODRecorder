"""로그 보기 (GET /api/logs, logview.py).

새것부터 한 쪽씩, 더 오래된 쪽 이어 읽기, 새 줄 받기, 순환된 파일 넘나들기, 쓰는 중인 마지막 줄, 거르기, 비밀 가리기,
읽는 양 상한, 접근 규칙(로그인·접근 키·보기 전용)을 본다.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_recorder import logview, web
from live_recorder.core import secretstore
from live_recorder.web.access import access_level, key_permission, LOGIN, KEY_NONE

from conftest import CHANNEL
from test_view_only import ViewSite
from test_web import Web

T0 = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
SECRET_COOKIE = "NID_AUT=planted-cookie-value-1234567"


class Cfg:
    def __init__(self, log_dir: Path, tmp: Path):
        self.log_dir = log_dir
        self.telegram_path = tmp / "telegram.json"
        self.setup_code_path = tmp / "setup-code"
        self.data_dir = tmp


def rec(i, level="INFO", **extra):
    return {"ts": (T0 + timedelta(seconds=i)).isoformat(timespec="milliseconds"), "level": level,
            "logger": "live_recorder.engine", "msg": f"줄 {i}", **extra}


def write(path: Path, rows, tail: str = ""):
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.write(tail)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(logview, "known_secrets", lambda cfg, extra=(): list(extra))
    return Cfg(log_dir, tmp_path)


def msgs(page):
    return [e["msg"] for e in page["entries"]]


def test_pages_newest_first_across_rotated_files(cfg):
    # 오래된 것: .log.2(0~9) → .log.1(10~19) → .log(20~29)
    write(cfg.log_dir / "live-recorder.log.2", [rec(i) for i in range(0, 10)])
    write(cfg.log_dir / "live-recorder.log.1", [rec(i) for i in range(10, 20)])
    write(cfg.log_dir / "live-recorder.log", [rec(i) for i in range(20, 30)])
    seen, before = [], None
    pages = 0
    while True:
        page = logview.read_page(cfg, before=before, limit=7)
        seen += msgs(page)
        pages += 1
        before = page["older"]
        if not before:
            break
    assert seen == [f"줄 {i}" for i in range(29, -1, -1)]
    assert pages == 5
    assert logview.read_page(cfg, limit=500)["files"] == 3


def test_partial_last_line_is_skipped_until_complete(cfg):
    path = cfg.log_dir / "live-recorder.log"
    partial = json.dumps(rec(3))
    write(path, [rec(0), rec(1), rec(2)], tail=partial[:20])
    page = logview.read_page(cfg)
    assert msgs(page) == ["줄 2", "줄 1", "줄 0"] and page["bad_lines"] == 0
    # 줄이 다 쓰이면 새 줄 받기로 들어온다
    with path.open("a", encoding="utf-8") as f:
        f.write(partial[20:] + "\n")
        f.write(json.dumps(rec(4)) + "\n")
    newer = logview.read_newer(cfg, after=page["newer"])
    assert msgs(newer) == ["줄 4", "줄 3"] and not newer["reset"] and not newer["more"]
    again = logview.read_newer(cfg, after=newer["newer"])
    assert again["entries"] == [] and again["newer"] == newer["newer"]


def test_newer_follows_rotation_by_inode(cfg):
    path = cfg.log_dir / "live-recorder.log"
    write(path, [rec(0), rec(1)])
    page = logview.read_page(cfg)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec(2)) + "\n")
    # 순환: 현재 파일이 .1이 되고 새 파일이 생긴다
    os.rename(path, cfg.log_dir / "live-recorder.log.1")
    write(path, [rec(3), rec(4)])
    newer = logview.read_newer(cfg, after=page["newer"])
    assert msgs(newer) == ["줄 4", "줄 3", "줄 2"] and not newer["reset"]
    # 읽던 파일이 지워지면 처음부터 다시 읽게 한다
    os.remove(cfg.log_dir / "live-recorder.log.1")
    assert logview.read_newer(cfg, after=page["newer"])["reset"] is True
    assert logview.read_page(cfg, before=page["newer"])["entries"] == []


def test_newer_too_many_keeps_newest_and_says_more(cfg):
    path = cfg.log_dir / "live-recorder.log"
    write(path, [rec(0)])
    page = logview.read_page(cfg)
    with path.open("a", encoding="utf-8") as f:
        for i in range(1, 11):
            f.write(json.dumps(rec(i)) + "\n")
    newer = logview.read_newer(cfg, after=page["newer"], limit=3)
    assert msgs(newer) == ["줄 10", "줄 9", "줄 8"] and newer["more"] is True


def test_filters_level_channel_text_and_bad_lines(cfg):
    rows = [rec(0, channel_id=CHANNEL, event="record_start"), rec(1, "WARNING", channel_id="f" * 32),
            rec(2, "ERROR", channel_id=CHANNEL.upper(), exc="Traceback\nValueError: 찾을글"), rec(3, "DEBUG")]
    write(cfg.log_dir / "live-recorder.log", rows, tail="")
    with (cfg.log_dir / "live-recorder.log").open("a", encoding="utf-8") as f:
        f.write("이건 JSON이 아니다\n[1, 2]\n")
    F = logview.Filter
    assert msgs(logview.read_page(cfg, flt=F(level=logview.LEVELS["WARNING"]))) == ["줄 2", "줄 1"]
    assert msgs(logview.read_page(cfg, flt=F(channel=CHANNEL))) == ["줄 2", "줄 0"]
    assert msgs(logview.read_page(cfg, flt=F(text="찾을글"))) == ["줄 2"]
    assert msgs(logview.read_page(cfg, flt=F(text="RECORD_START"))) == ["줄 0"]  # 대소문자 무시
    page = logview.read_page(cfg)
    assert page["bad_lines"] == 2 and len(page["entries"]) == 4
    first = page["entries"][-1]
    assert first["event"] == "record_start" and first["channel_id"] == CHANNEL and "fields" not in first


def test_secrets_masked_but_addresses_and_names_kept(cfg, monkeypatch):
    monkeypatch.setattr(logview, "known_secrets", lambda cfg, extra=(): ["planted-known-secret-xyz"] + list(extra))
    write(cfg.log_dir / "live-recorder.log", [
        rec(0, msg=f"쿠키 {SECRET_COOKIE} 비밀 planted-known-secret-xyz", client="203.0.113.7", event="login"),
        rec(1, msg="세션 planted-session-token-abc", nested={"v": "planted-known-secret-xyz"}),
    ])
    page = logview.read_page(cfg, session_secrets=["planted-session-token-abc"])
    blob = json.dumps(page, ensure_ascii=False)
    for value in ("planted-cookie-value-1234567", "planted-known-secret-xyz", "planted-session-token-abc"):
        assert value not in blob, value
    assert "[가림]" in blob
    login = page["entries"][1]
    assert login["fields"]["client"] == "203.0.113.7"  # 관리자가 보는 화면이라 접속 주소는 그대로
    # 가린 뒤에 찾는다(비밀 값으로 찾아도 나오지 않는다)
    assert logview.read_page(cfg, flt=logview.Filter(text="planted-known-secret"))["entries"] == []


def test_scan_limit_stops_and_continues(cfg):
    rows = [rec(i, "ERROR" if i == 0 else "INFO") for i in range(200)]
    write(cfg.log_dir / "live-recorder.log", rows)
    flt = logview.Filter(level=logview.LEVELS["ERROR"])
    page = logview.read_page(cfg, flt=flt, scan_bytes=2000)
    assert page["entries"] == [] and page["stopped"] is True and page["older"]
    found, before = [], page["older"]
    while before:
        page = logview.read_page(cfg, before=before, flt=flt, scan_bytes=2000)
        found += msgs(page)
        before = page["older"]
    assert found == ["줄 0"]


def test_bad_cursor_and_empty_dir(cfg):
    with pytest.raises(logview.CursorError):
        logview.read_page(cfg, before="../../etc")
    with pytest.raises(logview.CursorError):
        logview.read_newer(cfg, after="1-2")
    page = logview.read_page(cfg)
    assert page == {"entries": [], "older": None, "newer": None, "stopped": False, "bad_lines": 0, "files": 0}


# ---- 경로 ----

def test_access_rules():
    assert access_level("GET", "/api/logs") == LOGIN
    assert key_permission("GET", "/api/logs") == KEY_NONE


@pytest.fixture
def site(tmp_path, server, tools):
    with Web(tmp_path, server, tools) as w:
        yield w


def test_api_reads_real_log_and_follows(site):
    import logging

    r = site.client.get("/api/logs")
    assert r.status_code == 401
    site.login()
    log = logging.getLogger("live_recorder.web")
    log.warning("로그 보기 시험 %s", "첫째", extra={"event": "logview_test", "channel_id": CHANNEL})
    page = site.client.get("/api/logs", params={"q": "로그 보기 시험"}).json()
    assert [e["msg"] for e in page["entries"]] == ["로그 보기 시험 첫째"]
    e = page["entries"][0]
    assert e["level"] == "WARNING" and e["event"] == "logview_test" and e["channel_id"] == CHANNEL and "tmpl" not in e
    log.error("로그 보기 시험 %s", "둘째", extra={"event": "logview_test"})
    newer = site.client.get("/api/logs", params={"q": "로그 보기 시험", "after": page["newer"]}).json()
    assert [x["msg"] for x in newer["entries"]] == ["로그 보기 시험 둘째"]
    only_err = site.client.get("/api/logs", params={"q": "로그 보기 시험", "level": "error"}).json()
    assert [x["msg"] for x in only_err["entries"]] == ["로그 보기 시험 둘째"]
    # 세션 토큰은 로그에 섞여 들어와도 가린다
    token = site.client.cookies.get(web.SESSION_COOKIE)
    log.info("섞인 토큰 %s", token)
    assert token not in site.client.get("/api/logs").text
    for bad in ({"level": "LOUD"}, {"before": "x"}, {"after": "1-2-3", "before": "1-2-3"}, {"limit": "0"}, {"limit": "501"}):
        assert site.client.get("/api/logs", params=bad).status_code in (400, 422), bad


def test_view_only_mode_refuses(tmp_path, server, tools):
    with ViewSite(tmp_path, server, tools) as v:
        assert v.client.get("/api/logs").status_code in (401, 403)


# ---- 화면(헤드리스 Chromium. Playwright나 브라우저가 없으면 건너뛴다) ----

from test_settings_tabs import browser_site, open_page, until  # noqa: E402,F401  (fixture)


def test_screen_text_viewer_filters_follows_and_pages(browser_site):  # noqa: F811
    import logging

    log = logging.getLogger("live_recorder.web")
    for i in range(230):
        log.info("화면 시험 줄 %s", i, extra={"event": "logview_screen"})
    try:
        raise ValueError("시험 예외")
    except ValueError:
        log.exception("화면 시험 오류 %s", "하나", extra={"event": "logview_screen", "fails": 3})
    page = open_page(browser_site)
    page.click(".tab[data-view=logs]")
    until(page, "!document.getElementById('view-logs').hidden && location.hash === '#logs'")
    until(page, "document.querySelectorAll('#logs-body .logline').length >= 200")
    lines = "#logs-body .logline"
    # 로그 파일처럼 아래가 새 줄이다. 맨 아래는 로그인 줄, 그 위가 오류 줄(오류 내용은 그 줄 아래 들여 써서)
    last = page.eval_on_selector_all(lines, "(rows) => rows.slice(-2).map((r) => [r.innerText, r.className])")
    assert "로그인" in last[1][0], last
    err, cls = last[0]
    assert "ERROR" in err and "[logview_screen]" in err and "화면 시험 오류 하나" in err and "fails=3" in err, err
    assert "\n    Traceback" in err and "ValueError: 시험 예외" in err and "bad" in cls
    # 처음 열면 맨 아래(가장 새 줄)를 보고 있다
    assert page.evaluate("(b => b.scrollHeight - b.scrollTop - b.clientHeight < 5)(document.getElementById('logs-body'))")
    # 더 오래된 줄은 위에 붙는다
    page.click("#logs-older")
    until(page, f"document.querySelectorAll('{lines}').length > 200")
    text = page.inner_text("#logs-body")
    assert text.index("화면 시험 줄 0\n") < text.index("화면 시험 줄 229") < text.index("화면 시험 오류 하나")
    # 수준 거르기
    page.select_option("#logs-level", "ERROR")
    until(page, f"[...document.querySelectorAll('{lines}')].every((r) => r.dataset.level === 'ERROR')")
    # 찾기
    page.select_option("#logs-level", "")
    page.fill("#logs-q", "화면 시험 줄 7")
    until(page, f"document.querySelectorAll('{lines}').length === 11")  # 7, 70~79
    # 새 줄 자동으로 보기(주기 갱신 5초). 새 줄은 맨 아래에 붙는다
    log.info("화면 시험 줄 7 새로", extra={"event": "logview_screen"})
    until(page, f"document.querySelector('{lines}:last-child').innerText.includes('화면 시험 줄 7 새로')", timeout=15)
    # 긴 줄 바꿈 끄기
    page.uncheck("#logs-wrap")
    assert page.evaluate("document.getElementById('logs-body').classList.contains('nowrap')")
    # 좁은 화면에서도 쪽 전체가 가로로 넘치지 않는다(긴 줄은 뷰어 안에서만 넘친다)
    page.set_viewport_size({"width": 390, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    assert page.errors == []
