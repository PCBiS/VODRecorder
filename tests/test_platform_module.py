"""플랫폼 접점.

- 가짜 플랫폼(tests/fake_platform.py)으로 채널 추가 → 방송 확인 → 녹화 → 주소 갱신 → 종료 → 마무리를 치지직 코드 없이 끝낸다
- 플랫폼 모듈 밖의 제품 코드에 치지직 모듈 import, 치지직 주소·호스트·쿠키 이름·응답 필드 이름이 없다
- 예전 녹화·다시보기 DB가 그대로 열리고(사본 0600, 한 번만), 실패하면 원래대로 둔 채 뜨지 않는다
- 기존 쿠키 파일은 치지직 몫으로 읽힌다. `{platform}` 토큰
"""

import ast
import asyncio
import json
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

import fake_platform
from conftest import CHANNEL, Harness
from live_recorder.core import naming, secretstore
from live_recorder.core import store as st
from live_recorder.platforms import credentials, registry
from live_recorder.ext.downloads import store as replay_store
from webui_files import index_html, all_js

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "live_recorder"
CHZZK_DIR = PKG / "platforms" / "chzzk"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_ID = "fp000123"


@pytest.fixture
def fake(server, monkeypatch):
    """가짜 플랫폼을 등록하고, 수신기 프로세스가 이 모듈을 불러올 수 있게 시험 폴더를 PYTHONPATH에 넣는다."""
    seen = fake_platform.serve(server)
    monkeypatch.setattr(fake_platform.PLATFORM, "base", server.base)
    tests_dir = str(Path(__file__).resolve().parent)
    monkeypatch.setenv("PYTHONPATH", tests_dir + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""))
    registry.register(fake_platform.PLATFORM)
    yield seen
    registry.unregister(fake_platform.PLATFORM.id)


class ChzzkCallTrace:
    """치지직 모듈(platforms/chzzk/)의 함수가 한 번이라도 불리면 적는다. 모든 스레드에 건다."""

    def __init__(self):
        self.calls: list[str] = []
        self.prefix = str(CHZZK_DIR)

    def __call__(self, frame, event, arg):
        if event == "call" and frame.f_code.co_filename.startswith(self.prefix):
            self.calls.append(f"{Path(frame.f_code.co_filename).name}:{frame.f_code.co_name}")

    def __enter__(self):
        sys.setprofile(self)
        threading.setprofile_all_threads(self)
        return self

    def __exit__(self, *exc):
        threading.setprofile_all_threads(None)
        sys.setprofile(None)


def test_fake_platform_records_without_touching_chzzk_code(tmp_path, server, tools, fake, monkeypatch):
    """가짜 플랫폼 채널을 녹화한다. 엔진·수신기·마무리는 접점만 쓰고 치지직 코드는 한 줄도 돌지 않는다."""
    probe = tmp_path / "probe.jsonl"
    monkeypatch.setenv("FAKE_PLATFORM_PROBE", str(probe))
    h = Harness(tmp_path, server, tools)
    credentials.save(h.cfg, "fakeplat", {"FP_TOKEN": "fp-secret-token-1"})
    h.store.set_settings({"timemachine_enabled": True, "rewind_seconds": 0})  # 켜 두어도 타임머신이 없는 플랫폼에는 쓰지 않는다
    b = server.go_live(FAKE_ID, live_id="7001", initial=20, rate=10)
    server.requests.clear()

    async def scenario():
        async with h:
            ch = await h.engine.add_channel(channel_id=FAKE_ID, name="가짜채널", output_dir="fake", platform="fakeplat")
            assert ch.platform == "fakeplat"
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.5)
            server.expire_tokens()  # 주소 갱신: 수신기가 가짜 플랫폼의 방법으로 새 주소를 받는다
            await h.until(lambda: '"event": "url_refresh"' in h.log_text(), what="주소 갱신")
            await asyncio.sleep(0.5)
            b.end()
            await h.until(lambda: h.finalized(1), timeout=40, what="마무리")

    import live_recorder.platforms.chzzk  # noqa: F401  (불러오기만. 아래에서 함수가 불리는지 본다)
    with ChzzkCallTrace() as trace:
        asyncio.run(scenario())
    assert trace.calls == [], trace.calls[:20]

    s = h.sessions()[0]
    assert s["platform"] == "fakeplat" and s["source"] == "live" and s["finalize_status"] == "done", s
    assert s["url_refreshes"] >= 1 and Path(s["final_path"]).exists() and Path(s["final_path"]).stat().st_size > 0
    # 수신기 프로세스: 가짜 플랫폼 모듈로 주소를 갱신했고 치지직 모듈은 불려 오지도 않았다. 자격은 작업(stdin)으로 받았다
    lines = [json.loads(x) for x in probe.read_text(encoding="utf-8").splitlines()]
    assert lines and all(x["chzzk_modules"] == [] and x["token"] for x in lines), lines
    # 치지직 API(방송 확인·타임머신)는 부르지 않았다. 가짜 API는 자격을 자기 머리글로 받았다
    assert not [r for r in server.requests if r["path"].startswith("/api/")], server.requests[:5]
    assert fake and all(x["channel"] == FAKE_ID and x["token"] == "fp-secret-token-1" for x in fake)
    log = h.log_text()
    assert "timemachine_fallback" not in log and "fp-secret-token-1" not in log
    record_start = next(json.loads(x) for x in log.splitlines() if '"event": "record_start"' in x)
    assert record_start["platform"] == "fakeplat" and record_start["channel_id"] == FAKE_ID


def test_fake_platform_through_web_api(tmp_path, server, tools, fake):
    """화면 API: 플랫폼 목록, (플랫폼, 채널 ID) 채널, 타임머신 표시 감춤, 플랫폼별 쿠키, CSP."""
    from test_web import Web

    with Web(tmp_path, server, tools) as site:
        site.login()
        info = site.client.get("/api/platforms").json()
        # 유튜브는 들어 있는 플랫폼이라 늘 등록돼 있다
        # 씨미도 들어 있는 플랫폼이다
        assert info["default"] == "chzzk" and [p["id"] for p in info["platforms"]] == ["chzzk", "youtube", "cime", "fakeplat"]
        fp = info["platforms"][3]
        assert fp["features"] == {"timemachine": False, "rewind": False} and fp["credentials"]["names"] == ["FP_TOKEN"]
        assert "https://*.fake-thumbs.example" in site.client.get("/").headers["content-security-policy"]
        # 채널 추가: 플랫폼의 채널 ID 꼴로 검사한다
        assert site.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "x", "output_dir": "a",
                                                       "platform": "fakeplat"}).status_code == 400
        assert site.call("POST", "/api/channels", json={"channel_id": FAKE_ID, "name": "x", "output_dir": "a"}).status_code == 400
        assert site.call("POST", "/api/channels", json={"channel_id": FAKE_ID, "name": "x", "output_dir": "a",
                                                       "platform": "nope"}).status_code == 404
        r = site.call("POST", "/api/channels", json={"channel_id": FAKE_ID, "name": "가짜", "output_dir": "a", "platform": "fakeplat",
                                                    "auto_record": False})
        assert r.status_code == 201 and r.json() == {"channel_id": FAKE_ID, "platform": "fakeplat"}
        r = site.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "치지직", "output_dir": "b", "auto_record": False})
        assert r.json() == {"channel_id": CHANNEL, "platform": "chzzk"}  # 플랫폼을 적지 않으면 치지직(예전과 같은 요청)
        rows = {c["platform"]: c for c in site.client.get("/api/channels").json()["channels"]}
        assert rows["fakeplat"]["timemachine"] is False and rows["chzzk"]["timemachine"] is True
        assert rows["fakeplat"]["platform_name"] == "가짜 플랫폼"
        # 기본 플랫폼이 아닌 채널은 ?platform=으로 가리킨다
        assert site.call("PATCH", f"/api/channels/{FAKE_ID}", json={"name": "y"}).status_code == 404
        assert site.call("PATCH", f"/api/channels/{FAKE_ID}?platform=fakeplat", json={"name": "새 이름"}).status_code == 200
        assert site.h.store.get_channel(FAKE_ID, "fakeplat").name == "새 이름"
        # 기록: 타임머신이 없는 플랫폼의 세션에는 화면이 수신 방식을 내지 않도록 표시한다
        site.h.store.create_session(channel_id=FAKE_ID, live_id="7001", title="가짜 방송", source="live",
                                    part_path=str(tmp_path / "x.part"), output_path=str(tmp_path / "x.mp4"), platform="fakeplat")
        sessions = site.client.get("/api/sessions").json()["sessions"]
        assert sessions[0]["platform"] == "fakeplat" and sessions[0]["timemachine"] is False
        # 쿠키: 플랫폼별 파일과 이름. 기본(치지직) 응답 모양은 예전과 같다
        assert site.client.get("/api/cookies").json() == {"NID_AUT": False, "NID_SES": False, "last_check": None}
        assert site.call("PUT", "/api/cookies?platform=fakeplat", json={"NID_AUT": "x"}).status_code == 422
        saved = site.call("PUT", "/api/cookies?platform=fakeplat", json={"FP_TOKEN": "tok-1"}).json()
        assert saved == {"FP_TOKEN": True, "last_check": None}
        assert credentials.load(site.h.cfg, "fakeplat") == {"FP_TOKEN": "tok-1"} and credentials.load(site.h.cfg, "chzzk") == {}
        assert oct(os.stat(credentials.path(site.h.cfg, "fakeplat")).st_mode & 0o777) == "0o600"
        assert site.call("POST", "/api/cookies/check?platform=fakeplat").status_code == 404  # 확인 기능이 없는 플랫폼
        assert site.call("DELETE", "/api/cookies?platform=fakeplat").json() == {"FP_TOKEN": False, "last_check": None}
        assert site.call("DELETE", f"/api/channels/{FAKE_ID}?platform=fakeplat").status_code == 200
        assert site.h.store.get_channel(FAKE_ID, "fakeplat") is None and site.h.store.get_channel(CHANNEL) is not None


def test_ui_hides_timemachine_for_platforms_without_it():
    """화면: 기록의 수신 방식은 타임머신이 있는 플랫폼에만, 설정의 타임머신 묶음은 쓰는 플랫폼이 있을 때만 보인다."""
    js = all_js()
    assert 'if (s.timemachine !== false) facts.splice(1, 0, ["수신 방식"' in js
    assert '$("settings-timemachine").hidden = platforms.length > 0 && !platforms.some((p) => p.features.timemachine);' in js
    html = index_html()
    block = html[html.index('<div id="settings-timemachine">'):]
    assert block.index('name="timemachine_enabled"') < block.index("</div>\n      </div>")


# ---- import 검사와 문자열 검사 ----
# 플랫폼 모듈 밖에서 치지직을 아는 곳은 다시보기·클립 확장의 치지직 전용 모듈뿐이다
EXEMPT_DIRS = (PKG / "platforms",)
# 다시보기·클립 확장의 치지직 전용 모듈(영상 API 주소·응답 해석·주소 인식, 채널 목록 API, 네이버 서명 HLS).
# 플랫폼 모듈 밖에서 치지직을 아는 곳은 이 목록뿐이다
CHZZK_MODULES = (
    PKG / "ext" / "replay" / "chzzk_video.py", PKG / "ext" / "replay" / "catalog.py", PKG / "ext" / "replay" / "channel_list.py",
    PKG / "ext" / "replay" / "static" / "replay-list.js",
    PKG / "ext" / "clips" / "chzzk_clip.py", PKG / "ext" / "clips" / "clips.py",
    PKG / "ext" / "downloads" / "hls_transport.py",
)


def product_files(suffixes=(".py",)):
    for path in sorted(PKG.rglob("*")):
        if path.suffix in suffixes and path.is_file() and not any(path.is_relative_to(d) for d in EXEMPT_DIRS) \
                and path not in CHZZK_MODULES:
            yield path


def test_core_does_not_import_the_chzzk_module():
    """녹화 중심부·웹 서버·도구는 치지직 모듈을 import하지 않는다. 플랫폼은 registry로만 얻는다."""
    bad = []
    for path in product_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        pkg = path.relative_to(PKG.parent).with_suffix("").parts[:-1]
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = ".".join(pkg[:len(pkg) - node.level + 1] + ((node.module,) if node.module else ()))
                names = [base] + [f"{base}.{a.name}" for a in node.names]
            for name in names:
                if name.startswith("live_recorder.platforms.chzzk") or name in ("live_recorder.chzzk", "live_recorder.cookies"):
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
    assert bad == [], bad
    # 수신기 본체도 플랫폼별 분기 없이 접점만 쓴다
    receiver = (PKG / "recording" / "receiver.py").read_text(encoding="utf-8")
    assert "registry.load_module_platform" in receiver and "chzzk" not in receiver.lower()


# 치지직 주소·호스트·쿠키 이름·응답 필드 이름·조각 토큰 인자
CHZZK_STRINGS = (
    "chzzk.naver.com", "naver.com", "pstatic.net", "akamaized.net", "NID_AUT", "NID_SES",
    "livePlaybackJson", "liveId", "liveTitle", "liveCategoryValue", "liveImageUrl", "openDate", "timeMachineActive",
    "encodingTrack", "videoHeight", "loggedIn", "userIdHash", "hdnts", "__bgda__", "live-detail", "time-machine", "user-status",
)


def test_no_chzzk_strings_outside_the_platform_module():
    """플랫폼 모듈 밖의 제품 코드(파이썬·화면)에 치지직 주소·호스트·쿠키 이름·응답 필드 이름이 없다."""
    bad = []
    for path in product_files((".py", ".js", ".html", ".css")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            hits = [s for s in CHZZK_STRINGS if s in line]
            if hits:
                bad.append(f"{path.relative_to(ROOT)}:{n} {hits}")
    assert bad == [], "\n".join(bad)
    # 검사가 실제로 무언가를 본다: 치지직 모듈 안에는 있다
    inside = (CHZZK_DIR / "api.py").read_text(encoding="utf-8") + (CHZZK_DIR / "platform.py").read_text(encoding="utf-8")
    assert all(s in inside for s in ("chzzk.naver.com", "NID_AUT", "livePlaybackJson", "hdnts", "__bgda__", "akamaized.net"))


def test_interface_is_one_file_of_protocols():
    """접점은 한 파일(platforms/base.py)에 Protocol로 적는다. 치지직 모듈은 그 접점을 만족한다."""
    from live_recorder.platforms import base

    chzzk = registry.get("chzzk")
    assert isinstance(chzzk, base.Platform) and isinstance(fake_platform.PLATFORM, base.Platform)
    assert chzzk.id == "chzzk" and chzzk.name == "치지직" and chzzk.features.timemachine
    assert chzzk.credential_spec.names == ("NID_AUT", "NID_SES")
    assert isinstance(chzzk.client(type("C", (), {"allow_http": False})()), base.PlatformClient)
    assert chzzk.parse_channel("https://chzzk.naver.com/live/" + CHANNEL) == CHANNEL
    with pytest.raises(base.ChannelParseError):
        chzzk.parse_channel("https://evil.example/" + CHANNEL)


# ---- 플랫폼 열이 없던 DB와 쿠키 파일 ----
def load_fixture(name: str, db_path: Path, rec_root: Path) -> None:
    sql = (FIXTURES / name).read_text(encoding="utf-8").replace("/tmp/tmpqawzta0z/rec", str(rec_root))
    db = sqlite3.connect(db_path)
    db.executescript(sql)
    db.close()


def dump(path: Path) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return list(db.iterdump())
    finally:
        db.close()


def test_b12_recorder_db_opens_with_platform(tmp_path, caplog):
    """플랫폼 열이 없던 녹화 DB(형식 3)가 그대로 열린다. 옮기기 전 사본(0600, 한 번만)을 남기고 기존 줄은 치지직이 된다."""
    rec = tmp_path / "rec"
    (rec / "ch1").mkdir(parents=True)
    (rec / "ch2").mkdir()
    db_path = tmp_path / "recorder.sqlite3"
    load_fixture("b12_recorder.sql", db_path, rec)
    before = dump(db_path)
    account = tmp_path / "secrets" / "account.json"
    secretstore.create_account(account, "admin", "correct-horse-battery-1")
    caplog.set_level("INFO", logger="live_recorder.store")
    store = st.Store(db_path, rec)
    try:
        backup = tmp_path / "recorder.sqlite3.schema3.bak"
        assert backup.exists() and oct(os.stat(backup).st_mode & 0o777) == "0o600"
        assert dump(backup) == before  # 사본은 옮기기 전 그대로
        chans = store.list_channels()
        assert [(c.platform, c.channel_id, c.name, c.extension) for c in chans] == [
            ("chzzk", CHANNEL, "녹화채널", ".mp4"), ("chzzk", "f" * 32, "둘째채널", ".ts")]
        assert store.get_channel(CHANNEL).quality == "1080p"
        sessions = store.list_sessions(None, 10)
        assert [(s["id"], s["platform"], s["title"], s["source"]) for s in sessions] == [(1, "chzzk", "옛 방송", "timemachine")]
        assert store.get_state(CHANNEL)["platform"] == "chzzk"
        settings = store.get_settings()
        assert settings.rewind_seconds == 60 and settings.filename_date_format == "YYMMDD" and settings.check_interval == 30
        assert store._one("SELECT value FROM meta WHERE key='schema_version'")[0] == str(st.SCHEMA_VERSION)
        # 같은 채널 ID라도 플랫폼이 다르면 다른 채널이다. 새 세션 번호는 이어진다
        assert store.create_session(channel_id=CHANNEL, live_id="9002", title="새 방송", source="live",
                                    part_path=str(rec / "ch1" / "b.part"), output_path=str(rec / "ch1" / "b.mp4")) == 2
    finally:
        store.close()
    events = [r.event for r in caplog.records if hasattr(r, "event")]
    assert events.count("store_backup") == 1 and events.count("store_migrated") == 1
    assert secretstore.account_exists(account)  # 계정은 저장소 밖(0600 파일)이라 그대로다
    # 다시 열어도 아무 일도 없다(사본도 다시 만들지 않는다)
    caplog.clear()
    stamp = os.stat(backup).st_mtime_ns
    after_first = dump(db_path)
    st.Store(db_path, rec).close()
    assert dump(db_path) == after_first and os.stat(backup).st_mtime_ns == stamp
    assert not [r for r in caplog.records if getattr(r, "event", "").startswith("store_")]


def test_b12_recorder_db_migration_failure_leaves_it_untouched(tmp_path, caplog):
    """옮기다 실패하면 한 트랜잭션을 되돌려 원래대로 두고, 서버는 뜨지 않으며 이유를 로그에 남긴다."""
    rec = tmp_path / "rec"
    rec.mkdir()
    db_path = tmp_path / "data" / "recorder.sqlite3"
    db_path.parent.mkdir()
    load_fixture("b12_recorder.sql", db_path, rec)
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE channel_state_v4 (x)")  # 옮기기 도중(채널 표를 옮긴 다음)에 실패하게 만든다
    db.commit()
    db.close()
    before = dump(db_path)
    caplog.set_level("INFO", logger="live_recorder.store")
    with pytest.raises(st.StoreError) as e:
        st.Store(db_path, rec)
    assert "원래대로 두었습니다" in str(e.value)
    assert dump(db_path) == before  # 채널 표도 옮기기 전 그대로
    failed = [r for r in caplog.records if getattr(r, "event", "") == "store_migration_failed"]
    assert len(failed) == 1 and failed[0].from_version == 3 and failed[0].reason == "OperationalError"
    # 서버 명령도 뜨지 않는다(저장소 오류 종료코드 2)
    from live_recorder import cli
    assert cli.main(["--data-dir", str(db_path.parent), "--record-root", str(rec), "status"]) == 2


def test_b12_replay_db_opens_with_platform(tmp_path, caplog):
    """플랫폼 열이 없던 다시보기 DB가 그대로 열린다. 수집 목록·다시보기 채널·받은 기록·작업이 남고 치지직 몫이 된다."""
    state = tmp_path / "replay"
    state.mkdir()
    load_fixture("b12_replay.sql", state / "replay.sqlite3", tmp_path / "rec")
    before = dump(state / "replay.sqlite3")
    caplog.set_level("INFO", logger="live_recorder.replay.store")
    s = replay_store.Store(state)
    try:
        backup = state / "replay.sqlite3.schema1.bak"
        assert backup.exists() and oct(os.stat(backup).st_mode & 0o777) == "0o600" and dump(backup) == before
        q = lambda sql: [tuple(r) for r in s.db.execute(sql)]  # noqa: E731
        assert q("SELECT platform, channel, name FROM replay_channels ORDER BY channel") == [
            ("chzzk", CHANNEL, "녹화채널"), ("chzzk", "e" * 32, "다시보기전용")]
        assert q("SELECT platform, channel FROM replay_channels_removed") == [("chzzk", "d" * 32)]
        assert q("SELECT platform, channel, video, title FROM catalog_videos") == [("chzzk", CHANNEL, "100", "옛 수집")]
        assert q("SELECT platform, channel, status FROM catalog_channels") == [("chzzk", CHANNEL, "done")]
        assert q("SELECT platform, video, filename FROM downloads") == [("chzzk", "777", "old.mp4")]
        assert q("SELECT platform, id, status FROM records") == [("chzzk", "a" * 32, "completed")]
        assert s.get("job", "admin", "a" * 32)["filename"] == "old.mp4"
        assert s.db.execute("PRAGMA user_version").fetchone()[0] == replay_store.SCHEMA_VERSION
    finally:
        s.close()
    events = [r.event for r in caplog.records if hasattr(r, "event")]
    assert events.count("store_backup") == 1 and events.count("store_migrated") == 1
    after = dump(state / "replay.sqlite3")
    stamp = os.stat(backup).st_mtime_ns
    caplog.clear()
    replay_store.Store(state).close()
    assert dump(state / "replay.sqlite3") == after and os.stat(backup).st_mtime_ns == stamp
    assert not [r for r in caplog.records if getattr(r, "event", "").startswith("store_")]


def test_b12_replay_db_migration_failure_stops_the_server(tmp_path, server, tools, caplog):
    """다시보기 DB를 옮기지 못하면 원래대로 두고, 서버(웹)는 뜨지 않는다."""
    from fastapi.testclient import TestClient

    from live_recorder import web

    h = Harness(tmp_path, server, tools)
    (h.root / "replay").mkdir()
    h.store.set_extra({"ext:enabled": ["downloads", "replay", "clips"], "ext:downloads:replay_dir": "replay"})
    state = h.cfg.data_dir / "replay"
    state.mkdir()
    load_fixture("b12_replay.sql", state / "replay.sqlite3", h.root)
    db = sqlite3.connect(state / "replay.sqlite3")
    db.execute("CREATE TABLE records_v2 (x)")
    db.commit()
    db.close()
    before = dump(state / "replay.sqlite3")
    app = web.create_app(h.cfg, h.timings)
    with pytest.raises(replay_store.MigrationError):
        with TestClient(app, base_url="http://testserver"):
            pass
    assert dump(state / "replay.sqlite3") == before
    assert any(getattr(r, "event", "") == "store_migration_failed" and getattr(r, "db", "") == "replay" for r in caplog.records)


def test_existing_cookie_file_is_read_as_chzzk(tmp_path):
    """예전 쿠키 파일(secrets/chzzk-cookies.json)은 치지직 몫으로 읽힌다. 다른 플랫폼은 자기 파일을 쓴다."""
    class Cfg:
        data_dir = tmp_path

    old = tmp_path / "secrets" / "chzzk-cookies.json"  # 옛 AppConfig.cookie_path와 같은 자리
    secretstore.write_json(old, {"NID_AUT": "old-aut", "NID_SES": "old-ses"})
    assert credentials.path(Cfg, "chzzk") == old and credentials.path(Cfg, None) == old
    assert credentials.load(Cfg, "chzzk") == {"NID_AUT": "old-aut", "NID_SES": "old-ses"}
    registry.register(fake_platform.PLATFORM)
    try:
        credentials.save(Cfg, "fakeplat", {"FP_TOKEN": "t"})
        assert credentials.path(Cfg, "fakeplat").name == "fakeplat-cookies.json"
        assert credentials.load(Cfg, "chzzk") == {"NID_AUT": "old-aut", "NID_SES": "old-ses"}
    finally:
        registry.unregister("fakeplat")


def test_platform_filename_token():
    """`{platform}` 토큰(플랫폼 식별자). 토큰 표에 나오고 기본 패턴은 그대로다."""
    from datetime import datetime, timezone

    from live_recorder.core.config import Settings

    assert ("platform", "플랫폼(예: chzzk)") in naming.TOKEN_HELP and "{platform}" not in Settings().filename_pattern
    now = datetime(2026, 11, 13, 1, 2, 3, tzinfo=timezone.utc)
    common = dict(title="제목", channel_name="채널", quality="1080p", frame_rate="60", extension=".mp4",
                  broadcast_start=now, recording_start=now, now=now)
    assert naming.render("[{platform}] {safe_live_title}", **common) == "[chzzk] 제목.mp4"
    assert naming.render("[{platform}] {safe_live_title}", platform="fakeplat", **common) == "[fakeplat] 제목.mp4"
    assert naming.token_values(**common)["platform"] == "chzzk"


def test_platform_field_is_kept_in_strict_log_export():
    """로그의 platform 필드는 더 가리기에서도 남는다(프로그램이 정한 값). 문서 표에 있다."""
    from live_recorder import logmask

    assert logmask.FIELDS["platform"] == "code"
    m = logmask.Masker(True, channels=[(CHANNEL, "채널")])
    out = m.record({"ts": "t", "level": "INFO", "logger": "live_recorder.engine", "event": "record_start",
                    "channel_id": CHANNEL, "platform": "chzzk", "msg": "녹화 시작 x"})
    assert out["platform"] == "chzzk" and out["channel_id"] == "채널-1"
    doc = (ROOT / "docs" / "LOG-EXPORT.md").read_text(encoding="utf-8")
    assert "| `platform` |" in doc and re.search(r"\| engine \| `record_start` \|.*`platform`", doc)
