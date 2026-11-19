"""진단팩.

    python -m live_recorder diagnostics [--symptom start|recording|postprocessing|ui|storage|network|other]

다음을 zip 하나로 묶어 로그 폴더 아래 `diagnostics/`에 둔다. 4 MiB 이하, 최근 3개만 남긴다. 외부로 보내지 않는다.
- summary.json: 만든 시각, 증상, 도구·Python·ffmpeg·ffprobe 버전, 디스크 여유
- settings.json: 비밀 아닌 설정값, 채널별 화질·확장자·자동 녹화(채널은 순번으로만)
- sessions.json: 최근 세션의 시각, 채널 순번, 종료 사유, 종료코드, 길이, 크기, 마무리 결과, 주소 갱신 횟수
- log_summary.json: 최근 로그의 오류·경고를 (수준, 로거, 이벤트, 문장 틀)로 묶은 횟수와 처음·마지막 시각

넣지 않는 것: 쿠키, 계정, 토큰·chat_id, 채널 이름과 ID, 저장 폴더, 파일 이름, 방송 제목, 재생 주소, 원문 로그 문장.
로그 문장은 값이 채워지기 전의 틀(`tmpl`)만 쓴다. 틀이 없는 줄은 문장 없이 센다.
"""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import PROGRAM_NAME, __version__
from .core import store as st
from .core.config import AppConfig, Settings
from .platforms import credentials

MAX_BYTES = 4 * 1024 * 1024
KEEP = 3
SYMPTOMS = ("start", "recording", "postprocessing", "ui", "storage", "network", "other")
LOG_LINES = 50000
# 경로나 사용자가 정한 이름이 들어갈 수 있는 설정은 값 대신 개수·여부만 적는다
PATH_SETTINGS = ("fm_roots",)  # 확장의 경로 설정은 SettingSpec.path로 같은 규칙을 따른다
SESSION_KEYS = ("started_at", "ended_at", "end_reason", "exit_code", "media_seconds", "bytes", "source", "finalize_status",
                "clean_exit", "time_machine_active")


def _platform_tools(enabled: list[str]) -> dict[str, str]:
    """플랫폼 모듈이 쓰는 외부 도구의 판 + 출처."""
    from .platforms import registry

    out: dict[str, str] = {}
    for platform in registry.all_platforms():
        if not platform.tool_names or not _usable(platform, enabled):
            continue
        try:
            out.update(platform.tool_versions())
        except Exception as e:  # noqa: BLE001 - 진단 묶음은 끝까지 만든다
            out[platform.id] = f"판을 읽지 못했습니다: {type(e).__name__}"
    return out


def _gates(store, enabled: list[str]) -> dict:
    """플랫폼별 스캔 주기·채널 사이 간격·감시 중인 채널 수·한 바퀴 시간·멈춤 상태(이 프로세스의 창구)."""
    from .core import gate as gate_mod
    from .platforms import registry

    if store is None:
        return {}
    channels = store.list_channels()
    out = {}
    for platform in registry.all_platforms():
        if not _usable(platform, enabled):
            continue
        values = store.gate_settings(platform.id)
        n = sum(1 for ch in channels if ch.platform == platform.id and ch.auto_record)
        state = gate_mod.STATE.get(platform.id) or {}
        out[platform.id] = {"scan": values["scan"], "gap": values["gap"], "channels": n,
                            "cycle": gate_mod.cycle_seconds(n, values["gap"]), "paused": bool(state.get("paused")),
                            "pause_left": state.get("pause_left", 0.0)}
    return out


def _usable(platform, enabled: list[str]) -> bool:
    """꺼진 "켜야 쓰는 플랫폼"의 도구는 읽지도 부르지도 않는다."""
    return not platform.opt_in or (platform.id in enabled and not platform.enable_blocked())


def _platform_tool_status(cfg: AppConfig, enabled: list[str]) -> list[dict]:
    from .platforms import registry

    out: list[dict] = []
    for platform in registry.all_platforms():
        if not platform.tool_names:
            continue
        if not _usable(platform, enabled):
            out.append({"platform": platform.id, "platform_enabled": False})
            continue
        try:
            out += [{k: v for k, v in item.items() if k != "actions"} for item in platform.tool_status(cfg)]
        except Exception as e:  # noqa: BLE001
            out.append({"platform": platform.id, "error": type(e).__name__})
    return out


def _tool_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15).stdout
        return out.splitlines()[0][:120] if out else "알 수 없음"
    except (OSError, subprocess.TimeoutExpired):
        return "없음"


def _disk(path: Path) -> dict[str, int] | None:
    try:
        du = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": du.total, "used": du.used, "free": du.free}


def _settings(cfg: AppConfig, store: st.Store) -> dict[str, Any]:
    s = store.get_settings()
    out: dict[str, Any] = {}
    for key in Settings.keys():
        value = getattr(s, key)
        if key in PATH_SETTINGS:
            out[key] = {"count": len(value)} if isinstance(value, tuple) else {"set": bool(value)}
        elif key == "filename_pattern":
            # 틀에 적은 글자(채널 이름 등)가 섞일 수 있어 토큰 목록만 적는다
            import string
            out[key] = {"tokens": [f for _, f, _, _ in string.Formatter().parse(value) if f]}
        else:
            out[key] = list(value) if isinstance(value, tuple) else value
    from .core import extensions

    for values in extensions.settings_snapshot(store).values():  # 켠 확장의 설정도 같은 자리에(이전 빌드와 같은 꼴)
        out.update(values)
    channels = store.list_channels()
    out["channels"] = [{"index": i, "quality": c.quality, "extension": c.extension, "auto_record": c.auto_record}
                       for i, c in enumerate(channels, 1)]
    out["secrets"] = {"cookies": credentials.exists(cfg), "telegram": cfg.telegram_path.exists(),
                      "account": cfg.account_path.exists(), "setup_code": cfg.setup_code_path.exists()}  # 있는지만
    return out


def _log_lines(log_dir: Path) -> list[dict]:
    files = sorted(log_dir.glob("live-recorder.log*"), key=lambda p: p.stat().st_mtime)
    lines: list[str] = []
    for f in files:
        try:
            lines.extend(f.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    out = []
    for line in lines[-LOG_LINES:]:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _log_summary(records: list[dict]) -> dict[str, Any]:
    groups: dict[tuple, dict[str, Any]] = {}
    levels = Counter()
    for rec in records:
        level = rec.get("level")
        levels[level] += 1
        if level not in ("WARNING", "ERROR", "CRITICAL"):
            continue
        logger = rec.get("logger") if isinstance(rec.get("logger"), str) else ""
        tmpl = rec.get("tmpl") if isinstance(rec.get("tmpl"), str) and logger != "live_recorder.receiver" else None
        exc = None
        if isinstance(rec.get("exc"), str):
            last = rec["exc"].strip().splitlines()[-1] if rec["exc"].strip() else ""
            exc = last.split(":", 1)[0][:80]  # 예외 형식 이름만
        key = (level, logger, rec.get("event") if isinstance(rec.get("event"), str) else None, tmpl, exc)
        g = groups.setdefault(key, {"level": key[0], "logger": key[1], "event": key[2], "template": key[3],
                                    "exception": key[4], "count": 0, "first": rec.get("ts"), "last": rec.get("ts")})
        g["count"] += 1
        g["last"] = rec.get("ts")
    ordered = sorted(groups.values(), key=lambda g: (g["level"] != "ERROR", -g["count"]))
    return {"lines_read": len(records), "levels": dict(levels), "problems": ordered}


def _sessions(store: st.Store, records: list[dict], limit: int) -> list[dict]:
    index = {c.channel_id: i for i, c in enumerate(store.list_channels(), 1)}
    refreshes = Counter(r.get("session_id") for r in records if r.get("event") == "url_refresh")
    out = []
    for s in store.list_sessions(None, limit):
        row = {k: s.get(k) for k in SESSION_KEYS}
        row["session"] = s["id"]
        row["channel"] = index.get(s["channel_id"])  # 지운 채널이면 null
        row["refreshes"] = refreshes.get(s["id"], 0)
        out.append(row)
    return out


def build(cfg: AppConfig, symptom: str = "other", *, session_limit: int = 200) -> bytes:
    if symptom not in SYMPTOMS:
        raise ValueError(f"증상은 {', '.join(SYMPTOMS)} 가운데 하나입니다")
    records = _log_lines(cfg.log_dir)
    from .core.secretstore import read_setup_code

    code = read_setup_code(cfg.setup_code_path)
    store = st.Store(cfg.db_path, cfg.record_root) if cfg.db_path.exists() else None
    enabled = store.enabled_platforms() if store else []
    try:
        summary = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symptom": symptom,
            "program": PROGRAM_NAME,
            "versions": {"live_recorder": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
                         "ffmpeg": _tool_version(cfg.ffmpeg), "ffprobe": _tool_version(cfg.ffprobe),
                         **_platform_tools(enabled)},
            "tools": _platform_tool_status(cfg, enabled),
            "gates": _gates(store, enabled),  # 요청 창구. 외부 도구의 판·출처·받아 둔 판·마지막 갱신
            "disk": {"data": _disk(cfg.data_dir), "recordings": _disk(cfg.record_root), "logs": _disk(cfg.log_dir)},
        }
        parts = {"summary.json": summary, "log_summary.json": _log_summary(records)}
        if store:
            parts["settings.json"] = _settings(cfg, store)
        limit = session_limit
        while True:
            if store:
                parts["sessions.json"] = _sessions(store, records, limit)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for name, data in parts.items():
                    text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
                    if code:
                        # 첫 설정 코드는 어디에도 들어가지 않아야 한다. 그래도 섞이면 지운다(이중 방어)
                        text = text.replace(code, "<설정 코드>").replace(code.replace("-", ""), "<설정 코드>")
                    z.writestr(name, text)
            raw = buf.getvalue()
            if len(raw) <= MAX_BYTES or limit <= 1:
                break
            limit //= 2
            parts["log_summary.json"]["problems"] = parts["log_summary.json"]["problems"][: max(10, limit)]
        return raw
    finally:
        if store:
            store.close()


def create(cfg: AppConfig, symptom: str = "other") -> Path:
    data = build(cfg, symptom)
    folder = cfg.log_dir / "diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    name = time.strftime("diagnostic-%Y%m%d-%H%M%S", time.gmtime()) + f"-{os.getpid()}.zip"
    path = folder / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    packs = sorted(folder.glob("diagnostic-*.zip"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    for old in packs[:-KEEP]:
        old.unlink()
    return path
