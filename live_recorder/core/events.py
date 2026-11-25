"""사건 알림. 중심부가 일어난 일을 알리고, 확장이 듣는다.

- 알리는 쪽(`emit`)은 기다리지 않는다. 사건은 대기열에 넣고 바로 돌아온다. 녹화 감시(이벤트 루프)는 듣는 쪽 때문에 멈추지 않는다
- 듣는 쪽은 한 스레드가 차례로 부른다. 사건이 일어난 순서(`seq`)대로 전해진다
- 듣는 쪽의 예외는 로그(`event_listener_failed`)에 남기고 다음 듣는 쪽·다음 사건으로 넘어간다
- 듣는 쪽이 오래 붙잡으면 대기열이 쌓인다. 한도(`MAX_PENDING`)를 넘은 사건은 버리고 로그(`event_dropped`)에 남긴다

사건 이름은 `EVENTS`에 있다. 사건마다 (플랫폼, 채널 ID)(채널과 상관없으면 None)와 그 사건의 값이 실린다.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("live_recorder.events")

# 사건 이름과 실리는 값(values의 키)
EVENTS: dict[str, tuple[str, ...]] = {
    "server_started": ("channels",),
    "server_stopping": (),
    "channel_added": ("name", "output_dir", "quality", "extension", "auto_record", "video_codec"),
    "channel_updated": ("changes",),
    "channel_removed": ("result",),
    "record_started": ("session_id", "live_id", "source", "path"),
    "record_ended": ("session_id", "kind", "end_reason", "bytes"),
    "finalize_started": ("session_id",),
    "finalize_ended": ("session_id", "status", "path"),
    "settings_changed": ("keys",),
}
MAX_PENDING = 10_000


@dataclass(frozen=True)
class Event:
    name: str
    seq: int
    at: float
    platform: str | None = None
    channel_id: str | None = None
    values: dict[str, Any] = field(default_factory=dict)


Listener = Callable[[Event], None]
_STOP = object()


class EventBus:
    def __init__(self) -> None:
        self._listeners: list[tuple[str, Listener, str]] = []  # (사건 이름 또는 "*", 함수, 누가 등록했는지)
        self._queue: queue.Queue = queue.Queue()
        self._seq = itertools.count(1)
        self._seq_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending = 0  # 넣었지만 아직 다 전하지 않은 사건 수(_seq_lock으로 지킨다)
        self._idle = threading.Event()
        self._idle.set()

    def subscribe(self, name: str, fn: Listener, owner: str = "") -> None:
        if name != "*" and name not in EVENTS:
            raise ValueError(f"모르는 사건: {name}")
        self._listeners.append((name, fn, owner))

    def unsubscribe_owner(self, owner: str) -> None:
        self._listeners = [x for x in self._listeners if x[2] != owner]

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="live-recorder-events", daemon=True)
            self._thread.start()

    def emit(self, name: str, platform: str | None = None, channel_id: str | None = None, /, **values: Any) -> Event:
        if name not in EVENTS:
            raise ValueError(f"모르는 사건: {name}")
        with self._seq_lock:  # 번호를 매기는 순서와 대기열에 넣는 순서가 같게 한다
            event = Event(name, next(self._seq), time.time(), platform, channel_id, dict(values))
            if self._pending >= MAX_PENDING:
                log.warning("사건 대기열이 가득 차 버렸다", extra={"event": "event_dropped", "event_name": name})
                return event
            self._pending += 1
            self._idle.clear()
            self._queue.put(event)
        return event

    def drain(self, timeout: float = 5.0) -> bool:
        """쌓인 사건을 다 전할 때까지 기다린다(시험·종료용). 다 전했으면 참."""
        if self._thread is None:
            return self._pending == 0
        return self._idle.wait(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        """남은 사건을 전하고 멈춘다. 듣는 쪽이 끝나지 않으면 기다리지 않고 돌아온다(스레드는 데몬이다)."""
        if self._thread is None:
            return
        self._queue.put(_STOP)
        self._thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._idle.set()
                return
            for name, fn, owner in list(self._listeners):
                if name not in ("*", item.name):
                    continue
                try:
                    fn(item)
                except Exception as e:  # noqa: BLE001 - 듣는 쪽의 오류로 다른 듣는 쪽·녹화를 멈추지 않는다
                    log.warning("사건 처리 중 오류: %s", type(e).__name__,
                                extra={"event": "event_listener_failed", "event_name": item.name, "extension": owner or None})
            with self._seq_lock:
                self._pending -= 1
                if self._pending == 0:
                    self._idle.set()
