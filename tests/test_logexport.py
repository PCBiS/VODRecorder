"""로그 내려받기와 가리기."""

import ast
import io
import json
import logging
import re
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_recorder.platforms import credentials
from live_recorder import logexport, logmask, web
from live_recorder.core import logs, secretstore
from live_recorder.core import store as st

from conftest import CHANNEL
from test_view_only import ViewSite
from test_web import PASSWORD, Web
from conftest import REPLAY_EXTENSIONS
from live_recorder.core.extensions import LogFile
from live_recorder.ext.downloads.extension import JOB_LOG_FIELDS as REPLAY_FIELDS
from live_recorder.ext.downloads.extension import JOB_LOG_FILE as REPLAY_FILE


def job_logs(rows):
    """downloads 확장이 내놓는 작업 로그(로그 내려받기는 확장이 무엇인지 모른다)."""
    return {"files": [LogFile(REPLAY_FILE, list(rows), time_key="at", fields=REPLAY_FIELDS)]}

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "LOG-EXPORT.md"
EXPORT = "/api/logs/export"
OTHER = "f" * 32
ADMIN_NAME = "planted_admin_kim"
NOW = datetime(2026, 11, 13, 3, 0, tzinfo=timezone.utc)

# 늘 가려야 하는 값
AUT, SES = "AUTVALUE-planted-1234567", "SESVALUE-planted-7654321"
TG = "1234567890:AAH-planted-telegram-token-abcdefghijk"
SETUP = secretstore.format_code(bytes(range(20)))
BEARER = "planted-bearer-value-xyz"
HDR_COOKIE = "planted-cookie-header-qrs"
SIG = "1700000000_plantedsignature0123"
# 공유용에서 가려야 하는 값
CH_NAME, OTHER_NAME = "비밀채널가나", "다시보기전용다라"
TITLE = "비밀제목라마바"
FILE_NAME = f"[261112_210000] {CH_NAME} {TITLE}.mp4"
PATH = "/recordings/ch1/secretfolder/deep.mp4"
IPV4, IPV6, HOST = "203.0.113.77", "2001:db8::77", "planted-host.example.org"
VIDEO_NO, LIVE_ID = "98765432", "13579246"


def planted_lines(csrf: str, session: str) -> list[str]:
    """로그에 섞여 들어온 비밀·식별 정보(원래 없어야 하지만 내보내기에서 한 번 더 걸러야 한다)."""
    return [
        f"쿠키 NID_AUT={AUT}; NID_SES={SES}",
        f'{{"NID_AUT": "{AUT}"}}',
        f"텔레그램 https://api.telegram.org/bot{TG}/sendMessage 실패",
        f"[other] 첫 설정 코드: {SETUP}  화면 /setup 에 넣는다",  # 다른 머리말이 붙은 줄도 가린다
        f"[VOD Recorder] 첫 설정 코드: {SETUP}  화면 /setup 에 넣는다",
        f"세션 lr_session={session} csrf {csrf}",
        f"Authorization: Bearer {BEARER}",
        f"Cookie: foo={HDR_COOKIE}",
        f"https://a01-g-naver-vod.akamaized.net/v.mp4?__gda__={SIG}&x=1",
        f"녹화 시작 {FILE_NAME} {PATH} 채널 {CH_NAME} ({CHANNEL}) {OTHER_NAME} {OTHER}",
        f"접속 {IPV4} [{IPV6}]:8080 {HOST} 관리자 {ADMIN_NAME}",
        f"https://chzzk.naver.com/video/{VIDEO_NO} liveId={LIVE_ID} 제목 {TITLE}",
    ]


SECRETS = [AUT, SES, TG, TG.split(":", 1)[1], SETUP, SETUP.replace("-", ""), BEARER, HDR_COOKIE, SIG]
# 기본 수준에서도 늘 가리는 식별 정보: 접속한 쪽의 주소와 관리자 사용자 이름
ALWAYS = [IPV4, IPV6, ADMIN_NAME]
IDENTIFYING = [CHANNEL, OTHER, CH_NAME, OTHER_NAME, TITLE, FILE_NAME, "secretfolder", "deep.mp4", IPV4, IPV6, HOST, ADMIN_NAME,
               VIDEO_NO, LIVE_ID, "akamaized", "chzzk.naver.com"]


def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist()}


def blob(files: dict[str, bytes]) -> bytes:
    return b"\n".join(files.values()) + b"\n" + b"\n".join(n.encode() for n in files)


# ---- 가리기 단위 시험 ----

def test_masker_default_hides_secrets_addresses_and_admin_only():
    """기본 수준. 비밀·접속 주소·관리자 이름만 가리고 채널·제목·파일·치지직 주소는 그대로 둔다."""
    m = logmask.Masker(False, channels=[(CHANNEL, CH_NAME)], admin=ADMIN_NAME, secrets=[AUT, TG, SETUP, "short"], titles=[TITLE])
    out = "\n".join(m.text(line) for line in planted_lines("C" * 43, "S1" * 21 + "x"))
    for value in SECRETS + ALWAYS + ["C" * 43]:
        assert value not in out, value
    assert "[가림]" in out and "NID_AUT=[가림]" in out and "Authorization: [가림]" in out
    assert "접속 [주소] [[주소]]:8080" in out and "관리자 관리자" in out
    for value in (CH_NAME, CHANNEL, OTHER_NAME, TITLE, FILE_NAME, PATH, HOST, f"/video/{VIDEO_NO}", f"liveId={LIVE_ID}",
                  "https://a01-g-naver-vod.akamaized.net/v.mp4?__gda__=[가림]&x=1"):
        assert value in out, value
    assert m.counts["known_secret"] >= 3 and m.counts["header"] >= 2 and m.counts["address"] >= 2 and m.counts["admin"] == 1
    # 로그 줄은 모든 필드가 남고 client만 [주소]가 된다
    rec = {"ts": "2026-11-12T00:00:00.000+00:00", "msg": f"녹화 시작 {FILE_NAME} {IPV4}", "tmpl": "녹화 시작 %s", "event": "login",
           "client": "testclient", "channel_id": CHANNEL, "exc": f"Traceback\nValueError: {TITLE}", "surprise": TITLE}
    assert m.record(rec) == dict(rec, msg=f"녹화 시작 {FILE_NAME} [주소]", client="[주소]")


def test_masker_strict_replaces_identifiers_consistently():
    m = logmask.Masker(True, channels=[(CHANNEL, CH_NAME), (OTHER, OTHER_NAME)], admin=ADMIN_NAME, titles=[TITLE, "짧음"])
    out = "\n".join(m.text(line) for line in planted_lines("C" * 43, "S1" * 22))
    for value in SECRETS + IDENTIFYING:
        assert value not in out, value
    assert "채널-1" in out and "채널-2" in out and "관리자" in out and "[주소]" in out and "[파일].mp4" in out and "[제목]" in out
    assert m.channel(CHANNEL) == "채널-1" and m.channel(CHANNEL.upper()) == "채널-1" and m.channel(OTHER) == "채널-2"
    assert m.text(f"/video/{VIDEO_NO} 다시 /video/{VIDEO_NO}") == "/video/영상-1 다시 /video/영상-1"
    # 시각·숫자·크기는 남는다
    assert m.text("2026-11-12T03:04:05.123+00:00 12:34:56 1.5 GiB 3600초") == "2026-11-12T03:04:05.123+00:00 12:34:56 1.5 GiB 3600초"


def test_strict_record_keeps_template_and_listed_fields_only():
    m = logmask.Masker(True, channels=[(CHANNEL, CH_NAME)])
    rec = {"ts": "2026-11-12T00:00:00.000+00:00", "level": "INFO", "logger": "live_recorder.engine", "msg": f"녹화 시작 {FILE_NAME}",
           "tmpl": "녹화 시작 %s", "event": "record_start", "channel_id": CHANNEL, "session_id": 7, "live_id": LIVE_ID,
           "client": IPV4, "surprise": TITLE, "exc": f'Traceback ...\n  File "{PATH}", line 1\nValueError: {TITLE}'}
    out = m.record(rec)
    assert out == {"ts": rec["ts"], "level": "INFO", "logger": "live_recorder.engine", "event": "record_start", "channel_id": "채널-1",
                   "session_id": 7, "live_id": "방송-1", "client": "[주소]", "exc_type": "ValueError", "tmpl": "녹화 시작 %s"}
    assert m.counts["unknown_field"] == 1
    # 값을 채우지 않는 고정 문장은 tmpl로 남긴다. 그 밖의 문장(수신기 출력 등)은 뺀다
    assert m.record({"msg": "종료 시작", "level": "INFO"})["tmpl"] == "종료 시작"
    assert "tmpl" not in m.record({"msg": f"수신기 출력 {PATH}", "logger": "live_recorder.receiver"})
    assert "종료 시작" in logmask.constant_messages() and "녹화 시작 %s" not in logmask.constant_messages()


# ---- 묶음 ----

@pytest.fixture
def ex(tmp_path, server, tools):
    w = Web(tmp_path, server, tools, extensions=REPLAY_EXTENSIONS)  # 다시보기 작업 로그가 묶음에 들어가는지도 본다
    secretstore.remove(w.h.cfg.account_path)
    secretstore.create_account(w.h.cfg.account_path, ADMIN_NAME, PASSWORD)
    with w:
        r = w.client.post("/api/login", json={"username": ADMIN_NAME, "password": PASSWORD})
        assert r.status_code == 200, r.text
        w.csrf = w.client.get("/api/session").json()["csrf"]
        w.h.add_channel(name=CH_NAME)
        yield w


def plant(w):
    """비밀 저장소·DB·로그·다시보기 작업 로그에 심는다."""
    cfg = w.h.cfg
    credentials.save(cfg, "chzzk", {"NID_AUT": AUT, "NID_SES": SES})
    secretstore.telegram_save(cfg.telegram_path, TG, "424242")
    cfg.setup_code_path.write_text(SETUP)
    sid = w.h.store.create_session(channel_id=CHANNEL, live_id=LIVE_ID, title=TITLE, part_path=PATH + ".part", final_path=PATH,
                                   source="timemachine")
    w.h.store.update_session(sid, ended_at=st.now_iso() if hasattr(st, "now_iso") else None, end_reason="ended", bytes=1234)
    session = w.client.cookies.get(web.SESSION_COOKIE)
    log = logging.getLogger("live_recorder.engine")
    log.info("녹화 시작 %s", FILE_NAME, extra={"event": "record_start", "channel_id": CHANNEL, "session_id": sid, "source": "timemachine",
                                              "pid": 4242, "rewind": 60})
    log.info("방송 종료로 본다: %s", TITLE, extra={"event": "endlist_again", "channel_id": CHANNEL, "live_id": LIVE_ID})
    logging.getLogger("live_recorder.web").info("로그인", extra={"event": "login", "client": IPV4})
    logging.getLogger("live_recorder.web").warning("비밀번호 확인 실패", extra={"event": "login_failed", "client": IPV6})
    rlog = logging.getLogger("live_recorder.receiver")
    for line in planted_lines(w.csrf, session):
        rlog.info(line, extra={"channel_id": CHANNEL, "session_id": sid})
    try:
        raise ValueError(f"{TITLE} {PATH}")
    except ValueError:
        log.exception("채널 감시 중 예외", extra={"event": "watch_error", "channel_id": CHANNEL})
    for h in logging.getLogger("live_recorder").handlers:
        h.flush()
    # 다시보기 작업 로그와 다시보기 전용 채널
    rp = w.app.state.extensions.contexts["downloads"].service
    db = rp.manager.db
    with db:
        db.execute("INSERT INTO logs(job,at,level,message) VALUES(?,?,?,?)",
                   ("a" * 32, datetime.now(timezone.utc).isoformat(), "info", "받은 부분(1.5 GiB)을 남겼습니다."))
        db.execute("INSERT INTO logs(job,at,level,message) VALUES(?,?,?,?)",
                   ("a" * 32, datetime.now(timezone.utc).isoformat(), "error", f"실패 {FILE_NAME} NID_AUT={AUT} {OTHER_NAME}"))
        db.execute("INSERT INTO replay_channels(channel,name,added_at) VALUES(?,?,?)", (OTHER, OTHER_NAME, "2026-11-12T00:00:00+00:00"))
    return session


def export(w, **body):
    return w.call("POST", EXPORT, json=body)


def test_strict_bundle_contains_no_planted_value(ex):
    """더 가리기. 묶음의 어느 파일에도 심은 비밀·식별 값이 없다. 대신 이름은 파일을 건너 일관된다."""
    session = plant(ex)
    r = export(ex, strict=True)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip" and r.headers["cache-control"] == "no-store"
    assert re.fullmatch(r'attachment; filename="vodrecorder-logs-\d{8}-\d{6}\.zip"', r.headers["content-disposition"])
    files = unzip(r.content)
    assert set(files) == {"manifest.json", logexport.LOG_FILE, REPLAY_FILE, "diagnostics/summary.json",
                          "diagnostics/settings.json", "diagnostics/sessions.json", "diagnostics/log_summary.json"}
    data = blob(files)
    for value in SECRETS + IDENTIFYING + [ex.csrf, session, "424242"]:
        assert value.encode() not in data, value
    assert b"SQLite format 3" not in data
    lines = [json.loads(x) for x in files[logexport.LOG_FILE].splitlines()]
    assert lines and all("msg" not in x for x in lines)
    start = next(x for x in lines if x.get("event") == "record_start")
    assert start["channel_id"] == "채널-1" and start["tmpl"] == "녹화 시작 %s" and start["rewind"] == 60
    assert next(x for x in lines if x.get("event") == "endlist_again")["live_id"] == "방송-1"
    assert next(x for x in lines if x.get("event") == "login")["client"] == "[주소]"
    assert next(x for x in lines if x.get("event") == "watch_error")["exc_type"] == "ValueError"
    # 같은 채널은 모든 파일에서 같은 대신 이름이다
    sessions = json.loads(files["diagnostics/sessions.json"])
    settings = json.loads(files["diagnostics/settings.json"])
    assert sessions[0]["channel"] == "채널-1" and settings["channels"][0]["channel"] == "채널-1"
    replay = [json.loads(x) for x in files[REPLAY_FILE].splitlines()]
    assert replay[0]["message"] == "받은 부분(1.5 GiB)을 남겼습니다." and "채널-2" in replay[1]["message"]
    manifest = json.loads(files["manifest.json"])
    assert manifest["level"] == "strict" and manifest["level_label"] == "더 가리기"
    assert manifest["range"]["key"] == "3" and manifest["program"]["version"]
    assert manifest["files"][logexport.LOG_FILE]["lines"] == len(lines) and manifest["masked"]["message"] >= 1
    assert manifest["trimmed"]["applied"] is False and manifest["pseudonyms"]["channels"] == 2


def test_default_bundle_hides_secrets_addresses_and_admin_but_keeps_the_rest(ex):
    """기본 수준(요청에 strict를 주지 않음). 비밀·접속 주소·관리자 이름은 없고, 분석에 쓰는 값은 그대로 있다."""
    session = plant(ex)
    r = export(ex, range="all")
    assert r.status_code == 200, r.text
    files = unzip(r.content)
    data = blob(files)
    for value in SECRETS + ALWAYS + [ex.csrf, session, "424242"]:
        assert value.encode() not in data, value
    for value in (CH_NAME, OTHER_NAME, TITLE, FILE_NAME, CHANNEL, PATH, LIVE_ID, VIDEO_NO):  # 화면이 들어간다고 알린다
        assert value.encode() in data, value
    lines = [json.loads(x) for x in files[logexport.LOG_FILE].splitlines()]
    start = next(x for x in lines if x.get("event") == "record_start")
    assert start["msg"] == f"녹화 시작 {FILE_NAME}" and start["channel_id"] == CHANNEL
    login = next(x for x in lines if x.get("event") == "login")
    assert login["client"] == "[주소]" and login["msg"] == "로그인"
    assert TITLE in next(x for x in lines if x.get("event") == "watch_error")["exc"]
    manifest = json.loads(files["manifest.json"])
    assert manifest["level"] == "default" and manifest["level_label"] == "기본" and "share_mode" not in manifest
    assert manifest["masked"]["address"] >= 2 and manifest["masked"]["known_secret"] >= 1
    assert export(ex, share=False).status_code in (400, 422)  # 옛 이름은 받지 않는다


def test_nothing_left_on_disk_and_files_not_included(ex):
    """묶음을 서버에 남기지 않는다. 비밀 저장소·DB 파일은 넣지 않는다."""
    plant(ex)

    def snapshot():
        return {p: p.stat().st_size for base in (ex.h.cfg.data_dir, ex.h.root, ex.h.cfg.log_dir) for p in base.rglob("*")
                if p.is_file() and not p.name.startswith("live-recorder.log") and "-wal" not in p.name and "-shm" not in p.name}

    before = snapshot()
    files = unzip(export(ex).content)
    after = snapshot()
    assert set(after) == set(before)  # 새 파일이 없다(임시 zip 없음)
    assert not list(ex.h.cfg.data_dir.rglob("*.zip")) and not list(ex.h.root.rglob("*.zip"))
    names = " ".join(files)
    for banned in ("cookie", "account", "telegram", "sqlite", "secrets", ".mp4"):
        assert banned not in names


def write_log(log_dir: Path, stamps: list[datetime], rotated: bool = False):
    log_dir.mkdir(parents=True, exist_ok=True)
    rows = [json.dumps({"ts": t.isoformat(timespec="milliseconds"), "level": "INFO", "logger": "live_recorder.engine",
                        "msg": "종료 시작", "event": "shutdown", "pad": "x" * 200}) for t in stamps]
    if rotated:
        half = len(rows) // 2
        (log_dir / "live-recorder.log.1").write_text("\n".join(rows[:half]) + "\n", encoding="utf-8")
        (log_dir / "live-recorder.log").write_text("\n".join(rows[half:]) + "\nnot json\n", encoding="utf-8")
    else:
        (log_dir / "live-recorder.log").write_text("\n".join(rows) + "\n", encoding="utf-8")


@pytest.fixture
def plain(tmp_path, server, tools):
    from conftest import Harness

    h = Harness(tmp_path, server, tools)
    yield h
    h.store.close()


def test_range_selection(plain):
    """최근 1일·3일·7일·전체."""
    stamps = [NOW - timedelta(hours=h) for h in (240, 120, 48, 12, 1)]
    write_log(plain.cfg.log_dir, stamps, rotated=True)
    replay = [{"job": "b" * 32, "at": (NOW - timedelta(hours=h)).isoformat(), "level": "info", "message": "m"} for h in (100, 2)]
    counts = {}
    for key in ("1", "3", "7", "all"):
        name, data, manifest = logexport.build(plain.cfg, plain.store, range_key=key, extensions=job_logs(replay), now=NOW)
        counts[key] = (manifest["files"][logexport.LOG_FILE]["lines"], manifest["files"][REPLAY_FILE]["lines"])
        assert manifest["unreadable_log_lines"] == 1
    assert counts == {"1": (2, 1), "3": (3, 1), "7": (4, 2), "all": (5, 2)}
    assert name == "vodrecorder-logs-20261113-120000.zip"  # Asia/Seoul
    with pytest.raises(ValueError):
        logexport.build(plain.cfg, plain.store, range_key="2", now=NOW)
    # 순환된 파일은 오래된 것부터 읽는다
    lines = unzip(logexport.build(plain.cfg, plain.store, range_key="all", now=NOW)[1])[logexport.LOG_FILE].splitlines()
    ts = [json.loads(x)["ts"] for x in lines]
    assert ts == sorted(ts)


def test_size_limit_drops_oldest_lines_and_records_it(plain):
    """압축 전 크기가 한도를 넘으면 오래된 줄부터 빼고 manifest에 적는다."""
    stamps = [NOW - timedelta(minutes=i) for i in range(2000, 0, -1)]
    write_log(plain.cfg.log_dir, stamps)
    replay = [{"job": "b" * 32, "at": (NOW - timedelta(minutes=3000)).isoformat(), "level": "info", "message": "옛 줄"}]
    limit = logexport.MANIFEST_RESERVE + 300 * 1024
    _, data, manifest = logexport.build(plain.cfg, plain.store, range_key="all", extensions=job_logs(replay), now=NOW,
                                        max_bytes=limit)
    files = unzip(data)
    assert sum(len(v) for k, v in files.items() if k != "manifest.json") <= limit
    trimmed = manifest["trimmed"]
    assert trimmed["applied"] is True and trimmed["limit_bytes"] == limit
    assert trimmed["dropped_lines"][REPLAY_FILE] == 1 and trimmed["dropped_lines"][logexport.LOG_FILE] > 0
    kept = [json.loads(x)["ts"] for x in files[logexport.LOG_FILE].splitlines()]
    assert kept[-1] == stamps[-1].isoformat(timespec="milliseconds")  # 최근 줄은 남는다
    assert len(kept) + trimmed["dropped_lines"][logexport.LOG_FILE] == 2000
    # 실제 한도(64 MiB)
    assert logexport.MAX_RAW == 64 * 1024 * 1024


def test_second_export_is_refused_while_building(ex, monkeypatch):
    """한 번에 하나만 만든다. 만드는 동안 다른 요청(채널 목록)은 막히지 않는다."""
    gate, started = threading.Event(), threading.Event()
    real = logexport.build

    def slow(*args, **kwargs):
        started.set()
        gate.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(logexport, "build", slow)
    results = {}
    t = threading.Thread(target=lambda: results.setdefault("first", export(ex)))
    t.start()
    assert started.wait(10)
    second = export(ex)
    assert second.status_code == 409
    assert ex.client.get("/api/channels").status_code == 200  # 이벤트 루프는 막히지 않았다
    gate.set()
    t.join(20)
    assert results["first"].status_code == 200
    assert export(ex).status_code == 200  # 끝나면 다시 된다


def test_export_needs_login_csrf_and_is_refused_in_view_only(ex, tmp_path, server, tools):
    """로그인이 필요하고 CSRF를 거치며 보기 전용 모드에서는 거부한다."""
    assert web.access_level("POST", EXPORT) == web.LOGIN and ("POST", EXPORT, web.LOGIN) in web.ACCESS_RULES
    assert ex.client.post(EXPORT, json={}).status_code == 403  # CSRF 토큰 없음
    assert export(ex, range="2").status_code in (400, 422)
    ex.client.post("/api/logout", headers={"X-CSRF-Token": ex.csrf})
    assert ex.client.post(EXPORT, json={}, headers={"X-CSRF-Token": ex.csrf}).status_code in (401, 403)
    with ViewSite(tmp_path / "view", server, tools) as v:
        r = v.client.post(EXPORT, json={})
        assert r.status_code == 403 and r.json()["view_only"] is True


# ---- 문서 ----

def logging_extra_keys() -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for path in (ROOT / "live_recorder").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                    "debug", "info", "warning", "error", "exception", "critical", "log"):
                for kw in node.keywords:
                    if kw.arg == "extra":
                        assert isinstance(kw.value, ast.Dict), f"{path}: extra는 글자 그대로의 dict로 적는다(시험이 키를 읽는다)"
                        for key in kw.value.keys:
                            assert isinstance(key, ast.Constant), path
                            keys.setdefault(key.value, set()).add(path.name)
    return keys


def logging_events() -> set[str]:
    out = set()
    for path in (ROOT / "live_recorder").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "extra" and isinstance(node.value, ast.Dict):
                for k, v in zip(node.value.keys, node.value.values):
                    if isinstance(k, ast.Constant) and k.value == "event" and isinstance(v, ast.Constant):
                        out.add(v.value)
    return out


def test_every_logged_field_is_listed_in_the_doc_and_mask_rules():
    """로그에 쓰이는 필드 이름이 문서의 목록과 가리기 규칙에 모두 있다."""
    doc = DOC.read_text(encoding="utf-8")
    keys = logging_extra_keys()
    assert {"event", "channel_id", "session_id", "client"} <= set(keys)
    for key in set(keys) | set(logmask.FIELDS) | set(REPLAY_FIELDS) | {"exc_type"}:
        assert f"`{key}`" in doc, f"문서에 필드가 없다: {key}"
    for key in keys:
        assert key in logmask.FIELDS, f"가리기 규칙에 필드가 없다: {key}"
    for event in logging_events():
        assert f"`{event}`" in doc, f"문서에 이벤트가 없다: {event}"
    for name in [logexport.LOG_FILE, REPLAY_FILE, "manifest.json", "diagnostics/summary.json", "diagnostics/settings.json",
                 "diagnostics/sessions.json", "diagnostics/log_summary.json"]:
        assert f"`{name}`" in doc, name
