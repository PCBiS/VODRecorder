"""저장소 v2 → v3. 이전 빌드의 기존 DB가 새 코드로 그대로 열려야 한다."""

import sqlite3

from live_recorder.core import store as st

CH = "a" * 32

# 이전 이미지가 만든 v2 sessions 표(열 순서까지 그대로)
V2_SESSIONS = """
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL, live_id TEXT, title TEXT, source TEXT, note TEXT, time_machine_active INTEGER,
    pid INTEGER, started_at TEXT NOT NULL, ended_at TEXT, end_reason TEXT, end_detail TEXT, exit_code INTEGER,
    part_path TEXT, final_path TEXT, bytes INTEGER, media_seconds REAL, first_media_time TEXT, last_media_time TEXT,
    clean_exit INTEGER NOT NULL DEFAULT 0, finalize_status TEXT, finalize_detail TEXT, output_path TEXT
);
"""


def _make_v2(path, record_root):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '2');
        CREATE TABLE channels (channel_id TEXT PRIMARY KEY, name TEXT NOT NULL, output_dir TEXT NOT NULL,
            quality TEXT NOT NULL, extension TEXT NOT NULL CHECK (extension IN ('.ts', '.mp4')),
            auto_record INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO settings VALUES ('rewind_seconds', '3600');
    """ + V2_SESSIONS)
    db.execute("INSERT INTO channels VALUES (?, '채널', ?, 'best', '.mp4', 1, '2026-11-11T09:00:00.000+00:00')",
               (CH, str(record_root / "ch")))
    db.execute("INSERT INTO sessions (channel_id, live_id, title, source, started_at, ended_at, end_reason, exit_code,"
               " bytes, finalize_status, output_path) VALUES (?, '22143418', '첫 방송', 'timemachine',"
               " '2026-11-11T10:05:52.000+00:00', '2026-11-11T19:21:56.000+00:00', 'broadcast_end', 0, 34000000000,"
               " 'failed', '/recordings/ch/x.raw.mp4')", (CH,))
    db.execute("INSERT INTO sessions (channel_id, started_at) VALUES (?, '2026-11-11T19:21:56.700+00:00')", (CH,))
    db.commit()
    db.close()


def test_v2_database_opens_and_gains_columns(tmp_path):
    path = tmp_path / "db.sqlite3"
    _make_v2(path, tmp_path)
    store = st.Store(path, tmp_path)
    cols = {r[1] for r in store._all("PRAGMA table_info(sessions)")}
    assert {"quality_label", "url_refreshes", "finalize_retries", "live_id", "title"} <= cols
    assert store._one("SELECT value FROM meta WHERE key='schema_version'")[0] == str(st.SCHEMA_VERSION)  # 지금 형식까지 옮긴다
    old, empty = store.list_sessions(None, 10)
    assert old["title"] == "첫 방송" and old["live_id"] == "22143418" and old["bytes"] == 34000000000
    assert old["quality_label"] is None and old["url_refreshes"] is None and old["finalize_retries"] == 0
    assert empty["title"] is None and empty["live_id"] is None  # 기존 행의 빈 값은 그대로 둔다
    assert store.get_channel(CH).name == "채널" and store.get_settings().rewind_seconds == 3600
    # 옮긴 행도 자동 다시 마무리 후보가 된다(원본이 있는지는 부르는 쪽이 본다)
    assert [s["id"] for s in store.retryable_sessions()] == [old["id"]]
    sid = store.create_session(channel_id=CH, quality_label="1080p60", url_refreshes=2)
    assert store.get_session(sid)["quality_label"] == "1080p60"
    store.close()
    st.Store(path, tmp_path).close()  # 두 번 열어도 그대로다


def test_v1_without_title_columns_still_opens(tmp_path):
    """열이 아예 없는 아주 옛 저장소도 연다(title·live_id까지 없는 경우)."""
    path = tmp_path / "db.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id TEXT NOT NULL, started_at TEXT NOT NULL,
            ended_at TEXT, end_reason TEXT, clean_exit INTEGER NOT NULL DEFAULT 0, finalize_status TEXT,
            output_path TEXT, part_path TEXT, final_path TEXT);
    """)
    db.execute("INSERT INTO sessions (channel_id, started_at) VALUES (?, '2026-11-11T00:00:00+00:00')", (CH,))
    db.commit()
    db.close()
    store = st.Store(path, tmp_path)
    row = store.list_sessions(None, 5)[0]
    assert row["title"] is None and row["live_id"] is None and row["finalize_retries"] == 0
