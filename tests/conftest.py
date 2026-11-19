"""시험 도구: ffmpeg로 만든 합성 HLS 조각, 가짜 치지직 서버, 엔진 하네스.

플랫폼에 기대지 않는다. 모든 요청은 127.0.0.1의 가짜 서버로 간다.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from live_recorder.core import logs  # noqa: E402
from live_recorder.core import store as st  # noqa: E402
from live_recorder.core.config import AppConfig  # noqa: E402
from live_recorder.recording.engine import Engine, Timings  # noqa: E402

# 다시보기·클립 확장 셋(다시보기 시험은 모두 켜고 띄운다)
REPLAY_EXTENSIONS = ("downloads", "replay", "clips")
CHANNEL = "0123456789abcdef0123456789abcdef"
KST = timezone(timedelta(hours=9))


def _find_tools() -> tuple[str, str]:
    ff, fp = os.environ.get("LIVE_RECORDER_FFMPEG"), os.environ.get("LIVE_RECORDER_FFPROBE")
    if ff and fp:
        return ff, fp
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ff and fp:
        return ff, fp
    try:
        from static_ffmpeg import run  # type: ignore

        return run.get_or_fetch_platform_executables_else_raise()
    except Exception:  # noqa: BLE001
        pytest.exit("ffmpeg와 ffprobe가 필요하다(LIVE_RECORDER_FFMPEG, LIVE_RECORDER_FFPROBE)", 2)
        raise


@pytest.fixture(scope="session")
def tools() -> tuple[str, str]:
    return _find_tools()


@pytest.fixture(scope="session")
def media(tools, tmp_path_factory) -> Path:
    """1초짜리 fMP4 조각 400개(치지직과 같은 형식)."""
    out = tmp_path_factory.mktemp("media")
    subprocess.run(
        [tools[0], "-nostdin", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050",
         "-t", "400", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-g", "10", "-keyint_min", "10", "-sc_threshold", "0", "-c:a", "aac", "-b:a", "24k",
         "-f", "hls", "-hls_time", "1", "-hls_list_size", "0", "-hls_segment_type", "fmp4",
         "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", str(out / "seg%05d.m4s"),
         str(out / "all.m3u8")],
        check=True, cwd=out,
    )
    assert (out / "init.mp4").exists() and len(list(out.glob("seg*.m4s"))) >= 390
    return out


def id3_timestamp(pts90k: int) -> bytes:
    """HLS 묶음 음성(packed audio) 조각 앞의 ID3 머리말(com.apple.streaming.transportStreamTimestamp)."""
    owner = b"com.apple.streaming.transportStreamTimestamp\x00"
    body = owner + pts90k.to_bytes(8, "big")

    def syncsafe(n: int) -> bytes:
        return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])

    frame = b"PRIV" + syncsafe(len(body)) + b"\x00\x00" + body
    return b"ID3\x04\x00\x00" + syncsafe(len(frame)) + frame


@pytest.fixture(scope="session")
def split_media(tools, tmp_path_factory) -> Path:
    """흉내. 영상만 든 MPEG-TS 조각과 ID3 머리말이 붙은 AAC 조각(1초씩 120개)."""
    out = tmp_path_factory.mktemp("split")
    common = [tools[0], "-nostdin", "-hide_banner", "-loglevel", "error"]
    subprocess.run(common + ["-f", "lavfi", "-i", "testsrc=size=160x90:rate=10", "-t", "120",
                             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                             "-g", "10", "-keyint_min", "10", "-sc_threshold", "0", "-an",
                             "-f", "segment", "-segment_time", "1", "-segment_format", "mpegts",
                             str(out / "v%05d.ts")], check=True, cwd=out)
    raw = out / "raw"
    raw.mkdir()
    subprocess.run(common + ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "120",
                             "-c:a", "aac", "-b:a", "24k", "-f", "segment", "-segment_time", "1",
                             "-segment_format", "adts", str(raw / "a%05d.aac")], check=True, cwd=out)
    for seg in sorted(raw.glob("a*.aac")):
        i = int(seg.stem[1:])
        (out / seg.name).write_bytes(id3_timestamp(i * 90000) + seg.read_bytes())
    assert len(list(out.glob("v*.ts"))) >= 115 and len(list(out.glob("a*.aac"))) >= 115
    return out


class Broadcast:
    def __init__(self, live_id: str, title: str = "시험 방송", *, initial: int = 0, rate: float = 10.0,
                 total: int = 390, adult: bool = False):
        self.live_id = live_id
        self.title = title
        self.adult = adult
        self.initial = initial
        self.rate = rate
        self.total = total
        self.t0 = time.monotonic()
        self.frozen: int | None = None
        self.ended = False  # 재생목록에 ENDLIST
        self.api_open = True
        self.base_pdt = datetime(2026, 11, 11, 3, 0, 0, tzinfo=timezone.utc)
        self.open_date = self.base_pdt.astimezone(KST)
        self.tokens: set[str] = set()

    def published(self) -> int:
        if self.frozen is not None:
            return self.frozen
        return min(self.total, self.initial + int((time.monotonic() - self.t0) * self.rate))

    def freeze(self) -> None:
        self.frozen = self.published()

    def end(self) -> None:
        """방송 종료: 조각이 멈추고 ENDLIST가 붙고 API가 CLOSE가 된다."""
        self.freeze()
        self.ended = True
        self.api_open = False

    def pdt(self, index: int) -> datetime:
        return self.base_pdt + timedelta(seconds=index)


class FakeChzzk:
    """치지직 API와 HLS CDN 흉내."""

    def __init__(self, media_dir: Path):
        self.media_dir = media_dir
        self.broadcasts: dict[str, Broadcast | None] = {}
        self.tm_fail = False
        self.down = False  # 재생목록·조각 요청이 전부 503
        self.live_window = 6
        self.tm_window = 3600
        self.detail_queue: list[tuple[int, dict | None]] = []
        self.calls: list[tuple[str, str | None]] = []
        # 받은 요청의 경로·질의 키·머리글(요청 비교 시험이 쓴다)
        self.requests: list[dict] = []
        self.tm_calls: list[int] = []  # 타임머신 API가 불릴 때마다 그때 나와 있던 조각 수
        self.encrypted = False  # 재생목록에 EXT-X-KEY를 넣는다(수신기가 지원하지 않는 형식)
        self.extra_get = None  # 다시보기 가짜 서버가 끼어드는 자리(tests/replay_fake.py)
        self.extra_post = None  # POST가 끼어드는 자리(path, headers, body) -> (상태, [(머리글, 값)…], 본문) | None
        self.split: Path | None = None  # 영상·음성을 따로 두는 마스터(split_media 조각)
        self.audio_frozen: int | None = None  # 따로 두는 음성 재생목록을 이 조각 수에서 멈춘다
        # 가짜 텔레그램
        self.tg_messages: list[dict] = []
        self.tg_paths: list[str] = []
        self.tg_status = 200
        self.tg_hold = threading.Event()  # 풀릴 때까지 응답하지 않는다
        self.tg_hold.set()
        # 가짜 쿠키 확인 API
        self.user_status: tuple[int, dict] = (200, {"code": 200, "content": {"loggedIn": True, "userIdHash": "abc"}})
        self._token_n = 0
        self._lock = threading.Lock()
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.handle_error = lambda *args: None  # 수신기가 끊고 나간 연결은 조용히 넘긴다
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.tg_hold.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- 시험에서 쓰는 조작 ----
    def go_live(self, channel_id: str = CHANNEL, live_id: str = "1001", **kwargs) -> Broadcast:
        b = Broadcast(live_id, **kwargs)
        self.broadcasts[channel_id] = b
        return b

    def expire_tokens(self) -> None:
        for b in self.broadcasts.values():
            if b:
                b.tokens.clear()

    def count(self, prefix: str) -> int:
        return sum(1 for path, _ in self.calls if path.startswith(prefix))

    def _new_token(self, b: Broadcast) -> str:
        with self._lock:
            self._token_n += 1
            token = f"tok{self._token_n}"
        b.tokens.add(token)
        return token

    def _find(self, live_id: str) -> Broadcast | None:
        return next((b for b in self.broadcasts.values() if b and b.live_id == live_id), None)

    def _by_token(self, token: str) -> Broadcast | None:
        return next((b for b in self.broadcasts.values() if b and token in b.tokens), None)

    # ---- 응답 만들기 ----
    def _tracks(self) -> list[dict]:
        return [
            {"encodingTrackId": "720p", "videoHeight": 720, "videoFrameRate": "30.0", "videoBitRate": 3000000, "audioOnly": None},
            {"encodingTrackId": "1080p", "videoHeight": 1080, "videoFrameRate": "60.0", "videoBitRate": 8000000, "audioOnly": None},
            {"encodingTrackId": "audioOnly", "videoHeight": None, "videoFrameRate": None, "videoBitRate": None, "audioOnly": True},
        ]

    def _playback(self, b: Broadcast, kind: str) -> dict:
        token = self._new_token(b)
        path = f"{self.base}/hls/{token}/{kind}/master.m3u8?hdnts=st=1~exp=2~hmac={token}"
        open_text = b.open_date.strftime("%Y-%m-%dT%H:%M:%S")
        return {"meta": {"liveId": b.live_id},
                "live": {"open": open_text, "start": open_text, "status": "STARTED"},
                "media": [{"mediaId": "HLS", "protocol": "HLS", "path": path, "encodingTrack": self._tracks()}]}

    def live_detail(self, channel_id: str) -> tuple[int, dict]:
        if self.detail_queue:
            status, body = self.detail_queue.pop(0)
            return status, body if body is not None else {"code": status}
        if channel_id not in self.broadcasts:
            return 404, {"code": 404, "message": "채널이 존재하지 않습니다."}
        b = self.broadcasts[channel_id]
        if b is None:
            return 200, {"code": 200, "content": None}
        if not b.api_open:
            playback = {"live": {"status": "ENDED"}, "media": []}
            return 200, {"code": 200, "content": {"status": "CLOSE", "liveId": int(b.live_id),
                                                   "livePlaybackJson": json.dumps(playback)}}
        content = {
            "status": "OPEN", "liveId": int(b.live_id), "liveTitle": b.title, "liveCategoryValue": "시험",
            "adult": b.adult, "openDate": b.open_date.strftime("%Y-%m-%d %H:%M:%S"),
            "liveImageUrl": f"{self.base}/thumb/image_{{type}}.jpg", "timeMachineActive": True,
            "livePlaybackJson": json.dumps(self._playback(b, "live")),
        }
        return 200, {"code": 200, "content": content}

    def time_machine(self, live_id: str) -> tuple[int, dict]:
        b = self._find(live_id)
        self.tm_calls.append(b.published() if b else -1)
        if self.tm_fail:
            return 500, {"code": 500, "message": "boom"}
        if b is None:
            return 404, {"code": 404, "message": "존재하지 않는 라이브입니다."}
        return 200, {"code": 200, "content": {"openDate": "", "playback": self._playback(b, "tm")}}

    def media_playlist(self, b: Broadcast, kind: str) -> str:
        n = b.published()
        window = self.live_window if kind == "live" else self.tm_window
        first = max(0, n - window)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{first}",
                 '#EXT-X-MAP:URI="init.mp4"']
        if self.encrypted:
            lines.append('#EXT-X-KEY:METHOD=AES-128,URI="key.bin"')
        for i in range(first, n):
            lines.append("#EXT-X-PROGRAM-DATE-TIME:" + b.pdt(i).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            lines.append("#EXTINF:1.000000,")
            lines.append(f"seg{i:05d}.m4s")
        if b.ended:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def split_playlist(self, b: Broadcast, kind: str, track: str) -> str:
        n = b.published()
        if track == "a" and self.audio_frozen is not None:
            n = min(n, self.audio_frozen)
        window = self.live_window if kind == "live" else self.tm_window
        first = max(0, n - window)
        ext = "ts" if track == "v" else "aac"
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        for i in range(first, n):
            lines.append("#EXT-X-PROGRAM-DATE-TIME:" + b.pdt(i).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            lines.append("#EXTINF:1.000000,")
            lines.append(f"{track}{i:05d}.{ext}")
        if b.ended and not (track == "a" and self.audio_frozen is not None):
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    SPLIT_MASTER = (
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="low",DEFAULT=NO,URI="alow/media.m3u8"\n'
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a1",NAME="main",DEFAULT=YES,URI="aud/media.m3u8"\n'
        "#EXT-X-STREAM-INF:BANDWIDTH=3192000,RESOLUTION=1280x720,AUDIO=\"a1\"\nv720/media.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=8384000,RESOLUTION=1920x1080,AUDIO=\"a1\"\nv1080/media.m3u8\n"
    )

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # 머리글과 본문을 따로 쓰므로 Nagle이 켜져 있으면 응답마다 지연 ACK(약 40ms)를 기다린다.
            # 시험 서버가 느려지면 시간에 기대는 시험(되감기 길이 등)이 흔들린다
            disable_nagle_algorithm = True

            def log_message(self, *args):  # 조용히
                pass

            def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    if server.extra_post is not None:
                        handled = server.extra_post(self.path, self.headers, raw)
                        if handled is not None:
                            status, headers, body = handled
                            self.send_response(status)
                            for k, v in (headers.items() if isinstance(headers, dict) else headers):
                                self.send_header(k, v)
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            return
                    if path.startswith("/tg/bot") and path.endswith("/sendMessage"):
                        server.tg_paths.append(path)
                        server.tg_hold.wait(60)
                        if server.tg_status == 200:
                            server.tg_messages.append(json.loads(raw))
                        return self._send(server.tg_status, json.dumps({"ok": server.tg_status == 200}).encode())
                    return self._send(404, b"{}")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                server.calls.append((path, self.headers.get("Cookie")))
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                server.requests.append({"path": path, "query": query, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if server.extra_get is not None:
                    try:
                        handled = server.extra_get(self.path, self.headers)
                    except Exception as e:  # noqa: BLE001
                        handled = (500, {}, [str(e).encode()])
                    if handled is not None:
                        status, headers, body = handled
                        headers = dict(headers)
                        if isinstance(body, list):
                            headers.setdefault("Content-Length", str(sum(len(c) for c in body)))
                        elif "Content-Length" not in headers:
                            headers["Connection"] = "close"
                            self.close_connection = True
                        try:
                            self.send_response(status)
                            for k, v in headers.items():
                                self.send_header(k, v)
                            self.end_headers()
                            for chunk in body:
                                self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass
                        return
                parts = path.strip("/").split("/")
                try:
                    if parts[0] == "api" and parts[-1] == "user-status":
                        return self._send(server.user_status[0], json.dumps(server.user_status[1]).encode())
                    if parts[0] == "api" and parts[-1] == "live-detail":
                        status, body = server.live_detail(parts[4])
                        return self._send(status, json.dumps(body).encode())
                    if parts[0] == "api" and parts[-1] == "time-machine":
                        status, body = server.time_machine(parts[4])
                        return self._send(status, json.dumps(body).encode())
                    if parts[0] == "hls":
                        if server.down:
                            return self._send(503, b"down", "text/plain")
                        b = server._by_token(parts[1])
                        if b is None:
                            return self._send(403, b"expired", "text/plain")
                        kind = parts[2]
                        if server.split is not None:
                            if parts[3] == "master.m3u8":
                                return self._send(200, server.SPLIT_MASTER.encode(), "application/vnd.apple.mpegurl")
                            name = parts[4]
                            track = "a" if parts[3] in ("aud", "alow") else "v"
                            if parts[3] == "alow":
                                return self._send(404, b"not this one", "text/plain")
                            if name == "media.m3u8":
                                return self._send(200, server.split_playlist(b, kind, track).encode(),
                                                  "application/vnd.apple.mpegurl")
                            limit = b.published()
                            if track == "a" and server.audio_frozen is not None:
                                limit = min(limit, server.audio_frozen)
                            if not name.startswith(track) or int(name[1:6]) >= limit:
                                return self._send(404, b"not yet", "text/plain")
                            return self._send(200, (server.split / name).read_bytes(), "video/mp2t")
                        if parts[3] == "master.m3u8":
                            text = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=3192000,RESOLUTION=1280x720\nv720/media.m3u8\n"
                                    "#EXT-X-STREAM-INF:BANDWIDTH=8384000,RESOLUTION=1920x1080\nv1080/media.m3u8\n")
                            return self._send(200, text.encode(), "application/vnd.apple.mpegurl")
                        name = parts[4]
                        if name == "media.m3u8":
                            return self._send(200, server.media_playlist(b, kind).encode(), "application/vnd.apple.mpegurl")
                        if name == "init.mp4" or name.startswith("seg"):
                            if name.startswith("seg") and int(name[3:8]) >= b.published():
                                return self._send(404, b"not yet", "text/plain")
                            return self._send(200, (server.media_dir / name).read_bytes(), "video/mp4")
                    return self._send(404, b"{}")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:  # noqa: BLE001
                    try:
                        self._send(500, str(e).encode(), "text/plain")
                    except OSError:
                        pass

        return Handler


@pytest.fixture
def server(media):
    s = FakeChzzk(media)
    yield s
    s.close()


def fast_timings(**overrides) -> Timings:
    values = dict(
        backoff_steps=(0.2, 0.5, 1.0), max_same_failures=3, stop_grace=4.0, post_end_suppress=1.0,
        meta_retry_interval=0.1, meta_retry_max=5, control_poll=0.1, idle_poll=0.1, monitor_poll=0.1,
        progress_save=0.2, shutdown_deadline=10.0, segment_timeout=3.0, segment_attempts=3, stream_timeout=4.0,
        refresh_attempts=3, refresh_delay=0.1, reload_interval=0.1, check_interval_override=0.3, stall_override=30.0,
        endlist_wait_override=0.2,
        gate_interval_override=0.0, gate_front_floor=0.0,  # 시험에서는 창구 간격을 없앤다
    )
    values.update(overrides)
    return Timings(**values)


class Harness:
    def __init__(self, tmp_path: Path, server: FakeChzzk, tools: tuple[str, str], *, timings: Timings | None = None,
                 receiver_cmd: tuple[str, ...] | None = None, ffmpeg: str | None = None, disk_free=None):
        self.server = server
        self.tools = tools
        self.data = tmp_path / "data"
        self.root = tmp_path / "rec"
        self.root.mkdir(parents=True, exist_ok=True)
        kwargs = {}
        if receiver_cmd:
            kwargs["receiver_cmd"] = receiver_cmd
        self.cfg = AppConfig(data_dir=self.data, record_root=self.root, ffmpeg=ffmpeg or tools[0], ffprobe=tools[1],
                             api_base=server.base + "/api", allowed_hosts=("127.0.0.1",), allow_http=True,
                             telegram_api_base=server.base + "/tg", user_status_url=server.base + "/api/user-status",
                             **kwargs)
        self.timings = timings or fast_timings()
        self.disk_free = disk_free
        self.data.mkdir(parents=True, exist_ok=True)
        self.store = st.Store(self.cfg.db_path, self.root, self.cfg.telegram_path)
        self.engine: Engine | None = None

    def enable_telegram(self, token: str = "123456:TEST-TOKEN-sentinel", chat_id: str = "-1009999", **settings) -> None:
        from live_recorder.core import secretstore
        secretstore.telegram_save(self.cfg.telegram_path, token, chat_id)
        self.store.set_settings({"notify_enabled": True, **settings})

    def add_channel(self, channel_id: str = CHANNEL, name: str = "시험채널", **kwargs) -> st.Channel:
        return self.store.add_channel(channel_id, name, kwargs.pop("output_dir", "ch1"), **kwargs)

    async def __aenter__(self) -> "Harness":
        logs.setup(self.cfg.log_dir, console=False)
        self.engine = Engine(self.cfg, self.timings, disk_free=self.disk_free)
        await self.engine.start()
        return self

    async def __aexit__(self, *exc) -> None:
        assert self.engine
        await self.engine.stop()
        logs.setup(None, console=False)

    async def until(self, pred, timeout: float = 20.0, what: str = "조건"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = pred()
            if value:
                return value
            await asyncio.sleep(0.05)
        raise AssertionError(f"시간 안에 만족하지 못했다: {what}; 세션={self.brief()}")

    def sessions(self, channel_id: str | None = None) -> list[dict]:
        return self.store.list_sessions(channel_id, 500)

    def brief(self) -> list[tuple]:
        return [(s["id"], s["end_reason"], s["end_detail"], s["bytes"], s["finalize_status"], s["finalize_detail"])
                for s in self.sessions()]

    def state(self, channel_id: str = CHANNEL) -> dict:
        return self.store.get_state(channel_id)

    def recording(self) -> dict | None:
        return next((s for s in self.sessions() if s["ended_at"] is None and (s["bytes"] or self._part_size(s) > 0)), None)

    @staticmethod
    def _part_size(session: dict) -> int:
        try:
            return os.path.getsize(session["part_path"])
        except (OSError, TypeError):
            return 0

    def finalized(self, n: int = 1) -> list[dict] | None:
        done = [s for s in self.sessions() if s["finalize_status"] in ("done", "failed")]
        return done if len(done) >= n else None

    def log_text(self) -> str:
        return "".join(p.read_text(encoding="utf-8") for p in sorted(self.cfg.log_dir.glob("*.log*")))


@pytest.fixture
def harness(tmp_path, server, tools):
    return Harness(tmp_path, server, tools)


def probe(tools: tuple[str, str], path: Path) -> dict:
    out = subprocess.run([tools[1], "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def duration(tools: tuple[str, str], path: Path) -> float:
    return float(probe(tools, path)["format"]["duration"])


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


TEST_TIME_LIMIT = 300  # 초. 시험 하나가 이보다 오래 걸리면 멈춘 것으로 보고 실패로 끝낸다(전체 시험이 끝없이 멈추지 않게)


@pytest.fixture(autouse=True)
def test_time_limit():
    import signal

    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGALRM"):
        yield
        return

    def expired(signum, frame):
        raise TimeoutError(f"시험이 {TEST_TIME_LIMIT}초 안에 끝나지 않았다")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(TEST_TIME_LIMIT)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture(autouse=True)
def pass_fake_env_to_children(monkeypatch):
    """시험용 가짜 플랫폼·가짜 yt-dlp가 읽는 변수(FAKE_…)도 하위 프로세스에 넘긴다(제품은 정한 변수만 넘긴다)."""
    from live_recorder.core import childenv

    monkeypatch.setattr(childenv, "PASS_PREFIXES", childenv.PASS_PREFIXES + ["FAKE_", "COVERAGE_"])
