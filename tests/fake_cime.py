"""시험용 가짜 씨미. conftest의 가짜 서버(`FakeChzzk.extra_get`)에 얹는다. 실제 씨미는 부르지 않는다.

- `/cime/api/openapi/v1/{채널ID}/live-status`: 공개 상태 API
- `/cime/@{핸들}/live`: 방송 화면. 여러 채널의 방송 객체를 HTML 엔티티로 이스케이프한 자료로 함께 넣는다
- 재생 주소는 가짜 서버의 fMP4 HLS(`/hls/<토큰>/live/master.m3u8`)다
쓰는 법: `serve(server)` 뒤 `server.cime_channels[ID] = {"slug", "name", "adult"}`, 방송은 `server.go_live(ID, live_id=…)`.
"""

from __future__ import annotations

import html
import re
import json

PREFIX = "/cime"
GOOD_AUTH = "eyJhbGciOiJIUzI1NiJ9.eyJpYXQiOjE3NjMwMDAwMDAwMDAsImR1cmF0aW9uIjp7ImRheXMiOjE0fX0.c2ln"  # iat ms, duration 14일


def serve(server) -> None:
    server.cime_channels = {}
    server.cime_status = {}  # 채널 ID → 상태 API가 낼 HTTP 상태(404·429·500 등)
    server.cime_page_broken = False
    server.cime_live_id = {}  # 채널 ID → 방송 화면이 낼 방송 ID(바꿔 끼우기)
    server.cime_requests = []
    previous = server.extra_get

    def obj(cid: str, info: dict, auth: str | None) -> dict:
        b = server.broadcasts.get(cid)
        live = bool(b and b.api_open)
        url = ""
        if b is not None:
            token = server._new_token(b)
            url = f"{server.base}/hls/{token}/live/master.m3u8"
        if info.get("adult") and auth != GOOD_AUTH:
            url = ""  # 로그인하지 않으면 연령 제한 방송의 주소가 비었다(실측)
        live_id = server.cime_live_id.get(cid) or (b.live_id if b else "1")
        return {"id": str(live_id), "title": b.title if b else "지난 방송", "state": "ACTIVE" if live else "INACTIVE",
                "openedAt": "2026-11-14T03:00:00.000Z", "imageUrl": "https://streaming.cf.ci.me/ivs/thumb.jpg",
                "playback": {"id": None, "url": url, "urlUhd": None, "isMultitrack": False}, "isAdult": bool(info.get("adult")),
                "viewAccess": None, "channel": {"id": cid, "slug": info["slug"], "name": info["name"], "isLive": live}}

    def page(slug: str, cookie: str | None) -> tuple[int, bytes]:
        mine = next((cid for cid, info in server.cime_channels.items() if info["slug"].lower() == slug.lower()), None)
        if mine is None:
            return 404, b"<html>not found</html>"
        auth = None
        for part in (cookie or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "mauth-authorization-code":
                auth = value
        if server.cime_page_broken:
            return 200, b"<html><body>new layout</body></html>"
        # 다른 채널의 방송 객체가 먼저 나온다(반드시 채널 ID로 골라야 한다)
        items = [obj(cid, info, auth) for cid, info in server.cime_channels.items() if cid != mine] + \
                [obj(mine, server.cime_channels[mine], auth)]
        data = json.dumps({"props": {"lives": [{"type": "LIVE", "item": it} for it in items]}}, ensure_ascii=False)
        body = f'<html><head><script>window.__DATA__ = "{html.escape(data)}";</script></head><body></body></html>'
        return 200, body.encode()

    def extra_get(path: str, headers):
        if not path.startswith(PREFIX + "/"):
            return previous(path, headers) if previous else None
        rest = path[len(PREFIX):].split("?", 1)[0]
        server.cime_requests.append({"path": rest, "cookie": headers.get("Cookie")})
        parts = rest.strip("/").split("/")
        if parts[:3] == ["api", "openapi", "v1"] and parts[-1] == "live-status":
            cid = parts[3]
            if not cid.isdigit():
                return _json(400, {"statusCode": 400, "message": "bad", "error": "Bad Request"})
            if cid in server.cime_status:
                return _json(server.cime_status[cid], {"statusCode": server.cime_status[cid], "message": "x", "error": "x"})
            if cid not in server.cime_channels:
                return _json(404, {"statusCode": 404, "message": "없는 채널", "error": "Not Found"})
            b = server.broadcasts.get(cid)
            return _json(200, {"code": 200, "message": "ok", "content": {
                "isLive": bool(b and b.api_open), "title": b.title if b else "지난 방송", "openedAt": "2026-11-14T03:00:00Z"}})
        if len(parts) == 2 and parts[0].startswith("@") and parts[1] == "live":
            status, body = page(parts[0][1:], headers.get("Cookie"))
            return status, {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body))}, [body]
        return 404, {"Content-Length": "0"}, [b""]

    server.extra_get = extra_get


def _json(status: int, body: dict):
    data = json.dumps(body, ensure_ascii=False).encode()
    return status, {"Content-Type": "application/json", "Content-Length": str(len(data))}, [data]


# ---- 다시보기·클립 ----
# `serve_media(server, 미디어 폴더)`: 정보 API(`/cime/api/app/videos|clips/{ID}`), 서명 쿠키 API(POST
# `/cime/api/app/streams/sign-cookie`), 서명 쿠키가 있어야 주는 파일 호스트 흉내(`/cime/cdn/vod/{ID}/…` fMP4 HLS,
# `/cime/cdn/clip/{ID}/clip.mp4` 범위 요청). 미디어 폴더는 tests/replay_fake.build_media가 만든 것(fm4/, file.mp4).
SIGN_NAMES = ("CloudFront-Policy", "CloudFront-Signature", "CloudFront-Key-Pair-Id")
CDN = PREFIX + "/cdn"


def _cookies(header: str | None) -> dict[str, str]:
    out = {}
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            out[name] = value
    return out


def serve_media(server, media) -> None:
    import itertools
    import threading
    import time

    if not hasattr(server, "cime_channels"):
        serve(server)
    server.cime_vods = {}  # ID → {"title", "channel": (ID, 핸들, 이름), "adult", "state", "liveOpenedAt", "openedAt"}
    server.cime_clips = {}  # ID → {"title", "channel", "adult", "createdAt"}
    server.cime_ttl = (900, 540)  # 서명 쿠키의 ttlSec, refreshAfterSec
    server.cime_signs = {}  # 서명 값 → (만료 시각 monotonic, 경로)
    server.cime_sign_calls = []  # {"body", "cookie"}
    server.cime_cdn = []  # {"path", "cookie", "status"}
    server.cime_delay = 0.0  # 파일 요청마다 쉬는 시간
    server.cime_revoke_at = None  # 이 조각 번호를 처음 요청할 때 그때의 서명을 없앤다(403 뒤 새로 받기 시험)
    server.cime_no_range = False
    counter = itertools.count(1)
    lock = threading.Lock()
    fm4 = media / "fm4"
    file_bytes = (media / "file.mp4").read_bytes()
    previous_get, previous_post = server.extra_get, server.extra_post

    def auth_of(headers) -> str | None:
        return _cookies(headers.get("Cookie")).get("mauth-authorization-code")

    def info(kind: str, ident: str, headers):
        table = server.cime_vods if kind == "videos" else server.cime_clips
        item = table.get(ident)
        if item is None:
            return _json(404, {"code": 404, "message": "not found"})
        cid, slug, name = item["channel"]
        allowed = not item.get("adult") or auth_of(headers) == GOOD_AUTH
        folder = "vod" if kind == "videos" else "clip"
        url = f"{server.base}{CDN}/{folder}/{ident}/" + ("master.m3u8" if folder == "vod" else "clip.mp4")
        data = {"id": int(ident), "title": item["title"], "isAdult": bool(item.get("adult")), "viewAccess": None,
                "state": item.get("state", "ACTIVE"), "duration": item.get("duration", 120000),
                "channel": {"id": cid, "slug": slug, "name": name},
                "playback": {"url": url if allowed else None}}
        for k in ("liveOpenedAt", "openedAt", "createdAt"):
            if k in item:
                data[k] = item[k]
        return _json(200, {"code": 200, "message": "ok", "data": data})

    def sign(raw: bytes, headers):
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        server.cime_sign_calls.append({"body": body, "cookie": headers.get("Cookie")})
        kind = body.get("type")
        ident = str(body.get("videoId") if kind == "VIDEO" else body.get("clipId"))
        table, folder = (server.cime_vods, "vod") if kind == "VIDEO" else (server.cime_clips, "clip")
        item = table.get(ident)
        if item is None:
            return 404, [("Content-Type", "application/json")], b'{"code":404}'
        if item.get("adult") and auth_of(headers) != GOOD_AUTH:
            return 403, [("Content-Type", "application/json")], b'{"code":403}'
        ttl, after = server.cime_ttl
        sig = f"sig-{next(counter):04d}-SECRET"
        path = f"{CDN}/{folder}/{ident}/"
        with lock:
            server.cime_signs[sig] = (time.monotonic() + ttl, path)
        values = {"CloudFront-Policy": "policy-SECRET", "CloudFront-Signature": sig, "CloudFront-Key-Pair-Id": "KPAIR-SECRET"}
        cookies = [("Set-Cookie", f"{k}={v}; Domain=.ci.me; Path={path}; Secure; HttpOnly") for k, v in values.items()]
        data = json.dumps({"code": 200, "data": {"ttlSec": ttl, "refreshAfterSec": after, "expiresAt": "2026-11-14T00:15:00Z",
                                                 "cookiePath": path}}).encode()
        return 200, [("Content-Type", "application/json")] + cookies, data

    def cdn(rest: str, headers):
        cookie = headers.get("Cookie")
        values = _cookies(cookie)
        sig = values.get("CloudFront-Signature")
        with lock:
            entry = server.cime_signs.get(sig or "")
        ok = all(n in values for n in SIGN_NAMES) and entry is not None and entry[0] > time.monotonic() \
            and (PREFIX + rest).startswith(entry[1])
        name = rest.rsplit("/", 1)[-1]
        if ok and server.cime_revoke_at is not None and name == f"seg{server.cime_revoke_at:03d}.m4s":
            server.cime_revoke_at = None
            with lock:
                server.cime_signs.pop(sig, None)
            ok = False
        server.cime_cdn.append({"path": rest, "cookie": cookie, "status": 200 if ok else 403, "range": headers.get("Range")})
        if not ok:
            body = b"<Error><Code>MissingKey</Code></Error>" if not sig else b"<Error><Code>AccessDenied</Code></Error>"
            return 403, {"Content-Type": "text/xml", "Content-Length": str(len(body))}, [body]
        if server.cime_delay:
            time.sleep(server.cime_delay)
        parts = rest.strip("/").split("/")  # cdn/vod/ID/... | cdn/clip/ID/clip.mp4
        if parts[1] == "vod":
            if parts[3:] == ["master.m3u8"]:
                text = ("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-INDEPENDENT-SEGMENTS\n"
                        "#EXT-X-STREAM-INF:BANDWIDTH=180000,RESOLUTION=160x90,CODECS=\"avc1.42c00c,mp4a.40.2\"\n90p/media.m3u8\n"
                        "#EXT-X-STREAM-INF:BANDWIDTH=640000,RESOLUTION=320x180,CODECS=\"avc1.42c00c,mp4a.40.2\"\n180p/media.m3u8\n")
                data = text.encode()
                return 200, {"Content-Type": "application/vnd.apple.mpegurl", "Content-Length": str(len(data))}, [data]
            if len(parts) == 5 and parts[3] in ("90p", "180p"):
                f = fm4 / parts[4]
                if parts[4] in ("media.m3u8", "init.mp4") or re.fullmatch(r"seg\d{3}\.m4s", parts[4]):
                    if f.is_file():
                        data = f.read_bytes()
                        return 200, {"Content-Type": "application/octet-stream", "Content-Length": str(len(data))}, [data]
        if parts[1] == "clip" and parts[3:] == ["clip.mp4"]:
            rng = None if server.cime_no_range else headers.get("Range")
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", rng or "")
            if m:
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else len(file_bytes) - 1, len(file_bytes) - 1)
                body = file_bytes[start:end + 1]
                return 206, {"Content-Type": "video/mp4", "Accept-Ranges": "bytes", "Content-Length": str(len(body)),
                             "Content-Range": f"bytes {start}-{end}/{len(file_bytes)}"}, [body]
            return 200, {"Content-Type": "video/mp4", "Accept-Ranges": "bytes", "Content-Length": str(len(file_bytes))}, [file_bytes]
        return 404, {"Content-Length": "0"}, [b""]

    def extra_get(path: str, headers):
        rest = path[len(PREFIX):].split("?", 1)[0] if path.startswith(PREFIX + "/") else None
        if rest is not None:
            parts = rest.strip("/").split("/")
            if parts[:2] == ["api", "app"] and len(parts) == 4 and parts[2] in ("videos", "clips"):
                server.cime_requests.append({"path": rest, "cookie": headers.get("Cookie")})
                return info(parts[2], parts[3], headers)
            if parts[0] == "cdn":
                return cdn(rest, headers)
        return previous_get(path, headers) if previous_get else None

    def extra_post(path: str, headers, raw: bytes):
        if path.split("?", 1)[0] == PREFIX + "/api/app/streams/sign-cookie":
            server.cime_requests.append({"path": "/api/app/streams/sign-cookie", "cookie": headers.get("Cookie")})
            return sign(raw, headers)
        return previous_post(path, headers, raw) if previous_post else None

    server.extra_get = extra_get
    server.extra_post = extra_post


# ---- 채널 다시보기 목록 ----
# `serve_lists(server)`: `/cime/api/app/channels/{핸들}/videos?sort=RECENT&limit=30[&continuation=…]`.
# server.cime_list[핸들] = [쪽1의 줄들, 쪽2의 줄들, …]. 다음 쪽 표시는 "tok-<쪽 번호>"다.
# server.cime_list_status = [상태, …]: 앞에서부터 하나씩 그 상태로 답한다(재시도·403 시험).
def list_item(ident, cid, slug, name, title=None, opened="2026-11-13T12:00:00.000Z", adult=False, state="ACTIVE"):
    return {"id": str(ident), "title": title or f"씨미 영상 {ident}", "isAdult": adult, "state": state, "duration": 3600000,
            "liveOpenedAt": opened, "openedAt": opened, "imageUrl": f"https://streaming.cf.ci.me/ivs/{ident}/thumb.jpg",
            "viewAccess": None, "channel": {"id": cid, "slug": slug, "name": name}}


def serve_lists(server) -> None:
    from urllib.parse import parse_qs, urlsplit

    if not hasattr(server, "cime_channels"):
        serve(server)
    server.cime_list = {}
    server.cime_list_status = []
    server.cime_list_requests = []
    previous = server.extra_get

    def extra_get(path: str, headers):
        parts = urlsplit(path)
        segs = parts.path.split("/")
        if parts.path.startswith(PREFIX + "/api/app/channels/") and len(segs) == 7 and segs[6] == "videos":
            slug = segs[5]
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            server.cime_list_requests.append({"slug": slug, "query": q, "cookie": headers.get("Cookie")})
            if server.cime_list_status:
                status = server.cime_list_status.pop(0)
                if status != 200:
                    return _json(status, {"code": status, "message": "x"})
            pages = server.cime_list.get(slug)
            if pages is None:
                return _json(404, {"code": 404, "message": "없는 채널"})
            token = q.get("continuation")
            index = int(token.split("-")[1]) if token else 0
            items = pages[index] if index < len(pages) else []
            more = index + 1 < len(pages)
            return _json(200, {"code": 200, "message": "ok", "data": {
                "section": {"type": "VIDEO", "items": items, "attributes": {"size": int(q.get("limit", 10))}},
                "continuation": f"tok-{index + 1}" if more else None, "hasMoreData": more}})
        return previous(path, headers) if previous else None

    server.extra_get = extra_get
