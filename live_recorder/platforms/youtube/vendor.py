"""yt-dlp 교체 폴더. 설치된 기본 판은 그대로 두고, 데이터 폴더에 받아 둔 더 새 판을 먼저 쓴다.

폴더 구성(실행 사용자만 읽고 쓴다: 폴더 0700, 파일 0600):

    <데이터 폴더>/vendor/yt-dlp/
        current.json        쓸 판을 가리키는 표시 파일 {"version", "previous", "at"}
        state.json          마지막 갱신 시각과 결과(화면용)
        <판>/               휠 두 개(yt-dlp, yt-dlp-ejs)를 푼 곳. 이 폴더가 sys.path 맨 앞에 들어간다
        .tmp-<임의>/        받는 중인 임시 폴더(끝나면 지운다)

- 고르기는 서버가 시작할 때 한 번 한다(`choose`). 서버와 그 서버가 띄우는 수신기는 끝까지 그 판을 쓴다.
- 이 파일은 yt-dlp를 불러오지 않는다. 받은 판의 자체 점검은 따로 띄운 프로세스가 `ytdlp.py`로 한다.
- 받는 곳은 PyPI뿐이다(`pypi.org` 묶음 정보, `files.pythonhosted.org` 파일). SHA-256 확인은 전송 중 손상과 바꿔치기를
  막는 것이고 배포처 자체는 믿는다.
- 환경변수 `LIVE_RECORDER_YTDLP_REPLACE=off`면 이 기능을 끈다. 교체 폴더를 읽지 않고 갱신·되돌리기를 거부한다.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx

from ...core import httpread

log = logging.getLogger("live_recorder.youtube")

ENV_SWITCH = "LIVE_RECORDER_YTDLP_REPLACE"
INDEX_URL = "https://pypi.org/pypi/{name}/json"
VERSION_URL = "https://pypi.org/pypi/{name}/{version}/json"
ALLOWED_HOSTS = ("pypi.org", "files.pythonhosted.org")
MAIN, HELPER = "yt-dlp", "yt-dlp-ejs"
MAX_FILE = 20 * 1024 * 1024  # 파일당
MAX_INDEX = 16 * 1024 * 1024  # 묶음 정보(JSON). yt-dlp는 판 기록이 길어 1MB를 넘는다(1.2MB)
TIMEOUT = 60.0
CHECK_TIMEOUT = 60.0
MARKER, STATE = "current.json", "state.json"
LABEL_DEFAULT, LABEL_VENDOR = "설치된 기본 판", "교체한 판"

_lock = threading.Lock()


class UpdateError(Exception):
    """갱신·되돌리기를 할 수 없다. 문장은 화면에 그대로 보인다."""


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "").strip().lower() not in ("off", "0", "false", "no")


def root(cfg: Any) -> Path:
    return Path(cfg.data_dir) / "vendor" / "yt-dlp"


def parse_version(text: str) -> tuple[int, ...] | None:
    """yt-dlp 판(`2026.10.26`, 밤빌드 `2026.10.26.232843`). 숫자가 아니면 None(견주지 않는다)."""
    if not isinstance(text, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,4}", text.strip()):
        return None
    return tuple(int(x) for x in text.strip().split("."))


def newer(a: str | None, b: str | None) -> bool:
    """a가 b보다 새것인가. 견줄 수 없으면 아니다."""
    pa, pb = parse_version(a or ""), parse_version(b or "")
    return bool(pa and pb and pa > pb)


def default_version() -> str | None:
    """설치된 기본 판(불러오지 않고 묶음 정보에서 읽는다)."""
    from importlib import metadata

    try:
        return metadata.version(MAIN)
    except metadata.PackageNotFoundError:
        return None


def dist_version(folder: Path, name: str = MAIN) -> str | None:
    """판 폴더 안의 묶음 정보(`<이름>-<판>.dist-info/METADATA`)에서 판."""
    prefix = name.replace("-", "_") + "-"
    try:
        for d in folder.iterdir():
            if d.is_dir() and d.name.startswith(prefix) and d.name.endswith(".dist-info"):
                meta = Parser().parsestr((d / "METADATA").read_text(encoding="utf-8"), headersonly=True)
                return meta.get("Version")
    except OSError:
        return None
    return None


# ---- 권한 ----
def _private(path: Path) -> str | None:
    """실행 사용자 소유이고 남이 쓸 수 없는가. 아니면 까닭."""
    try:
        st = path.lstat()
    except OSError:
        return "폴더를 읽을 수 없습니다"
    if stat.S_ISLNK(st.st_mode):
        return "폴더가 링크입니다"
    if st.st_uid != os.getuid():
        return "폴더의 소유자가 실행 사용자가 아닙니다"
    if st.st_mode & 0o022:
        return "남이 쓸 수 있는 폴더입니다"
    return None


def _mkdir_private(path: Path) -> None:
    """없는 폴더를 하나씩 0700으로 만든다(parents=True는 중간 폴더를 umask대로 만든다)."""
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    if path.exists() and not missing:
        os.chmod(path, 0o700)


def _write_private(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def marker(cfg: Any) -> dict[str, Any]:
    return _read(root(cfg) / MARKER)


def complete(folder: Path, version: str) -> str | None:
    """판 폴더가 온전한가. 아니면 까닭."""
    if not (folder / "yt_dlp" / "__init__.py").is_file() or not (folder / "yt_dlp" / "version.py").is_file():
        return "판 폴더에 yt_dlp가 없습니다"
    if dist_version(folder) != version:
        return "판 폴더의 묶음 정보가 표시된 판과 다릅니다"
    return None


# ---- 시작할 때 고르기 ----
_chosen: dict[str, Any] = {"path": None, "label": LABEL_DEFAULT, "version": None, "note": ""}


def chosen() -> dict[str, Any]:
    return dict(_chosen)


def choose(cfg: Any, *, check: bool = True) -> dict[str, Any]:
    """서버가 시작할 때 쓸 판을 고른다. 돌려주는 값: {"path", "label", "version", "note"}."""
    base = default_version()
    out: dict[str, Any] = {"path": None, "label": LABEL_DEFAULT, "version": base, "note": ""}

    def done(note: str = "", level: int = logging.INFO) -> dict[str, Any]:
        out["note"] = note
        _chosen.clear()
        _chosen.update(out)
        log.log(level, "yt-dlp 고름: %s %s %s", out["version"], out["label"], note,
                extra={"event": "ytdlp_selected", "reason": "vendor" if out["path"] else "default"})
        return out

    if not enabled():
        return done("교체 기능이 꺼져 있습니다")
    folder = root(cfg)
    mark = marker(cfg)
    want = mark.get("version")
    if not folder.exists() or not want:
        return done()
    for path in (folder.parent, folder, folder / str(want)):
        why = _private(path) if path.exists() else None
        if why:
            return done(f"교체한 판을 쓰지 않습니다: {why}", logging.WARNING)
    target = folder / str(want)
    if not isinstance(want, str) or not parse_version(want) or not target.is_dir():
        return done("교체한 판을 쓰지 않습니다: 표시된 판의 폴더가 없습니다", logging.WARNING)
    why = complete(target, want)
    if why:
        return done(f"교체한 판을 쓰지 않습니다: {why}", logging.WARNING)
    if not newer(want, base):
        return done(f"설치된 기본 판({base})이 교체한 판({want})과 같거나 더 새것입니다")
    if check:
        ok, why = self_check(target, want)
        if not ok:
            log.error("교체한 yt-dlp를 불러오지 못해 기본 판으로 뜹니다: %s", why, extra={"event": "ytdlp_fallback"})
            return done(f"교체한 판({want})을 불러오지 못해 기본 판을 씁니다: {why}", logging.WARNING)
    out.update(path=str(target), label=LABEL_VENDOR, version=want)
    return done()


def fall_back(why: str) -> None:
    """도는 중에 교체한 판을 불러오다 실패했다(ytdlp.py가 부른다). 화면에 남긴다."""
    base = default_version()
    _chosen.update(path=None, label=LABEL_DEFAULT, version=base, note=f"교체한 판을 불러오지 못해 기본 판을 씁니다: {why}")


# ---- 자체 점검 ----
def self_check(folder: Path | None, version: str, *, deno: bool = False) -> tuple[bool, str]:
    """따로 띄운 프로세스에서 새 폴더(None이면 설치된 기본 판)의 yt-dlp를 불러 본다(네트워크 요청 없음).
    deno면 deno가 실행되는지도 본다. (성공, 까닭)."""
    cmd = [sys.executable, "-s", "-P", "-m", "live_recorder.platforms.youtube.ytdlp", "--self-check",
           str(folder) if folder else "-", version] + (["--deno"] if deno else [])
    pkg_parent = str(Path(__file__).resolve().parents[3])
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": pkg_parent, "HOME": os.environ.get("HOME", "/tmp"),
           "PYTHONDONTWRITEBYTECODE": "1", "YTDLP_NO_PLUGINS": "1"}  # 사용자 폴더의 yt-dlp 플러그인을 읽지 않는다
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CHECK_TIMEOUT, env=env, cwd=pkg_parent)
    except subprocess.TimeoutExpired:
        return False, f"자체 점검이 {CHECK_TIMEOUT:g}초 안에 끝나지 않았습니다"
    except OSError as e:
        return False, f"자체 점검을 실행하지 못했습니다: {type(e).__name__}"
    line = (proc.stdout.strip().splitlines() or [""])[-1]
    try:
        result = json.loads(line)
    except ValueError:
        return False, f"자체 점검 결과를 읽지 못했습니다(rc={proc.returncode}): {proc.stderr.strip()[-200:]}"
    return bool(result.get("ok")), str(result.get("reason") or "")


# ---- 상태 ----
def status(cfg: Any) -> dict[str, Any]:
    """화면·진단 요약용. 값은 비밀이 아니다."""
    now = chosen()
    mark = marker(cfg) if enabled() else {}
    pending = mark.get("version") if mark.get("version") and mark.get("version") != now.get("version") else None
    state = _read(root(cfg) / STATE) if enabled() else {}
    return {
        "name": MAIN, "enabled": enabled(), "running": _lock.locked(),
        "version": now.get("version"), "source": now.get("label"), "note": now.get("note") or "",
        "default_version": default_version(), "pending": pending,
        "last_update": state.get("last") or None,
        "actions": [{"id": "update", "label": "yt-dlp 갱신",
                     "confirm": "PyPI(pypi.org)에서 yt-dlp와 yt-dlp-ejs의 최신 판을 받아 체크섬을 확인하고, 따로 띄운 "
                                "프로세스에서 불러 보는 점검을 합니다. 받은 코드는 받는 즉시 이 점검 과정에서 실행되고, 다음 서버 시작부터는 "
                                "이 서버가 실행합니다. 진행할까요?"},
                    {"id": "rollback", "label": "기본 판으로 되돌리기",
                     "confirm": "교체한 판 표시를 지웁니다. 다음 서버 시작부터 설치된 기본 판을 실행합니다. 진행할까요?"}]
        if enabled() else [],
    }


def _record(cfg: Any, result: str, message: str, version: str | None = None) -> None:
    folder = root(cfg)
    _mkdir_private(folder)
    _write_private(folder / STATE, {"last": {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                            "result": result, "message": message, "version": version}})


# ---- 받기 ----
def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        raise UpdateError("허용하지 않는 주소에서 받으려 했습니다")


class Fetcher:
    """PyPI에서 받는다. 다른 호스트로 넘기는 응답(30x)은 따르지 않고 거부한다. 시험은 이 객체를 바꿔 끼운다."""

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=httpx.Timeout(TIMEOUT, connect=10.0), follow_redirects=False,
                                             headers={"User-Agent": "live-recorder-ytdlp-update"})

    def get(self, url: str, limit: int) -> bytes:
        _check_url(url)
        try:
            with self.client.stream("GET", url, headers=httpread.TEXT_ENCODINGS) as resp:
                if 300 <= resp.status_code < 400:
                    raise UpdateError("받는 곳이 다른 주소로 넘기려 했습니다. 받지 않았습니다")
                if resp.status_code != 200:
                    raise UpdateError(f"받지 못했습니다(HTTP {resp.status_code})")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise UpdateError(f"파일이 너무 큽니다({limit // (1024 * 1024)}MB 넘음)")
                buf = io.BytesIO()
                try:
                    for chunk in httpread.iter_body(resp, limit):  # 풀어 가며 센다
                        buf.write(chunk)
                except httpread.TooLarge:
                    raise UpdateError(f"파일이 너무 큽니다({limit // (1024 * 1024)}MB 넘음)") from None
                return buf.getvalue()
        except httpx.HTTPError as e:
            raise UpdateError(f"받는 중 연결 오류: {type(e).__name__}") from None

    def close(self) -> None:
        self.client.close()


def _index(fetcher: Fetcher, name: str, version: str | None = None) -> dict[str, Any]:
    url = (VERSION_URL.format(name=name, version=version) if version else INDEX_URL.format(name=name))
    try:
        data = json.loads(fetcher.get(url, MAX_INDEX))
    except ValueError:
        raise UpdateError("묶음 정보를 읽지 못했습니다") from None
    if not isinstance(data, dict) or not isinstance(data.get("info"), dict) or not isinstance(data.get("urls"), list):
        raise UpdateError("묶음 정보의 꼴이 다릅니다")
    return data


def _wheel_entry(data: dict[str, Any]) -> dict[str, Any]:
    """순수 파이썬 휠(`py3-none-any`) 하나. 내려진(yanked) 것은 뺀다."""
    for u in data["urls"]:
        if not isinstance(u, dict):
            continue
        name = str(u.get("filename") or "")
        if u.get("packagetype") == "bdist_wheel" and name.endswith("-py3-none-any.whl") and not u.get("yanked"):
            digest = (u.get("digests") or {}).get("sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise UpdateError("묶음 정보에 SHA-256이 없습니다")
            size = u.get("size")
            if isinstance(size, int) and size > MAX_FILE:
                raise UpdateError(f"파일이 너무 큽니다({MAX_FILE // (1024 * 1024)}MB 넘음)")
            return {"filename": name, "url": str(u.get("url") or ""), "sha256": digest}
    raise UpdateError("순수 파이썬 휠(py3-none-any)이 없습니다")


def _download(fetcher: Fetcher, entry: dict[str, Any]) -> bytes:
    data = fetcher.get(entry["url"], MAX_FILE)
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise UpdateError("받은 파일의 SHA-256이 묶음 정보와 다릅니다. 버렸습니다")
    return data


def _metadata(wheel: bytes, name: str) -> Any:
    try:
        with zipfile.ZipFile(io.BytesIO(wheel)) as z:
            meta = [n for n in z.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)
                    and n.lower().startswith(name.replace("-", "_").lower() + "-")]
            if len(meta) != 1:
                raise UpdateError("휠에 묶음 정보(METADATA)가 없습니다")
            return Parser().parsestr(z.read(meta[0]).decode("utf-8"), headersonly=True)
    except zipfile.BadZipFile:
        raise UpdateError("휠 파일이 깨졌습니다") from None


_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;]*?)\s*(?:;\s*(.*))?$")


def requirements(meta: Any) -> tuple[list[str], str | None]:
    """(이미지에 있어야 하는 필수 의존 이름들, 보조 묶음 yt-dlp-ejs의 요구 판).

    필수 의존은 `extra ==` 조건이 없는 `Requires-Dist`다. yt-dlp-ejs의 판은 `extra == "default"` 줄의 `==판`에서 읽는다
    (`Requires-Dist: yt-dlp-ejs==0.8.0; extra == 'default'`)."""
    required, helper = [], None
    for line in meta.get_all("Requires-Dist") or []:
        m = _REQ.match(line)
        if not m:
            continue
        name, spec, marker_text = m.group(1), m.group(2) or "", m.group(3) or ""
        if re.sub(r"[-_.]+", "-", name.lower()) == HELPER:
            pin = re.search(r"==\s*([0-9][0-9A-Za-z.]*)", spec)
            if pin and (not helper or "default" in marker_text):
                helper = pin.group(1)
            continue
        if "extra" not in marker_text:
            required.append(name)
    return required, helper


def deno_minimum(meta: Any) -> str | None:
    """받은 yt-dlp가 요구하는 deno의 가장 낮은 판(`deno>=판`). 실측: `deno>=2.9.8; extra == 'deno'`."""
    low = None
    for line in meta.get_all("Requires-Dist") or []:
        m = _REQ.match(line)
        if not m or m.group(1).lower() != "deno":
            continue
        for found in re.findall(r">=\s*([0-9][0-9A-Za-z.]*)", m.group(2) or ""):
            if low is None or newer(found, low):
                low = found
    return low


def image_deno_version() -> str | None:
    from importlib import metadata

    try:
        return metadata.version("deno")
    except metadata.PackageNotFoundError:
        return None


def _missing(names: list[str]) -> list[str]:
    from importlib import metadata

    out = []
    for name in names:
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            out.append(name)
    return out


def extract_wheel(wheel: bytes, dest: Path) -> None:
    """휠을 푼다. 폴더 밖으로 나가는 경로, 절대 경로, 링크는 거부한다. 폴더 0700, 파일 0600."""
    try:
        z = zipfile.ZipFile(io.BytesIO(wheel))
    except zipfile.BadZipFile:
        raise UpdateError("휠 파일이 깨졌습니다") from None
    with z:
        infos = z.infolist()
        total = 0
        for info in infos:
            name = info.filename
            p = PurePosixPath(name)
            if name.startswith(("/", "\\")) or "\\" in name or re.match(r"^[A-Za-z]:", name) or ".." in p.parts \
                    or "\x00" in name:
                raise UpdateError(f"휠 안에 받을 수 없는 경로가 있습니다: {name[:80]}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise UpdateError(f"휠 안에 링크가 있습니다: {name[:80]}")
            total += info.file_size
            if total > 8 * MAX_FILE:
                raise UpdateError("휠을 풀면 너무 큽니다")
        for info in infos:
            target = dest.joinpath(*PurePosixPath(info.filename).parts)
            if not target.resolve().is_relative_to(dest.resolve()):
                raise UpdateError(f"휠 안에 받을 수 없는 경로가 있습니다: {info.filename[:80]}")
            if info.is_dir():
                _mkdir_private(target)
                continue
            _mkdir_private(target.parent)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out, z.open(info) as src:
                shutil.copyfileobj(src, out)


def _keep(cfg: Any, extra: tuple[str | None, ...] = ()) -> set[str]:
    """지우지 않을 판 폴더: 표시된 것, 그 앞의 것, 지금 이 서버가 쓰는 것."""
    mark = marker(cfg)
    keep = {mark.get("version"), mark.get("previous"), chosen().get("version") if chosen().get("path") else None, *extra}
    return {str(v) for v in keep if v}


def prune(cfg: Any, keep: set[str]) -> None:
    folder = root(cfg)
    if not folder.is_dir():
        return
    for d in folder.iterdir():
        if d.is_dir() and not d.is_symlink() and (d.name.startswith(".tmp-") or (parse_version(d.name) and d.name not in keep)):
            shutil.rmtree(d, ignore_errors=True)


def update(cfg: Any, fetcher: Fetcher | None = None) -> dict[str, Any]:
    """최신 판을 받아 점검한 뒤 표시 파일을 바꾼다. 돌려주는 값: {"result", "message", "version"}."""
    if not enabled():
        raise UpdateError("yt-dlp 교체 기능이 꺼져 있습니다")
    if not _lock.acquire(blocking=False):
        raise UpdateError("이미 yt-dlp 갱신을 하고 있습니다")
    own = fetcher is None
    fetcher = fetcher or Fetcher()
    tmp: Path | None = None
    version: str | None = None
    try:
        log.info("yt-dlp 갱신 시작", extra={"event": "ytdlp_update_start"})
        data = _index(fetcher, MAIN)
        version = str(data["info"].get("version") or "")
        if not parse_version(version):
            raise UpdateError("최신 판 번호를 읽지 못했습니다")
        in_use = chosen().get("version") or default_version()
        mark = marker(cfg)
        if not newer(version, in_use):
            message = f"이미 최신입니다({in_use})"
            _record(cfg, "latest", message, version)
            log.info("yt-dlp 이미 최신 %s", version, extra={"event": "ytdlp_update_ok", "result": "latest"})
            return {"result": "latest", "message": message, "version": version}
        if mark.get("version") == version and not complete(root(cfg) / version, version):
            message = f"{version}을 이미 받아 두었습니다. 다음 서버 시작부터 적용됩니다"
            _record(cfg, "pending", message, version)
            return {"result": "pending", "message": message, "version": version}
        wheel = _download(fetcher, _wheel_entry(data))
        meta = _metadata(wheel, MAIN)
        if meta.get("Version") != version:
            raise UpdateError("휠의 판이 묶음 정보와 다릅니다")
        required, helper_version = requirements(meta)
        missing = _missing(required)
        if missing:
            raise UpdateError(f"새 yt-dlp가 설치본에 없는 묶음({', '.join(missing)})을 요구합니다. 이미지를 새로 빌드하거나 새 판으로 다시 설치해야 합니다")
        want_deno, have_deno = deno_minimum(meta), image_deno_version()
        if want_deno and (not have_deno or newer(want_deno, have_deno)):
            raise UpdateError(f"새 yt-dlp가 설치된 deno({have_deno or '없음'})보다 새 deno({want_deno} 이상)를 요구합니다. "
                              "이미지를 새로 빌드하거나 새 판으로 다시 설치해야 합니다")
        helper = None
        if helper_version:
            hdata = _index(fetcher, HELPER, helper_version)
            if str(hdata["info"].get("version")) != helper_version:
                raise UpdateError("yt-dlp-ejs 묶음 정보의 판이 요구한 판과 다릅니다")
            helper = _download(fetcher, _wheel_entry(hdata))
            if _metadata(helper, HELPER).get("Version") != helper_version:
                raise UpdateError("yt-dlp-ejs 휠의 판이 요구한 판과 다릅니다")
        folder = root(cfg)
        _mkdir_private(folder.parent)
        _mkdir_private(folder)
        for path in (folder.parent, folder):
            why = _private(path)
            if why:
                raise UpdateError(f"교체 폴더를 쓸 수 없습니다: {why}")
        tmp = folder / f".tmp-{secrets.token_hex(6)}"
        _mkdir_private(tmp)
        extract_wheel(wheel, tmp)
        if helper is not None:
            extract_wheel(helper, tmp)
        ok, why = self_check(tmp, version)
        if not ok:
            raise UpdateError(f"자체 점검에 실패했습니다: {why}. 이전 판을 그대로 씁니다")
        dest = folder / version
        if dest.exists():
            if version in _keep(cfg) - {mark.get("version")}:
                raise UpdateError("이 판의 폴더는 지금 쓰고 있어 바꿀 수 없습니다")
            shutil.rmtree(dest)
        os.replace(tmp, dest)
        tmp = None
        previous = mark.get("version") or None
        _write_private(folder / MARKER, {"version": version, "previous": previous,
                                         "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        prune(cfg, _keep(cfg))
        message = f"{version}을 받았습니다. 다음 서버 시작부터 적용됩니다"
        _record(cfg, "ok", message, version)
        log.info("yt-dlp 갱신 성공 %s (yt-dlp-ejs %s)", version, helper_version or "없음",
                 extra={"event": "ytdlp_update_ok", "result": "ok"})
        return {"result": "ok", "message": message, "version": version}
    except UpdateError as e:
        try:
            _record(cfg, "failed", str(e), version)
        except OSError:
            pass
        log.warning("yt-dlp 갱신 실패: %s", e, extra={"event": "ytdlp_update_failed"})
        raise
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)  # 받은 것을 지운다. 표시 파일은 그대로(이전 판 유지)
        if own:
            fetcher.close()
        _lock.release()


def rollback(cfg: Any) -> dict[str, Any]:
    """표시 파일을 지운다(다음 서버 시작부터 기본 판). 판 폴더는 지금 쓰는 것과 바로 앞의 것만 남긴다."""
    if not enabled():
        raise UpdateError("yt-dlp 교체 기능이 꺼져 있습니다")
    if not _lock.acquire(blocking=False):
        raise UpdateError("yt-dlp 갱신을 하고 있습니다. 끝난 뒤 다시 해 주십시오")
    try:
        mark = marker(cfg)
        folder = root(cfg)
        keep = _keep(cfg) | ({str(mark["version"])} if mark.get("version") else set())
        (folder / MARKER).unlink(missing_ok=True)
        in_use = chosen().get("version") if chosen().get("path") else None
        keep = {v for v in keep if v in (in_use, mark.get("version"))}
        prune(cfg, keep)
        message = "기본 판으로 되돌렸습니다. 다음 서버 시작부터 적용됩니다"
        if folder.exists():
            _record(cfg, "rollback", message, default_version())
        log.info("yt-dlp 기본 판으로 되돌림", extra={"event": "ytdlp_rollback"})
        return {"result": "rollback", "message": message, "version": default_version()}
    finally:
        _lock.release()
