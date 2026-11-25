"""SQLite 저장소.

동기 API다. 코어는 `asyncio.to_thread`로 불러 이벤트 루프를 막지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import secretstore
from .config import ConfigError, Settings, stored_pattern_problem
from ..platforms import registry
from . import naming
from . import codec as codec_mod
from . import quality as quality_mod

log = logging.getLogger("live_recorder.store")

SCHEMA_VERSION = 5  # 5: 표는 4와 같다. 이전 빌드에서 올라온 것을 알아 확장 설정을 한 번 옮긴다
# v4에서 채널·채널 상태·세션·조작에 플랫폼을 더했다. 옮기기 전 사본의 이름: <DB 파일>.schema<옛 버전>.bak
DEFAULT_PLATFORM = registry.DEFAULT

# 나중에 더한 세션 열. 옛 저장소를 열 때 없는 열만 더한다(기존 행은 빈 값). 값은 열 정의다.
#   v2: time_machine_active
#   v3: quality_label(화질 표시), url_refreshes(주소 갱신 횟수), finalize_retries
#       live_id·title은 처음부터 있던 열이지만, 없는 저장소를 만나도 열리게 함께 검사한다
SESSION_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("live_id", "TEXT"),
    ("title", "TEXT"),
    ("time_machine_active", "INTEGER"),
    ("quality_label", "TEXT"),
    ("url_refreshes", "INTEGER"),
    ("finalize_retries", "INTEGER NOT NULL DEFAULT 0"),
)
# 버전을 올리지 않고 더하는 열(값이 없어도 되는 열만). 열 때마다 없는 것을 더한다.
# 이전 빌드는 이 열을 읽지도 쓰지도 않으므로, 이 열이 생긴 저장소를 이전 빌드로 그대로 열 수 있다.
COMPAT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("channels", "video_codec", "TEXT"),  # 코덱 우선순위. 비면 기본값(h264)
    ("sessions", "video_codec", "TEXT"),  # 이 파일에 받은 영상 코덱
)

# 채널 상태
STOPPED = "stopped"  # 정지
WATCHING = "watching"  # 감시
RECORDING = "recording"  # 녹화
FINALIZING = "finalizing"  # 마무리
BACKOFF = "backoff"  # 대기(백오프)
STATE_LABELS = {STOPPED: "정지", WATCHING: "감시", RECORDING: "녹화", FINALIZING: "마무리", BACKOFF: "대기(백오프)"}

# 종료 사유
END_BROADCAST = "broadcast_end"
END_USER = "user_stop"
END_STALL = "stalled"
END_ERROR = "error"
END_SHUTDOWN = "server_shutdown"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channels (
    platform TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    name TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    quality TEXT NOT NULL,
    extension TEXT NOT NULL CHECK (extension IN ('.ts', '.mp4')),
    auto_record INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    video_codec TEXT,
    PRIMARY KEY (platform, channel_id)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channel_state (
    platform TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    state TEXT NOT NULL,
    live_id TEXT,
    live_title TEXT,
    session_id INTEGER,
    error TEXT,
    error_count INTEGER NOT NULL DEFAULT 0,
    error_flag INTEGER NOT NULL DEFAULT 0,
    skip_live_id TEXT,
    finalizing INTEGER NOT NULL DEFAULT 0,
    last_check_at TEXT,
    last_check TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (platform, channel_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    live_id TEXT,
    title TEXT,
    source TEXT,
    note TEXT,
    time_machine_active INTEGER,
    pid INTEGER,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT,
    end_detail TEXT,
    exit_code INTEGER,
    part_path TEXT,
    final_path TEXT,
    bytes INTEGER,
    media_seconds REAL,
    first_media_time TEXT,
    last_media_time TEXT,
    clean_exit INTEGER NOT NULL DEFAULT 0,
    finalize_status TEXT,
    finalize_detail TEXT,
    output_path TEXT,
    quality_label TEXT,
    url_refreshes INTEGER,
    finalize_retries INTEGER NOT NULL DEFAULT 0,
    platform TEXT NOT NULL DEFAULT '{DEFAULT_PLATFORM}',
    video_codec TEXT
);
CREATE INDEX IF NOT EXISTS sessions_channel_v4 ON sessions (platform, channel_id, id);
CREATE TABLE IF NOT EXISTS control (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    done_at TEXT,
    result TEXT,
    platform TEXT NOT NULL DEFAULT '{DEFAULT_PLATFORM}'
);
"""

# v3 → v4. 기본 키가 바뀌는 표는 새로 만들어 옮긴다. 기존 줄은 기본 플랫폼이다
# v3 → v4. 채널·채널 상태는 기본 키가 (플랫폼, 채널 ID)로 바뀌어 새로 만들어 옮기고, 세션·제어 요청은 열을 더한다.
# 기존 줄은 기본 플랫폼(치지직) 몫이다. 옛 표에 없는 열은 새 표의 기본값이 된다
REBUILD_V4 = {
    "channels": "CREATE TABLE channels_v4 (platform TEXT NOT NULL, channel_id TEXT NOT NULL, name TEXT NOT NULL, output_dir TEXT NOT NULL,"
                " quality TEXT NOT NULL, extension TEXT NOT NULL CHECK (extension IN ('.ts', '.mp4')), auto_record INTEGER NOT NULL DEFAULT 1,"
                " created_at TEXT NOT NULL, PRIMARY KEY (platform, channel_id))",
    "channel_state": "CREATE TABLE channel_state_v4 (platform TEXT NOT NULL, channel_id TEXT NOT NULL, state TEXT NOT NULL, live_id TEXT,"
                     " live_title TEXT, session_id INTEGER, error TEXT, error_count INTEGER NOT NULL DEFAULT 0,"
                     " error_flag INTEGER NOT NULL DEFAULT 0, skip_live_id TEXT, finalizing INTEGER NOT NULL DEFAULT 0, last_check_at TEXT,"
                     " last_check TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (platform, channel_id))",
}
ADD_PLATFORM_V4 = ("sessions", "control")


EXTRA_PREFIXES = ("ext:", "platform:")  # 설정 표에 함께 두는 키(확장 설정, 플랫폼 상태)
PLATFORMS_ENABLED_KEY = "platform:enabled"  # 켠 "켜야 쓰는 플랫폼" 목록
GATE_KEY = "platform:gate"  # 플랫폼 ID → {"scan": 스캔 주기, "gap": 채널 사이 간격}


class StoreError(Exception):
    pass


class StoreCorrupt(StoreError):
    pass


class ValidationError(StoreError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _earlier(candidate: str | None, fallback: str) -> str:
    """두 ISO 시각 중 앞선 것. candidate가 비었거나 읽을 수 없으면 fallback."""
    try:
        when = datetime.fromisoformat(candidate) if candidate else None
    except ValueError:
        when = None
    if when is None:
        return fallback
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return min(when.astimezone(timezone.utc), datetime.fromisoformat(fallback)).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Channel:
    channel_id: str
    name: str
    output_dir: str
    quality: str
    extension: str
    auto_record: bool
    platform: str = DEFAULT_PLATFORM
    video_codec: str = codec_mod.DEFAULT_PREFERENCE  # 코덱 우선순위(h264·hevc·av1·any)


# 저장 폴더로 쓸 수 없는 폴더. 자료 폴더 아래 전체와 로그 폴더 아래 전체.
# 다만 녹화 루트가 자료 폴더 안에 있는 배치는 녹화 루트와 그 하위를 허용한다(비밀 폴더·로그 폴더는 그래도 막는다).
# 설정 저장소를 열 때와 앱이 시작할 때 채운다
_RESERVED_DATA: set[Path] = set()
_RESERVED_TREES: set[Path] = set()


def reserve_dirs(data_dir: Path | None = None, log_dir: Path | None = None) -> None:
    if data_dir:
        real = Path(os.path.realpath(data_dir))
        _RESERVED_DATA.add(real)
        _RESERVED_TREES.add(real / "secrets")
    if log_dir:
        _RESERVED_TREES.add(Path(os.path.realpath(log_dir)))


def _inside(path: Path, top: Path) -> bool:
    return path == top or top in path.parents


def resolve_output_dir(record_root: Path, value: str) -> Path:
    """녹화 루트 아래만 받는다. `..`과 루트 밖 경로, 자료·로그 폴더(와 그 아래)는 거부한다.
    녹화 루트 자체(`.`)는 받는다."""
    if not value or "\x00" in value:
        raise ValidationError("저장 폴더가 비었습니다")
    raw = Path(value)
    if ".." in raw.parts:
        raise ValidationError("저장 폴더에 '..'을 쓸 수 없습니다")
    root = record_root.resolve()
    cand = raw if raw.is_absolute() else root / raw
    resolved = Path(os.path.realpath(cand))  # 심볼릭 링크를 풀고 본다
    if resolved != root and root not in resolved.parents:
        raise ValidationError("저장 폴더가 녹화 루트 밖입니다")
    in_data = any(_inside(resolved, d) and not _inside(root, d) for d in _RESERVED_DATA)
    if resolved != root and (in_data or any(_inside(resolved, r) for r in _RESERVED_TREES)):
        raise ValidationError("저장 폴더로 자료 폴더나 로그 폴더를 쓸 수 없습니다")
    return resolved


def normalize_codec_preference(value: object) -> str:
    """채널의 코덱 우선순위. 비면 기본값."""
    try:
        return codec_mod.normalize_preference(value)
    except ValueError as e:
        raise ValidationError(str(e)) from None


def _stored_codec_preference(value: object) -> str:
    """저장된 값을 읽는다. 비었거나 모르는 값이면 기본값으로 읽는다(멈추지 않는다)."""
    try:
        return codec_mod.normalize_preference(value)
    except ValueError:
        return codec_mod.DEFAULT_PREFERENCE


def normalize_extension(value: str) -> str:
    ext = (value or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    if ext not in (".ts", ".mp4"):
        raise ValidationError("확장자는 .ts 또는 .mp4입니다")
    return ext


class Store:
    def __init__(self, path: Path, record_root: Path, telegram_path: Path | None = None):
        self.path = Path(path)
        self.record_root = Path(record_root)
        reserve_dirs(self.path.parent)  # 설정 저장소가 든 자료 폴더
        self.telegram_path = telegram_path
        self._lock = threading.RLock()
        existed = self.path.exists() and self.path.stat().st_size > 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
            self._db.row_factory = sqlite3.Row
            if existed:
                rows = self._db.execute("PRAGMA quick_check").fetchall()
                if [r[0] for r in rows] != ["ok"]:
                    raise StoreCorrupt(f"저장소 검사 실패: {self.path}")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            version = self._version() if existed else None
            if version is not None and version > SCHEMA_VERSION:
                raise StoreError(f"모르는 저장소 버전: {version}")
            if version is not None and version < SCHEMA_VERSION:
                self._migrate(version)
            self._db.executescript(SCHEMA)
            self._ensure_compat_columns()
            if self._version() is None:
                self._db.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._ensure_installed_at()
        except sqlite3.DatabaseError as e:
            # 열리지 않으면 멈춘다. 빈 상태로 조용히 뜨지 않는다.
            raise StoreCorrupt(f"저장소를 열 수 없습니다: {self.path}: {e}") from e
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._pattern_warned: str | None = None
        self.filename_pattern_problem()  # 뜰 때 저장된 패턴을 쓸 수 없으면 경고를 남긴다

    def _ensure_compat_columns(self) -> None:
        """버전과 상관없이 없는 열만 더한다. 기존 행은 빈 값이다."""
        for table, name, decl in COMPAT_COLUMNS:
            cols = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if name not in cols:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _ensure_installed_at(self) -> None:
        """처음 설치한 시각을 한 번만 적는다. 이 값이 없던 저장소는 가장 이른 채널 등록·녹화 시작 시각으로 채운다."""
        if self._db.execute("SELECT 1 FROM meta WHERE key='installed_at'").fetchone():
            return
        row = self._db.execute("SELECT MIN(t) FROM (SELECT MIN(created_at) AS t FROM channels"
                               " UNION ALL SELECT MIN(started_at) FROM sessions)").fetchone()
        self._db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('installed_at', ?)", (_earlier(row[0], now_iso()),))

    def _version(self) -> int | None:
        has_meta = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if not has_meta:
            has_channels = self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channels'").fetchone()
            return 1 if has_channels else None
        row = self._db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row[0]) if row else None

    def backup_path(self, version: int) -> Path:
        """옮기기 전 사본: 같은 폴더의 `<파일 이름>.schema<옛 버전>.bak`."""
        return self.path.with_name(f"{self.path.name}.schema{version}.bak")

    def _migrate(self, version: int) -> None:
        """옛 형식에서 옮긴다. 먼저 사본(0600, 한 번만)을 남기고, 한 트랜잭션으로 옮긴다. 실패하면 원래대로 두고 멈춘다."""
        backup = self.backup_path(version)
        if not backup.exists():
            target = sqlite3.connect(backup)
            try:
                self._db.backup(target)
            finally:
                target.close()
            os.chmod(backup, 0o600)
            log.info("저장소를 옮기기 전 사본을 남겼다", extra={"event": "store_backup", "from_version": version})
        try:
            with self.tx() as db:
                if version in (1, 2):  # v1·v2 → v3: 없는 세션 열을 더한다. 기존 행은 빈 값(기본값)이다
                    cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
                    for name, decl in SESSION_ADDED_COLUMNS:
                        if name not in cols:
                            db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")
                if version <= 3:  # v3 → v4: 플랫폼
                    self._migrate_v4(db)
                if version <= 4:  # v4 → v5: 이전 빌드의 켜고 끄기·설정 키를 확장 몫으로. 확장이 정한 대로 옮긴다
                    from .extensions import upgrade_legacy_settings

                    upgrade_legacy_settings(db, version)
                db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                db.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (str(SCHEMA_VERSION),))
        except sqlite3.DatabaseError as e:
            log.error("저장소를 새 형식으로 옮기지 못했다. 원래대로 두었다",
                      extra={"event": "store_migration_failed", "from_version": version, "reason": type(e).__name__})
            raise StoreError(f"저장소를 새 형식으로 옮기지 못했습니다(원래대로 두었습니다): {e}") from None
        log.info("저장소를 새 형식으로 옮겼다", extra={"event": "store_migrated", "from_version": version})

    @staticmethod
    def _migrate_v4(db: sqlite3.Connection) -> None:
        def columns(table: str) -> list[str]:
            return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]

        for table, create in REBUILD_V4.items():
            if not columns(table):
                continue  # 옛 저장소에 없던 표는 아래 SCHEMA가 새로 만든다
            db.execute(create)
            keep = [c for c in columns(f"{table}_v4") if c != "platform" and c in columns(table)]
            names = ", ".join(keep)
            db.execute(f"INSERT INTO {table}_v4 (platform, {names}) SELECT ?, {names} FROM {table}", (DEFAULT_PLATFORM,))
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {table}_v4 RENAME TO {table}")
        for table in ADD_PLATFORM_V4:
            cols = columns(table)
            if cols and "platform" not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN platform TEXT NOT NULL DEFAULT '{DEFAULT_PLATFORM}'")
        db.execute("DROP INDEX IF EXISTS sessions_channel")

    def filename_pattern_problem(self) -> tuple[str, str] | None:
        """저장된 파일명 패턴을 쓸 수 없으면 (저장된 값, 까닭). 그때는 기본 패턴을 쓴다. 저장된 값은 고치지 않는다.
        같은 값에는 경고를 한 번만 남긴다."""
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key='filename_pattern'").fetchone()
        if row is None:
            return None
        try:
            stored = json.loads(row[0])
        except ValueError:
            stored = row[0]
        problem = stored_pattern_problem(stored)
        if problem is None:
            return None
        stored = str(stored)
        if self._pattern_warned != stored:
            self._pattern_warned = stored
            log.warning("저장된 파일명 패턴을 쓸 수 없어 기본 패턴을 쓴다", extra={"event": "filename_pattern_invalid"})
        return stored, problem

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    # ---- 채널 ----
    @staticmethod
    def _channel(row: sqlite3.Row) -> Channel:
        return Channel(row["channel_id"], row["name"], row["output_dir"], row["quality"], row["extension"], bool(row["auto_record"]),
                       row["platform"], _stored_codec_preference(row["video_codec"]))

    def resolve_platform(self, channel_id: str, platform: str | None) -> str:
        """플랫폼을 적지 않은 요청: 그 채널 ID를 가진 채널의 플랫폼(기본 플랫폼 먼저). 없으면 기본 플랫폼."""
        if platform:
            return platform
        row = self._one("SELECT platform FROM channels WHERE channel_id=? ORDER BY platform != ? LIMIT 1",
                        (channel_id, DEFAULT_PLATFORM))
        return row["platform"] if row else DEFAULT_PLATFORM

    def add_channel(self, channel_id: str, name: str, output_dir: str | None = None, quality: str = "best",
                    extension: str = ".mp4", auto_record: bool = True, platform: str | None = None,
                    video_codec: str | None = None) -> Channel:
        platform = platform or DEFAULT_PLATFORM
        plat = registry.find(platform)
        if plat is None:
            raise ValidationError(f"모르는 플랫폼입니다: {platform}")
        channel_id = (channel_id or "").strip()
        if plat.valid_channel_id(channel_id.lower()):
            channel_id = channel_id.lower()
        if not plat.valid_channel_id(channel_id):
            raise ValidationError(plat.channel_id_error)
        name = (name or "").strip()
        if not name:
            raise ValidationError("표시 이름이 비었습니다")
        if not (output_dir or "").strip():
            output_dir = self.default_output_dir(plat.id, channel_id, name)
        folder = resolve_output_dir(self.record_root, output_dir)
        ext = normalize_extension(extension)
        self._check_ts_needs_remux(ext)
        codec_pref = normalize_codec_preference(video_codec)
        try:
            with self.tx() as db:
                db.execute(
                    "INSERT INTO channels (platform, channel_id, name, output_dir, quality, extension, auto_record, created_at,"
                    " video_codec) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (platform, channel_id, name, str(folder), quality_mod.normalize(quality), ext, int(bool(auto_record)), now_iso(),
                     codec_pref),
                )
        except sqlite3.IntegrityError:
            raise ValidationError("이미 등록된 채널 ID입니다") from None
        return self.get_channel(channel_id, platform)  # type: ignore[return-value]

    def default_output_dir(self, platform_id: str, channel_id: str, name: str) -> str:
        """저장 폴더를 비워 두었을 때: `<플랫폼 식별자>/<채널 이름>`(녹화 루트 기준). 플랫폼 폴더 이름은 플랫폼 모듈의
        식별자 그대로다. 채널 이름은 표시 이름에 파일 이름 규칙을 건 것이고, 비거나 `.`·`..`이면 채널 ID다.
        같은 폴더를 다른 채널이 이미 쓰면 `<채널 이름>_<채널 ID 앞 8자>`. 더할 때 한 번 정해 저장한다(이름을 바꿔도 따라가지 않는다)."""
        part = naming.sanitize(name).rstrip(".").strip()
        part = naming._truncate_bytes(part, 200).rstrip(". ")  # 폴더 이름의 255바이트 안(뒤에 _ID 8자를 붙일 자리도 남긴다)
        if part in ("", ".", ".."):
            part = channel_id
        candidate = f"{platform_id}/{part}"
        taken = {row["output_dir"] for row in self._all("SELECT output_dir FROM channels")}
        if str(resolve_output_dir(self.record_root, candidate)) in taken:
            candidate = f"{platform_id}/{part}_{channel_id[:8]}"
        return candidate

    def update_channel(self, channel_id: str, platform: str | None = None, **changes: Any) -> Channel:
        platform = self.resolve_platform(channel_id, platform)
        allowed = {"name", "output_dir", "quality", "extension", "auto_record", "video_codec"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(f"바꿀 수 없는 항목: {sorted(unknown)}")  # 채널 ID는 못 바꾼다
        sets, args = [], []
        if not str(changes.get("output_dir", "x") or "").strip():
            changes.pop("output_dir")  # 수정에서 빈 폴더는 "바꾸지 않음"
        for key, value in changes.items():
            if key == "name":
                value = (value or "").strip()
                if not value:
                    raise ValidationError("표시 이름이 비었습니다")
            elif key == "output_dir":
                value = str(resolve_output_dir(self.record_root, value))
            elif key == "quality":
                value = quality_mod.normalize(value)
            elif key == "extension":
                value = normalize_extension(value)
                self._check_ts_needs_remux(value)
            elif key == "auto_record":
                value = int(bool(value))
            elif key == "video_codec":
                value = normalize_codec_preference(value)
            sets.append(f"{key}=?")
            args.append(value)
        if sets:
            with self.tx() as db:
                cur = db.execute(f"UPDATE channels SET {', '.join(sets)} WHERE platform=? AND channel_id=?",
                                 (*args, platform, channel_id))
                if cur.rowcount == 0:
                    raise ValidationError("없는 채널입니다")
        ch = self.get_channel(channel_id, platform)
        if ch is None:
            raise ValidationError("없는 채널입니다")
        return ch

    def _check_ts_needs_remux(self, ext: str) -> None:
        """변환을 끈 채 `.ts`를 고르는 조합은 거부한다. 받은 파일은 조각난 MP4라서다."""
        if ext == ".ts" and not self.get_settings().remux_enabled:
            raise ValidationError("마무리 변환이 꺼져 있으면 확장자 .ts를 쓸 수 없습니다")

    def delete_channel(self, channel_id: str, platform: str | None = None) -> None:
        platform = self.resolve_platform(channel_id, platform)
        with self.tx() as db:
            db.execute("DELETE FROM channels WHERE platform=? AND channel_id=?", (platform, channel_id))
            db.execute("DELETE FROM channel_state WHERE platform=? AND channel_id=?", (platform, channel_id))

    def get_channel(self, channel_id: str, platform: str | None = None) -> Channel | None:
        if platform:
            row = self._one("SELECT * FROM channels WHERE platform=? AND channel_id=?", (platform, channel_id))
        else:
            row = self._one("SELECT * FROM channels WHERE channel_id=? ORDER BY platform != ? LIMIT 1", (channel_id, DEFAULT_PLATFORM))
        return self._channel(row) if row else None

    def list_channels(self) -> list[Channel]:
        return [self._channel(r) for r in self._all("SELECT * FROM channels ORDER BY created_at, platform, channel_id")]

    # ---- 설정 ----
    def get_settings(self) -> Settings:
        stored = {r["key"]: json.loads(r["value"]) for r in self._all("SELECT key, value FROM settings")}
        return Settings.from_mapping(stored)

    def set_settings(self, changes: dict[str, Any]) -> Settings:
        """전부 검증한 뒤 한 트랜잭션으로 쓴다. 넘기지 않은 키는 그대로 둔다."""
        clean = {k: Settings.validate(k, v) for k, v in changes.items()}
        if clean.get("remux_enabled") is False:
            ts = [c.name for c in self.list_channels() if c.extension == ".ts"]
            if ts:
                raise ConfigError("확장자가 .ts인 채널이 있어 마무리 변환을 끌 수 없습니다: " + ", ".join(ts))
        if clean.get("notify_enabled") is True and self.telegram_path is not None:
            secret = secretstore.telegram_load(self.telegram_path)
            if not secret.get("bot_token") or not secret.get("chat_id"):
                raise ConfigError("봇 토큰과 chat_id를 먼저 저장해야 알림을 켤 수 있습니다")
        with self.tx() as db:
            for k, v in clean.items():
                db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (k, json.dumps(v)))
        return self.get_settings()

    # ---- 설정 표의 다른 값(확장) ----
    # `ext:`로 시작하는 키는 Settings 밖이다. get_settings는 읽지 않는다
    def enabled_platforms(self) -> list[str]:
        """켠 "켜야 쓰는 플랫폼"의 ID. 키가 없으면(처음 설치, 이전 빌드의 저장소) 아무것도 켜지 않은 것이다."""
        value = self.get_extra(PLATFORMS_ENABLED_KEY, [])
        return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []

    def set_platform_enabled(self, platform_id: str, on: bool) -> None:
        current = [x for x in self.enabled_platforms() if x != platform_id]
        self.set_extra({PLATFORMS_ENABLED_KEY: current + ([platform_id] if on else [])})

    def gate_settings(self, platform_id: str) -> dict[str, float]:
        """플랫폼의 스캔 주기와 채널 사이 간격. 저장한 값이 없으면 초깃값: 스캔 주기는 이전 빌드의 전역 확인 주기
        (`check_interval`, 저장돼 있을 때. 올라올 때 옮기는 규칙)나 기본 60초, 채널 사이 간격은 기본 15초."""
        from . import gate as gate_mod

        stored = self.get_extra(GATE_KEY, {}) or {}
        mine = stored.get(platform_id) if isinstance(stored, dict) else None
        mine = mine if isinstance(mine, dict) else {}
        legacy = self._one("SELECT value FROM settings WHERE key='check_interval'")
        scan = mine.get("scan")
        if not isinstance(scan, (int, float)) or isinstance(scan, bool):
            scan = gate_mod.DEFAULT_SCAN
            if legacy is not None:
                try:
                    scan = gate_mod.validate_scan(json.loads(legacy["value"]))
                except (ValueError, TypeError):
                    scan = gate_mod.DEFAULT_SCAN
        gap = mine.get("gap")
        if not isinstance(gap, (int, float)) or isinstance(gap, bool):
            gap = gate_mod.DEFAULT_GAP
        return {"scan": float(scan), "gap": float(gap)}

    def set_gate_settings(self, platform_id: str, scan: float | None = None, gap: float | None = None) -> dict[str, float]:
        from . import gate as gate_mod

        current = self.gate_settings(platform_id)
        if scan is not None:
            current["scan"] = gate_mod.validate_scan(scan)
        if gap is not None:
            current["gap"] = gate_mod.validate_gap(gap)
        stored = self.get_extra(GATE_KEY, {}) or {}
        stored = dict(stored) if isinstance(stored, dict) else {}
        stored[platform_id] = current
        self.set_extra({GATE_KEY: stored})
        return current

    def get_extra(self, key: str, default: Any = None) -> Any:
        if not key.startswith(EXTRA_PREFIXES):
            raise KeyError(key)
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_extra(self, values: dict[str, Any]) -> None:
        if not all(k.startswith(EXTRA_PREFIXES) for k in values):
            raise KeyError("ext:·platform: 키만 쓸 수 있습니다")
        with self.tx() as db:
            for k, v in values.items():
                db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (k, json.dumps(v)))

    # ---- 채널 상태 ----
    def set_state(self, channel_id: str, state: str | None = None, platform: str | None = None, **fields: Any) -> None:
        platform = self.resolve_platform(channel_id, platform)
        allowed = {"live_id", "live_title", "session_id", "error", "error_count", "error_flag", "skip_live_id",
                   "last_check_at", "last_check"}
        bad = set(fields) - allowed
        if bad:
            raise StoreError(f"모르는 상태 항목: {bad}")
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO channel_state (platform, channel_id, state, updated_at) VALUES (?, ?, ?, ?)",
                       (platform, channel_id, state or STOPPED, now_iso()))
            sets, args = ["updated_at=?"], [now_iso()]
            if state is not None:
                sets.append("state=?")
                args.append(state)
            for k, v in fields.items():
                sets.append(f"{k}=?")
                args.append(v)
            db.execute(f"UPDATE channel_state SET {', '.join(sets)} WHERE platform=? AND channel_id=?", (*args, platform, channel_id))

    def add_finalizing(self, channel_id: str, delta: int, platform: str | None = None) -> None:
        platform = self.resolve_platform(channel_id, platform)
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO channel_state (platform, channel_id, state, updated_at) VALUES (?, ?, ?, ?)",
                       (platform, channel_id, STOPPED, now_iso()))
            db.execute("UPDATE channel_state SET finalizing=MAX(0, finalizing+?), updated_at=? WHERE platform=? AND channel_id=?",
                       (delta, now_iso(), platform, channel_id))

    def reset_runtime_state(self) -> None:
        """기동할 때. 지난 실행의 진행 표시를 지운다. 건너뛸 방송 ID(live_id)는 남긴다."""
        with self.tx() as db:
            db.execute("UPDATE channel_state SET state=?, session_id=NULL, finalizing=0, updated_at=?", (STOPPED, now_iso()))

    def get_state(self, channel_id: str, platform: str | None = None) -> dict[str, Any]:
        platform = self.resolve_platform(channel_id, platform)
        row = self._one("SELECT * FROM channel_state WHERE platform=? AND channel_id=?", (platform, channel_id))
        if row is None:
            return {"channel_id": channel_id, "platform": platform, "state": STOPPED, "live_id": None, "live_title": None, "session_id": None,
                    "error": None, "error_count": 0, "error_flag": 0, "skip_live_id": None, "finalizing": 0,
                    "last_check_at": None, "last_check": None, "updated_at": None}
        return dict(row)

    @staticmethod
    def display_state(state_row: dict[str, Any]) -> str:
        """녹화 중이 아니고 마무리가 돌고 있으면 '마무리'로 보인다."""
        st = state_row["state"]
        if state_row.get("finalizing") and st != RECORDING:
            return FINALIZING
        return st

    # ---- 녹화 세션 ----
    def create_session(self, **fields: Any) -> int:
        fields.setdefault("started_at", now_iso())
        if not fields.get("platform"):
            fields["platform"] = self.resolve_platform(fields.get("channel_id", ""), None)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.tx() as db:
            cur = db.execute(f"INSERT INTO sessions ({cols}) VALUES ({marks})", tuple(fields.values()))
            return int(cur.lastrowid)

    def update_session(self, session_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as db:
            db.execute(f"UPDATE sessions SET {sets} WHERE id=?", (*fields.values(), session_id))

    def delete_session(self, session_id: int) -> None:
        """받은 것 없이 방송 종료로 본 재시작은 세션 기록으로 남기지 않는다."""
        with self.tx() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM sessions WHERE id=?", (session_id,))
        return dict(row) if row else None

    def list_sessions(self, channel_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if channel_id:
            rows = self._all("SELECT * FROM sessions WHERE channel_id=? ORDER BY id DESC LIMIT ?", (channel_id, limit))
        else:
            rows = self._all("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    def open_sessions(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id")]

    def unfinalized_sessions(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all(
            "SELECT * FROM sessions WHERE ended_at IS NOT NULL AND finalize_status IN ('pending', 'running') ORDER BY id")]

    def last_recorded_session(self, channel_id: str, platform: str | None = None) -> dict[str, Any] | None:
        """마지막으로 끝난, 받은 것이 있는 녹화."""
        platform = self.resolve_platform(channel_id, platform)
        row = self._one("SELECT * FROM sessions WHERE platform=? AND channel_id=? AND ended_at IS NOT NULL AND COALESCE(bytes, 0) > 0"
                        " ORDER BY id DESC LIMIT 1", (platform, channel_id))
        return dict(row) if row else None

    def sessions_of_live(self, channel_id: str, live_id: str, platform: str | None = None) -> list[dict[str, Any]]:
        """같은 방송에서 나뉜 세션."""
        platform = self.resolve_platform(channel_id, platform)
        return [dict(r) for r in self._all("SELECT * FROM sessions WHERE platform=? AND channel_id=? AND live_id=? ORDER BY id",
                                           (platform, channel_id, live_id))]

    def retryable_sessions(self) -> list[dict[str, Any]]:
        """마무리 실패로 남았고 아직 자동으로 다시 시도하지 않은 세션. 원본이 있는지는 부르는 쪽이 본다."""
        return [dict(r) for r in self._all(
            "SELECT * FROM sessions WHERE finalize_status='failed' AND output_path IS NOT NULL"
            " AND COALESCE(finalize_retries, 0)=0 ORDER BY id")]

    def sessions_between(self, start_iso: str, end_iso: str, channel_id: str | None = None) -> list[dict[str, Any]]:
        """구간과 겹치는 세션(시작 < 끝, 끝 > 시작 또는 진행 중). 시각은 UTC ISO 문자열로 견준다."""
        sql = ("SELECT * FROM sessions WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?)"
               + (" AND channel_id=?" if channel_id else "") + " ORDER BY started_at, id")
        args: tuple = (end_iso, start_iso, channel_id) if channel_id else (end_iso, start_iso)
        return [dict(r) for r in self._all(sql, args)]

    def history_start(self) -> str:
        """기록이 있을 수 있는 가장 이른 시각(UTC ISO): 설치 시각과 가장 이른 녹화 시작 중 앞선 것."""
        row = self._one("SELECT (SELECT value FROM meta WHERE key='installed_at'), (SELECT MIN(started_at) FROM sessions)")
        return _earlier(row[1], _earlier(row[0], now_iso()))

    def known_final_paths(self) -> set[str]:
        return {r[0] for r in self._all("SELECT final_path FROM sessions WHERE final_path IS NOT NULL")}

    def known_part_paths(self) -> set[str]:
        return {r[0] for r in self._all("SELECT part_path FROM sessions WHERE part_path IS NOT NULL")}

    # ---- 조작 대기열 (CLI → 서버) ----
    def push_control(self, channel_id: str, action: str, platform: str | None = None) -> int:
        platform = self.resolve_platform(channel_id, platform)
        with self.tx() as db:
            cur = db.execute("INSERT INTO control (platform, channel_id, action, created_at) VALUES (?, ?, ?, ?)",
                             (platform, channel_id, action, now_iso()))
            return int(cur.lastrowid)

    def pending_controls(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT * FROM control WHERE done_at IS NULL ORDER BY id")]

    def finish_control(self, control_id: int, result: str) -> None:
        with self.tx() as db:
            db.execute("UPDATE control SET done_at=?, result=? WHERE id=?", (now_iso(), result, control_id))

    def get_control(self, control_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM control WHERE id=?", (control_id,))
        return dict(row) if row else None
