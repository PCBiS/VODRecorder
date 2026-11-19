"""시험용 가짜 yt-dlp 파일. `live_recorder/platforms/youtube/ytdlp.py`와 같은 함수를 낸다.

실제 유튜브를 부르지 않는다. 해석은 conftest의 가짜 서버(`/fakeyt/extract`)에 묻는다. 그래서 수신기 프로세스(주소 갱신)도
같은 답을 받는다. 서버 쪽 답은 `serve(server)`가 정한다. 쿠키 글은 서버에 보내지 않고 "있는지"와 줄 수만 알린다.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

API = "/fakeyt/extract"
FILE_PATH = "/ytfile/"


@dataclass(frozen=True)
class Source:
    path: str | None = None
    label: str = "설치된 기본 판"

    def token(self) -> dict[str, Any]:
        return {"path": self.path, "label": self.label}

    @staticmethod
    def from_token(value: Any) -> "Source":
        if isinstance(value, dict):
            return Source(value.get("path"), str(value.get("label") or "설치된 기본 판"))
        return Source()


_selected = Source()


def use_source(source: Source) -> None:
    global _selected
    _selected = source


def select_source() -> Source:
    return _selected


@dataclass
class Outcome:
    kind: str
    info: dict[str, Any] | None = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def version(source: Source | None = None) -> str:
    return "0.0.fake"


def describe(source: Source | None = None) -> str:
    return f"{version(source)} ({(source or Source()).label})"


def extra_hosts() -> tuple[str, ...]:
    return ("127.0.0.1",)


def note_startup() -> None:
    pass


def extract(url: str, cookies_text: str | None = None, *, flat: bool = False, source: Source | None = None) -> Outcome:
    base = os.environ["FAKE_YTDLP_BASE"]
    lines = len([x for x in (cookies_text or "").splitlines() if "\t" in x])  # 쿠키 줄 수(값은 보내지 않는다)
    query = urllib.parse.urlencode({"url": url, "flat": int(flat), "cookie_lines": lines, "pid": os.getpid()})
    with urllib.request.urlopen(f"{base}{API}?{query}", timeout=10) as resp:
        body = json.loads(resp.read())
    return Outcome(body["kind"], body.get("info"), body.get("message", ""))


# ---- 가짜 서버 쪽 ----
def video_id(live_id: str) -> str:
    return ("v" + str(live_id).rjust(10, "0"))[:11]


def serve(server, handles: dict[str, tuple[str, str]] | None = None) -> list[dict]:
    """가짜 서버에 해석 API를 얹는다. `server.yt_modes[채널 ID]`로 답을 바꾼다:
    "login"(로그인 필요), "bot", "rate", "scheduled", "error". 받은 요청(주소·쿠키 줄 수·프로세스)을 돌려준다."""
    seen: list[dict] = []
    server.yt_modes = {}
    previous = server.extra_get
    handles = handles or {}

    def answer(params: dict[str, str]) -> dict:
        url, flat, lines = params["url"], params.get("flat") == "1", int(params.get("cookie_lines") or 0)
        seen.append({"url": url, "flat": flat, "cookie_lines": lines, "pid": int(params.get("pid") or 0)})
        path = urllib.parse.urlsplit(url).path.strip("/").split("/")
        if path == ["watch"]:
            return watch(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("v", [""])[0], lines)
        if flat:
            found = handles.get(path[0].lower()) if path else None
            if not found:
                return {"kind": "notfound", "message": "ERROR: [youtube:tab] This channel does not exist."}
            return {"kind": "ok", "info": {"_type": "playlist", "id": found[0], "channel_id": found[0], "channel": found[1]}}
        cid = path[1] if len(path) >= 3 and path[0] == "channel" else ""
        mode = server.yt_modes.get(cid)
        if mode == "login":
            if lines and server.yt_cookie_ok:
                mode = None
            else:
                return {"kind": "login", "message": "ERROR: This video is available to this channel's members"}
        if mode == "bot":
            return {"kind": "bot", "message": "ERROR: Sign in to confirm you're not a bot"}
        if mode == "rate":
            return {"kind": "rate", "message": "ERROR: HTTP Error 429: Too Many Requests"}
        if mode == "error":
            return {"kind": "error", "message": "ERROR: Something odd"}
        if mode == "scheduled":
            return {"kind": "offline", "message": "ERROR: This live event will begin in 3 hours."}
        b = server.broadcasts.get(cid)
        if not b or not b.api_open:
            return {"kind": "offline", "message": "ERROR: The channel is not currently live"}
        token = server._new_token(b)
        master = f"{server.base}/hls/{token}/live/master.m3u8"
        formats = [{"format_id": str(h), "height": h, "fps": 30.0, "tbr": tbr, "vcodec": "avc1", "acodec": "none",
                    "protocol": "m3u8_native", "manifest_url": master} for h, tbr in ((720, 3192.0), (1080, 8384.0))]
        formats.append({"format_id": "a", "height": None, "vcodec": "none", "acodec": "mp4a", "protocol": "m3u8_native",
                        "manifest_url": master})
        return {"kind": "ok", "info": {"id": video_id(b.live_id), "title": b.title, "is_live": True, "live_status": "is_live",
                                       "release_timestamp": b.open_date.timestamp(), "age_limit": 18 if b.adult else 0,
                                       "thumbnail": "https://i.ytimg.com/vi/x/hqdefault_live.jpg", "channel_id": cid,
                                       "formats": formats}}

    def watch(vid: str, lines: int) -> dict:
        """일반 영상. server.yt_videos[ID] = {"title", "channel", "mode"(login·rate·live·upcoming), "files": {...}}."""
        v = server.yt_videos.get(vid)
        server.yt_watch_calls.append(vid)
        if v is None:
            return {"kind": "notfound", "message": "ERROR: Video unavailable. This video does not exist"}
        mode = v.get("mode")
        if mode == "login" and not (lines and v.get("cookie_ok", True)):
            return {"kind": "login", "message": "ERROR: Sign in to confirm your age"}
        if mode == "rate":
            return {"kind": "rate", "message": "ERROR: This content isn't available, try again later."}
        live = {"live": "is_live", "upcoming": "is_upcoming"}.get(mode, "not_live")
        formats = []
        for fid, f in v.get("files", {}).items():
            name = f"{vid}-{fid}-g{server.yt_generation}"
            server.yt_files[name] = f["data"]
            formats.append({"format_id": fid, "protocol": "https", "url": f"{server.base}{FILE_PATH}{name}",
                            "vcodec": f.get("vcodec", "none"), "acodec": f.get("acodec", "none"), "height": f.get("height"),
                            "width": f.get("width"), "fps": f.get("fps"), "tbr": f.get("tbr"), "audio_channels": f.get("channels"),
                            "filesize": len(f["data"]) if f.get("size_known", True) else None,
                            "http_headers": {"User-Agent": "fake-yt-agent", "Accept": "*/*"}})
        return {"kind": "ok", "info": {"id": vid, "title": v.get("title", "영상"), "channel": v.get("channel", "가짜 채널"),
                                       "release_timestamp": v.get("ts"), "upload_date": v.get("upload_date"),
                                       "duration": v.get("duration", 10), "live_status": live, "is_live": live == "is_live",
                                       "age_limit": 0, "formats": formats}}

    def serve_file(path: str, headers):
        """범위 요청을 받는 가짜 googlevideo. server.yt_fail[이름] = [상태, …]이면 그 상태를 차례로 낸다."""
        name = path[len(FILE_PATH):].split("?", 1)[0]
        server.yt_file_requests.append({"name": name, "range": headers.get("Range"), "cookie": headers.get("Cookie"),
                                        "agent": headers.get("User-Agent")})
        queue = server.yt_fail.get(name.rsplit("-g", 1)[0]) or []
        if queue:
            status = queue.pop(0)
            return status, {"Content-Length": "0"}, [b""]
        data = server.yt_files.get(name)
        if data is None:
            return 404, {"Content-Length": "0"}, [b""]
        rng = headers.get("Range")
        if server.yt_delay:
            time.sleep(server.yt_delay)
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start, end = int(a), min(int(b) if b else len(data) - 1, len(data) - 1)
            body = data[start:end + 1]
            return 206, {"Content-Length": str(len(body)), "Content-Range": f"bytes {start}-{end}/{len(data)}",
                         "Content-Type": "video/mp4"}, [body]
        return 200, {"Content-Length": str(len(data)), "Content-Type": "video/mp4"}, [data]

    def extra_get(path: str, headers):
        if path.startswith(FILE_PATH):
            return serve_file(path, headers)
        if not path.startswith(API):
            return previous(path, headers) if previous else None
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query))
        data = json.dumps(answer(params)).encode()
        return 200, {"Content-Type": "application/json", "Content-Length": str(len(data))}, [data]

    server.yt_cookie_ok = True
    server.yt_videos = {}
    server.yt_files = {}
    server.yt_fail = {}
    server.yt_delay = 0.0
    server.yt_generation = 0  # 다시 분석할 때 주소를 바꾸고 싶으면 올린다
    server.yt_watch_calls = []
    server.yt_file_requests = []
    server.extra_get = extra_get
    return seen
