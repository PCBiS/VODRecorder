"""기록 화면의 주 이동 한계와 주 선택: 설치 시각, 기록 시작 한계, 주차 계산."""

import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live_recorder import web
from live_recorder.core import store as st

from conftest import CHANNEL
from test_web import Web, site  # noqa: F401  (fixture)

UI = Path(web.UI_DIR)


def _installed(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT value FROM meta WHERE key='installed_at'").fetchone()[0]
    finally:
        db.close()


def test_install_time_is_written_once(tmp_path):
    """새 저장소는 만든 시각을 적고, 다시 열어도 바꾸지 않는다."""
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    store = st.Store(tmp_path / "a.sqlite3", tmp_path)
    first = _installed(store.path)
    assert before <= datetime.fromisoformat(first) <= datetime.now(timezone.utc) + timedelta(seconds=1)
    assert store.history_start() == first
    store.close()
    store = st.Store(tmp_path / "a.sqlite3", tmp_path)
    assert _installed(store.path) == first
    store.close()


def test_install_time_backfilled_for_existing_store(tmp_path):
    """이 값이 없던 저장소는 가장 이른 채널 등록·녹화 시작 시각으로 채운다. 그보다 이른 녹화가 생기면 한계도 당겨진다."""
    store = st.Store(tmp_path / "b.sqlite3", tmp_path)
    store.add_channel(CHANNEL, "기록", "a", auto_record=False)
    store.create_session(channel_id=CHANNEL, started_at="2026-11-10T10:00:00.000+00:00", ended_at="2026-11-10T11:00:00.000+00:00")
    store.close()
    db = sqlite3.connect(tmp_path / "b.sqlite3")
    db.execute("DELETE FROM meta WHERE key='installed_at'")
    db.execute("UPDATE channels SET created_at='2026-11-05T01:02:03.000+00:00'")
    db.commit()
    db.close()
    store = st.Store(tmp_path / "b.sqlite3", tmp_path)
    assert _installed(store.path) == "2026-11-05T01:02:03.000+00:00" == store.history_start()
    store.create_session(channel_id=CHANNEL, started_at="2026-10-01T00:00:00.000+00:00", ended_at="2026-10-01T01:00:00.000+00:00")
    assert store.history_start() == "2026-10-01T00:00:00.000+00:00"
    assert _installed(store.path) == "2026-11-05T01:02:03.000+00:00"
    store.close()


def test_sessions_api_gives_earliest(site):
    site.login()
    start = site.h.store.history_start()
    for path in ("/api/sessions", "/api/view/sessions"):
        body = site.client.get(path).json()
        assert body["earliest"] == start and "sessions" in body and "disk" in body, path


WEEK_SCRIPT = r"""
const fs = require("fs"), vm = require("vm");
const ctx = { registerView() {}, formatWhen: () => "", Date, Math, Number, Set, Map, Array };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8") + ";this.out = { maxWeekOffset, weeksOfMonth, dayLabel };", ctx);
const { maxWeekOffset, weeksOfMonth, dayLabel } = ctx.out;
const t = (s) => Date.parse(s);
const now = t("2026-11-19T15:00:00+09:00");
const show = (y, m, e) => weeksOfMonth(y, m, now, e).map((w) => [w.offset, dayLabel(w.from), dayLabel(w.to), w.disabled]);
console.log(JSON.stringify({
  max: [maxWeekOffset(now, t("2026-11-15T10:32:00+09:00")), maxWeekOffset(now, null), maxWeekOffset(now, NaN),
        maxWeekOffset(now, t("2026-11-13T11:00:00+09:00")), maxWeekOffset(now, t("2026-11-13T13:00:00+09:00")),
        maxWeekOffset(now, t("2027-01-01T00:00:00+09:00"))],
  nov: show(2026, 11, t("2026-11-15T10:32:00+09:00")),
  oct: show(2026, 10, t("2026-09-20T00:00:00+09:00")),
}));
"""


def test_week_limit_and_month_weeks():
    """줄은 KST 정오 기준이다. 설치한 날의 줄이 든 주까지만 가고, 달의 주차 목록에서 그 앞 주는 고를 수 없다."""
    run = subprocess.run(["node", "-e", WEEK_SCRIPT, str(UI / "views" / "history.js")], capture_output=True, text=True, check=True)
    out = json.loads(run.stdout)
    # 이번 주 안에 설치했으면 이번 주뿐. 모르면 이번 주만. 13일 오전 11시는 12일 줄이라 한 주 더. 미래 값은 0
    assert out["max"] == [0, 0, 0, 1, 0, 0]
    assert out["nov"] == [[2, "10/30 (금)", "11/5 (목)", True], [1, "11/6 (금)", "11/12 (목)", True],
                          [0, "11/13 (금)", "11/19 (목)", False]]
    assert [w[0] for w in out["oct"]] == [7, 6, 5, 4, 3, 2] and not any(w[3] for w in out["oct"])
    assert out["oct"][0][1:3] == ["9/25 (금)", "10/1 (목)"] and out["oct"][-1][1:3] == ["10/30 (금)", "11/5 (목)"]


def test_history_screen_has_picker_between_prev_and_next():
    html = (UI / "index.html").read_text()
    assert html.index('id="tl-prev"') < html.index('id="tl-pick"') < html.index('id="tl-next"')
    for needed in ('<dialog id="week-dialog"', 'id="week-year"', 'id="week-month"', 'id="week-list"'):
        assert needed in html, needed
    js = (UI / "views" / "history.js").read_text()
    assert '$("tl-prev").disabled = weekOffset >= maxWeekOffset(now, earliestMs);' in js


def test_time_column_fits_the_chosen_date_shape():
    """녹화 목록의 시각 칸은 고정 너비가 아니라 가장 넓은 줄에 맞춘다(긴 날짜·시각 모양에서 채널 이름과 겹치지 않게)."""
    css = (UI / "style.css").read_text()
    js = (UI / "views" / "history.js").read_text()
    assert "grid-template-columns: var(--s-time, max-content) minmax(0, 1fr)" in css
    assert 'box.style.setProperty("--s-time"' in js and "fitTimeColumn();" in js
    assert ".section-head h2, .section-head .seg { margin-top: 0; margin-bottom: 0; }" in css
    downloads = (UI.parent / "ext" / "downloads" / "static" / "downloads.js").read_text()
    assert '$("jobs-cancel-all").disabled = !data.counts.active;' in downloads
