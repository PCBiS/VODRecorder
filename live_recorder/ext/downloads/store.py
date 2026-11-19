"""다시보기 작업 저장소. 정리된 공개 데이터만 저장한다.

형식은 replay.sqlite3의 records·requests·logs 표다. 여러 스레드에서 쓰도록 `check_same_thread=False`로 열고, 파일 권한을 0600으로 둔다.
채널·영상·작업·받은 기록 표에 플랫폼 열을 더했다(기존 줄은 치지직). 옛 형식은 사본을 남기고 옮긴다.
"""
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3


def safe_path(path):
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError('symlink_path')
    return path


def directory(path):
    path = safe_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError('directory_required')
    return path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


# 채널·영상·작업·받은 기록에 플랫폼을 더한 형식(user_version 2). 플랫폼 열이 없던 형식은 1로 본다
SCHEMA_VERSION = 2
DEFAULT_PLATFORM = 'chzzk'  # 플랫폼 열이 없던 옛 줄은 치지직 몫이다
TABLES = {
    'records': (
        'id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,'
        f" platform TEXT NOT NULL DEFAULT '{DEFAULT_PLATFORM}'"),
    'downloads': (
        'video TEXT NOT NULL, quality TEXT NOT NULL, title TEXT NOT NULL, filename TEXT NOT NULL, completed_at TEXT NOT NULL,'
        f" platform TEXT NOT NULL DEFAULT '{DEFAULT_PLATFORM}', PRIMARY KEY(platform,video,quality)"),
    'catalog_videos': (
        'channel TEXT NOT NULL, video TEXT NOT NULL, title TEXT NOT NULL, video_type TEXT NOT NULL,'
        ' publish_date TEXT, month TEXT, duration INTEGER, adult INTEGER NOT NULL, thumbnail TEXT,'
        f" seen INTEGER NOT NULL DEFAULT 0, platform TEXT NOT NULL DEFAULT '{DEFAULT_PLATFORM}', PRIMARY KEY(platform,channel,video)"),
    'catalog_channels': (
        'channel TEXT NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL, run INTEGER NOT NULL DEFAULT 0,'
        ' pages_done INTEGER NOT NULL DEFAULT 0, total_pages INTEGER, fetched INTEGER NOT NULL DEFAULT 0,'
        ' error TEXT, started_at TEXT, finished_at TEXT, checked_at TEXT, full_at TEXT,'
        f" platform TEXT NOT NULL DEFAULT '{DEFAULT_PLATFORM}', PRIMARY KEY(platform,channel)"),
    'replay_channels': (
        f"channel TEXT NOT NULL, name TEXT NOT NULL, added_at TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '{DEFAULT_PLATFORM}',"
        ' PRIMARY KEY(platform,channel)'),
    'replay_channels_removed': (
        f"channel TEXT NOT NULL, removed_at TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '{DEFAULT_PLATFORM}',"
        ' PRIMARY KEY(platform,channel)'),
}
log = logging.getLogger('live_recorder.replay.store')


class MigrationError(RuntimeError):
    """다시보기 저장소를 새 형식으로 옮기지 못했다. 저장소는 원래대로 두었고 서버는 뜨지 않는다."""


def backup_path(path, version):
    """옮기기 전 사본: 같은 폴더의 `<파일 이름>.schema<옛 버전>.bak`(녹화 저장소와 같은 규칙)."""
    path = Path(path)
    return path.with_name(f'{path.name}.schema{version}.bak')


class Store:
    def __init__(self, root):
        self.root = directory(root)
        self.lock = os.open(self.root / 'manager.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = safe_path(self.root / 'replay.sqlite3')
            self.path = path
            self.db = sqlite3.connect(path, check_same_thread=False)
            os.chmod(path, 0o600)
            self.db.row_factory = sqlite3.Row
            self.db.executescript('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;')
            try:
                self._migrate()
            except BaseException:
                self.db.close()
                raise
            # 표는 여기서만 만든다. 이미 있으면 그대로 둔다
            # downloads: 받은 영상 기억. 작업 내역 정리와 무관하게 남긴다
            # catalog_*: 채널 다시보기 목록 수집. 영상 줄은 화면에 보이는 값과 영상 번호만 둔다.
            #   seen은 그 영상을 마지막으로 본 전체 수집의 회차다
            # replay_channels*: 다시보기 채널(녹화 채널과 따로 둔 목록)과 관리자가 지운 채널
            self.db.executescript(''.join(f'CREATE TABLE IF NOT EXISTS {name}({cols});' for name, cols in TABLES.items()) + '''
              CREATE INDEX IF NOT EXISTS records_queue ON records(kind,status);
              CREATE TABLE IF NOT EXISTS requests(
                owner TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, ids TEXT NOT NULL,
                PRIMARY KEY(owner,key));
              CREATE TABLE IF NOT EXISTS logs(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL,
                at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS catalog_videos_month ON catalog_videos(platform,channel,month,publish_date);
            ''')
            self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        except BaseException:
            os.close(self.lock)
            raise

    def _columns(self, table):
        return [r[1] for r in self.db.execute(f'PRAGMA table_info({table})')]

    def _migrate(self):
        """플랫폼 열이 없던 형식이면 사본(0600, 한 번만)을 남기고 한 트랜잭션으로 옮긴다. 실패하면 원래대로 두고 멈춘다.
        이미 옮겼거나 새 저장소면 아무 일도 하지 않는다."""
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise MigrationError(f'모르는 다시보기 저장소 버전: {version}')
        old = [t for t in TABLES if self._columns(t) and 'platform' not in self._columns(t)]
        if not old:
            return
        version = version or 1
        backup = backup_path(self.path, version)
        if not backup.exists():
            target = sqlite3.connect(backup)
            try:
                self.db.backup(target)
            finally:
                target.close()
            os.chmod(backup, 0o600)
            log.info('다시보기 저장소를 옮기기 전 사본을 남겼다', extra={'event': 'store_backup', 'db': 'replay', 'from_version': version})
        level = self.db.isolation_level
        self.db.isolation_level = None  # 표 바꾸기(DDL)까지 한 트랜잭션에 넣는다
        try:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                for table in old:
                    self.db.execute(f'CREATE TABLE {table}_v2({TABLES[table]})')
                    keep = [c for c in self._columns(f'{table}_v2') if c != 'platform' and c in self._columns(table)]
                    names = ','.join(keep)
                    self.db.execute(f'INSERT INTO {table}_v2(platform,{names}) SELECT ?,{names} FROM {table}', (DEFAULT_PLATFORM,))
                    self.db.execute(f'DROP TABLE {table}')
                    self.db.execute(f'ALTER TABLE {table}_v2 RENAME TO {table}')
                self.db.execute('DROP INDEX IF EXISTS catalog_videos_month')
                self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            self.db.execute('COMMIT')
        except sqlite3.DatabaseError as e:
            log.error('다시보기 저장소를 새 형식으로 옮기지 못했다. 원래대로 두었다',
                      extra={'event': 'store_migration_failed', 'db': 'replay', 'from_version': version, 'reason': type(e).__name__})
            raise MigrationError(f'다시보기 저장소를 새 형식으로 옮기지 못했습니다(원래대로 두었습니다): {e}') from None
        finally:
            self.db.isolation_level = level
        log.info('다시보기 저장소를 새 형식으로 옮겼다', extra={'event': 'store_migrated', 'db': 'replay', 'from_version': version})

    def save(self, kind, owner, data):
        self.db.execute('INSERT INTO records(id,kind,owner,status,payload) VALUES(?,?,?,?,?) '
                        'ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload',
                        (data['id'], kind, owner, data['status'], canonical(data)))

    def get(self, kind, owner, ident):
        row = self.db.execute('SELECT payload FROM records WHERE id=? AND kind=? AND owner=?',
                              (ident, kind, owner)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()
        os.close(self.lock)
