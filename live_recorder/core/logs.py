"""구조화 로그. 한 줄에 JSON 하나, 크기로 순환한다."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if isinstance(record.msg, str) and record.args:
            data["tmpl"] = record.msg  # 진단팩은 값이 채워지기 전의 문장 틀만 쓴다
        for key, value in record.__dict__.items():
            if key not in _STD and not key.startswith("_"):
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def setup(log_dir: Path | None, *, console: bool = True, level: int = logging.INFO) -> None:
    root = logging.getLogger("live_recorder")
    root.setLevel(level)
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / "live-recorder.log", maxBytes=5 * 1024 * 1024,
                                                  backupCount=5, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(JsonFormatter())
        root.addHandler(ch)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청 주소(토큰 포함)를 찍지 않게 한다
    logging.getLogger("httpcore").setLevel(logging.WARNING)
