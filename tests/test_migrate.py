"""기존 설정 이전. 입력은 실제 설정을 닮게 직접 만든 가짜 파일이다."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from live_recorder.platforms import credentials
from live_recorder.core import extensions, secretstore
from live_recorder.core import store as st
from live_recorder.core.config import AppConfig, Settings

from conftest import ROOT

CH = "0123456789abcdef0123456789abcdef"
AUT, SES = "AUT-migrate-sentinel-0001", "SES-migrate-sentinel-0002"
TOKEN, CHAT = "123456:MIGRATE-TOKEN-sentinel", "-1009876543210"
PATTERN = "[{legacy_time}] {channel_name} {safe_live_title} {record_quality}{frame_rate}{file_extension}"  # 기존 프로그램의 값. 이 프로그램에는 모르는 토큰
NEW_PATTERN = "[{date} {time}] {channel_name} {safe_live_title} {record_quality}{frame_rate}{file_extension}"  # 기본 패턴


def write(folder: Path, name: str, data) -> None:
    (folder / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def old(tmp_path):
    """실제와 닮은 기존 설정: 치지직 채널 1개(best, mp4), timemachine_plus 3600초, 텔레그램 켬, 파일 관리자 켬·휴지통 끔."""
    folder = tmp_path / "old-config"
    folder.mkdir()
    fm_root = tmp_path / "old-recordings-root"
    fm_root.mkdir()
    write(folder, "channels.json", [
        {"id": CH, "name": "기존채널", "platform": "chzzk", "output_dir": "/recordings/기존채널", "quality": "best",
         "extension": "mp4", "record_enabled": True, "gpu_index": 1, "recordWatchParty": False},
        {"id": "f" * 32, "name": "씨미채널", "platform": "cime", "output_dir": "/recordings/x", "quality": "best",
         "extension": ".mp4", "record_enabled": True},
        {"id": "e" * 32, "name": "같이보기", "platform": "chzzk", "output_dir": "/recordings/y", "quality": "1080p60",
         "extension": ".ts", "record_enabled": True, "recordWatchParty": True},
        {"id": "d" * 32, "name": "밖", "platform": "chzzk", "output_dir": "/etc/outside", "quality": "best",
         "extension": ".mp4", "record_enabled": False},
    ])
    write(folder, "config.json", {
        "recheckInterval": 60, "filenamePattern": PATTERN, "autoPostProcessing": True, "deleteAfterPostProcessing": True,
        "plugin_type": "timemachine_plus", "timemachine_time_shift": 3600, "fileManagerEnabled": True,
        "fileManagerRoots": [str(fm_root), "/etc"], "fileManagerReadOnly": False, "trashEnabled": False,
        "replayEnabled": True, "replayDownloadRoot": "/recordings/replay", "replayMinFreeGiB": 5,
        "autoMoveEnabled": False, "splitRecording": False, "gpuProfile": "x"})
    write(folder, "cookie.json", {"chzzk": {"NID_AUT": AUT, "NID_SES": SES, "OTHER": "x"}, "youtube": {"SID": "y"}})
    write(folder, "notification.json", {
        "telegram_enabled": True, "telegram_bot_token": TOKEN, "telegram_chat_id": CHAT,
        "events": {"record_start": True, "record_end": True, "finalize_done": False, "old_unknown_event": True},
        "limits": {"dedupe_seconds": 120, "max_per_hour": 10}, "discord_webhook": "https://discord.example/x"})
    return folder, fm_root


@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "rec"
    root.mkdir()
    return AppConfig(data_dir=tmp_path / "data", record_root=root)


def snapshot(folder: Path) -> dict:
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(folder.iterdir())}


def cli(cfg, *args, **kw):
    env = dict(os.environ, LIVE_RECORDER_DATA_DIR=str(cfg.data_dir), LIVE_RECORDER_RECORD_ROOT=str(cfg.record_root),
               PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-m", "live_recorder", "migrate", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60, **kw)


def assert_no_secret(text: str) -> None:
    for secret in (AUT, SES, TOKEN, CHAT, "MIGRATE-TOKEN"):
        assert secret not in text, secret


def test_dry_run_writes_nothing_and_hides_secrets(old, cfg):
    folder, _ = old
    before = snapshot(folder)
    r = cli(cfg, "--from", str(folder), "--map", f"/recordings={cfg.record_root}")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert_no_secret(out + r.stderr)
    assert "미리 보기입니다" in out and not cfg.data_dir.exists()  # 아무것도 쓰지 않았다
    assert snapshot(folder) == before  # 기존 파일을 고치지 않았다
    for expected in ("| channels.json | [0] 기존채널 | 가져옴 |", "치지직 채널이 아닙니다", "recordWatchParty",
                     "| channels.json | [0] 기존채널.gpu_index | 무시 |", "녹화 루트 밖", "| config.json | autoMoveEnabled | 무시 |",
                     "timemachine_enabled = True", "rewind_seconds = 3600", "| config.json | fileManagerRoots: /etc | 건너뜀 |",
                     "| notification.json | events.old_unknown_event | 건너뜀 |", "notify_dedupe_seconds = 120",
                     "| notification.json | discord_webhook | 무시 |", "| cookie.json | youtube | 무시 |",
                     "비밀 저장소로 옮김. 설정됨(값 숨김)", "작업 내역은 옮기지 않습니다", "계정은 옮기지 않습니다"):
        assert expected in out, expected


def test_apply_matches_production_shape(old, cfg):
    """실제 설정과 같은 결과가 되는지 본다."""
    folder, fm_root = old
    before = snapshot(folder)
    r = cli(cfg, "--from", str(folder), "--map", f"/recordings={cfg.record_root}", "--apply")
    assert r.returncode == 0, r.stderr
    assert_no_secret(r.stdout + r.stderr)
    assert "적용했습니다." in r.stdout and snapshot(folder) == before
    store = st.Store(cfg.db_path, cfg.record_root)
    chans = store.list_channels()
    assert [(c.channel_id, c.name, c.quality, c.extension, c.auto_record) for c in chans] == [(CH, "기존채널", "best", ".mp4", True)]
    assert chans[0].output_dir == str(cfg.record_root / "기존채널")
    s = store.get_settings()
    assert (s.check_interval, s.filename_pattern, s.remux_enabled, s.delete_original) == (60, NEW_PATTERN, True, True)
    assert f"| config.json | filenamePattern | 건너뜀 | 값을 쓸 수 없습니다: 모르는 토큰: {{legacy_time}}. 기본 패턴({NEW_PATTERN})을 씁니다 |" in r.stdout
    assert (s.timemachine_enabled, s.rewind_seconds) == (True, 3600)
    assert (s.fm_enabled, s.fm_roots, s.fm_readonly, s.fm_trash) == (True, (str(fm_root),), False, False)
    # 다시보기 사용 여부는 확장 켜고 끄기, 저장 폴더·여유 공간은 downloads 확장의 설정이다
    assert extensions.enabled_names(store) == ["downloads", "replay", "clips"]
    ext = lambda key: extensions.get_setting(store, "downloads", key)  # noqa: E731
    assert (ext("replay_dir"), ext("replay_min_free_bytes")) == ("replay", 5 * 1024**3)
    assert (s.notify_enabled, s.notify_dedupe_seconds, s.notify_record_start, s.notify_finalize_done) == (True, 120, True, False)
    store.close()
    assert credentials.load(cfg, "chzzk") == {"NID_AUT": AUT, "NID_SES": SES}
    assert secretstore.telegram_load(cfg.telegram_path) == {"bot_token": TOKEN, "chat_id": CHAT}
    for path in (credentials.path(cfg, "chzzk"), cfg.telegram_path):
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not secretstore.account_exists(cfg.account_path)
    assert not (cfg.data_dir / "replay").exists()  # 작업 내역을 만들지 않았다
    for path in cfg.data_dir.rglob("*"):  # 비밀 저장소 밖에 비밀이 없다
        if path.is_file() and path.parent.name != "secrets":
            raw = path.read_bytes()
            assert all(x.encode() not in raw for x in (AUT, SES, TOKEN, CHAT)), path
    # 다시 실행하면 덮어쓰지 않고 멈춘다
    again = cli(cfg, "--from", str(folder), "--apply")
    assert again.returncode == 2 and "덮어쓰지 않고 멈춥니다" in again.stderr
    assert_no_secret(again.stdout + again.stderr)


def test_refuses_existing_data(old, cfg):
    folder, _ = old
    secretstore.create_account(cfg.account_path, "admin", "a long password")
    r = cli(cfg, "--from", str(folder))
    assert r.returncode == 2 and "관리자 계정" in r.stderr
    secretstore.remove(cfg.account_path)
    store = st.Store(cfg.db_path, cfg.record_root)
    store.add_channel(CH, "기존", "a")
    store.close()
    r = cli(cfg, "--from", str(folder), "--apply")
    assert r.returncode == 2 and "채널" in r.stderr
    assert st.Store(cfg.db_path, cfg.record_root).list_channels()[0].name == "기존"


def test_bad_and_missing_inputs(tmp_path, cfg):
    folder = tmp_path / "partial"
    folder.mkdir()
    (folder / "config.json").write_text("{not json")
    write(folder, "channels.json", {"not": "a list"})
    write(folder, "notification.json", {"telegram_enabled": True, "telegram_bot_token": "", "events": ["record_start", "nope"]})
    r = cli(cfg, "--from", str(folder), "--apply")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "| config.json | (파일) | 건너뜀 | 읽을 수 없습니다" in out and "| cookie.json | (파일) | 없음 |" in out
    assert "| channels.json | (파일) | 건너뜀 | 배열이 아닙니다 |" in out
    assert "알림을 켤 수 없습니다" in out and "| notification.json | events.nope | 건너뜀 |" in out
    s = st.Store(cfg.db_path, cfg.record_root).get_settings()
    assert s.notify_enabled is False and s.notify_record_start is True and s.timemachine_enabled == Settings().timemachine_enabled
    assert cli(cfg, "--from", str(tmp_path / "nope")).returncode == 2
    assert cli(cfg, "--from", str(folder), "--map", "relative=/x").returncode == 2


def test_invalid_values_are_skipped_with_reason(tmp_path, cfg):
    folder = tmp_path / "bad"
    folder.mkdir()
    write(folder, "config.json", {"recheckInterval": 3, "filenamePattern": "{unknown}", "timemachine_time_shift": 7200,
                                  "autoPostProcessing": False, "replayDownloadRoot": "/elsewhere/replay"})
    write(folder, "channels.json", [{"id": CH, "name": "ts", "platform": "chzzk", "output_dir": "ts", "extension": "ts",
                                     "quality": "1080", "record_enabled": "yes"},
                                    {"id": "short", "platform": "chzzk", "output_dir": "a"}])
    r = cli(cfg, "--from", str(folder))
    out = r.stdout
    for item in ("recheckInterval", "filenamePattern", "timemachine_time_shift", "replayDownloadRoot"):
        assert f"| config.json | {item} | 건너뜀 |" in out, item
    assert "변환이 꺼져 있으면 .ts를 쓸 수 없습니다" in out and "32자리 UID가 아닙니다" in out


def test_production_paths_and_old_event_names(tmp_path):
    """실제 값은 옛 컨테이너의 절대 경로다: output_dir=/recordings, fileManagerRoots=[/recordings],
    replayDownloadRoot=/recordings/replay. 새 녹화 루트도 같은 경로라 --map 없이 그대로 옮겨진다.
    시험에서는 tmp 폴더를 /recordings 자리로 쓴다. 옛 이벤트 이름 9종은 새 이벤트로 옮긴다."""
    rec = tmp_path / "recordings"
    rec.mkdir()
    cfg = AppConfig(data_dir=tmp_path / "data", record_root=rec)
    folder = tmp_path / "old"
    folder.mkdir()
    R = str(rec)
    write(folder, "channels.json", [{"id": CH, "name": "기존채널", "platform": "chzzk", "output_dir": R, "quality": "best",
                                     "extension": ".mp4", "record_enabled": True}])
    write(folder, "config.json", {"fileManagerEnabled": True, "fileManagerRoots": [R], "replayEnabled": True,
                                  "replayDownloadRoot": R + "/replay", "replayMinFreeGiB": 5.0, "moveAfterProcessing": R})
    old_events = {"record_started": True, "record_finished": True, "record_start_failed": True,
                  "record_abnormally_stopped": True, "record_user_stopped": False, "postprocess_finished": True,
                  "postprocess_failed": True, "cookie_auth_failed": True, "disk_space_low": True, "watchparty_skipped": True}
    write(folder, "notification.json", {"telegram_enabled": True, "telegram_bot_token": TOKEN, "telegram_chat_id": CHAT,
                                        "events": old_events})
    r = cli(cfg, "--from", str(folder), "--apply")
    assert r.returncode == 0, r.stderr
    assert_no_secret(r.stdout)
    assert "| config.json | moveAfterProcessing | 무시 |" in r.stdout
    assert "| notification.json | events.watchparty_skipped | 건너뜀 | 새 도구에 없는 이벤트 |" in r.stdout
    store = st.Store(cfg.db_path, cfg.record_root)
    ch = store.list_channels()[0]
    assert ch.output_dir == str(rec)  # 녹화 루트 자체
    s = store.get_settings()
    assert s.fm_roots == (str(rec),)
    assert extensions.get_setting(store, "downloads", "replay_dir") == "replay"
    assert extensions.get_setting(store, "downloads", "replay_min_free_bytes") == 5 * 1024**3
    expect = {"record_start": True, "record_end": True, "start_failed": True, "abnormal_end": True, "user_stop": False,
              "finalize_done": True, "finalize_failed": True, "cookie_expired": True, "disk_low": True}
    assert {k: getattr(s, f"notify_{k}") for k in expect} == expect
    assert s.notify_timemachine_fallback is True and s.notify_enabled is True
