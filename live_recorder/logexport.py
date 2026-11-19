"""로그 내려받기 묶음. 무엇이 들어가는지는 docs/LOG-EXPORT.md에 적었다.

묶음(zip, 메모리에서만 만든다. 디스크에 쓰지 않는다):
- manifest.json: 만든 시각, 프로그램 버전, 고른 기간·방식, 파일별 줄 수·크기, 가린 건수, 잘라 냈으면 그 사실
- logs/live-recorder.jsonl: 로그 파일(현재 파일과 순환된 파일)에서 고른 기간의 줄
- 켜진 확장이 내놓은 줄 파일(예: downloads 확장의 작업 로그). 확장이 정한 이름과 시각 필드로 고르고 가린다
- diagnostics/summary.json·settings.json·sessions.json·log_summary.json: 진단팩과 같은 요약

쿠키·계정·비밀 저장소 파일, DB 파일 자체, 녹화 파일은 넣지 않는다. 모든 줄은 logmask.Masker를 거친다.
"""

from __future__ import annotations

import io
import json
import platform
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import PROGRAM_NAME, __version__, diagnostics
from .core import secretstore
from .platforms import credentials, registry
from .core import store as st
from .core.config import AppConfig
from .logmask import Masker
from .core.naming import seoul

RANGES: dict[str, int | None] = {"1": 1, "3": 3, "7": 7, "all": None}
DEFAULT_RANGE = "3"
MAX_RAW = 64 * 1024 * 1024  # 압축 전 크기
MANIFEST_RESERVE = 256 * 1024  # manifest·디렉터리 몫
SESSION_LIMIT = 200
LOG_FILE = "logs/live-recorder.jsonl"
NOT_INCLUDED = ["쿠키(secrets/<플랫폼>-cookies.json)", "계정(secrets/account.json)", "텔레그램 봇 토큰(secrets/telegram.json)",
                "첫 설정 코드 파일"]


def not_included(extra: Iterable[str]) -> list[str]:
    """묶음에 넣지 않는 것. 확장은 "db:<파일>"(DB 파일)과 "media:<이름>"(영상 파일)을 더할 수 있다."""
    extra = list(extra)
    dbs = ["recorder.sqlite3"] + [x[3:] for x in extra if x.startswith("db:")]
    media = ["녹화"] + [x[6:] for x in extra if x.startswith("media:")]
    rest = [x for x in extra if not x.startswith(("db:", "media:"))]
    return NOT_INCLUDED + [f"DB 파일({', '.join(dbs)})", f"{'·'.join(media)} 영상 파일"] + rest


ARCHIVE_PREFIX = PROGRAM_NAME.lower().replace(" ", "") + "-logs"  # 로그 묶음 파일 이름의 앞머리(표시 이름에서 만든다)


def filename(moment: datetime) -> str:
    return moment.astimezone(seoul()).strftime(f"{ARCHIVE_PREFIX}-%Y%m%d-%H%M%S.zip")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def log_files(log_dir: Path) -> list[Path]:
    """오래된 것부터: live-recorder.log.5 … .1, live-recorder.log."""
    files = []
    for path in log_dir.glob("live-recorder.log*"):
        suffix = path.name[len("live-recorder.log"):]
        if suffix == "":
            files.append((0, path))
        elif suffix[1:].isdigit() and suffix.startswith("."):
            files.append((int(suffix[1:]), path))
    return [p for _, p in sorted(files, key=lambda x: -x[0])]


def read_log(log_dir: Path, since: datetime | None) -> tuple[list[dict], int]:
    """고른 기간의 로그 줄과 읽지 못한 줄 수."""
    out: list[dict] = []
    bad = 0
    for path in log_files(log_dir):
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue
                    if not isinstance(rec, dict):
                        bad += 1
                        continue
                    ts = _parse_ts(rec.get("ts"))
                    if since is not None and (ts is None or ts < since):
                        continue
                    out.append(rec)
        except OSError:
            continue
    return out, bad


def _sessions(store: st.Store, masker: Masker, records: list[dict]) -> list[dict]:
    refreshes: dict[Any, int] = {}
    for r in records:
        if r.get("event") == "url_refresh":
            refreshes[r.get("session_id")] = refreshes.get(r.get("session_id"), 0) + 1
    out = []
    for s in store.list_sessions(None, SESSION_LIMIT):
        row = {k: s.get(k) for k in diagnostics.SESSION_KEYS}
        row["session"] = s["id"]
        row["channel"] = masker.channel(s["channel_id"]) if s.get("channel_id") else None
        row["refreshes"] = refreshes.get(s["id"], 0)
        out.append(row)
    return out


def known_secrets(cfg: AppConfig, extra: Iterable[str] = ()) -> list[str]:
    """알고 있는 비밀 값: 저장한 쿠키 값, 텔레그램 봇 토큰, 첫 설정 코드, 열린 세션·CSRF 토큰(extra)."""
    values = list(extra)
    try:
        for platform in registry.all_platforms():
            for v in credentials.load(cfg, platform.id).values():
                if not v:
                    continue
                values.append(v)
                # 쿠키 파일 글이면 줄마다의 값도 따로(로그에는 값 하나만 나올 수 있다)
                for line in v.splitlines():
                    cells = line.split("\t")
                    if len(cells) >= 7 and len(cells[6].strip()) >= 8:
                        values.append(cells[6].strip())
    except (OSError, ValueError):
        pass
    try:
        tg = secretstore.telegram_load(cfg.telegram_path)
        for name in ("bot_token", "chat_id"):  # chat_id도 알고 있는 비밀로 가린다
            if tg.get(name) and len(str(tg[name])) >= 3:
                values.append(str(tg[name]))
    except (OSError, ValueError):
        pass
    code = secretstore.read_setup_code(cfg.setup_code_path)
    if code:
        values.append(code)
    return values


def _jsonl(rows: list[dict]) -> list[bytes]:
    return [(json.dumps(r, ensure_ascii=False, default=str) + "\n").encode("utf-8") for r in rows]


def build(cfg: AppConfig, store: st.Store, *, range_key: str = DEFAULT_RANGE, strict: bool = False,
          session_secrets: Iterable[str] = (), extensions: dict[str, Any] | None = None,
          now: datetime | None = None, max_bytes: int = MAX_RAW) -> tuple[str, bytes, dict]:
    """묶음을 만든다. (파일 이름, zip 바이트, manifest). 이벤트 루프 밖(스레드)에서 부른다.

    `extensions`는 켜진 확장이 내놓은 것(core.extensions.ExtensionHost.log_export): 줄 파일(LogFile), 진단 요약,
    필드 가리기 규칙, 가릴 제목·채널, 묶음 안내에 더할 것. 로그 내려받기는 확장이 무엇인지 모른다."""
    if range_key not in RANGES:
        raise ValueError("기간은 1·3·7·all 가운데 하나입니다")
    ext = extensions or {}
    moment = now or datetime.now(timezone.utc)
    days = RANGES[range_key]
    since = moment - timedelta(days=days) if days else None
    channels = [(c.channel_id, c.name) for c in store.list_channels()] + list(ext.get("channels", []))
    masker = Masker(strict, channels=channels, admin=secretstore.account_name(cfg.account_path),
                    secrets=known_secrets(cfg, session_secrets),
                    titles=[s.get("title") for s in store.list_sessions(None, 5000)] + list(ext.get("titles", [])),
                    extra_fields=ext.get("fields"))

    records, unreadable = read_log(cfg.log_dir, since)
    extra_files = []  # (LogFile, 기간 안의 줄)
    for item in ext.get("files", []):
        rows = []
        for row in item.rows:
            ts = _parse_ts(row.get(item.time_key))
            if since is None or (ts is not None and ts >= since):
                rows.append(dict(row))
        extra_files.append((item, rows))

    # 진단팩과 같은 요약. 채널은 대신 이름(채널-N)으로 적는다
    settings = diagnostics._settings(cfg, store)
    for item, ch in zip(settings.get("channels", []), store.list_channels()):
        item["channel"] = masker.channel(ch.channel_id)
        item.pop("index", None)
    diag = {
        "diagnostics/summary.json": {
            "created_at": moment.isoformat(timespec="seconds"),
            "versions": {"live_recorder": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
                         "ffmpeg": diagnostics._tool_version(cfg.ffmpeg), "ffprobe": diagnostics._tool_version(cfg.ffprobe)},
            "disk": {"data": diagnostics._disk(cfg.data_dir), "recordings": diagnostics._disk(cfg.record_root),
                     "logs": diagnostics._disk(cfg.log_dir)},
        },
        "diagnostics/settings.json": settings,
        "diagnostics/sessions.json": _sessions(store, masker, records),
        "diagnostics/log_summary.json": diagnostics._log_summary(records),
    }
    if ext.get("diagnostics"):  # 켜진 확장의 진단 요약(확장이 없으면 이 파일도 없다)
        diag["diagnostics/extensions.json"] = ext["diagnostics"]
    diag_bytes = {name: json.dumps(masker.value(data), ensure_ascii=False, indent=1, default=str).encode("utf-8")
                  for name, data in diag.items()}

    lines = {LOG_FILE: _jsonl([masker.record(r) for r in records])}
    stamps_of = {LOG_FILE: [_parse_ts(r.get("ts")) for r in records]}
    for item, rows in extra_files:
        masked = [masker.fixed_line(r, item.fields) if item.fields else masker.record(r) for r in rows]
        lines[item.name] = _jsonl(masked)
        stamps_of[item.name] = [_parse_ts(r.get(item.time_key)) for r in rows]

    # 압축 전 크기가 넘치면 오래된 줄부터 뺀다(모든 줄 파일을 시각 순으로 함께 본다)
    budget = max_bytes - MANIFEST_RESERVE - sum(len(b) for b in diag_bytes.values())
    total = sum(len(b) for v in lines.values() for b in v)
    dropped = {name: 0 for name in lines}
    if total > budget:
        floor = datetime.min.replace(tzinfo=timezone.utc)
        stamps = sorted(((ts or floor, name, i) for name, v in stamps_of.items() for i, ts in enumerate(v)), key=lambda x: x[0])
        gone: dict[str, set[int]] = {name: set() for name in lines}
        for _, name, i in stamps:
            if total <= budget:
                break
            total -= len(lines[name][i])
            gone[name].add(i)
        lines = {name: [b for i, b in enumerate(v) if i not in gone[name]] for name, v in lines.items()}
        dropped = {k: len(v) for k, v in gone.items()}

    files = {name: b"".join(v) for name, v in lines.items()}
    files.update(diag_bytes)
    manifest = {
        "created_at": moment.isoformat(timespec="seconds"),
        "created_at_seoul": moment.astimezone(seoul()).isoformat(timespec="seconds"),
        "program": {"name": PROGRAM_NAME, "version": __version__},
        "range": {"key": range_key, "days": days, "since": since.isoformat(timespec="seconds") if since else None},
        # 고른 가리기 수준: default(비밀·접속 주소·관리자 이름만) 또는 strict(더 가리기)
        "level": "strict" if strict else "default",
        "level_label": "더 가리기" if strict else "기본",
        "files": {name: {"lines": data.count(b"\n") if name.endswith(".jsonl") else None, "bytes": len(data)}
                  for name, data in files.items()},
        "masked": masker.summary(),
        "pseudonyms": {"channels": len(masker.channel_ids), "videos": len(masker.videos), "lives": len(masker.lives)},
        "unreadable_log_lines": unreadable,
        "trimmed": {"applied": any(dropped.values()), "limit_bytes": max_bytes, "dropped_lines": dropped},
        "not_included": not_included(ext.get("not_included", [])),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        for name, data in files.items():
            z.writestr(name, data)
    return filename(moment), buf.getvalue(), manifest
