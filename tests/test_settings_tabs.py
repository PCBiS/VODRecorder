"""설정 화면의 하위 탭.

마크업 검사는 늘 돈다. 동작(한 탭만 보임, 주소 # 유지, 탭별 저장, 저장하지 않은 변경 확인, 화살표 키, 좁은 화면)은
헤드리스 Chromium(Playwright)으로 실제 화면을 눌러 본다. Playwright나 브라우저가 없으면 그 시험만 건너뛴다.
"""

import json
import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from live_recorder.core import logs, secretstore
from live_recorder import web

from test_web import PASSWORD, USER
from webui_files import index_html, all_js

UI = Path(web.UI_DIR)
PARTS = ["record", "replay", "notify", "account", "general"]
LABELS = ["녹화", "다시보기", "알림", "계정·보안", "일반"]
FORMS = {"record": "settings-form", "replay": "settings-replay-form", "notify": "settings-notify-form",
         "account": "settings-account-form", "general": "settings-general-form"}
# 탭마다 들어가야 하는 설정 칸(묶음)
FIELDS = {
    # 전역 확인 주기 칸(check_interval)은 플랫폼별 스캔 주기·채널 사이 간격으로 바뀌었다(플랫폼 줄은 따로 저장)
    "record": ["min_free_gib", "timemachine_enabled", "rewind_seconds", "remux_enabled", "delete_original",
               "filename_pattern"],
    "replay": ["replay_dir", "replay_min_free_gib", "replay_connections", "replay_segment_connections", "replay_pause_while_recording",
               "replay_window_enabled", "replay_window_end_action"],
    "notify": ["notify_enabled", "notify_dedupe_seconds"],
    "account": ["session_minutes", "login_limit_enabled", "trusted_proxies"],
    "general": ["fm_enabled", "fm_readonly", "fm_trash", "fm_show_hidden", "fm_roots"],
}
# 화면 칸 이름 → 저장 키(GiB 칸은 바이트 키로 보낸다)
KEY_OF = {"min_free_gib": "min_free_bytes", "replay_min_free_gib": "replay_min_free_bytes"}
WORK = Path(os.environ.get("LIVE_RECORDER_TEST_WORK", "/nonexistent"))  # 브라우저·글꼴을 따로 둔 폴더(있으면)


def panel(html, part):
    start = html.index(f'<div id="spanel-{part}"')
    end = html.index('<div id="spanel-', start + 10) if part != PARTS[-1] else html.index("</section>", start)
    return html[start:end]


def test_markup_groups_and_roles():
    """탭 다섯 개와 role, 탭마다 들어갈 칸, 탭마다 저장 단추. 다른 폼(텔레그램·비밀번호·로그)도 맞는 탭에 있다."""
    html = index_html()
    tablist = html[html.index('id="settings-tabs"'):html.index("</div>", html.index('id="settings-tabs"'))]
    assert 'role="tablist"' in tablist
    tabs = re.findall(r'<button class="subtab" id="stab-([a-z]+)" type="button" role="tab" data-stab="\1" aria-controls="spanel-\1"'
                      r' aria-selected="false" tabindex="-1">([^<]+)</button>', tablist)
    assert tabs == list(zip(PARTS, LABELS))
    for part in PARTS:
        body = panel(html, part)
        assert f'role="tabpanel" aria-labelledby="stab-{part}"' in body and " hidden>" in body.split("\n")[0]
        assert f'<form id="{FORMS[part]}" class="panel" data-part="{part}"' in body
        assert body.count('type="submit">이 탭 저장</button>') == 1
        for name in FIELDS[part]:
            assert f'name="{name}"' in body, (part, name)
            for other in PARTS:
                if other != part:
                    assert f'name="{name}"' not in panel(html, other), (name, other)
    assert 'id="event-list"' in panel(html, "notify") and 'id="telegram-form"' in panel(html, "notify")
    assert 'id="password-form"' in panel(html, "account") and 'id="export-form"' in panel(html, "general")
    assert 'id="window-rules"' in panel(html, "replay")
    js = all_js()
    keys = {KEY_OF.get(n, n) for names in FIELDS.values() for n in names} | {"replay_window_rules"}
    for key in keys:
        assert f"{key}:" in js, key


def _playwright():
    if WORK.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(WORK / "pw-browsers"))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright가 없다")
    return sync_playwright


@pytest.fixture
def browser_site(tmp_path, server, tools, monkeypatch, request):
    libs = [WORK / "syslibs/usr/lib/x86_64-linux-gnu", WORK / "syslibs/lib/x86_64-linux-gnu"]
    libs = [p for p in libs if p.exists()]
    if libs:
        # 이 환경의 Playwright 드라이버(node)와 Chromium이 쓰는 라이브러리. 이 시험 동안만 둔다
        monkeypatch.setenv("LD_LIBRARY_PATH", ":".join(str(p) for p in libs))
    if (WORK / "fonts.conf").exists():
        monkeypatch.setenv("FONTCONFIG_FILE", str(WORK / "fonts.conf"))
    sync_playwright = _playwright()
    import uvicorn

    from conftest import Harness

    h = Harness(tmp_path, server, tools)
    secretstore.create_account(h.cfg.account_path, USER, PASSWORD)
    logs.setup(h.cfg.log_dir, console=False)
    from conftest import REPLAY_EXTENSIONS

    h.store.set_extra({"ext:enabled": list(REPLAY_EXTENSIONS)})  # 설정의 "다시보기" 하위 탭은 downloads 확장이 켜져 있을 때만 있다
    prepare = getattr(request, "param", None)  # 간접 매개변수로 앱을 만들기 전 준비(가짜 확장 켜기 등)
    if callable(prepare):
        prepare(h)
    app = web.create_app(h.cfg, h.timings, disk_free=h.disk_free)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 30  # 서버가 뜨지 못하면(시작 실패) 끝없이 기다리지 않는다
    while not srv.started:
        if not t.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("시험 서버가 뜨지 않았다(시작 실패)")
        time.sleep(0.05)
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"브라우저를 띄우지 못했다: {type(e).__name__}")
            try:
                yield {"url": f"http://127.0.0.1:{port}", "browser": browser, "store": h.store, "app": app}
            finally:
                browser.close()
    finally:
        srv.should_exit = True
        t.join(10)
        logs.setup(None, console=False)
        h.store.close()


def open_page(site, width=1280, hash_=""):
    ctx = site["browser"].new_context(viewport={"width": width, "height": 900}, locale="ko-KR")
    page = ctx.new_page()
    page.dialogs = []
    page.answer = True
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))

    def on_dialog(d):
        page.dialogs.append(d.message)
        d.accept() if page.answer else d.dismiss()

    page.on("dialog", on_dialog)
    page.goto(site["url"] + "/login")
    page.fill("#username", USER)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector("#view-channels:not([hidden])")
    if hash_:
        page.goto(site["url"] + "/" + hash_)
        page.wait_for_selector("#view-settings:not([hidden])")
        until(page, "document.querySelector('#settings-form [name=rewind_seconds]').value !== ''")
    return page


def until(page, expr, timeout=10.0):
    """CSP(unsafe-eval 없음) 때문에 wait_for_function 대신 evaluate로 기다린다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.evaluate(expr):
            return
        time.sleep(0.05)
    raise AssertionError(f"시간 안에 참이 되지 않았다: {expr}")


def visible_part(page):
    shown = [p for p in PARTS if page.is_visible(f"#spanel-{p}")]
    assert len(shown) == 1, shown
    selected = [p for p in PARTS if page.get_attribute(f"#stab-{p}", "aria-selected") == "true"]
    assert selected == shown
    assert [p for p in PARTS if page.get_attribute(f"#stab-{p}", "tabindex") == "0"] == shown
    return shown[0]


def hash_of(page):
    return page.evaluate("location.hash")


def test_one_tab_hash_deep_link_reload_and_keys(browser_site):
    page = open_page(browser_site, hash_="#settings/notify")
    assert visible_part(page) == "notify" and hash_of(page) == "#settings/notify"
    page.click("#stab-account")
    assert visible_part(page) == "account" and hash_of(page) == "#settings/account"
    page.reload()
    page.wait_for_selector("#view-settings:not([hidden])")
    assert visible_part(page) == "account"
    # 다른 메뉴를 다녀와도 고른 탭이 남는다. 다른 메뉴의 주소는 그대로다
    page.click(".tab[data-view=channels]")
    assert hash_of(page) == "#channels"
    page.click(".tab[data-view=settings]")
    assert visible_part(page) == "account" and hash_of(page) == "#settings/account"
    # 주소창에서 바꿔도 따라간다
    page.evaluate("location.hash = '#settings/replay'")
    until(page, "document.getElementById('stab-replay').getAttribute('aria-selected') === 'true'")
    assert visible_part(page) == "replay"
    # 잘못된 탭 이름은 녹화 탭
    page.goto(browser_site["url"] + "/#settings/nope")
    page.wait_for_selector("#view-settings:not([hidden])")
    assert visible_part(page) == "record"
    # 화살표·Home·End
    page.focus("#stab-record")
    page.keyboard.press("ArrowRight")
    assert visible_part(page) == "replay" and page.evaluate("document.activeElement.id") == "stab-replay"
    page.keyboard.press("End")
    assert visible_part(page) == "general"
    page.keyboard.press("ArrowRight")
    assert visible_part(page) == "record"  # 끝에서 처음으로 돈다
    page.keyboard.press("ArrowLeft")
    assert visible_part(page) == "general"
    page.keyboard.press("Home")
    assert visible_part(page) == "record" and hash_of(page) == "#settings/record"
    assert page.errors == [] and page.dialogs == []


def test_per_tab_save_sends_only_that_tab(browser_site):
    page = open_page(browser_site, hash_="#settings/record")
    sent = []
    page.on("request", lambda r: sent.append(json.loads(r.post_data)) if r.method == "PATCH" and r.url.endswith("/api/settings") else None)
    # 다른 곳(CLI 등)에서 알림 설정을 바꿔 둔다. 녹화 탭 저장이 이 값을 덮으면 안 된다
    browser_site["store"].set_settings({"notify_dedupe_seconds": 777, "session_minutes": 55})
    page.fill("#settings-form [name=rewind_seconds]", "61")
    page.fill("#settings-form [name=filename_pattern]", "{channel_name} {safe_live_title}{file_extension}")
    page.click("#settings-form button[type=submit]")
    page.wait_for_selector("#toast:has-text('설정을 저장')")
    assert sent == [{"rewind_seconds": 61, "filename_pattern": "{channel_name} {safe_live_title}{file_extension}"}]
    s = browser_site["store"].get_settings()
    assert s.rewind_seconds == 61 and s.notify_dedupe_seconds == 777 and s.session_minutes == 55
    # 저장한 뒤 고치던 것이 없는 다른 탭은 서버의 새 값으로 다시 채운다
    assert page.input_value("#settings-account-form [name=session_minutes]") == "55"
    # 계정 탭: 바꾼 칸만 보낸다
    page.click("#stab-account")
    page.check("#settings-account-form [name=login_limit_enabled]") if not page.is_checked(
        "#settings-account-form [name=login_limit_enabled]") else page.uncheck("#settings-account-form [name=login_limit_enabled]")
    page.click("#settings-account-form button[type=submit]")
    until(page, "document.querySelectorAll('#toast').length && !document.getElementById('toast').hidden")
    time.sleep(0.3)
    assert list(sent[-1]) == ["login_limit_enabled"]
    assert browser_site["store"].get_settings().notify_dedupe_seconds == 777
    # 고치던 탭은 다른 탭을 저장해도 그대로 남고, 저장할 때 바꾼 칸만 보낸다(그 사이 서버에서 바뀐 값을 덮지 않는다)
    page.click("#stab-notify")
    page.check("#settings-notify-form [name=notify_enabled]") if not page.is_checked("#settings-notify-form [name=notify_enabled]") \
        else page.uncheck("#settings-notify-form [name=notify_enabled]")
    page.evaluate("location.hash = '#settings/general'")  # 주소로 옮기기도 확인을 받는다(받아들이면 버린다)
    assert visible_part(page) == "general" and page.dialogs[-1].startswith("저장하지 않은 변경")
    page.click("#stab-notify")
    browser_site["store"].set_settings({"notify_dedupe_seconds": 888})
    page.fill("#settings-notify-form [name=notify_dedupe_seconds]", "889")
    page.click("#stab-record")  # 확인을 받아들이면 버린다
    page.click("#stab-notify")
    page.fill("#settings-notify-form [name=notify_dedupe_seconds]", "889")
    browser_site["store"].set_settings({"session_minutes": 66})
    page.click("#settings-notify-form button[type=submit]")
    until(page, "document.getElementById('settings-notify-form').notify_dedupe_seconds.value === '889'")
    time.sleep(0.3)
    assert sent[-1] == {"notify_dedupe_seconds": 889} and browser_site["store"].get_settings().session_minutes == 66
    # 바뀐 값이 없으면 보내지 않는다
    n = len(sent)
    page.click("#stab-general")
    page.click("#settings-general-form button[type=submit]")
    page.wait_for_selector("#toast:has-text('바뀐 값이 없습니다')")
    assert len(sent) == n


def test_unsaved_changes_are_confirmed(browser_site):
    page = open_page(browser_site, hash_="#settings/notify")
    page.fill("#settings-notify-form [name=notify_dedupe_seconds]", "4321")
    # 다른 탭으로: 거절하면 그대로 남는다
    page.answer = False
    page.click("#stab-record")
    assert len(page.dialogs) == 1 and "저장하지 않은 변경" in page.dialogs[0]
    assert visible_part(page) == "notify" and page.input_value("#settings-notify-form [name=notify_dedupe_seconds]") == "4321"
    # 다른 메뉴로: 거절하면 설정에 남는다
    page.click(".tab[data-view=history]")
    assert len(page.dialogs) == 2 and page.is_visible("#view-settings") and hash_of(page) == "#settings/notify"
    # 화살표 키도 묻는다
    page.focus("#stab-notify")
    page.keyboard.press("ArrowRight")
    assert len(page.dialogs) == 3 and visible_part(page) == "notify"
    # 받아들이면 옮기고, 고치던 값은 저장된 값으로 돌아간다
    page.answer = True
    page.click("#stab-account")
    assert len(page.dialogs) == 4 and visible_part(page) == "account"
    page.click("#stab-notify")
    assert page.input_value("#settings-notify-form [name=notify_dedupe_seconds]") != "4321"
    assert len(page.dialogs) == 4  # 고친 것이 없으면 묻지 않는다
    # 비밀 칸(비밀번호 변경)에 적어 둔 것도 저장하지 않은 변경이다
    page.click("#stab-account")
    page.fill("#password-form [name=current]", "something")
    page.answer = False
    page.click(".tab[data-view=channels]")
    assert len(page.dialogs) == 5 and page.is_visible("#view-settings")
    page.answer = True
    page.click(".tab[data-view=channels]")
    assert page.is_visible("#view-channels") and hash_of(page) == "#channels"
    page.click(".tab[data-view=settings]")
    assert page.input_value("#password-form [name=current]") == ""
    assert page.errors == []


def test_narrow_screen_all_tabs_reachable(browser_site):
    page = open_page(browser_site, width=390, hash_="#settings/record")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    for part in PARTS:
        box = page.locator(f"#stab-{part}").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 390, part
        page.click(f"#stab-{part}")
        assert visible_part(page) == part
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_export_strict_confirmation_and_notice(browser_site):
    """"더 가리기"는 기본 꺼짐. 켤 때 한 번 묻고 취소하면 꺼진 채로 둔다. 끌 때는 묻지 않는다. 무엇이 들어가는지 늘 보인다."""
    page = open_page(browser_site, hash_="#settings/general")
    info = page.inner_text("#export-info")
    assert page.is_visible("#export-info")
    for text in ("채널 이름·방송 제목·파일 이름이 들어갑니다", "쿠키·비밀번호·토큰·접속 주소는 들어가지 않습니다", "docs/LOG-EXPORT.md"):
        assert text in info, text
    assert not page.is_checked("#export-strict")
    page.answer = False
    page.click("#export-strict")
    assert len(page.dialogs) == 1 and "문제 분석이 어려워" in page.dialogs[0] and not page.is_checked("#export-strict")
    page.answer = True
    page.click("#export-strict")
    assert len(page.dialogs) == 2 and page.is_checked("#export-strict")
    page.click("#export-strict")  # 끌 때는 묻지 않는다
    assert len(page.dialogs) == 2 and not page.is_checked("#export-strict")
    # 보내는 값: 기본은 strict false, 켜면 true
    bodies = []
    page.on("request", lambda r: bodies.append(json.loads(r.post_data)) if r.url.endswith("/api/logs/export") else None)
    with page.expect_download() as dl:
        page.click("#export-button")
    assert re.fullmatch(r"vodrecorder-logs-\d{8}-\d{6}\.zip", dl.value.suggested_filename)
    page.click("#export-strict")
    with page.expect_download():
        page.click("#export-button")
    assert bodies == [{"range": "3", "strict": False}, {"range": "3", "strict": True}]
    assert page.errors == []


def test_display_and_filename_formats_follow_settings(browser_site):
    """화면 모양을 바꾸면 기록 목록의 시각이 바뀌고(새로고침 뒤에도), 파일 이름 예시는 고른 모양을 따른다."""
    from datetime import datetime, timedelta, timezone

    from conftest import CHANNEL

    store = browser_site["store"]
    store.add_channel(CHANNEL, "시각채널", "ch1")
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    sid = store.create_session(channel_id=CHANNEL, title="시각 방송", started_at=started.isoformat())
    store.update_session(sid, ended_at=(started + timedelta(minutes=30)).isoformat(), end_reason="broadcast_end", bytes=1000)
    page = open_page(browser_site)
    page.click(".tab[data-view=history]")
    page.wait_for_selector("#session-list .s-time")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", page.inner_text("#session-list .s-time"))
    # 파일 이름 예시(녹화 탭)
    page.click(".tab[data-view=settings]")
    page.click("#stab-record")
    until(page, "document.getElementById('filename-example').textContent.length > 0")
    page.select_option("#settings-form [name=filename_date_format]", "YYMMDD")
    page.select_option("#settings-form [name=filename_time_format]", "HH시MM분SS초")
    until(page, "/^\\[\\d{6} \\d{2}시\\d{2}분\\d{2}초\\]/.test(document.getElementById('filename-example').textContent)")
    page.fill("#settings-form [name=filename_pattern]", "{없는토큰}")
    until(page, "document.getElementById('filename-example').textContent.includes('모르는 토큰')")
    page.answer = True  # 녹화 탭에 고친 것을 버리고 옮긴다
    # 화면 모양(일반 탭)
    page.click("#stab-general")
    page.select_option("#settings-general-form [name=display_date_format]", "M월 D일")
    page.select_option("#settings-general-form [name=display_time_format]", "오전/오후 H:MM")
    assert re.search(r"\d+월 \d+일 (오전|오후) \d+:\d{2}", page.inner_text("#display-example"))
    page.click("#settings-general-form button[type=submit]")
    page.wait_for_selector("#toast:has-text('설정을 저장')")
    page.click(".tab[data-view=history]")
    until(page, "/^\\d+월 \\d+일 (오전|오후) \\d+:\\d{2}$/.test((document.querySelector('#session-list .s-time') || {}).textContent || '')")
    assert re.fullmatch(r"\d+월 \d+일 (오전|오후) \d+:\d{2}", page.inner_text("#session-list .s-time"))
    page.reload()
    page.wait_for_selector("#session-list .s-time")
    assert re.fullmatch(r"\d+월 \d+일 (오전|오후) \d+:\d{2}", page.inner_text("#session-list .s-time"))
    assert store.get_settings().display_time_format == "오전/오후 H:MM"
    assert store.get_settings().filename_time_format == "HHhMMmSSs"  # 녹화 탭은 저장하지 않았다
    assert page.errors == []


def test_token_table_inserts_at_caret_and_fits_narrow_screen(browser_site):
    """표는 펼친 채로 열리고 접을 수 있다. 토큰을 누르면 커서 자리에 들어가고 저장하지 않은 변경이 된다."""
    page = open_page(browser_site, hash_="#settings/record")
    until(page, "document.querySelectorAll('#token-rows .token-row').length === 11")  # {platform}을 더해 11개
    assert page.evaluate("document.getElementById('token-help').open") is True
    tokens = page.eval_on_selector_all("#token-rows .token-insert", "els => els.map(e => e.textContent)")
    assert "{date}" in tokens and "{time}" in tokens and "{legacy_time}" not in tokens
    page.fill("#settings-form [name=filename_pattern]", "AB")
    page.evaluate("() => { const i = document.querySelector('#settings-form [name=filename_pattern]'); i.focus(); i.setSelectionRange(1, 1); }")
    page.click("#token-rows .token-insert >> text='{channel_name}'")
    assert page.input_value("#settings-form [name=filename_pattern]") == "A{channel_name}B"
    until(page, "document.getElementById('filename-example').textContent.startsWith('A채널 이름B')")
    # 저장하지 않은 변경이므로 다른 탭으로 갈 때 묻는다
    page.answer = False
    page.click("#stab-general")
    assert page.dialogs and visible_part(page) == "record"
    # 모양을 바꾸면 예시 값이 바뀐다
    page.select_option("#settings-form [name=filename_time_format]", "HH-MM")
    until(page, "/^\\d{2}-\\d{2}$/.test([...document.querySelectorAll('#token-rows .token-row')][1].lastElementChild.textContent)")
    page.click("#token-help > summary")
    assert page.evaluate("document.getElementById('token-help').open") is False
    page.click("#token-help > summary")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    right = page.evaluate("Math.max(...[...document.querySelectorAll('#token-help *')].map(e => e.getBoundingClientRect().right))")
    assert right <= 390
    assert page.errors == []


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_theme_button_shows_icons(browser_site, scheme):
    """색 구성 단추는 글자 대신 그림(자동: 반씩 나눈 달·해와 A, 밝게: 해, 어둡게: 달)을 보이고, 지금 상태는
    접근성 이름과 풍선 도움말로 둔다. 동작·순서(자동 → 밝게 → 어둡게)와 저장 방식은 그대로다.
    LR_CAPTURE_DIR가 있으면 상태마다 단추 캡처를 남긴다."""
    page = open_page(browser_site)
    page.emulate_media(color_scheme=scheme)
    capture = os.environ.get("LR_CAPTURE_DIR")
    seen = []
    for expected in ("auto", "light", "dark"):
        state = page.get_attribute("#theme", "data-state")
        label = page.get_attribute("#theme", "aria-label")
        visible = [i for i in ("auto", "light", "dark") if page.is_visible(f"#theme .theme-icon[data-icon={i}]")]
        seen.append((state, label, page.get_attribute("#theme", "title"), visible, page.inner_text("#theme").strip()))
        if capture:
            Path(capture).mkdir(parents=True, exist_ok=True)
            page.locator(".topbar, header").first.screenshot(path=str(Path(capture) / f"theme-{scheme}-{expected}.png"))
        page.click("#theme")
    labels = {"auto": "색: 자동", "light": "색: 밝게", "dark": "색: 어둡게"}
    assert [s[0] for s in seen] == ["auto", "light", "dark"]
    for state, label, title, visible, text in seen:
        assert label == title == labels[state] and visible == [state] and text == "", (state, label, visible, text)
    assert page.evaluate("localStorage.getItem('lr-theme')") is None  # 어둡게 다음은 다시 자동(저장을 지운다)
    box = page.locator("#theme .theme-icon[data-icon=auto]").bounding_box()
    assert box and box["width"] >= 16 and box["height"] >= 16
    assert page.errors == []
