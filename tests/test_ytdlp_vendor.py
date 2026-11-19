"""yt-dlp 교체. 실제 PyPI는 부르지 않는다. 가짜 배포처(httpx.MockTransport)와 가짜 yt-dlp 휠로 시험한다.

가짜 휠의 `yt_dlp`는 판 문자열과 `YoutubeDL`(extract_info)만 있는 작은 꾸러미다. 자체 점검(따로 띄운 프로세스)은
진짜 점검 코드(`ytdlp.py --self-check`)가 이 꾸러미를 불러 본다.
"""

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from live_recorder.platforms.youtube import vendor, ytdlp

ROOT = Path(__file__).resolve().parents[1]
BASE = vendor.default_version()
NEW, NEWER, NEWEST = "2099.1.1", "2099.2.2", "2099.3.3"
EJS = "0.9.0"

GOOD_INIT = '''from .version import __version__


class YoutubeDL:
    def __init__(self, opts=None):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        raise RuntimeError("가짜")
'''


def wheel(files: dict[str, str], links: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
    return buf.getvalue()


def main_wheel(version: str, *, init: str = GOOD_INIT, reported: str | None = None, requires: tuple[str, ...] = (),
               ejs: str | None = EJS, extra: dict[str, str] | None = None, links: tuple[str, ...] = ()) -> bytes:
    meta = [f"Metadata-Version: 2.1", "Name: yt-dlp", f"Version: {version}"]
    meta += [f"Requires-Dist: {r}" for r in requires]
    if ejs:
        meta += [f"Requires-Dist: yt-dlp-ejs=={ejs}; extra == 'default'", f"Requires-Dist: yt-dlp-ejs=={ejs}; extra == 'pin'"]
    files = {"yt_dlp/__init__.py": init, "yt_dlp/extractor/youtube/deep.py": "X = 1\n", "yt_dlp/version.py": f"__version__ = {reported or version!r}\n",
             f"yt_dlp-{version}.dist-info/METADATA": "\n".join(meta) + "\n"}
    files.update(extra or {})
    return wheel(files, links)


def ejs_wheel(version: str = EJS) -> bytes:
    return wheel({"yt_dlp_ejs/__init__.py": "VERSION = %r\n" % version,
                  f"yt_dlp_ejs-{version}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: yt-dlp-ejs\nVersion: {version}\n"})


def index(name: str, version: str, body: bytes, *, url: str | None = None, sha: str | None = None, size: int | None = None):
    fname = name.replace("-", "_")
    return {"info": {"name": name, "version": version, "requires_dist": None},
            "urls": [{"filename": f"{fname}-{version}.tar.gz", "packagetype": "sdist", "digests": {"sha256": "0" * 64},
                      "size": 1, "url": f"https://files.pythonhosted.org/packages/s/{fname}.tar.gz", "yanked": False},
                     {"filename": f"{fname}-{version}-py3-none-any.whl", "packagetype": "bdist_wheel",
                      "digests": {"sha256": sha or hashlib.sha256(body).hexdigest(), "md5": "x"},
                      "size": len(body) if size is None else size,
                      "url": url or f"https://files.pythonhosted.org/packages/w/{fname}-{version}-py3-none-any.whl",
                      "yanked": False}]}


REAL_FETCHER = vendor.Fetcher


class FakePyPI:
    """PyPI 흉내. routes: 주소 → (상태, 본문 bytes, 머리글)."""

    def __init__(self):
        self.routes: dict[str, tuple[int, bytes, dict]] = {}
        self.seen: list[str] = []

    def publish(self, version: str, body: bytes | None = None, *, ejs: bytes | None = None, ejs_version: str = EJS, **kw):
        body = body if body is not None else main_wheel(version)
        data = index("yt-dlp", version, body, **kw)
        self.routes["https://pypi.org/pypi/yt-dlp/json"] = (200, json.dumps(data).encode(), {})
        self.routes[data["urls"][1]["url"]] = (200, body, {})
        if ejs is not False:
            eb = ejs if ejs is not None else ejs_wheel(ejs_version)
            edata = index("yt-dlp-ejs", ejs_version, eb)
            self.routes[f"https://pypi.org/pypi/yt-dlp-ejs/{ejs_version}/json"] = (200, json.dumps(edata).encode(), {})
            self.routes[edata["urls"][1]["url"]] = (200, eb, {})
        return data

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.seen.append(url)
        status, body, headers = self.routes.get(url, (404, b"not found", {}))
        return httpx.Response(status, content=body, headers=headers)

    def fetcher(self) -> vendor.Fetcher:
        return REAL_FETCHER(httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=False))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv(vendor.ENV_SWITCH, raising=False)
    monkeypatch.setattr(vendor, "_chosen", {"path": None, "label": vendor.LABEL_DEFAULT, "version": BASE, "note": ""})
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    return SimpleNamespace(data_dir=data)


@pytest.fixture
def pypi():
    return FakePyPI()


def modes(folder: Path) -> set[str]:
    out = set()
    for p in [folder, *folder.rglob("*")]:
        mode = stat.S_IMODE(p.lstat().st_mode)
        out.add(("d" if p.is_dir() else "f") + oct(mode))
    return out


# ---- 받아 적용 ----
def test_update_then_next_start_uses_it(cfg, pypi):
    pypi.publish(NEW)
    r = vendor.update(cfg, pypi.fetcher())
    assert r["result"] == "ok" and "다음 서버 시작부터" in r["message"], r
    folder = vendor.root(cfg) / NEW
    assert (folder / "yt_dlp" / "__init__.py").is_file() and (folder / "yt_dlp_ejs" / "__init__.py").is_file()
    assert vendor.marker(cfg)["version"] == NEW
    assert modes(vendor.root(cfg).parent) <= {"d0o700", "f0o600"}, modes(vendor.root(cfg).parent)
    assert not [p for p in vendor.root(cfg).iterdir() if p.name.startswith(".tmp-")]
    assert set(pypi.seen) == {u for u in pypi.routes if "tar.gz" not in u}  # 휠만 받았다
    assert all(u.startswith(("https://pypi.org/", "https://files.pythonhosted.org/")) for u in pypi.seen)
    # 도는 서버는 아직 기본 판이다. 상태에 받아 둔 판이 보인다
    st = vendor.status(cfg)
    assert st["version"] == BASE and st["source"] == "설치된 기본 판" and st["pending"] == NEW
    assert st["last_update"]["result"] == "ok"
    # 다음 시작: 교체한 판을 고른다(자체 점검 프로세스 통과)
    picked = vendor.choose(cfg)
    assert picked["path"] == str(folder) and picked["label"] == "교체한 판" and picked["version"] == NEW, picked
    src = ytdlp.Source(picked["path"], picked["label"])
    assert ytdlp.version(src) == NEW and ytdlp.describe(src) == f"{NEW} (교체한 판)"
    assert vendor.status(cfg)["pending"] is None


def test_already_latest(cfg, pypi):
    pypi.publish(BASE)
    r = vendor.update(cfg, pypi.fetcher())
    assert r["result"] == "latest" and "이미 최신입니다" in r["message"]
    assert not (vendor.root(cfg) / BASE).exists() and not vendor.marker(cfg)
    assert not any(".whl" in u for u in pypi.seen)  # 받지 않았다


def _fails(cfg, pypi, text: str):
    with pytest.raises(vendor.UpdateError) as e:
        vendor.update(cfg, pypi.fetcher())
    assert text in str(e.value), str(e.value)
    folder = vendor.root(cfg)
    leftovers = [p.name for p in folder.iterdir()] if folder.exists() else []
    assert all(n in ("state.json", vendor.MARKER) or n == NEW for n in leftovers), leftovers
    return str(e.value)


def test_checksum_mismatch(cfg, pypi):
    pypi.publish(NEW, sha="a" * 64)
    _fails(cfg, pypi, "SHA-256")
    assert not vendor.marker(cfg) and vendor.status(cfg)["last_update"]["result"] == "failed"


def test_redirect_and_other_host_are_refused(cfg, pypi):
    data = pypi.publish(NEW)
    url = data["urls"][1]["url"]
    pypi.routes[url] = (302, b"", {"Location": "https://evil.example/yt.whl"})
    _fails(cfg, pypi, "다른 주소로 넘기려")
    pypi.publish(NEW, url="https://evil.example/yt_dlp.whl")
    _fails(cfg, pypi, "허용하지 않는 주소")
    assert "https://evil.example/yt_dlp.whl" not in pypi.seen
    pypi.routes["https://pypi.org/pypi/yt-dlp/json"] = (301, b"", {"Location": "https://evil.example/json"})
    _fails(cfg, pypi, "다른 주소로 넘기려")


def test_size_limits(cfg, pypi, monkeypatch):
    pypi.publish(NEW, size=vendor.MAX_FILE + 1)
    _fails(cfg, pypi, "너무 큽니다")
    body = main_wheel(NEW, extra={"yt_dlp/big.bin": "x" * 5000})
    pypi.publish(NEW, body, size=10)  # 묶음 정보는 작다고 하는데 실제로는 크다
    monkeypatch.setattr(vendor, "MAX_FILE", 2000)
    _fails(cfg, pypi, "너무 큽니다")


@pytest.mark.parametrize("bad", ["../evil.py", "/abs/evil.py", "yt_dlp/../../evil.py", "C:/evil.py", "yt_dlp\\..\\evil.py"])
def test_bad_paths_in_wheel(cfg, pypi, bad):
    pypi.publish(NEW, main_wheel(NEW, extra={bad: "x = 1\n"}))
    _fails(cfg, pypi, "받을 수 없는 경로")
    assert not (cfg.data_dir / "evil.py").exists() and not (cfg.data_dir.parent / "evil.py").exists()


def test_link_in_wheel(cfg, pypi):
    pypi.publish(NEW, main_wheel(NEW, links=("yt_dlp/passwd",)))
    _fails(cfg, pypi, "링크")


def test_self_check_failure_keeps_previous(cfg, pypi):
    pypi.publish(NEW)
    vendor.update(cfg, pypi.fetcher())
    pypi.publish(NEWER, main_wheel(NEWER, reported="1.0"))  # 판 문자열이 다르다
    _fails(cfg, pypi, "자체 점검에 실패")
    assert vendor.marker(cfg)["version"] == NEW and not (vendor.root(cfg) / NEWER).exists()
    pypi.publish(NEWER, main_wheel(NEWER, init="raise ImportError('깨짐')\n"))
    msg = _fails(cfg, pypi, "자체 점검에 실패")
    assert "불러오지 못했습니다" in msg
    pypi.publish(NEWER, main_wheel(NEWER, init="__version__ = '2099.2.2'\n"))  # YoutubeDL이 없다
    assert "YoutubeDL" in _fails(cfg, pypi, "자체 점검에 실패")
    assert vendor.marker(cfg)["version"] == NEW


def test_helper_failure_discards_both(cfg, pypi):
    pypi.publish(NEW, ejs=False)  # yt-dlp-ejs 묶음 정보가 없다(404)
    _fails(cfg, pypi, "HTTP 404")
    assert not vendor.marker(cfg) and not (vendor.root(cfg) / NEW).exists()
    pypi.publish(NEW, ejs=ejs_wheel("0.1.0"))  # 요구한 판과 다른 휠
    _fails(cfg, pypi, "yt-dlp-ejs")


def test_missing_required_dependency(cfg, pypi):
    pypi.publish(NEW, main_wheel(NEW, requires=("surely-not-installed-pkg>=1",)))
    _fails(cfg, pypi, "이미지를 새로 빌드하거나 새 판으로 다시 설치해야 합니다")
    # extra 조건이 붙은 의존(선택)은 보지 않는다
    pypi.publish(NEW, main_wheel(NEW, requires=("surely-not-installed-pkg>=1; extra == 'default'",)))
    assert vendor.update(cfg, pypi.fetcher())["result"] == "ok"


def test_requirements_parse_real_shape():
    """실제 yt-dlp 2026.10.26 휠의 METADATA 꼴."""
    from email.parser import Parser

    meta = Parser().parsestr("Name: yt-dlp\nVersion: 2026.10.26\n"
                             "Requires-Dist: curl-cffi!=0.6.*,<0.18,>=0.5.10; implementation_name == \"cpython\" and extra == \"curl-cffi\"\n"
                             "Requires-Dist: certifi>=2026.10.19; extra == 'default'\n"
                             "Requires-Dist: yt-dlp-ejs==0.8.0; extra == 'default'\n"
                             "Requires-Dist: yt-dlp-ejs==0.8.0; extra == 'pin'\n"
                             "Requires-Dist: deno>=2.9.8; extra == 'deno'\n", headersonly=True)
    assert vendor.requirements(meta) == ([], "0.8.0")


# ---- 되돌리기·폴더 비움·기본 판이 더 새것·깨진 판·권한 ----
def test_rollback_and_prune(cfg, pypi, monkeypatch):
    for v in (NEW, NEWER, NEWEST):
        pypi.publish(v)
        vendor.update(cfg, pypi.fetcher())
    root = vendor.root(cfg)
    assert vendor.marker(cfg) == {**vendor.marker(cfg), "version": NEWEST, "previous": NEWER}
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == [NEWER, NEWEST]  # 표시된 것과 바로 앞의 것만
    # 지금 이 서버가 NEWER를 쓴다고 하자
    monkeypatch.setattr(vendor, "_chosen", {"path": str(root / NEWER), "label": "교체한 판", "version": NEWER, "note": ""})
    r = vendor.rollback(cfg)
    assert r["result"] == "rollback" and "다음 서버 시작부터" in r["message"]
    assert not vendor.marker(cfg) and sorted(p.name for p in root.iterdir() if p.is_dir()) == [NEWER, NEWEST]
    assert vendor.choose(cfg)["path"] is None


def test_running_version_folder_is_never_touched(cfg, pypi, monkeypatch):
    """녹화 중 갱신해도 도는 서버·수신기가 쓰는 판의 폴더는 그대로다. 수신기는 서버가 고른 경로를 받는다."""
    pypi.publish(NEW)
    vendor.update(cfg, pypi.fetcher())
    picked = vendor.choose(cfg)
    token = ytdlp.Source(picked["path"], picked["label"]).token()
    before = {p.relative_to(vendor.root(cfg)): p.read_bytes() for p in (vendor.root(cfg) / NEW).rglob("*") if p.is_file()}
    for v in (NEWER, NEWEST):
        pypi.publish(v)
        vendor.update(cfg, pypi.fetcher())
    after = {p.relative_to(vendor.root(cfg)): p.read_bytes() for p in (vendor.root(cfg) / NEW).rglob("*") if p.is_file()}
    assert before == after and ytdlp.Source.from_token(token).path == str(vendor.root(cfg) / NEW)
    assert sorted(p.name for p in vendor.root(cfg).iterdir() if p.is_dir()) == [NEW, NEWER, NEWEST]


def test_empty_folder_and_older_or_broken_vendor_use_default(cfg, pypi):
    pypi.publish(NEW)
    vendor.update(cfg, pypi.fetcher())
    import shutil

    shutil.rmtree(vendor.root(cfg))  # 관리자가 손으로 비움
    assert vendor.choose(cfg)["path"] is None
    # 기본 판이 더 새것(이미지를 새로 빌드함)
    old = "2020.1.1"
    pypi.publish(old)
    vendor._chosen["version"] = "2000.1.1"  # 갱신 단계의 견주기를 지나가게
    vendor.update(cfg, pypi.fetcher())
    picked = vendor.choose(cfg)
    assert picked["path"] is None and "같거나 더 새것" in picked["note"], picked
    # 깨진 교체 판(불러오면 예외): 기본 판으로 뜨고 까닭을 남긴다
    folder = vendor.root(cfg) / old
    shutil.rmtree(folder)
    vendor.extract_wheel(main_wheel(NEW, init="raise RuntimeError('깨짐')\n"), folder.with_name(NEW))
    vendor._write_private(vendor.root(cfg) / vendor.MARKER, {"version": NEW, "previous": None})
    picked = vendor.choose(cfg)
    assert picked["path"] is None and "불러오지 못해" in picked["note"], picked
    assert "불러오지 못해" in vendor.status(cfg)["note"]


def test_permission_checks(cfg, pypi):
    pypi.publish(NEW)
    vendor.update(cfg, pypi.fetcher())
    root = vendor.root(cfg)
    os.chmod(root, 0o777)
    try:
        picked = vendor.choose(cfg, check=False)
        assert picked["path"] is None and "남이 쓸 수 있는" in picked["note"]
    finally:
        os.chmod(root, 0o700)
    os.chmod(root / NEW, 0o770)
    assert "남이 쓸 수 있는" in vendor.choose(cfg, check=False)["note"]
    os.chmod(root / NEW, 0o700)
    assert vendor.choose(cfg, check=False)["path"] == str(root / NEW)


def test_load_falls_back_when_vendored_import_breaks(tmp_path):
    """도는 중에 교체한 판을 불러오다 실패하면 기본 판으로 내려가고 서버는 계속 돈다."""
    broken = tmp_path / "v"
    vendor.extract_wheel(main_wheel(NEW, init="raise RuntimeError('깨짐')\n"), broken)
    code = ("import json, sys; from live_recorder.platforms.youtube import ytdlp, vendor; "
            f"m = ytdlp._load(ytdlp.Source({str(broken)!r}, '교체한 판')); "
            "print(json.dumps([m.version.__version__, ytdlp.select_source().label, vendor.chosen()['note']]))")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    got = json.loads(out.strip().splitlines()[-1])
    assert got[0] == BASE and got[1] == "설치된 기본 판" and "불러오지 못해" in got[2], got


def test_one_update_at_a_time(cfg, pypi):
    pypi.publish(NEW)
    vendor._lock.acquire()
    try:
        with pytest.raises(vendor.UpdateError, match="이미"):
            vendor.update(cfg, pypi.fetcher())
        with pytest.raises(vendor.UpdateError, match="갱신을 하고 있습니다"):
            vendor.rollback(cfg)
        assert vendor.status(cfg)["running"] is True
    finally:
        vendor._lock.release()


# ---- 기능 끔·화면 API ----
def test_switch_off(cfg, pypi, monkeypatch):
    pypi.publish(NEW)
    vendor.update(cfg, pypi.fetcher())
    monkeypatch.setenv(vendor.ENV_SWITCH, "off")
    picked = vendor.choose(cfg)
    assert picked["path"] is None and "꺼져 있습니다" in picked["note"]
    with pytest.raises(vendor.UpdateError, match="꺼져"):
        vendor.update(cfg, pypi.fetcher())
    with pytest.raises(vendor.UpdateError, match="꺼져"):
        vendor.rollback(cfg)
    st = vendor.status(cfg)
    assert st["enabled"] is False and st["actions"] == [] and st["pending"] is None and st["last_update"] is None


def test_web_api(tmp_path, server, tools, pypi, monkeypatch):
    from test_web import Web

    monkeypatch.delenv(vendor.ENV_SWITCH, raising=False)
    monkeypatch.setattr(vendor, "_chosen", {"path": None, "label": vendor.LABEL_DEFAULT, "version": BASE, "note": ""})
    monkeypatch.setattr(vendor, "Fetcher", lambda client=None: pypi.fetcher())
    pypi.publish(NEW)
    with Web(tmp_path, server, tools) as site:
        site.login()
        site.h.store.set_platform_enabled("youtube", True)  # 켠 상태로 시험한다
        tools_list = site.client.get("/api/tools").json()["tools"]
        t = next(x for x in tools_list if x["name"] == "yt-dlp")
        assert t["platform"] == "youtube" and t["source"] == "설치된 기본 판" and t["default_version"] == BASE
        assert [a["id"] for a in t["actions"]] == ["update", "rollback"] and all("진행할까요?" in a["confirm"] for a in t["actions"])
        assert site.client.post("/api/tools/yt-dlp/update").status_code == 403  # CSRF 토큰 없음
        r = site.call("POST", "/api/tools/yt-dlp/update")
        assert r.status_code == 200 and r.json()["result"] == "ok", r.text
        t = next(x for x in site.client.get("/api/tools").json()["tools"] if x["name"] == "yt-dlp")
        assert t["pending"] == NEW and t["last_update"]["result"] == "ok"
        pypi.publish(NEWER, sha="b" * 64)
        r = site.call("POST", "/api/tools/yt-dlp/update")
        assert r.status_code == 409 and "SHA-256" in r.text
        assert site.call("POST", "/api/tools/yt-dlp/nope").status_code == 404
        assert site.call("POST", "/api/tools/nope/update").status_code == 404
        r = site.call("POST", "/api/tools/yt-dlp/rollback")
        assert r.status_code == 200 and r.json()["result"] == "rollback"
        summary = site.client.get("/api/tools").json()
        monkeypatch.setenv(vendor.ENV_SWITCH, "off")
        assert site.call("POST", "/api/tools/yt-dlp/update").status_code == 403
        t = next(x for x in site.client.get("/api/tools").json()["tools"] if x["name"] == "yt-dlp")
        assert t["actions"] == [] and t["enabled"] is False
        log = site.h.log_text() if hasattr(site.h, "log_text") else ""
        assert summary and (not log or '"event": "ytdlp_update_ok"' in log)


def test_diagnostics_has_tool_status(tmp_path, monkeypatch):
    from live_recorder import diagnostics
    from live_recorder.core.config import AppConfig

    from live_recorder.core import store as st

    cfg = AppConfig(data_dir=tmp_path / "d", record_root=tmp_path / "r")
    (tmp_path / "d").mkdir()
    (tmp_path / "r").mkdir()
    st.Store(cfg.db_path, cfg.record_root).set_platform_enabled("youtube", True)
    raw = diagnostics.build(cfg)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        summary = json.loads(z.read("summary.json"))
    tool = next(t for t in summary["tools"] if t["name"] == "yt-dlp")
    assert tool["default_version"] == BASE and "actions" not in tool
    assert summary["versions"]["yt-dlp"].endswith("(설치된 기본 판)")


def test_server_start_picks_vendored_version(tmp_path, server, tools, pypi, monkeypatch):
    """서버가 시작할 때 표시된 판을 고르고, 시작 로그에 "판 (출처)"를 남긴다. 클라이언트도 그 판을 쓴다."""
    import asyncio

    from conftest import Harness
    from live_recorder.platforms import registry

    monkeypatch.delenv(vendor.ENV_SWITCH, raising=False)
    monkeypatch.setattr(ytdlp, "_selected", ytdlp.DEFAULT)
    monkeypatch.setattr(vendor, "_chosen", {"path": None, "label": vendor.LABEL_DEFAULT, "version": BASE, "note": ""})
    h = Harness(tmp_path, server, tools)
    h.store.set_platform_enabled("youtube", True)
    pypi.publish(NEW)
    vendor.update(SimpleNamespace(data_dir=h.cfg.data_dir), pypi.fetcher())

    async def scenario():
        async with h:
            client = registry.get("youtube").client(h.cfg)
            return ytdlp.select_source(), client.source

    picked, client_source = asyncio.run(scenario())
    assert picked.path == str(vendor.root(h.cfg) / NEW) and picked.label == "교체한 판" and client_source == picked
    lines = [json.loads(x) for x in h.log_text().splitlines()]
    assert any(x.get("event") == "tool_version" and x.get("msg") == f"yt-dlp: {NEW} (교체한 판)" for x in lines)
    assert any(x.get("event") == "ytdlp_selected" and x.get("reason") == "vendor" for x in lines)


def test_update_refuses_newer_deno_than_image(cfg, pypi):
    """받은 yt-dlp가 설치된 deno보다 새 판을 요구하면 거절한다(deno는 교체하지 않는다)."""
    pypi.publish(NEW, main_wheel(NEW, requires=("deno>=99.0.0; extra == 'deno'", "deno==99.0.0; extra == 'pin-deno'")))
    msg = _fails(cfg, pypi, "이미지를 새로 빌드하거나 새 판으로 다시 설치해야 합니다")
    assert "deno" in msg and "99.0.0" in msg
    have = vendor.image_deno_version()
    pypi.publish(NEW, main_wheel(NEW, requires=(f"deno>={have}; extra == 'deno'",)))
    assert vendor.update(cfg, pypi.fetcher())["result"] == "ok"
