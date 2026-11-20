"""나뉜 화면 스크립트를 실제 브라우저로 연다.

헤드리스 Chromium(Playwright)으로 여섯 화면을 차례로 열어 모두 뜨고 콘솔 오류가 없는지 본다.
가짜 확장을 켠 서버에서는 확장의 탭·설정 묶음·스크립트가 뼈대를 거쳐 뜨는지 본다. Playwright가 없으면 건너뛴다.
"""

import pytest

import fake_extension
from live_recorder.core import extensions
from test_settings_tabs import browser_site, open_page, until  # noqa: F401  (fixture)

VIEWS = ["channels", "history", "replay", "files", "cookies", "logs", "settings"]


def watch_console(page):
    page.console_errors = []
    page.bad_responses = []
    page.on("console", lambda m: page.console_errors.append(m.text) if m.type == "error" else None)
    page.on("response", lambda r: page.bad_responses.append(f"{r.status} {r.url}") if r.status >= 400 else None)


def visit_all(page):
    for view in VIEWS:
        page.click(f".tab[data-view={view}]")
        until(page, f"!document.getElementById('view-{view}').hidden")
        until(page, f"location.hash.startsWith('#{view}')")
        assert page.is_visible(f"#view-{view}"), view
        assert [v for v in VIEWS if page.is_visible(f"#view-{v}")] == [view]
    page.wait_for_timeout(500)


def enable_files(h):
    # 파일 관리자가 꺼져 있으면 파일 화면의 요청이 403(정해진 동작)이라 브라우저가 콘솔에 남긴다. 켜고 본다
    h.store.set_settings({"fm_enabled": True})


@pytest.mark.parametrize("browser_site", [enable_files], indirect=True)
def test_all_six_views_open_without_console_errors(browser_site):  # noqa: F811
    page = open_page(browser_site)
    watch_console(page)
    page.reload()
    page.wait_for_selector("#view-channels:not([hidden])")
    loaded = page.evaluate("[...document.scripts].map((s) => s.getAttribute('src'))")
    assert loaded.index("/static/boot.js") < loaded.index("/static/views/channels.js")
    assert [x for x in loaded if x.startswith(("/static/views/", "/static/ext/"))] == [
        "/static/views/channels.js", "/static/views/history.js", "/static/views/files.js", "/static/views/cookies.js",
        "/static/views/logs.js", "/static/views/settings.js", "/static/ext/downloads/downloads.js",
        "/static/ext/downloads/catalog.js"]  # 채널 목록 화면은 downloads의 공통 부분이 됐다(전에는 replay의 replay-list.js)
    visit_all(page)
    assert page.is_hidden("#boot-failed")
    assert page.errors == [] and page.console_errors == [], (page.errors, page.console_errors, page.bad_responses)


def enable_fake(h):
    enable_files(h)
    fake_extension.EXTENSION.reset()
    extensions.register(fake_extension.EXTENSION)
    h.store.set_extra({extensions.ENABLED_KEY: ["downloads", "replay", "clips", "fakeext"]})


@pytest.fixture
def unregister_fake():
    yield
    extensions.unregister("fakeext")


@pytest.mark.parametrize("browser_site", [enable_fake], indirect=True)
def test_fake_extension_tab_and_group_load_through_the_skeleton(unregister_fake, browser_site):  # noqa: F811
    page = open_page(browser_site)
    watch_console(page)
    page.reload()
    page.wait_for_selector("#view-channels:not([hidden])")
    visit_all(page)
    page.click(".tab[data-view=fakeext]")
    page.wait_for_selector("#view-fakeext:not([hidden])")
    until(page, "(document.getElementById('fakeext-result') || {}).textContent === '단계 3, 사건 ' + "
                "document.getElementById('fakeext-result').textContent.split('사건 ')[1]")
    assert page.text_content("#fakeext-result").startswith("단계 3, 사건 ")
    page.goto(browser_site["url"] + "/#settings/general")
    page.wait_for_selector("#spanel-general:not([hidden])")
    assert page.is_visible("#ext-group-fakeext") and page.text_content("#fakeext-group-text") == "가짜 확장 설정 묶음입니다"
    assert page.is_visible("#extensions-group")
    assert page.is_checked("#extensions-list input[name=fakeext]")
    assert page.errors == [] and page.console_errors == [], (page.errors, page.console_errors, page.bad_responses)


def combo(*names):
    def prepare(h):
        enable_files(h)
        h.store.set_extra({extensions.ENABLED_KEY: list(names)})
    prepare.names = names
    return prepare


COMBOS = [combo("downloads", "replay", "clips"), combo("downloads", "replay"), combo("downloads", "clips"), combo()]
# 켜진 작업 종류가 둘 이상이면 "다운로드"(전에는 "다시보기·클립")
TITLES = {("downloads", "replay", "clips"): "다운로드", ("downloads", "replay"): "다시보기", ("downloads", "clips"): "클립"}


@pytest.mark.parametrize("browser_site", COMBOS, indirect=True, ids=["all", "no_clips", "no_replay", "none"])
def test_each_extension_combination_opens_without_console_errors(browser_site, request):  # noqa: F811
    """조합마다 화면이 뜨고 콘솔 오류가 없다. 다시보기 탭과 제목은 켜진 것에 맞춘다(둘 다·다시보기만·클립만)."""
    names = request.node.callspec.params["browser_site"].names
    page = open_page(browser_site)
    watch_console(page)
    page.reload()
    page.wait_for_selector("#view-channels:not([hidden])")
    views = [v for v in VIEWS if v != "replay" or "downloads" in names]
    for view in views:
        page.click(f".tab[data-view={view}]")
        until(page, f"!document.getElementById('view-{view}').hidden")
    if "downloads" in names:
        page.click(".tab[data-view=replay]")
        until(page, f"document.getElementById('replay-title').textContent === {TITLES[names]!r}")
        assert page.text_content("#replay-tab-label") == TITLES[names]
        assert page.text_content("#stab-replay") == TITLES[names]  # 설정의 하위 탭 이름도 같은 규칙
        assert page.is_visible("#replay-mode-list") == ("replay" in names)
        if "replay" not in names:
            assert page.is_visible("#replay-url-pane")  # 주소로 받기만 남는다
            assert page.input_value("#analyze-form textarea") == "" and "clips" in page.get_attribute("#analyze-form textarea", "placeholder")
        page.goto(browser_site["url"] + "/#settings/replay")
        page.wait_for_selector("#spanel-replay:not([hidden])")
    else:
        assert page.locator(".tab[data-view=replay]").count() == 0 and page.locator("#stab-replay").count() == 0
    page.goto(browser_site["url"] + "/#settings/general")
    page.wait_for_selector("#spanel-general:not([hidden])")
    assert page.is_visible("#extensions-group")  # 들어 있는 세 확장이 늘 나온다
    assert page.locator("#extensions-list input").count() == 5  # 유튜브 영상, 씨미 다시보기·클립 확장이 더해졌다
    assert page.errors == [] and page.console_errors == [], (page.errors, page.console_errors, page.bad_responses)
