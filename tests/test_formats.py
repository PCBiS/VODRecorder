"""날짜·시각 모양: 파일 이름과 화면."""

import asyncio
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_recorder.core import naming
from live_recorder import web
from live_recorder.core import store as st
from live_recorder.core.config import DISPLAY_DATE_SHAPES, DISPLAY_TIME_SHAPES, ConfigError, Settings

from conftest import CHANNEL
from test_view_only import ViewSite
from webui_files import index_html, all_js

UI = Path(web.UI_DIR)
KST = naming.KST
AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=KST)  # 한 자리 월·일·시를 일부러 고른 시각
EXAMPLE = "/api/settings/filename-example"

FILE_DATES = {"YYYY-MM-DD": "2026-01-02", "YYYY.MM.DD": "2026.01.02", "YYYY_MM_DD": "2026_01_02", "YYYYMMDD": "20260102",
              "YY-MM-DD": "26-01-02", "YY.MM.DD": "26.01.02", "YYMMDD": "260102"}
FILE_TIMES = {"HHhMMmSSs": "03h04m05s", "HH-MM-SS": "03-04-05", "HH.MM.SS": "03.04.05", "HHMMSS": "030405",
              "HH시MM분SS초": "03시04분05초", "HHhMMm": "03h04m", "HH-MM": "03-04"}


def render(pattern, now=AT, **shapes):
    return naming.render(pattern, title="제목", channel_name="채널", quality="1080p", frame_rate="60", extension=".mp4",
                         broadcast_start=now - timedelta(hours=1), recording_start=now, now=now, **shapes)


# ---- 파일 이름 ----

def test_every_file_shape_for_a_fixed_instant():
    assert list(naming.DATE_SHAPES) == list(FILE_DATES) and list(naming.TIME_SHAPES) == list(FILE_TIMES)  # 정해진 목록 그대로
    for d, want_d in FILE_DATES.items():
        for t, want_t in FILE_TIMES.items():
            assert render("{date} {time}#{date}#{time}{file_extension}", date_shape=d, time_shape=t) == \
                f"{want_d} {want_t}#{want_d}#{want_t}.mp4", (d, t)
    for shape in list(FILE_DATES) + list(FILE_TIMES):
        assert "/" not in shape and ":" not in shape  # 파일 이름에 쓸 수 없는 글자는 목록에 없다
    # 늘 24시간제, KST. 자정·정오, UTC로 주어도 한국 시각
    midnight = datetime(2026, 11, 12, 15, 0, 0, tzinfo=timezone.utc)  # KST 자정
    noon = datetime(2026, 11, 13, 12, 0, 0, tzinfo=KST)
    assert render("{date} {time}", now=midnight, date_shape="YYMMDD", time_shape="HH-MM-SS") == "261113 00-00-00.mp4"
    assert render("{date} {time}", now=noon, time_shape="HH시MM분SS초") == "2026-11-13 12시00분00초.mp4"
    assert render("{date} {time}", now=datetime(2026, 11, 13, 23, 59, 59, tzinfo=KST), time_shape="HHhMMm") == "2026-11-13 23h59m.mp4"


def test_default_pattern_gives_yyyy_mm_dd_hhhmmmsss():
    """기본 패턴·기본 모양에서 `{date} {time}`은 `YYYY-MM-DD HHhMMmSSs`(`%Y-%m-%d %Hh%Mm%Ss`)다. 다른 시각 토큰은 고정된 모양 그대로다."""
    pattern = Settings().filename_pattern
    for now in (AT, datetime(2026, 11, 11, 19, 3, 27, tzinfo=KST), datetime(2026, 12, 31, 23, 59, 59, tzinfo=KST)):
        old = now.astimezone(KST).strftime("%Y-%m-%d %Hh%Mm%Ss")
        assert render(pattern, now=now) == render(pattern, now=now, date_shape="YYYY-MM-DD", time_shape="HHhMMmSSs") == \
            f"[{old}] 채널 제목 1080p60.mp4"
    assert render("{date} {time}", now=datetime(2026, 11, 11, 19, 3, 27, tzinfo=KST)) == "2026-11-11 19h03m27s.mp4"
    fixed = render("{recording_time} {broadcast_time} {start_time}", date_shape="YYMMDD", time_shape="HH-MM")
    assert fixed == "260102_030405 260102_020405 2026-01-02.mp4"  # 그대로


def test_unknown_shapes_are_rejected():
    for key, bad in (("filename_date_format", "YYYY/MM/DD"), ("filename_date_format", "%Y"), ("filename_time_format", "HH:MM:SS"),
                     ("filename_time_format", ""), ("display_date_format", "DD/MM/YYYY"), ("display_time_format", "h:mm a"),
                     ("display_date_format", 3)):
        with pytest.raises(ConfigError):
            Settings.validate(key, bad)
    with pytest.raises(ValueError):
        render("{date}", date_shape="nope")
    naming.validate_pattern("{date}_{time}{file_extension}")
    assert Settings.validate("display_time_format", "오전/오후 H:MM") == "오전/오후 H:MM"


def test_previous_build_settings_mean_defaults(tmp_path):
    """이전 빌드의 설정 저장소에는 새 키가 없다. 없으면 기본 모양이다."""
    store = st.Store(tmp_path / "recorder.sqlite3", tmp_path / "rec", tmp_path / "telegram.json")
    store.set_settings({"rewind_seconds": 60, "filename_pattern": "[{date} {time}] {channel_name}{file_extension}"})
    store.close()
    db = sqlite3.connect(tmp_path / "recorder.sqlite3")
    keys = {r[0] for r in db.execute("SELECT key FROM settings")}
    db.execute("DELETE FROM settings WHERE key IN ('filename_date_format','filename_time_format','display_date_format','display_time_format')")
    db.commit()
    db.close()
    assert not keys & {"filename_date_format", "display_time_format"}  # 새 키를 저장해 두지 않는다
    store = st.Store(tmp_path / "recorder.sqlite3", tmp_path / "rec", tmp_path / "telegram.json")
    try:
        s = store.get_settings()
        assert (s.filename_date_format, s.filename_time_format, s.display_date_format, s.display_time_format) == \
            ("YYYY-MM-DD", "HHhMMmSSs", "YYYY-MM-DD", "HH:MM")
        assert s.rewind_seconds == 60
        s = store.set_settings({"filename_time_format": "HH-MM"})
        assert s.filename_time_format == "HH-MM" and s.filename_date_format == "YYYY-MM-DD"
    finally:
        store.close()


def test_recording_uses_the_chosen_shapes(harness, server):
    """저장한 모양은 다음에 만드는 녹화 파일 이름에 쓰인다."""
    server.go_live(title="모양 방송")

    async def scenario():
        async with harness:
            harness.store.set_settings({"filename_pattern": "{date}_{time} {channel_name}", "rewind_seconds": 0,
                                        "filename_date_format": "YYMMDD", "filename_time_format": "HH시MM분SS초"})
            harness.add_channel()
            await harness.until(lambda: harness.sessions() and harness.recording(), what="녹화")

    asyncio.run(scenario())
    name = Path(harness.sessions()[0]["final_path"]).name
    assert re.fullmatch(r"\d{6}_\d{2}시\d{2}분\d{2}초 시험채널\.mp4", name), name


def test_example_endpoint(site_login):
    """서버가 실제 규칙으로 예시 이름을 만든다. 저장하지 않는다. 로그인이 필요하다."""
    site = site_login
    r = site.client.get(EXAMPLE, params={"pattern": "[{date} {time}] {channel_name} {date}_{time}{file_extension}",
                                         "date_format": "YY.MM.DD", "time_format": "HH-MM"})
    assert r.status_code == 200, r.text
    assert re.fullmatch(r"\[\d{2}\.\d{2}\.\d{2} \d{2}-\d{2}\] 채널 이름 \d{2}\.\d{2}\.\d{2}_\d{2}-\d{2}\.mp4", r.json()["example"]), r.json()
    for bad in ({"pattern": "{nope}", "date_format": "YYMMDD", "time_format": "HH-MM"},
                {"pattern": "{date}", "date_format": "YYYY/MM/DD", "time_format": "HH-MM"},
                {"pattern": "{date}", "date_format": "YYMMDD", "time_format": "HH:MM"}):
        r = site.client.get(EXAMPLE, params=bad)
        assert r.status_code == 400 and r.json()["error"], bad
    assert site.client.get(EXAMPLE).status_code in (400, 422)
    assert site.h.store.get_settings().filename_time_format == "HHhMMmSSs"  # 저장하지 않았다
    # 설정 저장: 목록에 없는 값은 거부한다
    assert site.call("PATCH", "/api/settings", json={"filename_time_format": "HH:MM"}).status_code == 400
    assert site.call("PATCH", "/api/settings", json={"display_date_format": "YYYY년"}).status_code == 400
    r = site.call("PATCH", "/api/settings", json={"filename_time_format": "HHMMSS", "display_time_format": "오전/오후 H:MM"})
    assert r.status_code == 200 and r.json()["settings"]["filename_time_format"] == "HHMMSS"
    # 화면 모양은 첫 요청(/api/mode)에 함께 온다(예전 모양이 잠깐 보이지 않게)
    assert site.client.get("/api/mode").json()["display"] == {"date": "YYYY-MM-DD", "time": "오전/오후 H:MM"}
    assert web.access_level("GET", EXAMPLE) == web.LOGIN and ("GET", EXAMPLE, web.LOGIN) in web.ACCESS_RULES
    site.client.post("/api/logout", headers={"X-CSRF-Token": site.csrf})
    assert site.client.get(EXAMPLE, params={"pattern": "{date}", "date_format": "YYMMDD", "time_format": "HH-MM"}).status_code == 401


def test_example_endpoint_refused_in_view_only(tmp_path, server, tools):
    with ViewSite(tmp_path, server, tools) as v:
        r = v.client.get(EXAMPLE, params={"pattern": "{date}", "date_format": "YYMMDD", "time_format": "HH-MM"})
        assert r.status_code == 403 and r.json()["view_only"] is True
        assert v.client.get("/api/mode").json()["display"] == {"date": "YYYY-MM-DD", "time": "HH:MM"}


@pytest.fixture
def site_login(tmp_path, server, tools):
    from test_web import Web

    with Web(tmp_path, server, tools) as w:
        w.login()
        yield w


# ---- 화면 ----

def run_format(cases):
    """app.js의 formatWhen을 Node에서 돌린다."""
    js = all_js()
    start = js.index("const DISPLAY_DATE_SHAPES")
    end = js.index("\n}\n", js.index("function formatWhen(")) + 3
    script = js[start:end] + f"\nconst out = {json.dumps(cases)}.map(([v, o]) => formatWhen(v, o));\nconsole.log(JSON.stringify(out));"
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_every_display_shape_for_a_fixed_instant():
    js = all_js()
    assert f"const DISPLAY_DATE_SHAPES = {json.dumps(list(DISPLAY_DATE_SHAPES), ensure_ascii=False)};" in js
    assert f"const DISPLAY_TIME_SHAPES = {json.dumps(list(DISPLAY_TIME_SHAPES), ensure_ascii=False)};" in js
    iso = "2026-01-01T18:04:05Z"  # KST 2026-01-02 03:04:05
    dates = {"YYYY-MM-DD": "2026-01-02", "YYYY.MM.DD": "2026.01.02", "YYYY/MM/DD": "2026/01/02", "YY.MM.DD": "26.01.02",
             "MM/DD": "01/02", "M월 D일": "1월 2일"}
    times = {"HH:MM": ("03:04", "03:04:05"), "HH:MM:SS": ("03:04:05", "03:04:05"), "오전/오후 H:MM": ("오전 3:04", "오전 3:04:05")}
    cases, want = [], []
    for d, dv in dates.items():
        for t, (tv, tsv) in times.items():
            shapes = {"date": d, "time": t}
            cases += [[iso, {"shapes": shapes}], [iso, {"shapes": shapes, "seconds": True}], [iso, {"shapes": shapes, "time": False}],
                      [iso, {"shapes": shapes, "date": False}]]
            want += [f"{dv} {tv}", f"{dv} {tsv}", dv, tv]
    ampm = {"date": "M월 D일", "time": "오전/오후 H:MM"}
    edge = [["2026-11-12T15:00:00Z", {"shapes": ampm}],  # KST 자정
            ["2026-11-13T03:00:00Z", {"shapes": ampm}],  # 정오
            ["2026-11-13T03:59:09Z", {"shapes": ampm, "seconds": True}],
            ["2026-11-13T14:59:59Z", {"shapes": ampm}],  # 23:59
            ["2026-11-12T15:00:00Z", {"shapes": {"date": "YYYY-MM-DD", "time": "HH:MM"}}],
            [1762963200000, {"shapes": {"date": "YYYY-MM-DD", "time": "HH:MM"}}],  # 밀리초
            ["2026-11-13 09:30:00", {"shapes": {"date": "YYYY-MM-DD", "time": "HH:MM"}}],  # 시간대 없는 한국 시각(게시 날짜)
            ["", {}], [None, {}], ["nope", {}]]
    want += ["11월 13일 오전 12:00", "11월 13일 오후 12:00", "11월 13일 오후 12:59:09", "11월 13일 오후 11:59", "2026-11-13 00:00",
             "2025-11-13 01:00", "2026-11-13 09:30", "", "", ""]
    assert run_format(cases + edge) == want


def test_no_other_date_formatting_in_screen_code():
    """날짜·시각 글자는 formatWhen 한 곳에서만 만든다. 타임라인 축 눈금(kstParts)만 짧은 고정 모양이다."""
    js = all_js()
    assert "toLocale" not in js and "Intl.DateTimeFormat" not in js
    for local in (".getHours(", ".getMinutes(", ".getSeconds(", ".getMonth(", ".getDate(", ".getDay(", ".getFullYear("):
        assert local not in js, local  # 브라우저의 시간대를 따르는 값은 쓰지 않는다
    body = js[js.index("function formatWhen("):js.index("\n}\n", js.index("function formatWhen("))]
    axis = js[js.index("function kstParts("):js.index("\n}\n", js.index("function kstParts("))]
    rest = js.replace(body, "").replace(axis, "")
    assert not re.search(r"getUTC(Hours|Minutes|Seconds|Month|Date|Day|FullYear)\(", rest)
    assert js.count("kstParts(") == 3  # 정의, 짧은 날짜 눈금(dayLabel), 주 선택의 연·월(rowDate. 글자를 만들지 않는다)
    for name in ("localTime(", "localDate(", "toISOString().slice"):
        assert name not in js, name
    for other in ("login.js", "setup.js", "boot.js"):
        assert "toLocale" not in (UI / other).read_text(encoding="utf-8")


# ---- 패턴에 직접 적은 금지 글자 ----

BAD_PATTERNS = ["{channel_name}/{safe_live_title}", "..\\{channel_name}", "{date}:{time}", "{channel_name}\n{date}",
                "a|b{date}", "{date}\x07", '"{date}"', "<{date}>", "{date}*", "{date}?"]


def test_literal_forbidden_characters_are_rejected_on_save(site_login):
    site = site_login
    for bad in BAD_PATTERNS:
        with pytest.raises(ValueError):
            naming.validate_pattern(bad)
        r = site.call("PATCH", "/api/settings", json={"filename_pattern": bad})
        assert r.status_code == 400 and "쓸 수 없는 글자" in r.json()["error"], bad
        r = site.client.get(EXAMPLE, params={"pattern": bad, "date_format": "YYMMDD", "time_format": "HH-MM"})
        assert r.status_code == 400 and "쓸 수 없는 글자" in r.json()["error"], bad
    # 토큰 값의 금지 글자는 예전처럼 바꾸고, 토큰 밖의 `+`·공백·괄호·점은 쓸 수 있다
    for good in ("[{date} {time}] {channel_name} {safe_live_title}{file_extension}", "{date}+{time} ({channel_name}).{record_quality}"):
        naming.validate_pattern(good)
        assert site.call("PATCH", "/api/settings", json={"filename_pattern": good}).status_code == 200, good


def test_stored_bad_pattern_cannot_leave_the_channel_folder(tmp_path):
    """이전 빌드가 저장해 둔 패턴은 읽을 때 거부하지 않고, 파일명을 만들 때 금지 글자를 `_`로 바꾼다."""
    store = st.Store(tmp_path / "recorder.sqlite3", tmp_path / "rec", tmp_path / "telegram.json")
    store.close()
    db = sqlite3.connect(tmp_path / "recorder.sqlite3")
    bad = "../../밖/{channel_name}\\..\\x:{safe_live_title}\n|{date}"
    db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", ("filename_pattern", json.dumps(bad)))
    db.commit()
    db.close()
    store = st.Store(tmp_path / "recorder.sqlite3", tmp_path / "rec", tmp_path / "telegram.json")
    try:
        pattern = store.get_settings().filename_pattern
    finally:
        store.close()
    assert pattern == bad
    folder = tmp_path / "rec" / "ch1"
    folder.mkdir(parents=True)
    name = render(pattern, date_shape="YYMMDD")
    for ch in '/\\:\n|':
        assert ch not in name, (ch, name)
    final = naming.unique_path(folder, name)
    assert final.parent == folder and final.resolve().is_relative_to(folder.resolve())
    assert name == "밖_채널_.._x_제목__260102.mp4"


# ---- 저장된 패턴에 모르는 토큰 ----

DEFAULT = "[{date} {time}] {channel_name} {safe_live_title} {record_quality}{frame_rate}{file_extension}"
UNKNOWN = "[{legacy_time}] {channel_name} {safe_live_title}{file_extension}"  # 모르는 토큰의 예(옛 저장값)


def _store_with_pattern(path, value):
    store = st.Store(path, path.parent / "rec", path.parent / "telegram.json")
    store.close()
    db = sqlite3.connect(path)
    db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('filename_pattern', ?)", (json.dumps(value),))
    db.commit()
    db.close()


def test_unknown_token_is_rejected_like_any_other():
    with pytest.raises(ValueError):
        naming.validate_pattern(UNKNOWN)
    with pytest.raises(ConfigError):
        Settings.validate("filename_pattern", UNKNOWN)
    assert Settings().filename_pattern == DEFAULT


def test_stored_unknown_token_uses_default_and_warns_once(tmp_path, caplog):
    path = tmp_path / "recorder.sqlite3"
    _store_with_pattern(path, UNKNOWN)
    with caplog.at_level("WARNING", logger="live_recorder.store"):
        store = st.Store(path, tmp_path / "rec", tmp_path / "telegram.json")
    try:
        assert store.get_settings().filename_pattern == DEFAULT  # 서버를 멈추지 않고 기본 패턴
        stored, reason = store.filename_pattern_problem()
        assert stored == UNKNOWN and "모르는 토큰" in reason
        warned = [r for r in caplog.records if getattr(r, "event", None) == "filename_pattern_invalid"]
        assert len(warned) == 1  # 같은 값에는 한 번만
    finally:
        store.close()
    assert json.loads(sqlite3.connect(path).execute("SELECT value FROM settings WHERE key='filename_pattern'").fetchone()[0]) == UNKNOWN


def test_recording_with_stored_unknown_token_uses_default_pattern(harness, server):
    server.go_live(title="기본 패턴")
    _store_with_pattern(harness.cfg.db_path, UNKNOWN)

    async def scenario():
        async with harness:
            harness.store.set_settings({"rewind_seconds": 0})
            harness.add_channel()
            await harness.until(lambda: harness.sessions() and harness.recording(), what="녹화")

    asyncio.run(scenario())
    name = Path(harness.sessions()[0]["final_path"]).name
    assert re.fullmatch(r"\[\d{4}-\d{2}-\d{2} \d{2}h\d{2}m\d{2}s\] 시험채널 기본 패턴 .*\.mp4", name), name


def test_settings_show_notice_and_valid_save_clears_it(tmp_path, server, tools):
    from test_web import Web

    w = Web(tmp_path, server, tools)
    _store_with_pattern(w.h.cfg.db_path, UNKNOWN)
    with w:
        w.login()
        data = w.client.get("/api/settings").json()
        assert data["settings"]["filename_pattern"] == DEFAULT
        assert data["filename_pattern_problem"]["stored"] == UNKNOWN and "모르는 토큰" in data["filename_pattern_problem"]["reason"]
        # 다른 키를 저장해도 저장된 값은 그대로이고 안내도 남는다
        r = w.call("PATCH", "/api/settings", json={"rewind_seconds": 30})
        assert r.status_code == 200 and r.json()["filename_pattern_problem"]["stored"] == UNKNOWN
        # 모르는 토큰은 저장할 때 거부한다
        assert w.call("PATCH", "/api/settings", json={"filename_pattern": UNKNOWN}).status_code == 400
        r = w.call("PATCH", "/api/settings", json={"filename_pattern": DEFAULT})
        assert r.status_code == 200 and r.json()["filename_pattern_problem"] is None
    js = all_js()
    assert "모르는 토큰이 있어 기본 패턴을 쓰고 있습니다" in js


TOKENS_URL = "/api/settings/filename-tokens"


def test_token_table_comes_from_the_accepted_token_list(site_login):
    """표의 토큰은 서버가 받아 주는 토큰과 같다. {legacy_time}은 없다. 예시 값은 고른 모양을 따른다."""
    site = site_login
    r = site.client.get(TOKENS_URL, params={"date_format": "YYMMDD", "time_format": "HH시MM분SS초"})
    assert r.status_code == 200, r.text
    rows = r.json()["tokens"]
    assert [t["token"] for t in rows] == [name for name, _ in naming.TOKEN_HELP]
    assert {t["token"] for t in rows} == set(naming.TOKENS) and "legacy_time" not in {t["token"] for t in rows}
    for name in naming.TOKENS:
        naming.validate_pattern("{" + name + "}")  # 표의 모든 토큰이 받아들여진다
    ex = {t["token"]: t["example"] for t in rows}
    assert re.fullmatch(r"\d{6}", ex["date"]) and re.fullmatch(r"\d{2}시\d{2}분\d{2}초", ex["time"])
    assert re.fullmatch(r"\d{6}_\d{6}", ex["recording_time"]) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ex["start_time"])
    assert ex["safe_live_title"] == "방송 제목_ 예시" and ex["file_extension"] == ".mp4" and ex["record_quality"] == "1080p"
    assert all(t["meaning"] for t in rows)
    assert site.client.get(TOKENS_URL, params={"date_format": "YYYY/MM/DD", "time_format": "HH-MM"}).status_code == 400
    assert web.access_level("GET", TOKENS_URL) == web.LOGIN and ("GET", TOKENS_URL, web.LOGIN) in web.ACCESS_RULES
    # 화면은 목록을 따로 적어 두지 않는다(서버에서 받는다). 화면과 도움말에 {legacy_time}이 없다
    html = index_html()
    js = all_js()
    assert "legacy_time" not in html and "legacy_time" not in js and "/api/settings/filename-tokens?" in js
    for name in ("recording_time", "broadcast_time", "safe_live_title", "record_quality", "frame_rate"):
        assert name not in html and f'"{name}"' not in js
    site.client.post("/api/logout", headers={"X-CSRF-Token": site.csrf})
    assert site.client.get(TOKENS_URL, params={"date_format": "YYMMDD", "time_format": "HH-MM"}).status_code == 401


def test_token_table_refused_in_view_only(tmp_path, server, tools):
    with ViewSite(tmp_path, server, tools) as v:
        r = v.client.get(TOKENS_URL, params={"date_format": "YYMMDD", "time_format": "HH-MM"})
        assert r.status_code == 403 and r.json()["view_only"] is True


def test_removed_token_name_appears_nowhere_in_product_or_docs():
    """지원하지 않는 토큰의 이름은 코드·화면·README·LOG-EXPORT.md 어디에도 없다(시험만 모르는 토큰의 예로 쓴다)."""
    root = UI.parents[1]
    files = [p for p in (root / "live_recorder").rglob("*") if p.is_file() and p.suffix in (".py", ".js", ".html", ".css")]
    files += [root / "README.md", root / "docs" / "LOG-EXPORT.md"]
    for path in files:
        assert "legacy_time" not in path.read_text(encoding="utf-8"), path
