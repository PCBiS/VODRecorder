"""씨미 채널의 다시보기 목록에서 골라 받기. 실제 씨미는 부르지 않는다.

가짜 씨미(tests/fake_cime.py의 serve_lists)가 여러 쪽의 목록을 다음 쪽 표시(continuation)로 낸다.
치지직 목록의 동작·요청·저장은 tests/test_replay_list.py가 그대로 지킨다.
"""

import asyncio
import json
from pathlib import Path

import pytest

import fake_cime
from conftest import CHANNEL
from live_recorder.ext.cime_media import channel_list as cl
from live_recorder.ext.downloads import api
from live_recorder.ext.downloads.catalog import BACKOFF, Collector
from live_recorder.ext.downloads.store import Store
from live_recorder.platforms.cime import platform as cm
from replay_fake import FakeVod, build_media

CID, SLUG, NAME = "1007346", "jeongnae", "정나은양"
OTHER_CID, OTHER_SLUG = "2000001", "other"


async def instant(_seconds):
    await asyncio.sleep(0)


@pytest.fixture(scope="module")
def cime_files(tools, tmp_path_factory):
    out = tmp_path_factory.mktemp("cime_list_media")
    build_media(tools[0], out)
    return out


def pages(n, per=3, start=900, cid=CID, slug=SLUG):
    """n쪽. 최근 것부터(번호가 큰 것부터). 날짜는 쪽마다 한 달씩 앞으로."""
    out, ident = [], start
    for p in range(n):
        month = 11 - p
        out.append([fake_cime.list_item(ident - i, cid, slug, NAME, opened=f"2026-{month:02d}-0{1 + i}T03:00:00.000Z")
                    for i in range(per)])
        ident -= per
    return out


def site(tmp_path, server, tools, exts, monkeypatch, files=None):
    from test_web import Web

    monkeypatch.setenv(cm.BASE_ENV, server.base + fake_cime.PREFIX)
    monkeypatch.delenv(cm.ENV_FORBID, raising=False)
    if files is not None:
        fake_cime.serve_media(server, files)
    fake_cime.serve_lists(server)
    server.cime_channels[CID] = {"slug": SLUG, "name": NAME}
    w = Web(tmp_path, server, tools, extensions=exts)
    w.h.store.set_platform_enabled("cime", True)
    return w


def collector_of(w):
    return w.app.state.extensions.contexts["downloads"].lists.collector


def wait_done(w, platform="cime", channel=CID):
    return w.wait(lambda: (lambda c: c if c["status"] in ("done", "failed") else None)(
        collector_of(w).summary(channel, platform)), timeout=30, what="수집")


def add_cime(w, text=f"https://ci.me/@{SLUG}"):
    return w.call("POST", "/api/replay/channels", json={"channel_id": text, "platform": "cime"})


# ---- 쪽 읽기 ----
def test_parse_page_rows_next_and_adult():
    items = [fake_cime.list_item(5, CID, SLUG, NAME), fake_cime.list_item(4, CID, SLUG, NAME, adult=True),
             fake_cime.list_item(3, OTHER_CID, OTHER_SLUG, "남"), fake_cime.list_item(2, CID, SLUG, NAME, state="PROCESSING"),
             {"id": "x"}]
    body = {"code": 200, "data": {"section": {"type": "VIDEO", "items": items}, "continuation": "tok-1", "hasMoreData": True}}
    data = cl.parse_page(200, body, None, CID)
    assert [v["video_no"] for v in data["videos"]] == ["5", "4"]  # 다른 채널·처리 중·꼴이 틀린 줄은 버린다
    first, adult = data["videos"]
    assert first == {"video_no": "5", "title": "씨미 영상 5", "video_type": "replay", "publish_date": "2026-11-13 21:00:00",
                     "month": "2026-11", "duration": 3600, "adult": False, "thumbnail": "https://streaming.cf.ci.me/ivs/5/thumb.jpg"}
    assert adult["adult"] is True and adult["thumbnail"] is None
    assert data["next"] == "tok-1" and data["channel_name"] == NAME and data["total_pages"] is None
    last = cl.parse_page(200, {"code": 200, "data": {"section": {"items": []}, "continuation": "z", "hasMoreData": False}}, "z", CID)
    assert last["next"] is None and last["videos"] == []
    with pytest.raises(api.catalog.ListError):
        cl.parse_page(200, {"code": 200, "data": "bad"}, None, CID)


# ---- 수집기 단위: 쪽 사이 간격, 창구, 재시도와 403, 전체 수집 뒤 정리 ----
class Gate:
    def __init__(self):
        self.ranks = []

    def slot(self, rank):
        gate = self

        class Slot:
            async def __aenter__(self):
                gate.ranks.append(rank)

            async def __aexit__(self, *exc):
                return False
        return Slot()


def unit(tmp_path, answers):
    store = Store(tmp_path / "state")
    calls, sleeps = [], []

    async def fetch(channel, cursor):
        calls.append(cursor)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def sleep(seconds):
        sleeps.append(seconds)

    provider = api.ListProvider(platform="cime", extension="cime_media", name="씨미", subject="씨미가", fetch=fetch,
                                parse=lambda s, p, c, ch: cl.parse_page(s, p, c, ch), video_url=lambda n, c="": n)
    gate = Gate()
    c = Collector(store.db, {"cime": provider}, sleep=sleep, jitter=lambda: 1.0, default="cime", gate=lambda pid: gate)
    return store, c, calls, sleeps, gate


def body(items, token=None):
    return {"code": 200, "data": {"section": {"type": "VIDEO", "items": items}, "continuation": token, "hasMoreData": bool(token)}}


def test_collect_follows_next_marks_spaces_pages_and_uses_the_gate(tmp_path):
    ps = pages(3)
    answers = [(200, body(ps[0], "tok-1")), (200, body(ps[1], "tok-2")), (200, body(ps[2]))]
    store, c, calls, sleeps, gate = unit(tmp_path, answers)
    asyncio.run(c.collect(CID, "full"))
    assert calls == [None, "tok-1", "tok-2"]
    assert sleeps == [4.0, 4.0]  # 3초 + 0~2초(여기서는 1초), 첫 쪽 앞에서는 쉬지 않는다
    assert gate.ranks == [2, 2, 2]  # 모든 요청이 씨미 창구의 EXTRA 줄로
    s = c.summary(CID)
    assert s["status"] == "done" and s["count"] == 9 and s["pages_done"] == 3 and s["total_pages"] is None
    rows = store.db.execute("SELECT platform, channel FROM catalog_videos").fetchall()
    assert {tuple(r) for r in rows} == {("cime", CID)}
    store.close()


def test_retry_then_forbidden_and_cleanup_after_full(tmp_path):
    ps = pages(2)
    answers = [(429, None), (503, None), (200, body(ps[0], "tok-1")), api.catalog.FetchError("x"), (200, body(ps[1]))]
    store, c, calls, sleeps, _ = unit(tmp_path, answers)
    asyncio.run(c.collect(CID, "full"))
    assert c.summary(CID)["status"] == "done" and sleeps[:2] == [BACKOFF[0], BACKOFF[1]]
    assert c.summary(CID)["count"] == 6
    # 다음 전체 수집에서 사라진 영상은 지운다(끝까지 성공했을 때만)
    c.request(CID, "full")
    answers += [(200, body(ps[0][:2]))]
    asyncio.run(c.collect(CID, "full"))
    assert c.summary(CID)["count"] == 2
    # 403은 바로 실패하고 받은 줄은 남는다(정리하지 않는다)
    answers += [(403, None)]
    asyncio.run(c.collect(CID, "full"))
    s = c.summary(CID)
    assert s["status"] == "failed" and "씨미가 목록 요청을 거부했습니다(HTTP 403)" in s["error"] and s["count"] == 2
    store.close()


# ---- 화면 경로 ----
def test_cime_only_add_collect_months_select_and_state(tmp_path, server, tools, monkeypatch, cime_files):
    """cime_media만 켠 경우(replay 없음): 목록 보기가 있고, 주소로 채널을 더해 모으고 년·월로 보고 골라 받는다."""
    w = site(tmp_path, server, tools, ["downloads", "cime_media"], monkeypatch, files=cime_files)
    server.cime_list[SLUG] = pages(2, per=2, start=502)  # 502, 501 / 500, 499
    server.cime_vods["501"] = {"title": "지난 방송 제목", "channel": (CID, SLUG, NAME), "liveOpenedAt": "2026-11-02T03:00:00Z"}
    with w:
        w.login()
        collector_of(w).sleep = instant
        r = w.client.get("/api/replay/channels")
        assert r.status_code == 200 and [p["platform"] for p in r.json()["providers"]] == ["cime"]
        r = add_cime(w)
        assert r.status_code == 201 and r.json() == {"channel_id": CID, "name": NAME}, r.text
        assert add_cime(w, "https://evil.example/@x").status_code == 400
        chans = w.client.get("/api/replay/channels").json()["channels"]
        assert [(c["platform"], c["channel_id"], c["platform_name"]) for c in chans] == [("cime", CID, "씨미")]
        r = w.call("POST", f"/api/replay/channels/{CID}/collect?platform=cime", json={"mode": "auto"})
        assert r.status_code == 200 and r.json()["started"] == "full"
        assert wait_done(w)["count"] == 4
        reqs = server.cime_list_requests
        assert [q["query"].get("continuation") for q in reqs] == [None, "tok-1"]
        assert all(q["query"]["limit"] == "30" and q["query"]["sort"] == "RECENT" and q["slug"] == SLUG for q in reqs)
        months = w.client.get(f"/api/replay/channels/{CID}/months?platform=cime").json()["months"]
        assert months == [{"month": "2026-11", "count": 2}, {"month": "2026-10", "count": 2}]
        videos = w.client.get(f"/api/replay/channels/{CID}/videos?platform=cime&month=2026-11").json()["videos"]
        assert [v["video_no"] for v in videos] == ["501", "502"] or [v["video_no"] for v in videos] == ["502", "501"]
        v501 = next(v for v in videos if v["video_no"] == "501")
        assert v501["url"] == f"https://ci.me/@{SLUG}/vods/501" and v501["state"] is None
        # 고른 영상은 "씨미 다시보기" 작업으로 들어가고, 받으면 받은 영상 표시가 붙는다(플랫폼·종류·ID)
        r = w.call("POST", "/api/replay/select", json={"items": [{"video_no": "501", "title": "지난 방송 제목", "platform": "cime",
                                                                     "channel_id": CID}]})
        assert r.status_code == 200 and r.json()["queued"] == 1, r.text
        job = w.wait(lambda: next((j for j in w.client.get("/api/replay/jobs").json()["jobs"] if j["status"] == "completed"), None),
                     timeout=120, what="받기")
        assert job["kind"] == "cime_vod" and job["url"] == f"https://ci.me/@{SLUG}/vods/501"
        videos = w.client.get(f"/api/replay/channels/{CID}/videos?platform=cime&month=2026-11").json()["videos"]
        assert next(v for v in videos if v["video_no"] == "501")["state"] == "downloaded"
        # 지운 채널은 녹화 채널이어도 다시 들어오지 않는다(치지직과 같다)
        assert w.call("DELETE", f"/api/replay/channels/{CID}?platform=cime").status_code == 200
        w.h.store.add_channel(CID, NAME, "c1", platform="cime")
        assert w.client.get("/api/replay/channels").json()["channels"] == []


def test_chzzk_and_cime_channels_mix_and_platform_off(tmp_path, server, tools, monkeypatch, vod_media_for_mix):
    """replay와 cime_media를 함께 켜면 한 화면에 섞인다. 씨미를 끄면 씨미 공급자가 빠진다. 녹화 채널은 스스로 들어온다."""
    FakeVod(server, vod_media_for_mix)
    w = site(tmp_path, server, tools, ["downloads", "replay", "cime_media"], monkeypatch)
    w.h.add_channel()  # 치지직 녹화 채널
    w.h.store.add_channel(CID, NAME, "c1", platform="cime")  # 씨미 녹화 채널
    cm.Handles(w.h.cfg.data_dir).set(CID, SLUG)
    with w:
        w.login()
        info = w.client.get("/api/replay/channels").json()
        assert [p["platform"] for p in info["providers"]] == ["chzzk", "cime"]
        assert {(c["platform"], c["channel_id"], c["recording"]) for c in info["channels"]} == {("chzzk", CHANNEL, True), ("cime", CID, True)}
        assert next(p for p in info["providers"] if p["platform"] == "chzzk")["video_types"] is True
        # 치지직 채널의 기본 경로(플랫폼 질의 없음)는 예전과 같다
        assert w.client.get(f"/api/replay/channels/{CHANNEL}/months").status_code == 200
        assert w.client.get(f"/api/replay/channels/{CID}/months").status_code == 404  # 씨미 채널은 platform=cime으로
        w.h.store.set_platform_enabled("cime", False)
        info = w.client.get("/api/replay/channels").json()
        assert [p["platform"] for p in info["providers"]] == ["chzzk"] and {c["platform"] for c in info["channels"]} == {"chzzk"}
        assert w.client.get(f"/api/replay/channels/{CID}/months?platform=cime").status_code == 404


@pytest.fixture(scope="module")
def vod_media_for_mix(tools, tmp_path_factory):
    out = tmp_path_factory.mktemp("mix_media")
    build_media(tools[0], out)
    return out


def test_replay_only_has_no_cime_provider_and_page_fragment_once(tmp_path, server, tools, monkeypatch):
    from webui_files import index_html

    w = site(tmp_path, server, tools, ["downloads", "replay"], monkeypatch)
    with w:
        w.login()
        assert [p["platform"] for p in w.client.get("/api/replay/channels").json()["providers"]] == ["chzzk"]
        r = w.call("POST", "/api/replay/channels", json={"channel_id": f"@{SLUG}", "platform": "cime"})
        assert r.status_code == 404
    both = index_html(["downloads", "replay", "cime_media"])
    assert both.count('id="replay-list-pane"') == 1  # 두 공급자가 같은 조각을 밝혀도 한 번만
    assert 'id="replay-list-pane"' in index_html(["downloads", "cime_media"])
    assert 'id="replay-list-pane"' not in index_html(["downloads", "clips"])


def test_cookie_is_sent_and_screen_marks_platform():
    from webui_files import all_js

    js = all_js()
    assert "vl-add-platform" in js and "[${c.platform_name}]" in js  # 플랫폼 표지
    assert "연령 제한 영상입니다" in js
    src = Path(cl.__file__).read_text(encoding="utf-8")
    assert 'credentials.load, self.cfg, "cime"' in src and "site_headers(cookies" in src  # 로그인 쿠키를 싣는다
