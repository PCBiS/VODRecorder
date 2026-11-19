"""시험용 가짜 확장. 시험 폴더에만 있다.

등록하는 것: 경로 셋(로그인·공개·쓰기), 위쪽 탭 하나와 설정 > 일반의 묶음 하나, 화면 스크립트·스타일
(`tests/fake_extension_static/`), 설정 키 하나(`level`, 1~9, 기본 3), 자기 저장소(`<데이터>/ext/fakeext/fake.sqlite3`),
사건 듣기(모든 사건을 적는다. `record_started`에서는 일부러 예외를 낸다. `record_ended`에서는 1초 붙잡는다),
시작·종료 때 할 일(백그라운드 작업 하나), 로그 내려받기 줄·필드·진단 요약, 파일 관리자 잠금 대상, 채널 화면 정보.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import APIRouter

from live_recorder.core.config import ConfigError
from live_recorder.core.extensions import LOGIN, PUBLIC, AccessRule, Extension, SettingsGroup, SettingSpec, Tab

STATIC = Path(__file__).resolve().parent / "fake_extension_static"


def level(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9:
        raise ConfigError("level은 1~9의 정수입니다")
    return value


class FakeExtension(Extension):
    name = "fakeext"
    title = "가짜 확장"
    version = "1.0"
    access_rules = (AccessRule("GET", "/api/fakeext/ping", LOGIN), AccessRule("POST", "/api/fakeext/note", LOGIN),
                    AccessRule("GET", "/api/fakeext/public", PUBLIC))
    tabs = (Tab("fakeext", "가짜"),)
    settings_groups = (SettingsGroup("general", "fakeext", "가짜 확장 설정"),)
    scripts = ("fake.js",)
    styles = ("fake.css",)
    static_dir = STATIC
    settings = {"level": SettingSpec(3, level, "단계")}
    log_fields = {"fake_note": "keep"}

    def __init__(self):
        self.reset()

    def reset(self):
        self.events: list = []  # 받은 사건(사건 스레드에서 쌓인다)
        self.calls: list[str] = []  # 불린 걸이
        self.db: sqlite3.Connection | None = None
        self.task: asyncio.Task | None = None
        self.ticks = 0
        self.lock_path: Path | None = None
        self.slow = threading.Event()  # record_ended를 붙잡는 동안 켜진다

    # ---- 등록 ----
    def routes(self, ctx):
        self.calls.append("routes")
        r = APIRouter()

        @r.get("/api/fakeext/ping")
        async def ping():
            return {"pong": True, "level": ctx.setting("level"), "events": len(self.events)}

        @r.post("/api/fakeext/note")
        async def note(body: dict):
            text = str(body.get("text", ""))[:100]
            self.db.execute("INSERT INTO notes(text) VALUES(?)", (text,))
            self.db.commit()
            ctx.log.info("가짜 확장 메모", extra={"event": "fakeext_note"})
            return {"ok": True}

        @r.get("/api/fakeext/public")
        async def public():
            return {"public": True}

        return r

    def open_storage(self, ctx):
        self.calls.append("open_storage")
        self.db = sqlite3.connect(ctx.data_dir / "fake.sqlite3", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS notes(text TEXT NOT NULL)")
        self.db.commit()

    def listeners(self, ctx):
        return {"*": self.on_any, "record_started": self.on_record_started, "record_ended": self.on_record_ended}

    def on_any(self, event):
        self.events.append(event)

    def on_record_started(self, event):
        raise RuntimeError("가짜 확장이 일부러 낸 오류")

    def on_record_ended(self, event):
        self.slow.set()
        time.sleep(1.0)  # 듣는 쪽이 느려도 녹화 감시는 멈추지 않는다
        self.slow.clear()

    async def start(self, ctx):
        self.calls.append("start")

        async def loop():
            while True:
                self.ticks += 1
                await asyncio.sleep(0.05)

        self.task = asyncio.create_task(loop())

    async def stop(self, ctx):
        self.calls.append("stop")
        if self.task:
            self.task.cancel()
        if self.db:
            self.db.close()
            self.db = None

    def log_export(self, ctx):
        return [("events.jsonl", [{"ts": "2026-11-13T00:00:00+00:00", "level": "INFO", "logger": "live_recorder.ext.fakeext",
                                   "event": e.name, "fake_note": f"seq {e.seq}"} for e in self.events])]

    def diagnostics(self, ctx):
        return {"events": len(self.events)}

    def lock_targets(self, ctx):
        return [self.lock_path] if self.lock_path else []

    def channel_info(self, ctx, channel):
        return {"seen": sum(1 for e in self.events if e.channel_id == channel.get("channel_id"))}


class ChildExtension(Extension):
    """다른 확장(fakeext)이 있어야 켤 수 있는 확장."""
    name = "fakechild"
    title = "가짜 하위 확장"
    version = "0.1"
    requires = ("fakeext",)


EXTENSION = FakeExtension()
CHILD = ChildExtension()
