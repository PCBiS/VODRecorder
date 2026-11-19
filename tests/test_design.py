"""화면 모양새. 색 변수는 한곳에, 두 구성의 글자 대비 4.5:1 이상, 바깥 자원 없음, 움직임 줄이기."""

import re
from pathlib import Path

from live_recorder import web

UI = Path(web.UI_DIR)
CSS = (UI / "style.css").read_text(encoding="utf-8")


def block(selector: str, nested: bool = False) -> dict[str, str]:
    """선택자 블록의 변수들."""
    start = CSS.index(selector + " {")
    body = CSS[start + len(selector) + 2:CSS.index("}", start)]
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body))


LIGHT = block(":root")
DARK = block(':root[data-theme="dark"]')
AUTO_DARK = block('  :root:not([data-theme="light"])')
FORCED_LIGHT = block(':root[data-theme="light"]')


def lum(hex_: str) -> float:
    h = hex_.strip().lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a: str, b: str) -> float:
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# (글자, 바탕): 본문, 흐린 글자, 강조색 글자, 강조 단추 글자, 경고·오류 글자, 선택한 탭, 채널 카드 위 글자, 상태 배지
PAIRS = [("--ink", "--surface"), ("--ink", "--bg"), ("--ink", "--btn-bg"), ("--ink", "--btn-hover"), ("--danger", "--btn-bg"), ("--muted", "--surface"), ("--muted", "--bg"), ("--muted", "--idle-bg"),
         ("--muted", "--field"), ("--ink", "--field"),
         ("--accent", "--surface"), ("--accent", "--bg"), ("--accent", "--ok-bg"), ("--accent-ink", "--accent"),
         ("--danger", "--surface"), ("--danger-ink", "--danger"), ("--warn", "--warn-bg"), ("--rec", "--rec-bg"),
         ("--ink", "--live-bg"), ("--ink", "--idle-y-bg"), ("--ink", "--err-bg"), ("--muted", "--live-bg"),
         ("--muted", "--idle-y-bg"), ("--muted", "--err-bg"), ("--live", "--live-bg"), ("--idle-y", "--idle-y-bg"),
         ("--err", "--err-bg"), ("--danger", "--err-bg")]


def test_palette_is_defined_once_per_scheme_and_both_dark_blocks_match():
    assert AUTO_DARK == DARK, "브라우저 설정의 어두운 구성과 화면에서 고른 어두운 구성은 같아야 한다"
    assert {k: v for k, v in LIGHT.items() if k in FORCED_LIGHT} == FORCED_LIGHT
    assert set(LIGHT) >= set(DARK) and set(DARK) >= {k for k in FORCED_LIGHT}
    for name in ("--radius-card", "--radius-input", "--radius-pill", "--font", "--gutter", "--fast", "--accent", "--topbar-bg"):
        assert name in LIGHT, name
    assert LIGHT["--font"].startswith('-apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, "Apple SD Gothic Neo"')
    # 색 이름(#값)은 변수 정의 밖에 거의 쓰지 않는다. 남은 것은 카드 위 상태 점의 고정 색뿐이다
    rest = CSS[CSS.index("* { box-sizing: border-box; }"):]
    stray = set(re.findall(r"#[0-9a-fA-F]{3,6}\b", rest))
    assert stray <= {"#f2c94c", "#e5484d", "#ff8a80", "#fff"}, stray


def test_text_contrast_is_at_least_4_5_in_both_schemes():
    measured = {}
    for scheme, palette in (("light", LIGHT), ("dark", DARK)):
        for fg, bg in PAIRS:
            r = ratio(palette[fg], palette[bg])
            measured[(scheme, fg, bg)] = round(r, 2)
            assert r >= 4.5, (scheme, fg, bg, round(r, 2))
    # 보고용: 가장 낮은 값
    assert min(measured.values()) >= 4.5


def test_keeps_status_colours_focus_motion_and_no_external_assets():
    # 채널 카드 상태 색과 막대 구분은 그대로 변수로
    for health, var in (("recording", "--live"), ("idle", "--idle-y"), ("error", "--err")):
        assert f'.card-grid .card[data-health="{health}"] {{ border-color: var({var}); background: var({var}-bg); }}' in CSS
    for kind in ("normal", "split", "live"):
        assert f".tl-bar.{kind}" in CSS
    assert ":focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }" in CSS  # 키보드 초점
    assert "@media (prefers-reduced-motion: reduce)" in CSS and "transition-duration: 0s !important" in CSS
    assert "backdrop-filter" in CSS and "@supports" in CSS  # 흐림이 없으면 단색
    assert "[hidden] { display: none !important; }" in CSS
    assert "@import" not in CSS and "url(" not in CSS and "@font-face" not in CSS  # 바깥 글꼴·그림 없음
    for page in ("index.html", "login.html", "setup.html"):
        html = (UI / page).read_text(encoding="utf-8")
        assert '<link rel="stylesheet" href="/static/style.css">' in html  # 로그인·첫 설정도 같은 모양
        assert not re.search(r'(?:src|href)="(?:https?:)?//', html)
