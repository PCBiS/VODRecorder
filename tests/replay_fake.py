"""다시보기 시험용 가짜 서버. 치지직 영상 API, neonplayer MPD, 영상 CDN을 흉내 낸다.

- 단일 파일(MP4): 범위 요청을 받는다. 주소의 서명(`_lsu_sa_`)이 맞아야 준다.
- HLS형(TS 조각): 재생목록의 조각 줄에는 서명이 없다. 조각 요청에 서명이 없으면 403이다(질의 물려주기 확인).
- 빠른 다시보기(HLS): 경로에 토큰이 든 주소. 조각은 상대 주소다.
- fMP4 HLS(초기화 조각 EXT-X-MAP): 조각 단위 이어받기 시험용(`/vod/fm4/<토큰>/media.m3u8`).
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SIG = "SYNTHETIC-SIGNATURE-0001"
FAST_TOKEN = "hdntl=exp=1~hmac=FASTTOKEN"
DASH_NO, FAST_NO, ADULT_NO, BROKEN_NO = "100", "200", "300", "400"


def build_media(ffmpeg: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    common = ["-f", "lavfi", "-i", "testsrc=size=160x90:rate=10", "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=22050",
              "-t", "120", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "20", "-c:a", "aac", "-b:a", "24k"]
    subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *common, "-movflags", "+faststart",
                    str(out / "file.mp4")], check=True)
    hls = out / "hls"
    hls.mkdir(exist_ok=True)
    subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *common, "-f", "hls", "-hls_time", "2",
                    "-hls_list_size", "0", "-hls_playlist_type", "vod", "-hls_segment_filename", str(hls / "seg%03d.ts"),
                    str(hls / "media.m3u8")], check=True)
    fm4 = out / "fm4"
    fm4.mkdir(exist_ok=True)
    subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *common, "-f", "hls", "-hls_time", "2",
                    "-hls_list_size", "0", "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4",
                    "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "seg%03d.m4s", "media.m3u8"],
                   check=True, cwd=fm4)


class FakeVod:
    def __init__(self, server, media: Path):
        self.server = server
        self.media = media
        self.base = server.base
        self.delay = 0.0  # 조각(64 KiB)마다 쉬는 시간. 취소 시험에서 다운로드를 늦춘다
        self.broken_media = False  # 영상 대신 쓰레기를 준다
        self.media_status = 200
        self.ranges: list[str] = []
        self.media_cookies: list[str | None] = []
        self.lock = threading.Lock()
        self.file_bytes = (media / "file.mp4").read_bytes()
        self.list_calls = 0  # 채널 다시보기 목록 호출 수
        # 채널 정보 API. 이름 표에 없는 채널은 치지직처럼 code 200에 빈 채널(channelId null)로 답한다
        self.channel_names: dict[str, str] = {}
        self.channel_status = 200  # 200이 아니면 그 상태로 답한다(이름을 받지 못한 경우)
        self.channel_calls: list[str] = []
        self.segments: list[str] = []  # 받아 간 HLS 조각 이름(이어받기 시험)
        self.no_range = False  # 범위 요청을 받지 않는 서버
        server.extra_get = self.handle

    # ---- 응답 ----
    def video_json(self, no: str) -> tuple[int, dict]:
        base = {"videoNo": int(no), "videoType": "REPLAY", "channel": {"channelName": "가짜채널"},
                "liveOpenDate": "2026-11-10 19:03:27", "duration": 120, "publishDate": "2026-11-11 00:00:00",
                "thumbnailImageUrl": "https://video-phinf.pstatic.net/thumb.jpg", "userAdultStatus": None,
                "liveRewindPlaybackJson": None}
        if no == DASH_NO:
            return 200, {"code": 200, "content": dict(base, videoTitle="DASH 다시보기", adult=False, vodStatus="ABR_HLS",
                                                      videoId="VID100", inKey="INKEY100")}
        if no == BROKEN_NO:
            return 200, {"code": 200, "content": dict(base, videoTitle="깨진 영상", adult=False, vodStatus="ABR_HLS",
                                                      videoId="VID400", inKey="INKEY400")}
        if no == FAST_NO:
            playback = {"media": [{"mediaId": "HLS", "protocol": "HLS",
                                   "path": f"{self.base}/vod/fast/{FAST_TOKEN}/master.m3u8",
                                   "encodingTrack": [{"encodingTrackId": "90p", "videoHeight": 90, "videoFrameRate": "10.0",
                                                      "videoBitRate": 100000}]}]}
            return 200, {"code": 200, "content": dict(base, videoTitle="빠른 다시보기", adult=False, vodStatus="NONE",
                                                      videoId=None, inKey=None, liveRewindPlaybackJson=json.dumps(playback))}
        if no == ADULT_NO:
            return 200, {"code": 200, "content": dict(base, videoTitle="성인", adult=True, vodStatus="ABR_HLS", videoId=None,
                                                      inKey=None, userAdultStatus="NOT_LOGIN_USER")}
        return 200, {"code": 404, "message": "없는 영상", "content": None}

    def mpd(self, vid: str) -> str:
        file_url = f"{self.base}/vod/{vid}/file.mp4?_lsu_sa_={SIG}"
        m3u = f"{self.base}/vod/{vid}/hls/media.m3u8?_lsu_sa_={SIG}"
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:nvod="urn:naver:vod:2020" type="static" mediaPresentationDuration="PT2M">
 <Period>
  <AdaptationSet mimeType="video/mp4">
   <Representation id="PD_90P_160_100_24" bandwidth="150000" width="160" height="90" frameRate="10" mimeType="video/mp4">
    <nvod:Label kind="qualityId">90P_160_100_24</nvod:Label><nvod:Label kind="fps">10</nvod:Label>
    <BaseURL>{file_url.replace("&", "&amp;")}</BaseURL>
   </Representation>
  </AdaptationSet>
  <AdaptationSet mimeType="video/mp2t" nvod:m3u="{m3u}">
   <Representation id="hls-uuid-0001" bandwidth="160000" width="160" height="90" frameRate="10" nvod:m3u="{m3u}">
    <BaseURL>{self.base}/vod/{vid}/hls/</BaseURL>
   </Representation>
   <Representation id="hls-uuid-0002" bandwidth="90000" width="128" height="72" frameRate="10" nvod:m3u="{m3u}">
    <BaseURL>{self.base}/vod/{vid}/hls/</BaseURL>
   </Representation>
  </AdaptationSet>
  <AdaptationSet mimeType="audio/mp4"><Representation id="audio" bandwidth="64000"><BaseURL>{file_url}</BaseURL></Representation></AdaptationSet>
 </Period>
</MPD>"""

    def video_list(self, page: int) -> dict:
        """채널 다시보기 목록. 실제 응답의 필드 이름을 따르고 화면에 넘기지 않을 필드도 섞는다."""
        def item(no, title, vtype, publish, adult=False):
            return {"videoNo": int(no), "videoId": "SECRET-VIDEO-ID", "videoTitle": title, "videoType": vtype,
                    "publishDate": publish, "thumbnailImageUrl": "https://nng-phinf.pstatic.net/thumb.jpg?type=o500x280_blur",
                    "trailerUrl": "https://a01-g-naver-vod.akamaized.net/trailer.mp4?__gda__=SECRET", "duration": 120,
                    "readCount": 3, "publishDateAt": 1, "adult": adult, "channel": {"channelId": "x"}, "tags": ["t"]}
        data = [item(DASH_NO, "DASH 다시보기", "REPLAY", "2026-11-11 00:00:00"),
                item(FAST_NO, "빠른 다시보기", "REPLAY", "2026-11-02 00:00:00"),
                item(ADULT_NO, "성인", "REPLAY", "2026-10-30 00:00:00", adult=True),
                item("500", "업로드", "UPLOAD", "2026-10-01 00:00:00")] if page == 0 else []
        return {"code": 200, "message": None, "content": {"page": page, "size": 30, "totalCount": 4, "totalPages": 1, "data": data}}

    def handle(self, raw_path: str, headers):
        parts = urlsplit(raw_path)
        path, query = parts.path, parse_qs(parts.query)
        if re.fullmatch(r"/api/service/v1/channels/[0-9a-f]{32}/videos", path):
            with self.lock:
                self.list_calls += 1
            # 쪽 번호·크기·정렬을 빠뜨린 요청은 거부한다(httpx가 params=로 주소의 쿼리를 버리던 결함을 잡는다)
            if query.get("sortType") != ["LATEST"] or query.get("pagingType") != ["PAGE"] or query.get("size") != ["30"] or "page" not in query:
                return 400, {"Content-Type": "application/json"}, [b'{"code":400}']
            body = self.video_list(int(query["page"][0]))
            return 200, {"Content-Type": "application/json"}, [json.dumps(body).encode()]
        m = re.fullmatch(r"/api/service/v1/channels/([0-9a-f]{32})", path)
        if m:
            with self.lock:
                self.channel_calls.append(m.group(1))
            if self.channel_status != 200:
                return self.channel_status, {"Content-Type": "application/json"}, [b'{"code":500}']
            name = self.channel_names.get(m.group(1))
            content = {"channelId": m.group(1), "channelName": name, "channelImageUrl": None, "verifiedMark": False} if name else \
                {"channelId": None, "channelName": None}
            return 200, {"Content-Type": "application/json"}, [json.dumps({"code": 200, "message": None, "content": content}).encode()]
        m = re.fullmatch(r"/api/service/v2/videos/(\d+)", path)
        if m:
            status, body = self.video_json(m.group(1))
            return status, {"Content-Type": "application/json"}, [json.dumps(body).encode()]
        m = re.fullmatch(r"/api/neonplayer/vodplay/v2/playback/(VID\d+)", path)
        if m:
            if query.get("key", [""])[0] != "IN" + "KEY" + m.group(1)[3:]:
                return 403, {}, [b"bad key"]
            return 200, {"Content-Type": "application/dash+xml"}, [self.mpd(m.group(1)).encode()]
        if path.startswith("/vod/"):
            with self.lock:
                self.media_cookies.append(headers.get("Cookie"))
            return self.serve_media(path, query, headers)
        return None

    def serve_media(self, path: str, query: dict, headers):
        if self.media_status != 200:
            return self.media_status, {}, [b"denied"]
        fast = re.fullmatch(r"/vod/fast/(hdntl=[^/]+)/(.+)", path)
        if fast:
            if fast.group(1) != FAST_TOKEN:
                return 403, {}, [b"token"]
            name = fast.group(2)
            if name == "master.m3u8":
                text = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=160000,RESOLUTION=160x90\nv90/media.m3u8\n"
                return 200, {"Content-Type": "application/vnd.apple.mpegurl"}, [text.encode()]
            if name == "v90/media.m3u8":
                return 200, {"Content-Type": "application/vnd.apple.mpegurl"}, [(self.media / "hls" / "media.m3u8").read_bytes()]
            m = re.fullmatch(r"v90/(seg\d{3}\.ts)", name)
            if m:
                with self.lock:
                    self.segments.append(m.group(1))
                data = (self.media / "hls" / m.group(1)).read_bytes()
                return 200, {"Content-Type": "video/mp2t", "Content-Length": str(len(data))}, self._slow(data)
            return 404, {}, [b""]
        fm4 = re.fullmatch(r"/vod/fm4/(hdntl=[^/]+)/(media\.m3u8|init\.mp4|seg\d{3}\.m4s)", path)
        if fm4:
            if fm4.group(1) != FAST_TOKEN:
                return 403, {}, [b"token"]
            with self.lock:
                self.segments.append("fm4/" + fm4.group(2))
            data = (self.media / "fm4" / fm4.group(2)).read_bytes()
            return 200, {"Content-Type": "application/octet-stream", "Content-Length": str(len(data))}, self._slow(data)
        if query.get("_lsu_sa_", [""])[0] != SIG:
            return 403, {}, [b"signature"]
        broken = path.startswith("/vod/VID400/")
        m = re.fullmatch(r"/vod/VID\d+/hls/(media\.m3u8|seg\d{3}\.ts)", path)
        if m:
            if m.group(1).endswith(".ts"):
                with self.lock:
                    self.segments.append(m.group(1))
            data = (self.media / "hls" / m.group(1)).read_bytes()
            ctype = "application/vnd.apple.mpegurl" if m.group(1).endswith("m3u8") else "video/mp2t"
            return 200, {"Content-Type": ctype, "Content-Length": str(len(data))}, self._slow(data)
        if re.fullmatch(r"/vod/VID\d+/file\.mp4", path):
            data = b"\x00garbage" * 50000 if broken or self.broken_media else self.file_bytes
            rng = None if self.no_range else headers.get("Range")
            if rng:
                with self.lock:
                    self.ranges.append(rng)
                m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng)
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                body = data[start:end + 1]
                return 206, {"Content-Type": "video/mp4", "Accept-Ranges": "bytes", "Content-Length": str(len(body)),
                             "Content-Range": f"bytes {start}-{end}/{len(data)}"}, self._slow(body)
            return 200, {"Content-Type": "video/mp4", "Accept-Ranges": "bytes", "Content-Length": str(len(data))}, self._slow(data)
        return 404, {}, [b""]

    def _slow(self, data: bytes):
        step = 65536
        for i in range(0, len(data), step):
            if self.delay:
                time.sleep(self.delay)
            yield data[i:i + step]
