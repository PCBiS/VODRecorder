"""다시보기 채널 목록 수집·년월 보기·선택 다운로드·중복 방지·전체 취소·상태 필터와 숨김 속성."""

import asyncio
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_recorder import web
from live_recorder.ext.replay import catalog, channel_list
from live_recorder.ext.replay.catalog import BACKOFF, CollectFailed, Collector, FetchError
from live_recorder.ext.downloads.manager import ReplayManager
from live_recorder.ext.downloads.store import Store

from conftest import CHANNEL
from conftest import REPLAY_EXTENSIONS
from replay_fake import ADULT_NO, DASH_NO, FAST_NO, FakeVod
from test_replay import DASH, analyze, enqueue, job, vod_media, wait_job  # noqa: F401 (vod_media는 준비물)
from test_view_only import ViewSite
from test_web import Web
from webui_files import index_html, all_js

WEBUI = Path(__file__).resolve().parents[1] / "live_recorder" / "webui"
LIST = f"/api/replay/channels/{CHANNEL}/videos"
MONTHS = f"/api/replay/channels/{CHANNEL}/months"
COLLECT = f"/api/replay/channels/{CHANNEL}/collect"
CH = "c" * 32


async def instant(_seconds):
    await asyncio.sleep(0)


def collect_state(w):
    return next(c for c in w.client.get("/api/replay/channels").json()["channels"] if c["channel_id"] == CHANNEL)["collect"]


def collected(w):
    r = w.call("POST", COLLECT, json={"mode": "auto"})
    assert r.status_code == 200, r.text
    return w.wait(lambda: (c := collect_state(w))["status"] == "done" and c, what="목록 수집")


@pytest.fixture
def rl(tmp_path, server, tools, vod_media):
    fake = FakeVod(server, vod_media)
    w = Web(tmp_path, server, tools, extensions=REPLAY_EXTENSIONS)
    w.app.state.extensions.contexts["replay"].catalog.collect_sleep = instant  # 시험에서는 쉬지 않는다
    with w:
        w.fake = fake
        w.login()
        w.h.add_channel()
        yield w


def select(w, nos, confirm=None):
    body = {"items": [{"video_no": n, "title": f"제목 {n}"} for n in nos]}
    if confirm is not None:
        body["confirm"] = confirm
    return w.call("POST", "/api/replay/select", json=body)


def jobs(w, status=None):
    return w.client.get("/api/replay/jobs", params={"status": status} if status else None).json()


def states(w, month):
    return {v["video_no"]: v["state"] for v in w.client.get(LIST, params={"month": month}).json()["videos"]}


# ---- 수집기 단위 시험(가짜 응답) ----

def item(no, title=None, publish="2026-11-11 10:00:00", vtype="REPLAY", **extra):
    return {"videoNo": no, "videoTitle": title or f"영상 {no}", "videoType": vtype, "publishDate": publish, "duration": 60,
            "adult": False, "thumbnailImageUrl": "https://nng-phinf.pstatic.net/a.jpg", **extra}


def page_body(page, total_pages, items, total_count=None):
    return {"code": 200, "content": {"page": page, "size": 30, "totalPages": total_pages,
                                     "totalCount": len(items) * total_pages if total_count is None else total_count, "data": items}}


class Script:
    """쪽마다 차례로 돌려줄 응답. (상태, 본문) 또는 예외. 동시에 몇 개를 부르는지도 센다."""

    def __init__(self, pages):
        self.pages = {k: list(v) for k, v in pages.items()}
        self.calls = []
        self.inflight = 0
        self.max_inflight = 0
        self.gate = None  # 이 쪽을 부르면 막힌다(취소 시험)

    async def __call__(self, channel, page):
        self.calls.append(page)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(0)
            if self.gate is not None and page == self.gate[0]:
                await self.gate[1].wait()
            answer = self.pages[page].pop(0) if len(self.pages[page]) > 1 else self.pages[page][0]
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            self.inflight -= 1


def ok(page, total, items, total_count=None):
    return 200, page_body(page, total, items, total_count)


class Box:
    def __init__(self, tmp_path):
        self.store = Store(tmp_path / "state")
        self.db = self.store.db
        self.sleeps = []

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        await asyncio.sleep(0)

    def collector(self, fetch, **kwargs):
        kwargs.setdefault("sleep", self.sleep)
        return Collector(self.db, fetch, **kwargs)

    def rows(self, channel=CH):
        return {r["video"]: dict(r) for r in self.db.execute("SELECT * FROM catalog_videos WHERE channel=?", (channel,))}

    def close(self):
        self.store.close()


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    yield b
    b.close()


def test_pacing_between_pages_and_one_request_at_a_time(box):
    """0쪽부터 totalPages까지 차례로, 쪽 사이에 3초+0~2초를 쉬고 요청은 하나씩만."""
    script = Script({p: [ok(p, 3, [item(p * 10 + i) for i in range(3)])] for p in range(3)})
    jit = iter([0.0, 2.0])
    c = box.collector(script, jitter=lambda: next(jit))
    asyncio.run(c.collect(CH, "full"))
    assert script.calls == [0, 1, 2] and script.max_inflight == 1
    assert box.sleeps == [3.0, 5.0]  # 0쪽 앞에서는 쉬지 않는다
    s = c.summary(CH)
    assert s["status"] == "done" and s["count"] == 9 and s["pages_done"] == 3 and s["total_pages"] == 3 and s["fetched"] == 9
    assert s["checked_at"] and s["full_at"] and s["estimate_seconds"] == 12
    # 기본 무작위 값은 0~2초다
    default = Collector(box.db, script)
    assert all(0.0 <= default.jitter() <= 2.0 for _ in range(200))


def test_queue_runs_one_channel_at_a_time(box):
    """한 번에 한 채널만 수집하고 나머지는 차례를 기다린다. 이미 차례에 있으면 다시 넣지 않는다."""
    script = Script({p: [ok(p, 2, [item(100 + p)])] for p in range(2)})

    async def run():
        c = box.collector(script)
        c.start()
        assert c.request(CH, "full") and c.request("d" * 32, "full")
        assert not c.request(CH, "latest")
        assert c.summary("d" * 32)["status"] == "queued"
        for _ in range(200):
            await asyncio.sleep(0)
            if all(c.summary(x)["status"] == "done" for x in (CH, "d" * 32)):
                break
        await c.close()
        return c

    c = asyncio.run(run())
    assert c.summary(CH)["status"] == "done" and c.summary("d" * 32)["status"] == "done"
    assert script.max_inflight == 1 and script.calls == [0, 1, 0, 1]


def test_backoff_on_429_5xx_and_network_then_success(box):
    """429·5xx·연결 실패는 30초, 60초 뒤 다시 부르고 성공하면 이어 간다."""
    script = Script({0: [(429, None), FetchError("x"), ok(0, 2, [item(1)])], 1: [(503, None), ok(1, 2, [item(2)])]})
    c = box.collector(script, jitter=lambda: 1.0)
    asyncio.run(c.collect(CH, "full"))
    assert c.summary(CH)["status"] == "done" and set(box.rows()) == {"1", "2"}
    assert box.sleeps == [30.0, 60.0, 4.0, 30.0]


def test_backoff_gives_up_after_three_retries_and_keeps_rows(box):
    """세 번 다시 해도 안 되면(30·60·120초) 실패로 끝내고, 받은 줄과 받은 쪽수를 남긴다."""
    script = Script({0: [ok(0, 3, [item(1), item(2)])], 1: [(500, None)]})
    c = box.collector(script, jitter=lambda: 0.0)
    asyncio.run(c.collect(CH, "full"))
    assert box.sleeps == [3.0, *BACKOFF] and list(BACKOFF) == [30.0, 60.0, 120.0]
    assert script.calls == [0, 1, 1, 1, 1]
    s = c.summary(CH)
    assert s["status"] == "failed" and "HTTP 500" in s["error"] and "3번" in s["error"]
    assert s["pages_done"] == 1 and s["total_pages"] == 3 and set(box.rows()) == {"1", "2"}


def test_403_fails_at_once(box):
    """403은 다시 부르지 않고 바로 실패다."""
    script = Script({0: [ok(0, 2, [item(1)])], 1: [(403, {"code": 403, "message": "BODY-SENTINEL"})]})
    c = box.collector(script, jitter=lambda: 0.0)
    asyncio.run(c.collect(CH, "full"))
    assert script.calls == [0, 1] and box.sleeps == [3.0]
    s = c.summary(CH)
    assert s["status"] == "failed" and "403" in s["error"] and "BODY-SENTINEL" not in s["error"]
    assert set(box.rows()) == {"1"} and s["pages_done"] == 1


def test_cancel_keeps_rows_received_so_far(box):
    """취소해도 그때까지 받은 줄은 남는다. 차례를 기다리던 수집도 취소된다."""
    script = Script({0: [ok(0, 3, [item(1)])], 1: [ok(1, 3, [item(2)])], 2: [ok(2, 3, [item(3)])]})
    script.gate = (1, asyncio.Event())

    async def run():
        c = box.collector(script)
        c.start()
        c.request(CH, "full")
        c.request("d" * 32, "full")
        for _ in range(200):
            await asyncio.sleep(0)
            if script.calls == [0, 1]:
                break
        assert c.summary(CH)["status"] == "running"
        assert c.cancel("d" * 32) and c.summary("d" * 32)["status"] == "cancelled"
        assert c.cancel(CH)
        for _ in range(50):
            await asyncio.sleep(0)
        await c.close()
        return c

    c = asyncio.run(run())
    s = c.summary(CH)
    assert s["status"] == "cancelled" and s["pages_done"] == 1 and s["error"]
    assert set(box.rows()) == {"1"}
    assert not c.cancel(CH)  # 끝난 수집은 취소할 것이 없다


def test_full_deletes_unseen_rows_only_on_success(box):
    """전체 수집이 끝까지 성공했을 때에만 이번에 보이지 않은 영상을 지운다. 받은 기록은 건드리지 않는다."""
    with box.db:
        box.db.execute("INSERT INTO catalog_videos(channel,video,title,video_type,adult,seen) VALUES(?,?,?,?,0,0)", (CH, "999", "지워진 영상", "replay"))
        box.db.execute("INSERT INTO downloads(video,quality,title,filename,completed_at) VALUES('999','q','지워진 영상','old.mp4','2026-11-01T00:00:00+00:00')")
    fail = Script({0: [ok(0, 2, [item(1)])], 1: [(403, None)]})
    c = box.collector(fail)
    asyncio.run(c.collect(CH, "full"))
    assert c.summary(CH)["status"] == "failed" and set(box.rows()) == {"1", "999"}
    # 새 영상 확인은 지우지 않는다
    latest = Script({0: [ok(0, 1, [item(1), item(2)])]})
    asyncio.run(box.collector(latest).collect(CH, "latest"))
    assert set(box.rows()) == {"1", "2", "999"}
    good = Script({0: [ok(0, 2, [item(1)])], 1: [ok(1, 2, [item(2)])]})
    asyncio.run(box.collector(good).collect(CH, "full"))
    assert c.summary(CH)["status"] == "done" and set(box.rows()) == {"1", "2"}
    assert box.db.execute("SELECT COUNT(*) FROM downloads WHERE video='999'").fetchone()[0] == 1


def test_total_pages_change_and_duplicates(box):
    """도중에 totalPages가 늘면 그 값까지 받고, 두 쪽에 나온 같은 영상은 한 줄로 둔다."""
    script = Script({0: [ok(0, 2, [item(1), item(2)])], 1: [ok(1, 3, [item(2), item(3)])], 2: [ok(2, 3, [item(4)])]})
    c = box.collector(script)
    asyncio.run(c.collect(CH, "full"))
    assert script.calls == [0, 1, 2]
    s = c.summary(CH)
    assert s["status"] == "done" and s["count"] == 4 and s["fetched"] == 4 and s["total_pages"] == 3
    # 줄어들면 줄어든 값에서 멈춘다
    shrink = Script({0: [ok(0, 3, [item(1)])], 1: [ok(1, 2, [item(2)])], 2: [ok(2, 2, [item(9)])]})
    asyncio.run(box.collector(shrink).collect(CH, "full"))
    assert shrink.calls == [0, 1] and set(box.rows()) == {"1", "2"}


def test_latest_stops_on_all_known_page_and_refreshes_page_zero(box):
    """새 영상 확인은 0쪽부터 받다가 한 쪽이 모두 이미 있는 영상이면 멈춘다. 0쪽은 늘 받아 제목을 고친다."""
    full = Script({0: [ok(0, 3, [item(5), item(4)])], 1: [ok(1, 3, [item(3), item(2)])], 2: [ok(2, 3, [item(1)])]})
    asyncio.run(box.collector(full).collect(CH, "full"))
    latest = Script({0: [ok(0, 4, [item(7), item(6)])], 1: [ok(1, 4, [item(5, "새 제목"), item(4)])],
                     2: [ok(2, 4, [item(3), item(2)])], 3: [ok(3, 4, [item(1)])]})
    asyncio.run(box.collector(latest).collect(CH, "latest"))
    assert latest.calls == [0, 1]
    rows = box.rows()
    assert {"6", "7"} <= set(rows) and rows["5"]["title"] == "새 제목"
    again = Script({0: [ok(0, 4, [item(7, "고친 제목"), item(6)])]})
    c = box.collector(again)
    asyncio.run(c.collect(CH, "latest"))
    assert again.calls == [0] and box.rows()["7"]["title"] == "고친 제목"
    assert c.summary(CH)["status"] == "done" and c.summary(CH)["mode"] == "latest"


def test_restart_marks_running_collection_interrupted_and_does_not_resume(box):
    """재시작 때 수집 중이던 것은 중단으로 표시하고 스스로 다시 시작하지 않는다."""
    with box.db:
        box.db.execute("INSERT INTO catalog_channels(channel,status,mode,pages_done,total_pages,fetched) VALUES(?,?,?,?,?,?)",
                       (CH, "running", "full", 3, 10, 90))
        box.db.execute("INSERT INTO catalog_channels(channel,status,mode) VALUES(?,?,?)", ("d" * 32, "queued", "latest"))
    script = Script({0: [ok(0, 1, [item(1)])]})

    async def run():
        c = box.collector(script)
        c.start()
        for _ in range(20):
            await asyncio.sleep(0)
        assert c.auto(CH) is None  # 중단된 수집은 화면을 열어도 스스로 다시 하지 않는다
        for _ in range(20):
            await asyncio.sleep(0)
        await c.close()
        return c

    c = asyncio.run(run())
    assert script.calls == []
    s = c.summary(CH)
    assert s["status"] == "interrupted" and s["error"] == catalog.RESTART_ERROR and s["pages_done"] == 3
    assert c.summary("d" * 32)["status"] == "interrupted"


def test_auto_start_rules(box):
    """처음 여는 채널은 전체 수집, 마지막 확인에서 30분이 지났으면 새 영상 확인."""
    moment = [datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)]
    script = Script({0: [ok(0, 1, [item(1)])]})
    c = box.collector(script, clock=lambda: moment[0])
    assert c.auto(CH) == "full" and c.summary(CH)["status"] == "queued"
    c.queue.clear()
    asyncio.run(c.collect(CH, "full"))
    assert c.auto(CH) is None
    moment[0] += timedelta(minutes=29)
    assert c.auto(CH) is None
    moment[0] += timedelta(minutes=2)
    assert c.auto(CH) == "latest" and c.summary(CH)["mode"] == "latest"


def test_rows_with_missing_values_are_kept_and_bad_shape_fails_without_body(box):
    """줄에 값이 빠져도 그 줄만 빈 값으로 두고 이어 간다. 쪽 전체를 읽을 수 없으면 실패이고 이유에 본문을 넣지 않는다."""
    odd = [{"videoNo": 11}, {"videoNo": 12, "videoTitle": None, "publishDate": "nope", "duration": "x", "adult": True,
                              "thumbnailImageUrl": "https://nng-phinf.pstatic.net/b.jpg"}, {"videoTitle": "번호 없음"}, "garbage"]
    script = Script({0: [ok(0, 2, odd)], 1: [(200, {"code": 200, "content": {"page": 1, "data": "BODY-SENTINEL"}})]})
    c = box.collector(script)
    asyncio.run(c.collect(CH, "full"))
    rows = box.rows()
    assert set(rows) == {"11", "12"}
    assert rows["12"]["publish_date"] is None and rows["12"]["month"] is None and rows["12"]["duration"] is None
    assert rows["12"]["adult"] == 1 and rows["12"]["thumbnail"] is None and rows["11"]["title"] == "영상 11"
    s = c.summary(CH)
    assert s["status"] == "failed" and "BODY-SENTINEL" not in s["error"] and s["pages_done"] == 1
    assert c.months(CH) == [{"month": catalog.NO_MONTH, "count": 2}]
    assert [v["video_no"] for v in c.videos(CH, catalog.NO_MONTH)] == ["12", "11"]
    with pytest.raises(channel_list.ListError):
        channel_list.parse_page(200, {"code": 200, "content": {"page": 3, "totalPages": 1, "totalCount": 1, "data": []}}, 0)
    with pytest.raises(channel_list.ListError):
        channel_list.parse_page(200, {"code": 500}, 0)


def test_months_are_grouped_in_seoul_time(box, monkeypatch):
    """달은 게시 날짜를 Asia/Seoul로 바꿔 정한다. 시각 문자열의 시간대(SOURCE_TZ)가 바뀌어도 서울 기준이다."""
    assert channel_list.publish_fields("2026-11-01 00:30:00") == ("2026-11-01 00:30:00", "2026-11")
    assert channel_list.publish_fields("2026-10-31 23:59:59")[1] == "2026-10"
    monkeypatch.setattr(channel_list, "SOURCE_TZ", timezone.utc)
    assert channel_list.publish_fields("2026-10-31 20:00:00") == ("2026-11-01 05:00:00", "2026-11")
    assert channel_list.publish_fields("2026-13-01 00:00:00") == (None, None)


def test_months_order_type_filter_and_search_cap(box):
    """달은 최근부터, 종류 필터는 달의 수에도 걸린다. 검색은 수집 목록 전체에서 200개까지, 넘치면 알린다."""
    items = []
    for i in range(250):
        month = 1 + i % 12
        items.append(item(1000 + i, f"검색어 {i}", publish=f"2025-{month:02d}-15 12:00:00", vtype="REPLAY" if i % 2 else "UPLOAD"))
    items += [item(5000, "다른 제목", publish="2026-01-02 00:00:00"), item(5001, "다른 제목 업로드", publish="2026-01-03 00:00:00", vtype="UPLOAD")]
    pages = [items[i:i + 30] for i in range(0, len(items), 30)]
    script = Script({p: [ok(p, len(pages), rows)] for p, rows in enumerate(pages)})
    c = box.collector(script)
    asyncio.run(c.collect(CH, "full"))
    months = c.months(CH)
    assert [m["month"] for m in months][:3] == ["2026-01", "2025-12", "2025-11"] and sum(m["count"] for m in months) == 252
    assert months[0]["count"] == 2
    assert c.months(CH, "replay")[0] == {"month": "2026-01", "count": 1}
    assert sum(m["count"] for m in c.months(CH, "upload")) == 126
    jan = c.videos(CH, "2026-01")
    assert [v["video_no"] for v in jan] == ["5001", "5000"]  # 게시 날짜 내림차순
    assert set(jan[0]) == {"video_no", "title", "video_type", "publish_date", "month", "duration", "adult", "thumbnail"}
    assert [v["video_no"] for v in c.videos(CH, "2026-01", "replay")] == ["5000"]
    found, more = c.search(CH, "검색어")
    assert len(found) == 200 and more is True
    assert len({v["month"] for v in found}) == 10  # 여러 달에 걸쳐 찾는다(12월부터 3월까지 200개)
    assert found == sorted(found, key=lambda v: v["publish_date"], reverse=True)
    found, more = c.search(CH, "검색어", "replay")
    assert len(found) == 125 and more is False and all(v["video_type"] == "replay" for v in found)
    assert c.search(CH, "업로드")[0][0]["video_no"] == "5001"


# ---- 저장소 옮기기 ----

def old_replay_db(state):
    """이전 빌드의 다시보기 DB. 수집 표가 없다."""
    state.mkdir(parents=True)
    db = sqlite3.connect(state / "replay.sqlite3")
    db.executescript('''
      CREATE TABLE records(id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
      CREATE INDEX records_queue ON records(kind,status);
      CREATE TABLE requests(owner TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, ids TEXT NOT NULL, PRIMARY KEY(owner,key));
      CREATE TABLE logs(seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
    ''')
    return db


def test_previous_build_db_opens_and_gets_catalog_tables(tmp_path):
    """이전 빌드의 다시보기 DB가 그대로 열리고 수집 표가 더해진다. 받은 기록과 작업 내역은 그대로다."""
    state, root = tmp_path / "state", tmp_path / "rec" / "replay"
    root.mkdir(parents=True)
    db = old_replay_db(state)
    db.execute('''CREATE TABLE downloads(video TEXT NOT NULL, quality TEXT NOT NULL, title TEXT NOT NULL, filename TEXT NOT NULL,
                  completed_at TEXT NOT NULL, PRIMARY KEY(video,quality))''')
    db.execute("INSERT INTO downloads(video,quality,title,filename,completed_at) VALUES('777','q','옛 영상','old.mp4','2026-11-11T00:00:00+00:00')")
    old = {"id": "b" * 32, "status": "failed", "url": "https://chzzk.naver.com/video/778", "title": "실패", "updated_at": "2026-11-11T00:00:00+00:00"}
    db.execute("INSERT INTO records(id,kind,owner,status,payload) VALUES(?,?,?,?,?)", (old["id"], "job", "admin", "failed", json.dumps(old)))
    db.commit()
    db.close()

    async def run(first):
        m = ReplayManager(state, root, 0, download_base=tmp_path / "rec")
        try:
            tables = {r[0] for r in m.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"records", "requests", "logs", "downloads", "catalog_videos", "catalog_channels"} <= tables
            assert m.get_job("admin", old["id"])["status"] == "failed"
            assert m.db.execute("SELECT title FROM downloads WHERE video='777'").fetchone()[0] == "옛 영상"
            c = Collector(m.db, Script({0: [ok(0, 1, [item(777)])]}), sleep=instant)
            if first:
                await c.collect(CH, "full")
            assert c.summary(CH)["count"] == 1  # 두 번째로 열 때도 수집 목록이 남아 있다
        finally:
            await m.close()

    asyncio.run(run(True))
    asyncio.run(run(False))


# ---- 웹 API ----

def test_collect_api_reads_only_the_catalog(rl):
    """처음 열면 전체 수집, 화면은 수집한 목록만 읽는다. 필요한 필드만 넘긴다."""
    chans = rl.client.get("/api/replay/channels").json()["channels"]
    assert [(c["channel_id"], c["name"]) for c in chans] == [(CHANNEL, "시험채널")]
    assert chans[0]["collect"]["status"] is None and chans[0]["collect"]["count"] == 0
    assert rl.fake.list_calls == 0  # 채널 목록만 보는 것으로는 치지직을 부르지 않는다
    done = collected(rl)
    assert done["count"] == 4 and done["mode"] == "full" and done["pages_done"] == 1 and done["checked_at"]
    assert rl.call("POST", COLLECT, json={"mode": "auto"}).json()["started"] is None  # 방금 확인했다
    months = rl.client.get(MONTHS).json()
    assert months["months"] == [{"month": "2026-11", "count": 2}, {"month": "2026-10", "count": 2}]
    assert months["collect"]["status"] == "done"
    assert rl.client.get(MONTHS, params={"type": "upload"}).json()["months"] == [{"month": "2026-10", "count": 1}]
    r = rl.client.get(LIST, params={"month": "2026-11"})
    assert r.status_code == 200, r.text
    videos = r.json()["videos"]
    assert [v["video_no"] for v in videos] == [DASH_NO, FAST_NO]
    first = videos[0]
    # 줄마다 받을 주소(공급자가 만든다)가 더해졌다. 화면의 "옵션"이 이 주소로 분석한다
    assert set(first) == {"video_no", "title", "video_type", "publish_date", "month", "duration", "adult", "thumbnail", "state", "url"}
    assert first["url"].endswith(f"/video/{DASH_NO}")
    assert first["video_type"] == "replay" and first["state"] is None and first["thumbnail"].startswith("https://nng-phinf.pstatic.net/")
    oct_ = rl.client.get(LIST, params={"month": "2026-10"}).json()["videos"]
    adult = next(v for v in oct_ if v["video_no"] == ADULT_NO)
    assert adult["adult"] is True and adult["thumbnail"] is None  # 성인 영상의 썸네일은 넘기지 않는다
    found = rl.client.get(LIST, params={"q": "다시보기"}).json()
    assert [v["video_no"] for v in found["videos"]] == [DASH_NO, FAST_NO] and found["truncated"] is False
    for text in (r.text, json.dumps(found), json.dumps(oct_)):
        assert "SECRET" not in text and "trailer" not in text.lower()
    assert rl.fake.list_calls == 1  # 보기는 몇 번을 해도 치지직을 다시 부르지 않는다
    assert rl.client.get(LIST).status_code == 400 and rl.client.get(LIST, params={"month": "2026-13"}).status_code == 400
    # 등록하지 않은 채널은 거부한다
    other = "f" * 32
    assert rl.client.get(f"/api/replay/channels/{other}/videos", params={"month": "2026-11"}).status_code == 404
    assert rl.client.get(f"/api/replay/channels/{other}/months").status_code == 404
    assert rl.call("POST", f"/api/replay/channels/{other}/collect", json={"mode": "full"}).status_code == 404
    assert rl.client.get("/api/replay/channels/../videos").status_code == 404
    assert rl.call("POST", COLLECT, json={"mode": "everything"}).status_code in (400, 422)
    # 새 영상 확인·전체 다시 수집·취소
    assert rl.call("POST", COLLECT, json={"mode": "latest"}).json()["started"] == "latest"
    rl.wait(lambda: collect_state(rl)["status"] == "done", what="새 영상 확인")
    assert rl.call("POST", f"{COLLECT}/cancel").json()["cancelled"] is False


OTHER = "f" * 32
REPLAY_CHANNELS = "/api/replay/channels"


def replay_channels(w):
    return {c["channel_id"]: c for c in w.client.get(REPLAY_CHANNELS).json()["channels"]}


def catalog_rows(w, channel=CHANNEL):
    return w.app.state.extensions.contexts["downloads"].service.manager.db.execute("SELECT COUNT(*) FROM catalog_videos WHERE channel=?", (channel,)).fetchone()[0]


def test_add_replay_channel_by_id_and_refusals(rl):
    """녹화 채널이 아닌 채널도 더한다. 이름은 채널 정보 API를 한 번 불러 받는다. 없는 채널·이름을 받지 못한 경우는 넣지 않는다."""
    rl.fake.channel_names[OTHER] = "다른 스트리머"
    r = rl.call("POST", REPLAY_CHANNELS, json={"channel_id": OTHER})
    assert r.status_code == 201, r.text
    assert r.json() == {"channel_id": OTHER, "name": "다른 스트리머"} and rl.fake.channel_calls == [OTHER]
    chans = replay_channels(rl)
    assert chans[OTHER]["recording"] is False and chans[OTHER]["name"] == "다른 스트리머" and chans[OTHER]["collect"]["status"] is None
    assert chans[CHANNEL]["recording"] is True  # 녹화 채널은 스스로 들어와 있다
    assert rl.call("POST", REPLAY_CHANNELS, json={"channel_id": OTHER}).status_code == 409
    # 녹화 채널이 아니어도 목록 수집·년월 보기가 된다(처음 열면 전체 수집)
    assert rl.call("POST", f"{REPLAY_CHANNELS}/{OTHER}/collect", json={"mode": "auto"}).json()["started"] == "full"
    rl.wait(lambda: replay_channels(rl)[OTHER]["collect"]["status"] == "done", what="다른 채널 수집")
    assert rl.client.get(f"{REPLAY_CHANNELS}/{OTHER}/months").json()["months"][0] == {"month": "2026-11", "count": 2}
    assert rl.h.store.get_channel(OTHER) is None  # 감시·녹화 목록에는 들어가지 않는다
    # 없는 채널
    missing = "e" * 32
    r = rl.call("POST", REPLAY_CHANNELS, json={"channel_id": missing})
    assert r.status_code == 404 and "없는 채널" in r.json()["error"] and missing not in replay_channels(rl)
    # 이름을 받지 못함(치지직 오류)
    rl.fake.channel_status = 503
    r = rl.call("POST", REPLAY_CHANNELS, json={"channel_id": "d" * 32})
    assert r.status_code == 502 and "추가하지 않았습니다" in r.json()["error"] and "d" * 32 not in replay_channels(rl)
    # 서버는 32자리 ID만 받는다(주소는 화면이 ID로 바꿔 보낸다)
    for bad in ("https://chzzk.naver.com/" + OTHER, "abc", OTHER.upper()):
        assert rl.call("POST", REPLAY_CHANNELS, json={"channel_id": bad}).status_code == 400, bad


def test_screen_parses_urls_like_ui_14():
    """다시보기 채널 추가 칸은 채널 추가 칸과 같은 함수로 주소·ID를 해석한다."""
    js = all_js()
    # 주소 규칙은 화면에 적지 않고 치지직 플랫폼 모듈이 준다(/api/platforms의 channel_rule). 기본 규칙(치지직)으로 견준다
    from live_recorder.platforms import registry

    body = js[js.index("function parseChannelId"):js.index("function formPlatform")]
    assert "parseChannelId(raw)" in js[js.index("async function addReplayChannel"):]
    cid = "0123456789abcdef0123456789abcdef"
    cases = [cid, cid.upper(), f"https://chzzk.naver.com/live/{cid}", f"chzzk.naver.com/{cid}", f"https://m.chzzk.naver.com/{cid}?x=1",
             f"www.chzzk.naver.com/live/{cid}/", f"https://evil.example/{cid}", "nope"]
    rule = json.dumps(registry.get("chzzk").channel_rule)
    script = (f"function channelRule() {{ return {rule}; }}\n" + body
              + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map((c) => parseChannelId(c))));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [cid] * 6 + [None, None]


def test_replay_channel_limit_is_50(box):
    """다시보기 채널은 50개까지다. 녹화 채널을 스스로 넣을 때도 넘기지 않는다."""
    names = []

    async def channel_fetch(cid):
        names.append(cid)
        return 200, {"code": 200, "content": {"channelId": cid, "channelName": "새 채널"}}

    c = box.collector(Script({}), channel_fetch=channel_fetch)
    with box.db:
        for i in range(catalog.MAX_CHANNELS):
            box.db.execute("INSERT INTO replay_channels(channel,name,added_at) VALUES(?,?,?)", (f"{i:032x}", f"채널 {i}", "2026-11-12T00:00:00+00:00"))
    with pytest.raises(catalog.ChannelError) as e:
        asyncio.run(c.add_channel(CH))
    assert e.value.status == 409 and "50" in e.value.message and names == []  # 넘치면 치지직을 부르지도 않는다
    assert c.sync_recording([(CH, "녹화")]) == [] and not c.add_recording(CH, "녹화") and not c.has_channel(CH)
    with box.db:
        box.db.execute("DELETE FROM replay_channels WHERE channel=?", (f"{0:032x}",))
    assert asyncio.run(c.add_channel(CH)) == {"channel_id": CH, "name": "새 채널"} and len(c.channels()) == 50


def test_remove_cancels_running_collection_and_deletes_catalog(box):
    """수집 중인 채널을 지우면 수집을 먼저 취소하고 수집 목록과 함께 지운다. 받은 기록은 남긴다."""
    script = Script({0: [ok(0, 3, [item(1)])], 1: [ok(1, 3, [item(2)])]})
    script.gate = (1, asyncio.Event())
    with box.db:
        box.db.execute("INSERT INTO downloads(video,quality,title,filename,completed_at) VALUES('1','q','받은 영상','a.mp4','2026-11-01T00:00:00+00:00')")

    async def run():
        c = box.collector(script)
        c.sync_recording([(CH, "녹화")])
        c.start()
        c.request(CH, "full")
        for _ in range(200):
            await asyncio.sleep(0)
            if script.calls == [0, 1]:
                break
        assert c.summary(CH)["status"] == "running"
        task = c.current[1]
        await c.remove_channel(CH)
        assert task.done()
        with pytest.raises(catalog.ChannelError):
            await c.remove_channel(CH)
        await c.close()
        return c

    c = asyncio.run(run())
    assert not c.has_channel(CH) and box.rows() == {} and c.summary(CH)["status"] is None
    assert box.db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 1
    # 지운 녹화 채널은 스스로 다시 넣지 않는다. 녹화 채널을 다시 추가하면 넣는다
    assert c.sync_recording([(CH, "녹화")]) == [] and not c.has_channel(CH)
    assert c.add_recording(CH, "녹화") and c.has_channel(CH)


def test_channel_name_is_refreshed_from_list_response(box):
    """목록 응답에 그 채널의 이름이 있으면 그것으로 새로 고친다."""
    c = box.collector(Script({0: [ok(0, 1, [item(1, channel={"channelId": CH, "channelName": "바뀐 이름"}),
                                            item(2, channel={"channelId": "other", "channelName": "남의 이름"})])]}))
    c.sync_recording([(CH, "옛 이름")])
    asyncio.run(c.collect(CH, "latest"))
    assert c.channels()[0]["name"] == "바뀐 이름"


def test_recording_channel_add_and_delete_vs_replay_list(rl):
    """녹화 채널을 더하면 다시보기 채널에 들어가고, 녹화 채널을 지워도 다시보기 채널과 수집 목록은 남는다.
    관리자가 다시보기 채널에서 지운 녹화 채널은 스스로 다시 넣지 않고, 녹화 채널을 지웠다가 다시 추가하면 넣는다."""
    collected(rl)
    before = catalog_rows(rl)
    assert before == 4
    # 녹화 채널 추가 → 다시보기 채널에 들어간다(이름은 녹화 채널 이름. 채널 정보 API를 부르지 않는다)
    assert rl.call("POST", "/api/channels", json={"channel_id": OTHER, "name": "새 녹화", "output_dir": "b"}).status_code == 201
    chans = replay_channels(rl)
    assert chans[OTHER]["recording"] is True and chans[OTHER]["name"] == "새 녹화" and rl.fake.channel_calls == []
    # 녹화 채널을 지워도 수집 목록은 남는다
    assert rl.call("DELETE", f"/api/channels/{CHANNEL}").status_code == 200
    chans = replay_channels(rl)
    assert CHANNEL in chans and chans[CHANNEL]["recording"] is False and chans[CHANNEL]["collect"]["count"] == before
    assert catalog_rows(rl) == before
    # 다시보기 채널에서 지우면 수집 목록도 지운다. 받은 기록은 남는다
    db = rl.app.state.extensions.contexts["downloads"].service.manager.db
    with db:
        db.execute("INSERT INTO downloads(video,quality,title,filename,completed_at) VALUES(?,?,?,?,?)", (DASH_NO, "q", "받은 영상", "x.mp4", "2026-11-11T00:00:00+00:00"))
    assert rl.call("DELETE", f"{REPLAY_CHANNELS}/{CHANNEL}").status_code == 200
    assert CHANNEL not in replay_channels(rl) and catalog_rows(rl) == 0
    assert db.execute("SELECT COUNT(*) FROM catalog_channels WHERE channel=?", (CHANNEL,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM downloads WHERE video=?", (DASH_NO,)).fetchone()[0] == 1
    assert rl.client.get(LIST, params={"month": "2026-11"}).status_code == 404
    # 다시보기 채널에서 지운 녹화 채널(OTHER)은 채널 목록을 다시 읽어도(녹화 채널 동기화) 스스로 들어오지 않는다
    assert rl.call("DELETE", f"{REPLAY_CHANNELS}/{OTHER}").status_code == 200
    assert OTHER not in replay_channels(rl) and OTHER not in replay_channels(rl)
    # 녹화 채널을 지웠다가 다시 추가하면 넣는다
    assert rl.call("DELETE", f"/api/channels/{OTHER}").status_code == 200
    assert OTHER not in replay_channels(rl)
    assert rl.call("POST", "/api/channels", json={"channel_id": OTHER, "name": "다시 녹화", "output_dir": "b"}).status_code == 201
    assert replay_channels(rl)[OTHER]["recording"] is True


def write_catalog_db(state, with_catalog):
    """이전 빌드의 다시보기 DB. 아주 옛 형식(수집 표 없음) 또는 앞 작업의 수집 표(다시보기 채널 표 없음)."""
    db = old_replay_db(state)
    db.execute("CREATE TABLE downloads(video TEXT NOT NULL, quality TEXT NOT NULL, title TEXT NOT NULL, filename TEXT NOT NULL, "
               "completed_at TEXT NOT NULL, PRIMARY KEY(video,quality))")
    if with_catalog:
        db.executescript("""
          CREATE TABLE catalog_videos(channel TEXT NOT NULL, video TEXT NOT NULL, title TEXT NOT NULL, video_type TEXT NOT NULL,
            publish_date TEXT, month TEXT, duration INTEGER, adult INTEGER NOT NULL, thumbnail TEXT, seen INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(channel,video));
          CREATE INDEX catalog_videos_month ON catalog_videos(channel,month,publish_date);
          CREATE TABLE catalog_channels(channel TEXT PRIMARY KEY, status TEXT NOT NULL, mode TEXT NOT NULL, run INTEGER NOT NULL DEFAULT 0,
            pages_done INTEGER NOT NULL DEFAULT 0, total_pages INTEGER, fetched INTEGER NOT NULL DEFAULT 0, error TEXT, started_at TEXT,
            finished_at TEXT, checked_at TEXT, full_at TEXT);
        """)
        for i in range(3):
            db.execute("INSERT INTO catalog_videos(channel,video,title,video_type,publish_date,month,duration,adult,thumbnail,seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (CHANNEL, str(100 + i), f"옛 수집 {i}", "replay", f"2026-09-0{i + 1} 00:00:00", "2026-09", 60, 0, None, 1))
        db.execute("INSERT INTO catalog_channels(channel,status,mode,run,pages_done,total_pages,fetched,finished_at,checked_at,full_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)", (CHANNEL, "done", "full", 1, 1, 1, 3, "2026-11-01T00:00:00+00:00",
                                                   "2026-11-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00"))
    db.commit()
    db.close()


@pytest.mark.parametrize("with_catalog", [False, True], ids=["old-db", "catalog-db"])
def test_upgrade_adds_recording_channels_once_and_keeps_catalog(tmp_path, server, tools, vod_media, with_catalog):
    """이 기능이 없던 빌드의 DB로 처음 뜨면 녹화 채널을 다시보기 채널에 넣고, 이미 수집해 둔 목록은 그대로 이어 쓴다."""
    FakeVod(server, vod_media)
    w = Web(tmp_path, server, tools, extensions=REPLAY_EXTENSIONS)
    w.app.state.extensions.contexts["replay"].catalog.collect_sleep = instant
    w.h.add_channel()
    write_catalog_db(w.h.cfg.data_dir / "replay", with_catalog)
    with w:
        collector = w.app.state.extensions.contexts["replay"].catalog.collector
        assert [(c["channel_id"], c["name"]) for c in collector.channels()] == [(CHANNEL, "시험채널")]  # 화면을 열기 전, 시작할 때 넣었다
        s = collector.summary(CHANNEL)
        assert s["count"] == (3 if with_catalog else 0) and s["status"] == ("done" if with_catalog else None)
        w.login()
        if with_catalog:
            videos = w.client.get(LIST, params={"month": "2026-09"}).json()["videos"]
            assert [v["video_no"] for v in videos] == ["102", "101", "100"]
            # 수집한 적이 있고 마지막 확인에서 30분이 지났으므로 새 영상 확인만 한다
            assert w.call("POST", COLLECT, json={"mode": "auto"}).json()["started"] == "latest"
        else:
            assert w.call("POST", COLLECT, json={"mode": "auto"}).json()["started"] == "full"
        w.wait(lambda: collector.summary(CHANNEL)["status"] == "done", what="수집")


def test_select_download_default_quality_duplicate_and_confirm(rl):
    """고른 영상을 기본 화질(best)로 받고, 다시 넣으려면 확인 표시가 있어야 한다. 상태는 수집 목록에 붙는다."""
    collected(rl)
    assert select(rl, [str(n) for n in range(1, 32)]).status_code == 400  # 30개 제한
    assert select(rl, ["abc"]).status_code == 400
    r = select(rl, [DASH_NO])
    assert r.status_code == 200, r.text
    assert r.json()["queued"] == 1
    # 분석을 기다리는 동안에도 중복이다
    dup = select(rl, [DASH_NO])
    assert dup.status_code == 409 and dup.json()["duplicates"] == [f"제목 {DASH_NO}"]
    created = rl.wait(lambda: jobs(rl)["jobs"], timeout=60, what="선택 다운로드 작업")
    assert len(created) == 1 and created[0]["url"].endswith(f"/video/{DASH_NO}")
    # best: 분석 결과 화질 목록(순서)에서 가장 높은 높이의 첫 항목. 주소로 받기 카드의 기본값과 같다
    top = analyze(rl, [DASH])["items"][0]["qualities"]
    assert created[0]["quality"] == top[0]["id"] and top[0]["height"] == max(q["height"] or 0 for q in top)
    done = wait_job(rl, created[0]["id"])
    assert done["status"] == "completed", done
    state = states(rl, "2026-11")
    assert state[DASH_NO] == "downloaded" and state[FAST_NO] is None
    found = rl.client.get(LIST, params={"q": "DASH"}).json()["videos"]
    assert found[0]["state"] == "downloaded"  # 검색 결과에도 상태가 붙는다
    # 주소로 받기도 같은 규칙이다
    a = analyze(rl, [DASH])
    r = rl.call("POST", "/api/replay/jobs", json={"analysis_id": a["id"], "items": [{"url": DASH, "quality": "PD_90P_160_100_24"}],
                                                   "key": "dup-key-00001"})
    assert r.status_code == 409 and r.json()["duplicates"] == ["DASH 다시보기"]
    # 파일을 지우면 "받았으나 파일 없음"
    (rl.h.root / "replay" / done["filename"]).unlink()
    assert states(rl, "2026-11")[DASH_NO] == "missing"
    # 확인 표시가 있으면 넣는다
    assert select(rl, [DASH_NO], confirm=True).status_code == 200


def test_cancel_all_and_status_filter_counts(rl):
    """상태 필터와 건수, 대기·진행 전체 취소."""
    rl.fake.delay = 0.05
    a = analyze(rl, [DASH])
    first = enqueue(rl, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}])[0]
    second = rl.call("POST", "/api/replay/jobs", json={"analysis_id": a["id"], "items": [{"url": DASH, "quality": "hls-uuid-0002"}],
                                                       "key": "second-key-01", "confirm": True}).json()["jobs"][0]
    rl.wait(lambda: job(rl, first["id"])["status"] == "running", what="첫 작업 시작")
    st = rl.client.get("/api/replay/status").json()
    assert st["active"] == 2
    data = jobs(rl, "attention")
    assert data["counts"]["active"] == 2 and data["counts"]["attention"] == 2 and data["counts"]["completed"] == 0
    assert rl.client.get("/api/replay/jobs", params={"status": "nope"}).status_code == 400
    r = rl.call("POST", "/api/replay/cancel-active")
    assert r.status_code == 200 and r.json()["cancelled"] == 2
    rl.fake.delay = 0
    for ident in (first["id"], second["id"]):
        assert wait_job(rl, ident, ("cancelled",))["status"] == "cancelled"
    data = jobs(rl, "failed")
    assert data["counts"] == {"attention": 0, "active": 0, "failed": 2, "completed": 0}
    assert {j["id"] for j in data["jobs"]} == {first["id"], second["id"]}
    assert jobs(rl, "completed")["jobs"] == [] and jobs(rl, "attention")["jobs"] == []
    assert rl.client.get("/api/replay/status").json()["active"] == 0


NEW_ROUTES = (("GET", "/api/replay/channels"), ("GET", LIST), ("GET", MONTHS), ("POST", COLLECT), ("POST", f"{COLLECT}/cancel"),
              ("POST", "/api/replay/select"), ("POST", "/api/replay/cancel-active"), ("POST", "/api/replay/channels"),
              ("DELETE", f"/api/replay/channels/{CHANNEL}"))


def test_new_routes_need_login_and_csrf(rl):
    """새 경로는 로그인이 필요하고 상태를 바꾸는 요청은 CSRF를 거친다."""
    # 다시보기 경로의 접근 규칙은 확장이 등록한다(중심부 표 + 켜진 확장의 규칙)
    access = rl.app.state.access
    for method, path in NEW_ROUTES:
        assert access.level(method, path) == web.LOGIN, path
    for method, path in (("POST", f"/api/replay/channels/{CHANNEL}/collect"), ("POST", "/api/replay/channels"),
                         ("DELETE", f"/api/replay/channels/{CHANNEL}")):
        assert any(r[0] == method and r[2] == web.LOGIN and path.startswith(r[1].rstrip("*")) for r in access.rules[len(web.ACCESS_RULES):])
    assert rl.client.post("/api/replay/channels", json={"channel_id": "f" * 32}).status_code == 403
    assert rl.client.delete(f"/api/replay/channels/{CHANNEL}").status_code == 403
    assert rl.client.post("/api/replay/cancel-active").status_code == 403  # CSRF 토큰 없음
    assert rl.client.post("/api/replay/select", json={"items": [{"video_no": DASH_NO}]}).status_code == 403
    assert rl.client.post(COLLECT, json={"mode": "full"}).status_code == 403
    assert rl.client.post(f"{COLLECT}/cancel").status_code == 403
    assert collect_state(rl)["status"] is None and rl.fake.list_calls == 0  # 거부된 요청은 수집을 시작하지 않았다
    rl.client.post("/api/logout", headers={"X-CSRF-Token": rl.csrf})
    for method, path in NEW_ROUTES:
        if method == "GET":
            assert rl.client.get(path, params={"month": "2026-11"}).status_code == 401, path


def test_new_routes_refused_in_view_only_mode(tmp_path, server, tools):
    """계정이 없는 보기 전용 모드에서는 목록·수집 경로를 모두 거부한다."""
    with ViewSite(tmp_path, server, tools) as v:
        v.h.add_channel()
        for method, path in NEW_ROUTES:
            r = v.client.request(method, path, json={"mode": "full", "channel_id": CHANNEL} if method == "POST" else None)
            assert r.status_code == 403 and r.json()["view_only"] is True, path


# ---- 화면 ----

def test_screen_elements_match_app_js():
    """화면 요소와 app.js의 일치. boot()가 묶는 id가 index.html에 모두 있어야 시작 실패가 나지 않는다."""
    html = index_html()
    js = all_js()
    ids = set(re.findall(r'id="([^"]+)"', html))
    for ident in ("replay-mode-list", "replay-mode-url", "replay-list-pane", "replay-url-pane", "vl-channel", "vl-type", "vl-search",
                  "vl-collect", "vl-latest", "vl-remove", "vl-add-form", "vl-add-input", "vl-add-note", "vl-full", "vl-collect-cancel", "vl-collect-error", "vl-months", "vl-month-prev",
                  "vl-month-next", "vl-month-label", "vl-month-chips", "vl-search-note", "vl-selected", "vl-clear", "vl-download",
                  "vl-all", "vl-body", "jobs-cancel-all", "replay-badge"):
        assert ident in ids, ident
        assert f'$("{ident}")' in js or f"#{ident}" in js, ident
    for ident in set(re.findall(r'\$\("([a-z0-9-]+)"\)', js)):
        assert ident in ids, f"app.js가 찾는 요소가 화면에 없다: {ident}"
    for gone in ("vl-prev", "vl-next", "vl-page", "?page="):  # 페이지 넘기기는 없앴다
        assert gone not in html and gone not in js, gone
    for name in ("attention", "active", "failed", "completed"):
        assert f'data-filter="{name}"' in html and f'data-count="{name}"' in html
    assert "/api/replay/select" in js and "/api/replay/cancel-active" in js and "confirm: true" in js
    assert '{ mode: "auto" }' in js and "/collect/cancel" in js and "/months?" in js
    assert "<script>" not in html and "style=" not in html  # CSP: 인라인 스크립트·스타일 없음


def css_rules(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def test_hidden_attribute_always_wins_and_tab_spacing():
    """숨김 속성은 어떤 클래스의 display에도 지지 않는다. 탭 글자와 배지 사이는 좁은 한 칸이고 빈칸 글자를 두지 않는다."""
    css = (WEBUI / "style.css").read_text(encoding="utf-8")
    rules = css_rules(css)
    hidden = [body for sel, body in rules if sel == "[hidden]"]
    assert hidden and re.search(r"display:\s*none\s*!important", hidden[0])
    # 숨김 속성을 쓰는 요소의 클래스 가운데 display를 정하는 것(예전 결함: .tab-badge, .row)이 있어도 공통 규칙이 이긴다
    pages = [index_html()] + [(WEBUI / p).read_text(encoding="utf-8") for p in ("login.html", "setup.html")]  # 확장 셋을 켠 화면
    with_display = set()
    for html in pages:
        for tag in re.findall(r"<[a-z]+\b[^>]*>", html):
            if not re.search(r"\shidden(?=[\s>/])", tag):
                continue
            m = re.search(r'class="([^"]+)"', tag)
            for cls in (m.group(1).split() if m else []):
                if any(re.search(rf"\.{re.escape(cls)}(?![\w-])", sel) and "display" in body for sel, body in rules):
                    with_display.add(cls)
    assert {"tab-badge", "row"} <= with_display  # 이 둘은 숨김 속성을 받으면서 display를 가진다
    assert not any("!important" in body and "display" in body for sel, body in rules if sel != "[hidden]")
    assert ".boot-failed:not([hidden])" in css  # 보일 때만 배치 규칙은 그대로 둔다
    # 탭 단추: 글자와 배지 사이에 빈칸 글자가 없고, 간격은 gap 하나로 준다(배지를 숨기면 간격도 없다)
    index = pages[0]
    tabs = re.findall(r'<button class="tab"[^>]*>(.*?)</button>', index)
    assert len(tabs) == 6 and all(t == t.strip() for t in tabs)
    # 탭 이름은 켜진 확장에 맞춰 화면이 바꾸므로 글자를 span에 둔다(글자와 배지 사이에 빈칸 글자는 여전히 없다)
    assert '><span id="replay-tab-label">다시보기</span><span id="replay-badge" class="tab-badge" hidden></span></button>' in index
    tab = [body for sel, body in rules if sel == ".tab"][0]
    assert "inline-flex" in tab and re.search(r"gap:\s*\.3rem", tab)
    badge = [body for sel, body in rules if sel == ".tab-badge"][0]
    assert "margin" not in badge
    # 좁은 화면에서도 탭 사이 간격을 따로 줄이지 않는다
    assert not [body for sel, body in rules if sel == ".tabs" and "gap" in body and ".25rem" not in body]
