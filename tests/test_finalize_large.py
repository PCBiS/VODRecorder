"""큰 녹화에서 나온 결함: 큰 파일의 검증 시간 제한, 실패 원본 다시 마무리,
종료 표시 직후의 빈 재시작."""

import argparse
import asyncio
import os
import stat
from pathlib import Path

from live_recorder import cli
from live_recorder.recording import finalize as fin
from live_recorder.core import store as st
from live_recorder.core.config import AppConfig, Settings
from live_recorder.recording.finalize import Finalizer, scaled_timeout

from conftest import CHANNEL, Harness, duration, fast_timings

from test_finalize import make_part


def _script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _finalizer(tmp_path, tools, ffprobe=None) -> Finalizer:
    cfg = AppConfig(data_dir=tmp_path / "data", record_root=tmp_path, ffmpeg=tools[0], ffprobe=ffprobe or tools[1])
    return Finalizer(cfg, st.Store(tmp_path / "data" / "db.sqlite3", tmp_path))


def test_timeouts_scale_with_size():
    gib = 1024 ** 3
    assert scaled_timeout(0, 120, 60) == 120 and scaled_timeout(10 * 1024 ** 2, 120, 60) < 121
    # 34GB 원본의 예: 3분에 0.9GB를 읽는 속도면 전체 읽기에 약 1시간 53분이 걸린다
    measured_seconds = 34e9 / (0.9e9 / 180)
    assert scaled_timeout(int(34e9), 3600, fin.SCAN_PER_GIB) > 2 * measured_seconds
    assert scaled_timeout(int(34e9), 3600, fin.REMUX_PER_GIB) > 4 * 3600
    assert scaled_timeout(80 * gib, 120, fin.RESULT_PROBE_PER_GIB) > 80 * 60


def test_source_probe_reads_only_the_head(tmp_path, media, tools, monkeypatch):
    """원본 검증은 앞부분만 표준 입력으로 넣는다. 파일 경로를 ffprobe에 주지 않는다."""
    part, _ = make_part(media, tmp_path / "ch")
    with open(part, "ab") as f:  # 뒤에 큰 꼬리를 붙인다
        f.write(b"\x00" * (3 * 1024 * 1024))
    monkeypatch.setattr(fin, "HEAD_BYTES", 256 * 1024)
    counted = tmp_path / "bytes.txt"
    args_seen = tmp_path / "args.txt"
    fake = _script(tmp_path / "ffprobe-count", f'echo "$@" > "{args_seen}"\nwc -c > "{counted}"\n'
                                               'echo \'{"streams":[{"codec_type":"video"}],"format":{}}\'\n')
    fz = _finalizer(tmp_path, tools, fake)
    result = asyncio.run(fz.probe(part, head=True))
    assert result.ok and result.has_video
    assert int(counted.read_text().split()[0]) == 256 * 1024
    assert str(part) not in args_seen.read_text() and "pipe:0" in args_seen.read_text()
    # 진짜 ffprobe로도 앞부분만 보고 영상 스트림을 찾는다
    real = asyncio.run(_finalizer(tmp_path, tools).probe(part, head=True))
    assert real.ok and real.has_video, real.detail


def test_slow_full_read_of_source_does_not_fail_finalize(tmp_path, media, tools, monkeypatch):
    """실측 결함 재현: 원본 전체를 읽는 ffprobe가 시간 제한에 걸리는 상황. 수신기의 길이 합을 쓰면 통과한다."""
    part, final = make_part(media, tmp_path / "ch")
    slow = _script(tmp_path / "ffprobe-slow",
                   'for a; do last="$a"; done\n'
                   f'case "$last" in *.remux.part) ;; *.part) sleep 30; exit 1;; esac\nexec "{tools[1]}" "$@"\n')
    monkeypatch.setattr(fin.Finalizer, "scan_duration", lambda *a, **k: (_ for _ in ()).throw(AssertionError("전체 읽기")))
    fz = _finalizer(tmp_path, tools, slow)
    status, detail, out = asyncio.run(fz.finalize_file(part, final, Settings(), expected=20.0))
    assert status == "done" and out == final and not part.exists(), detail
    assert abs(duration(tools, final) - 20.0) < 1.0


def test_unknown_length_reads_whole_source_with_scaled_limit(tmp_path, media, tools, monkeypatch):
    """서버 재기동으로 수신기의 길이 합을 모르면 크기에 맞춘 제한으로 전체를 읽는다."""
    part, final = make_part(media, tmp_path / "ch")
    limits = []
    real_run = fin.Finalizer._run

    async def spy(self, args, timeout, **kw):
        limits.append((args[-1], timeout))
        return await real_run(self, args, timeout, **kw)

    monkeypatch.setattr(fin.Finalizer, "_run", spy)
    status, detail, _ = asyncio.run(_finalizer(tmp_path, tools).finalize_file(part, final, Settings(), expected=None))
    assert status == "done", detail
    scans = [t for a, t in limits if a == str(part)]
    assert scans and scans[0] >= fin.SCAN_TIMEOUT  # 원본 전체 읽기는 길이 측정 한 번뿐이다
    assert any(a == "pipe:0" and t == fin.PROBE_TIMEOUT for a, t in limits)


def _failed_session(store: st.Store, media: Path, folder: Path, *, garbage: bool = False, **extra) -> tuple[int, Path, Path]:
    """마무리 실패로 원본이 `.raw.mp4`로 남은 세션을 만든다."""
    part, final = make_part(media, folder, "[2026-11-11 19h05m52s] 채널 방송 1080p60.mp4")
    raw = final.with_name(final.stem + ".raw.mp4")
    if garbage:
        part.write_bytes(b"\x00" * 50000)
    os.rename(part, raw)
    sid = store.create_session(channel_id=CHANNEL, live_id="1001", title="방송", source="timemachine",
                               part_path=str(part), final_path=str(final), ended_at=st.now_iso(),
                               end_reason=st.END_BROADCAST, exit_code=0, clean_exit=1,
                               first_media_time="2026-11-11T03:00:00+00:00", last_media_time="2026-11-11T03:00:20+00:00",
                               media_seconds=20.0, finalize_status="failed", output_path=str(raw),
                               finalize_detail="원본 검증 실패(ffprobe rc=-1: timeout). 원본 보존: " + raw.name, **extra)
    return sid, raw, final


def test_failed_raw_is_refinalized_once_on_start(tmp_path, server, media, tools):
    h = Harness(tmp_path, server, tools)
    h.add_channel(auto_record=False)
    sid, raw, final = _failed_session(h.store, media, h.root / "ch1")
    bad_sid, bad_raw, _ = _failed_session(h.store, media, h.root / "ch1b", garbage=True)
    gone_sid, gone_raw, _ = _failed_session(h.store, media, h.root / "ch1c")
    gone_raw.unlink()

    async def scenario():
        async with h:
            await h.until(lambda: h.store.get_session(sid)["finalize_status"] == "done"
                          and h.store.get_session(bad_sid)["finalize_status"] == "failed"
                          and h.store.get_session(bad_sid)["finalize_retries"] == 1, what="다시 마무리")
            await h.engine.wait_finalizers()

    asyncio.run(scenario())
    s = h.store.get_session(sid)
    assert s["finalize_retries"] == 1 and s["output_path"] == str(final) and final.exists() and not raw.exists()
    assert "다시 마무리" in s["finalize_detail"] and abs(duration(tools, final) - 20.0) < 1.0
    bad = h.store.get_session(bad_sid)
    assert bad_raw.exists() and bad["output_path"] == str(bad_raw) and bad_raw.stat().st_size == 50000  # 원본을 남긴다
    assert h.store.get_session(gone_sid)["finalize_retries"] == 0  # 원본이 없으면 손대지 않는다
    detail = bad["finalize_detail"]

    async def again():  # 다시 실패한 것은 다음 기동에 더 시도하지 않는다
        async with h:
            await asyncio.sleep(0.5)
            await h.engine.wait_finalizers()

    h2 = Harness(tmp_path, server, tools)
    h = h2
    asyncio.run(again())
    bad = h2.store.get_session(bad_sid)
    assert bad["finalize_retries"] == 1 and bad["finalize_detail"] == detail and bad_raw.exists()


def test_cli_refinalize_by_session_number(tmp_path, media, tools, capsys):
    cfg = AppConfig(data_dir=tmp_path / "data", record_root=tmp_path / "rec", ffmpeg=tools[0], ffprobe=tools[1])
    (tmp_path / "rec").mkdir()
    store = st.Store(cfg.db_path, cfg.record_root)
    store.add_channel(CHANNEL, "채널", "ch1", auto_record=False)
    sid, raw, final = _failed_session(store, media, cfg.record_root / "ch1", finalize_retries=1)
    assert cli.cmd_refinalize(cfg, argparse.Namespace(session_id=sid)) == 0  # 횟수와 상관없이 CLI는 한다
    assert "마무리=done" in capsys.readouterr().out
    assert final.exists() and not raw.exists() and store.get_session(sid)["finalize_retries"] == 2
    assert cli.cmd_refinalize(cfg, argparse.Namespace(session_id=sid)) == 1  # 이제 남은 원본이 없다
    assert cli.cmd_refinalize(cfg, argparse.Namespace(session_id=9999)) == 1


def test_empty_restart_after_endlist_is_broadcast_end(tmp_path, server, tools):
    h = Harness(tmp_path, server, tools, timings=fast_timings(post_end_suppress=10.0))  # 기본은 60초
    h.add_channel()
    h.store.set_settings({"rewind_seconds": 0})
    h.enable_telegram()
    b = server.go_live(title="끝나는 방송")
    states = []

    async def scenario():
        async with h:
            await h.until(h.recording, what="녹화 시작")
            await asyncio.sleep(1.0)
            b.freeze()
            b.ended = True  # 재생목록은 끝났는데 API는 아직 방송 중이라고 답한다
            await h.until(lambda: h.sessions() and h.sessions()[0]["ended_at"], what="첫 파일 끝")
            for _ in range(40):
                states.append(h.state()["state"])
                await asyncio.sleep(0.05)
            b.api_open = False
            await h.until(lambda: h.finalized(1), what="마무리")
            await asyncio.sleep(0.5)

    asyncio.run(scenario())
    sessions = h.sessions()
    assert [s["end_reason"] for s in sessions] == ["broadcast_end"], h.brief()
    assert h.state()["error_count"] == 0 and not h.state()["error_flag"]
    assert st.BACKOFF not in states
    texts = " ".join(m.get("text", "") for m in server.tg_messages)
    assert "시작 실패" not in texts
    assert "endlist_again" in h.log_text()
