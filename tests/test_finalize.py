"""마무리. 변환 실패와 검증 실패에서 원본이 남는지."""

import asyncio
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from live_recorder.core import store as st
from live_recorder.core.config import AppConfig, Settings
from live_recorder.recording.finalize import Finalizer, within_tolerance

from conftest import duration, probe


def make_part(media: Path, folder: Path, name: str = "방송.mp4", count: int = 20) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / name
    part = Path(str(final) + ".part")
    with open(part, "wb") as f:
        f.write((media / "init.mp4").read_bytes())
        for i in range(count):
            f.write((media / f"seg{i:05d}.m4s").read_bytes())
    return part, final


def script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def finalizer(tmp_path, tools, ffmpeg=None) -> Finalizer:
    cfg = AppConfig(data_dir=tmp_path / "data", record_root=tmp_path, ffmpeg=ffmpeg or tools[0], ffprobe=tools[1])
    return Finalizer(cfg, st.Store(tmp_path / "data" / "db.sqlite3", tmp_path))


def run(fz, part, final, settings=Settings(), expected=None):
    return asyncio.run(fz.finalize_file(part, final, settings, expected=expected))


def test_tolerance():
    assert within_tolerance(100.0, 101.9) and not within_tolerance(100.0, 102.5)
    assert within_tolerance(36000, 36300) and not within_tolerance(36000, 36400)  # 1%


def test_remux_success_deletes_original_only_after_verify(tmp_path, media, tools):
    part, final = make_part(media, tmp_path / "ch")
    status, detail, out = run(finalizer(tmp_path, tools), part, final)
    assert status == "done" and out == final and final.exists() and not part.exists(), detail
    assert abs(duration(tools, final) - 20.0) < 1.0
    fmt = probe(tools, final)
    assert [s["codec_type"] for s in fmt["streams"]] == ["video", "audio"]
    assert sorted(p.name for p in final.parent.iterdir()) == ["방송.mp4"]


def test_remux_keeps_original_when_asked(tmp_path, media, tools):
    part, final = make_part(media, tmp_path / "ch")
    raw = part.read_bytes()
    status, _, out = run(finalizer(tmp_path, tools), part, final, Settings(delete_original=False))
    assert status == "done" and final.exists() and (final.parent / "방송.raw.mp4").read_bytes() == raw and not part.exists()


def test_remux_to_ts(tmp_path, media, tools):
    part, final = make_part(media, tmp_path / "ch", "방송.ts")
    status, detail, out = run(finalizer(tmp_path, tools), part, final)
    assert status == "done" and "mpegts" in probe(tools, final)["format"]["format_name"], detail


def test_failing_ffmpeg_preserves_original(tmp_path, media, tools):
    """실패하는 가짜 ffmpeg. 원본이 남고 완성본은 생기지 않는다."""
    part, final = make_part(media, tmp_path / "ch")
    raw = part.read_bytes()
    fake = script(tmp_path / "ffmpeg-fail", "echo boom >&2\nexit 1\n")
    status, detail, kept = run(finalizer(tmp_path, tools, fake), part, final)
    assert status == "failed" and "변환 실패" in detail
    assert kept.name == "방송.raw.mp4" and kept.read_bytes() == raw
    assert not final.exists() and not part.exists()
    assert sorted(p.name for p in final.parent.iterdir()) == ["방송.raw.mp4"]


def test_partial_output_from_failing_ffmpeg_is_removed(tmp_path, media, tools):
    part, final = make_part(media, tmp_path / "ch")
    fake = script(tmp_path / "ffmpeg-partial", 'for a; do last="$a"; done\nhead -c 1000 /dev/urandom > "$last"\nexit 1\n')
    status, _, kept = run(finalizer(tmp_path, tools, fake), part, final)
    assert status == "failed" and sorted(p.name for p in final.parent.iterdir()) == ["방송.raw.mp4"]


def test_short_result_preserves_original(tmp_path, media, tools):
    """종료코드는 0이지만 길이가 짧은 결과. 원본을 지우지 않는다."""
    part, final = make_part(media, tmp_path / "ch")
    raw = part.read_bytes()
    # 진짜 ffmpeg에 `-t 5`를 끼워 넣어 앞 5초만 담긴 결과를 만든다
    fake = script(tmp_path / "ffmpeg-short",
                  'n=$#; i=1; set -- "$@"\nargs=""\nfor a; do if [ $i -eq $n ]; then out="$a"; fi; i=$((i+1)); done\n'
                  f'exec "{tools[0]}" -nostdin -loglevel error -y -i "$FAKE_PART" -t 5 -c copy -f mp4 "$out"\n')
    os.environ["FAKE_PART"] = str(part)
    try:
        for expected in (None, 20.0):
            if not part.exists():
                part, final = make_part(media, tmp_path / f"ch{expected}")
                raw = part.read_bytes()
                os.environ["FAKE_PART"] = str(part)
            status, detail, kept = run(finalizer(tmp_path, tools, fake), part, final, expected=expected)
            assert status == "failed" and "길이 불일치" in detail, detail
            assert kept.read_bytes() == raw and not final.exists()
            assert not Path(str(final) + ".remux.part").exists()
    finally:
        os.environ.pop("FAKE_PART", None)


def test_wrong_reported_length_falls_back_to_measuring(tmp_path, media, tools):
    """수신기가 보고한 길이가 틀려도 원본을 직접 재서 맞으면 통과한다."""
    part, final = make_part(media, tmp_path / "ch")
    status, detail, _ = run(finalizer(tmp_path, tools), part, final, expected=500.0)
    assert status == "done", detail


def test_existing_file_is_never_overwritten(tmp_path, media, tools):
    """같은 이름의 완성본이 이미 있으면 번호를 붙인다."""
    part, final = make_part(media, tmp_path / "ch")
    final.write_bytes(b"precious")
    status, _, out = run(finalizer(tmp_path, tools), part, final)
    assert status == "done" and out.name == "방송 (1).mp4" and final.read_bytes() == b"precious"


def test_garbage_and_empty_original(tmp_path, tools):
    folder = tmp_path / "ch"
    folder.mkdir()
    part = folder / "x.mp4.part"
    part.write_bytes(b"\x00" * 50000)  # 무작위 바이트는 가끔 ffprobe가 영상으로 읽어 시험이 흔들린다
    status, detail, kept = run(finalizer(tmp_path, tools), part, folder / "x.mp4")
    assert status == "failed" and "원본 검증 실패" in detail and kept.exists() and kept.stat().st_size == 50000
    part.write_bytes(b"")
    status, _, _ = run(finalizer(tmp_path, tools), part, folder / "x.mp4")
    assert status == "skipped" and not part.exists()


def test_remux_disabled_keeps_container(tmp_path, media, tools):
    part, final = make_part(media, tmp_path / "ch", "방송.ts")
    raw = part.read_bytes()
    status, _, out = run(finalizer(tmp_path, tools), part, final, Settings(remux_enabled=False))
    assert status == "done" and out.name == "방송.mp4" and out.read_bytes() == raw  # 실제 컨테이너(fMP4)를 따른다


def _recorder_ffmpeg(tmp_path, tools) -> tuple[str, Path]:
    """자신의 nice 값과 입출력 등급을 파일에 적고 진짜 ffmpeg로 바뀌는 가짜 ffmpeg."""
    record = tmp_path / "prio.txt"
    fake = tmp_path / "ffmpeg-prio"
    fake.write_text(f"""#!{sys.executable}
import os, sys
sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
from live_recorder import lowprio
if "-version" not in sys.argv:
    with open({str(record)!r}, "a") as f:
        f.write(f"{{os.getpriority(os.PRIO_PROCESS, 0)}} {{lowprio.get_io_class(0)}}\\n")
os.execv({tools[0]!r}, [{tools[0]!r}] + sys.argv[1:])
""")
    fake.chmod(0o755)
    return str(fake), record


def test_remux_runs_at_low_priority(tmp_path, media, tools):
    """변환 ffmpeg는 nice 19, 입출력 idle 등급으로 돈다. 설정을 끄면 그대로다."""
    fake, record = _recorder_ffmpeg(tmp_path, tools)
    part, final = make_part(media, tmp_path / "ch")
    status, detail, _ = run(finalizer(tmp_path, tools, fake), part, final, Settings())
    assert status == "done", detail
    assert record.read_text().split() == ["19", "3"]
    record.unlink()
    part, final = make_part(media, tmp_path / "ch2")
    status, _, _ = run(finalizer(tmp_path, tools, fake), part, final, Settings(finalize_low_priority=False))
    assert status == "done"
    nice, io_class = record.read_text().split()
    assert nice == str(os.getpriority(os.PRIO_PROCESS, 0)) and io_class != "3"


def test_cannot_lower_priority_still_converts(tmp_path, media, tools, monkeypatch):
    """우선순위를 낮출 수 없는 환경을 흉내 낸다. 경고만 남기고 변환은 그대로 끝난다."""
    from live_recorder.recording import finalize as fin
    from live_recorder import lowprio

    launcher = tmp_path / "launcher-denied"
    launcher.write_text(f"""#!{sys.executable}
import os, sys
sys.stderr.write({lowprio.WARNING_PREFIX!r} + " 우선순위를 낮추지 못했다(nice: Operation not permitted). 그대로 실행한다\\n")
sys.stderr.flush()
os.execvp(sys.argv[1], sys.argv[1:])
""")
    launcher.chmod(0o755)
    monkeypatch.setattr(fin.lowprio, "wrap", lambda cmd: [str(launcher), *cmd])
    records = []

    class Catch(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Catch()
    logging.getLogger("live_recorder.finalize").addHandler(handler)
    try:
        part, final = make_part(media, tmp_path / "ch")
        status, detail, out = run(finalizer(tmp_path, tools), part, final, Settings())
    finally:
        logging.getLogger("live_recorder.finalize").removeHandler(handler)
    assert status == "done" and out.exists() and not part.exists(), detail
    assert any(getattr(r, "event", None) == "lowprio_failed" for r in records)
