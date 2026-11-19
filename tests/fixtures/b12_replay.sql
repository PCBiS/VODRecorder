BEGIN TRANSACTION;
CREATE TABLE catalog_channels(
                channel TEXT PRIMARY KEY, status TEXT NOT NULL, mode TEXT NOT NULL, run INTEGER NOT NULL DEFAULT 0,
                pages_done INTEGER NOT NULL DEFAULT 0, total_pages INTEGER, fetched INTEGER NOT NULL DEFAULT 0,
                error TEXT, started_at TEXT, finished_at TEXT, checked_at TEXT, full_at TEXT);
INSERT INTO "catalog_channels" VALUES('0123456789abcdef0123456789abcdef','done','full',1,1,1,1,NULL,NULL,'2026-11-12T00:00:00+00:00','2026-11-12T00:00:00+00:00','2026-11-12T00:00:00+00:00');
CREATE TABLE catalog_videos(
                channel TEXT NOT NULL, video TEXT NOT NULL, title TEXT NOT NULL, video_type TEXT NOT NULL,
                publish_date TEXT, month TEXT, duration INTEGER, adult INTEGER NOT NULL, thumbnail TEXT,
                seen INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(channel,video));
INSERT INTO "catalog_videos" VALUES('0123456789abcdef0123456789abcdef','100','옛 수집','replay','2026-09-01 00:00:00','2026-09',60,0,NULL,1);
CREATE TABLE downloads(
                video TEXT NOT NULL, quality TEXT NOT NULL, title TEXT NOT NULL, filename TEXT NOT NULL,
                completed_at TEXT NOT NULL, PRIMARY KEY(video,quality));
INSERT INTO "downloads" VALUES('777','q','옛 영상','old.mp4','2026-11-11T00:00:00+00:00');
CREATE TABLE logs(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL,
                at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE records(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL);
INSERT INTO "records" VALUES('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','job','admin','completed','{"completed_at":"2026-11-11T00:00:00+00:00","filename":"old.mp4","id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","quality":"q","status":"completed","title":"옛 영상","updated_at":"2026-11-11T00:00:00+00:00","url":"https://chzzk.naver.com/video/777"}');
CREATE TABLE replay_channels(
                channel TEXT PRIMARY KEY, name TEXT NOT NULL, added_at TEXT NOT NULL);
INSERT INTO "replay_channels" VALUES('0123456789abcdef0123456789abcdef','녹화채널','2026-11-12T00:00:00+00:00');
INSERT INTO "replay_channels" VALUES('eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee','다시보기전용','2026-11-12T00:00:00+00:00');
CREATE TABLE replay_channels_removed(
                channel TEXT PRIMARY KEY, removed_at TEXT NOT NULL);
INSERT INTO "replay_channels_removed" VALUES('dddddddddddddddddddddddddddddddd','2026-11-12T00:00:00+00:00');
CREATE TABLE requests(
                owner TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, ids TEXT NOT NULL,
                PRIMARY KEY(owner,key));
CREATE INDEX records_queue ON records(kind,status);
CREATE INDEX catalog_videos_month ON catalog_videos(channel,month,publish_date);
DELETE FROM "sqlite_sequence";
COMMIT;
