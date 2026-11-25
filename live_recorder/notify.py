"""텔레그램 알림.

전송은 따로 도는 작업 하나가 대기열에서 꺼내 보낸다. 녹화 감시와 마무리는
`notify()`로 대기열에 넣기만 하고 기다리지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .core import httpread

MAX_RESPONSE = 64 * 1024  # 텔레그램 응답 상한

from . import PROGRAM_NAME
from .core import secretstore
from .core.config import AppConfig, Settings

log = logging.getLogger("live_recorder.notify")

# 이벤트 → (표시 이름, 필수 여부). 설정 키는 `notify_<이벤트>`다.
EVENTS: dict[str, tuple[str, bool]] = {
    "start_failed": ("녹화 시작 실패", True),
    "abnormal_end": ("비정상 종료", True),
    "timemachine_fallback": ("타임머신 실패, 일반 수신 전환", True),
    "codec_changed": ("녹화 중 영상 코덱 바뀜", True),
    "finalize_failed": ("마무리 실패", True),
    "disk_low": ("디스크 부족으로 녹화 거부", True),
    "cookie_expired": ("쿠키 만료", True),
    "record_start": ("녹화 시작", False),
    "record_end": ("녹화 종료", False),
    "user_stop": ("사용자 중지", False),
    "finalize_done": ("마무리 완료", False),
}
QUEUE_MAX = 200
SEND_TIMEOUT = 10.0


@dataclass
class Message:
    event: str
    text: str


def build_text(event: str, channel: str = "", reason: str = "", file: str = "") -> str:
    """이벤트 이름, 채널 이름, 사유, 파일 이름만 넣는다."""
    lines = [f"[{PROGRAM_NAME}] {EVENTS.get(event, (event, False))[0]}"]
    if channel:
        lines.append(f"채널: {channel}")
    if reason:
        lines.append(f"사유: {reason}")
    if file:
        lines.append(f"파일: {file}")
    return "\n".join(lines)[:3500]


class Notifier:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._queue: asyncio.Queue[Message] = asyncio.Queue(QUEUE_MAX)
        self._last: dict[tuple[str, str], float] = {}
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self.sent = 0
        self.failed = 0

    def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(SEND_TIMEOUT, connect=5.0), follow_redirects=False)
        self._task = asyncio.create_task(self._run(), name="notifier")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._client:
            await self._client.aclose()

    def notify(self, settings: Settings, event: str, channel_id: str = "", channel: str = "", reason: str = "",
               file: str = "") -> bool:
        """대기열에 넣고 바로 돌아온다. 절대 기다리거나 예외를 내지 않는다."""
        try:
            if not settings.notify_enabled or not getattr(settings, f"notify_{event}", False):
                return False
            now = time.monotonic()
            window = settings.notify_dedupe_seconds
            key = (event, channel_id)
            if window > 0 and key in self._last and now - self._last[key] < window:
                return False
            self._last[key] = now
            self._queue.put_nowait(Message(event, build_text(event, channel, reason, file)))
            return True
        except asyncio.QueueFull:
            log.warning("알림 대기열이 가득 차 버렸다", extra={"event": "notify_dropped", "notify_event": event})
            return False
        except Exception as e:  # noqa: BLE001 - 알림 때문에 녹화가 멈추면 안 된다
            log.warning("알림 준비 실패: %s", type(e).__name__, extra={"event": "notify_error"})
            return False

    async def _run(self) -> None:
        while True:
            msg = await self._queue.get()
            ok, detail = await self.send(msg.text)
            if ok:
                self.sent += 1
            else:
                self.failed += 1
                # 주소(토큰 포함)는 찍지 않는다
                log.warning("알림 전송 실패: %s", detail, extra={"event": "notify_failed", "notify_event": msg.event})

    async def send(self, text: str) -> tuple[bool, str]:
        """한 번 보낸다. (성공 여부, 토큰 없는 설명)."""
        secret = await asyncio.to_thread(secretstore.telegram_load, self.cfg.telegram_path)
        token, chat_id = secret.get("bot_token"), secret.get("chat_id")
        if not token or not chat_id:
            return False, "봇 토큰이나 chat_id가 없습니다"
        base = self.cfg.telegram_api_base
        parts = urlsplit(base)
        official = parts.scheme == "https" and parts.hostname == "api.telegram.org"
        local_test = self.cfg.allow_http and parts.hostname in ("127.0.0.1", "localhost")
        if not (official or local_test):
            return False, "허용하지 않는 알림 호스트"
        assert self._client is not None
        try:
            resp = await httpread.aget(self._client, f"{base}/bot{token}/sendMessage", MAX_RESPONSE, method="POST",
                                       json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
        except httpx.HTTPError as e:
            return False, f"요청 오류 {type(e).__name__}"  # 예외 문장에는 주소가 들어갈 수 있어 형식 이름만 쓴다
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"
        return True, "전송함"

    async def send_test(self) -> tuple[bool, str]:
        """이벤트 스위치와 중복 억제를 무시하고 한 번 보낸다."""
        return await self.send(f"[{PROGRAM_NAME}] 테스트 알림\n알림 설정이 동작합니다")

    def configured(self) -> dict[str, bool]:
        secret = secretstore.telegram_load(self.cfg.telegram_path)
        return {"bot_token": bool(secret.get("bot_token")), "chat_id": bool(secret.get("chat_id"))}
