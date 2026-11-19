"""자식 프로세스의 공통 도움.

`die_with_parent()`: 부모(서버)가 죽으면 같이 끝난다. 리눅스는 PR_SET_PDEATHSIG로 커널이 신호를 준다.
macOS처럼 그 기능이 없으면 부모 번호가 바뀌는지(고아가 되면 바뀐다) 1초마다 보는 스레드로 대신한다.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import threading
import time

PR_SET_PDEATHSIG = 1


def _watch(parent: int, interval: float) -> None:
    while True:
        time.sleep(interval)
        if os.getppid() != parent:
            os.kill(os.getpid(), signal.SIGTERM)
            return


def die_with_parent(interval: float = 1.0) -> str:
    """'prctl' | 'watch'. 끝나는 신호는 SIGTERM이다(받는 쪽의 정상 종료 처리를 탄다)."""
    if sys.platform.startswith("linux"):
        try:
            if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0) == 0:
                return "prctl"
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_watch, args=(os.getppid(), interval), name="parent-watch", daemon=True).start()
    return "watch"
