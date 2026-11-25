"""진단팩. zip 안의 모든 파일에서 비밀·식별 값이 0건인지 본다."""

import asyncio
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from live_recorder.platforms import credentials
from live_recorder import diagnostics
from live_recorder.core import secretstore
from live_recorder.core.config import AppConfig

from conftest import CHANNEL, ROOT, Harness

AUT, SES = "AUT-diag-sentinel-0001", "SES-diag-sentinel-0002"
TOKEN, CHAT = "123456:DIAG-TOKEN-sentinel", "-1004242424242"
USER, PASSWORD = "diagadmin", "diag password sentinel"
NAME, TITLE = "진단채널이름", "진단 방송 제목 sentinel"


def test_pack_has_no_secrets_or_identifiers(tmp_path, server, tools):
    logs = tmp_path / "separate-logs"
    h = Harness(tmp_path, server, tools)
    h.cfg = AppConfig(**{**h.cfg.__dict__, "log_dir_override": logs})
    h.add_channel(name=NAME, output_dir="진단폴더")
    h.store.set_settings({"rewind_seconds": 0, "filename_pattern": "{channel_name} 진단패턴글자 {safe_live_title}{file_extension}",
                          "fm_roots": [str(tmp_path)], "trusted_proxies": ["10.0.0.1"]})
    credentials.save(h.cfg, "chzzk", {"NID_AUT": AUT, "NID_SES": SES})
    secretstore.telegram_save(h.cfg.telegram_path, TOKEN, CHAT)
    secretstore.create_account(h.cfg.account_path, USER, PASSWORD)
    b = server.go_live(title=TITLE)

    async def scenario():
        async with h:
            await h.until(h.recording, what="녹화")
            await asyncio.sleep(0.6)
            server.expire_tokens()  # 주소 갱신을 일으킨다
            await asyncio.sleep(1.0)
            b.end()
            await h.until(lambda: h.finalized(1), what="마무리")

    asyncio.run(scenario())
    assert (logs / "live-recorder.log").exists() and not (h.data / "logs").exists()  # 로그 폴더가 따로다
    session = h.sessions()[0]
    file_name = Path(session["output_path"]).name
    # 로그에 이름·제목이 든 줄이 실제로 있다(진단팩이 이것을 옮기지 않는지 보려는 것)
    log_text = (logs / "live-recorder.log").read_text()
    assert file_name in log_text
    with open(logs / "live-recorder.log", "a") as f:
        f.write(json.dumps({"ts": "2026-11-11T00:00:00Z", "level": "ERROR", "logger": "live_recorder.receiver",
                            "msg": f"{NAME} {TITLE} hdnts=SECRET", "exc": f"Traceback\nValueError: {TITLE}"}) + "\n")
        f.write(json.dumps({"ts": "2026-11-11T00:00:01Z", "level": "WARNING", "logger": "live_recorder.engine",
                            "event": "x", "msg": f"녹화 끝 {file_name}", "tmpl": "녹화 끝 %s: %s"}) + "\n")

    path = diagnostics.create(h.cfg, "recording")
    assert path.parent == logs / "diagnostics" and path.stat().st_size <= diagnostics.MAX_BYTES
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    forbidden = [AUT, SES, TOKEN, CHAT, "DIAG-TOKEN", USER, PASSWORD, NAME, TITLE, CHANNEL, file_name, "진단폴더",
                 "진단패턴글자", "hdnts", "hmac", "SECRET", server.base, str(tmp_path)]
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert set(names) == {"summary.json", "log_summary.json", "settings.json", "sessions.json"}
        blob = "\n".join(z.read(n).decode() for n in names)
        parsed = {n: json.loads(z.read(n)) for n in names}
    for value in forbidden:
        assert value not in blob, value
    # 쓸모 있는 값은 들어 있다
    assert parsed["summary.json"]["symptom"] == "recording" and parsed["summary.json"]["versions"]["ffmpeg"].startswith("ffmpeg")
    assert parsed["settings.json"]["channels"] == [{"index": 1, "quality": "best", "extension": ".mp4", "auto_record": True, "video_codec": "h264"}]
    assert parsed["settings.json"]["secrets"] == {"cookies": True, "telegram": True, "account": True, "setup_code": False}
    assert parsed["settings.json"]["filename_pattern"] == {"tokens": ["channel_name", "safe_live_title", "file_extension"]}
    s0 = parsed["sessions.json"][0]
    assert s0["channel"] == 1 and s0["end_reason"] == "broadcast_end" and s0["refreshes"] >= 1 and s0["media_seconds"] > 0
    problems = parsed["log_summary.json"]["problems"]
    assert any(p["template"] == "녹화 끝 %s: %s" for p in problems)
    assert any(p["logger"] == "live_recorder.receiver" and p["template"] is None and p["exception"] == "ValueError"
               for p in problems)


def test_keeps_three_and_cli(tmp_path):
    data, root, logs = tmp_path / "data", tmp_path / "rec", tmp_path / "logs"
    root.mkdir()
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(data), LIVE_RECORDER_RECORD_ROOT=str(root),
               LIVE_RECORDER_LOG_DIR=str(logs), PYTHONPATH=str(ROOT))
    for _ in range(4):
        r = subprocess.run([sys.executable, "-m", "live_recorder", "diagnostics", "--symptom", "start"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
    packs = sorted((logs / "diagnostics").glob("diagnostic-*.zip"))
    assert len(packs) == 3
    with zipfile.ZipFile(packs[-1]) as z:
        assert json.loads(z.read("summary.json"))["symptom"] == "start" and "settings.json" not in z.namelist()
