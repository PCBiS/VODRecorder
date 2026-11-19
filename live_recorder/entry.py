"""명령의 입구. 설치한 명령 `vod-recorder`와 `python -m live_recorder`가 여기로 들어온다.

윈도우에서는 무거운 모듈(fcntl 등 유닉스 전용)을 불러오기 전에 지원하지 않는다는 안내를 보이고 끝난다.
"""

from __future__ import annotations

import sys

WINDOWS_NOTICE = ("윈도우는 지원하지 않습니다. WSL(Windows Subsystem for Linux)에 리눅스를 설치해 그 안에서 실행하거나, "
                  "Docker Desktop으로 컨테이너 이미지를 써 주십시오.")
WINDOWS_EXIT = 3


def main(argv: list[str] | None = None, platform: str | None = None) -> int:
    from .core.locations import system_name

    if system_name(platform) == "windows":
        print(WINDOWS_NOTICE, file=sys.stderr)
        return WINDOWS_EXIT
    from .cli import main as cli_main

    return cli_main(argv)
