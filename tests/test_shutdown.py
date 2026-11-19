"""기동과 종료. 서버 종료 중 녹화."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from live_recorder.core import store as st
from live_recorder.core.config import AppConfig
from live_recorder.recording.engine import AlreadyRunning, Engine, InstanceLock, StartupError

from conftest import CHANNEL, ROOT, Harness, duration, fast_timings, probe


def _serve(env: dict) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "live_recorder", "serve", "--quiet"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def _wait(pred, timeout=30.0, what="조건"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"시간 안에 만족하지 못했다: {what}")


def test_sigterm_while_recording_then_finalize_on_restart(tmp_path, server, tools):
    """녹화 중 SIGTERM. 파일이 읽히고, 다음 기동에 마무리된다. CLI 프로세스로 실제 신호를 보낸다."""
    data, root = tmp_path / "data", tmp_path / "rec"
    root.mkdir()
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(data), LIVE_RECORDER_RECORD_ROOT=str(root),
               LIVE_RECORDER_FFMPEG=tools[0], LIVE_RECORDER_FFPROBE=tools[1],
               LIVE_RECORDER_API_BASE=server.base + "/api", LIVE_RECORDER_ALLOWED_HOSTS="127.0.0.1",
               LIVE_RECORDER_ALLOW_HTTP="1", PYTHONPATH=str(ROOT))
    data.mkdir()
    store = st.Store(data / "recorder.sqlite3", root)
    store.add_channel(CHANNEL, "시험채널", "ch1")
    store.set_settings({"rewind_seconds": 30})
    b = server.go_live(initial=60, rate=4, title="종료 시험")

    proc = _serve(env)
    try:
        s = _wait(lambda: next((x for x in store.list_sessions() if x["part_path"] and os.path.exists(x["part_path"])
                                and os.path.getsize(x["part_path"]) > 200_000), None), what="녹화 중")
        part = Path(s["part_path"])
        receiver_pid = s["pid"]
        assert Path(f"/proc/{receiver_pid}").exists()
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0, proc.stderr.read()[-2000:]
        assert time.monotonic() - t0 < 15
    finally:
        if proc.poll() is None:
            proc.kill()
    # 수신기도 같이 끝났고, 파일은 닫혀서 읽힌다
    assert not Path(f"/proc/{receiver_pid}").exists()
    s = store.get_session(s["id"])
    assert s["end_reason"] == "server_shutdown" and s["ended_at"] and s["finalize_status"] == "pending", s
    assert part.exists() and not Path(s["final_path"]).exists()
    info = probe(tools, part)
    assert any(x["codec_type"] == "video" for x in info["streams"])
    assert InstanceLock.held(data / "instance.lock") is False

    # 다음 기동: 방송은 끝나 있다. 남은 .part를 마무리한다
    b.end()
    proc = _serve(env)
    try:
        s2 = _wait(lambda: (lambda x: x if x["finalize_status"] in ("done", "failed") else None)(store.get_session(s["id"])),
                   what="재기동 뒤 마무리")
        assert InstanceLock.held(data / "instance.lock") is True
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
    assert s2["finalize_status"] == "done", s2
    out = Path(s2["output_path"])
    assert out.exists() and not part.exists() and out.name.endswith("종료 시험 1080p60.mp4")
    assert duration(tools, out) >= 30  # 되감기 30초가 들어 있다
    assert len(store.list_sessions()) == 1  # 끝난 방송을 다시 녹화하지 않았다


def test_orphan_part_and_stale_temp_are_handled_on_start(tmp_path, server, tools, media):
    """기록이 있는 `.part`는 마무리하고, 기록이 없는 `.part`는 손대지 않고 로그·알림만 남긴다.
    변환 임시 파일은 기록이 있는 세션의 것만 지운다."""
    h = Harness(tmp_path, server, tools)
    h.add_channel()
    h.enable_telegram(notify_dedupe_seconds=0)
    folder = h.root / "ch1"
    folder.mkdir()

    def fill(path, count=12):
        with open(path, "wb") as f:
            f.write((media / "init.mp4").read_bytes())
            for i in range(count):
                f.write((media / f"seg{i:05d}.m4s").read_bytes())

    recorded = folder / "지난 녹화.mp4.part"
    fill(recorded)
    rec_id = h.store.create_session(channel_id=CHANNEL, live_id="8", part_path=str(recorded),
                                    final_path=str(folder / "지난 녹화.mp4"), pid=999998)
    known_temp = folder / "지난 녹화.mp4.remux.part"
    known_temp.write_bytes(b"junk")
    orphan = folder / "완성본을 이름만 바꾼 것.mp4.part"
    fill(orphan)
    orphan_bytes = orphan.read_bytes()
    stale = folder / "모르는 것.mp4.remux.part"
    stale.write_bytes(b"junk")
    # 지난 실행이 닫지 못한 세션(파일 없음)
    open_id = h.store.create_session(channel_id=CHANNEL, live_id="9", part_path=str(folder / "없는.mp4.part"),
                                     final_path=str(folder / "없는.mp4"), pid=999999)

    async def scenario():
        async with h:
            await h.until(lambda: h.store.get_session(rec_id)["finalize_status"] == "done", what="기록 있는 파일 마무리")
            await h.engine.wait_finalizers()
            await h.until(lambda: any("기록 없는 임시 파일" in m["text"] for m in server.tg_messages), what="알림")

    asyncio.run(scenario())
    assert sorted(p.name for p in folder.iterdir()) == sorted(
        ["지난 녹화.mp4", "완성본을 이름만 바꾼 것.mp4.part", "모르는 것.mp4.remux.part"])
    assert abs(duration(tools, folder / "지난 녹화.mp4") - 12) < 1.5
    assert orphan.read_bytes() == orphan_bytes and stale.read_bytes() == b"junk"  # 손대지 않았다
    assert len(h.sessions()) == 2  # 기록 없는 파일로 세션을 만들지 않았다
    assert "recover_orphan" in h.log_text()
    closed = h.store.get_session(open_id)
    assert closed["ended_at"] and closed["end_reason"] == "server_shutdown" and closed["finalize_status"] == "skipped"


def test_second_instance_is_refused(tmp_path, server, tools):
    h = Harness(tmp_path, server, tools)

    async def scenario():
        async with h:
            other = Engine(h.cfg, fast_timings())
            with pytest.raises(AlreadyRunning):
                await other.start()
            assert InstanceLock.held(h.cfg.lock_path) is True
        again = Engine(h.cfg, fast_timings())  # 풀린 뒤에는 뜬다
        await again.start()
        await again.stop()

    asyncio.run(scenario())
    # 다른 프로세스에서는 거부된다
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(h.data), LIVE_RECORDER_RECORD_ROOT=str(h.root),
               LIVE_RECORDER_FFMPEG=tools[0], LIVE_RECORDER_FFPROBE=tools[1], PYTHONPATH=str(ROOT))
    first = _serve(env)
    try:
        _wait(lambda: InstanceLock.held(h.cfg.lock_path), what="첫 인스턴스 기동")
        second = subprocess.run([sys.executable, "-m", "live_recorder", "serve", "--quiet"], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=30)
        assert second.returncode == 2 and "이미 실행 중" in second.stderr
    finally:
        first.send_signal(signal.SIGTERM)
        assert first.wait(timeout=20) == 0


def test_missing_tools_stop_startup(tmp_path, server, tools):
    """ffmpeg, ffprobe, 수신 도구가 없으면 기동을 멈춘다."""
    for kwargs in ({"ffmpeg": str(tmp_path / "no-ffmpeg")}, {"receiver_cmd": (str(tmp_path / "no-receiver"),)}):
        h = Harness(tmp_path / str(len(kwargs)) / next(iter(kwargs)), server, tools, **kwargs)

        async def scenario():
            engine = Engine(h.cfg, fast_timings())
            with pytest.raises(StartupError):
                await engine.start()
            assert InstanceLock.held(h.cfg.lock_path) is False

        asyncio.run(scenario())
    bad = AppConfig(data_dir=tmp_path / "d3", record_root=tmp_path / "r3", ffmpeg=tools[0], ffprobe=str(tmp_path / "nope"))
    (tmp_path / "r3").mkdir()

    async def scenario2():
        with pytest.raises(StartupError):
            await Engine(bad, fast_timings()).start()

    asyncio.run(scenario2())


def test_corrupt_store_stops_serve(tmp_path, tools):
    """저장소가 깨졌으면 serve가 오류로 끝난다."""
    data, root = tmp_path / "data", tmp_path / "rec"
    data.mkdir()
    root.mkdir()
    (data / "recorder.sqlite3").write_bytes(b"garbage" * 1000)
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(data), LIVE_RECORDER_RECORD_ROOT=str(root),
               LIVE_RECORDER_FFMPEG=tools[0], LIVE_RECORDER_FFPROBE=tools[1], PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-m", "live_recorder", "serve", "--quiet"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "기동 실패" in r.stderr
    assert (data / "recorder.sqlite3").read_bytes().startswith(b"garbage")


def test_cli_roundtrip(tmp_path, tools):
    """CLI: 채널·설정·쿠키. 쿠키 값은 인자로 받지 않고 다시 내보내지 않는다."""
    data, root = tmp_path / "data", tmp_path / "rec"
    root.mkdir()
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(data), LIVE_RECORDER_RECORD_ROOT=str(root), PYTHONPATH=str(ROOT))

    def cli(*args, stdin=None):
        return subprocess.run([sys.executable, "-m", "live_recorder", *args], cwd=ROOT, env=env, input=stdin,
                              capture_output=True, text=True, timeout=30)

    assert cli("channel", "add", CHANNEL, "--name", "채널", "--dir", "a").returncode == 0
    assert cli("channel", "add", CHANNEL, "--name", "채널", "--dir", "a").returncode == 1  # 중복
    assert cli("channel", "add", "f" * 32, "--name", "x", "--dir", "../밖").returncode == 1
    assert cli("settings", "set", "rewind_seconds=7200").returncode == 1
    assert "rewind_seconds = 600" in cli("settings", "set", "rewind_seconds=600").stdout
    r = cli("cookie", "set", stdin=json.dumps({"NID_AUT": "SECRET-AUT", "NID_SES": "SECRET-SES"}))
    assert r.returncode == 0 and "SECRET" not in r.stdout + r.stderr and "설정됨" in r.stdout
    assert "SECRET" not in cli("cookie", "status").stdout
    status = json.loads(cli("status", "--json").stdout)
    assert status["server_running"] is False and status["channels"][0]["state"] == "stopped"
    assert cli("start", CHANNEL).returncode == 1  # 서버가 없다
    assert cli("channel", "set", CHANNEL, "--auto", "off").returncode == 0
    assert "자동=끔" in cli("channel", "list").stdout
    assert cli("channel", "remove", CHANNEL).returncode == 0 and cli("channel", "list").stdout == ""


def test_created_files_are_owner_only(tmp_path, server, tools):
    """파일과 폴더는 소유자 전용이다(umask 077): 녹화 중인 .part, 완성본, 채널 폴더, 저장소의 -wal·-shm, 로그.
    저장소도 CLI로 만들어 이 시험 프로세스의 umask가 섞이지 않게 한다."""
    data, root = tmp_path / "data", tmp_path / "rec"
    root.mkdir()
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(data), LIVE_RECORDER_RECORD_ROOT=str(root),
               LIVE_RECORDER_FFMPEG=tools[0], LIVE_RECORDER_FFPROBE=tools[1],
               LIVE_RECORDER_API_BASE=server.base + "/api", LIVE_RECORDER_ALLOWED_HOSTS="127.0.0.1",
               LIVE_RECORDER_ALLOW_HTTP="1", PYTHONPATH=str(ROOT))
    cli = lambda *a: subprocess.run([sys.executable, "-m", "live_recorder", *a], cwd=ROOT, env=env,  # noqa: E731
                                    capture_output=True, text=True, timeout=60)
    assert cli("channel", "add", CHANNEL, "--name", "권한", "--dir", "ch/깊은폴더").returncode == 0
    assert cli("settings", "set", "rewind_seconds=0").returncode == 0
    b = server.go_live(initial=10, rate=4)
    proc = _serve(env)
    try:
        part = _wait(lambda: next((Path(s["part_path"]) for s in _sessions(data, root)
                                   if s["part_path"] and Path(s["part_path"]).exists()
                                   and Path(s["part_path"]).stat().st_size > 0), None), what="녹화 중")
        modes = {"part": part.stat().st_mode & 0o777, "channel folder": part.parent.stat().st_mode & 0o777,
                 "parent folder": part.parent.parent.stat().st_mode & 0o777}
        for name in ("recorder.sqlite3", "recorder.sqlite3-wal", "recorder.sqlite3-shm"):
            modes[name] = (data / name).stat().st_mode & 0o777
        modes["log"] = (data / "logs" / "live-recorder.log").stat().st_mode & 0o777
        b.end()
        final = _wait(lambda: next((Path(s["output_path"]) for s in _sessions(data, root)
                                    if s["finalize_status"] == "done"), None), what="마무리")
        modes["final"] = final.stat().st_mode & 0o777
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
    expect = {k: (0o700 if "folder" in k else 0o600) for k in modes}
    assert {k: oct(v) for k, v in modes.items()} == {k: oct(v) for k, v in expect.items()}


def _sessions(data, root):
    s = st.Store(data / "recorder.sqlite3", root)
    try:
        return s.list_sessions(None, 50)
    finally:
        s.close()
