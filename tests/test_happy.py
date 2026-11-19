"""기본 흐름: 감지 → 녹화 → 마무리."""

import asyncio
import json
import os
from pathlib import Path

from conftest import CHANNEL, duration, probe


def test_detect_record_finalize(harness, server, tools):
    harness.add_channel(quality="1080p")
    harness.store.set_settings({"rewind_seconds": 0})
    b = server.go_live(title="기본 흐름")

    async def scenario():
        async with harness:
            rec = await harness.until(harness.recording, what="녹화 시작")
            part = Path(rec["part_path"])
            assert part.name.endswith(".mp4.part") and part.exists()
            assert not Path(rec["final_path"]).exists()
            assert rec["pid"] and rec["started_at"]
            assert harness.state()["state"] == "recording"
            await asyncio.sleep(1.0)
            b.end()
            done = await harness.until(lambda: harness.finalized(1), what="마무리")
            return done[0]

    s = asyncio.run(scenario())
    assert s["end_reason"] == "broadcast_end" and s["exit_code"] == 0, s
    assert s["finalize_status"] == "done", s
    out = Path(s["output_path"])
    assert out.exists() and out.suffix == ".mp4" and not Path(s["part_path"]).exists()
    assert "시험채널 기본 흐름 1080p60.mp4" in out.name
    info = probe(tools, out)
    assert any(x["codec_type"] == "video" for x in info["streams"])
    assert "mp4" in info["format"]["format_name"]
    assert abs(duration(tools, out) - s["media_seconds"]) <= 2.0
    assert float(info["format"].get("start_time", 0)) < 1.0
    assert s["source"] == "timemachine" and s["ended_at"] and s["bytes"] > 0
    assert [p.name for p in out.parent.iterdir()] == [out.name]  # 임시 파일이 남지 않는다
    for line in harness.log_text().splitlines():
        assert "ts" in json.loads(line)
    assert oct(os.stat(harness.cfg.db_path).st_mode & 0o777) == "0o600"
