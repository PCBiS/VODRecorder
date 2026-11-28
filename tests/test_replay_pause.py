"""다시보기 일시정지·이어받기·시간대·녹화 중 멈춤, 속도·남은 시간, 선택 다운로드의 분석 실패.

이어받기 시험은 가짜 서버만 쓴다. 이어받은 결과가 끊김 없이 처음부터 받은 결과와 같은 내용인지 바이트로 견준다.
시간대 시험은 시계를 바꿔 넣는다(실제로 기다리지 않는다).
"""

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from live_recorder.core import store as st
from live_recorder import web
from live_recorder.core.config import ConfigError, Settings
from live_recorder.ext.downloads import admission, hls_transport, resume, schedule, segments
from live_recorder.ext.downloads import media as vod  # 받는 방법은 downloads의 media
from live_recorder.ext.downloads.manager import ReplayManager, estimate

from conftest import CHANNEL, duration
from conftest import REPLAY_EXTENSIONS
from replay_fake import ADULT_NO, DASH_NO, FAST_NO, FAST_TOKEN, SIG, FakeVod
from test_replay import DASH, FAST, analyze, enqueue, job, replay_dir, vod_media, wait_job  # noqa: F401 (vod_media는 준비물)
from test_web import PASSWORD, USER, Web
from webui_files import index_html, all_js

KST = timezone(timedelta(hours=9))
FILE_Q = "PD_90P_160_100_24"
WEBUI = Path(__file__).resolve().parents[1] / "live_recorder" / "webui"


@pytest.fixture
def rp(tmp_path, server, tools, vod_media):
    fake = FakeVod(server, vod_media)
    with Web(tmp_path, server, tools, extensions=REPLAY_EXTENSIONS) as w:
        w.fake = fake
        w.login()
        yield w


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def logs(w, ident):
    return [e["message"] for e in w.client.get(f"/api/replay/jobs/{ident}/logs").json()["logs"]]


def pause(w, ident):
    r = w.call("POST", f"/api/replay/jobs/{ident}/pause")
    assert r.status_code == 200, r.text
    return r.json()


def resume_job(w, ident):
    r = w.call("POST", f"/api/replay/jobs/{ident}/resume")
    assert r.status_code == 200, r.text
    return r.json()


def work_of(w, ident) -> Path:
    return replay_dir(w) / ".work" / ident


def ranges_len(ranges):
    """Range 요청 목록의 바이트 합(받기 전 확인용 0-0과 64 KiB 이하의 견주기 요청은 뺀다)."""
    total = 0
    for r in ranges:
        a, b = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", r).groups())
        if b - a + 1 > resume_sample():
            total += b - a + 1
    return total


def resume_sample():
    return vod.RangeDownloader.SAMPLE


def start_slow_file_job(w, delay=0.2, key="pause-file-0001"):
    w.call("PATCH", "/api/settings", json={"replay_connections": 4})
    w.fake.delay = delay
    a = analyze(w, [DASH])
    ident = enqueue(w, a, [{"url": DASH, "quality": FILE_Q}], key=key)[0]["id"]
    w.wait(lambda: job(w, ident)["downloaded_bytes"] > 250_000, timeout=30, what="받는 중")
    # 받은 구간이 기록에 들어갈 때까지(1초마다 fsync 뒤 기록) 기다린다
    w.wait(lambda: (st_ := resume.load(work_of(w, ident))) and resume.covered(st_.get("done", [])) > 0, timeout=30, what="기록")
    return ident


# ---- 단일 파일 범위 이어받기 -------------------------------------------------------------

def test_file_pause_resume_continues_from_received_ranges(rp):
    """일시정지하면 부분 파일과 기록을 남기고, 재개하면 받지 않은 구간만 받는다. 결과는 원본과 바이트까지 같다."""
    ident = start_slow_file_job(rp)
    paused = pause(rp, ident)
    assert paused["status"] == "paused" and paused["pause_reason"] == "user" and paused["partial_bytes"] > 0, paused
    work = work_of(rp, ident)
    state = json.loads((work / "resume.json").read_text())
    assert state["kind"] == "range" and state["size"] == len(rp.fake.file_bytes) and state["variant"] == FILE_Q
    covered = resume.covered(state["done"])
    assert 0 < covered < state["size"]
    raw = (work / "resume.json").read_bytes()
    assert SIG.encode() not in raw and b"http" not in raw  # 기록에 주소·서명이 없다
    assert (work / "video.mp4").stat().st_size == state["size"]
    time.sleep(1.0)
    assert job(rp, ident)["status"] == "paused"  # 사용자가 멈춘 작업은 저절로 다시 시작하지 않는다
    before = len(rp.fake.ranges)
    rp.fake.delay = 0
    resumed = resume_job(rp, ident)
    assert resumed["status"] in ("paused", "running", "completed")
    done = wait_job(rp, ident)
    assert done["status"] == "completed", done
    out = replay_dir(rp) / done["filename"]
    assert out.read_bytes() == rp.fake.file_bytes  # 끊김 없이 받은 것과 같다
    # 이어받을 때 받은 양은 남은 구간뿐이다(기록에 있는 구간을 다시 받지 않는다)
    assert ranges_len(rp.fake.ranges[before:]) == state["size"] - covered
    assert any("다음부터 이어받습니다" in m for m in logs(rp, ident))
    assert "이어받습니다" in done["resume_note"]
    assert not work.exists()  # 완료하면 작업 폴더를 지운다


def test_resume_restarts_when_partial_does_not_match(rp):
    """부분 파일이 서버 내용과 맞지 않으면 처음부터 다시 받고 그 사실을 작업에 적는다. 완료 검증은 그대로 거친다."""
    ident = start_slow_file_job(rp, key="mismatch-0001")
    pause(rp, ident)
    part = work_of(rp, ident) / "video.mp4"
    with open(part, "r+b") as f:  # 받아 둔 첫 구간의 앞부분을 망가뜨린다
        f.write(b"\xff" * 4096)
    rp.fake.delay = 0
    resume_job(rp, ident)
    done = wait_job(rp, ident)
    assert done["status"] == "completed", done
    assert (replay_dir(rp) / done["filename"]).read_bytes() == rp.fake.file_bytes
    assert done["resume_note"] == "부분 파일이 서버 내용과 맞지 않아 처음부터 다시 받습니다."
    assert any("검증" in m for m in logs(rp, ident))


def test_resume_restarts_when_server_has_no_ranges(rp):
    """범위 요청을 받지 않는 서버는 이어받을 수 없다. 처음부터 받고 적는다."""
    rp.fake.no_range = True
    rp.fake.delay = 0.2
    a = analyze(rp, [DASH])
    ident = enqueue(rp, a, [{"url": DASH, "quality": FILE_Q}], key="norange-0001")[0]["id"]
    rp.wait(lambda: job(rp, ident)["downloaded_bytes"] > 150_000, timeout=30, what="받는 중")
    pause(rp, ident)
    assert not (work_of(rp, ident) / "resume.json").exists()
    rp.fake.delay = 0
    resume_job(rp, ident)
    done = wait_job(rp, ident)
    assert done["status"] == "completed", done
    assert (replay_dir(rp) / done["filename"]).read_bytes() == rp.fake.file_bytes
    assert done["resume_note"] == "영상 서버가 범위 요청을 받지 않아 처음부터 다시 받습니다."


def test_section_job_cannot_resume_and_restarts(rp, tools):
    """ffmpeg 구간 받기는 이어받을 수 없는 방식이다. 처음부터 다시 받고 적는다."""
    rp.fake.delay = 0.3
    a = analyze(rp, [DASH])
    ident = enqueue(rp, a, [{"url": DASH, "quality": FILE_Q, "section": "00:00:30~00:01:00"}], key="section-0001")[0]["id"]
    rp.wait(lambda: (work_of(rp, ident) / "video.mp4").exists(), timeout=30, what="ffmpeg 출력")
    pause(rp, ident)
    rp.fake.delay = 0
    resume_job(rp, ident)
    done = wait_job(rp, ident)
    assert done["status"] == "completed", done
    assert done["resume_note"] == "구간 받기는 이어받을 수 없어 처음부터 다시 받습니다."
    assert abs(duration(tools, replay_dir(rp) / done["filename"]) - 30) < 2.5


def test_range_downloader_resumes_in_process(server, vod_media, tmp_path, monkeypatch):
    """수신기 단위: 중간에 멈춘 뒤 새 수신기로 다시 돌리면 받지 않은 구간만 받고 결과가 같다."""
    fake = FakeVod(server, vod_media)
    monkeypatch.setattr(vod, "ALLOW_LOCAL_HTTP", True)
    url = f"{fake.base}/vod/VID100/file.mp4?_lsu_sa_={SIG}"
    work = tmp_path / "w"
    work.mkdir()
    out = work / "video.mp4"

    def stop_after(amount, total):
        if amount > 300_000:
            raise RuntimeError("멈춤")

    fake.delay = 0.1
    first = vod.RangeDownloader(url, str(out), 2, variant_id="q", on_bytes=stop_after)
    first.part = first.MIN_PART
    with pytest.raises(Exception):
        first.run()
    covered = resume.covered(resume.load(work)["done"])
    assert 0 < covered < len(fake.file_bytes)
    fake.delay = 0
    events = []
    before = len(fake.ranges)
    vod.RangeDownloader(url, str(out), 2, variant_id="q", on_event=lambda n, **d: events.append((n, d))).run()
    assert out.read_bytes() == fake.file_bytes
    assert events == [("resume", {"mode": "range", "bytes": covered, "total": len(fake.file_bytes)})]
    assert ranges_len(fake.ranges[before:]) == len(fake.file_bytes) - covered
    # 다른 화질이었으면 이어받지 않는다
    events.clear()
    vod.RangeDownloader(url, str(out), 2, variant_id="other", on_event=lambda n, **d: events.append((n, d))).run()
    assert events == [("resume", {"mode": "restart", "reason": "changed"})] and out.read_bytes() == fake.file_bytes


# ---- HLS 조각 이어받기 -------------------------------------------------------------------

def test_hls_pause_resume_continues_after_received_segments(rp):
    """빠른 다시보기(HLS): 받은 조각 다음부터 받는다. 결과는 끊김 없이 받은 파일과 같다."""
    a = analyze(rp, [FAST])
    q = a["items"][0]["qualities"][0]["id"]
    baseline = wait_job(rp, enqueue(rp, a, [{"url": FAST, "quality": q}], key="fast-base-001")[0]["id"])
    assert baseline["status"] == "completed", baseline
    total = len([n for n in rp.fake.segments if n.endswith(".ts")])
    assert total == 60
    rp.fake.segments.clear()
    rp.fake.delay = 0.15
    ident = enqueue(rp, a, [{"url": FAST, "quality": q}], key="fast-pause-01")[0]["id"]
    seg_dir = work_of(rp, ident) / "seg"
    rp.wait(lambda: seg_dir.is_dir() and len([p for p in seg_dir.iterdir() if p.suffix == ".ts"]) >= 12, timeout=30, what="조각")
    paused = pause(rp, ident)
    assert paused["status"] == "paused" and paused["partial_bytes"] > 0
    have = sorted(p.name for p in seg_dir.iterdir() if p.suffix == ".ts")
    assert all(segments.NAME.fullmatch(n) for n in have)
    state = json.loads((work_of(rp, ident) / "resume.json").read_text())
    assert state["kind"] == "hls" and state["count"] == 60 and FAST_TOKEN.split("~")[1] not in json.dumps(state)
    served = len(rp.fake.segments)
    rp.fake.delay = 0
    resume_job(rp, ident)
    done = wait_job(rp, ident)
    assert done["status"] == "completed", done
    after = rp.fake.segments[served:]
    # 받지 않은 조각과, 받아 둔 마지막 조각을 견주려고 한 번 더 받은 것뿐이다
    assert len(after) == 60 - len(have) + 1, (len(after), len(have))
    assert sha(replay_dir(rp) / done["filename"]) == sha(replay_dir(rp) / baseline["filename"])
    assert re.search(r"받아 둔 조각 \d+개", done["resume_note"])


def test_hls_segments_with_init_map_resume_in_process(server, vod_media, tmp_path, monkeypatch, tools):
    """수신기 단위(fMP4, EXT-X-MAP): 끊긴 뒤 이어받은 결과가 끊김 없이 받은 결과와 같다. 재생목록이 바뀌면 처음부터 받는다."""
    fake = FakeVod(server, vod_media)
    monkeypatch.setattr(vod, "ALLOW_LOCAL_HTTP", True)
    monkeypatch.setattr(hls_transport, "ALLOW_LOCAL_HTTP", True)
    url = f"{fake.base}/vod/fm4/{FAST_TOKEN}/media.m3u8"

    def run(work, **kwargs):
        work.mkdir(exist_ok=True)
        d = segments.SegmentDownloader(url, work, tools[0], None, False, "q1", vod.media_headers(), check_url=vod._check_url, **kwargs)
        d.run(str(work / "video.mp4"))
        return d

    base = tmp_path / "base"
    run(base)
    assert (base / "seg" / "map-000.mp4").exists()

    def stop(value):
        if value and value > 40:
            raise RuntimeError("멈춤")

    work = tmp_path / "w"
    with pytest.raises(RuntimeError):
        run(work, on_progress=stop)
    have = [p for p in (work / "seg").iterdir() if p.suffix == ".m4s"]
    assert 0 < len(have) < 60
    events = []
    second = run(work, on_event=lambda n, **d: events.append((n, d)))
    assert events[0][0] == "resume" and events[0][1]["mode"] == "segments" and events[0][1]["count"] == len(have)
    estimates = [d["estimate"] for n, d in events if n == "space"]  # 영상 크기 추정을 알린다
    assert estimates and all(d.get("mode") == "hls" for n, d in events if n == "space")
    real = sum(p.stat().st_size for p in (work / "seg").iterdir() if p.suffix == ".m4s")
    assert 0.5 * real < estimates[-1] < 1.5 * real
    assert second.fetched == 60 - len(have)
    assert sha(work / "video.mp4") == sha(base / "video.mp4")
    assert abs(duration(tools, work / "video.mp4") - 120) < 3
    # 같은 작업 폴더인데 화질이 다르면(기록과 맞지 않으면) 처음부터 받는다
    events.clear()
    (work / "video.mp4").unlink()
    third = segments.SegmentDownloader(url, work, tools[0], None, False, "q2", vod.media_headers(), check_url=vod._check_url,
                                       on_event=lambda n, **d: events.append((n, d)))
    third.run(str(work / "video.mp4"))
    assert [e for e in events if e[0] == "resume"] == [("resume", {"mode": "restart", "reason": "changed"})] and third.fetched == 60


def test_hls_segments_parallel_connections_match_sequential(server, vod_media, tmp_path, monkeypatch, tools):
    """조각을 여러 연결로 함께 받아도 결과가 하나씩 받은 것과 같다. 끊긴 뒤(받은 조각이 띄엄띄엄 있어도) 이어받는다.
    한 조각이 실패하면 전체가 실패하고, 그때까지 받은 조각은 남는다."""
    fake = FakeVod(server, vod_media)
    monkeypatch.setattr(vod, "ALLOW_LOCAL_HTTP", True)
    monkeypatch.setattr(hls_transport, "ALLOW_LOCAL_HTTP", True)
    url = f"{fake.base}/vod/fm4/{FAST_TOKEN}/media.m3u8"

    def make(work, **kwargs):
        work.mkdir(exist_ok=True)
        return segments.SegmentDownloader(url, work, tools[0], None, False, "q1", vod.media_headers(), check_url=vod._check_url, **kwargs)

    base = tmp_path / "base"
    make(base).run(str(base / "video.mp4"))
    par = tmp_path / "par"
    seen = []
    d = make(par, connections=8, on_progress=seen.append)
    d.run(str(par / "video.mp4"))
    assert d.fetched == 60 and sha(par / "video.mp4") == sha(base / "video.mp4")
    assert seen == sorted(seen) and len(seen) == 60  # 진행률은 부른 스레드에서, 줄어들지 않게 알린다

    def stop(value):
        if value and value > 40:
            raise RuntimeError("멈춤")

    work = tmp_path / "w"
    with pytest.raises(RuntimeError):
        make(work, connections=8, on_progress=stop).run(str(work / "video.mp4"))
    have = [p for p in (work / "seg").iterdir() if p.suffix == ".m4s"]
    assert 0 < len(have) < 60
    second = make(work, connections=8)
    second.run(str(work / "video.mp4"))
    assert second.fetched == 60 - len(have) and sha(work / "video.mp4") == sha(base / "video.mp4")

    # 조각 하나가 계속 실패하면 전체가 실패한다
    bad = tmp_path / "bad"
    broken = make(bad, connections=8, attempts=1)
    real = broken._fetch_file

    def flaky(client, uri, index, ext):
        if index == 30:
            raise segments.ReplayFailure("not_found")
        return real(client, uri, index, ext)

    broken._fetch_file = flaky
    with pytest.raises(segments.ReplayFailure) as e:
        broken.run(str(bad / "video.mp4"))
    assert e.value.code == "not_found" and not (bad / "video.mp4").exists()
    assert 0 < len([p for p in (bad / "seg").iterdir() if p.suffix == ".m4s"]) < 60


def test_playlist_parsing_rules():
    """조각 목록: 구간과 겹치는 조각만 고른다. 바이트 범위 조각은 이 방식으로 받지 않는다. 지문은 서명을 뺀다."""
    def playlist(query):
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:5"]
        for i in range(10):
            lines += ["#EXTINF:2.000,", f"https://a.pstatic.net/v/seg{i}.ts?{query}"]
        return ("\n".join(lines + ["#EXT-X-ENDLIST"]) + "\n").encode()

    p = segments.Playlist(playlist("_lsu_sa_=A"))
    assert len(p.segments) == 10 and p.sequence == 5
    assert p.fingerprint == segments.Playlist(playlist("_lsu_sa_=B")).fingerprint  # 서명이 바뀌어도 같은 목록
    assert p.select((5.0, 9.0)) == ([2, 3, 4], 1.0)
    assert p.select(None) == (list(range(10)), 0.0)
    with pytest.raises(Exception):
        p.select((100.0, 110.0))
    with pytest.raises(segments.Unsupported):
        segments.Playlist(b"#EXTM3U\n#EXTINF:2,\n#EXT-X-BYTERANGE:100@0\nhttps://a.pstatic.net/a.ts\n#EXT-X-ENDLIST\n")


# ---- 전체 일시정지·재시작 뒤 유지 ---------------------------------------------------------

def test_global_pause_holds_queue_and_survives_restart(tmp_path, server, tools, vod_media):
    """전체 일시정지: 받는 중인 작업을 멈추고 새 작업을 시작하지 않는다. 재시작해도 스위치와 일시정지가 남고,
    끄면 대기열 순서대로 이어받는다. 사용자가 멈춘 작업은 그대로 둔다."""
    fake = FakeVod(server, vod_media)
    w = Web(tmp_path, server, tools, extensions=REPLAY_EXTENSIONS)
    with w:
        w.fake = fake
        w.login()
        first = start_slow_file_job(w, key="global-0001")
        a = analyze(w, [DASH])
        second = enqueue(w, a, [{"url": DASH, "quality": FILE_Q}], key="global-0002")[0]["id"]
        mine = enqueue(w, a, [{"url": DASH, "quality": FILE_Q}], key="global-0003")[0]["id"]
        assert pause(w, mine)["status"] == "paused"  # 대기 중이면 바로 일시정지
        r = w.call("PUT", "/api/replay/control", json={"paused": True})
        assert r.status_code == 200 and r.json()["admission"]["reason"] == "global"
        held = w.wait(lambda: (j := job(w, first))["status"] == "paused" and j, timeout=15, what="전체 정지")
        assert held["pause_reason"] == "global" and held["partial_bytes"] > 0
        time.sleep(1.2)
        assert job(w, second)["status"] == "queued"  # 새 작업을 시작하지 않는다
        status = w.client.get("/api/replay/status").json()
        assert status["admission"]["open"] is False and status["admission"]["paused"] is True and status["paused_jobs"] == 2
        attention = w.client.get("/api/replay/jobs", params={"status": "attention"}).json()
        assert {first, second, mine} <= {j["id"] for j in attention["jobs"]}
    fake.delay = 0
    app = web.create_app(w.h.cfg, w.h.timings)  # 같은 데이터 폴더로 다시 띄운다
    with TestClient(app, base_url="http://testserver") as c:
        c.post("/api/login", json={"username": USER, "password": PASSWORD})
        csrf = c.get("/api/session").json()["csrf"]
        assert c.get("/api/replay/status").json()["admission"]["paused"] is True  # 스위치가 남는다
        j1 = c.get(f"/api/replay/jobs/{first}").json()
        assert j1["status"] == "paused" and j1["pause_reason"] == "global"  # interrupted가 아니다
        assert (w.h.root / "replay" / ".work" / first / "resume.json").exists()  # 부분 파일이 남는다
        assert c.get(f"/api/replay/jobs/{second}").json()["status"] == "interrupted"  # 대기 중 재시작은 그대로
        assert c.get(f"/api/replay/jobs/{mine}").json()["status"] == "paused"
        r = c.put("/api/replay/control", json={"paused": False}, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 200 and r.json()["admission"]["open"] is True
        deadline = time.monotonic() + 60
        while c.get(f"/api/replay/jobs/{first}").json()["status"] != "completed" and time.monotonic() < deadline:
            time.sleep(0.1)
        done = c.get(f"/api/replay/jobs/{first}").json()
        assert done["status"] == "completed", done
        assert (w.h.root / "replay" / done["filename"]).read_bytes() == fake.file_bytes
        assert "이어받습니다" in (done["resume_note"] or "")
        time.sleep(1.0)
        assert c.get(f"/api/replay/jobs/{mine}").json()["status"] == "paused"  # 사용자가 멈춘 작업은 남는다


def test_pause_and_resume_rules(rp):
    """끝난 작업은 일시정지할 수 없고, 일시정지하지 않은 작업은 재개할 수 없다. 대기 중 일시정지는 부분 파일이 없다."""
    a = analyze(rp, [DASH])
    done = wait_job(rp, enqueue(rp, a, [{"url": DASH, "quality": FILE_Q}])[0]["id"])
    assert rp.call("POST", f"/api/replay/jobs/{done['id']}/pause").status_code == 409
    assert rp.call("POST", f"/api/replay/jobs/{done['id']}/resume").status_code == 409
    assert rp.call("POST", f"/api/replay/jobs/{'0' * 32}/pause").status_code == 404
    rp.call("PUT", "/api/replay/control", json={"paused": True})
    ident = enqueue(rp, a, [{"url": DASH, "quality": FILE_Q}], key="rules-0002")[0]["id"]
    assert pause(rp, ident)["pause_reason"] == "user"
    rp.call("PUT", "/api/replay/control", json={"paused": False})
    assert resume_job(rp, ident)["status"] == "queued"  # 부분 파일이 없으면 그냥 대기열로
    assert wait_job(rp, ident)["status"] == "completed"


# ---- 부분 파일 잠금과 취소 -------------------------------------------------------------------

def test_partial_files_are_hidden_locked_and_removed_on_cancel(rp):
    ident = start_slow_file_job(rp, key="lock-0001")
    pause(rp, ident)
    work = work_of(rp, ident)
    assert work.parent.name == ".work" and (work / "video.mp4").exists()
    root = str(rp.h.root)
    assert rp.call("PATCH", "/api/settings", json={"fm_enabled": True, "fm_roots": [root]}).status_code == 200
    time.sleep(1.1)
    top = next(r["key"] for r in rp.client.get("/api/files/roots").json()["roots"] if r["label"] == root)
    listing = rp.client.get("/api/files/list", params={"root": top, "path": "replay"}).json()
    assert [e["name"] for e in listing["entries"]] == []  # 숨김 이름이라 보이지 않는다
    for path in ("replay", "replay/.work", f"replay/.work/{ident}", f"replay/.work/{ident}/video.mp4"):
        r = rp.call("POST", "/api/files/delete", json={"root": top, "paths": [path], "mode": "permanent"})
        assert r.status_code == 423, (path, r.text)
    assert rp.call("POST", "/api/files/rename", json={"root": top, "path": "replay", "new_name": "x"}).status_code == 423
    r = rp.call("POST", f"/api/replay/jobs/{ident}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert not work.exists()  # 취소하면 부분 파일을 지운다
    assert ident not in rp.app.state.extensions.contexts["downloads"].service.manager.held


# ---- 시간대 규칙 --------------------------------------------------------------------------

def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=KST)


def test_window_rules_midnight_and_weekdays():
    """2026-11-13은 금요일이다. 금 23:00–02:00은 토 02:00까지 열리고, 그 구간은 금요일에 속한다."""
    rules = schedule.parse_rules([{"days": [4], "start": "23:00", "end": "02:00"}])
    assert schedule.window_state(rules, at(2026, 11, 13, 22, 59)) == (False, None, at(2026, 11, 13, 23, 0))
    s = schedule.window_state(rules, at(2026, 11, 14, 1, 30))
    assert s.open and s.closes_at == at(2026, 11, 14, 2, 0)
    nxt = schedule.window_state(rules, at(2026, 11, 14, 2, 0))
    assert not nxt.open and nxt.next_open_at == at(2026, 11, 20, 23, 0)  # 다음 금요일
    # 목요일 밤에는 열리지 않는다(구간은 시작한 요일에 속한다)
    assert not schedule.window_state(rules, at(2026, 11, 13, 1, 0)).open
    # 평일 09:00–19:00. UTC 시각으로 줘도 서울 시각으로 따진다
    weekdays = schedule.parse_rules([{"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "19:00"}])
    assert schedule.window_state(weekdays, datetime(2026, 11, 13, 0, 30, tzinfo=timezone.utc)).open  # 금 09:30 KST
    sat = schedule.window_state(weekdays, at(2026, 11, 14, 10, 0))
    assert not sat.open and sat.next_open_at == at(2026, 11, 16, 9, 0)  # 월요일
    # 겹치는 규칙은 합친다
    merged = schedule.parse_rules([{"days": [4], "start": "18:00", "end": "23:30"}, {"days": [4], "start": "23:00", "end": "01:00"}])
    assert schedule.window_state(merged, at(2026, 11, 13, 20, 0)).closes_at == at(2026, 11, 14, 1, 0)
    assert schedule.window_state([], at(2026, 11, 13, 12, 0)) == (False, None, None)
    for bad in ([{"days": [], "start": "01:00", "end": "02:00"}], [{"days": [7], "start": "01:00", "end": "02:00"}],
                [{"days": [1], "start": "01:00", "end": "01:00"}], [{"days": [1], "start": "1:00", "end": "02:00"}],
                [{"days": [1], "start": "01:00", "end": "24:00"}], [{"days": [1], "start": "01:00", "end": "02:00", "x": 1}],
                [{"days": [1], "start": "01:00", "end": "02:00"}] * 8, {"days": [1]}):
        with pytest.raises(ValueError):
            schedule.parse_rules(bad)


def settings(**kw):
    # 다운로드 설정은 downloads 확장의 설정이다(기본값은 SettingSpec)
    from types import SimpleNamespace

    from live_recorder.ext.downloads.extension import SETTINGS

    return SimpleNamespace(**{**{k: spec.default for k, spec in SETTINGS.items()}, **kw})


def test_admission_reasons_and_end_action():
    rules = ({"days": [0, 1, 2, 3, 4, 5, 6], "start": "09:00", "end": "19:00"},)
    inside, outside = at(2026, 11, 13, 10, 0), at(2026, 11, 13, 20, 0)
    s = admission.evaluate(settings(), outside)
    assert s["open"] and not s["suspend"] and s["reason"] is None
    s = admission.evaluate(settings(replay_window_enabled=True, replay_window_rules=rules), outside)
    assert s["open"] is False and s["reason"] == "window" and s["suspend"] is True  # 기본: 멈추고 다음 시간대에 이어받기
    assert s["next_open_at"] == at(2026, 11, 14, 9, 0).isoformat() and s["reason_label"] == "다운로드 시간대 밖"
    s = admission.evaluate(settings(replay_window_enabled=True, replay_window_rules=rules, replay_window_end_action="finish"), outside)
    assert s["open"] is False and s["suspend"] is False  # 끝까지 받기: 새 작업만 시작하지 않는다
    assert admission.evaluate(settings(replay_window_enabled=True, replay_window_rules=rules), inside)["open"]
    # 녹화 중(기본 켜짐): 녹화나 마무리가 하나라도 있으면 멈춘다. 끄면 보지 않는다
    for act in ({"recording": 1, "finalizing": 0}, {"recording": 0, "finalizing": 1}):
        s = admission.evaluate(settings(), inside, act)
        assert s["reason"] == "recording" and s["suspend"]
    assert admission.evaluate(settings(replay_pause_while_recording=False), inside, {"recording": 1, "finalizing": 1})["open"]
    # 우선순위: 전체 → 여유 공간 → 녹화 → 시간대
    s = admission.evaluate(settings(replay_paused=True, replay_window_enabled=True, replay_window_rules=rules), outside,
                           {"recording": 1, "finalizing": 0})
    assert s["reasons"] == ["global", "recording", "window"] and s["reason"] == "global"
    assert admission.with_space(s, True)["reasons"] == ["global", "space", "recording", "window"]
    finish = admission.evaluate(settings(replay_window_enabled=True, replay_window_rules=rules, replay_window_end_action="finish"), outside)
    spaced = admission.with_space(finish, True)
    assert spaced["reason"] == "space" and spaced["suspend"] is True and spaced["reason_label"] == "여유 공간 부족"


def test_end_action_per_rule_wins_over_the_global_setting():
    """규칙마다 둔 끝 동작이 전체 설정보다 먼저다. 없는 규칙은 전체 설정을 따른다. 같은 시각에 끝나면 멈춤이 이긴다."""
    every = [0, 1, 2, 3, 4, 5, 6]
    day = {"days": every, "start": "09:00", "end": "19:00", "end_action": "finish"}
    night = {"days": every, "start": "22:00", "end": "02:00", "end_action": "pause"}
    plain = {"days": every, "start": "04:00", "end": "05:00"}
    rules = (day, night, plain)
    assert schedule.rules_to_json(schedule.parse_rules(list(rules))) == list(rules)  # 끝 동작이 없는 규칙은 그대로 없다

    def state(when, default="pause", use=rules):
        return admission.evaluate(settings(replay_window_enabled=True, replay_window_rules=use, replay_window_end_action=default), when)

    s = state(at(2026, 11, 13, 20, 0))  # 낮 규칙(끝까지 받기)이 끝난 뒤: 전체 설정이 멈춤이어도 멈추지 않는다
    assert s["reason"] == "window" and s["suspend"] is False and s["end_action"] == "finish"
    s = state(at(2026, 11, 14, 3, 0), default="finish")  # 밤 규칙(멈춤)이 끝난 뒤: 전체 설정이 끝까지여도 멈춘다
    assert s["reason"] == "window" and s["suspend"] is True and s["end_action"] == "pause"
    assert state(at(2026, 11, 14, 6, 0), default="finish")["suspend"] is False  # 끝 동작이 없는 규칙: 전체 설정
    assert state(at(2026, 11, 14, 6, 0), default="pause")["suspend"] is True
    assert state(at(2026, 11, 13, 10, 0))["open"] and state(at(2026, 11, 13, 10, 0))["end_action"] == "finish"
    both = ({**day, "end_action": "finish"}, {"days": every, "start": "12:00", "end": "19:00", "end_action": "pause"})
    assert state(at(2026, 11, 13, 20, 0), default="finish", use=both)["suspend"] is True
    for bad in ({**day, "end_action": "stop"}, {**day, "extra": 1}):
        try:
            schedule.parse_rules([bad])
        except ValueError:
            continue
        raise AssertionError(bad)


def test_channel_rules_win_over_the_global_rules():
    """채널을 지정한 규칙: 그 채널의 작업은 자기 규칙만 따른다(시간대 사용 설정과 상관없이). 다른 작업은 전체 규칙을 따른다."""
    every = [0, 1, 2, 3, 4, 5, 6]
    day = {"days": every, "start": "09:00", "end": "19:00"}
    own = {"days": every, "start": "01:00", "end": "08:00", "end_action": "finish", "target": "chzzk:" + "a" * 32}
    rules = (day, own)
    mine, other = "chzzk:" + "a" * 32, "chzzk:" + "b" * 32
    assert schedule.rules_to_json(schedule.parse_rules(list(rules))) == list(rules)
    assert schedule.target_of("chzzk", "a" * 32) == mine and schedule.target_of("", "x") is None

    def state(when, target=None, enabled=True):
        return admission.evaluate(settings(replay_window_enabled=enabled, replay_window_rules=rules), when, target=target)

    noon, night = at(2026, 11, 13, 12, 0), at(2026, 11, 13, 3, 0)
    assert state(noon)["open"] and state(noon)["window_scope"] == "all" and not state(night)["open"]
    s = state(noon, mine)  # 낮: 전체 규칙으로는 열려 있지만 이 채널은 자기 규칙(새벽)만 본다
    assert s["reason"] == "window" and s["window_scope"] == "channel" and s["suspend"] is False and s["end_action"] == "finish"
    assert s["next_open_at"] == at(2026, 11, 14, 1, 0).isoformat()
    assert state(night, mine)["open"]  # 새벽: 전체 규칙으로는 닫혀 있지만 이 채널은 열려 있다
    assert state(noon, other)["open"] and not state(night, other)["open"]  # 자기 규칙이 없는 채널은 전체 규칙
    # 시간대 사용을 꺼도 채널의 자기 규칙은 따른다. 다른 작업은 언제든 받는다
    assert state(noon, mine, enabled=False)["reason"] == "window" and state(night, mine, enabled=False)["open"]
    assert state(night, other, enabled=False)["open"] and state(night, enabled=False)["window_scope"] is None
    for bad in ({**day, "target": "no-colon"}, {**day, "target": "chzzk:a b"}, {**day, "target": 3}):
        try:
            schedule.parse_rules([bad])
        except ValueError:
            continue
        raise AssertionError(bad)


def test_window_settings_are_validated_and_kept(rp):
    """시간대 설정의 검증과 유지. downloads 확장의 설정이라 설정 API로 바꾸고 확장 설정으로 남는다."""
    from live_recorder.core import extensions

    def patch(body):
        return rp.call("PATCH", "/api/settings", json=body)

    assert patch({"replay_window_enabled": True}).status_code == 400  # 규칙 없이 켤 수 없다
    for bad in ({"replay_window_rules": [{"days": [1], "start": "10:00", "end": "10:00"}]},
                {"replay_window_end_action": "stop"}, {"replay_paused": "maybe"}):
        assert patch(bad).status_code == 400, bad
    assert patch({"replay_window_rules": [{"days": [3, 1], "start": "23:00", "end": "02:00"}],
                  "replay_window_enabled": True, "replay_window_end_action": "finish", "replay_paused": True,
                  "replay_pause_while_recording": False}).status_code == 200
    store = rp.h.store  # 저장소에 남는다(재시작 뒤에도)
    get = lambda key: extensions.get_setting(store, "downloads", key)  # noqa: E731
    assert get("replay_window_rules") == ({"days": [1, 3], "start": "23:00", "end": "02:00"},)
    assert get("replay_window_enabled") and get("replay_window_end_action") == "finish" and get("replay_paused")
    assert get("replay_pause_while_recording") is False
    assert patch({"replay_window_rules": []}).status_code == 400  # 켜 둔 채 규칙을 모두 지울 수 없다
    assert rp.client.get("/api/settings").json()["settings"]["replay_window_end_action"] == "finish"


def test_window_gate_with_injected_clock(rp):
    """시간대 밖이면 시작하지 않고, 열리면 시작한다. 끝날 때 멈춤이면 일시정지했다가 다음 시간대에 이어받는다."""
    service = rp.app.state.extensions.contexts["downloads"].service
    now = {"t": at(2026, 11, 13, 20, 0)}
    service.clock = lambda: now["t"]
    r = rp.call("PATCH", "/api/settings", json={"replay_window_rules": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "09:00", "end": "19:00"}],
                                                 "replay_window_enabled": True, "replay_connections": 4})
    assert r.status_code == 200, r.text
    a = analyze(rp, [DASH])
    ident = enqueue(rp, a, [{"url": DASH, "quality": FILE_Q}], key="window-0001")[0]["id"]
    time.sleep(1.5)
    assert job(rp, ident)["status"] == "queued"
    gate = rp.client.get("/api/replay/status").json()["admission"]
    assert gate["reason"] == "window" and gate["next_open_at"] == at(2026, 11, 14, 9, 0).isoformat()
    rp.fake.delay = 0.2
    now["t"] = at(2026, 11, 14, 9, 0)  # 열린다
    service.poke()
    rp.wait(lambda: job(rp, ident)["downloaded_bytes"] > 250_000, timeout=30, what="시간대 안에서 받기")
    rp.wait(lambda: (s_ := resume.load(work_of(rp, ident))) and resume.covered(s_.get("done", [])) > 0, timeout=30, what="기록")
    now["t"] = at(2026, 11, 14, 19, 0)  # 닫힌다(끝날 때 멈춤)
    service.poke()
    held = rp.wait(lambda: (j := job(rp, ident))["status"] == "paused" and j, timeout=15, what="시간대 끝 멈춤")
    assert held["pause_reason"] == "window"
    rp.fake.delay = 0
    now["t"] = at(2026, 11, 15, 9, 30)  # 다음 시간대
    service.poke()
    done = wait_job(rp, ident)
    assert done["status"] == "completed" and (replay_dir(rp) / done["filename"]).read_bytes() == rp.fake.file_bytes
    assert "이어받습니다" in done["resume_note"]


def test_window_end_finish_keeps_running_job(rp):
    """끝까지 받기: 시간대가 끝나도 받는 중인 작업은 끝까지 받고, 다음 작업은 시간대가 열릴 때까지 기다린다."""
    service = rp.app.state.extensions.contexts["downloads"].service
    now = {"t": at(2026, 11, 13, 10, 0)}
    service.clock = lambda: now["t"]
    rp.call("PATCH", "/api/settings", json={"replay_window_rules": [{"days": [4], "start": "09:00", "end": "19:00"}],
                                            "replay_window_enabled": True, "replay_window_end_action": "finish",
                                            "replay_connections": 4})
    ident = start_slow_file_job(rp, delay=0.1, key="finish-0001")
    a = analyze(rp, [DASH])
    nxt = enqueue(rp, a, [{"url": DASH, "quality": FILE_Q}], key="finish-0002")[0]["id"]
    now["t"] = at(2026, 11, 13, 19, 30)
    service.poke()
    done = wait_job(rp, ident)
    assert done["status"] == "completed" and done.get("resume_note") is None
    time.sleep(1.5)
    assert job(rp, nxt)["status"] == "queued"
    rp.fake.delay = 0
    now["t"] = at(2026, 11, 20, 9, 0)
    service.poke()
    assert wait_job(rp, nxt)["status"] == "completed"


def test_queue_skips_jobs_outside_their_channel_window(rp):
    """대기열: 전체 시간대가 닫혀 있어도, 자기 규칙의 시간대가 열린 채널의 작업은 앞 작업을 건너뛰어 시작한다."""
    from live_recorder.ext.downloads.manager import OWNER

    service = rp.app.state.extensions.contexts["downloads"].service
    now = {"t": at(2026, 11, 13, 20, 0)}  # 금요일 저녁: 전체 규칙(09~19시) 밖, 채널 규칙(19~23시) 안
    service.clock = lambda: now["t"]
    mine = "chzzk:" + "c" * 32
    assert rp.call("PATCH", "/api/settings", json={"replay_window_rules": [
        {"days": [4], "start": "09:00", "end": "19:00"}, {"days": [4], "start": "19:00", "end": "23:00", "target": mine}],
        "replay_window_enabled": True, "replay_connections": 4}).status_code == 200
    a = analyze(rp, [DASH])
    first = enqueue(rp, a, [{"url": DASH, "quality": FILE_Q}], key="chan-0001")[0]["id"]
    second = enqueue(rp, a, [{"url": DASH, "quality": FILE_Q}], key="chan-0002", confirm=True)[0]["id"]
    manager = service.manager
    data = manager._get("job", OWNER, second)
    assert data["status"] == "queued"
    data["policy_target"] = mine
    manager._save("job", OWNER, data)
    service.poke()
    assert wait_job(rp, second)["status"] == "completed"  # 뒤에 넣었지만 자기 시간대가 열려 있다
    assert job(rp, first)["status"] == "queued"  # 전체 규칙을 따르는 작업은 기다린다
    now["t"] = at(2026, 11, 20, 9, 30)
    service.poke()
    assert wait_job(rp, first)["status"] == "completed"


# ---- 녹화 중 멈춤 -------------------------------------------------------------------------

def test_recording_pauses_downloads_until_recording_and_finalize_end(rp, server):
    """실제 녹화 엔진: 녹화가 시작되면 받는 중인 작업이 일시정지하고, 녹화와 마무리가 끝나면 이어받아 끝낸다."""
    rp.h.store.set_settings({"rewind_seconds": 0})
    ident = start_slow_file_job(rp, key="rec-0001")
    assert rp.call("POST", "/api/channels", json={"channel_id": CHANNEL, "name": "녹화", "output_dir": "a"}).status_code == 201
    b = server.go_live(title="녹화 중 멈춤")
    rp.wait(rp.h.recording, what="녹화")
    held = rp.wait(lambda: (j := job(rp, ident))["status"] == "paused" and j, timeout=15, what="녹화 중 멈춤")
    assert held["pause_reason"] == "recording"
    gate = rp.client.get("/api/replay/status").json()["admission"]
    assert gate["reason"] == "recording" and gate["recording"] >= 1
    rp.fake.delay = 0
    time.sleep(1.0)
    assert job(rp, ident)["status"] == "paused"  # 녹화하는 동안 다시 시작하지 않는다
    b.end()
    rp.wait(lambda: rp.h.finalized(1), timeout=30, what="마무리")
    done = wait_job(rp, ident)
    assert done["status"] == "completed" and (replay_dir(rp) / done["filename"]).read_bytes() == rp.fake.file_bytes
    assert rp.h.finalized(1)[0]["finalize_status"] == "done"  # 녹화는 그대로 마무리됐다


def test_engine_activity_only_reads():
    """엔진의 공개 속성과 저장소 조회만 읽는다."""
    calls = []

    class Store:
        def open_sessions(self):
            calls.append("open")
            return [{"id": 1}]

        def unfinalized_sessions(self):
            calls.append("unfinalized")
            return [{"id": 2}, {"id": 3}]

    engine = SimpleNamespace(workers={"a": SimpleNamespace(recording=False, in_broadcast=True),
                                      "b": SimpleNamespace(recording=False, in_broadcast=False)}, store=Store())
    assert admission.engine_activity(engine) == {"recording": 1, "finalizing": 2}
    assert calls == ["open", "unfinalized"]
    assert admission.engine_activity(SimpleNamespace(workers={}, store=None)) == {"recording": 0, "finalizing": 0}


# ---- 속도·남은 시간 ----------------------------------------------------------------------

def test_speed_and_remaining_time_estimate():
    assert estimate([], None) == (0, None)
    speed, eta = estimate([(0.0, 0, 10.0), (4.0, 4_000_000, 20.0)], 10_000_000)
    assert speed == 1_000_000 and eta == 6  # 크기를 알면 바이트로
    speed, eta = estimate([(0.0, 100, 10.0), (5.0, 600, 20.0)], None)
    assert speed == 100 and eta == 40  # 크기를 모르면 진행률로(80% 남음, 5초에 10%)
    assert estimate([(0.0, 100, None), (5.0, 600, None)], None) == (100, None)  # 모르면 비운다


def test_running_job_reports_speed_and_eta(rp):
    ident = start_slow_file_job(rp, delay=0.3, key="eta-0001")
    j = rp.wait(lambda: (x := job(rp, ident))["eta_seconds"] is not None and x["speed_bps"] > 0 and x, timeout=20, what="속도")
    assert j["total_bytes"] == len(rp.fake.file_bytes) and j["downloaded_bytes"] <= j["total_bytes"]
    rp.fake.delay = 0
    done = wait_job(rp, ident)
    assert done["eta_seconds"] is None and done["speed_bps"] == 0


# ---- 선택 다운로드의 분석 실패 ------------------------------------------------------------

def test_select_download_keeps_analysis_failures_as_failed_jobs(rp):
    rp.h.add_channel()
    r = rp.call("POST", "/api/replay/select", json={"items": [{"video_no": DASH_NO, "title": "목록 제목"},
                                                                {"video_no": ADULT_NO, "title": "성인 영상"}]})
    assert r.status_code == 200, r.text
    jobs = rp.wait(lambda: (lambda x: x if len(x) == 2 else None)(rp.client.get("/api/replay/jobs").json()["jobs"]), timeout=60,
                   what="작업 둘")
    failed = next(j for j in jobs if j["url"].endswith(ADULT_NO))
    assert failed["status"] == "failed" and failed["error_code"] == "login_required" and "로그인" in failed["error"]
    assert failed["title"] == "성인 영상" and failed["quality"] is None  # 분석하지 못해 목록의 제목을 쓴다. 화질은 재시도 때 고른다
    assert any("분석에 실패" in m for m in logs(rp, failed["id"]))
    counts = rp.client.get("/api/replay/jobs", params={"status": "failed"}).json()["counts"]
    assert counts["failed"] == 1
    # 상태 칸은 수집한 목록에 붙는다. 성인 영상은 10월 게시다
    assert rp.call("POST", f"/api/replay/channels/{CHANNEL}/collect", json={"mode": "auto"}).status_code == 200
    rp.wait(lambda: rp.client.get("/api/replay/channels").json()["channels"][0]["collect"]["status"] == "done", what="목록 수집")
    states = {v["video_no"]: v["state"] for v in rp.client.get(f"/api/replay/channels/{CHANNEL}/videos",
                                                                  params={"month": "2026-10"}).json()["videos"]}
    assert states[ADULT_NO] == "failed"
    ok = next(j for j in jobs if j["url"].endswith(DASH_NO))
    assert wait_job(rp, ok["id"])["status"] == "completed"
    # 재시도하면 다시 분석하고 규칙(best)으로 화질을 고른다. 여전히 쿠키가 없으므로 같은 이유로 실패한다
    assert rp.call("POST", f"/api/replay/jobs/{failed['id']}/retry").status_code == 200
    again = wait_job(rp, failed["id"])
    assert again["status"] == "failed" and again["error_code"] == "login_required" and again["attempt"] == 2


def test_failed_job_retry_picks_quality_by_rule(rp):
    """분석 실패로 남긴 작업(화질 없음)을 재시도하면 수신 프로세스가 규칙으로 화질을 고르고 파일 이름도 정한다."""
    manager = rp.app.state.extensions.contexts["downloads"].service.manager
    job_ = manager._auto_failed_job(DASH, "목록 제목", "best", "failed", "network_timeout", "시간 초과")
    with manager.db:
        manager.store.save("job", "admin", job_)
    assert rp.call("POST", f"/api/replay/jobs/{job_['id']}/retry").status_code == 200
    done = wait_job(rp, job_["id"])
    assert done["status"] == "completed", done
    assert done["quality"] == FILE_Q and done["quality_label"] == "90p"
    assert done["filename"] == "[261110_190327] 가짜채널 DASH 다시보기.mp4"


# ---- 저장소 옮기기 ----------------------------------------------------------------------

@pytest.fixture
def job_kinds():
    """대기열만 따로 여는 시험: 작업 종류(다시보기·클립)는 그 확장이 켜졌을 때 등록된다. 여기서 직접 등록한다."""
    from live_recorder.ext.clips.extension import CLIP_KIND
    from live_recorder.ext.downloads import api
    from live_recorder.ext.replay.extension import VIDEO_KIND

    api.register_kind(VIDEO_KIND)
    api.register_kind(CLIP_KIND)
    yield
    api.unregister_kind("video")
    api.unregister_kind("clip")


def test_existing_replay_store_opens_unchanged(tmp_path, job_kinds):
    """이전 빌드의 다시보기 저장소(records·requests·logs·downloads)가 그대로 열린다. 상태값의 뜻은 그대로다."""
    state, root = tmp_path / "state", tmp_path / "rec" / "replay"
    state.mkdir()
    root.mkdir(parents=True)
    (root / "old.mp4").write_bytes(b"x")
    work = root / ".work"
    work.mkdir()
    db = sqlite3.connect(state / "replay.sqlite3")
    db.executescript('''
      CREATE TABLE records(id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
      CREATE INDEX records_queue ON records(kind,status);
      CREATE TABLE requests(owner TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, ids TEXT NOT NULL, PRIMARY KEY(owner,key));
      CREATE TABLE logs(seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT NOT NULL, at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
      CREATE TABLE downloads(video TEXT NOT NULL, quality TEXT NOT NULL, title TEXT NOT NULL, filename TEXT NOT NULL,
        completed_at TEXT NOT NULL, PRIMARY KEY(video,quality));
    ''')
    base = {"url": "https://chzzk.naver.com/video/777", "quality": "q1", "title": "옛 영상", "section": None,
            "updated_at": "2026-11-11T00:00:00+00:00", "attempt": 1}
    rows = {"a" * 32: "completed", "b" * 32: "queued", "c" * 32: "running", "d" * 32: "interrupted", "e" * 32: "failed"}
    for ident, status in rows.items():
        payload = dict(base, id=ident, status=status, stage=status, filename="old.mp4" if status == "completed" else None)
        db.execute("INSERT INTO records(id,kind,owner,status,payload) VALUES(?,?,?,?,?)", (ident, "job", "admin", status, json.dumps(payload)))
        (work / ident).mkdir()
        (work / ident / "video.mp4").write_bytes(b"partial")
    analysis = {"id": "f" * 32, "status": "queued", "items": [], "_urls": ["https://chzzk.naver.com/video/778"],
                "_auto": {"titles": ["고른 영상"], "quality": "best"}, "updated_at": "2026-11-11T00:00:00+00:00"}
    db.execute("INSERT INTO records(id,kind,owner,status,payload) VALUES(?,?,?,?,?)", (analysis["id"], "analysis", "admin", "queued", json.dumps(analysis)))
    db.execute("INSERT INTO requests VALUES(?,?,?,?)", ("admin", "key-00000001", "{}", json.dumps(["a" * 32])))
    db.execute("INSERT INTO logs(job,at,level,message) VALUES(?,?,?,?)", ("a" * 32, "2026-11-11T00:00:00+00:00", "info", "옛 로그"))
    db.execute("INSERT INTO downloads(video,quality,title,filename,completed_at) VALUES(?,?,?,?,?)", ("777", "q1", "옛 영상", "old.mp4", "2026-11-11T00:00:00+00:00"))
    db.commit()
    db.close()

    async def run():
        m = ReplayManager(state, root, 0, download_base=tmp_path / "rec")
        try:
            assert m.get_job("admin", "a" * 32)["status"] == "completed"
            assert m.get_job("admin", "b" * 32)["status"] == "interrupted"  # 끝나지 않은 작업은 interrupted
            assert m.get_job("admin", "c" * 32)["status"] == "interrupted"
            assert m.get_job("admin", "d" * 32)["status"] == "interrupted" and m.get_job("admin", "e" * 32)["status"] == "failed"
            assert not any((work / i).exists() for i in rows)  # 일시정지가 아닌 작업의 부분 파일은 지운다
            assert m.logs("admin", "a" * 32, 0)["logs"][0]["message"] == "옛 로그"
            assert m.db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
            assert m.video_states(["777", "778"]) == {"777": "downloaded", "778": "failed"}
            listed = m.list_jobs("admin", "attention")
            # 분석을 기다리던 선택 다운로드는 조용히 사라지지 않고 중단 작업으로 남는다
            auto = [j for j in listed["jobs"] if j["url"].endswith("/778")]
            assert len(auto) == 1 and auto[0]["status"] == "interrupted" and auto[0]["title"] == "고른 영상"
            assert m.paused_count() == 0 and m.get_analysis("admin", "f" * 32)["status"] == "interrupted"
            columns = {r[1] for r in m.db.execute("PRAGMA table_info(records)")}
            assert columns == {"id", "kind", "owner", "status", "payload", "platform"}  # 플랫폼 열만 더했다(기존 줄은 치지직)
        finally:
            await m.close()

    asyncio.run(run())


def test_paused_job_survives_manager_restart(tmp_path, job_kinds):
    """저장소 단위: 일시정지한 작업과 그 부분 파일은 재시작해도 남는다."""
    state, root = tmp_path / "state", tmp_path / "rec" / "replay"

    async def first():
        m = ReplayManager(state, root, 0, download_base=tmp_path / "rec")
        try:
            job_ = m._new_job({"url": DASH, "platform": "chzzk", "kind": "video", "title": "t", "quality": "q", "quality_label": "",
                               "section": None, "duration_seconds": None, "filename_hint": None})
            job_.update(status="paused", stage="paused", pause_reason="window")
            with m.db:
                m.store.save("job", "admin", job_)
            (m.work_root / job_["id"]).mkdir()
            (m.work_root / job_["id"] / "video.mp4").write_bytes(b"partial")
            return job_["id"]
        finally:
            await m.close()

    ident = asyncio.run(first())

    async def second():
        m = ReplayManager(state, root, 0, download_base=tmp_path / "rec")
        try:
            j = m.get_job("admin", ident)
            assert j["status"] == "paused" and j["pause_reason"] == "window"
            assert (m.work_root / ident / "video.mp4").read_bytes() == b"partial"
            assert ident in m.held and str(m.work_root / ident / "video.mp4") in map(str, m.active_paths())
            await m.cancel("admin", ident)
            assert not (m.work_root / ident).exists()
        finally:
            await m.close()

    asyncio.run(second())


# ---- API·화면 ---------------------------------------------------------------------------------------

def test_new_routes_need_login_and_csrf(rp):
    for method, path in (("POST", f"/api/replay/jobs/{'a' * 32}/pause"), ("POST", f"/api/replay/jobs/{'a' * 32}/resume"),
                         ("PUT", "/api/replay/control")):
        assert web.access_level(method, path) == web.LOGIN
    assert rp.client.put("/api/replay/control", json={"paused": True}).status_code == 403  # CSRF 토큰 없음
    assert rp.client.post(f"/api/replay/jobs/{'a' * 32}/pause").status_code == 403
    assert rp.call("PUT", "/api/replay/control", json={"paused": "yes"}).status_code == 422
    assert rp.call("PUT", "/api/replay/control", json={"paused": True, "x": 1}).status_code == 422
    bad = rp.call("PATCH", "/api/settings", json={"replay_window_rules": [{"days": [1], "start": "10:00", "end": "10:00"}]})
    assert bad.status_code == 400
    assert rp.call("PATCH", "/api/settings", json={"replay_window_enabled": True}).status_code == 400
    assert rp.client.get("/api/settings").json()["settings"]["replay_pause_while_recording"] is True  # 기본 켜짐
    rp.client.post("/api/logout", headers={"X-CSRF-Token": rp.csrf})
    assert rp.client.get("/api/replay/status").status_code == 401


def test_screen_elements_for_pause_and_window():
    """화면 요소와 app.js의 일치. 인라인 스크립트·스타일 없음, el() 도우미만 쓴다."""
    html = index_html()
    js = all_js()
    ids = set(re.findall(r'id="([^"]+)"', html))
    for ident in ("replay-pause-all", "replay-admission", "window-rules", "window-add", "window-now"):
        assert ident in ids and f'$("{ident}")' in js, ident
    for ident in set(re.findall(r'\$\("([a-z0-9-]+)"\)', js)):
        assert ident in ids, f"app.js가 찾는 요소가 화면에 없다: {ident}"
    for name in ("replay_pause_while_recording", "replay_window_enabled"):
        assert f'name="{name}"' in html and f"f.{name}" in js  # 탭마다 그 탭의 폼(f)에서 읽고 채운다
    assert html.count('name="replay_window_end_action"') == 2 and 'value="pause"' in html and 'value="finish"' in html
    for text in ("/api/replay/control", "/pause`", "/resume`", "eta_seconds", "pause_reason", "resume_note", "partial_bytes"):
        assert text in js, text
    assert "<script>" not in html and "style=" not in html and "innerHTML" not in js
    assert "function formatWhen(" in js and "KST_OFFSET_MS = 9 * 3600000" in js  # 시각은 한 함수가 Asia/Seoul로 만든다


# ---- 배지·전체 취소에서 일시정지 제외, HLS형의 두 배 공간 ---------------------------

def test_badge_and_cancel_all_leave_paused_jobs(rp):
    """일시정지한 작업은 탭 배지와 "대기·진행 전체 취소"에 들지 않는다. 부분 파일도 남는다."""
    ident = start_slow_file_job(rp, key="badge-0001")
    pause(rp, ident)
    rp.call("PUT", "/api/replay/control", json={"paused": True})  # 다음 작업이 대기에 머물게 한다
    a = analyze(rp, [DASH])
    other = enqueue(rp, a, [{"url": DASH, "quality": FILE_Q}], key="badge-0002")[0]["id"]
    st_ = rp.client.get("/api/replay/status").json()
    assert st_["active"] == 1 and st_["paused_jobs"] == 1
    r = rp.call("POST", "/api/replay/cancel-active")
    assert r.status_code == 200 and r.json()["cancelled"] == 1
    assert job(rp, other)["status"] == "cancelled"
    assert job(rp, ident)["status"] == "paused" and (work_of(rp, ident) / "resume.json").exists()
    assert rp.client.get("/api/replay/status").json()["active"] == 0
    assert rp.call("POST", f"/api/replay/jobs/{ident}/cancel").json()["status"] == "cancelled"  # 줄에서 하나씩 취소한다
    assert not work_of(rp, ident).exists()


def test_space_need_counts_hls_twice():
    job_ = {"space_mode": "hls", "size_estimate": 1000, "partial_bytes": 300}
    assert ReplayManager.space_need(job_) == 1700
    assert ReplayManager.space_need(job_, on_disk=1000) == 1000  # 조각을 다 받았으면 담을 결과만큼
    assert ReplayManager.space_need({"size_estimate": 1000}) == 0  # 단일 파일은 지금 방식대로
    assert ReplayManager.space_need({"space_mode": "hls"}) == 0  # 크기를 모르면 더하지 않는다


def test_hls_pauses_when_double_size_does_not_fit(tmp_path, server, tools, vod_media):
    """HLS형은 영상 크기의 두 배가 들어야 한다. 모자라면 여유 공간 부족으로 멈추고, 공간이 생기면 이어받는다."""
    free = {"bytes": 2_000_000}
    fake = FakeVod(server, vod_media)
    with Web(tmp_path, server, tools, extensions=REPLAY_EXTENSIONS, disk_free=lambda path: free["bytes"]) as w:
        w.fake = fake
        w.login()
        w.call("PATCH", "/api/settings", json={"replay_min_free_bytes": 0, "replay_segment_connections": 1})  # 하나씩 받아야 받는 도중에 멈춘다
        a = analyze(w, [FAST])
        q = a["items"][0]["qualities"][0]["id"]
        size = sum(p.stat().st_size for p in (vod_media / "hls").glob("seg*.ts"))
        assert size < free["bytes"] < 2 * size  # 한 번은 들어가도 두 번은 안 들어간다
        fake.delay = 0.05
        ident = enqueue(w, a, [{"url": FAST, "quality": q}], key="hls-space-01")[0]["id"]
        held = w.wait(lambda: (j := job(w, ident))["status"] == "paused" and j, timeout=30, what="공간 부족 멈춤")
        assert held["pause_reason"] == "space" and held["space_mode"] == "hls" and held["size_estimate"] > 0
        time.sleep(1.5)
        assert job(w, ident)["status"] == "paused"  # 두 배가 들어가지 않으니 다시 시작하지 않는다
        gate = w.client.get("/api/replay/status").json()["admission"]
        assert gate["reason"] == "space" and gate["space_need"] > 0
        fake.delay = 0
        free["bytes"] = 10**12
        done = wait_job(w, ident)
        assert done["status"] == "completed", done
        assert "이어받습니다" in done["resume_note"]
