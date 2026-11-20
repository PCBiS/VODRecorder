"""화면에서 보는 로그 (로그 보기). 로그 파일(현재 파일과 순환된 파일)을 새것부터 읽어 한 쪽씩 내준다.

- 읽기만 한다. 파일을 바꾸거나 따로 남기지 않는다. 이벤트 루프 밖(스레드)에서 부른다.
- 자리표(cursor)는 `<장치>-<inode>-<바이트 위치>`다. 파일이 순환돼 이름이 바뀌어도(.log → .log.1) inode는 같아
  읽던 자리를 잃지 않는다. 자리표의 파일이 순환으로 지워졌으면 더 오래된 쪽은 끝, 새 줄 받기는 `reset`이다.
- 아직 다 쓰이지 않은 마지막 줄(줄바꿈 없음)은 건너뛰고 다음에 읽는다.
- 화면은 로그인한 관리자만 본다. 접속 주소·채널·제목·파일 이름은 그대로 보이고, 비밀(쿠키·토큰·설정 코드·세션 값
  꼴과 알고 있는 비밀 값)만 로그 내려받기와 같은 규칙(`logmask.Masker.secrets_only`)으로 가린다.
- 한 번에 읽는 양에 상한(`SCAN_BYTES`)이 있다. 거르기에 맞는 줄이 드물어 상한에 닿으면 거기까지만 내주고
  `stopped`를 참으로 둔다. 더 오래된 쪽은 이어서 읽을 수 있다.
"""

from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .core.config import AppConfig
from .logexport import known_secrets, log_files
from .logmask import Masker

LEVELS: dict[str, int] = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
PAGE_DEFAULT = 200
PAGE_MAX = 500
SCAN_BYTES = 8 * 1024 * 1024  # 요청 하나가 읽는 줄의 크기 상한
MAX_LINE = 2 * 1024 * 1024  # 이보다 긴 줄은 읽지 못한 줄로 센다
CHUNK = 64 * 1024
CURSOR_RE = re.compile(r"\A(\d{1,20})-(\d{1,20})-(\d{1,15})\Z")
# 한 줄의 앞에 따로 보이는 필드. 나머지는 "fields"로 묶는다
MAIN_KEYS = ("ts", "level", "logger", "msg", "event", "channel_id", "exc")


class CursorError(ValueError):
    pass


@dataclass(frozen=True)
class Cursor:
    dev: int
    ino: int
    offset: int

    @classmethod
    def parse(cls, text: str) -> "Cursor":
        m = CURSOR_RE.match(text or "")
        if not m:
            raise CursorError("자리표가 틀립니다")
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    def __str__(self) -> str:
        return f"{self.dev}-{self.ino}-{self.offset}"


@dataclass(frozen=True)
class Filter:
    level: int = 0  # 이 수준 이상만
    channel: str | None = None  # 채널 ID(대소문자 무시)
    text: str | None = None  # 값 어디에든 든 글(대소문자 무시)

    def cheap(self, rec: dict[str, Any]) -> bool:
        if self.level and LEVELS.get(str(rec.get("level", "")).upper(), 0) < self.level:
            return False
        if self.channel and str(rec.get("channel_id") or "").lower() != self.channel.lower():
            return False
        return True

    def words(self, rec: dict[str, Any]) -> bool:
        if not self.text:
            return True
        hay = " ".join(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str) for v in rec.values())
        return self.text.casefold() in hay.casefold()


@dataclass(frozen=True)
class _File:
    path: Path
    dev: int
    ino: int
    size: int

    def at(self, offset: int) -> Cursor:
        return Cursor(self.dev, self.ino, offset)


def _files(log_dir: Path) -> list[_File]:
    """새것부터: live-recorder.log, .log.1, … .log.5."""
    out = []
    for path in reversed(log_files(log_dir)):
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append(_File(path, st.st_dev, st.st_ino, st.st_size))
    return out


def _complete_end(f, size: int) -> int:
    """다 쓰인 마지막 줄의 끝. 줄바꿈 없이 끝나는 꼬리(쓰는 중인 줄)는 뺀다."""
    pos = size
    while pos > 0:
        n = min(CHUNK, pos)
        f.seek(pos - n)
        data = f.read(n)
        i = data.rfind(b"\n")
        if i >= 0:
            return pos - n + i + 1
        pos -= n
        if size - pos > MAX_LINE:
            return 0
    return 0


def _backward(f, end: int) -> Iterator[tuple[int, bytes]]:
    """`end` 앞의 줄을 뒤에서부터. (줄의 시작 위치, 줄)."""
    pos, buf = end, b""
    while True:
        cut = buf.rfind(b"\n", 0, max(len(buf) - 1, 0))
        if cut >= 0:
            line, buf = buf[cut + 1:], buf[:cut + 1]
            yield pos + cut + 1, line
            continue
        if pos == 0:
            if buf:
                yield 0, buf
            return
        if len(buf) > MAX_LINE:
            buf = b"\n"  # 너무 긴 줄은 버리고 읽지 못한 줄 하나로 센다
            yield pos + 1, b"\x00"
        n = min(CHUNK, pos)
        pos -= n
        f.seek(pos)
        buf = f.read(n) + buf


def _forward(f, start: int, end: int) -> Iterator[tuple[int, bytes]]:
    f.seek(start)
    pos = start
    while pos < end:
        line = f.readline(MAX_LINE + 1)
        if not line or not line.endswith(b"\n"):
            return
        yield pos, line
        pos += len(line)


class _Reader:
    def __init__(self, cfg: AppConfig, flt: Filter, session_secrets: Iterable[str]):
        self.flt = flt
        self.masker = Masker(False, secrets=known_secrets(cfg, session_secrets))
        self.bad = 0

    def entry(self, raw: bytes) -> dict[str, Any] | None:
        """맞는 줄이면 화면에 보낼 모양, 아니면 None."""
        line = raw.strip()
        if not line:
            return None
        try:
            rec = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            self.bad += 1
            return None
        if not isinstance(rec, dict):
            self.bad += 1
            return None
        if not self.flt.cheap(rec):
            return None
        rec = {str(k): self._mask(v) for k, v in rec.items()}
        if not self.flt.words(rec):
            return None
        out: dict[str, Any] = {k: rec.get(k) for k in MAIN_KEYS if rec.get(k) is not None}
        rest = {k: v for k, v in rec.items() if k not in MAIN_KEYS and k != "tmpl"}
        if rest:
            out["fields"] = rest
        return out

    def _mask(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.masker.secrets_only(value)
        if isinstance(value, list):
            return [self._mask(v) for v in value]
        if isinstance(value, dict):
            return {str(k): self._mask(v) for k, v in value.items()}
        return value


def read_page(cfg: AppConfig, *, before: str | None = None, limit: int = PAGE_DEFAULT, flt: Filter = Filter(),
              session_secrets: Iterable[str] = (), scan_bytes: int = SCAN_BYTES) -> dict[str, Any]:
    """새것부터 한 쪽. `before`가 없으면 맨 끝에서, 있으면 그 자리 앞에서 시작한다.

    돌려주는 것: entries(새것부터), older(더 오래된 쪽의 자리표, 끝이면 None), newer(맨 끝에서 시작했을 때 새 줄을
    받을 자리표), stopped(읽는 양 상한에 닿음), bad_lines, files."""
    limit = max(1, min(PAGE_MAX, limit))
    reader = _Reader(cfg, flt, session_secrets)
    files = _files(cfg.log_dir)
    start_index, start_offset, newer = 0, None, None
    if before:
        cur = Cursor.parse(before)
        found = [i for i, x in enumerate(files) if (x.dev, x.ino) == (cur.dev, cur.ino)]
        if not found:
            return {"entries": [], "older": None, "newer": None, "stopped": False, "bad_lines": 0, "files": len(files)}
        start_index, start_offset = found[0], min(cur.offset, files[found[0]].size)
    entries: list[dict[str, Any]] = []
    older: Cursor | None = None
    scanned, stopped = 0, False
    for i in range(start_index, len(files)):
        item = files[i]
        try:
            with item.path.open("rb") as f:
                end = start_offset if (i == start_index and start_offset is not None) else _complete_end(f, item.size)
                if i == 0 and not before:
                    newer = item.at(end)
                for pos, raw in _backward(f, end):
                    if len(entries) >= limit or scanned >= scan_bytes:
                        stopped = len(entries) < limit
                        older = item.at(pos + len(raw))
                        break
                    scanned += len(raw)
                    e = reader.entry(raw)
                    if e is not None:
                        entries.append(e)
        except OSError:
            continue
        if older is not None:
            break
        if len(entries) >= limit or scanned >= scan_bytes:
            # 이 파일을 다 읽은 채 상한에 닿았다. 다음 파일의 끝에서 이어 읽는다
            if i + 1 < len(files):
                stopped = len(entries) < limit
                older = files[i + 1].at(files[i + 1].size)
            break
    if not files and not before:
        newer = None
    return {"entries": entries, "older": str(older) if older else None, "newer": str(newer) if newer else None,
            "stopped": stopped, "bad_lines": reader.bad, "files": len(files)}


def read_newer(cfg: AppConfig, *, after: str, limit: int = PAGE_DEFAULT, flt: Filter = Filter(),
               session_secrets: Iterable[str] = ()) -> dict[str, Any]:
    """`after` 뒤에 새로 쓰인 줄(새것부터). 너무 많으면 새것 `limit`개만 주고 `more`를 참으로 둔다.
    자리표의 파일이 순환으로 지워졌으면 `reset`이 참이다(화면이 처음부터 다시 읽는다)."""
    limit = max(1, min(PAGE_MAX, limit))
    cur = Cursor.parse(after)
    files = _files(cfg.log_dir)
    found = [i for i, x in enumerate(files) if (x.dev, x.ino) == (cur.dev, cur.ino)]
    if not found:
        return {"entries": [], "newer": None, "reset": True, "more": False, "bad_lines": 0}
    reader = _Reader(cfg, flt, session_secrets)
    kept: deque[dict[str, Any]] = deque(maxlen=limit)
    seen = 0
    newer = cur
    for i in range(found[0], -1, -1):
        item = files[i]
        start = min(cur.offset, item.size) if i == found[0] else 0
        try:
            with item.path.open("rb") as f:
                pos = start
                for pos, raw in _forward(f, start, item.size):
                    e = reader.entry(raw)
                    if e is not None:
                        kept.append(e)
                        seen += 1
                    pos += len(raw)
                newer = item.at(pos)
        except OSError:
            continue
    entries = list(kept)
    entries.reverse()
    return {"entries": entries, "newer": str(newer), "reset": False, "more": seen > len(entries), "bad_lines": reader.bad}
