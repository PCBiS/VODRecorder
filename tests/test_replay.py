"""다시보기. 웹 API → 대기열 → 수신 프로세스 → 가짜 서버. 다시보기 취소."""

import json
import os
import time
from pathlib import Path

import pytest


from conftest import duration, probe
from conftest import REPLAY_EXTENSIONS
from live_recorder.platforms import credentials
from replay_fake import ADULT_NO, BROKEN_NO, DASH_NO, FAST_NO, SIG, FakeVod, build_media
from test_web import Web

AUT, SES = "AUT-replay-sentinel-0001", "SES-replay-sentinel-0002"
DASH = f"https://chzzk.naver.com/video/{DASH_NO}"
FAST = f"https://chzzk.naver.com/video/{FAST_NO}"
ADULT = f"https://chzzk.naver.com/video/{ADULT_NO}"
BROKEN = f"https://chzzk.naver.com/video/{BROKEN_NO}"


@pytest.fixture(scope="session")
def vod_media(tools, tmp_path_factory):
    out = tmp_path_factory.mktemp("vodmedia")
    build_media(tools[0], out)
    return out


@pytest.fixture
def rp(tmp_path, server, tools, vod_media):
    fake = FakeVod(server, vod_media)
    with Web(tmp_path, server, tools, extensions=REPLAY_EXTENSIONS) as w:
        w.fake = fake
        w.login()
        yield w


def analyze(w, urls):
    r = w.call("POST", "/api/replay/analyze", json={"urls": urls})
    assert r.status_code == 200, r.text
    ident = r.json()["id"]
    return w.wait(lambda: (lambda a: a if a["status"] in ("completed", "failed") else None)(
        w.client.get(f"/api/replay/analyses/{ident}").json()), timeout=60, what="분석")


def enqueue(w, analysis, items, key="request-key-0001", confirm=True):
    # 같은 영상을 여러 번 받는 시험이 많다. 중복 확인은 test_replay_list.py에서 따로 본다
    r = w.call("POST", "/api/replay/jobs", json={"analysis_id": analysis["id"], "items": items, "key": key, "confirm": confirm})
    assert r.status_code == 200, r.text
    return r.json()["jobs"]


def job(w, ident):
    return w.client.get(f"/api/replay/jobs/{ident}").json()


def wait_job(w, ident, statuses=("completed", "failed", "cancelled"), timeout=90):
    return w.wait(lambda: (lambda j: j if j["status"] in statuses else None)(job(w, ident)), timeout=timeout,
                  what=f"작업 {statuses}")


def replay_dir(w) -> Path:
    return w.h.root / "replay"


def visible(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if not p.name.startswith("."))


def test_analysis_items_and_errors(rp):
    """DASH·빠른 다시보기·성인(쿠키 없음)·없는 영상. 쿼리와 끝의 /를 뗀다."""
    a = analyze(rp, [DASH + "/?t=10", FAST, ADULT, "https://chzzk.naver.com/video/999"])
    assert a["status"] == "completed"
    dash, fast, adult, missing = a["items"]
    assert dash["url"] == DASH and dash["mode"] == "dash" and dash["title"] == "DASH 다시보기"
    assert dash["channel"] == "가짜채널" and dash["open_date"] == "2026-11-10 19:03:27" and dash["duration_seconds"] == 120
    assert dash["thumbnail"] == "https://video-phinf.pstatic.net/thumb.jpg" and dash["supports"] == {"section": True}
    # 같은 높이면 단일 파일을 남기고, 높이·대역폭 내림차순. 오디오는 빠진다
    assert [(q["id"], q["label"]) for q in dash["qualities"]] == [("PD_90P_160_100_24", "90p"), ("hls-uuid-0002", "72p (HLS)")]
    assert fast["mode"] == "hls" and fast["supports"] == {"section": False}
    assert [q["label"] for q in fast["qualities"]] == ["90p"]
    assert adult["error_code"] == "login_required" and "로그인" in adult["error"] and adult["qualities"] == []
    assert missing["error_code"] == "not_found"
    for item in a["items"]:  # 서명된 주소가 결과에 없다
        assert SIG not in json.dumps(item) and "hdntl" not in json.dumps(item) and "INKEY" not in json.dumps(item)
    # 한 요청에 10개까지. 치지직 주소만
    assert rp.call("POST", "/api/replay/analyze", json={"urls": [DASH] * 11}).status_code == 400
    for bad in ("http://chzzk.naver.com/video/1", "https://chzzk.naver.com/video/abc", "https://evil.example/video/1",
                "https://chzzk.naver.com/live/abc", "https://www.youtube.com/watch?v=aaaaaaaaaaa"):
        assert rp.call("POST", "/api/replay/analyze", json={"urls": [bad]}).status_code == 400, bad


def test_full_download_multiconnection(rp, tools):
    """여러 연결로 받고, 검증한 뒤 정해진 이름으로 둔다."""
    rp.call("PATCH", "/api/settings", json={"replay_connections": 4})
    a = analyze(rp, [DASH])
    jobs = enqueue(rp, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}])
    done = wait_job(rp, jobs[0]["id"])
    assert done["status"] == "completed" and done["progress"] == 100 and done["can_download"], done
    assert done["filename"] == "[261110_190327] 가짜채널 DASH 다시보기.mp4"
    out = replay_dir(rp) / done["filename"]
    assert abs(duration(tools, out) - 120) < 1.5
    assert out.read_bytes() == rp.fake.file_bytes  # 조각을 제자리에 맞게 썼다
    assert oct(out.stat().st_mode & 0o777) == "0o600"  # 수신 프로세스의 umask 077
    assert len({r for r in rp.fake.ranges if r != "bytes=0-0"}) >= 2  # 여러 범위로 나눠 받았다
    assert visible(replay_dir(rp)) == [done["filename"]] and list((replay_dir(rp) / ".work").iterdir()) == []
    logs = rp.client.get(f"/api/replay/jobs/{done['id']}/logs").json()["logs"]
    assert any("검증" in e["message"] for e in logs)
    r = rp.client.get(f"/api/replay/jobs/{done['id']}/file")
    assert r.status_code == 200 and r.content == rp.fake.file_bytes and "attachment" in r.headers["content-disposition"]
    # 같은 이름이 있으면 번호를 붙인다
    again = enqueue(rp, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}], key="request-key-0002")
    assert wait_job(rp, again[0]["id"])["filename"] == "[261110_190327] 가짜채널 DASH 다시보기 (1).mp4"
    # 같은 요청 키로 다시 보내면 새 작업을 만들지 않는다(멱등 키)
    assert enqueue(rp, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}], key="request-key-0002")[0]["id"] == again[0]["id"]


def test_section_from_single_file(rp, tools):
    """단일 파일의 구간은 ffmpeg 복사 모드로 받는다. 서명된 주소는 인자에 없다."""
    a = analyze(rp, [DASH])
    jobs = enqueue(rp, a, [{"url": DASH, "quality": "PD_90P_160_100_24", "section": "00:00:30~00:01:00"}])
    done = wait_job(rp, jobs[0]["id"])
    assert done["status"] == "completed", done
    assert abs(duration(tools, replay_dir(rp) / done["filename"]) - 30) < 2.5


def test_hls_type_section_inherits_signature(rp, tools):
    """HLS형 표현의 조각에는 재생목록 주소의 서명을 물려준다."""
    a = analyze(rp, [DASH])
    jobs = enqueue(rp, a, [{"url": DASH, "quality": "hls-uuid-0002", "section": "00:00:10~00:00:20"}])
    done = wait_job(rp, jobs[0]["id"])
    assert done["status"] == "completed", done
    assert abs(duration(tools, replay_dir(rp) / done["filename"]) - 10) < 2.5
    assert oct((replay_dir(rp) / done["filename"]).stat().st_mode & 0o777) == "0o600"  # ffmpeg도 umask를 물려받는다


def test_fast_replay_hls_and_no_section(rp, tools):
    """빠른 다시보기는 마스터 재생목록에서 화질을 고르고, 구간은 지원하지 않는다."""
    a = analyze(rp, [FAST])
    q = a["items"][0]["qualities"][0]["id"]
    r = rp.call("POST", "/api/replay/jobs", json={"analysis_id": a["id"], "key": "fast-section-1",
                                                   "items": [{"url": FAST, "quality": q, "section": "00:00:00~00:00:10"}]})
    assert r.status_code == 400
    jobs = enqueue(rp, a, [{"url": FAST, "quality": q}], key="fast-full-0001")
    done = wait_job(rp, jobs[0]["id"])
    assert done["status"] == "completed", done
    assert abs(duration(tools, replay_dir(rp) / done["filename"]) - 120) < 3
    assert probe(tools, replay_dir(rp) / done["filename"])["format"]["format_name"].startswith("mov")


def test_options_are_validated(rp):
    """분석 결과에 없는 화질, 형식이 틀린 구간, 끝이 시작보다 이른 구간, 영상보다 긴 구간."""
    a = analyze(rp, [DASH])
    for item in ({"url": DASH, "quality": "nope"}, {"url": DASH, "quality": "PD_90P_160_100_24", "section": "0:0:1~0:0:2"},
                 {"url": DASH, "quality": "PD_90P_160_100_24", "section": "00:01:00~00:00:30"},
                 {"url": DASH, "quality": "PD_90P_160_100_24", "section": "00:00:00~01:00:00"},
                 {"url": FAST, "quality": "PD_90P_160_100_24"}):
        r = rp.call("POST", "/api/replay/jobs", json={"analysis_id": a["id"], "items": [item], "key": "bad-options-1"})
        assert r.status_code == 400, item
    r = rp.call("POST", "/api/replay/jobs", json={"analysis_id": a["id"], "items": [{"url": DASH, "quality": "x", "split": 4}],
                                                   "key": "bad-options-2"})
    assert r.status_code == 422  # 정해진 키만 받는다


def _descendants(pid: int) -> set[int]:
    out: set[int] = set()
    try:
        tasks = list(Path(f"/proc/{pid}/task").iterdir())
    except OSError:
        return out
    for task in tasks:
        try:
            kids = [int(x) for x in (task / "children").read_text().split()]
        except OSError:
            continue
        for kid in kids:
            out.add(kid)
            out |= _descendants(kid)
    return out


def _worker_pids() -> set[int]:
    """이 시험 프로세스가 띄운 다시보기 수신 프로세스. 같은 기계에서 도는 다른 시험의 프로세스는 넣지 않는다."""
    out = set()
    for pid in _descendants(os.getpid()):
        try:
            if b"live_recorder.ext.downloads.worker" in Path(f"/proc/{pid}/cmdline").read_bytes():
                out.add(pid)
        except OSError:
            pass
    return out

def test_cancel_removes_temp_and_retry_completes(rp, tools):
    """다시보기 취소. 취소 뒤 임시 파일이 없고, 같은 작업을 다시 시도할 수 있다."""
    rp.fake.delay = 0.05
    a = analyze(rp, [DASH])
    jobs = enqueue(rp, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}])
    ident = jobs[0]["id"]
    work = replay_dir(rp) / ".work" / ident
    rp.wait(lambda: job(rp, ident)["downloaded_bytes"] > 100_000, what="받는 중")
    assert work.exists() and any(work.iterdir())
    pids = _worker_pids()
    assert pids
    r = rp.call("POST", f"/api/replay/jobs/{ident}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled", r.json()
    assert not work.exists()
    assert visible(replay_dir(rp)) == []
    time.sleep(0.3)
    assert not (_worker_pids() & pids)  # 수신 프로세스(그룹)가 끝났다
    logs = [e["message"] for e in rp.client.get(f"/api/replay/jobs/{ident}/logs").json()["logs"]]
    assert "취소를 요청했습니다." in logs and "작업을 취소했습니다." in logs
    # 재시도: 같은 작업 ID, 시도 횟수 2
    rp.fake.delay = 0
    r = rp.call("POST", f"/api/replay/jobs/{ident}/retry")
    assert r.status_code == 200 and r.json()["jobs"][0]["attempt"] == 2
    done = wait_job(rp, ident)
    assert done["status"] == "completed" and done["attempt"] == 2
    assert rp.call("POST", f"/api/replay/jobs/{ident}/retry").status_code == 409  # 완료한 작업은 재시도하지 않는다


def test_invalid_media_is_not_published(rp):
    """받은 파일이 영상이 아니면 실패하고 아무것도 남기지 않는다."""
    a = analyze(rp, [BROKEN])
    q = a["items"][0]["qualities"][0]["id"]
    jobs = enqueue(rp, a, [{"url": BROKEN, "quality": q}])
    done = wait_job(rp, jobs[0]["id"])
    assert done["status"] == "failed" and done["error_code"] == "invalid_media", done
    assert visible(replay_dir(rp)) == [] and list((replay_dir(rp) / ".work").iterdir()) == []


def test_media_denied_has_fixed_error(rp):
    a = analyze(rp, [DASH])
    rp.fake.media_status = 403
    jobs = enqueue(rp, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}])
    done = wait_job(rp, jobs[0]["id"])
    assert done["status"] == "failed" and done["error_code"] == "http_403", done
    assert SIG not in json.dumps(done)


def test_cookies_only_reach_the_api(rp, server):
    """쿠키는 서버 저장소에서만 읽고, 수신 프로세스의 인자·환경변수·작업 DB·로그에 없다."""
    credentials.save(rp.h.cfg, "chzzk", {"NID_AUT": AUT, "NID_SES": SES})
    rp.fake.delay = 0.03
    a = analyze(rp, [DASH])
    jobs = enqueue(rp, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}])
    ident = jobs[0]["id"]
    rp.wait(lambda: _worker_pids(), what="수신 프로세스")
    blobs = []
    for pid in _worker_pids():
        for name in ("cmdline", "environ"):
            try:
                blobs.append(Path(f"/proc/{pid}/{name}").read_bytes())
            except OSError:
                pass
    rp.fake.delay = 0
    wait_job(rp, ident)
    assert blobs
    for blob in blobs:
        assert AUT.encode() not in blob and SIG.encode() not in blob
    api = [c for p, c in server.calls if p.startswith("/api/service/v2/videos") or p.startswith("/api/neonplayer")]
    assert api and all(c == f"NID_AUT={AUT}; NID_SES={SES}" for c in api)
    assert rp.fake.media_cookies and all(c is None for c in rp.fake.media_cookies)
    for path in rp.h.data.rglob("*"):
        if path.is_file() and path.parent.name != "secrets":
            raw = path.read_bytes()
            assert AUT.encode() not in raw and SES.encode() not in raw and SIG.encode() not in raw, path
    for r in (rp.client.get("/api/replay/jobs"), rp.client.get(f"/api/replay/analyses/{a['id']}")):
        assert AUT not in r.text and SIG not in r.text


def test_settings_apply_immediately_and_refuse_while_busy(rp, tools):
    """저장 경로·최소 여유 공간은 저장 즉시 적용하고, 진행 중인 작업이 있으면 거부한다.
    사용 여부(replay_enabled)는 확장 켜고 끄기라 설정 API가 모르는 키다."""
    rp.fake.delay = 0.05
    a = analyze(rp, [DASH])
    jobs = enqueue(rp, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}])
    rp.wait(lambda: job(rp, jobs[0]["id"])["status"] == "running", what="진행")
    for change in ({"replay_dir": "other"}, {"replay_min_free_bytes": 1}):
        r = rp.call("PATCH", "/api/settings", json=change)
        assert r.status_code == 409, change
    assert rp.call("PATCH", "/api/settings", json={"replay_connections": 2}).status_code == 200  # 다음 작업부터 적용
    rp.call("POST", f"/api/replay/jobs/{jobs[0]['id']}/cancel")
    rp.fake.delay = 0
    # 조각 동시 받기 수: 기본 4, 1~16, 작업 프로세스에 넘긴다
    assert rp.client.get("/api/settings").json()["settings"]["replay_segment_connections"] == 4
    assert rp.call("PATCH", "/api/settings", json={"replay_segment_connections": 8}).status_code == 200
    assert rp.app.state.extensions.contexts["downloads"].service._extras()["segment_connections"] == 8
    for bad in ({"replay_dir": "../outside"}, {"replay_dir": "/etc"}, {"replay_connections": 0}, {"replay_connections": 17},
                {"replay_segment_connections": 0}, {"replay_segment_connections": 17}):
        assert rp.call("PATCH", "/api/settings", json=bad).status_code == 400, bad
    assert rp.call("PATCH", "/api/settings", json={"replay_dir": "다른곳"}).status_code == 200
    a2 = analyze(rp, [DASH])
    done = wait_job(rp, enqueue(rp, a2, [{"url": DASH, "quality": "PD_90P_160_100_24"}], key="after-move-01")[0]["id"])
    assert (rp.h.root / "다른곳" / done["filename"]).exists()
    r = rp.call("PATCH", "/api/settings", json={"replay_enabled": False})
    assert r.status_code == 400 and "replay_enabled" in r.json()["error"]  # 모르는 키
    # 저장 공간이 모자라면 시작하지 않고 기다린다. 멈춘 이유는 "여유 공간 부족"이다
    rp.call("PATCH", "/api/settings", json={"replay_min_free_bytes": 10**18})
    a3 = analyze(rp, [DASH])
    waiting = enqueue(rp, a3, [{"url": DASH, "quality": "PD_90P_160_100_24"}], key="no-space-0001")[0]["id"]
    time.sleep(1.5)
    assert job(rp, waiting)["status"] == "queued"
    gate = rp.client.get("/api/replay/status").json()["admission"]
    assert gate["open"] is False and gate["reason"] == "space" and gate["reason_label"] == "여유 공간 부족"


def test_restart_marks_unfinished_as_interrupted(tmp_path, server, tools, vod_media):
    """서버가 재시작되면 끝나지 않은 작업은 interrupted로 남고 자동으로 다시 받지 않는다."""
    fake = FakeVod(server, vod_media)
    fake.delay = 0.05
    w = Web(tmp_path, server, tools, extensions=REPLAY_EXTENSIONS)
    with w:
        w.login()
        a = analyze(w, [DASH])
        ident = enqueue(w, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}])[0]["id"]
        w.wait(lambda: job(w, ident)["downloaded_bytes"] > 50_000, what="받는 중")
    fake.delay = 0
    # 같은 데이터 폴더로 다시 띄운다
    from fastapi.testclient import TestClient

    from live_recorder import web

    app = web.create_app(w.h.cfg, w.h.timings)
    with TestClient(app, base_url="http://testserver") as c:
        c.post("/api/login", json={"username": "admin", "password": "correct horse battery"})
        j = c.get(f"/api/replay/jobs/{ident}").json()
        assert j["status"] == "interrupted" and j["can_download"] is False
        time.sleep(1.0)
        assert c.get(f"/api/replay/jobs/{ident}").json()["status"] == "interrupted"  # 자동으로 다시 받지 않는다
        assert not (w.h.root / "replay" / ".work" / ident).exists()


def test_retention_keeps_files(rp):
    """완료된 작업 내역은 1시간 뒤 정리하고 영상 파일은 남긴다."""
    from datetime import datetime, timedelta, timezone

    a = analyze(rp, [DASH])
    done = wait_job(rp, enqueue(rp, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}])[0]["id"])
    manager = rp.app.state.extensions.contexts["downloads"].service.manager
    manager.prune_completed(datetime.now(timezone.utc) + timedelta(minutes=59))
    assert rp.client.get(f"/api/replay/jobs/{done['id']}").status_code == 200
    manager.prune_completed(datetime.now(timezone.utc) + timedelta(hours=1, seconds=1))
    assert rp.client.get(f"/api/replay/jobs/{done['id']}").status_code == 404
    assert (replay_dir(rp) / done["filename"]).exists()


def test_replay_download_runs_at_low_priority(rp):
    """다시보기 수신 프로세스(와 그 자식 ffmpeg)도 낮은 우선순위로 돈다. 설정을 끄면 그대로다.

    이 작업의 프로세스만 읽는다. 기계 전체에서 이름으로 찾으면 같은 시각에 도는 다른 시험의 프로세스가 섞인다."""
    from live_recorder import lowprio

    manager = rp.app.state.extensions.contexts["downloads"].service.manager

    def job_priority(ident):
        """작업의 수신 프로세스가 실행기에서 exec한 뒤(받기 시작한 뒤)의 (nice, 입출력 등급)."""
        rp.wait(lambda: job(rp, ident)["downloaded_bytes"] > 0, timeout=30, what="받는 중")
        proc = rp.wait(lambda: manager.processes.get(ident), what="수신 프로세스")
        return os.getpriority(os.PRIO_PROCESS, proc.pid), lowprio.get_io_class(proc.pid)

    rp.fake.delay = 0.5  # 읽는 동안 수신 프로세스가 끝나지 않게 충분히 늦춘다(부하가 있어도)
    a = analyze(rp, [DASH])
    ident = enqueue(rp, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}])[0]["id"]
    assert job_priority(ident) == (19, lowprio.IOPRIO_CLASS_IDLE)
    rp.call("POST", f"/api/replay/jobs/{ident}/cancel")
    assert rp.call("PATCH", "/api/settings", json={"finalize_low_priority": False}).status_code == 200
    ident = enqueue(rp, a, [{"url": DASH, "quality": "PD_90P_160_100_24"}], key="normal-prio-1")[0]["id"]
    nice, io_class = job_priority(ident)
    assert nice == os.getpriority(os.PRIO_PROCESS, 0) and io_class != lowprio.IOPRIO_CLASS_IDLE
    rp.call("POST", f"/api/replay/jobs/{ident}/cancel")
    rp.fake.delay = 0

