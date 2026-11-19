"""파일 관리자.

모든 경로 입력은 `FileManager.resolve()` 하나를 거친다. 이 함수는
1. 경로 문자열을 검사하고(절대 경로, `..`, 역슬래시, NUL 거부),
2. 심볼릭 링크를 풀어 정규화한 위치가 허용 루트 안인지 보고,
3. 그 위치를 루트 디렉터리부터 한 단계씩 `O_NOFOLLOW`로 다시 연다.
조작은 3에서 연 디렉터리 fd를 기준(`dir_fd`)으로 한다. 2와 3 사이에 누가 폴더를 심볼릭 링크로
바꿔치기해도 3에서 거부되고, 연 뒤에 바꿔도 fd는 원래 폴더를 가리킨다(검사와 사용 사이의 경합 방지).

잠금은 경로 문자열이 아니라 (장치, inode)로 비교한다. 대소문자나 유니코드 정규화가 다른 이름으로
같은 파일을 가리켜도 같은 항목으로 본다.
"""

from __future__ import annotations

import ctypes
import sys
import errno
import hashlib
import os
import shutil
import stat
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

TRASH_DIR = ".trash"
PART_SUFFIXES = (".part",)
FORBIDDEN_ROOTS = ("/", "/proc", "/sys", "/dev", "/etc", "/boot", "/run", "/bin", "/sbin", "/lib", "/lib64", "/usr",
                   "/System", "/Library", "/Applications", "/cores")  # 뒤의 넷은 macOS의 시스템 경로
MAX_NAME_BYTES = 255
MAX_ITEMS = 200
RENAME_NOREPLACE = 1
RENAME_EXCL_DARWIN = 0x00000004  # macOS <sys/stdio.h>의 RENAME_EXCL
_AT_FDCWD = -100

# 화면과 서버가 주고받는 필드 이름은 여기 한 곳에서 정한다.
# 웹 API의 요청 모델(web.py)과 화면(app.js의 FILE_FIELDS)이 이 표와 같은지 시험이 확인한다.
FIELDS: dict[str, tuple[str, ...]] = {
    "list": ("root", "path"),
    "download": ("root", "path"),
    "mkdir": ("root", "path", "name"),
    "rename": ("root", "path", "new_name"),
    "move": ("root", "paths", "dest_root", "dest_path"),
    "delete": ("root", "paths", "mode"),
}
ENTRY_FIELDS = ("name", "path", "type", "size", "mtime", "locked", "symlink")


class FileError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Root:
    key: str
    label: str
    path: str  # 정규화한 실제 경로
    dev: int
    ino: int
    source: str  # channel | replay | setting


@dataclass
class Target:
    root: Root
    parent_fd: int  # 호출한 쪽이 닫는다(close())
    name: str  # 루트 자체면 ""
    rel: str  # 루트 기준 상대 경로(정규화한 실제 위치)

    def close(self) -> None:
        try:
            os.close(self.parent_fd)
        except OSError:
            pass

    def __enter__(self) -> "Target":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _renameat2(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    """대상 이름이 있으면 실패한다. 덮어쓰지 않는다.
    리눅스는 renameat2(RENAME_NOREPLACE), macOS는 renameatx_np(RENAME_EXCL)다."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        r = libc.renameatx_np(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), RENAME_EXCL_DARWIN)
    else:
        r = libc.renameat2(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), RENAME_NOREPLACE)
    if r != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def check_name(name: object) -> str:
    """새 이름에 `/`, `\\`, `..`이 있거나 비었으면 거부한다. 숨김 이름(점으로 시작)과 `.part`로 끝나는 이름도
    받지 않는다. 기동 복구가 완성된 파일을 남은 임시 파일로 잘못 보지 않게 하기 위해서다."""
    if not isinstance(name, str):
        raise FileError(400, "이름이 올바르지 않습니다")
    if (not name or name in (".", "..") or "/" in name or "\\" in name or ".." in name or "\x00" in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name) or name != name.strip()
            or len(name.encode("utf-8", "surrogateescape")) > MAX_NAME_BYTES):
        raise FileError(400, "이름에 쓸 수 없는 문자가 있거나 너무 깁니다")
    if name.startswith(".") or name.casefold().endswith(PART_SUFFIXES):
        raise FileError(400, "점으로 시작하는 이름과 .part로 끝나는 이름은 쓸 수 없습니다")
    return name


def split_rel(path: object) -> list[str]:
    """루트 기준 상대 경로를 나눈다. 절대 경로, `..`, 역슬래시, NUL은 거부한다."""
    if path is None:
        path = ""
    if not isinstance(path, str) or len(path) > 4096 or "\x00" in path or "\\" in path:
        raise FileError(400, "경로가 올바르지 않습니다")
    if path.startswith("/"):
        raise FileError(400, "절대 경로는 쓸 수 없습니다")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise FileError(400, "'..'은 쓸 수 없습니다")
    return parts


def _open_dir(name: str, dir_fd: int | None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    return os.open(name, flags, dir_fd=dir_fd) if dir_fd is not None else os.open(name, flags)


def identity_set(paths: Iterable[str | os.PathLike]) -> set[tuple[int, int]]:
    """잠긴 파일과 그 위 모든 폴더의 (장치, inode). 아직 없는 파일은 있는 가장 가까운 폴더부터 센다."""
    out: set[tuple[int, int]] = set()
    for raw in paths:
        p = Path(os.path.abspath(raw))
        while True:
            try:
                st = os.lstat(p)
            except OSError:
                if p.parent == p:
                    break
                p = p.parent
                continue
            out.add((st.st_dev, st.st_ino))
            if p.parent == p:
                break
            p = p.parent
    return out


def is_hidden(name: str) -> bool:
    """숨김 파일과 임시 파일(.part)."""
    return name.startswith(".") or name.endswith(PART_SUFFIXES)


class FileManager:
    def __init__(self, data_dir: Path, roots: Callable[[], list[tuple[str, str, str]]],
                 locked_paths: Callable[[], list[str]], *, after_check: Callable[[Root, list[str]], None] | None = None,
                 log_dir: Path | None = None):
        """roots: (실제 경로, 표시 이름, 출처) 목록을 돌려주는 함수. locked_paths: 진행 중인 파일 경로 목록."""
        self.data_dir = Path(os.path.realpath(data_dir))
        self.log_dir = Path(os.path.realpath(log_dir)) if log_dir else None
        self._roots_source = roots
        self._locked_source = locked_paths
        self.after_check = after_check  # 시험용. 검사와 사용 사이에 끼어들 자리

    # ---- 루트 ----
    def root_problem(self, path: str) -> str | None:
        """허용 루트로 쓸 수 없는 이유. 없으면 None."""
        if not os.path.isabs(path):
            return "절대 경로가 아닙니다"
        real = os.path.realpath(path)
        if not os.path.isdir(real):
            return "폴더가 없습니다"
        if real == "/" or any(real == f or real.startswith(f + os.sep) for f in FORBIDDEN_ROOTS if f != "/"):
            return "시스템 경로이거나 그 하위입니다"
        data = str(self.data_dir)
        if real == data or real.startswith(data + os.sep) or data.startswith(real.rstrip(os.sep) + os.sep):
            return "데이터 폴더(비밀 저장소)를 포함합니다"
        if self.log_dir:
            logs = str(self.log_dir)
            if real == logs or real.startswith(logs + os.sep) or logs.startswith(real.rstrip(os.sep) + os.sep):
                return "로그 폴더를 포함합니다"
        return None

    def roots(self) -> list[Root]:
        out: dict[str, Root] = {}
        for path, label, source in self._roots_source():
            if self.root_problem(path):
                continue
            real = os.path.realpath(path)
            if real in out:
                continue
            st = os.stat(real)
            key = hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:16]
            out[real] = Root(key, label, real, st.st_dev, st.st_ino, source)
        return list(out.values())

    def get_root(self, key: object) -> Root:
        for r in self.roots():
            if r.key == key:
                return r
        raise FileError(404, "허용 루트가 아닙니다")

    # ---- 경로 해석 ----
    def resolve(self, root_key: object, rel: object, *, follow_last: bool = True) -> Target:
        """모든 파일 경로가 거치는 함수. 실제 위치가 허용 루트 안일 때만 그 부모 폴더의 fd를 연다."""
        root = self.get_root(root_key)
        parts = split_rel(rel)
        joined = os.path.join(root.path, *parts) if parts else root.path
        if parts and not follow_last:
            real_parent = os.path.realpath(os.path.dirname(joined))
            real = os.path.join(real_parent, parts[-1])
        else:
            real = os.path.realpath(joined)
        # 심볼릭 링크를 푼 위치가 어느 허용 루트 안인가. 요청한 루트를 먼저 본다
        owner = None
        for candidate in [root] + [r for r in self.roots() if r.key != root.key]:
            if real == candidate.path or real.startswith(candidate.path.rstrip(os.sep) + os.sep):
                owner = candidate
                break
        if owner is None:
            raise FileError(403, "허용 루트 밖입니다")
        rel_parts = [p for p in os.path.relpath(real, owner.path).split(os.sep) if p not in ("", ".")]
        if self.after_check:
            self.after_check(owner, rel_parts)
        fd = self._walk(owner, rel_parts[:-1])
        return Target(owner, fd, rel_parts[-1] if rel_parts else "", "/".join(rel_parts))

    def _walk(self, root: Root, parts: list[str]) -> int:
        try:
            fd = _open_dir(root.path, None)
        except OSError:
            raise FileError(404, "허용 루트를 열 수 없습니다") from None
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (root.dev, root.ino):
            os.close(fd)
            raise FileError(409, "허용 루트가 바뀌었습니다")
        for part in parts:
            try:
                nxt = _open_dir(part, fd)
            except OSError as e:
                os.close(fd)
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise FileError(409, "경로가 바뀌었습니다(심볼릭 링크)") from None
                raise FileError(404, "경로가 없습니다") from None
            os.close(fd)
            fd = nxt
        return fd

    # ---- 잠금 ----
    def locked_ids(self) -> set[tuple[int, int]]:
        return identity_set(self._locked_source())

    def _check_unlocked(self, t: Target, locked: set[tuple[int, int]]) -> None:
        st = self._lstat(t)
        if (st.st_dev, st.st_ino) in locked:
            raise FileError(423, "녹화·마무리·다운로드 중인 파일이나 그 파일이 든 폴더입니다")

    @staticmethod
    def _lstat(t: Target) -> os.stat_result:
        try:
            return os.stat(t.name, dir_fd=t.parent_fd, follow_symlinks=False) if t.name else os.fstat(t.parent_fd)
        except FileNotFoundError:
            raise FileError(404, "항목이 없습니다") from None

    # ---- 조회 ----
    @staticmethod
    def _check_hidden(rel: object, show_hidden: bool) -> None:
        """숨김을 끈 상태에서는 `.`으로 시작하는 경로 조각을 거부한다(목록에 없는 것을 이름으로 열지 않게)."""
        if not show_hidden and isinstance(rel, str) and any(is_hidden(p) for p in rel.split("/") if p):
            raise FileError(404, "경로가 없습니다")

    def list_dir(self, root_key: object, rel: object, show_hidden: bool) -> dict:
        self._check_hidden(rel, show_hidden)
        locked = self.locked_ids()
        with self.resolve(root_key, rel) as t:
            self._check_hidden(t.rel, show_hidden)  # 심볼릭 링크를 푼 실제 경로의 단계 이름도 본다
            try:
                fd = _open_dir(t.name, t.parent_fd) if t.name else os.dup(t.parent_fd)
            except NotADirectoryError:
                raise FileError(400, "폴더가 아닙니다") from None
            except OSError as e:
                if e.errno == errno.ENOTDIR:
                    raise FileError(400, "폴더가 아닙니다") from None
                if e.errno == errno.ELOOP:
                    raise FileError(409, "경로가 바뀌었습니다(심볼릭 링크)") from None
                raise FileError(404, "경로가 없습니다") from None
            try:
                entries = []
                with os.scandir(fd) as it:
                    for entry in it:
                        if not show_hidden and is_hidden(entry.name):
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        link = stat.S_ISLNK(st.st_mode)
                        is_dir = stat.S_ISDIR(st.st_mode) or (link and entry.is_dir())
                        entries.append({
                            "name": entry.name,
                            "path": "/".join(p for p in (t.rel, entry.name) if p),
                            "type": "dir" if is_dir else "file",
                            "size": None if is_dir else st.st_size,
                            "mtime": st.st_mtime,
                            "locked": (st.st_dev, st.st_ino) in locked,
                            "symlink": link,
                        })
            finally:
                os.close(fd)
            entries.sort(key=lambda e: (e["type"] != "dir", unicodedata.normalize("NFC", e["name"]).casefold(), e["name"]))
            return {"root": t.root.key, "path": t.rel, "entries": entries[:5000], "truncated": len(entries) > 5000}

    def usage(self) -> list[dict]:
        out = []
        for r in self.roots():
            try:
                du = shutil.disk_usage(r.path)
            except OSError:
                continue
            out.append({"key": r.key, "label": r.label, "source": r.source, "total": du.total, "used": du.used, "free": du.free})
        return out

    def open_file(self, root_key: object, rel: object, show_hidden: bool = True) -> tuple[int, int, str]:
        """내려받기. (fd, 크기, 이름). 호출한 쪽이 fd를 닫는다."""
        self._check_hidden(rel, show_hidden)
        with self.resolve(root_key, rel) as t:
            self._check_hidden(t.rel, show_hidden)  # 심볼릭 링크를 푼 실제 경로의 단계 이름도 본다
            if not t.name:
                raise FileError(400, "폴더는 내려받을 수 없습니다")
            try:
                # O_NONBLOCK: FIFO 같은 것을 열다 멈추지 않게. 일반 파일인지 본 뒤 다시 막는 읽기로 돌린다
                fd = os.open(t.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=t.parent_fd)
            except OSError as e:
                raise FileError(409 if e.errno == errno.ELOOP else 404, "파일을 열 수 없습니다") from None
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                raise FileError(400, "일반 파일이 아닙니다")
            os.set_blocking(fd, True)
            return fd, st.st_size, t.name

    # ---- 변경 ----
    def mkdir(self, root_key: object, rel: object, name: object) -> None:
        name = check_name(name)
        with self.resolve(root_key, rel) as t:
            fd = _open_dir(t.name, t.parent_fd) if t.name else os.dup(t.parent_fd)
            try:
                os.mkdir(name, 0o755, dir_fd=fd)
            except FileExistsError:
                raise FileError(409, "같은 이름이 이미 있습니다") from None
            finally:
                os.close(fd)

    def rename(self, root_key: object, rel: object, new_name: object) -> str:
        new_name = check_name(new_name)
        locked = self.locked_ids()
        with self.resolve(root_key, rel, follow_last=False) as t:
            if not t.name:
                raise FileError(400, "허용 루트 자체는 바꿀 수 없습니다")
            self._check_unlocked(t, locked)
            try:
                _renameat2(t.parent_fd, t.name, t.parent_fd, new_name)
            except OSError as e:
                raise self._os_error(e) from None
            return "/".join(p for p in (t.rel.rsplit("/", 1)[0] if "/" in t.rel else "", new_name) if p)

    def move(self, root_key: object, rels: object, dest_root: object, dest_rel: object) -> int:
        items = self._items(rels)
        locked = self.locked_ids()
        moved = 0
        with self.resolve(dest_root, dest_rel) as d:
            dest_fd = _open_dir(d.name, d.parent_fd) if d.name else os.dup(d.parent_fd)
            try:
                for rel in items:
                    with self.resolve(root_key, rel, follow_last=False) as t:
                        if not t.name:
                            raise FileError(400, "허용 루트 자체는 옮길 수 없습니다")
                        self._check_unlocked(t, locked)
                        src = self._lstat(t)
                        if stat.S_ISDIR(src.st_mode) and self._inside(dest_fd, (src.st_dev, src.st_ino)):
                            raise FileError(400, "폴더를 자기 안으로 옮길 수 없습니다")
                        try:
                            _renameat2(t.parent_fd, t.name, dest_fd, t.name)
                        except OSError as e:
                            raise self._os_error(e) from None
                        moved += 1
            finally:
                os.close(dest_fd)
        return moved

    @staticmethod
    def _inside(dir_fd: int, ident: tuple[int, int]) -> bool:
        """dir_fd가 ident 폴더 자신이거나 그 아래인가."""
        fd = os.dup(dir_fd)
        try:
            for _ in range(4096):
                st = os.fstat(fd)
                if (st.st_dev, st.st_ino) == ident:
                    return True
                parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=fd)
                pst = os.fstat(parent)
                os.close(fd)
                fd = parent
                if (pst.st_dev, pst.st_ino) == (st.st_dev, st.st_ino):
                    return False
            return False
        finally:
            os.close(fd)

    def delete(self, root_key: object, rels: object, mode: object, trash_enabled: bool) -> int:
        """화면이 알려 준 방식(mode)과 설정이 다르면 아무것도 하지 않는다."""
        expected = "trash" if trash_enabled else "permanent"
        if mode != expected:
            raise FileError(409, "삭제 방식이 바뀌었습니다. 화면을 새로 고친 뒤 다시 확인하십시오")
        items = self._items(rels)
        locked = self.locked_ids()
        done = 0
        for rel in items:
            with self.resolve(root_key, rel, follow_last=False) as t:
                if not t.name:
                    raise FileError(400, "허용 루트 자체는 지울 수 없습니다")
                if t.rel.split("/")[0] == TRASH_DIR and trash_enabled:
                    raise FileError(400, "휴지통 안의 항목은 휴지통으로 옮길 수 없습니다")
                self._check_unlocked(t, locked)
                st = self._lstat(t)
                if trash_enabled:
                    self._to_trash(t)
                elif stat.S_ISDIR(st.st_mode):
                    shutil.rmtree(t.name, dir_fd=t.parent_fd)  # 심볼릭 링크를 따라가지 않는 구현(Linux)
                else:
                    os.unlink(t.name, dir_fd=t.parent_fd)
                done += 1
        return done

    def _to_trash(self, t: Target) -> None:
        """휴지통은 그 항목이 든 허용 루트 아래 `.trash`다. 같은 이름이 있으면 시각을 붙인다."""
        root_fd = self._walk(t.root, [])
        try:
            try:
                os.mkdir(TRASH_DIR, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            trash_fd = _open_dir(TRASH_DIR, root_fd)
            try:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                for n in range(1000):
                    candidate = t.name if n == 0 else f"{t.name}.{stamp}" + (f"-{n}" if n > 1 else "")
                    try:
                        _renameat2(t.parent_fd, t.name, trash_fd, candidate)
                        return
                    except FileExistsError:
                        continue
                    except OSError as e:
                        if e.errno == errno.EEXIST:
                            continue
                        raise self._os_error(e) from None
                raise FileError(409, "휴지통에 같은 이름이 너무 많습니다")
            finally:
                os.close(trash_fd)
        finally:
            os.close(root_fd)

    @staticmethod
    def _items(rels: object) -> list[str]:
        if not isinstance(rels, list) or not 1 <= len(rels) <= MAX_ITEMS or not all(isinstance(r, str) for r in rels):
            raise FileError(400, f"항목은 1~{MAX_ITEMS}개입니다")
        return rels

    @staticmethod
    def _os_error(e: OSError) -> FileError:
        if e.errno == errno.EEXIST or e.errno == errno.ENOTEMPTY:
            return FileError(409, "같은 이름이 이미 있습니다")
        if e.errno == errno.ENOENT:
            return FileError(404, "항목이 없습니다")
        if e.errno == errno.EXDEV:
            return FileError(400, "다른 파일 시스템으로는 옮길 수 없습니다")
        if e.errno == errno.EINVAL:
            return FileError(400, "폴더를 자기 안으로 옮길 수 없습니다")
        if e.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return FileError(403, "권한이 없습니다")
        return FileError(500, "파일 작업에 실패했습니다")


def iter_fd(fd: int, chunk: int = 1 << 20) -> Iterator[bytes]:
    """내려받기 응답용. 연 fd에서 읽고 끝나면 닫는다."""
    try:
        while True:
            data = os.read(fd, chunk)
            if not data:
                return
            yield data
    finally:
        os.close(fd)
