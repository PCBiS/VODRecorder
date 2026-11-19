"""도커 없는 설치.

설치본 시험: 작업 트리에서 휠을 만들고(setuptools, 네트워크 없이) 새 가상환경에 풀어 설치한 뒤 하위 프로세스로 본다.
가상환경은 `--without-pip`로 만들고, 의존 묶음만 이 시험을 돌리는 환경의 site-packages를 .pth로 빌려 쓴다
(네트워크에서 받지 않으려는 것. 이 프로그램 자체는 반드시 설치본에서 불러오는지 확인한다).
"""

import configparser
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import sysconfig
import time
import tomllib
import zipfile
from pathlib import Path

import httpx
import pytest

from live_recorder import PROGRAM_NAME, __version__, doctor, entry
from live_recorder.core import locations
from live_recorder.core.config import AppConfig

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "live_recorder"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    base = tmp_path_factory.mktemp("install")
    src = base / "src"
    shutil.copytree(PKG, src / "live_recorder", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(ROOT / "pyproject.toml", src / "pyproject.toml")
    shutil.copy(ROOT / "LICENSE", src / "LICENSE")
    (src / "docs").mkdir()
    shutil.copy(ROOT / "docs" / "INSTALL.md", src / "docs" / "INSTALL.md")
    dist = base / "dist"
    out = subprocess.run([sys.executable, "-c", "import sys, setuptools.build_meta as b; print(b.build_wheel(sys.argv[1]))",
                          str(dist)], cwd=src, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    wheel = dist / out.stdout.strip().splitlines()[-1]
    venv = base / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=120)
    py = venv / "bin" / "python"
    purelib = Path(subprocess.run([str(py), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                  capture_output=True, text=True, check=True).stdout.strip())
    with zipfile.ZipFile(wheel) as z:
        z.extractall(purelib)
        entry_points = z.read(next(n for n in z.namelist() if n.endswith("entry_points.txt"))).decode()
    (purelib / "_deps.pth").write_text(sysconfig.get_path("purelib") + "\n")  # 의존 묶음만 빌린다
    cp = configparser.ConfigParser()
    cp.read_string(entry_points)
    target = cp["console_scripts"]["vod-recorder"]
    module, func = target.split(":")
    script = venv / "bin" / "vod-recorder"
    script.write_text(f"#!{py}\nimport sys\nfrom {module} import {func}\nsys.exit({func}())\n")
    script.chmod(0o755)
    # deno는 그 가상환경의 실행 파일 폴더에서 찾는다(설치하면 같은 자리에 깔린다)
    deno = Path(sysconfig.get_path("scripts")) / "deno"
    if deno.exists():
        (venv / "bin" / "deno").symlink_to(deno)
    home = base / "home"
    home.mkdir()
    return {"venv": venv, "wheel": wheel, "script": script, "py": py, "home": home, "base": base}


def run_env(installed, tools, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LIVE_RECORDER_", "PYTHON", "XDG_"))}
    env.update(HOME=str(installed["home"]), LIVE_RECORDER_FFMPEG=tools[0], LIVE_RECORDER_FFPROBE=tools[1])
    env.update(extra)
    return env


# ---- 휠 내용과 설치본 ----
def test_wheel_holds_every_non_python_file(installed):
    with zipfile.ZipFile(installed["wheel"]) as z:
        names = set(z.namelist())
        meta = z.read(next(n for n in names if n.endswith("METADATA"))).decode()
    want = {str(p.relative_to(ROOT)) for p in PKG.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
    assert want - names == set()
    assert "Name: vod-recorder" in meta and f"Version: {__version__}" in meta and "Requires-Python: >=3.13" in meta
    # 라이선스: 메타데이터의 표현과 LICENSE 전문(dist-info/licenses/)
    assert "License-Expression: Unlicense" in meta and "License-File: LICENSE" in meta
    with zipfile.ZipFile(installed["wheel"]) as z:
        found = [n for n in z.namelist() if n.endswith(".dist-info/licenses/LICENSE")]
        assert found and z.read(found[0]) == (ROOT / "LICENSE").read_bytes()


def test_installed_command_version_and_doctor(installed, tools):
    env = run_env(installed, tools)
    r = subprocess.run([str(installed["script"]), "--version"], capture_output=True, text=True, env=env,
                       cwd=installed["base"], timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == f"{PROGRAM_NAME} {__version__}", r.stderr
    where = subprocess.run([str(installed["py"]), "-c", "import live_recorder; print(live_recorder.__file__)"],
                           capture_output=True, text=True, env=env, cwd=installed["base"]).stdout
    assert where.startswith(str(installed["venv"])), where  # 작업 트리가 아니라 설치본을 부른다
    r = subprocess.run([str(installed["script"]), "doctor", "--port", str(_free_port())], capture_output=True, text=True,
                       env=env, cwd=installed["base"], timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[정상] ffmpeg 구성요소" in r.stdout and "꼭 필요한 것이 모두 있습니다" in r.stdout
    assert str(installed["home"] / ".local" / "share" / "vod-recorder") in r.stdout  # 리눅스 관례 위치
    # python -m live_recorder 도 같은 일을 한다
    r2 = subprocess.run([str(installed["py"]), "-m", "live_recorder", "--version"], capture_output=True, text=True, env=env,
                        cwd=installed["base"], timeout=60)
    assert r2.returncode == 0 and r2.stdout.strip() == f"{PROGRAM_NAME} {__version__}"


def test_installed_server_starts_and_serves_the_screen(installed, tools):
    port = _free_port()
    env = run_env(installed, tools, LIVE_RECORDER_PORT=str(port))
    proc = subprocess.Popen([str(installed["script"]), "web", "--quiet"], env=env, cwd=installed["base"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(base + "/status", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert proc.poll() is None, proc.stderr.read().decode()[-2000:]
            assert time.monotonic() < deadline, "서버가 뜨지 않았다"
            time.sleep(0.3)
        assert httpx.get(base + "/setup", timeout=5).status_code == 200  # 계정이 없으면 첫 설정 화면
        for asset in ("/static/app.js", "/static/style.css", "/static/views/settings.js"):
            assert httpx.get(base + asset, timeout=5).status_code == 200, asset
        assert "VOD Recorder" in httpx.get(base + "/setup", timeout=5).text
        data = installed["home"] / ".local" / "share" / "vod-recorder"
        assert (data / "recorder.sqlite3").exists() and (installed["home"] / ".local" / "state" / "vod-recorder" / "logs").is_dir()
        assert (installed["home"] / "Videos" / "vod-recorder").is_dir()
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        err = proc.stderr.read().decode()
    assert "첫 설정 코드" in err  # 첫 설정 코드는 표준 오류(저널·서비스 로그)에 찍힌다


def test_installed_server_refuses_without_ffmpeg(installed, tools, tmp_path):
    env = run_env(installed, tools, LIVE_RECORDER_FFMPEG=str(tmp_path / "no-ffmpeg"))
    r = subprocess.run([str(installed["script"]), "--data-dir", str(tmp_path / "d"), "--record-root", str(tmp_path / "r"),
                        "web", "--port", str(_free_port())], capture_output=True, text=True, env=env,
                       cwd=installed["base"], timeout=60)
    assert r.returncode == 2 and "기동 실패" in r.stderr and "ffmpeg: 찾지 못했습니다" in r.stderr and "고치" not in r.stdout
    assert "brew install ffmpeg" in r.stderr or "apt install ffmpeg" in r.stderr


# ---- 기본 위치 ----
def test_default_locations_per_system(tmp_path):
    home = tmp_path / "h"
    lin = locations.defaults("linux", {}, home)
    assert (lin.data, lin.logs, lin.recordings) == (home / ".local/share/vod-recorder", home / ".local/state/vod-recorder/logs",
                                                    home / "Videos/vod-recorder")
    xdg = locations.defaults("linux", {"XDG_DATA_HOME": "/x/data", "XDG_STATE_HOME": "/x/state"}, home)
    assert (xdg.data, xdg.logs) == (Path("/x/data/vod-recorder"), Path("/x/state/vod-recorder/logs"))
    assert locations.defaults("linux", {"XDG_DATA_HOME": "relative"}, home).data == home / ".local/share/vod-recorder"
    mac = locations.defaults("darwin", {}, home)
    assert (mac.data, mac.logs, mac.recordings) == (home / "Library/Application Support/VOD Recorder",
                                                    home / "Library/Logs/VOD Recorder", home / "Movies/VOD Recorder")
    assert locations.system_name("win32") == "windows" and locations.system_name("linux") == "linux"


def test_command_line_and_environment_win_over_defaults(tmp_path, monkeypatch):
    for k in ("LIVE_RECORDER_DATA_DIR", "LIVE_RECORDER_RECORD_ROOT", "LIVE_RECORDER_LOG_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    cfg = AppConfig.from_env()
    if locations.system_name() == "linux":
        assert cfg.data_dir == tmp_path / ".local/share/vod-recorder" and cfg.log_dir == tmp_path / ".local/state/vod-recorder/logs"
    monkeypatch.setenv("LIVE_RECORDER_DATA_DIR", str(tmp_path / "env-data"))
    cfg = AppConfig.from_env()
    assert cfg.data_dir == tmp_path / "env-data" and cfg.log_dir == tmp_path / "env-data" / "logs"  # 예전과 같은 뜻
    cfg = AppConfig.from_env(str(tmp_path / "cli-data"), str(tmp_path / "cli-rec"), str(tmp_path / "cli-logs"))
    assert (cfg.data_dir, cfg.record_root, cfg.log_dir) == (tmp_path / "cli-data", tmp_path / "cli-rec", tmp_path / "cli-logs")


def _dockerfile_env() -> dict[str, str]:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    block = re.search(r"^ENV (.+?)(?=^\S)", text, re.S | re.M).group(1)
    return dict(re.findall(r"(LIVE_RECORDER_\w+)=(\S+)", block))


def test_container_paths_stay_the_same(monkeypatch):
    """컨테이너 이미지: Dockerfile이 지금 경로를 환경변수로 준다. compose 예시의 정의를 바꾸지 않아도 같은 자리로 뜬다."""
    env = _dockerfile_env()
    assert env["LIVE_RECORDER_DATA_DIR"] == "/data" and env["LIVE_RECORDER_RECORD_ROOT"] == "/recordings"
    assert env["LIVE_RECORDER_LOG_DIR"] == "/logs"
    for compose in ("compose.yaml",):
        text = (ROOT / compose).read_text(encoding="utf-8")
        merged = dict(env, **dict(re.findall(r"^\s+(LIVE_RECORDER_\w+):\s*\"?([^\s\"#]+)", text, re.M)))
        for k in list(os.environ):
            if k.startswith("LIVE_RECORDER_"):
                monkeypatch.delenv(k)
        for k, v in merged.items():
            monkeypatch.setenv(k, v)
        cfg = AppConfig.from_env()
        assert (cfg.data_dir, cfg.record_root, cfg.log_dir) == (Path("/data"), Path("/recordings"), Path("/logs")), compose


# ---- 점검의 안내, 윈도우 ----
def test_doctor_explains_missing_tools(tmp_path):
    checks = doctor.check_ffmpeg(str(tmp_path / "none"), "ffprobe-not-here-xyz")
    assert [c.ok for c in checks] == [False, False]
    assert all("고치는 방법" in c.line() for c in checks)
    cfg = AppConfig(data_dir=tmp_path / "d", record_root=tmp_path / "r", ffmpeg=str(tmp_path / "none"), ffprobe="ffprobe-not-here-xyz")
    problems = doctor.startup_problems(cfg)
    assert len(problems) == 2 and all(p.endswith(("니다.", "십시오.")) for p in problems)
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        cfg2 = AppConfig(data_dir=locked / "d", record_root=tmp_path / "r", ffmpeg=sys.executable, ffprobe=sys.executable)
        if os.geteuid() != 0:
            assert any("자료 폴더" in p and "쓸 수 없습니다" in p for p in doctor.startup_problems(cfg2))
    finally:
        locked.chmod(0o700)


def test_windows_is_refused_with_wsl_notice(capsys):
    assert entry.main(["--version"], platform="win32") == entry.WINDOWS_EXIT
    err = capsys.readouterr().err
    assert "윈도우는 지원하지 않습니다" in err and "WSL" in err
    # 입구 모듈은 유닉스 전용 모듈을 불러오기 전에 끝낸다
    src = (PKG / "entry.py").read_text(encoding="utf-8")
    assert src.index("system_name(platform)") < src.index("from .cli import")


def test_youtube_tools_missing_do_not_block_startup(monkeypatch):
    import sysconfig as sc

    real = sc.get_path
    monkeypatch.setattr(sc, "get_path", lambda name, *a, **k: "/nonexistent" if name == "scripts" else real(name, *a, **k))
    checks = doctor.check_platform_tools()
    assert any(c.name == "deno(유튜브용)" for c in checks)
    assert any(not c.ok and not c.required for c in checks)  # 선택 항목이라 서버 시작을 막지 않는다


# ---- pyproject와 requirements의 일치 ----
def test_pyproject_pins_match_requirements():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    reqs = [line.split("#")[0].strip() for line in (ROOT / "requirements.txt").read_text().splitlines()]
    assert sorted(project["dependencies"]) == sorted(r for r in reqs if r)
    assert all("==" in d for d in project["dependencies"])
    assert project["name"] == "vod-recorder" and project["version"] == __version__
    assert project["scripts"]["vod-recorder"] == "live_recorder.entry:main"
    assert project["requires-python"] == ">=%d.%d" % doctor.MIN_PYTHON


# ---- 운영체제에 기대는 곳 ----
def test_os_dependent_calls_have_a_macos_path():
    from live_recorder import files, lowprio
    from live_recorder.core import procutil

    assert "renameatx_np" in (PKG / "files.py").read_text(encoding="utf-8") and files.RENAME_EXCL_DARWIN == 4
    if not sys.platform.startswith("linux"):
        with pytest.raises(OSError):
            lowprio.set_io_idle()
    src = (PKG / "lowprio.py").read_text(encoding="utf-8")
    assert 'sys.platform.startswith("linux")' in src  # macOS에서는 같은 번호의 다른 시스템 호출을 부르지 않는다
    assert procutil.die_with_parent.__doc__ and "watch" in (PKG / "core" / "procutil.py").read_text(encoding="utf-8")
    for path in ((PKG / "recording" / "receiver.py"), (PKG / "ext" / "downloads" / "worker.py")):
        assert "die_with_parent" in path.read_text(encoding="utf-8")


def test_parent_watch_ends_orphans(tmp_path):
    """macOS 대용: 부모가 죽으면 부모 번호가 바뀌고 자식은 SIGTERM으로 끝난다."""
    code = ("import sys, time; sys.path.insert(0, %r)\n"
            "from live_recorder.core import procutil\n"
            "import threading, os\n"
            "parent = os.getppid()\n"
            "threading.Thread(target=procutil._watch, args=(parent, 0.2), daemon=True).start()\n"
            "print('ready', flush=True); time.sleep(30)\n") % str(ROOT)
    wrapper = ("import subprocess, sys, time\n"
               "p = subprocess.Popen([sys.executable, '-c', %r], stdout=subprocess.PIPE)\n"
               "print(p.pid, flush=True); p.stdout.readline(); sys.exit(0)\n") % code
    out = subprocess.run([sys.executable, "-c", wrapper], capture_output=True, text=True, timeout=30)
    child = int(out.stdout.split()[0])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    os.kill(child, signal.SIGKILL)
    raise AssertionError("고아가 된 자식이 끝나지 않았다")


# ---- 예시와 문서 ----
def test_service_examples_and_install_doc():
    unit = (ROOT / "packaging" / "systemd" / "vod-recorder.service").read_text(encoding="utf-8")
    for option in ("NoNewPrivileges=true", "ProtectSystem=strict", "PrivateTmp=true", "ReadWritePaths="):
        assert option in unit
    plist = next((ROOT / "packaging" / "launchd").glob("*.plist")).read_text(encoding="utf-8")
    assert "<key>ProgramArguments</key>" in plist and "vod-recorder" in plist
    doc = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    for word in ("pipx install", "ffmpeg", "vod-recorder doctor", "systemctl --user", "launchctl", "linux/arm64",
                 "WSL", "첫 설정 코드", "journalctl", "pipx upgrade", "pipx uninstall", "3.13"):
        assert word in doc, word
    assert not re.search(r"\b(?:PKG|API|SEC|REC|VOD|MOD|CM|YT|RL|ADM|UI)-\d+|\bb\d{1,2}\b", doc)
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "DENO_SHA256_X86_64" in docker and "DENO_SHA256_AARCH64" in docker and "--platform" not in docker.split("FROM", 1)[1].split("\n")[0]


# ---- 라이선스 고지와 이미지 라벨 ----
def _dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _final_stage(text: str) -> str:
    return text[text.rindex("\nFROM "):]


def test_image_carries_license_texts_and_labels():
    text = _dockerfile()
    final = _final_stage(text)
    assert re.search(r"^COPY --from=ffmpeg /src/COPYING\.LGPLv2\.1 /src/LICENSE\.md /usr/share/licenses/ffmpeg/$", final, re.M)
    assert re.search(r"^COPY LICENSE docs/THIRD-PARTY\.md /usr/share/licenses/vodrecorder/$", final, re.M)
    assert "chmod -R a=rX /usr/share/licenses/ffmpeg /usr/share/licenses/vodrecorder" in final  # 읽기 전용, 누구나 읽음
    # FFmpeg 단계는 받은 소스를 /src에 푼다(두 파일이 거기 있다)
    ffmpeg_stage = text[text.index("AS ffmpeg"):text.index("AS deps")]
    assert "WORKDIR /src" in ffmpeg_stage and "tar xJf ffmpeg.tar.xz --strip-components=1" in ffmpeg_stage
    # OCI 라벨: 라이선스는 고정, 저장소 주소는 빌드 인자(없으면 빈 라벨)
    assert 'ARG SOURCE_URL=""' in final
    assert 'org.opencontainers.image.licenses="Unlicense AND LGPL-2.1-or-later"' in final
    assert 'org.opencontainers.image.source="${SOURCE_URL}"' in final and 'org.opencontainers.image.url="${SOURCE_URL}"' in final
    assert 'org.opencontainers.image.documentation="${SOURCE_URL:+${SOURCE_URL}/blob/main/docs/THIRD-PARTY.md}"' in final
    assert 'org.opencontainers.image.title="VOD Recorder"' in final and "org.opencontainers.image.description=" in final
    # FFmpeg는 LGPL로만 빌드한다
    for flag in ("--enable-gpl", "--enable-version3", "--enable-nonfree"):
        assert flag not in text, flag


def test_dockerignore_lets_only_the_license_files_through():
    import fnmatch

    rules = [line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]

    def ignored(path: str) -> bool:
        result = False
        for rule in rules:
            neg = rule.startswith("!")
            pat = rule[1:] if neg else rule
            parts = path.split("/")
            hit = fnmatch.fnmatch(path, pat) or any(fnmatch.fnmatch("/".join(parts[:i]), pat) for i in range(1, len(parts)))
            if hit:
                result = not neg
        return result

    assert not ignored("LICENSE") and not ignored("docs/THIRD-PARTY.md")
    for other in ("docs/INSTALL.md", "docs/API.md", "docs/LOG-EXPORT.md", "README.md", "tests/conftest.py"):
        assert ignored(other), other
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("This is free and unencumbered software released into the public domain.")


def test_third_party_notice_matches_the_dockerfile():
    text = _dockerfile()
    notice = (ROOT / "docs" / "THIRD-PARTY.md").read_text(encoding="utf-8")
    version = re.search(r"^ARG FFMPEG_VERSION=(\S+)$", text, re.M).group(1)
    sha = re.search(r"^ARG FFMPEG_SHA256=([0-9a-f]{64})$", text, re.M).group(1)
    configure = re.search(r"\./configure (.+?)\s*\\\n\s+(.+?)\s*\\\n", text, re.S)
    flags = [f for f in (configure.group(1) + " " + configure.group(2)).split() if f.startswith("--") and not f.startswith("--prefix")]
    assert re.search(r"^\| 판 \| " + re.escape(version) + r" \|$", notice, re.M)
    assert f"ffmpeg-{version}.tar.xz" in notice
    assert re.search(r"^\| 소스 SHA-256 \| `" + sha + r"` \|$", notice, re.M)
    listed = re.search(r"^\| 빌드 옵션 \| (.+) \|$", notice, re.M).group(1)
    assert re.findall(r"`([^`]+)`", listed)[0].split() == flags
    assert "python:3.15.2-slim-trixie" in notice and "python:3.15.2-slim-trixie" in text
    for path in ("/usr/share/licenses/ffmpeg/", "/usr/share/licenses/vodrecorder/", "COPYING.LGPLv2.1", "LICENSE.md"):
        assert path in notice
