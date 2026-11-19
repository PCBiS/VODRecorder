"""다시보기·클립을 확장으로: 켜고 끄는 조합, 꺼진 확장의 작업, 이전 빌드에서 올라올 때, 처음 설치.

조합: 셋 다 켬 / clips만 끔 / replay만 끔 / 모두 끔. 조합마다 꺼진 확장의 동작과 "꺼진 것의 흔적 없음"을 본다.
클립 분석은 실제 치지직을 부르므로(가짜 서버 없음) 주소 인식과 거절까지만 본다.
"""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from conftest import CHANNEL, REPLAY_EXTENSIONS, Harness
from live_recorder.core import extensions
from live_recorder.core import store as st
from live_recorder.ext.downloads import api
from replay_fake import FakeVod, build_media
from test_web import Web
from webui_files import index_html

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VOD = "https://chzzk.naver.com/video/100"
CLIP = "https://chzzk.naver.com/clips/abcdEFGH1234"
ALL, NO_CLIPS, NO_REPLAY, NONE = REPLAY_EXTENSIONS, ("downloads", "replay"), ("downloads", "clips"), ()
COMBOS = {"all": ALL, "no_clips": NO_CLIPS, "no_replay": NO_REPLAY, "none": NONE}


@pytest.fixture(scope="module")
def vod_media(tools, tmp_path_factory):
    out = tmp_path_factory.mktemp("vodmedia")
    build_media(tools[0], out)
    return out


def site(tmp_path, server, tools, enabled):
    w = Web(tmp_path, server, tools, extensions=list(enabled) or None)
    if not enabled:
        w.h.store.set_extra({extensions.ENABLED_KEY: []})
    return w


@pytest.mark.parametrize("combo", list(COMBOS))
def test_each_combination_behaves_and_leaves_no_trace(tmp_path, server, tools, combo):
    """조합마다 다시보기 탭·설정 하위 탭·API·저장소가 켜진 것만 있다. 녹화·기록·파일 관리자·로그 내려받기는 그대로다."""
    enabled = COMBOS[combo]
    with site(tmp_path, server, tools, enabled) as w:
        w.login()
        html = w.client.get("/").text
        status = w.client.get("/api/replay/status")
        settings = w.client.get("/api/settings").json()["settings"]
        if "downloads" not in enabled:
            # 다시보기 탭, 설정의 다시보기 하위 탭, 관련 API, 백그라운드 일이 없고 다시보기 DB를 열지 않는다
            for marker in ('data-view="replay"', 'id="stab-replay"', 'id="spanel-replay"', "/static/ext/", "다시보기"):
                assert marker not in html, marker
            assert status.status_code == 404
            assert not any(k.startswith("replay_") for k in settings)
            assert w.call("PATCH", "/api/settings", json={"replay_dir": "x"}).status_code == 400
            assert not (w.h.cfg.data_dir / "replay").exists()
            assert w.call("POST", "/api/replay/analyze", json={"urls": [VOD]}).status_code == 404
            assert w.client.get("/api/replay/channels").status_code == 404
            assert [k.kind for k in api.kinds()] == []
        else:
            assert status.status_code == 200 and status.json()["ready"] is True
            assert 'data-view="replay"' in html and 'id="spanel-replay"' in html
            assert settings["replay_dir"] == "replay" and "replay_enabled" not in settings
            assert (w.h.cfg.data_dir / "replay" / "replay.sqlite3").exists()
            kinds = [k["kind"] for k in w.client.get("/api/replay/kinds").json()["kinds"]]
            assert kinds == [k for k, ext in (("video", "replay"), ("clip", "clips")) if ext in enabled]
            # 주소: 켜진 종류는 받고 꺼진 종류는 그 확장이 꺼져 있다고 거절한다
            if "clips" in enabled:
                assert api.parse(CLIP)[1].kind == "clip"
            else:
                r = w.call("POST", "/api/replay/analyze", json={"urls": [CLIP]})
                assert r.status_code == 400 and r.json()["error"] == "클립 확장이 꺼져 있습니다"
            if "replay" in enabled:
                assert api.parse(VOD)[1].kind == "video"
                assert w.client.get("/api/replay/channels").status_code == 200
                assert 'id="replay-mode-list"' in html and 'id="replay-list-pane"' in html
            else:
                r = w.call("POST", "/api/replay/analyze", json={"urls": [VOD]})
                assert r.status_code == 400 and r.json()["error"] == "다시보기 확장이 꺼져 있습니다"
                assert w.client.get("/api/replay/channels").status_code == 404  # 채널 목록 방식과 그 API·수집이 없다
                assert w.call("POST", "/api/replay/select", json={"items": [{"video_no": "1"}]}).status_code == 404
                assert 'id="replay-mode-list"' not in html and 'id="replay-list-pane"' not in html
        # 어느 조합에서도 그대로인 것
        assert w.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "채널", "output_dir": "a"}).status_code == 201
        assert w.client.get("/api/sessions").status_code == 200
        assert w.call("PATCH", "/api/settings", json={"fm_enabled": True}).status_code == 200
        assert w.call("POST", "/api/logs/export", json={"range": "1"}).status_code == 200
        roots = {r["source"] for r in w.client.get("/api/files/roots").json()["roots"]}
        assert ("replay" in roots) == ("downloads" in enabled)
    assert [k.kind for k in api.kinds()] == []  # 서버를 내리면 종류 등록도 풀린다


def test_page_matches_each_combination():
    """화면 문서: 조합마다 넣는 조각이 다르다(채널 목록은 replay, 다시보기 탭·하위 탭은 downloads)."""
    full = index_html(ALL)
    assert 'id="replay-mode-list"' in full and 'id="spanel-replay"' in full and "채널·다시보기 폴더" in full
    no_replay = index_html(NO_REPLAY)
    assert 'id="replay-mode-list"' not in no_replay and 'id="replay-url-pane"' in no_replay
    off = index_html(NONE)
    assert "다시보기" not in off and "lr-slot" not in off and "채널 폴더는 자동으로 들어갑니다" in off


def wait(pred, timeout=60, what="조건"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"시간 안에 만족하지 못했다: {what}")


def test_jobs_of_a_disabled_extension_wait_and_resume(tmp_path, server, tools, vod_media):
    """꺼진 확장의 작업은 지우지 않는다. 목록에 보이되 진행하지 않고 "확장이 꺼져 있어 진행할 수 없습니다"로 표시하며
    취소는 된다. 다시 켜면 이어서 진행한다."""
    fake = FakeVod(server, vod_media)

    def job_of(w, ident):
        return w.client.get(f"/api/replay/jobs/{ident}").json()

    # 1) 셋 다 켜고 전체 일시정지한 채 다시보기 작업 둘을 넣고 일시정지해 둔다(재시작해도 남는 작업,
    #    대기 중인 작업은 재시작하면 중단으로 남는다. 확장을 켜고 끄는 것은 재시작으로만 먹는다)
    with Web(tmp_path, server, tools, extensions=ALL) as w:
        w.login()
        assert w.call("PUT", "/api/replay/control", json={"paused": True}).status_code == 200
        r = w.call("POST", "/api/replay/analyze", json={"urls": [VOD]})
        ident = r.json()["id"]
        a = wait(lambda: (lambda x: x if x["status"] == "completed" else None)(w.client.get(f"/api/replay/analyses/{ident}").json()))
        quality = a["items"][0]["qualities"][-1]["id"]
        jobs = []
        for key in ("blocked-job-0001", "blocked-job-0002"):
            r = w.call("POST", "/api/replay/jobs", json={"analysis_id": a["id"], "items": [{"url": VOD, "quality": quality}],
                                                       "key": key, "confirm": True})
            jobs.append(r.json()["jobs"][0]["id"])
        for j in jobs:
            assert w.call("POST", f"/api/replay/jobs/{j}/pause").json()["status"] == "paused"
    (w.h.cfg.account_path).unlink()  # 같은 데이터 폴더로 다시 띄운다(시험 도구가 계정을 새로 만든다)
    # 2) replay를 끄고(clips만 켬) 띄운다: 작업은 남고 진행하지 않으며 표시가 붙는다. 하나는 취소한다
    with Web(tmp_path, server, tools, extensions=NO_REPLAY) as w:
        w.login()
        assert w.call("PUT", "/api/replay/control", json={"paused": False}).status_code == 200
        time.sleep(1.5)
        listed = {j["id"]: j for j in w.client.get("/api/replay/jobs?status=active").json()["jobs"]}
        assert set(jobs) <= set(listed)
        for j in jobs:
            assert listed[j]["status"] == "paused" and listed[j]["blocked"] == "확장이 꺼져 있어 진행할 수 없습니다"
        assert w.call("POST", f"/api/replay/jobs/{jobs[0]}/resume").status_code == 200  # 재개해도 꺼진 동안은 진행하지 않는다
        time.sleep(1.5)
        assert job_of(w, jobs[0])["status"] == "paused" and job_of(w, jobs[0])["blocked"]
        assert w.call("POST", f"/api/replay/jobs/{jobs[1]}/cancel").json()["status"] == "cancelled"
        assert w.call("POST", f"/api/replay/jobs/{jobs[1]}/retry").status_code == 409  # 꺼진 확장의 작업은 다시 시도하지 않는다
    (w.h.cfg.account_path).unlink()
    # 3) 다시 켜면 이어서 진행한다
    with Web(tmp_path, server, tools, extensions=ALL) as w:
        w.login()
        done = wait(lambda: (lambda j: j if j["status"] in ("completed", "failed") else None)(job_of(w, jobs[0])), timeout=90)
        assert done["status"] == "completed" and "blocked" not in done, done
    assert fake


# ---- 이전 빌드에서 올라올 때와 처음 설치 ----
def b12_data(tmp_path, replay_enabled=None, extra_settings=None):
    data, rec = tmp_path / "data", tmp_path / "rec"
    (rec / "ch1").mkdir(parents=True)
    (rec / "ch2").mkdir()
    data.mkdir()
    db = sqlite3.connect(data / "recorder.sqlite3")
    db.executescript((FIXTURES / "b12_recorder.sql").read_text(encoding="utf-8").replace("/tmp/tmpqawzta0z/rec", str(rec)))
    values = dict(extra_settings or {})
    if replay_enabled is not None:
        values["replay_enabled"] = replay_enabled
    for key, value in values.items():
        db.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (key, json.dumps(value)))
    db.commit()
    db.close()
    (data / "replay").mkdir()
    db = sqlite3.connect(data / "replay" / "replay.sqlite3")
    db.executescript((FIXTURES / "b12_replay.sql").read_text(encoding="utf-8"))
    db.close()
    return data, rec


def test_upgrade_from_b12_turns_on_all_three_and_keeps_everything(tmp_path, server, tools, caplog):
    """예전 빌드에서 올라오면(replay_enabled가 없었으면 그 빌드의 기본값 켜짐) 세 확장이 켜지고 설정·작업·받은 기록·
    수집 목록·다시보기 채널이 유지된다. replay_enabled는 없어지고 옮긴 사실이 로그에 남는다."""
    b12_data(tmp_path, extra_settings={"replay_dir": "옛폴더", "replay_paused": True, "replay_connections": 4,
                                       "replay_window_rules": [{"days": [0], "start": "01:00", "end": "02:00"}]})
    caplog.set_level("INFO", logger="live_recorder.extensions")
    with Web(tmp_path, server, tools) as w:  # Web은 확장 목록을 건드리지 않는다
        w.login()
        store = w.h.store
        assert extensions.enabled_names(store) == ["downloads", "replay", "clips"]
        settings = w.client.get("/api/settings").json()["settings"]
        assert (settings["replay_dir"], settings["replay_paused"], settings["replay_connections"]) == ("옛폴더", True, 4)
        assert settings["replay_window_rules"] == [{"days": [0], "start": "01:00", "end": "02:00"}]
        assert settings["rewind_seconds"] == 60 and settings["check_interval"] == 30  # 중심부 설정도 그대로
        rows = {r[0] for r in store._all("SELECT key FROM settings")}
        assert not {"replay_enabled", "replay_dir", "replay_paused"} & rows  # 옛 키는 없앴다
        channels = {c["channel_id"]: c["name"] for c in w.client.get("/api/replay/channels").json()["channels"]}
        # 녹화 채널(둘째채널)은 스스로 들어오고 관리자가 지운 채널(ddd…)은 다시 들어오지 않는다
        assert channels == {CHANNEL: "녹화채널", "e" * 32: "다시보기전용", "f" * 32: "둘째채널"}
        db = w.app.state.extensions.contexts["downloads"].service.manager.db
        assert [tuple(r) for r in db.execute("SELECT platform, video, filename FROM downloads")] == [("chzzk", "777", "old.mp4")]
        months = w.client.get(f"/api/replay/channels/{CHANNEL}/months").json()["months"]
        assert months == [{"month": "2026-09", "count": 1}]
        assert [c.name for c in store.list_channels()] == ["녹화채널", "둘째채널"]
    moved = [r for r in caplog.records if getattr(r, "event", "") == "extensions_migrated"]
    assert len(moved) == 1 and moved[0].extension == "downloads,replay,clips" and moved[0].from_version == 3
    assert (tmp_path / "data" / "recorder.sqlite3.schema3.bak").exists()


def test_upgrade_with_replay_off_turns_all_off(tmp_path):
    """저장된 replay_enabled가 꺼져 있었으면 세 확장을 모두 끈 것으로 옮긴다. 설정 값은 옮겨 둔다(켜면 이어진다)."""
    data, rec = b12_data(tmp_path, replay_enabled=False, extra_settings={"replay_dir": "옛폴더"})
    store = st.Store(data / "recorder.sqlite3", rec)
    try:
        assert extensions.enabled_names(store) == []
        assert extensions.get_setting(store, "downloads", "replay_dir") == "옛폴더"
        assert store.get_extra("ext:downloads:replay_dir") == "옛폴더"
        with pytest.raises(st.ConfigError):
            store.set_settings({"replay_enabled": True})  # 그 뒤로는 모르는 키다
    finally:
        store.close()


def test_fresh_install_has_all_extensions_off(tmp_path, server, tools):
    """처음 설치하면 세 확장이 모두 꺼져 있다(기본 기능은 녹화만)."""
    h = Harness(tmp_path, server, tools)
    assert extensions.enabled_names(h.store) == []
    assert extensions.read_enabled(h.cfg.db_path) == []
    with Web(tmp_path / "w", server, tools) as w:
        w.login()
        info = w.client.get("/api/extensions").json()
        assert [(x["name"], x["enabled"], x["active"]) for x in info["extensions"]][:3] == [
            ("downloads", False, False), ("replay", False, False), ("clips", False, False)]
        assert w.call("PUT", "/api/extensions", json={"enabled": ["replay"]}).status_code == 400  # 필요한 확장이 꺼져 있다
        assert w.call("PUT", "/api/extensions", json={"enabled": ["downloads", "clips"]}).json()["restart_needed"] is True
        assert w.client.get("/api/replay/status").status_code == 404  # 다음 서버 시작부터 먹는다


# ---- 중심부에 다시보기를 아는 코드가 없다 ----
ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "live_recorder"
CORE = [PKG / "core", PKG / "recording", PKG / "web", PKG / "platforms", PKG / "logexport.py", PKG / "logmask.py",
        PKG / "files.py", PKG / "diagnostics.py", PKG / "webui"]
REPLAY_WORDS = ("replay", "clip", "vod", "다시보기", "클립")


def code_lines(path: Path):
    """주석·문서 문자열을 뺀 코드 줄(파이썬), 주석을 뺀 줄(화면 파일)."""
    import io
    import tokenize

    text = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        for n, line in enumerate(text.splitlines(), 1):
            code = line.split("//")[0] if path.suffix == ".js" else line
            if path.suffix == ".html" and "<!--" in code:
                code = code.split("<!--")[0]
            yield n, code
        return
    prev = None
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev in (None, tokenize.INDENT, tokenize.NEWLINE, tokenize.NL, tokenize.DEDENT):
            prev = tok.type
            continue  # 문서 문자열
        if tok.type in (tokenize.NAME, tokenize.STRING):
            yield tok.start[0], tok.string
        prev = tok.type


def test_core_has_no_replay_knowledge():
    """중심부(core·recording·web·플랫폼·로그 내려받기·파일 관리자·화면 뼈대)의 코드에 다시보기·클립을 아는 곳이 없다.
    확장 이름은 들어 있는 확장 표(core.extensions.BUILTIN, 모듈 이름 문자열)에만 있다."""
    bad = []
    for base in CORE:
        paths = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.suffix in (".py", ".js", ".html"))
        for path in paths:
            for n, code in code_lines(path):
                low = code.lower()
                if any(w in low for w in REPLAY_WORDS):
                    if path.name == "extensions.py" and ("live_recorder.ext." in code or code in ('"replay"', '"clips"')):
                        continue  # BUILTIN 표
                    bad.append(f"{path.relative_to(ROOT)}:{n} {code.strip()[:80]}")
    assert bad == [], "\n".join(bad)
