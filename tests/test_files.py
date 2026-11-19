"""파일 관리자. 파일 관리자 꺼짐, 경로 탈출, 녹화 중 폴더 조작, 삭제 방식."""

import os
import re
import time
import unicodedata
from pathlib import Path

import pytest

from live_recorder import files, web

from conftest import REPLAY_EXTENSIONS
from conftest import CHANNEL
from test_web import Web, all_routes
from webui_files import all_js


@pytest.fixture
def fm(tmp_path, server, tools):
    with Web(tmp_path, server, tools, extensions=REPLAY_EXTENSIONS) as w:  # 다시보기 저장 폴더도 허용 루트다(downloads 확장)
        w.login()
        store = w.h.store
        store.add_channel(CHANNEL, "채널", "ch1")
        w.ch_dir = w.h.root / "ch1"
        w.ch_dir.mkdir(exist_ok=True)
        extra = tmp_path / "extra-root"
        extra.mkdir()
        w.extra = extra
        assert w.call("PATCH", "/api/settings", json={"fm_enabled": True, "fm_roots": [str(extra)]}).status_code == 200
        time.sleep(1.1)  # 설정 캐시
        w.roots = {r["source"] + (":" + r["label"] if r["source"] == "setting" else ""): r["key"]
                   for r in w.client.get("/api/files/roots").json()["roots"]}
        w.ch = next(k for s, k in w.roots.items() if s == "channel")
        w.ex = next(k for s, k in w.roots.items() if s.startswith("setting"))
        yield w


def names(w, root, path=""):
    r = w.client.get("/api/files/list", params={"root": root, "path": path})
    assert r.status_code == 200, r.text
    return [e["name"] for e in r.json()["entries"]]


def post(w, op, **body):
    return w.call("POST", f"/api/files/{op}", json=body)


def test_disabled_rejects_every_file_route(tmp_path, server, tools):
    """꺼 둔 채 모든 파일 경로를 호출하면 전부 거부한다."""
    with Web(tmp_path, server, tools, extensions=REPLAY_EXTENSIONS) as w:  # 다시보기 저장 폴더도 허용 루트다(downloads 확장)
        w.login()
        file_routes = [(m, p) for m, p in all_routes(w.app) if p.startswith("/api/files")]
        assert len(file_routes) == 7
        for method, path in file_routes:
            if method == "GET":
                r = w.client.get(path, params={"root": "x", "path": ""})
            else:
                r = w.call(method, path, json={"root": "x", "path": "a", "name": "b", "new_name": "c", "paths": ["a"],
                                               "dest_root": "x", "dest_path": "", "mode": "permanent"})
            assert r.status_code == 403 and "꺼져" in r.json()["error"], (method, path, r.status_code)


def test_roots_and_listing(fm):
    """허용 루트, 폴더 먼저·이름순, 숨김·임시 파일은 기본으로 안 보인다."""
    assert set(s.split(":")[0] for s in fm.roots) == {"channel", "replay", "setting"}
    (fm.ch_dir / "b.mp4").write_bytes(b"x" * 10)
    (fm.ch_dir / "A.mp4").write_bytes(b"x")
    (fm.ch_dir / "z폴더").mkdir()
    (fm.ch_dir / "가폴더").mkdir()
    (fm.ch_dir / ".hidden").write_bytes(b"")
    (fm.ch_dir / "rec.mp4.part").write_bytes(b"")
    r = fm.client.get("/api/files/list", params={"root": fm.ch, "path": ""}).json()
    assert [e["name"] for e in r["entries"]] == ["z폴더", "가폴더", "A.mp4", "b.mp4"]
    entry = next(e for e in r["entries"] if e["name"] == "b.mp4")
    assert set(entry) == set(files.ENTRY_FIELDS) and entry["size"] == 10 and entry["type"] == "file"
    fm.call("PATCH", "/api/settings", json={"fm_show_hidden": True})
    time.sleep(1.1)
    assert ".hidden" in names(fm, fm.ch) and "rec.mp4.part" in names(fm, fm.ch)
    usage = fm.client.get("/api/files/roots").json()
    assert all(r["usage"]["total"] > 0 for r in usage["roots"]) and usage["delete_mode"] == "permanent"
    # 내려받기
    r = fm.client.get("/api/files/download", params={"root": fm.ch, "path": "b.mp4"})
    assert r.status_code == 200 and r.content == b"x" * 10
    assert fm.client.get("/api/files/download", params={"root": fm.ch, "path": "z폴더"}).status_code == 400


def test_bad_roots_are_refused(fm):
    """시스템 경로, 데이터 폴더(비밀 저장소)와 그 상위, 없는 폴더는 허용 루트가 될 수 없다."""
    for bad in (["/"], ["/etc"], ["/proc"], [str(fm.h.data)], [str(fm.h.data / "secrets")], [str(fm.h.data.parent)],
                [str(fm.h.root / "no-such")], ["relative/path"], ["/a/../b"]):
        r = fm.call("PATCH", "/api/settings", json={"fm_roots": bad})
        assert r.status_code == 400, bad
    for root in fm.client.get("/api/files/roots").json()["roots"]:
        listing = fm.client.get("/api/files/list", params={"root": root["key"], "path": ""}).json()
        assert "secrets" not in [e["name"] for e in listing["entries"]]


def test_path_escape_is_refused(fm, tmp_path):
    """`..`, 절대 경로, 루트 밖을 가리키는 심볼릭 링크가 새 이름·대상 폴더·경로 어디에 있어도 거부한다."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    (fm.ch_dir / "keep.mp4").write_bytes(b"keep")
    (fm.ch_dir / "sub").mkdir()
    (fm.ch_dir / "link-out").symlink_to(outside)
    (fm.ch_dir / "file-link").symlink_to(outside / "secret.txt")
    (fm.ch_dir / "link-in").symlink_to(fm.ch_dir / "sub")  # 루트 안을 가리키는 링크는 된다
    for path in ("../", "..", "sub/../../", "/etc", "/etc/passwd", "link-out", "link-out/secret.txt", "file-link",
                 "sub\\..\\..", "a\x00b"):
        r = fm.client.get("/api/files/list", params={"root": fm.ch, "path": path})
        assert r.status_code in (400, 403, 404), (path, r.status_code)
        r = fm.client.get("/api/files/download", params={"root": fm.ch, "path": path})
        assert r.status_code in (400, 403, 404), (path, r.status_code)
        assert "outside" not in r.text
    assert names(fm, fm.ch, "link-in") == []
    for new_name in ("../x", "a/b", "a\\b", "..", ".", "", " x", "x\x00", "a..b"):
        assert post(fm, "rename", root=fm.ch, path="keep.mp4", new_name=new_name).status_code == 400, new_name
        assert post(fm, "mkdir", root=fm.ch, path="", name=new_name).status_code == 400, new_name
    for dest in ("../", "/tmp", "link-out", "link-out/sub"):
        r = post(fm, "move", root=fm.ch, paths=["keep.mp4"], dest_root=fm.ch, dest_path=dest)
        assert r.status_code in (400, 403, 404), dest
    for bad_root in ("", "nope", "../", fm.ch + "x"):
        assert fm.client.get("/api/files/list", params={"root": bad_root, "path": ""}).status_code == 404
    # 링크 자체의 이름 변경·삭제는 링크만 다룬다. 대상은 그대로다
    assert post(fm, "rename", root=fm.ch, path="link-out", new_name="renamed-link").status_code == 200
    assert post(fm, "delete", root=fm.ch, paths=["renamed-link", "file-link"], mode="permanent").status_code == 200
    assert (outside / "secret.txt").read_text() == "outside"
    assert (fm.ch_dir / "keep.mp4").read_bytes() == b"keep"


def test_basic_operations_and_no_overwrite(fm):
    """폴더 생성, 이름 변경, 여러 항목 이동. 같은 이름이 있으면 덮어쓰지 않고 거부한다."""
    for n in ("a.mp4", "b.mp4", "c.mp4"):
        (fm.ch_dir / n).write_bytes(n.encode())
    assert post(fm, "mkdir", root=fm.ch, path="", name="보관").status_code == 200
    assert post(fm, "mkdir", root=fm.ch, path="", name="보관").status_code == 409
    assert post(fm, "rename", root=fm.ch, path="a.mp4", new_name="b.mp4").status_code == 409
    assert (fm.ch_dir / "b.mp4").read_bytes() == b"b.mp4"
    assert post(fm, "rename", root=fm.ch, path="a.mp4", new_name="첫째.mp4").json() == {"path": "첫째.mp4"}
    assert post(fm, "move", root=fm.ch, paths=["첫째.mp4", "b.mp4"], dest_root=fm.ch, dest_path="보관").json() == {"moved": 2}
    assert names(fm, fm.ch, "보관") == ["b.mp4", "첫째.mp4"]
    (fm.ch_dir / "b.mp4").write_bytes(b"new")
    assert post(fm, "move", root=fm.ch, paths=["b.mp4"], dest_root=fm.ch, dest_path="보관").status_code == 409
    assert (fm.ch_dir / "보관" / "b.mp4").read_bytes() == b"b.mp4"
    assert post(fm, "move", root=fm.ch, paths=["보관"], dest_root=fm.ch, dest_path="보관").status_code == 400
    # 다른 허용 루트로 옮기기
    assert post(fm, "move", root=fm.ch, paths=["c.mp4"], dest_root=fm.ex, dest_path="").status_code == 200
    assert (fm.extra / "c.mp4").exists()
    # 허용 루트 자체는 바꾸거나 지울 수 없다
    assert post(fm, "rename", root=fm.ch, path="", new_name="x").status_code == 400
    assert post(fm, "delete", root=fm.ch, paths=[""], mode="permanent").status_code == 400


def test_recording_locks_file_and_folders(fm, server):
    """녹화 중인 파일과 그 폴더·상위 폴더의 이름 변경·이동·삭제를 막고 화면에 잠김을 표시한다."""
    fm.h.store.update_channel(CHANNEL, output_dir="ch1/2026/11월")
    folder = fm.h.root / "ch1" / "2026" / "11월"
    folder.mkdir(parents=True, exist_ok=True)
    (fm.ch_dir / "other.mp4").write_bytes(b"o")
    fm.h.store.set_settings({"rewind_seconds": 0})
    server.go_live(title="잠금 시험")
    fm.wait(fm.h.recording, what="녹화")
    roots = {r["label"]: r["key"] for r in fm.client.get("/api/files/roots").json()["roots"]}
    ch_root = roots["채널: 채널"]
    # 채널 저장 폴더가 바뀌었으니 루트는 2026/11월이다. 상위 폴더는 다른 루트(설정 루트)에 넣어 시험한다
    assert fm.call("PATCH", "/api/settings", json={"fm_roots": [str(fm.h.root)]}).status_code == 200
    time.sleep(1.1)
    top = next(r["key"] for r in fm.client.get("/api/files/roots").json()["roots"] if r["label"] == str(fm.h.root))
    listing = fm.client.get("/api/files/list", params={"root": top, "path": "ch1"}).json()["entries"]
    assert {e["name"]: e["locked"] for e in listing} == {"2026": True, "other.mp4": False}
    fm.call("PATCH", "/api/settings", json={"fm_show_hidden": True})
    time.sleep(1.1)
    part = [e for e in fm.client.get("/api/files/list", params={"root": ch_root, "path": ""}).json()["entries"]]
    assert any(e["name"].endswith(".part") and e["locked"] for e in part)
    part_name = next(e["name"] for e in part if e["name"].endswith(".part"))
    for path in ("ch1", "ch1/2026", "ch1/2026/11월", f"ch1/2026/11월/{part_name}"):
        assert post(fm, "rename", root=top, path=path, new_name="x").status_code == 423, path
        assert post(fm, "move", root=top, paths=[path], dest_root=top, dest_path="").status_code == 423, path
        assert post(fm, "delete", root=top, paths=[path], mode="permanent").status_code == 423, path
    assert folder.exists() and (folder / part_name).exists()
    assert post(fm, "rename", root=top, path="ch1/other.mp4", new_name="other2.mp4").status_code == 200  # 옆 파일은 된다
    # 대소문자·유니코드 정규화가 다른 이름으로 가리켜도 잠금을 피하지 못한다(같은 이름으로 해석되지 않으면 없는 항목이다)
    nfd = unicodedata.normalize("NFD", "ch1/2026/11월")
    assert post(fm, "delete", root=top, paths=[nfd], mode="permanent").status_code in (404, 423)
    assert post(fm, "delete", root=top, paths=["CH1"], mode="permanent").status_code in (404, 423)
    assert folder.exists()


def test_lock_compares_identity_not_spelling(tmp_path):
    """잠금 비교는 (장치, inode)로 한다. 하드 링크나 다른 철자로 같은 항목을 가리켜도 잠긴 것으로 본다."""
    root = tmp_path / "r"
    (root / "dir").mkdir(parents=True)
    (root / "dir" / "rec.part").write_bytes(b"x")
    os.link(root / "dir" / "rec.part", root / "alias.part")
    ids = files.identity_set([root / "dir" / "rec.part", root / "dir" / "future-file.mp4"])
    st = os.lstat(root / "alias.part")
    assert (st.st_dev, st.st_ino) in ids
    st = os.lstat(root / "dir")
    assert (st.st_dev, st.st_ino) in ids
    assert files.identity_set([tmp_path / "missing" / "deep" / "x"])  # 없는 경로는 있는 가장 가까운 폴더부터


def test_replay_download_locks_its_folder(fm, server, vod_media):
    """다운로드 중인 작업 폴더와 그 위(다시보기 폴더)는 잠긴다."""
    from replay_fake import DASH_NO, FakeVod

    fake = FakeVod(server, vod_media)
    fake.delay = 0.4  # 시험하는 동안 다운로드가 끝나지 않게 늦춘다
    from test_replay import analyze, enqueue, job

    a = analyze(fm, [f"https://chzzk.naver.com/video/{DASH_NO}"])
    ident = enqueue(fm, a, [{"url": a["items"][0]["url"], "quality": "PD_90P_160_100_24"}])[0]["id"]
    fm.wait(lambda: job(fm, ident)["downloaded_bytes"] > 0, what="받는 중")
    assert fm.call("PATCH", "/api/settings", json={"fm_roots": [str(fm.h.root)]}).status_code == 200
    time.sleep(1.1)
    top = next(r["key"] for r in fm.client.get("/api/files/roots").json()["roots"] if r["label"] == str(fm.h.root))
    assert job(fm, ident)["status"] == "running"
    assert post(fm, "rename", root=top, path="replay", new_name="x").status_code == 423
    assert post(fm, "delete", root=top, paths=["replay"], mode="permanent").status_code == 423
    assert (fm.h.root / "replay").is_dir()
    fm.call("POST", f"/api/replay/jobs/{ident}/cancel")
    fake.delay = 0


@pytest.fixture(scope="session")
def vod_media(tools, tmp_path_factory):
    from replay_fake import build_media

    out = tmp_path_factory.mktemp("fm-vodmedia")
    build_media(tools[0], out)
    return out


def test_readonly_allows_only_browse_and_download(fm):
    (fm.ch_dir / "a.mp4").write_bytes(b"a")
    fm.call("PATCH", "/api/settings", json={"fm_readonly": True})
    time.sleep(1.1)
    assert names(fm, fm.ch) == ["a.mp4"]
    assert fm.client.get("/api/files/download", params={"root": fm.ch, "path": "a.mp4"}).status_code == 200
    for op, body in (("mkdir", dict(path="", name="n")), ("rename", dict(path="a.mp4", new_name="b.mp4")),
                     ("move", dict(paths=["a.mp4"], dest_root=fm.ex, dest_path="")),
                     ("delete", dict(paths=["a.mp4"], mode="permanent"))):
        r = post(fm, op, root=fm.ch, **body)
        assert r.status_code == 403 and "읽기 전용" in r.json()["error"], op
    assert (fm.ch_dir / "a.mp4").exists()


def test_delete_mode_matches_what_screen_said(fm):
    """휴지통 켬·끔 각각에서 화면이 알려 준 방식과 실제 결과가 같다. 다르면 아무것도 하지 않는다."""
    (fm.ch_dir / "a.mp4").write_bytes(b"a")
    (fm.ch_dir / "dir").mkdir()
    (fm.ch_dir / "dir" / "in.mp4").write_bytes(b"i")
    roots = fm.client.get("/api/files/roots").json()
    assert roots["delete_mode"] == "permanent"
    assert post(fm, "delete", root=fm.ch, paths=["a.mp4"], mode="trash").status_code == 409  # 화면이 휴지통이라고 했다면
    assert (fm.ch_dir / "a.mp4").exists()
    assert post(fm, "delete", root=fm.ch, paths=["a.mp4", "dir"], mode="permanent").json() == {"deleted": 2}
    assert not (fm.ch_dir / "a.mp4").exists() and not (fm.ch_dir / "dir").exists() and not (fm.ch_dir / ".trash").exists()

    fm.call("PATCH", "/api/settings", json={"fm_trash": True})
    time.sleep(1.1)
    assert fm.client.get("/api/files/roots").json()["delete_mode"] == "trash"
    (fm.ch_dir / "b.mp4").write_bytes(b"b")
    assert post(fm, "delete", root=fm.ch, paths=["b.mp4"], mode="permanent").status_code == 409
    assert (fm.ch_dir / "b.mp4").exists()
    assert post(fm, "delete", root=fm.ch, paths=["b.mp4"], mode="trash").status_code == 200
    assert not (fm.ch_dir / "b.mp4").exists() and (fm.ch_dir / ".trash" / "b.mp4").read_bytes() == b"b"
    (fm.ch_dir / "b.mp4").write_bytes(b"b2")
    assert post(fm, "delete", root=fm.ch, paths=["b.mp4"], mode="trash").status_code == 200
    trash = sorted(p.name for p in (fm.ch_dir / ".trash").iterdir())
    assert len(trash) == 2 and trash[0] == "b.mp4" and re.fullmatch(r"b\.mp4\.\d{8}-\d{6}", trash[1])
    assert ".trash" not in names(fm, fm.ch)  # 숨김


def test_check_and_use_race_is_refused(tmp_path, server, tools):
    """검사(실제 위치가 루트 안)와 사용 사이에 폴더를 루트 밖 심볼릭 링크로 바꿔치기해도 밖을 건드리지 않는다."""
    outside = tmp_path / "victim"
    outside.mkdir()
    (outside / "important.mp4").write_bytes(b"precious")
    swapped = {}

    def swap(root, parts):
        target = Path(root.path) / "sub"
        if parts and parts[0] == "sub" and not swapped and not target.is_symlink():
            (target / "important.mp4").unlink()
            target.rmdir()
            target.symlink_to(outside)
            swapped["done"] = True

    w = Web(tmp_path, server, tools)
    w.app = web.create_app(w.h.cfg, w.h.timings, files_after_check=swap)
    from fastapi.testclient import TestClient

    w.client = TestClient(w.app, base_url="http://testserver", follow_redirects=False)
    with w:
        w.login()
        w.h.store.add_channel(CHANNEL, "채널", "ch1")
        (w.h.root / "ch1" / "sub").mkdir(parents=True)
        (w.h.root / "ch1" / "sub" / "important.mp4").write_bytes(b"decoy")
        w.call("PATCH", "/api/settings", json={"fm_enabled": True})
        time.sleep(1.1)
        root = w.client.get("/api/files/roots").json()["roots"][0]["key"]
        r = post(w, "delete", root=root, paths=["sub/important.mp4"], mode="permanent")
        assert swapped and r.status_code == 409, r.text
        swapped.clear()
        r = w.client.get("/api/files/download", params={"root": root, "path": "sub/important.mp4"})
        assert r.status_code in (403, 409) and b"precious" not in r.content
    assert (outside / "important.mp4").read_bytes() == b"precious"


def test_field_names_are_shared_with_screen():
    """화면(app.js의 FILE_FIELDS), 서버 요청 모델, files.FIELDS가 같은 필드 이름을 쓴다."""
    js = all_js()
    m = re.search(r"const FILE_FIELDS = (\{.*?\});", js, re.S)
    assert m, "app.js에 FILE_FIELDS가 없다"
    import json as _json

    screen = _json.loads(m.group(1))
    assert {k: tuple(v) for k, v in screen.items()} == files.FIELDS
    from live_recorder.web.routes import files as fm_routes

    models = {"mkdir": fm_routes.MkdirBody, "rename": fm_routes.RenameBody, "move": fm_routes.MoveBody, "delete": fm_routes.DeleteBody}
    for op, model in models.items():
        assert tuple(model.model_fields) == files.FIELDS[op], op


def test_system_subpaths_are_refused(fm, tmp_path):
    """`/`를 뺀 시스템 경로는 그 하위도 허용 루트가 될 수 없다. 녹화 루트 밖의 다른 폴더는 받는다."""
    manager = fm.app.state.files
    for bad in ("/etc/ssl", "/usr/share", "/proc/self", "/dev/shm", "/run/user", "/sys/kernel", "/boot", "/lib/x86_64-linux-gnu"):
        if os.path.isdir(bad):
            assert manager.root_problem(bad), bad
            assert fm.call("PATCH", "/api/settings", json={"fm_roots": [bad]}).status_code == 400, bad
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert manager.root_problem(str(outside)) is None
    assert fm.call("PATCH", "/api/settings", json={"fm_roots": [str(outside)]}).status_code == 200
    # 링크로 돌아가도 실제 위치로 판단한다
    (tmp_path / "to-etc").symlink_to("/etc")
    assert manager.root_problem(str(tmp_path / "to-etc"))


def test_new_names_cannot_be_hidden_or_part(fm):
    """새 이름으로 점으로 시작하는 이름과 `.part`로 끝나는 이름을 받지 않는다(오인 방지)."""
    (fm.ch_dir / "완성본.mp4").write_bytes(b"done")
    for name in (".hidden", ".trash", "완성본.mp4.part", "x.PART", "a.remux.part"):
        assert post(fm, "rename", root=fm.ch, path="완성본.mp4", new_name=name).status_code == 400, name
        assert post(fm, "mkdir", root=fm.ch, path="", name=name).status_code == 400, name
    assert sorted(p.name for p in fm.ch_dir.iterdir()) == ["완성본.mp4"]
    assert post(fm, "rename", root=fm.ch, path="완성본.mp4", new_name="partial.mp4").status_code == 200  # 중간의 part는 된다
