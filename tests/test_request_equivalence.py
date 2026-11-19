"""치지직으로 나가는 요청이 예전과 같은지.

가짜 서버가 받은 요청의 경로 모양, 질의 키, 머리글 이름과 고정 값을 기준 코드가 보낸 것(tests/fixtures/b12_requests.json)과 견준다.
대상: 방송 확인, 타임머신, 주소 갱신(타임머신·일반 두 방식), 쿠키 확인, 재생목록·조각, 썸네일 주소 처리.
기준 코드에서 한 번 `RECORD_B12=1`로 돌려 기대값을 남겼다(그때만 옛 경로를 썼다). 그 뒤로는 견주기만 한다.
"""

import asyncio
import json
import os
import re
from pathlib import Path

from conftest import CHANNEL
from live_recorder.platforms import credentials, registry

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "b12_requests.json"
FIXED_HEADERS = ("user-agent", "referer", "origin", "accept", "cache-control", "pragma")
AUT, SES = "AUT-equiv-0001", "SES-equiv-0002"


def save_cookies(harness):
    credentials.save(harness.cfg, "chzzk", {"NID_AUT": AUT, "NID_SES": SES})


def thumbnail_url(raw):
    return registry.get("chzzk").thumbnail_url(raw)


def shape(req: dict) -> dict:
    path = req["path"]
    path = path.replace(CHANNEL, "<channel>")
    path = re.sub(r"/live/\d+/", "/live/<live>/", path)
    path = re.sub(r"^/hls/tok\d+/", "/hls/<token>/", path)
    path = re.sub(r"seg\d{5}", "seg<n>", path)
    keys = sorted({kv.split("=", 1)[0] for kv in req["query"].split("&") if kv}) if req["query"] else []
    headers = req["headers"]
    fixed = {k: headers[k] for k in FIXED_HEADERS if k in headers}
    cookie = headers.get("cookie")
    return {"path": path, "query_keys": keys, "header_names": sorted(k for k in headers if k not in ("content-length",)),
            "fixed": fixed, "cookie_names": sorted(c.split("=", 1)[0].strip() for c in cookie.split(";")) if cookie else []}


def kinds(requests: list[dict]) -> dict:
    out: dict[str, list] = {}
    for req in requests:
        s = shape(req)
        p = s["path"]
        if p.endswith("/live-detail"):
            kind = "live_detail"
        elif p.endswith("/time-machine"):
            kind = "time_machine"
        elif p.endswith("/user-status"):
            kind = "cookie_check"
        elif p.startswith("/hls/"):
            kind = "stream_" + ("master" if p.endswith("master.m3u8") else "media" if p.endswith("media.m3u8") else "segment")
        else:
            continue
        if s not in out.setdefault(kind, []):
            out[kind].append(s)
    return out


def run_scenarios(make_harness, server) -> dict:
    result = {}
    for tm in (True, False):
        server.requests.clear()
        harness = make_harness(f"tm{int(tm)}")
        harness.add_channel()
        save_cookies(harness)
        harness.store.set_settings({"rewind_seconds": 10, "timemachine_enabled": tm})
        b = server.go_live(initial=30, rate=10)

        async def scenario():
            async with harness:
                await harness.until(harness.recording, what="녹화")
                await asyncio.sleep(0.6)
                server.expire_tokens()  # 주소 갱신
                await harness.until(lambda: '"event": "url_refresh"' in harness.log_text(), what="주소 갱신")
                await asyncio.sleep(0.5)
                await harness.engine.check_cookies("button")
                b.end()
                await harness.until(lambda: harness.finalized(1), what="마무리")

        asyncio.run(scenario())
        result["timemachine" if tm else "live"] = kinds(server.requests)
        server.broadcasts.clear()
    result["thumbnail"] = [[raw, thumbnail_url(raw)] for raw in (
        "https://livecloud-thumb.akamaized.net/chzzk/livecloud/KR/stream/1/live/2/record/3/thumbnail/image_{type}.jpg",
        "https://x.akamaized.net/a.jpg", "https://nng-phinf.pstatic.net/b.jpg", "http://x.akamaized.net/a.jpg",
        "https://evil.example/a.jpg", "https://x.akamaized.net/{size}/a.jpg", "", None)]
    return json.loads(json.dumps(result, sort_keys=True))


def test_requests_sent_to_chzzk_match_b12(tmp_path, server, tools):
    from conftest import Harness

    def make(name):
        return Harness(tmp_path / name, server, tools)

    got = run_scenarios(make, server)
    if os.environ.get("RECORD_B12") == "1":
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(json.dumps(got, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    want = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for k in ("live_detail", "time_machine", "cookie_check", "stream_master", "stream_media", "stream_segment"):
        assert want["timemachine"].get(k), f"기대값에 {k}가 없다"
    assert want["live"].get("live_detail") and not want["live"].get("time_machine")
    g, w = normalize(got), normalize(want)
    # 조각 토큰(질의)은 403을 받은 뒤에만 붙인다. 이번에 붙였는지는 시점에 달려 있으므로 예전 코드가 쓴 키 안에 드는지만 본다
    assert set(g.pop("segment_query_keys")) <= set(w.pop("segment_query_keys"))
    assert g == w


def normalize(data: dict) -> dict:
    """순서와 시점(재시도 중 조각 토큰을 붙였는지)에 흔들리지 않게 모은다. 조각 요청은 붙일 수 있는 질의 키만 견준다."""
    out = {"thumbnail": data["thumbnail"]}
    for mode in ("timemachine", "live"):
        kinds_ = {}
        for kind, shapes in data[mode].items():
            if kind == "stream_segment":
                shapes = [dict(s, path=re.sub(r"/[^/]+$", "/<file>", s["path"]), query_keys=[]) for s in shapes]
            kinds_[kind] = sorted({json.dumps(s, sort_keys=True) for s in shapes})
        out[mode] = kinds_
    out["segment_query_keys"] = sorted({k for mode in ("timemachine", "live") for s in data[mode].get("stream_segment", [])
                                        for k in s["query_keys"]})
    return out
