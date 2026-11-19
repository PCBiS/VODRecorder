"""접근 키. 프로그램이 화면 없이 API를 쓰도록 관리자가 발급하는 Bearer 키.

- 값은 `vr_` + 무작위 256비트(URL에 쓰는 base64). 발급한 그때 한 번만 돌려주고 서버에는 SHA-256 해시만 둔다.
  (값이 256비트 무작위라 해시를 거꾸로 찾을 수 없으므로 느린 해시를 쓰지 않는다.)
- 저장은 비밀 저장소의 `api-keys.json`(0600). 설정·채널 저장소(SQLite)와 로그에는 넣지 않는다.
- 값 비교는 시간이 일정한 방법(hmac.compare_digest)으로 저장된 모든 해시와 한다.
- 마지막으로 쓴 시각과 횟수는 메모리에서 세고 30초에 한 번만 파일에 쓴다(읽기 요청마다 파일을 쓰지 않는다).
- 환경변수 `LIVE_RECORDER_API_KEYS=off`(또는 0·false·no)면 이 기능을 끈다.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import secretstore

ENV_SWITCH = "LIVE_RECORDER_API_KEYS"
PREFIX = "vr_"
MAX_KEYS = 10
SCOPES = ("view", "operate")
SCOPE_LABELS = {"view": "보기", "operate": "운영"}
EXPIRY_DAYS = {"none": None, "30d": 30, "90d": 90, "1y": 365}
NAME_MAX = 40
FLUSH_EVERY = 30.0


class ApiKeyError(ValueError):
    """발급 입력이 틀렸거나 더 만들 수 없다."""


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "").strip().lower() not in ("off", "0", "false", "no")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime | None) -> str | None:
    return when.isoformat(timespec="seconds") if when else None


class KeyStore:
    def __init__(self, path: Path, clock=_now):
        self.path = path
        self.clock = clock
        self.lock = threading.RLock()  # touch·flush가 _save(잠금 안에서 다시 읽기)를 부른다
        self._keys: list[dict[str, Any]] | None = None
        self._mtime: int | None = None
        self._dirty_at: float | None = None

    # ---- 파일 ----
    def _load(self, force: bool = False) -> list[dict[str, Any]]:
        """파일의 키 목록. 파일이 바뀌었으면 다시 읽는다. 다시 읽을 때 파일이 정본이다(다른 곳에서 폐기한 키는
        되살리지 않는다). 메모리에만 있던 사용 기록(횟수·마지막 사용)만 남은 키에 옮긴다."""
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if self._keys is None or force or mtime != self._mtime:
            data = secretstore.read_json(self.path)
            keys = data.get("keys") if isinstance(data.get("keys"), list) else []
            fresh = [k for k in keys if isinstance(k, dict) and isinstance(k.get("hash"), str)]
            mine = {k.get("id"): k for k in self._keys or []}
            for k in fresh:
                old = mine.get(k.get("id"))
                if old is not None and old.get("hash") == k["hash"]:
                    k["uses"] = max(int(k.get("uses") or 0), int(old.get("uses") or 0))
                    stamps = [s for s in (k.get("last_used_at"), old.get("last_used_at")) if s]
                    k["last_used_at"] = max(stamps) if stamps else None
            self._keys = fresh
            self._mtime = mtime
        return self._keys

    def _write(self) -> None:
        """잠금(self.lock과 파일 잠금) 안에서만 부른다."""
        secretstore.write_json(self.path, {"version": 1, "keys": self._keys or []})
        self._mtime = os.stat(self.path).st_mtime_ns
        self._dirty_at = None

    @contextmanager
    def _mutate(self):
        """읽고-고쳐-쓰기를 잠금 하나로. 들어갈 때 파일을 다시 읽는다(다른 프로세스가 바꾼 것을 덮지 않게)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, secretstore.file_lock(self.path):
            yield self._load(force=True)

    def _save(self) -> None:
        """사용 기록 쓰기. 파일을 다시 읽어 합친 뒤 쓴다."""
        with self._mutate():
            self._write()

    # ---- 관리 ----
    def list(self) -> list[dict[str, Any]]:
        """화면에 보일 값(해시는 내보내지 않는다)."""
        with self.lock:
            now = self.clock()
            out = []
            for k in self._load():
                expires = k.get("expires_at")
                expired = bool(expires and datetime.fromisoformat(expires) <= now)
                out.append({"id": k["id"], "name": k["name"], "scope": k["scope"], "scope_label": SCOPE_LABELS[k["scope"]],
                            "prefix": k["prefix"], "created_at": k["created_at"], "expires_at": expires, "expired": expired,
                            "last_used_at": k.get("last_used_at"), "uses": int(k.get("uses") or 0)})
            return out

    def create(self, name: Any, scope: Any, expiry: Any) -> tuple[str, dict[str, Any]]:
        """발급. (키 값, 목록 줄). 값은 이때만 돌려준다."""
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_MAX:
            raise ApiKeyError(f"이름은 1~{NAME_MAX}자입니다")
        if scope not in SCOPES:
            raise ApiKeyError("권한은 보기(view)나 운영(operate)입니다")
        if expiry not in EXPIRY_DAYS:
            raise ApiKeyError("만료는 없음(none)·30일(30d)·90일(90d)·1년(1y) 가운데 하나입니다")
        with self._mutate() as keys:
            if len(keys) >= MAX_KEYS:
                raise ApiKeyError(f"접근 키는 {MAX_KEYS}개까지 만들 수 있습니다. 쓰지 않는 키를 폐기해 주십시오")
            value = PREFIX + secrets.token_urlsafe(32)
            now = self.clock()
            days = EXPIRY_DAYS[expiry]
            item = {"id": secrets.token_hex(8), "name": name.strip(), "scope": scope, "prefix": value[:8],
                    "hash": _digest(value), "created_at": _iso(now), "expires_at": _iso(now + timedelta(days=days)) if days else None,
                    "last_used_at": None, "uses": 0}
            keys.append(item)
            self._write()
        return value, next(k for k in self.list() if k["id"] == item["id"])

    def revoke(self, ident: str) -> bool:
        with self._mutate() as keys:
            kept = [k for k in keys if k["id"] != ident]
            if len(kept) == len(keys):
                return False
            self._keys = kept
            self._write()
            return True

    def revoke_all(self) -> int:
        """모든 키를 폐기한다. 폐기한 수."""
        with self._mutate() as keys:
            if not keys and not self.path.exists():
                return 0
            count = len(keys)
            self._keys = []
            self._write()
            return count

    def count(self) -> int:
        with self.lock:
            return len(self._load())

    # ---- 인증 ----
    def verify(self, value: str) -> dict[str, Any] | None:
        """맞고 만료되지 않은 키면 그 줄(사본). 아니면 None(까닭을 구분하지 않는다)."""
        if not isinstance(value, str) or not value.startswith(PREFIX) or len(value) > 200:
            return None
        digest = _digest(value)
        with self.lock:
            found = None
            for k in self._load():  # 모두와 견준다(맞는 줄을 찾아도 멈추지 않는다)
                if hmac.compare_digest(k["hash"], digest):
                    found = k
            if found is None:
                return None
            expires = found.get("expires_at")
            if expires and datetime.fromisoformat(expires) <= self.clock():
                return None
            return {k: v for k, v in found.items() if k != "hash"}

    def touch(self, ident: str) -> None:
        """마지막으로 쓴 시각과 횟수. 파일에는 30초에 한 번 쓴다."""
        with self.lock:
            for k in self._load():
                if k["id"] == ident:
                    k["last_used_at"] = _iso(self.clock())
                    k["uses"] = int(k.get("uses") or 0) + 1
            mono = time.monotonic()
            if self._dirty_at is None:
                self._dirty_at = mono
            if mono - self._dirty_at >= FLUSH_EVERY or self._mtime is None:
                self._save()

    def flush(self) -> None:
        with self.lock:
            if self._dirty_at is not None and self._keys is not None:
                self._save()
