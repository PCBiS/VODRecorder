"""사건 알림과 확장 등록.

- 사건은 일어난 순서대로 전해지고, 듣는 쪽의 오류·지연이 알리는 쪽(녹화 감시)을 막지 않는다
- 가짜 확장(tests/fake_extension.py)을 켜면 경로·탭·설정 키·저장소·사건 듣기·로그 줄·잠금 대상·채널 정보가 모두 동작하고,
  끄면 흔적이 없다. 가짜 확장이 사건 처리에서 오류를 내고 붙잡아도 녹화와 마무리는 이어진다
- 접근 규칙이 없는 확장 경로, 다른 경로와 겹치는 확장 경로가 있으면 서버가 뜨지 않는다
- 필요한 확장이 꺼져 있으면 켤 수 없다. 켜고 끄는 것은 다음 서버 시작부터 먹는다
"""

import ast
import io
import json
import logging
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

import fake_extension
from conftest import CHANNEL, Harness
from live_recorder import web
from live_recorder.core import events as ev
from live_recorder.core import extensions, logs, secretstore
from test_web import PASSWORD, USER

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "live_recorder"


# ---- 사건 알림 ----
def test_events_keep_order_and_listeners_never_block_the_emitter(caplog):
    bus = ev.EventBus()
    got: list[int] = []
    slow_started = threading.Event()

    def slow(event):
        slow_started.set()
        time.sleep(0.3)

    def failing(event):
        if event.seq % 3 == 0:
            raise ValueError("듣는 쪽 오류")

    bus.subscribe("*", lambda e: got.append(e.seq))
    bus.subscribe("record_started", failing, owner="fake")
    bus.subscribe("record_ended", slow, owner="fake")
    bus.start()
    t0 = time.monotonic()
    sent = []
    for i in range(30):
        sent.append(bus.emit("record_started" if i % 2 else "record_ended", "chzzk", CHANNEL, session_id=i).seq)
    assert time.monotonic() - t0 < 0.1  # 알리는 쪽은 듣는 쪽(0.3초씩 붙잡는다)을 기다리지 않는다
    assert slow_started.wait(2)
    assert bus.drain(15)
    assert got == sent == sorted(sent)  # 일어난 순서대로
    failed = [r for r in caplog.records if getattr(r, "event", "") == "event_listener_failed"]
    assert failed and all(r.event_name == "record_started" and r.extension == "fake" for r in failed)
    with pytest.raises(ValueError):
        bus.emit("no_such_event")
    bus.stop()


def test_event_names_and_values_are_listed():
    """사건 이름과 실리는 값은 core.events.EVENTS 한 곳에 있다. 중심부가 실제로 알리는 사건은 모두 거기에 있다."""
    emitted = set()
    for path in PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "emit" \
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                if "events" in ast.unparse(node.func.value):
                    emitted.add(node.args[0].value)
                    for kw in node.keywords:
                        assert kw.arg in ev.EVENTS[node.args[0].value], (path.name, node.args[0].value, kw.arg)
    assert emitted == set(ev.EVENTS)


# ---- 가짜 확장 ----
@pytest.fixture
def fake_ext():
    fake_extension.EXTENSION.reset()
    extensions.register(fake_extension.EXTENSION)
    extensions.register(fake_extension.CHILD)
    yield fake_extension.EXTENSION
    extensions.unregister("fakeext")
    extensions.unregister("fakechild")


class Site:
    def __init__(self, tmp_path, server, tools, enabled=()):
        self.h = Harness(tmp_path, server, tools)
        if enabled:
            self.h.store.set_extra({extensions.ENABLED_KEY: list(enabled)})
        secretstore.create_account(self.h.cfg.account_path, USER, PASSWORD)
        logs.setup(self.h.cfg.log_dir, console=False)
        self.app = web.create_app(self.h.cfg, self.h.timings, disk_free=self.h.disk_free)
        self.client = TestClient(self.app, base_url="http://testserver", follow_redirects=False)
        self.csrf = ""

    def __enter__(self):
        self.client.__enter__()
        r = self.client.post("/api/login", json={"username": USER, "password": PASSWORD})
        assert r.status_code == 200
        self.csrf = self.client.get("/api/session").json()["csrf"]
        return self

    def __exit__(self, *exc):
        self.client.__exit__(*exc)
        logs.setup(None, console=False)

    def call(self, method, path, **kwargs):
        return self.client.request(method, path, headers={"X-CSRF-Token": self.csrf}, **kwargs)

    def wait(self, pred, timeout=30.0, what="조건"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = pred()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"시간 안에 만족하지 못했다: {what}; {self.h.brief()}")


def export_zip(site) -> zipfile.ZipFile:
    r = site.call("POST", "/api/logs/export", json={"range": "all"})
    assert r.status_code == 200, r.text
    return zipfile.ZipFile(io.BytesIO(r.content))


def test_enabled_fake_extension_registers_everything_and_recording_survives_it(tmp_path, server, tools, fake_ext):
    with Site(tmp_path, server, tools, enabled=["fakeext"]) as site:
        assert fake_ext.calls[:3] == ["routes", "open_storage", "start"]
        # 저장소: 자기 폴더의 DB
        assert (site.h.cfg.data_dir / "ext" / "fakeext" / "fake.sqlite3").exists()
        # 경로와 접근 규칙: 로그인·CSRF·공개는 중심부 관문이 정한다
        assert site.client.get("/api/fakeext/ping").json()["pong"] is True
        anon = TestClient(site.app, base_url="http://testserver")
        assert anon.get("/api/fakeext/ping").status_code == 401 and anon.get("/api/fakeext/public").json() == {"public": True}
        assert site.client.post("/api/fakeext/note", json={"text": "x"}).status_code == 403  # CSRF 없음
        assert site.call("POST", "/api/fakeext/note", json={"text": "메모"}).json() == {"ok": True}
        # 화면: 스크립트·스타일 목록(뼈대가 불러온다), 탭과 설정 묶음, 정적 파일
        html = site.client.get("/").text
        assert "/static/ext/fakeext/fake.js" in html and "/static/ext/fakeext/fake.css" in html
        assert site.client.get("/static/ext/fakeext/fake.js").status_code == 200
        info = site.client.get("/api/extensions").json()
        assert info["ui"]["tabs"] == [{"ext": "fakeext", "view": "fakeext", "label": "가짜"}]
        assert info["ui"]["groups"] == [{"ext": "fakeext", "subtab": "general", "id": "fakeext", "title": "가짜 확장 설정"}]
        # 들어 있는 확장(downloads·replay·clips, 꺼짐) 다음에 시험이 등록한 확장
        assert [(x["name"], x["enabled"], x["active"]) for x in info["extensions"]][-2:] == [("fakeext", True, True), ("fakechild", False, False)]
        # 설정 키: 기본값과 검증
        assert site.client.get("/api/extensions/fakeext/settings").json() == {"settings": {"level": 3}}
        assert site.call("PATCH", "/api/extensions/fakeext/settings", json={"level": 12}).status_code == 400
        assert site.call("PATCH", "/api/extensions/fakeext/settings", json={"nope": 1}).status_code == 400
        assert site.call("PATCH", "/api/extensions/fakeext/settings", json={"level": 7}).json() == {"settings": {"level": 7}}
        assert site.client.get("/api/fakeext/ping").json()["level"] == 7
        # 사건: 채널 추가 → 녹화 시작(듣는 쪽이 예외) → 녹화 끝(듣는 쪽이 1초 붙잡음) → 마무리
        assert site.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "확장채널", "output_dir": "ext"}).status_code == 201
        b = server.go_live(initial=20, rate=10)
        site.wait(site.h.recording, what="녹화")
        time.sleep(0.5)
        b.end()
        site.wait(lambda: site.h.finalized(1), what="마무리")
        session = site.h.sessions()[0]
        assert session["finalize_status"] == "done" and session["end_reason"] == "broadcast_end"
        site.app.state.engine.events.drain(10)
        names = [e.name for e in fake_ext.events]
        for name in ("server_started", "channel_added", "record_started", "record_ended", "finalize_started", "finalize_ended",
                     "settings_changed"):
            assert name in names, (name, names)
        assert names.index("record_started") < names.index("record_ended") < names.index("finalize_ended")
        started = next(e for e in fake_ext.events if e.name == "record_started")
        assert (started.platform, started.channel_id, started.values["session_id"]) == ("chzzk", CHANNEL, session["id"])
        assert "event_listener_failed" in site.h.log_text()  # 예외는 로그에 남고 녹화는 이어졌다
        # 채널 화면 정보
        row = site.client.get("/api/channels").json()["channels"][0]
        assert row["ext"]["fakeext"]["seen"] >= 4
        # 파일 관리자 잠금 대상
        site.call("PATCH", "/api/settings", json={"fm_enabled": True})
        locked = site.h.root / "ext" / "locked.bin"
        locked.write_bytes(b"x")
        fake_ext.lock_path = locked
        roots = site.client.get("/api/files/roots").json()["roots"]
        key = next(x["key"] for x in roots if x["label"] == "채널: 확장채널")
        r = site.call("POST", "/api/files/rename", json={"root": key, "path": "locked.bin", "new_name": "y.bin"})
        assert r.status_code == 423 and locked.exists(), r.text
        fake_ext.lock_path = None
        assert site.call("POST", "/api/files/rename", json={"root": key, "path": "locked.bin", "new_name": "y.bin"}).status_code == 200
        # 로그 내려받기: 확장이 내놓은 줄과 진단 요약
        z = export_zip(site)
        rows = [json.loads(x) for x in z.read("ext/fakeext/events.jsonl").decode().splitlines()]
        assert rows and rows[0]["fake_note"].startswith("seq ")
        assert json.loads(z.read("diagnostics/extensions.json"))["fakeext"]["version"] == "1.0"
        assert fake_ext.ticks > 0  # 백그라운드 일이 돌았다
    assert fake_ext.calls[-1] == "stop" and fake_ext.task.cancelled()


def test_disabled_fake_extension_leaves_no_trace(tmp_path, server, tools, fake_ext):
    with Site(tmp_path, server, tools) as site:
        assert fake_ext.calls == []  # 경로·저장소·시작이 하나도 불리지 않았다
        assert not (site.h.cfg.data_dir / "ext").exists()
        assert site.client.get("/api/fakeext/ping").status_code == 404
        anon = TestClient(site.app, base_url="http://testserver")
        assert anon.get("/api/fakeext/public").status_code == 401  # 규칙도 등록되지 않았다(기본 거부)
        html = site.client.get("/").text
        assert "/static/ext/" not in html
        assert site.client.get("/static/ext/fakeext/fake.js").status_code == 404
        info = site.client.get("/api/extensions").json()
        assert info["ui"] == {"scripts": [], "styles": [], "tabs": [], "groups": []}
        assert site.client.get("/api/extensions/fakeext/settings").status_code == 404
        assert site.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "채널", "output_dir": "a"}).status_code == 201
        site.app.state.engine.events.drain(5)
        assert fake_ext.events == []
        assert "ext" not in site.client.get("/api/channels").json()["channels"][0]
        z = export_zip(site)
        assert not [n for n in z.namelist() if n.startswith("ext/") or n == "diagnostics/extensions.json"]
        # 켜도 다음 서버 시작부터다
        assert site.call("PUT", "/api/extensions", json={"enabled": ["fakechild"]}).status_code == 400  # 필요한 확장이 꺼져 있다
        r = site.call("PUT", "/api/extensions", json={"enabled": ["fakeext"]}).json()
        assert r["restart_needed"] is True and "다음 서버 시작" in r["note"]
        assert site.client.get("/api/fakeext/ping").status_code == 404
        assert site.call("PUT", "/api/extensions", json={"enabled": ["nope"]}).status_code == 400


def test_extensions_group_lists_the_builtin_extensions(site_plain):
    """설정 > 일반의 "확장" 묶음에 들어 있는 세 확장이 이름·설명·필요한 확장과 함께 나온다. 처음 설치는 모두 꺼짐."""
    # 유튜브 영상(youtube_video)이 넷째로, 씨미 다시보기·클립(cime_media)이 다섯째로 더해졌다
    assert list(extensions.BUILTIN) == ["downloads", "replay", "clips", "youtube_video", "cime_media"]
    info = site_plain.client.get("/api/extensions").json()
    rows = {x["name"]: x for x in info["extensions"]}
    assert [rows[n]["enabled"] for n in ("downloads", "replay", "clips", "youtube_video", "cime_media")] == [False] * 5
    assert rows["replay"]["requires"] == ["downloads"] and rows["clips"]["requires"] == ["downloads"]
    assert rows["youtube_video"]["requires"] == ["downloads"] and rows["cime_media"]["requires"] == ["downloads"]
    assert set(rows["downloads"]["required_by"]) == {"replay", "clips", "youtube_video", "cime_media"} and all(rows[n]["description"] for n in rows)
    html = site_plain.client.get("/").text
    assert '<section id="extensions-group" class="panel" hidden>' in html
    from webui_files import all_js
    assert '$("extensions-group").hidden = list.length === 0;' in all_js()


@pytest.fixture
def site_plain(tmp_path, server, tools):
    with Site(tmp_path, server, tools) as s:
        yield s


class NoRule(fake_extension.Extension):
    name = "norule"

    def routes(self, ctx):
        r = APIRouter()

        @r.get("/api/norule/x")
        async def x():
            return {}

        return r


class Collides(fake_extension.Extension):
    name = "collides"
    access_rules = (extensions.AccessRule("GET", "/api/channels", extensions.LOGIN),)

    def routes(self, ctx):
        r = APIRouter()

        @r.get("/api/channels")
        async def x():
            return {}

        return r


@pytest.mark.parametrize("ext, why", [(NoRule(), "접근 규칙이 없는 경로"), (Collides(), "겹칩니다")])
def test_extension_route_without_rule_or_colliding_stops_startup(tmp_path, server, tools, ext, why):
    """확장 경로에 접근 규칙이 없거나 중심부·다른 확장의 경로와 겹치면 서버가 뜨지 않는다."""
    extensions.register(ext)
    try:
        h = Harness(tmp_path, server, tools)
        h.store.set_extra({extensions.ENABLED_KEY: [ext.name]})
        with pytest.raises(extensions.ExtensionError) as e:
            web.create_app(h.cfg, h.timings)
        assert why in str(e.value)
    finally:
        extensions.unregister(ext.name)


# ---- 의존 방향 ----
# 예외가 없다: 중심부(core·recording·web·로그 내려받기·파일 관리자 등)는 확장 폴더를 부르지 않는다
def imports_of(path: Path) -> set[str]:
    pkg = path.relative_to(ROOT).with_suffix("").parts[:-1]
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(pkg[:len(pkg) - node.level + 1] + ((node.module,) if node.module else ()))
            out.add(base)
            out |= {f"{base}.{a.name}" for a in node.names}
    return {m for m in out if m.startswith("live_recorder")}


def test_dependencies_point_one_way():
    """core는 recording·web을 부르지 않고, recording은 web을 부르지 않는다. 확장 폴더(ext/) 밖의 어느 모듈도
    확장을 이름으로 부르지 않는다(예외 없음). 웹 경로 묶음끼리도 서로 부르지 않는다."""
    bad = []
    for path in sorted(PKG.rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        mods = imports_of(path)
        if rel.startswith("live_recorder/core/"):
            bad += [f"{rel}: {m}" for m in mods if m.startswith(("live_recorder.recording", "live_recorder.web"))]
        if rel.startswith(("live_recorder/recording/", "live_recorder/platforms/")):
            bad += [f"{rel}: {m}" for m in mods if m.startswith("live_recorder.web")]
        if not rel.startswith("live_recorder/ext/"):
            bad += [f"{rel}: {m}" for m in mods if m.startswith(("live_recorder.ext", "fake_extension"))]
        if rel.startswith("live_recorder/web/routes/") and not rel.endswith("__init__.py"):
            me = path.stem
            bad += [f"{rel}: {m}" for m in mods if m.startswith("live_recorder.web.routes.") and m.split(".")[3] != me]
    assert bad == [], "\n".join(bad)


DOWNLOADS_PUBLIC = ("live_recorder.ext.downloads.api", "live_recorder.ext.downloads.media")


def test_replay_and_clips_use_downloads_only_through_its_public_interface():
    """replay·clips는 downloads의 공개 접점(api: 작업 종류 등록·작업 만들기·저장소에 붙기, media: 받는 방법)만 쓰고,
    서로를 모른다. 중심부는 확장을 BUILTIN 표(모듈 이름 문자열)로만 안다."""
    bad = []
    # youtube_video도 같다(replay·clips를 모른다)
    # cime_media도 같다(다른 확장을 모른다)
    for name, others in (("replay", ("clips", "youtube_video", "cime_media")), ("clips", ("replay", "youtube_video", "cime_media")),
                         ("youtube_video", ("replay", "clips", "cime_media")), ("cime_media", ("replay", "clips", "youtube_video"))):
        for path in sorted((PKG / "ext" / name).rglob("*.py")):
            for m in imports_of(path):
                if (m.startswith("live_recorder.ext.downloads") and m != "live_recorder.ext.downloads"
                        and not any(m == p or m.startswith(p + ".") for p in DOWNLOADS_PUBLIC)):
                    bad.append(f"{path.name}: {m}")
                if any(m.startswith(f"live_recorder.ext.{o}") for o in others):
                    bad.append(f"{path.name}: {m}")
    assert bad == [], bad
    for path in (PKG / "ext" / "downloads").rglob("*.py"):
        assert not {m for m in imports_of(path) if m.startswith(("live_recorder.ext.replay", "live_recorder.ext.clips",
                                                                 "live_recorder.ext.youtube_video",
                                                                 "live_recorder.ext.cime_media"))}, path.name


def test_core_package_holds_the_commons():
    """설정·저장소·로그·비밀 저장소·이름 규칙·사건 알림·확장 등록은 core에 있고 옛 자리에는 없다(껍데기 파일 없음)."""
    for name in ("config", "store", "logs", "secretstore", "naming", "events", "extensions"):
        assert (PKG / "core" / f"{name}.py").exists(), name
        assert not (PKG / f"{name}.py").exists(), name
    assert not (PKG / "web.py").exists() and (PKG / "web" / "app.py").exists()
    for group in ("channels", "history", "cookies", "settings", "files", "logs"):  # 다시보기 경로는 확장으로 옮겼다
        assert (PKG / "web" / "routes" / f"{group}.py").exists(), group


def test_page_lists_core_scripts_in_order():
    """index.html의 목록(뼈대가 불러오는 순서)은 서버의 화면별 파일 목록과 같고, 서버가 그대로 내려 준다."""
    from webui_files import script_paths
    from live_recorder.web import app as web_app

    listed = ["/static/" + str(p.relative_to(web.UI_DIR)) for p in script_paths()[1:] if p.is_relative_to(web.UI_DIR)]
    assert listed == list(web_app.CORE_SCRIPTS)
    for p in script_paths():
        assert p.exists() and p.stat().st_mode & 0o044 == 0o044
    js = (Path(web.UI_DIR) / "app.js").read_text(encoding="utf-8")
    assert 'node.onerror = () => reject(new Error(`화면 스크립트를 받지 못했습니다: ${src}`));' in js
    assert "if (window.lrBoot) window.lrBoot.run(start);" in js


# ---- 화면 파일 불러오기 ----
def test_a_missing_or_throwing_view_file_shows_the_boot_guide(tmp_path):
    """뼈대가 불러오는 화면별 파일 하나를 받지 못하거나 그 파일이 도중에 오류를 내면 시작 실패 안내가 뜬다."""
    import shutil

    from test_ui_screens import MODE_ADMIN, SESSION_OK, CHANNELS_OK, assert_failed, assert_ok, boot_screen

    ok = {"/api/mode": MODE_ADMIN, "/api/session": SESSION_OK, "/api/channels": CHANNELS_OK}
    assert_ok(boot_screen("index", ok))
    ui = Path(web.UI_DIR)
    assert_failed(boot_screen("index", ok, skip=[str(ui / "views" / "cookies.js")]), "화면 스크립트를 받지 못했습니다")
    # 화면 폴더를 복사해 한 파일이 맨 위에서 던지게 한다
    copy = tmp_path / "webui"
    shutil.copytree(ui, copy)
    broken = copy / "views" / "files.js"
    broken.write_text('"use strict";\nthrow new Error("이전 빌드의 파일");\n', encoding="utf-8")
    result = boot_screen("index", ok, html=str(copy / "index.html"),
                         scripts=[str(copy / "theme.js"), str(copy / "boot.js"), str(copy / "app.js")])
    assert_failed(result, "이전 빌드의 파일")
