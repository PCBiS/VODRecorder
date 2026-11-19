BEGIN TRANSACTION;
CREATE TABLE channel_state (
    channel_id TEXT PRIMARY KEY,
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
    updated_at TEXT NOT NULL
);
INSERT INTO "channel_state" VALUES('0123456789abcdef0123456789abcdef','watching',NULL,NULL,NULL,NULL,0,0,NULL,0,NULL,NULL,'2026-11-13T03:30:57.224+00:00');
CREATE TABLE channels (
    channel_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    quality TEXT NOT NULL,
    extension TEXT NOT NULL CHECK (extension IN ('.ts', '.mp4')),
    auto_record INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
INSERT INTO "channels" VALUES('0123456789abcdef0123456789abcdef','녹화채널','/tmp/tmpqawzta0z/rec/ch1','1080p','.mp4',1,'2026-11-13T03:30:57.224+00:00');
INSERT INTO "channels" VALUES('ffffffffffffffffffffffffffffffff','둘째채널','/tmp/tmpqawzta0z/rec/ch2','best','.ts',1,'2026-11-13T03:30:57.224+00:00');
CREATE TABLE control (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL,
    done_at TEXT,
    result TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "meta" VALUES('schema_version','3');
CREATE TABLE sessions (
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
    finalize_retries INTEGER NOT NULL DEFAULT 0
);
INSERT INTO "sessions" VALUES(1,'0123456789abcdef0123456789abcdef','9001','옛 방송','timemachine',NULL,NULL,NULL,'2026-11-12T10:00:00+00:00','2026-11-12T11:00:00+00:00','broadcast_end',NULL,NULL,'/tmp/tmpqawzta0z/rec/ch1/a.mp4.part','/tmp/tmpqawzta0z/rec/ch1/a.mp4',12345,NULL,NULL,NULL,0,'done',NULL,'/tmp/tmpqawzta0z/rec/ch1/a.mp4',NULL,NULL,0);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO "settings" VALUES('rewind_seconds','60');
INSERT INTO "settings" VALUES('filename_date_format','"YYMMDD"');
INSERT INTO "settings" VALUES('check_interval','30');
CREATE INDEX sessions_channel ON sessions (channel_id, id);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('sessions',1);
COMMIT;
