"""씨미 다시보기·클립. 실제 씨미는 부르지 않는다.

가짜 씨미(tests/fake_cime.py의 serve_media)가 정보 API, 서명 쿠키 API, 서명 쿠키가 없으면 403을 내는 파일 호스트
(fMP4 HLS 다시보기와 mp4 클립)를 낸다.
"""

import ast
import json
import re
from pathlib import Path

import pytest

import fake_cime
from conftest import probe
from live_recorder.core import extensions
from live_recorder.ext.cime_media import clip, common, vod
from live_recorder.ext.cime_media import extension as cx
from live_recorder.ext.downloads import api, media
from live_recorder.platforms import credentials
from live_recorder.platforms.cime import platform as cm
from replay_fake import build_media

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "live_recorder"
EXTS = ["downloads", "cime_media"]
CID = "1007346"
VOD_URL = "https://ci.me/@jeongnae/vods/501"
CLIP_URL = "https://ci.me/clips/701"
SEGMENTS = 60  # 2초 조각 60개(120초)


@pytest.fixture(scope="module")
def cime_files(tools, tmp_path_factory):
    out = tmp_path_factory.mktemp("cime_media")
    build_media(tools[0], out)
    return out


@pytest.fixture
def cw(tmp_path, server, tools, cime_files, monkeypatch):
    from test_web import Web

    monkeypatch.setenv(cm.BASE_ENV, server.base + fake_cime.PREFIX)
    monkeypatch.delenv(cm.ENV_FORBID, raising=False)
    fake_cime.serve_media(server, cime_files)
    channel = (CID, "jeongnae", "정나은양")
    server.cime_vods["501"] = {"title": "지난 방송 제목", "channel": channel, "liveOpenedAt": "2026-11-13T12:04:05Z",
                               "openedAt": "2026-11-13T12:00:00Z"}
    server.cime_vods["502"] = {"title": "연령 제한 방송", "channel": channel, "adult": True, "openedAt": "2026-11-12T01:02:03Z"}
    server.cime_vods["503"] = {"title": "처리 중", "channel": channel, "state": "PROCESSING", "openedAt": "2026-11-12T01:02:03Z"}
    server.cime_clips["701"] = {"title": "클립 제목", "channel": channel, "createdAt": "2026-11-14T00:30:00Z", "duration": 120000}
    w = Web(tmp_path, server, tools, extensions=EXTS)
    w.h.store.set_platform_enabled("cime", True)
    with w:
        w.login()
        yield w


def analyze(w, urls):
    r = w.call("POST", "/api/replay/analyze", json={"urls": urls})
    assert r.status_code == 200, r.text
    ident = r.json()["id"]
    return w.wait(lambda: (lambda a: a if a["status"] in ("completed", "failed") else None)(
        w.client.get(f"/api/replay/analyses/{ident}").json()), timeout=60, what="분석")


def enqueue(w, analysis, quality=None, confirm=True, key="cime-request-0001", index=0):
    item = analysis["items"][index]
    body = {"analysis_id": analysis["id"], "items": [{"url": item["url"], "quality": quality or item["qualities"][0]["id"]}],
            "key": key, "confirm": confirm}
    return w.call("POST", "/api/replay/jobs", json=body)


def job(w, ident):
    return w.client.get(f"/api/replay/jobs/{ident}").json()


def logs(w, ident):
    return [e["message"] for e in w.client.get(f"/api/replay/jobs/{ident}/logs").json()["logs"]]


def wait_job(w, ident, statuses=("completed", "failed", "cancelled"), timeout=120):
    return w.wait(lambda: (lambda j: j if j["status"] in statuses else None)(job(w, ident)), timeout=timeout, what=f"작업 {statuses}")


def replay_dir(w) -> Path:
    return w.h.root / "replay"


def segs_of(server, start=0):
    return [x for x in server.cime_cdn[start:] if x["path"].endswith(".m4s") and x["status"] == 200]


# ---- 주소 인식과 거절, 열쇠, 파일 이름 ----
def test_parse_urls_and_keys():
    want = {"platform": "cime", "kind": "cime_vod", "url": VOD_URL, "video_id": "501"}
    for text in (VOD_URL, "ci.me/@jeongnae/vods/501/", "https://ci.me/@jeongnae/vods/501?from=share", "https://www.ci.me/@jeongnae/vods/501"):
        assert vod.parse(text) == want, text
        assert clip.parse(text) is None
    for text in ("https://ci.me/clips/701", "ci.me/clips/701/?t=3", "https://ci.me/@jeongnae/clips/701"):
        assert clip.parse(text) == {"url": CLIP_URL, "platform": "cime", "kind": "cime_clip", "video_id": "701"}, text
        assert vod.parse(text) is None
    assert vod.parse("https://ci.me/@jeongnae/live")["rejected"] == "live_video"
    for text in ("https://ci.me/@jeongnae", "https://ci.me/@jeongnae/vods", "https://ci.me/@jeongnae/clips"):
        assert vod.parse(text)["rejected"] == "unsupported_url", text
    for text in ("https://evil.example/@x/vods/1", "https://chzzk.naver.com/video/1", "ftp://ci.me/@x/vods/1"):
        with pytest.raises(media.Failure):
            vod.parse(text)
        with pytest.raises(media.Failure):
            clip.parse(text)
    assert vod.key(want) == "cime:vod:501" and clip.key(clip.parse(CLIP_URL)) == "cime:clip:701"


def test_filename_and_time_rules():
    assert common.kst("2026-11-13T12:04:05Z") == "2026-11-13 21:04:05"
    assert common.kst("bad") is None and common.kst(None) is None
    name = vod.filename({"title": "제목: 이상한/글자", "channel": "정나은양", "open_date": "2026-11-13 21:04:05", "video_id": "501"})
    assert name.startswith("[261113_210405] 정나은양 제목") and name.endswith(".mp4") and "/" not in name


# ---- 분석 ----
def test_analysis_lists_qualities_and_kinds(cw, server):
    a = analyze(cw, [VOD_URL, CLIP_URL])
    v, c = a["items"]
    assert v["error"] is None and v["kind"] == "cime_vod" and v["mode"] == "hls" and v["platform"] == "cime", v
    assert v["title"] == "지난 방송 제목" and v["channel"] == "정나은양" and v["open_date"] == "2026-11-13 21:04:05"
    assert v["duration_seconds"] == 120 and v["supports"] == {"section": False} and v["thumbnail"] is None
    assert [q["height"] for q in v["qualities"]] == [180, 90]
    assert [q["label"] for q in v["qualities"]] == ["180p · 약 9MB", "90p · 약 3MB"]  # 대역폭×길이
    assert c["error"] is None and c["kind"] == "cime_clip" and c["mode"] == "clip" and c["open_date"] == "2026-11-14 09:30:00"
    assert c["supports"] == {"section": False}
    kinds = cw.client.get("/api/replay/kinds").json()["kinds"]
    assert [(k["kind"], k["title"]) for k in kinds] == [("cime_vod", "씨미 다시보기"), ("cime_clip", "씨미 클립")]
    assert any("ci.me" in k["example"] for k in kinds)
    # 분석에서 서명 쿠키를 받는다(다시보기·클립 하나씩). 서명 쿠키 값은 화면으로 나가지 않는다
    assert [x["body"]["type"] for x in server.cime_sign_calls] == ["VIDEO", "CLIP"]
    assert "SECRET" not in json.dumps(a)


def test_login_rejected_missing_live_and_channel(cw, server):
    urls = ["https://ci.me/@jeongnae/vods/502", "https://ci.me/@jeongnae/vods/999", "https://ci.me/@jeongnae/vods/503",
            "https://ci.me/@jeongnae/live", "https://ci.me/@jeongnae"]
    errors = [i["error"] for i in analyze(cw, urls)["items"]]
    assert errors[0].startswith("로그인이 필요한 영상입니다")
    assert errors[1] == "영상을 찾지 못했습니다."
    assert errors[2] == "지금 받을 수 있는 상태의 영상이 아닙니다."
    assert errors[3] == "방송 중인 영상은 채널을 등록해 녹화해 주십시오."
    assert "채널·목록 주소는 받지 않습니다" in errors[4]
    credentials.save(cw.h.cfg, "cime", {"mauth-authorization-code": "eyJ.bad.token"})
    assert analyze(cw, [urls[0]])["items"][0]["error"] == "쿠키가 만료됐거나 볼 권한이 없습니다."
    credentials.save(cw.h.cfg, "cime", {"mauth-authorization-code": fake_cime.GOOD_AUTH})
    assert analyze(cw, [urls[0]])["items"][0]["error"] is None


# ---- 다시보기 받기: mp4 하나, 받는 중 서명 쿠키 갱신, 쿠키가 다른 곳에 없음, 받은 영상 기억 ----
def test_vod_download_renews_signed_cookies(cw, server, tools):
    credentials.save(cw.h.cfg, "cime", {"mauth-authorization-code": fake_cime.GOOD_AUTH})
    server.cime_ttl = (30, 1)  # 1초마다 새로 받는다
    server.cime_delay = 0.04
    a = analyze(cw, [VOD_URL])
    signs = len(server.cime_sign_calls)
    r = enqueue(cw, a, quality=a["items"][0]["qualities"][-1]["id"])  # 90p
    assert r.status_code == 200, r.text
    j = wait_job(cw, r.json()["jobs"][0]["id"])
    assert j["status"] == "completed", (j, logs(cw, j["id"]))
    assert j["filename"] == "[261113_210405] 정나은양 지난 방송 제목.mp4"
    out = replay_dir(cw) / j["filename"]
    info = probe(tools, out)
    assert sorted(s["codec_type"] for s in info["streams"]) == ["audio", "video"]
    assert abs(float(info["format"]["duration"]) - 120) < 3
    assert len(segs_of(server)) == SEGMENTS
    assert len(server.cime_sign_calls) - signs >= 3  # 작업의 분석 + 받는 중 갱신
    assert all(x["body"] == {"type": "VIDEO", "videoId": "501"} for x in server.cime_sign_calls)
    assert all(x["status"] == 200 for x in server.cime_cdn), [x for x in server.cime_cdn if x["status"] != 200][:3]
    # 로그인 쿠키는 ci.me API에만, 서명 쿠키는 그 영상의 파일 경로에만
    assert all("mauth-authorization-code" not in (x["cookie"] or "") for x in server.cime_cdn)
    assert all("CloudFront" not in (x["cookie"] or "") for x in server.cime_requests)
    assert all(fake_cime.GOOD_AUTH in (x["cookie"] or "") for x in server.cime_requests if x["path"].startswith("/api/app/"))
    assert all(x["path"].startswith("/cdn/vod/501/") for x in server.cime_cdn)
    # 로그·작업 DB·작업 폴더에 쿠키 값이 없다
    secrets = ("SECRET", fake_cime.GOOD_AUTH)
    text = cw.h.log_text() + "\n".join(logs(cw, j["id"]))
    assert not any(s in text for s in secrets)
    for path in cw.h.root.rglob("*"):
        if path.is_file() and path.suffix in (".db", ".json", ".sqlite", ".log", "") and path.stat().st_size < 50_000_000:
            data = path.read_bytes()
            if path.name != "credentials.json" and "cred" not in path.name:
                assert b"SECRET" not in data and fake_cime.GOOD_AUTH.encode() not in data, path
    # 받은 영상 기억(플랫폼·종류·ID)
    a = analyze(cw, ["ci.me/@jeongnae/vods/501/"])
    r = enqueue(cw, a, confirm=False, key="cime-request-0002")
    assert r.status_code == 409 and r.json()["duplicates"] == ["지난 방송 제목"]


def test_vod_forbidden_renews_once_and_continues(cw, server):
    server.cime_revoke_at = 10  # 조각 10을 요청할 때 서명이 없어진다(403)
    a = analyze(cw, [VOD_URL])
    signs = len(server.cime_sign_calls)
    j = wait_job(cw, enqueue(cw, a).json()["jobs"][0]["id"])
    assert j["status"] == "completed", (j, logs(cw, j["id"]))
    denied = [x for x in server.cime_cdn if x["status"] == 403]
    assert [x["path"].rsplit("/", 1)[-1] for x in denied] == ["seg010.m4s"]
    assert len(server.cime_sign_calls) - signs == 2  # 작업의 분석 + 403 뒤 한 번
    assert len({x["path"] for x in segs_of(server)}) == SEGMENTS


def test_vod_pause_resume_skips_received_segments(cw, server):
    server.cime_delay = 0.1
    a = analyze(cw, [VOD_URL])
    ident = enqueue(cw, a).json()["jobs"][0]["id"]
    seg_dir = replay_dir(cw) / ".work" / ident / "seg"
    cw.wait(lambda: seg_dir.is_dir() and len([p for p in seg_dir.iterdir() if p.suffix == ".m4s"]) >= 12, timeout=60, what="조각")
    r = cw.call("POST", f"/api/replay/jobs/{ident}/pause")
    assert r.status_code == 200 and r.json()["status"] == "paused", r.text
    have = len([p for p in seg_dir.iterdir() if p.suffix == ".m4s"])
    state = (replay_dir(cw) / ".work" / ident / "resume.json").read_text()
    assert "SECRET" not in state and "CloudFront" not in state
    served, signs = len(segs_of(server)), len(server.cime_sign_calls)
    server.cime_delay = 0
    assert cw.call("POST", f"/api/replay/jobs/{ident}/resume").status_code == 200
    j = wait_job(cw, ident)
    assert j["status"] == "completed", (j, logs(cw, ident))
    assert len(segs_of(server, 0)) - served <= SEGMENTS - have + 1  # 받은 조각은 다시 받지 않는다(견주기 한 번까지)
    assert len(server.cime_sign_calls) > signs  # 이어받기는 서명 쿠키를 새로 받는다


# ---- 클립 받기 ----
def test_clip_download_with_signed_cookies(cw, server, tools, cime_files):
    a = analyze(cw, [CLIP_URL])
    j = wait_job(cw, enqueue(cw, a).json()["jobs"][0]["id"])
    assert j["status"] == "completed", (j, logs(cw, j["id"]))
    assert j["filename"] == "[261114_093000] 정나은양 클립 제목.mp4"
    out = replay_dir(cw) / j["filename"]
    assert sorted(s["codec_type"] for s in probe(tools, out)["streams"]) == ["audio", "video"]
    got = [x for x in server.cime_cdn if x["path"] == "/cdn/clip/701/clip.mp4"]
    assert got and all(x["status"] == 200 and "CloudFront-Signature" in x["cookie"] for x in got)
    assert all("mauth-authorization-code" not in (x["cookie"] or "") for x in got)
    a = analyze(cw, ["https://ci.me/@jeongnae/clips/701"])
    r = enqueue(cw, a, confirm=False, key="cime-request-0003")
    assert r.status_code == 409 and r.json()["duplicates"] == ["클립 제목"]


def test_clip_forbidden_mid_download_renews(cw, server):
    """파일 요청이 403이면 서명 쿠키를 한 번 새로 받아 다시 시도한다."""
    a = analyze(cw, [CLIP_URL])
    signs = len(server.cime_sign_calls)
    original = server.extra_get
    state = {"done": False}

    def revoking(path, headers):
        if "/cdn/clip/" in path and not state["done"]:
            state["done"] = True
            server.cime_signs.clear()
        return original(path, headers)

    server.extra_get = revoking
    j = wait_job(cw, enqueue(cw, a).json()["jobs"][0]["id"])
    assert j["status"] == "completed", (j, logs(cw, j["id"]))
    assert any(x["status"] == 403 for x in server.cime_cdn)
    assert len(server.cime_sign_calls) - signs >= 2


# ---- 씨미가 꺼졌을 때, 확장 정보 ----
def test_disabled_platform_and_extension_info(cw, server):
    cw.h.store.set_platform_enabled("cime", False)
    r = cw.call("POST", "/api/replay/analyze", json={"urls": [VOD_URL]})
    assert r.status_code == 400 and "씨미를 쓰려면 설정에서 먼저 켜 주십시오" in r.text
    rows = {x["name"]: x for x in cw.client.get("/api/extensions").json()["extensions"]}
    assert rows["cime_media"]["title"] == "씨미 다시보기·클립" and rows["cime_media"]["requires"] == ["downloads"]
    assert rows["cime_media"]["description"]
    assert rows["cime_media"]["requires_platforms"] == ["씨미"] and rows["downloads"]["requires_platforms"] == []


def test_screen_shows_kind_and_needed_platform():
    """분석 결과·작업 줄의 종류 이름, 주소 칸 예시(켜진 종류의 example), 확장 묶음의 필요한 플랫폼."""
    from webui_files import all_js

    js = all_js()
    assert "필요한 플랫폼: ${x.requires_platforms.join" in js
    assert "kindTitle(job.kind)" in js and "kindTitle(item.kind)" in js and "k.example" in js
    assert '(item.kind || "video") === "video" ? "빠른 다시보기"' in js  # 씨미 다시보기에 "빠른 다시보기"를 붙이지 않는다
    assert "!k.title.includes(p.name)" in js


def test_default_off_and_registered():
    assert extensions.BUILTIN["cime_media"] == "live_recorder.ext.cime_media.extension"
    assert cx.EXTENSION.requires == ("downloads",) and cx.EXTENSION.requires_platforms == ("cime",)
    assert {k.kind for k in cx.EXTENSION.job_kinds} == {"cime_vod", "cime_clip"}
    assert all(k.platform == "cime" for k in cx.EXTENSION.job_kinds)


# ---- downloads 공개 접점만, 창구, 요청 호스트 ----
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pkg = path.relative_to(PKG.parent).with_suffix("").parts[:-1]
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = list(pkg[: len(pkg) - node.level + 1]) if node.level else []
            mod = ".".join(base + ([node.module] if node.module else []))
            out.add(mod)
            out |= {f"{mod}.{a.name}" for a in node.names}
    return out


def test_uses_only_the_public_downloads_interface_and_the_gate():
    bad = []
    for path in sorted((PKG / "ext" / "cime_media").rglob("*.py")):
        for m in _imports(path):
            if m.startswith("live_recorder.ext.downloads.") and not m.startswith(("live_recorder.ext.downloads.api",
                                                                                 "live_recorder.ext.downloads.media")):
                bad.append(f"{path.name}: {m}")
            if m.startswith(("live_recorder.ext.replay", "live_recorder.ext.clips", "live_recorder.ext.youtube_video")):
                bad.append(f"{path.name}: {m}")
    assert bad == [], bad
    # 분석(정보 API·서명 쿠키·마스터 재생목록)은 그 플랫폼 창구의 EXTRA 줄로 나간다(작업 종류의 platform이 cime)
    assert all(k.platform == "cime" for k in cx.EXTENSION.job_kinds)
    src = (PKG / "ext" / "downloads" / "manager.py").read_text(encoding="utf-8")
    assert "gate.slot(2)" in src and "_analysis_gate" in src
    # 요청 호스트는 ci.me와 streaming.cf.ci.me뿐이다
    assert cm.SITE_HOSTS == ("ci.me", "www.ci.me") and common.FILE_HOSTS == ("streaming.cf.ci.me",)


def test_analysis_goes_through_the_cime_gate(cw, server):
    """씨미 분석은 씨미 창구를 쓴다(분석 한 번에 자리 하나)."""
    from live_recorder.ext.downloads import manager as mgr

    used = []

    original = mgr.ReplayManager._analysis_gate

    def spy(self, data):
        gate = original(self, data)
        used.append(gate is not None)
        return gate

    mgr.ReplayManager._analysis_gate = spy
    try:
        a = analyze(cw, [VOD_URL])
    finally:
        mgr.ReplayManager._analysis_gate = original
    assert a["items"][0]["error"] is None and used == [True], used


def test_signed_cookie_header_only_for_the_video_path(monkeypatch):
    calls = []
    s = common.SignedCookies("vod", "501", None, VOD_URL, clock=lambda: 100.0)
    s.values = {n: "v" for n in common.SIGN_NAMES}
    s.path = "/vod/501/"
    s.refresh_at = 200.0
    monkeypatch.setattr(s, "renew", lambda: calls.append(1))
    media.configure(common.FILE_HOSTS, {})
    assert "CloudFront-Signature=v" in s.headers("https://streaming.cf.ci.me/vod/501/90p/seg000.m4s")["Cookie"]
    assert s.headers("https://streaming.cf.ci.me/vod/999/90p/seg000.m4s") == {}
    assert s.headers("https://ci.me/api/app/videos/501") == {}
    assert s.headers("https://evil.example/vod/501/a") == {}
    assert calls == []
    s.clock = lambda: 250.0  # 갱신 시각이 지났다
    s.headers("https://streaming.cf.ci.me/vod/501/90p/seg001.m4s")
    assert calls == [1]


def test_log_export_masks_signed_cookie_names():
    """로그 묶음의 가리기 목록에 서명 쿠키 이름을 더했다."""
    from live_recorder import logmask

    text = logmask.Masker(False).text("Cookie: CloudFront-Policy=eyJTdGF0ZW1lbnQiOltdfQ__; CloudFront-Signature=AbCdEf123456~xyz; "
                                      "CloudFront-Key-Pair-Id=K2ABCDEFGHIJ")
    assert "eyJTdGF0ZW1lbnQ" not in text and "AbCdEf123456" not in text and "K2ABCDEFGHIJ" not in text, text
    text = logmask.Masker(False).text("set CloudFront-Signature=AbCdEf123456~xyz CloudFront-Key-Pair-Id=K2ABCDEFGHIJ path=/v/1/")
    assert "AbCdEf123456" not in text and "K2ABCDEFGHIJ" not in text and "CloudFront-Signature=" in text, text
