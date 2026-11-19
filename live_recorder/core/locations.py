"""저장 위치의 기본값. 환경변수나 명령줄로 정하지 않았을 때 운영체제 관례를 따른다.

- 리눅스: 자료 `$XDG_DATA_HOME/vod-recorder`(없으면 `~/.local/share/vod-recorder`),
  로그 `$XDG_STATE_HOME/vod-recorder/logs`(없으면 `~/.local/state/vod-recorder/logs`), 녹화 `~/Videos/vod-recorder`
- macOS: `~/Library/Application Support/VOD Recorder`, `~/Library/Logs/VOD Recorder`, `~/Movies/VOD Recorder`
- 컨테이너 이미지는 Dockerfile이 `/data`, `/logs`, `/recordings`를 환경변수로 준다(여기 기본값을 쓰지 않는다)

정한 순서: 명령줄 → 환경변수(LIVE_RECORDER_DATA_DIR·_RECORD_ROOT·_LOG_DIR) → 이 기본값.
자료 폴더만 정하고 로그 폴더를 정하지 않으면 예전처럼 자료 폴더 아래 `logs`다(이전 설치와 같은 뜻).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .. import PROGRAM_NAME

MAC_NAME = PROGRAM_NAME  # macOS 폴더 이름(표시 이름 그대로)
APP_DIR = PROGRAM_NAME.lower().replace(" ", "-")  # 리눅스 폴더 이름(배포 이름과 같다)


@dataclass(frozen=True)
class Locations:
    data: Path
    logs: Path
    recordings: Path


def system_name(platform: str | None = None) -> str:
    """'linux' | 'macos' | 'windows' | 그 밖의 이름."""
    p = platform if platform is not None else sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "macos"
    if p.startswith(("win", "cygwin", "msys")):
        return "windows"
    return p


def defaults(platform: str | None = None, env: Mapping[str, str] | None = None, home: Path | None = None) -> Locations:
    env = os.environ if env is None else env
    home = Path(home) if home is not None else Path.home()
    if system_name(platform) == "macos":
        return Locations(home / "Library" / "Application Support" / MAC_NAME, home / "Library" / "Logs" / MAC_NAME,
                         home / "Movies" / MAC_NAME)
    data_home = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME", "").startswith("/") else home / ".local" / "share"
    state_home = Path(env["XDG_STATE_HOME"]) if env.get("XDG_STATE_HOME", "").startswith("/") else home / ".local" / "state"
    return Locations(data_home / APP_DIR, state_home / APP_DIR / "logs", home / "Videos" / APP_DIR)
