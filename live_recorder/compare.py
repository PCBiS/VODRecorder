"""다른 녹화 프로그램이 만든 파일(기준)과 새 녹화본 비교.

    python -m live_recorder compare <기준 파일> <새 파일> [--rewind 초] [--full] [--json]

두 파일을 읽기만 한다. 기본은 ffprobe로 머리말만 읽는다(30~47 GB 파일을 전부 읽지 않는다).
`--full`이면 영상 패킷을 끝까지 읽어 실제 길이를 잰다.

첫 프레임의 실제 시각:
- 새 파일: 이 도구의 세션 기록(첫 조각의 PROGRAM-DATE-TIME)에서 얻는다. 기록이 없으면 알 수 없음이다.
- 기준 파일: 파일 이름의 `[YYYY-MM-DD HHhMMmSSs]`(녹화를 시작한 시각, KST)에서 되감기 초(`--rewind`,
  예: 3600)를 뺀 값으로 **추정**한다. 방송이 되감기보다 짧았으면 실제 첫 시각은 이보다 늦다.
  참고로 파일 수정 시각에서 길이를 뺀 값도 함께 적는다(변환에 걸린 시간만큼 늦게 나온다).

판정:
- 길이: 차이가 30초 또는 기준 길이의 0.1% 가운데 큰 값 이내면 통과. 새 파일이 더 길면 "확인 필요"(사유 확인).
- 앞부분: 새 파일의 첫 시각이 기준보다 10초 넘게 늦지 않으면 통과. 어느 한쪽 시각을 모르면 "판정 불가".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core import store as st
from .core.config import AppConfig
from .core.naming import KST

LENGTH_ABS = 30.0
LENGTH_REL = 0.001
START_LATE = 10.0
NAME_TIME = re.compile(r"\[(\d{4})-(\d{2})-(\d{2}) (\d{2})h(\d{2})m(\d{2})s\]")


def probe(ffprobe: str, path: Path) -> dict[str, Any]:
    out = subprocess.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-i", str(path)],
                         capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise ValueError(f"ffprobe가 읽지 못했습니다: {path.name}")
    data = json.loads(out.stdout)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    fmt = data.get("format") or {}

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def rate(v):
        if isinstance(v, str) and "/" in v:
            a, _, b = v.partition("/")
            return round(float(a) / float(b), 3) if float(b) else None
        return num(v)

    return {"container": fmt.get("format_name"), "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name"), "width": video.get("width"), "height": video.get("height"),
            "frame_rate": rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
            "duration": num(fmt.get("duration")), "size": path.stat().st_size}


def scan_duration(ffprobe: str, path: Path) -> float | None:
    """--full. 영상 패킷을 끝까지 읽어 첫 패킷부터 마지막 패킷 끝까지의 길이를 잰다."""
    proc = subprocess.Popen([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "packet=pts_time,duration_time", "-of", "csv=p=0", "-i", str(path)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    lo = hi = None
    assert proc.stdout
    for line in proc.stdout:
        parts = line.strip().split(",")
        try:
            pts = float(parts[0])
        except (ValueError, IndexError):
            continue
        try:
            dur = float(parts[1])
        except (ValueError, IndexError):
            dur = 0.0
        lo = pts if lo is None else min(lo, pts)
        hi = pts + dur if hi is None else max(hi, pts + dur)
    proc.wait()
    return None if lo is None or hi is None else hi - lo


def baseline_start(path: Path, duration: float | None, rewind: float) -> dict[str, Any]:
    m = NAME_TIME.search(path.name)
    named = None
    if m:
        y, mo, d, h, mi, s = map(int, m.groups())
        named = datetime(y, mo, d, h, mi, s, tzinfo=KST)
    est = named - timedelta(seconds=rewind) if named else None
    by_mtime = None
    if duration:
        by_mtime = datetime.fromtimestamp(path.stat().st_mtime, KST) - timedelta(seconds=duration)
    return {"value": est, "estimated": True,
            "how": (f"추정: 파일 이름의 녹화 시작 시각 {named.isoformat()} − 되감기 {rewind:g}초" if named
                    else "알 수 없음: 파일 이름에 [YYYY-MM-DD HHhMMmSSs]가 없습니다"),
            "reference_mtime_minus_duration": by_mtime.isoformat(timespec="seconds") if by_mtime else None}


def new_start(path: Path, cfg: AppConfig | None) -> dict[str, Any]:
    if cfg is None or not cfg.db_path.exists():
        return {"value": None, "estimated": False, "how": "알 수 없음: 데이터 폴더가 없습니다"}
    real = os.path.realpath(path)
    store = st.Store(cfg.db_path, cfg.record_root)
    try:
        for s in reversed(store.list_sessions(None, 100000)):
            if s.get("output_path") and os.path.realpath(s["output_path"]) == real and s.get("first_media_time"):
                when = datetime.fromisoformat(s["first_media_time"])
                return {"value": when, "estimated": False, "how": f"세션 기록 #{s['id']}의 첫 조각 시각"}
    finally:
        store.close()
    return {"value": None, "estimated": False, "how": "알 수 없음: 이 파일의 세션 기록이 없습니다"}


def judge(base: dict, new: dict, base_start: dict, new_start_: dict) -> list[dict[str, str]]:
    rows = []
    bd, nd = base.get("duration"), new.get("duration")
    if bd and nd:
        tol = max(LENGTH_ABS, bd * LENGTH_REL)
        diff = nd - bd
        if abs(diff) <= tol:
            verdict = "통과"
        elif diff > 0:
            verdict = "확인 필요"
        else:
            verdict = "불일치"
        rows.append({"item": "길이", "verdict": verdict,
                     "detail": f"기준 {bd:.1f}초, 새 {nd:.1f}초, 차이 {diff:+.1f}초(허용 ±{tol:.1f}초)"})
    else:
        rows.append({"item": "길이", "verdict": "판정 불가", "detail": "길이를 읽지 못했습니다"})
    bs, ns = base_start.get("value"), new_start_.get("value")
    if bs and ns:
        late = (ns - bs).total_seconds()
        rows.append({"item": "앞부분", "verdict": "통과" if late <= START_LATE else "불일치",
                     "detail": f"새 파일이 기준보다 {late:+.1f}초(허용 +{START_LATE:g}초). 기준 시각은 추정"})
    else:
        rows.append({"item": "앞부분", "verdict": "판정 불가", "detail": "첫 시각을 알 수 없는 파일이 있습니다"})
    for key, label in (("video_codec", "영상 코덱"), ("audio_codec", "오디오 코덱"), ("height", "해상도 높이"),
                       ("frame_rate", "프레임레이트")):
        same = base.get(key) == new.get(key)
        rows.append({"item": label, "verdict": "같음" if same else "다름", "detail": f"기준 {base.get(key)}, 새 {new.get(key)}"})
    return rows


def compare(base_path: Path, new_path: Path, cfg: AppConfig | None, ffprobe: str = "ffprobe", rewind: float = 0.0,
            full: bool = False) -> dict[str, Any]:
    base, new = probe(ffprobe, base_path), probe(ffprobe, new_path)
    if full:
        for info, path in ((base, base_path), (new, new_path)):
            measured = scan_duration(ffprobe, path)
            info["duration_header"], info["duration"] = info["duration"], measured
    bs, ns = baseline_start(base_path, base["duration"], rewind), new_start(new_path, cfg)
    rows = judge(base, new, bs, ns)
    iso = lambda d: d.isoformat(timespec="seconds") if d else None  # noqa: E731
    return {
        "baseline": dict(base, first_time=iso(bs["value"]), first_time_how=bs["how"], first_time_estimated=True,
                         reference_mtime_minus_duration=bs["reference_mtime_minus_duration"]),
        "new": dict(new, first_time=iso(ns["value"]), first_time_how=ns["how"], first_time_estimated=False),
        "duration_source": "packets(--full)" if full else "header",
        "checks": rows,
        "pass": all(r["verdict"] in ("통과", "같음") for r in rows if r["item"] in ("길이", "앞부분")),
    }


def render(result: dict[str, Any]) -> str:
    b, n = result["baseline"], result["new"]
    keys = [("container", "컨테이너"), ("video_codec", "영상 코덱"), ("audio_codec", "오디오 코덱"), ("width", "너비"),
            ("height", "높이"), ("frame_rate", "프레임레이트"), ("duration", "길이(초)"), ("size", "크기(바이트)"),
            ("first_time", "첫 프레임 시각")]
    lines = ["| 항목 | 기준 | 새 |", "|---|---|---|"]
    for key, label in keys:
        lines.append(f"| {label} | {b.get(key)} | {n.get(key)} |")
    lines.append(f"| 첫 시각 근거 | {b['first_time_how']} | {n['first_time_how']} |")
    if b.get("reference_mtime_minus_duration"):
        lines.append(f"| 참고: 수정 시각 − 길이 | {b['reference_mtime_minus_duration']} | |")
    lines += ["", f"길이 출처: {'패킷 전체 읽기' if result['duration_source'].startswith('packets') else '머리말'}", "",
              "| 판정 | 결과 | 내용 |", "|---|---|---|"]
    for r in result["checks"]:
        lines.append(f"| {r['item']} | {r['verdict']} | {r['detail']} |")
    lines += ["", "전환 기준(길이·앞부분): " + ("통과" if result["pass"] else "통과 못 함 또는 판정 불가")]
    return "\n".join(lines)


def to_json(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)
