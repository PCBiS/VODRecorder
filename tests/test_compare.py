"""비교 도구. 합성 파일로 같음·짧음·늦게 시작함을 구분한다."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from live_recorder import compare
from live_recorder.core import store as st
from live_recorder.core.config import AppConfig

from conftest import CHANNEL, ROOT

BASE_NAME = "[2026-11-11 19h00m00s] 채널 방송 1080p60.mp4"


def make(tools, path: Path, seconds: int) -> Path:
    subprocess.run([tools[0], "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050", "-t", str(seconds), "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "24k", "-movflags", "+faststart",
                    str(path)], check=True)
    return path


@pytest.fixture(scope="module")
def files(tools, tmp_path_factory):
    d = tmp_path_factory.mktemp("cmp")
    return {"base": make(tools, d / BASE_NAME, 60), "same": make(tools, d / "same.mp4", 60),
            "short": make(tools, d / "short.mp4", 20)}


@pytest.fixture
def cfg(tmp_path, files):
    root = tmp_path / "rec"
    root.mkdir()
    c = AppConfig(data_dir=tmp_path / "data", record_root=root)
    c.data_dir.mkdir()
    s = st.Store(c.db_path, root)
    s.add_channel(CHANNEL, "채널", "a")
    for name, first in (("same", "2026-11-11T10:00:05+00:00"), ("short", "2026-11-11T10:00:00+00:00")):
        s.create_session(channel_id=CHANNEL, output_path=str(files[name]), first_media_time=first, ended_at="x")
    s.close()
    return c


def run_cli(cfg, *args):
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(cfg.data_dir), LIVE_RECORDER_RECORD_ROOT=str(cfg.record_root),
               PYTHONPATH=str(ROOT), LIVE_RECORDER_FFPROBE=cfg.ffprobe)
    return subprocess.run([sys.executable, "-m", "live_recorder", "compare", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def test_same_file_passes(files, cfg, tools):
    cfg = AppConfig(data_dir=cfg.data_dir, record_root=cfg.record_root, ffprobe=tools[1])
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in files.values()}
    r = compare.compare(files["base"], files["same"], cfg, tools[1])
    assert r["pass"] is True
    checks = {c["item"]: c["verdict"] for c in r["checks"]}
    assert checks == {"길이": "통과", "앞부분": "통과", "영상 코덱": "같음", "오디오 코덱": "같음", "해상도 높이": "같음",
                      "프레임레이트": "같음"}
    assert r["baseline"]["first_time"] == "2026-11-11T19:00:00+09:00" and r["baseline"]["first_time_estimated"] is True
    assert "추정" in r["baseline"]["first_time_how"] and "세션 기록" in r["new"]["first_time_how"]
    assert r["new"]["first_time"] == "2026-11-11T10:00:05+00:00"
    assert r["baseline"]["video_codec"] == "h264" and r["baseline"]["height"] == 90 and r["baseline"]["frame_rate"] == 10
    assert r["duration_source"] == "header"
    assert {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in files.values()} == before  # 읽기만 한다


def test_short_and_late_are_detected(files, cfg, tools):
    r = compare.compare(files["base"], files["short"], cfg, tools[1])
    checks = {c["item"]: c for c in r["checks"]}
    assert checks["길이"]["verdict"] == "불일치" and r["pass"] is False
    # 늦게 시작함: 새 파일의 첫 시각이 기준보다 30초 늦다
    s = st.Store(cfg.db_path, cfg.record_root)
    s.create_session(channel_id=CHANNEL, output_path=str(files["same"]), first_media_time="2026-11-11T10:00:30+00:00", ended_at="x")
    s.close()
    r = compare.compare(files["base"], files["same"], cfg, tools[1])
    checks = {c["item"]: c for c in r["checks"]}
    assert checks["길이"]["verdict"] == "통과" and checks["앞부분"]["verdict"] == "불일치" and r["pass"] is False
    assert "+30.0초" in checks["앞부분"]["detail"]
    # 되감기를 주면 기준 시각이 그만큼 앞으로 간다
    r = compare.compare(files["base"], files["same"], cfg, tools[1], rewind=3600)
    assert r["baseline"]["first_time"] == "2026-11-11T18:00:00+09:00"


def test_longer_needs_review_and_full_scan(files, cfg, tools):
    r = compare.compare(files["short"], files["base"], None, tools[1], full=True)
    checks = {c["item"]: c["verdict"] for c in r["checks"]}
    assert checks["길이"] == "확인 필요" and checks["앞부분"] == "판정 불가"
    assert r["duration_source"].startswith("packets") and abs(r["new"]["duration"] - 60) < 1.5
    assert "duration_header" in r["new"]


def test_cli_text_and_json(files, cfg, tools):
    cfg = AppConfig(data_dir=cfg.data_dir, record_root=cfg.record_root, ffprobe=tools[1])
    r = run_cli(cfg, str(files["base"]), str(files["same"]))
    assert r.returncode == 0, r.stderr
    assert "| 길이 | 통과 |" in r.stdout and "추정" in r.stdout and "전환 기준(길이·앞부분): 통과" in r.stdout
    r = run_cli(cfg, str(files["base"]), str(files["short"]), "--json")
    assert r.returncode == 1
    data = json.loads(r.stdout)
    assert data["pass"] is False and data["baseline"]["duration"] > data["new"]["duration"]
    assert run_cli(cfg, str(files["base"]), "/nope.mp4").returncode == 2
