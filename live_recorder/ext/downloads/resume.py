"""이어받기 기록. 수신 프로세스 안에서만 쓴다.

작업 폴더(`<다시보기 폴더>/.work/<작업ID>/`, 숨김)에 부분 파일과 함께 `resume.json`을 둔다.
기록에는 디스크에 확실히 쓴 것만 적는다. 데이터를 fsync한 뒤에 기록을 임시 이름으로 쓰고 fsync해 이름을 바꾼다.
그래서 수신 프로세스가 언제 강제로 끝나도(일시정지는 종료 신호 뒤 강제 종료다) 기록은 실제보다 적게 말할 뿐
많게 말하지 않는다. 기록에 없는 부분은 다시 받는다.

기록에는 주소·서명·쿠키를 넣지 않는다. 화질 ID, 구간, 크기, 서버 검증값(ETag·Last-Modified), 재생목록 지문만 둔다.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable

STATE = "resume.json"
VERSION = 1

# 처음부터 다시 받는 이유(작업 로그와 화면에 정해진 문장으로 나간다)
RESTART_REASONS = ("not_resumable", "no_range", "changed", "mismatch", "no_state", "unsupported")

Event = Callable[..., None]


def has_prior(work: Path) -> bool:
    """지난 시도가 남긴 것이 있는지."""
    try:
        return any(True for _ in os.scandir(work))
    except FileNotFoundError:
        return False


def load(work: Path) -> dict[str, Any] | None:
    try:
        with open(work / STATE, "rb") as f:
            data = json.loads(f.read(1 << 20))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("v") == VERSION else None


def save(work: Path, state: dict[str, Any]) -> None:
    """원자적으로 바꾼다. 부르기 전에 기록이 가리키는 데이터를 fsync해 둔다."""
    tmp = work / (STATE + ".tmp")
    data = json.dumps(dict(state, v=VERSION), ensure_ascii=False, separators=(",", ":")).encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, work / STATE)
    fsync_dir(work)


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def reset(work: Path) -> None:
    """작업 폴더를 비운다(처음부터 받기). 폴더 자체는 남긴다. 링크는 따라가지 않는다."""
    try:
        entries = list(os.scandir(work))
    except FileNotFoundError:
        return
    for entry in entries:
        path = Path(entry.path)
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(path)
        else:
            path.unlink()


def restart(work: Path, on_event: Event | None, reason: str) -> None:
    """이어받을 수 없을 때: 남은 것이 있으면 지우고, 처음부터 받는다는 사실을 알린다."""
    if has_prior(work):
        reset(work)
        if on_event:
            on_event("resume", mode="restart", reason=reason if reason in RESTART_REASONS else "no_state")


def merge(intervals: list[list[int]]) -> list[list[int]]:
    """[시작, 끝) 구간들을 정렬해 합친다."""
    out: list[list[int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


def gaps(done: list[list[int]], total: int) -> list[tuple[int, int]]:
    """받지 않은 [시작, 끝) 구간."""
    out, pos = [], 0
    for start, end in merge(done):
        if start > pos:
            out.append((pos, min(start, total)))
        pos = max(pos, end)
    if pos < total:
        out.append((pos, total))
    return [(a, b) for a, b in out if b > a]


def covered(done: list[list[int]]) -> int:
    return sum(end - start for start, end in merge(done))
