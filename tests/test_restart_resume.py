"""서버가 꺼졌다 다시 뜬 뒤의 이어받기. 실제 프로세스에 신호를 보낸다."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from live_recorder.core import store as st

from conftest import CHANNEL, ROOT, duration, parse_iso


def _serve(env: dict) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "live_recorder", "serve", "--quiet"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def _wait(pred, timeout=40.0, what="조건"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"시간 안에 만족하지 못했다: {what}")


@pytest.fixture
def site(tmp_path, server, tools):
    data, root = tmp_path / "data", tmp_path / "rec"
    root.mkdir()
    data.mkdir()
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(data), LIVE_RECORDER_RECORD_ROOT=str(root),
               LIVE_RECORDER_FFMPEG=tools[0], LIVE_RECORDER_FFPROBE=tools[1],
               LIVE_RECORDER_API_BASE=server.base + "/api", LIVE_RECORDER_ALLOWED_HOSTS="127.0.0.1",
               LIVE_RECORDER_ALLOW_HTTP="1", PYTHONPATH=str(ROOT))
    store = st.Store(data / "recorder.sqlite3", root)
    store.add_channel(CHANNEL, "시험채널", "ch1")
    store.set_settings({"rewind_seconds": 30})
    return env, store, data


def recording(store, min_bytes=200_000, after_id=0):
    return next((x for x in store.list_sessions() if x["id"] > after_id and x["ended_at"] is None and x["part_path"]
                 and os.path.exists(x["part_path"]) and os.path.getsize(x["part_path"]) > min_bytes), None)


def stop(proc, sig=signal.SIGTERM):
    proc.send_signal(sig)
    proc.wait(timeout=25)


def log_text(data: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted((data / "logs").glob("*.log*")))


def finish(env, store, b):
    """방송을 끝내고 다시 띄워, 받은 것이 있는 세션이 모두 마무리될 때까지 기다린다.
    다시 뜬 서버는 방송이 끝난 것을 알 때까지 한 번 더 이어 받을 수 있다(남은 조각이 있으면 세션이 하나 더 생긴다)."""
    b.end()
    b.api_open = False
    proc = _serve(env)
    try:
        def settled():
            xs = store.list_sessions()
            if any(x["ended_at"] is None or x["finalize_status"] in ("pending", "running") for x in xs):
                return None
            return [x for x in xs if x["finalize_status"] in ("done", "failed")]
        time.sleep(1.0)
        return _wait(settled, what="마무리")
    finally:
        stop(proc)


def test_clean_restart_continues_right_after_the_previous_file(site, server, tools):
    """정상 종료 뒤 다시 뜨면 되감기를 하지 않고 앞 파일의 마지막 조각 바로 뒤부터 받는다. 겹치지도 비지도 않는다."""
    env, store, data = site
    b = server.go_live(initial=120, rate=4, title="이어받기")
    proc = _serve(env)
    try:
        one = _wait(lambda: recording(store), what="녹화 중")
    finally:
        stop(proc)
    one = store.get_session(one["id"])
    assert one["end_reason"] == "server_shutdown" and one["last_media_time"], one
    time.sleep(2.0)  # 꺼져 있는 동안 방송은 계속된다(조각 8개쯤)
    proc = _serve(env)
    try:
        two = _wait(lambda: recording(store, after_id=one["id"]), what="다시 녹화")
        two = _wait(lambda: (lambda x: x if x["first_media_time"] else None)(store.get_session(two["id"])), what="첫 조각")
    finally:
        stop(proc)
    assert two["source"] == "timemachine"
    assert parse_iso(two["first_media_time"]) == parse_iso(one["last_media_time"]), (one["last_media_time"], two["first_media_time"])
    assert two["note"] == "서버가 다시 뜬 뒤 앞 파일에 이어 받음", two["note"]
    assert "restart_resume" in log_text(data) and "restart_resume_gap" not in log_text(data)
    done = finish(env, store, b)
    assert all(x["finalize_status"] == "done" for x in done), [(x["id"], x["finalize_detail"]) for x in done]
    # 세 파일을 이으면 방송의 한 구간이 된다: 길이의 합이 처음부터 끝까지의 시간과 같다
    total = sum(duration(tools, Path(x["output_path"])) for x in done)
    span = (parse_iso(done[-1]["last_media_time"]) - parse_iso(done[0]["first_media_time"])).total_seconds()
    assert abs(total - span) <= 1.5, (total, span)


def test_restart_after_a_kill_continues_from_the_last_saved_point(site, server, tools):
    """갑자기 죽은 경우(SIGKILL). 마지막으로 저장한 지점부터 받는다. 저장이 2초에 한 번이라 그만큼은 앞 파일과 겹칠 수 있다."""
    env, store, data = site
    b = server.go_live(initial=120, rate=4, title="강제 종료")
    proc = _serve(env)
    try:
        one = _wait(lambda: recording(store), what="녹화 중")
        _wait(lambda: store.get_session(one["id"])["last_media_time"], what="진행 저장")
        time.sleep(2.5)
    finally:
        stop(proc, signal.SIGKILL)
    saved = store.get_session(one["id"])
    assert saved["ended_at"] is None and saved["last_media_time"]  # 닫지 못한 채로 남았다
    proc = _serve(env)
    try:
        two = _wait(lambda: recording(store, after_id=one["id"]), what="다시 녹화")
        two = _wait(lambda: (lambda x: x if x["first_media_time"] else None)(store.get_session(two["id"])), what="첫 조각")
    finally:
        stop(proc)
    one = store.get_session(one["id"])
    assert one["end_reason"] == "server_shutdown" and one["end_detail"] == "지난 실행이 닫지 못한 세션", one
    assert parse_iso(two["first_media_time"]) == parse_iso(saved["last_media_time"])
    assert two["note"].startswith("서버가 다시 뜬 뒤 앞 파일에 이어 받음")
    done = finish(env, store, b)
    first = next(x for x in done if x["id"] == one["id"])
    assert first["finalize_status"] == "done", first
    # 앞 파일의 실제 끝(파일 길이로 잰다)과 둘째 파일의 처음: 빈 곳은 없고, 겹침은 저장 주기(2초, 이 방송에서는 조각 8개)에 여유를 둔 만큼 안이다
    real_end = (parse_iso(first["first_media_time"]) - parse_iso("1970-01-01T00:00:00+00:00")).total_seconds() + duration(tools, Path(first["output_path"]))
    start2 = (parse_iso(two["first_media_time"]) - parse_iso("1970-01-01T00:00:00+00:00")).total_seconds()
    assert -1.0 <= real_end - start2 <= 16.0, real_end - start2


def test_outage_longer_than_the_playlist_window_reports_the_gap(site, server, tools):
    """타임머신 재생목록의 창보다 오래 꺼져 있었으면 창의 첫 조각부터 받고, 빈 시간을 메모와 로그에 남긴다."""
    env, store, data = site
    server.tm_window = 20  # 조각 20개(이 방송에서는 5초)만 담는 타임머신 재생목록
    b = server.go_live(initial=120, rate=4, total=390, title="긴 중단")
    proc = _serve(env)
    try:
        one = _wait(lambda: recording(store, min_bytes=50_000), what="녹화 중")
    finally:
        stop(proc)
    one = store.get_session(one["id"])
    time.sleep(9.0)  # 조각 36개가 지나간다(창은 20개)
    proc = _serve(env)
    try:
        two = _wait(lambda: recording(store, min_bytes=50_000, after_id=one["id"]), what="다시 녹화")
        two = _wait(lambda: (lambda x: x if x["first_media_time"] and x["note"] else None)(store.get_session(two["id"])), what="첫 조각")
    finally:
        stop(proc)
    hole = (parse_iso(two["first_media_time"]) - parse_iso(one["last_media_time"])).total_seconds()
    assert hole >= 5.0, hole
    assert two["note"] == f"서버가 다시 뜬 뒤 앞 파일에 이어 받음. 앞 파일과 {hole:.1f}초가 비었습니다", two["note"]
    assert "restart_resume_gap" in log_text(data)
    b.end()


def test_a_new_broadcast_after_restart_is_recorded_as_before(site, server, tools):
    """다시 떴을 때 다른 방송이면 이어받지 않는다. 예전처럼 되감기를 적용한다."""
    env, store, data = site
    server.go_live(live_id="1001", initial=120, rate=4, title="앞 방송")
    proc = _serve(env)
    try:
        one = _wait(lambda: recording(store), what="녹화 중")
    finally:
        stop(proc)
    b = server.go_live(live_id="1002", initial=120, rate=4, title="새 방송")
    proc = _serve(env)
    try:
        two = _wait(lambda: recording(store, after_id=one["id"]), what="새 방송 녹화")
        two = _wait(lambda: (lambda x: x if x["first_media_time"] else None)(store.get_session(two["id"])), what="첫 조각")
        behind = b.pdt(b.published()) - parse_iso(two["first_media_time"])
    finally:
        stop(proc)
    assert two["live_id"] == "1002" and two["note"] is None
    assert behind.total_seconds() >= 25  # 되감기 30초가 들어갔다
    assert "restart_resume" not in log_text(data)
    b.end()


@pytest.mark.parametrize("reason", ["user_stop", "broadcast_end", "error"])
def test_only_a_shutdown_is_continued(site, server, tools, reason):
    """앞 세션이 서버 종료로 끝난 것이 아니면(사용자 중지 등) 이어받지 않는다."""
    env, store, data = site
    b = server.go_live(live_id="1001", initial=120, rate=4, title="중지 뒤")
    sid = store.create_session(channel_id=CHANNEL, live_id="1001", bytes=1000, last_media_time=b.pdt(100).isoformat(),
                               ended_at=st.now_iso(), end_reason=reason, finalize_status="skipped")
    proc = _serve(env)
    try:
        two = _wait(lambda: recording(store, after_id=sid), what="녹화")
        two = _wait(lambda: (lambda x: x if x["first_media_time"] else None)(store.get_session(two["id"])), what="첫 조각")
    finally:
        stop(proc)
    assert two["note"] is None and parse_iso(two["first_media_time"]) != b.pdt(100)
    assert "restart_resume" not in log_text(data)
    b.end()
