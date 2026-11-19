"""낮은 우선순위로 다른 명령을 실행한다.

    python -m live_recorder.lowprio <명령> [인자...]

자기 자신의 CPU 우선순위(nice 19)와 입출력 우선순위(idle 등급)를 낮춘 뒤 그 명령으로 바뀐다(exec).
낮춘 상태는 exec 뒤의 프로세스와 그 프로세스가 만드는 스레드·자식에게 이어진다.
권한을 모두 내린 비루트 컨테이너에서도 우선순위를 낮추는 일은 허용된다(높이는 일만 막힌다).
낮출 수 없으면 표준 오류에 경고 한 줄(`WARNING_PREFIX`)을 남기고 그대로 실행한다.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys

WARNING_PREFIX = "live-recorder-lowprio:"
NICE = 19
IOPRIO_CLASS_IDLE = 3
IOPRIO_CLASS_SHIFT = 13
IOPRIO_WHO_PROCESS = 1
_SYSCALLS = {"x86_64": (251, 252), "aarch64": (30, 31), "arm64": (30, 31)}  # (ioprio_set, ioprio_get)


def _syscall(number: int, *args: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    r = libc.syscall(number, *args)
    if r < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return r


def _linux_numbers() -> tuple[int, int] | None:
    """ioprio 시스템 호출 번호. 리눅스에서만 있다(macOS에서 같은 번호는 다른 호출이므로 부르지 않는다)."""
    if not sys.platform.startswith("linux"):
        return None
    return _SYSCALLS.get(platform.machine())


def set_io_idle(pid: int = 0) -> None:
    if not sys.platform.startswith("linux"):
        raise OSError(0, "입출력 우선순위는 리눅스에서만 낮춥니다")
    numbers = _linux_numbers()
    if not numbers:
        raise OSError(0, f"ioprio_set 번호를 모르는 아키텍처: {platform.machine()}")
    _syscall(numbers[0], IOPRIO_WHO_PROCESS, pid, IOPRIO_CLASS_IDLE << IOPRIO_CLASS_SHIFT)


def get_io_class(pid: int = 0) -> int:
    numbers = _linux_numbers()
    if not numbers:
        raise OSError(0, "unknown architecture")
    return _syscall(numbers[1], IOPRIO_WHO_PROCESS, pid) >> IOPRIO_CLASS_SHIFT


def lower_self() -> list[str]:
    """낮추지 못한 항목의 설명 목록. 비었으면 둘 다 낮췄다."""
    problems = []
    try:
        os.setpriority(os.PRIO_PROCESS, 0, NICE)
    except OSError as e:
        problems.append(f"nice: {e.strerror or type(e).__name__}")
    try:
        set_io_idle(0)
    except OSError as e:
        problems.append(f"ioprio: {e.strerror or type(e).__name__}")
    return problems


def wrap(command: list[str]) -> list[str]:
    """명령 앞에 이 실행기를 붙인다."""
    return [sys.executable, "-m", "live_recorder.lowprio", *command]


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("사용법: python -m live_recorder.lowprio <명령> [인자...]\n")
        sys.exit(2)
    problems = lower_self()
    if problems:
        sys.stderr.write(f"{WARNING_PREFIX} 우선순위를 낮추지 못했습니다({'; '.join(problems)}). 그대로 실행합니다\n")
        sys.stderr.flush()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
