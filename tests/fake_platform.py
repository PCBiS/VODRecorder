"""시험용 가짜 플랫폼. 시험 폴더에만 있다.

치지직과 다른 점: 채널 ID 꼴(`fp` + 숫자 6자리), API 응답 모양(`/fakeplat/v1/streams/<ID>`의 `online`·`stream`),
자격 이름(`FP_TOKEN`, 머리글 `X-Fp-Token`으로 보낸다), 타임머신 없음, 조각 토큰 없음.
API는 conftest의 가짜 서버(`FakeChzzk.extra_get`)에 얹고, 재생목록·조각은 그 서버의 HLS를 그대로 쓴다.

수신기(별도 프로세스)는 이 모듈을 이름(`fake_platform`)으로 불러온다. `FAKE_PLATFORM_PROBE`가 있으면
주소를 갱신할 때 수신기 프로세스에 불려 와 있는 치지직 모듈 목록을 그 파일에 적는다(치지직 코드를 거치지 않았다는 증거).
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

from live_recorder.platforms.base import (ApiError, CheckResult, CredentialSpec, Features, LiveInfo, Playback, Verdict,
                                          host_allowed)
from live_recorder.core.quality import Track

ID_RE = re.compile(r"^fp[0-9]{6}$")
HANDLES: dict[str, tuple[str, str]] = {"@fakeone": ("fp000777", "가짜 이름표 채널")}  # 조회 시험
API_PATH = "/fakeplat/v1/streams/"


def stream_info(body: Any) -> dict | None:
    """가짜 API 응답: {"online": bool, "stream": {"sid", "name", "since"(epoch), "playlist", "picture", "mature"}}"""
    if not isinstance(body, dict) or not isinstance(body.get("online"), bool):
        raise ApiError("fakeplat: 모르는 응답")
    return body.get("stream") if body["online"] else None


class FakeClient:
    def __init__(self, base: str, allow_http: bool):
        self.base = base
        self.allow_http = allow_http
        self.http = httpx.AsyncClient(timeout=5)
        self.calls: list[str] = []

    def url(self, channel_id: str) -> str:
        return self.base + API_PATH + channel_id

    async def check_live(self, channel_id: str, credentials: dict[str, str] | None) -> CheckResult:
        self.calls.append("check_live")
        try:
            resp = await self.http.get(self.url(channel_id), headers=PLATFORM.request_headers("api", credentials))
            stream = stream_info(resp.json())
        except (httpx.HTTPError, ValueError, ApiError) as e:
            return CheckResult(Verdict.UNKNOWN, detail=f"fakeplat: {type(e).__name__}")
        if stream is None:
            return CheckResult(Verdict.OFFLINE)
        started = datetime.fromtimestamp(stream["since"], timezone.utc)
        info = LiveInfo(live_id=str(stream["sid"]), title=stream["name"], category="", adult=bool(stream.get("mature")),
                        open_date=started.isoformat(), image_url=stream.get("picture") or "", started_at=started,
                        tracks=[Track("hi", 1080, 60.0, 8_000_000), Track("lo", 720, 30.0, 3_000_000)],
                        hls_url=stream["playlist"], playback_live_id=str(stream["sid"]))
        return CheckResult(Verdict.LIVE, info)

    async def playback(self, channel_id: str, info: LiveInfo, credentials: dict[str, str] | None, *,
                       timemachine: bool) -> Playback:
        self.calls.append(f"playback tm={timemachine}")
        assert not timemachine, "타임머신이 없는 플랫폼에 타임머신을 청했다"
        return Playback(info.hls_url or "", "live", {"url": self.url(channel_id)})

    async def check_credentials(self, credentials: dict[str, str] | None) -> str:
        return "missing" if not credentials else "valid"

    async def lookup_channel(self, query: str, credentials: dict[str, str] | None) -> tuple[str, str]:
        """`@이름`을 가짜 서버의 이름표로 조회한다(없으면 까닭을 낸다)."""
        self.calls.append(f"lookup {query}")
        found = HANDLES.get(query.lower())
        if found is None:
            from live_recorder.platforms.base import ChannelParseError
            raise ChannelParseError("그 이름의 채널을 찾지 못했습니다")
        return found

    async def aclose(self) -> None:
        await self.http.aclose()


class FakePlatform:
    id = "fakeplat"
    name = "가짜 플랫폼"
    features = Features(timemachine=False, rewind=False)
    min_check_interval = 0.0
    tool_names: tuple[str, ...] = ()
    opt_in = False  # 늘 켜져 있다
    url_refresh_max = 0.0
    credential_spec = CredentialSpec(names=("FP_TOKEN",), title="가짜 토큰", help="시험용 토큰입니다.", checkable=False)
    thumbnail_hosts = ("fake-thumbs.example",)
    channel_hint = "https://fake.example/c/… 또는 fp000000"
    channel_id_error = "채널 ID는 fp와 숫자 6자리입니다"
    channel_rule = {"id_pattern": "^fp[0-9]{6}$", "hosts": ["fake.example"], "path_prefix": "c",
                    "error": "가짜 플랫폼 주소나 fp000000 꼴의 ID가 아닙니다"}
    log_mask = {"cookie_patterns": ("FP_TOKEN",), "query_params": (), "live_id_keys": ("sid",)}
    base = ""  # 시험이 가짜 서버 주소를 넣는다

    def parse_channel(self, text: str) -> str:
        value = str(text or "").strip().lower()
        if value.startswith("@") and len(value) > 1:
            from live_recorder.platforms.base import ChannelLookupNeeded
            raise ChannelLookupNeeded(value)  # 조회 단계를 시험한다
        if not ID_RE.match(value):
            raise ValueError(self.channel_rule["error"])
        return value

    def valid_channel_id(self, value: str) -> bool:
        return isinstance(value, str) and bool(ID_RE.match(value))

    def clean_credential_file(self, text: str) -> str:
        raise ValueError("이름 붙은 값으로 넣습니다")

    def credential_summary(self, values: dict[str, str]) -> dict[str, Any]:
        return {}

    def tool_versions(self) -> dict[str, str]:
        return {}

    def pick_credentials(self, text: str) -> tuple[dict[str, str], dict[str, Any]]:
        from live_recorder.platforms import cookiefile
        chosen = cookiefile.pick(cookiefile.parse(text), ("fake.example",), ("FP_TOKEN",))
        return {"FP_TOKEN": chosen["FP_TOKEN"].value}, {"names": ["FP_TOKEN"], "expires": None}

    def enable_blocked(self) -> str | None:
        return None

    def opt_in_notice(self, cfg: Any) -> dict[str, Any]:
        return {}

    def prepare_enable(self, cfg: Any) -> None:
        pass

    def startup(self, cfg: Any) -> None:
        pass

    def tool_status(self, cfg: Any) -> list[dict[str, Any]]:
        return []

    def tool_action(self, cfg: Any, name: str, action: str) -> dict[str, Any]:
        raise KeyError(name)

    def allowed_hosts(self, cfg: Any) -> tuple[str, ...]:
        return ("127.0.0.1",)

    def request_headers(self, kind: str, credentials: dict[str, str] | None) -> dict[str, str]:
        headers = {"User-Agent": "fakeplat-test"}
        if kind == "api" and credentials and credentials.get("FP_TOKEN"):
            headers["X-Fp-Token"] = credentials["FP_TOKEN"]
        return headers

    def thumbnail_url(self, raw: str | None) -> str | None:
        return raw if raw and host_allowed(raw, self.thumbnail_hosts) else None

    def client(self, cfg: Any) -> FakeClient:
        return FakeClient(self.base or os.environ["FAKE_PLATFORM_BASE"], bool(getattr(cfg, "allow_http", False)))

    # ---- 수신기 프로세스 쪽 ----
    def refresh_stream(self, http: httpx.Client, spec: dict[str, Any], credentials: dict[str, str] | None) -> str:
        probe = os.environ.get("FAKE_PLATFORM_PROBE")
        if probe:
            loaded = sorted(m for m in sys.modules if m.startswith("live_recorder.platforms.chzzk"))
            with open(probe, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"pid": os.getpid(), "chzzk_modules": loaded,
                                     "token": bool(credentials and credentials.get("FP_TOKEN"))}) + "\n")
        if os.environ.get("FAKE_PLATFORM_CHANGE"):
            from live_recorder.platforms.base import BroadcastChanged
            raise BroadcastChanged("fakeplat: 다른 방송")  # 시험
        resp = http.get(spec["url"], headers=self.request_headers("api", credentials))
        stream = stream_info(resp.json())
        if stream is None:
            raise ApiError("fakeplat: 방송이 끝났다")
        return stream["playlist"]

    def segment_token(self, master_url: str) -> tuple[str, str] | None:
        return None


PLATFORM = FakePlatform()


def serve(server) -> list[dict]:
    """conftest의 가짜 서버에 가짜 플랫폼 API를 얹는다. 받은 요청(경로·머리글 이름)을 돌려준다."""
    seen: list[dict] = []
    previous = server.extra_get

    def extra_get(path: str, headers):
        if not path.startswith(API_PATH):
            return previous(path, headers) if previous else None
        cid = path[len(API_PATH):].split("?", 1)[0]
        seen.append({"channel": cid, "token": headers.get("X-Fp-Token")})
        b = server.broadcasts.get(cid)
        if not b or not b.api_open:
            body = {"online": False}
        else:
            token = server._new_token(b)
            body = {"online": True, "stream": {
                "sid": b.live_id, "name": b.title, "since": b.open_date.timestamp(), "mature": False,
                "playlist": f"{server.base}/hls/{token}/live/master.m3u8",
                "picture": "https://cdn.fake-thumbs.example/p.jpg"}}
        data = json.dumps(body).encode()
        return 200, {"Content-Type": "application/json", "Content-Length": str(len(data))}, [data]

    server.extra_get = extra_get
    return seen
