"""쿠키 노출. 녹화 중 프로세스 인자·환경변수·로그·저장소에 쿠키가 없다."""

import asyncio
import os
from pathlib import Path


from conftest import CHANNEL
from live_recorder.platforms import credentials

AUT, SES = "AUT-s3cr3t-sentinel-0001", "SES-s3cr3t-sentinel-0002"


def descendants(pid: int) -> list[int]:
    """이 시험 프로세스가 띄운 모든 프로세스(수신기 포함)."""
    out = []
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            kids = [int(x) for x in (task / "children").read_text().split()]
        except OSError:
            continue
        for kid in kids:
            out.append(kid)
            out.extend(descendants(kid))
    return out


def test_cookies_never_leak(harness, server):
    harness.add_channel()
    harness.store.set_settings({"rewind_seconds": 5})
    credentials.save(harness.cfg, "chzzk", {"NID_AUT": AUT, "NID_SES": SES})
    b = server.go_live(initial=20)
    seen = {}

    async def scenario():
        async with harness:
            rec = await harness.until(harness.recording, what="녹화")
            pid = rec["pid"]
            seen["cmdline"] = Path(f"/proc/{pid}/cmdline").read_bytes()
            seen["environ"] = Path(f"/proc/{pid}/environ").read_bytes()
            seen["all"] = b"".join(Path(f"/proc/{p}/cmdline").read_bytes() for p in descendants(os.getpid()))
            await asyncio.sleep(0.5)
            server.expire_tokens()  # 주소 갱신 경로도 지나게 한다
            await asyncio.sleep(0.8)
            b.end()
            await harness.until(lambda: harness.finalized(1), what="마무리")

    asyncio.run(scenario())
    assert b"live_recorder.recording.receiver" in seen["cmdline"]
    for blob in (seen["cmdline"], seen["environ"], seen["all"]):
        assert AUT.encode() not in blob and SES.encode() not in blob
    # 쿠키는 실제로 쓰였다: API 요청에만 실리고 CDN(재생목록·조각) 요청에는 실리지 않는다
    api = [c for p, c in server.calls if p.startswith("/api/")]
    hls = [c for p, c in server.calls if p.startswith("/hls/")]
    assert api and all(c == f"NID_AUT={AUT}; NID_SES={SES}" for c in api)
    assert hls and all(c is None for c in hls)
    # 로그, 저장소, 데이터 폴더의 다른 어떤 파일에도 없다
    log = harness.log_text()
    assert log and AUT not in log and SES not in log
    assert "hmac=" not in log and "hdnts" not in log  # 토큰이 든 재생 주소도 로그에 없다
    for path in harness.data.rglob("*"):
        if path.is_file() and path != credentials.path(harness.cfg, "chzzk"):
            blob = path.read_bytes()
            assert AUT.encode() not in blob and SES.encode() not in blob, path
    for path in harness.root.rglob("*"):
        if path.is_file() and path.stat().st_size < 5_000_000:
            assert AUT.encode() not in path.read_bytes()
    assert oct(os.stat(credentials.path(harness.cfg, "chzzk")).st_mode & 0o777) == "0o600"
    rows = harness.sessions()
    assert AUT not in repr(rows) and SES not in repr(rows)
