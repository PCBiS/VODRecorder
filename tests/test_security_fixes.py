"""공개 전 보안 점검에서 나온 문제의 시험. 고치기 전에는 실패하고 고친 뒤 통과한다."""

import os
import asyncio
import json

import httpx
import pytest

from live_recorder.core import apikeys
from test_web import PASSWORD, USER, Web


@pytest.fixture
def site(tmp_path, server, tools, monkeypatch):
    monkeypatch.delenv(apikeys.ENV_SWITCH, raising=False)
    with Web(tmp_path, server, tools) as w:
        w.login()
        yield w


def _client(app, peer="198.51.100.7"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 40000)), base_url="http://testserver")


# ---- A1. 동시 요청으로 시도 제한을 넘지 못한다 ----
def test_concurrent_wrong_logins_verify_at_most_the_limit(site, monkeypatch):
    from live_recorder.core import secretstore

    calls = []
    real = secretstore.verify_account

    def counting(*args):
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(secretstore, "verify_account", counting)

    async def run():
        async with _client(site.app) as c:
            rs = await asyncio.gather(*[c.post("/api/login", json={"username": USER, "password": f"wrong-{i}"}) for i in range(40)])
            return [r.status_code for r in rs]

    codes = asyncio.run(run())
    assert codes.count(401) == 5 and codes.count(429) == 35, codes
    assert len(calls) == 5  # 검증까지 간 것은 다섯 번뿐


def test_concurrent_wrong_keys_are_counted_before_verifying(site):
    async def run():
        async with _client(site.app, "198.51.100.8") as c:
            rs = await asyncio.gather(*[c.get("/api/channels", headers={"Authorization": f"Bearer vr_{'x' * 40}{i}"})
                                        for i in range(30)])
            return [r.status_code for r in rs]

    codes = asyncio.run(run())
    assert codes.count(401) == 5 and codes.count(429) == 25, codes


# ---- A2. 남의 틀린 시도로 맞는 비밀번호·키가 막히지 않는다 ----
def test_global_failures_slow_down_but_do_not_lock(site, monkeypatch):
    from live_recorder.web import auth

    monkeypatch.setattr(auth, "GLOBAL_SLOWDOWN", 0.01)

    async def run():
        for i in range(40):  # 서로 다른 주소 40곳에서 한 번씩 틀린다(전체 한도 30 넘김)
            async with _client(site.app, f"198.51.100.{i + 10}") as c:
                assert (await c.post("/api/login", json={"username": USER, "password": "wrong"})).status_code == 401
        async with _client(site.app, "203.0.113.200") as c:
            return (await c.post("/api/login", json={"username": USER, "password": PASSWORD})).status_code

    assert asyncio.run(run()) == 200
    assert site.app.state.limiter.busy()


def test_basic_authorization_goes_to_the_session_path(site):
    key = site.call("POST", "/api/keys", json={"name": "k", "scope": "view"}).json()["value"]
    for _ in range(8):  # Basic은 키 시도가 아니다(키 실패로 세지 않는다)
        r = site.client.get("/api/channels", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert r.status_code == 200  # 세션 쿠키로 통과
    assert site.client.get("/api/channels", headers={"Authorization": f"Bearer {key}"}).status_code == 200


# ---- A3. 본문 크기 상한 ----
def test_large_body_is_refused_before_reading(site):
    big = json.dumps({"username": USER, "password": "x" * (2 * 1024 * 1024)})
    r = site.client.post("/api/login", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "too_large"

    async def chunked():
        async def gen():
            for _ in range(40):
                yield b"x" * 65536
        async with _client(site.app, "198.51.100.99") as c:
            return await c.post("/api/login", content=gen(), headers={"Content-Type": "application/json"})

    r = asyncio.run(chunked())
    assert r.status_code == 413
    # 쿠키 파일 올리기는 더 큰 상한(쿠키 파일 256 KiB를 JSON에 싣는다)
    text = "# Netscape HTTP Cookie File\n" + ("#" * 100 + "\n") * 2400
    r = site.call("POST", "/api/cookies/upload?platform=chzzk", json={"text": text[:250 * 1024]})
    assert r.status_code != 413
    # 글 길이 상한
    r = site.client.post("/api/login", json={"username": "u" * 300, "password": "x"})
    assert r.status_code == 422


# ---- A4. 재생목록 KEY·MAP의 URI가 둘이면 받지 않는다 ----
PARENT = "https://vod.pstatic.net/root/playlist.m3u8?_lsu_sa_=SIG&policy=A"


def _pl(tag):
    return f"#EXTM3U\n#EXT-X-TARGETDURATION:5\n{tag}\n#EXTINF:5,\na.ts\n#EXT-X-ENDLIST\n".encode()


@pytest.mark.parametrize("tag", [
    '#EXT-X-KEY:METHOD=AES-128,URI="k.key",URI="file:///etc/passwd"',
    '#EXT-X-MAP:URI="init.mp4",URI="https://evil.example/x"',
    '#EXT-X-MAP:URI="init.mp4" ,URI="file:/etc/x"',
    '#EXT-X-KEY:METHOD=NONE,URI="file:///etc/passwd"',
])
def test_duplicate_or_stray_uri_is_refused(tag):
    from live_recorder.ext.downloads import hls_transport, segments
    from live_recorder.ext.downloads.errors import ReplayFailure

    with pytest.raises(ReplayFailure):
        hls_transport.rewrite_media_playlist(_pl(tag), PARENT)
    if tag.startswith("#EXT-X-MAP"):
        with pytest.raises(ReplayFailure):
            segments.rewrite_plain(_pl(tag), "https://cdn.example/v/media.m3u8", lambda u: u)


def test_key_map_lines_are_rebuilt_from_known_attributes():
    from live_recorder.ext.downloads import hls_transport

    out = hls_transport.rewrite_media_playlist(_pl('#EXT-X-MAP:URI="init.mp4",X-EVIL="file:/etc/x"'), PARENT).decode()
    line = next(x for x in out.splitlines() if x.startswith("#EXT-X-MAP:"))
    assert line.startswith('#EXT-X-MAP:URI="https://vod.pstatic.net/root/init.mp4?') and "X-EVIL" not in line and line.count("URI=") == 1


def test_local_playlist_keeps_only_known_tags(tmp_path):
    from live_recorder.ext.downloads import segments

    text = ('#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:2\n#EXT-X-MAP:URI="https://cdn.example/v/init.mp4"\n'
            '#EXTINF:2,\nhttps://cdn.example/v/0.m4s\n#EXT-X-DATERANGE:ID="x",X-URL="file:/etc/x"\n#EXTINF:2,\nhttps://cdn.example/v/1.m4s\n'
            '#EXT-X-ENDLIST\n')
    pl = segments.Playlist(text.encode())
    d = segments.SegmentDownloader.__new__(segments.SegmentDownloader)
    d.seg_dir = tmp_path
    payload = d.local_playlist(pl, [0, 1], {0: tmp_path / "a.m4s", 1: tmp_path / "b.m4s"}).decode()
    assert "DATERANGE" not in payload and "file:/etc" not in payload
    assert payload.count("#EXT-X-MAP:URI=") == 1 and f'file:{tmp_path}/map-000.mp4' in payload


# ---- A5. 로그 가리기 ----
@pytest.mark.parametrize("text, secret", [
    ("GET https://a.akamaized.net/hls/hdntl=exp=1700000000~acl=*~hmac=abcdef0123456789abcdef/seg1.ts", "hmac=abcdef0123456789"),
    ("GET https://a.pstatic.net/v/__gda__=1700000000_0123456789abcdef0123/x.mp4", "0123456789abcdef0123"),
    ("POST https://api.telegram.org/bot123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll/sendMessage", "AAAbbbCCCdddEEEfff"),
    ("cookie mauth-authorization-code=eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
    (".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tsapisid-value-0002xyz", "sapisid-value-0002xyz"),
])
def test_log_mask_covers_more_secret_shapes(text, secret):
    from live_recorder import logmask

    assert secret not in logmask.Masker(False).text(text)


def test_known_secrets_include_chat_id_and_cookie_file_values(tmp_path):
    from types import SimpleNamespace

    from live_recorder import logexport
    from live_recorder.core import secretstore
    from live_recorder.platforms import credentials

    cfg = SimpleNamespace(data_dir=tmp_path, telegram_path=tmp_path / "secrets" / "telegram.json",
                          setup_code_path=tmp_path / "secrets" / "setup-code")
    secretstore.telegram_save(cfg.telegram_path, bot_token="1:x", chat_id="987654321")
    credentials.save(cfg, "youtube", {"cookies_txt": ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tsid-secret-value-01\n"
                                                    ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tsapisid-secret-02\n"})
    values = logexport.known_secrets(cfg)
    assert "987654321" in values and "sid-secret-value-01" in values


def test_receiver_log_hides_urls(capsys):
    from live_recorder.recording import receiver

    assert receiver._redact_urls("실패: https://x.example/a/b?token=SECRET") == "실패: https://x.example/…"


# ---- A6·C4. 압축된 큰 응답을 끝까지 메모리로 풀지 않는다 ----
BOMB_LIMIT = 1024 * 1024


def _bomb(encoding="gzip", size=40 * 1024 * 1024):
    import gzip
    import zlib

    raw = b"0" * size
    return gzip.compress(raw) if encoding == "gzip" else zlib.compress(raw)


def _bomb_handler(encoding="gzip"):
    data = _bomb(encoding)

    def handler(request):
        # stream=으로 주어 응답이 미리 읽히지 않게 한다(실제 네트워크와 같게)
        return httpx.Response(200, headers={"Content-Encoding": encoding, "Content-Type": "application/json"},
                              stream=httpx.ByteStream(data))
    return handler


@pytest.fixture
def inflate_meter(monkeypatch):
    """풀기 함수를 감싸 한 번에 풀어 낸 양과 모두 풀어 낸 양을 잰다."""
    from live_recorder.core import httpread

    seen = {"calls": 0, "max": 0, "total": 0}
    original = httpread._new_decoder

    class Meter:
        def __init__(self, inner):
            self.inner = inner

        def decompress(self, data, max_length=0):
            out = self.inner.decompress(data, max_length)
            seen["calls"] += 1
            seen["max"] = max(seen["max"], len(out))
            seen["total"] += len(out)
            return out

        @property
        def unconsumed_tail(self):
            return self.inner.unconsumed_tail

        def flush(self):
            out = self.inner.flush()
            seen["total"] += len(out)
            return out

    def wrapped(encoding):
        d = original(encoding)
        return None if d is None else Meter(d)

    monkeypatch.setattr(httpread, "_new_decoder", wrapped)
    return seen


def _bounded(seen, limit=BOMB_LIMIT):
    from live_recorder.core import httpread

    assert seen["calls"] > 0, "풀기 함수를 거치지 않았다"
    assert seen["max"] <= httpread.STEP and seen["total"] <= limit + httpread.STEP, seen


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_bounded_reader_stops_on_compressed_bomb(inflate_meter, encoding):
    from live_recorder.core import httpread

    with httpx.Client(transport=httpx.MockTransport(_bomb_handler(encoding))) as c:
        with pytest.raises(httpread.TooLarge):
            httpread.get(c, "https://x.example/", BOMB_LIMIT)
    _bounded(inflate_meter)
    inflate_meter.update(calls=0, max=0, total=0)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_bomb_handler(encoding))) as c:
            await httpread.aget(c, "https://x.example/", BOMB_LIMIT)

    with pytest.raises(httpread.TooLarge):
        asyncio.run(run())
    _bounded(inflate_meter)


def test_bounded_reader_decodes_normal_and_refuses_other_encodings():
    import gzip
    import zlib

    from live_recorder.core import httpread

    body = b'{"ok": true}'
    for enc, data in (("gzip", gzip.compress(body)), ("deflate", zlib.compress(body)),
                      ("deflate", zlib.compress(body)[2:-4]), ("identity", body), ("", body)):
        def handler(request, enc=enc, data=data):
            assert request.headers["accept-encoding"] == "gzip, deflate"  # 풀 수 있는 것만 청한다
            return httpx.Response(200, headers={"Content-Encoding": enc} if enc else {}, stream=httpx.ByteStream(data))
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            assert httpread.get(c, "https://x.example/", 1024).json() == {"ok": True}, enc
    for enc in ("br", "gzip, gzip", "zstd"):
        def handler(request, enc=enc):
            return httpx.Response(200, headers={"Content-Encoding": enc}, stream=httpx.ByteStream(b"x"))
        with httpx.Client(transport=httpx.MockTransport(handler)) as c, pytest.raises(httpread.BadEncoding):
            httpread.get(c, "https://x.example/", 1024)
    deep = b"[" * 100000 + b"]" * 100000
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=httpx.ByteStream(deep)))) as c:
        with pytest.raises(ValueError):
            httpread.get(c, "https://x.example/", len(deep)).json()


def test_api_clients_stop_on_compressed_bomb(inflate_meter, monkeypatch):
    """실제 클라이언트들에 압축 폭탄을 보내 실패로 끝나고 푼 양이 상한 안인지 본다."""
    from types import SimpleNamespace

    from live_recorder.ext.cime_media import common as cime_common
    from live_recorder.ext.downloads import media
    from live_recorder.ext.downloads.errors import ReplayFailure
    from live_recorder.platforms.base import ApiError
    from live_recorder.platforms.chzzk import api as chzzk_api
    from live_recorder.platforms.cime import platform as cime
    from live_recorder.recording import receiver

    def reset():
        inflate_meter.update(calls=0, max=0, total=0)

    async_transport = httpx.MockTransport(_bomb_handler())
    # 치지직 API
    client = chzzk_api.ChzzkClient("https://api.chzzk.naver.com", ("naver.com",), retry=chzzk_api.RetryPolicy(attempts=1))
    client._client = httpx.AsyncClient(transport=async_transport)
    with pytest.raises(ApiError):
        asyncio.run(client._get_json("https://api.chzzk.naver.com/x", None))
    _bounded(inflate_meter, chzzk_api.MAX_RESPONSE)
    reset()
    # 씨미 화면
    cm = cime.CimeClient(SimpleNamespace(data_dir=None, allow_http=False))
    cm._client = httpx.AsyncClient(transport=async_transport)
    with pytest.raises(ApiError):
        asyncio.run(cm._get(cime.base() + "/@x", {}, BOMB_LIMIT))
    _bounded(inflate_meter)
    reset()
    # 다시보기 받기의 JSON·재생목록
    monkeypatch.setattr(media, "_check_url", lambda u: u)
    with httpx.Client(transport=httpx.MockTransport(_bomb_handler())) as c:
        with pytest.raises(ReplayFailure):
            media._get(c, "https://cdn.example/x.m3u8", {}, BOMB_LIMIT)
    _bounded(inflate_meter)
    reset()
    # 씨미 정보 API(공통 읽기)
    monkeypatch.setattr(media, "http_client", lambda timeout=20.0: httpx.Client(transport=httpx.MockTransport(_bomb_handler())))
    with pytest.raises(cime_common.Failure):
        cime_common.get_info(next(iter(cime_common.INFO_PATH)), "1", None, "https://ci.me/")
    _bounded(inflate_meter, cime_common.MAX_JSON)
    reset()
    # 녹화 수신기의 재생목록
    r = receiver.Receiver.__new__(receiver.Receiver)
    r.client = httpx.Client(transport=httpx.MockTransport(_bomb_handler()))
    r.platform = SimpleNamespace(request_headers=lambda kind, cred: {})
    with pytest.raises(httpx.HTTPError):
        r._get_text("https://cdn.example/live.m3u8")
    _bounded(inflate_meter, receiver.MAX_PLAYLIST)


# ---- A7. 씨미 화면 해석의 시간 상한 ----
def test_cime_page_parsing_is_bounded():
    import time

    from live_recorder.platforms.cime import page

    body = '{"playback":' * 20000 + "x"  # 닫히지 않는 객체가 끝없이 이어진 화면
    t = time.monotonic()
    assert page.live_objects(body) == []
    assert time.monotonic() - t < page.MAX_SECONDS * 2 + 1


# ---- B1. 접근 키와 계정 수명 ----
def test_keys_rejected_without_account_and_revocable(tmp_path, server, tools, monkeypatch):
    from fastapi.testclient import TestClient

    from live_recorder.core import apikeys, secretstore
    from test_web import PASSWORD, Web

    monkeypatch.delenv(apikeys.ENV_SWITCH, raising=False)
    with Web(tmp_path, server, tools) as w:
        w.login()
        bare = TestClient(w.app, base_url="http://testserver", follow_redirects=False)
        r = w.call("POST", "/api/keys", json={"name": "k", "scope": "view", "expiry": "none"})
        h = {"Authorization": "Bearer " + r.json()["value"]}
        assert bare.get("/api/channels", headers=h).status_code == 200
        account = w.h.cfg.account_path.read_bytes()
        secretstore.remove(w.h.cfg.account_path)
        assert bare.get("/api/channels", headers=h).status_code == 401  # 계정이 없으면 키도 받지 않는다
        w.h.cfg.account_path.write_bytes(account)
        assert bare.get("/api/channels", headers=h).status_code == 200
        r = w.call("POST", "/api/account/password", json={"current": PASSWORD, "new": PASSWORD + "x", "revoke_keys": True})
        assert r.status_code == 200, r.text
        assert bare.get("/api/channels", headers=h).status_code == 401


def test_password_change_keeps_keys_by_default(tmp_path, server, tools, monkeypatch):
    from live_recorder.core import apikeys
    from test_web import PASSWORD, Web

    monkeypatch.delenv(apikeys.ENV_SWITCH, raising=False)
    with Web(tmp_path, server, tools) as w:
        w.login()
        w.call("POST", "/api/keys", json={"name": "k", "scope": "view", "expiry": "none"})
        assert w.call("POST", "/api/account/password", json={"current": PASSWORD, "new": PASSWORD + "x"}).status_code == 200
        from test_web import USER

        assert w.client.post("/api/login", json={"username": USER, "password": PASSWORD + "x"}).status_code == 200
        w.csrf = w.client.get("/api/session").json()["csrf"]
        assert len(w.call("GET", "/api/keys").json()["keys"]) == 1


def test_cli_account_delete_revokes_keys(tmp_path, monkeypatch, capsys):
    from live_recorder import cli
    from live_recorder.core import apikeys, secretstore

    monkeypatch.delenv(apikeys.ENV_SWITCH, raising=False)
    data, rec = tmp_path / "data", tmp_path / "rec"
    rec.mkdir()
    (data / "secrets").mkdir(parents=True)
    secretstore.create_account(data / "secrets" / "account.json", "admin", "Correct-Horse-9!")
    keys = apikeys.KeyStore(data / "secrets" / "api-keys.json")
    keys.create("a", "view", "none")
    keys.create("b", "operate", "none")
    assert cli.main(["--data-dir", str(data), "--record-root", str(rec), "account", "delete"]) == 0
    assert apikeys.KeyStore(data / "secrets" / "api-keys.json").count() == 0
    assert "2개" in capsys.readouterr().out


# ---- B4. 파일 관리자 범위 ----
def _fm(tmp_path, roots):
    from live_recorder import files

    data, logs = tmp_path / "data", tmp_path / "data-logs"
    data.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    return files.FileManager(data, lambda: [(str(r), str(r), "setting") for r in roots], lambda: [], log_dir=logs)


def test_file_manager_rejects_log_folder_and_hidden_and_fifo(tmp_path):
    from live_recorder import files

    root = tmp_path / "share"
    (root / "sub").mkdir(parents=True)
    (root / ".hidden").mkdir()
    (root / ".hidden" / "x.txt").write_text("x")
    (root / "a.txt").write_text("a")
    os.mkfifo(root / "pipe")
    fm = _fm(tmp_path, [root])
    assert fm.root_problem(str(tmp_path / "data-logs")) and fm.root_problem(str(tmp_path))
    key = fm.roots()[0].key
    with pytest.raises(files.FileError) as e:
        fm.list_dir(key, ".hidden", False)
    assert e.value.status == 404
    assert fm.list_dir(key, ".hidden", True)["entries"]
    with pytest.raises(files.FileError):
        fm.open_file(key, ".hidden/x.txt", False)
    with pytest.raises(files.FileError) as e:
        fm.list_dir(key, "a.txt", True)
    assert e.value.status == 400
    with pytest.raises(files.FileError) as e:
        fm.open_file(key, "pipe")  # 막히지 않고 바로 거부
    assert e.value.status == 400
    fd, size, _ = fm.open_file(key, "a.txt")
    assert os.read(fd, 10) == b"a"
    os.close(fd)


def test_hidden_check_follows_symlinks(tmp_path):
    from live_recorder import files

    root = tmp_path / "share2"
    (root / ".secret").mkdir(parents=True)
    (root / ".secret" / "k.txt").write_text("k")
    os.symlink(root / ".secret", root / "plain")  # 이름은 숨김이 아니지만 실제 위치는 숨김 폴더
    fm = _fm(tmp_path, [root])
    key = fm.roots()[0].key
    with pytest.raises(files.FileError):
        fm.list_dir(key, "plain", False)
    with pytest.raises(files.FileError):
        fm.open_file(key, "plain/k.txt", False)
    assert fm.list_dir(key, "plain", True)["entries"]


def test_output_dir_rejects_data_and_log_folders(tmp_path):
    from live_recorder.core import store as st

    data = tmp_path / "rec" / ".data"
    st.reserve_dirs(data, tmp_path / "rec" / "logs")
    root = tmp_path / "rec"
    (data / "secrets").mkdir(parents=True)
    (root / "logs").mkdir()
    for bad in (".data", ".data/secrets", ".data/secrets/x", "logs", "logs/a"):
        with pytest.raises(st.ValidationError):
            st.resolve_output_dir(root, bad)
    assert st.resolve_output_dir(root, ".") == root.resolve()
    assert st.resolve_output_dir(root, "chzzk/a") == (root / "chzzk" / "a").resolve()


def test_output_dir_inside_data_folder(tmp_path):
    from live_recorder.core import store as st

    # 자료 폴더가 녹화 루트 안: 자료 폴더 아래 전체를 거부
    root = tmp_path / "rec2"
    data = root / "app-data"
    (data / "cache").mkdir(parents=True)
    st.reserve_dirs(data, data / "logs")
    with pytest.raises(st.ValidationError):
        st.resolve_output_dir(root, "app-data/cache")
    # 녹화 루트가 자료 폴더 안: 녹화 루트와 그 하위는 허용, 비밀·로그 폴더는 거부
    data2 = tmp_path / "data2"
    root2 = data2 / "recordings"
    (root2 / "a").mkdir(parents=True)
    st.reserve_dirs(data2, data2 / "logs")
    assert st.resolve_output_dir(root2, "a") == (root2 / "a").resolve()
    assert st.resolve_output_dir(root2, ".") == root2.resolve()


# ---- B2. Host 검사 ----
def test_allowed_web_hosts(tmp_path, server, tools, monkeypatch):
    from fastapi.testclient import TestClient

    from live_recorder.web import app as webapp

    assert webapp.host_allowed("evil.example", ())
    allowed = ("rec.example.org",)
    for ok in ("rec.example.org", "REC.example.org:8443", "127.0.0.1:8080", "[::1]:8080", "localhost", "10.0.0.5"):
        assert webapp.host_allowed(ok, allowed), ok
    for bad in ("evil.example", "rec.example.org.evil.example", "", None):
        assert not webapp.host_allowed(bad, allowed), bad
    monkeypatch.setenv(webapp.HOSTS_ENV, "rec.example.org")
    with Web(tmp_path, server, tools) as w:
        assert TestClient(w.app, base_url="http://evil.example").get("/login").status_code == 400
        assert TestClient(w.app, base_url="http://rec.example.org").get("/login").status_code == 200


# ---- B3. Secure 쿠키는 신뢰 프록시의 X-Forwarded-Proto만 본다 ----
def test_forwarded_proto_only_from_trusted_proxy():
    from starlette.requests import Request

    from live_recorder.web import auth

    def req(peer, proto):
        return Request({"type": "http", "method": "GET", "path": "/", "scheme": "http", "server": ("x", 80),
                        "client": (peer, 1234), "headers": [(b"x-forwarded-proto", proto.encode())], "query_string": b""})

    assert not auth.request_is_https(req("10.0.0.9", "https"), ())
    assert not auth.request_is_https(req("10.0.0.9", "https"), ("127.0.0.1/32",))
    assert auth.request_is_https(req("127.0.0.1", "https"), ("127.0.0.1/32",))
    assert not auth.request_is_https(req("127.0.0.1", "https, http"), ("127.0.0.1/32",))


def test_uvicorn_does_not_rewrite_forwarded_headers(monkeypatch):
    from live_recorder.web import app as webapp

    monkeypatch.delenv(webapp.CONCURRENCY_ENV, raising=False)
    config = webapp.server_config(object(), "127.0.0.1", 0)
    assert config.proxy_headers is False and config.limit_concurrency == webapp.DEFAULT_CONCURRENCY
    monkeypatch.setenv(webapp.CONCURRENCY_ENV, "50")
    assert webapp.server_config(object(), "127.0.0.1", 0).limit_concurrency == 50
    monkeypatch.setenv(webapp.CONCURRENCY_ENV, "5")
    from live_recorder.core.config import ConfigError

    with pytest.raises(ConfigError):
        webapp.concurrency_limit()


def test_connection_limit_does_not_block_normal_use(tmp_path, server, tools, monkeypatch):
    """실제 uvicorn을 상한(기본 200)으로 띄워 화면·API·내려받기를 함께 많이 불러도 막히지 않는지 본다."""
    import socket
    import threading
    import time as _time

    import uvicorn

    from live_recorder.web import app as webapp
    from test_web import PASSWORD, USER

    from conftest import Harness
    from live_recorder import web
    from live_recorder.core import secretstore

    monkeypatch.delenv(webapp.CONCURRENCY_ENV, raising=False)
    h = Harness(tmp_path, server, tools)
    secretstore.create_account(h.cfg.account_path, USER, PASSWORD)
    app = web.create_app(h.cfg, h.timings, disk_free=h.disk_free)
    if True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = uvicorn.Server(webapp.server_config(app, "127.0.0.1", port))
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        deadline = _time.monotonic() + 30
        while not srv.started:
            assert t.is_alive() and _time.monotonic() < deadline
            _time.sleep(0.05)
        try:
            base = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=base) as c:
                assert c.post("/api/login", json={"username": USER, "password": PASSWORD}).status_code == 200
                cookies = dict(c.cookies)
            codes = []

            def hit(path):
                with httpx.Client(base_url=base, cookies=cookies, timeout=20) as c:
                    codes.append(c.get(path).status_code)

            paths = ["/", "/api/channels", "/api/settings", "/static/app.js"] * 15
            threads = [threading.Thread(target=hit, args=(p,)) for p in paths]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            assert codes.count(200) == len(paths), sorted(set(codes))
        finally:
            srv.should_exit = True
            t.join(10)


# ---- B5. 비밀 파일 쓰기 ----
def test_secret_write_uses_random_temp_and_does_not_follow_links(tmp_path):
    import threading

    from live_recorder.core import secretstore

    path = tmp_path / "secrets" / "x.json"
    path.parent.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep")
    os.symlink(victim, path.with_name(path.name + ".tmp"))  # 예전의 고정 임시 이름에 놓은 링크
    secretstore.write_json(path, {"a": 1})
    assert victim.read_text() == "keep" and secretstore.read_json(path) == {"a": 1}
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    def writer(n):
        for i in range(30):
            secretstore.write_json(path, {"n": n, "i": i})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(secretstore.read_json(path)) == {"n", "i"}
    assert [p.name for p in path.parent.iterdir() if p.name.endswith(".tmp") and not p.is_symlink()] == []


def test_broken_secret_file_gives_clear_error(tmp_path, server, tools):
    from live_recorder.core import secretstore

    path = tmp_path / "x.json"
    path.write_text("{broken")
    with pytest.raises(secretstore.SecretFileError) as e:
        secretstore.read_json(path)
    assert "깨졌습니다" in str(e.value)
    with Web(tmp_path / "w", server, tools) as w:
        w.login()
        w.h.cfg.telegram_path.parent.mkdir(parents=True, exist_ok=True)
        w.h.cfg.telegram_path.write_text("{broken")
        r = w.call("GET", "/api/settings")
        assert r.status_code == 500 and r.json()["code"] == "secret_file_broken", r.text


# ---- B6. 하위 프로세스 환경 ----
def test_child_env_is_minimal():
    from live_recorder.core import childenv

    src = {"PATH": "/bin", "HOME": "/h", "LIVE_RECORDER_ADMIN_PASSWORD": "pw", "LIVE_RECORDER_FFMPEG": "/x/ffmpeg",
           "AWS_SECRET_ACCESS_KEY": "s", "GITHUB_TOKEN": "t", "PYTHONPATH": "/extra", "LC_ALL": "C.UTF-8"}
    env = childenv.child_env("/pkg", src)
    assert env["PYTHONPATH"] == "/pkg" + os.pathsep + "/extra" and env["LIVE_RECORDER_FFMPEG"] == "/x/ffmpeg"
    assert "LIVE_RECORDER_ADMIN_PASSWORD" not in env and "AWS_SECRET_ACCESS_KEY" not in env and "GITHUB_TOKEN" not in env
    assert env["YTDLP_NO_PLUGINS"] == "1" and env["LC_ALL"] == "C.UTF-8"


def test_admin_password_removed_from_environment(tmp_path, server, tools, monkeypatch):
    monkeypatch.setenv("LIVE_RECORDER_ADMIN_USER", "admin2")
    monkeypatch.setenv("LIVE_RECORDER_ADMIN_PASSWORD", "Correct-Horse-9!")
    with Web(tmp_path, server, tools):
        assert "LIVE_RECORDER_ADMIN_PASSWORD" not in os.environ


def test_ytdlp_self_check_runs_isolated():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "live_recorder" / "platforms" / "youtube" / "vendor.py").read_text(encoding="utf-8")
    assert '"-s", "-P", "-m"' in text and '"YTDLP_NO_PLUGINS": "1"' in text


# ---- B7. 조각 토큰은 마스터와 같은 호스트에만 ----
def test_segment_token_only_for_master_host():
    from live_recorder.recording import receiver

    r = receiver.Receiver.__new__(receiver.Receiver)
    r.use_segment_token, r.segment_token = True, ("_tk", "SECRET")
    r.master_url = "https://cdn.example/live/master.m3u8?_tk=SECRET"
    assert r._segment_url("https://cdn.example/live/1.ts") == "https://cdn.example/live/1.ts?_tk=SECRET"
    assert r._segment_url("https://other.example/live/1.ts") == "https://other.example/live/1.ts"
    assert r._segment_url("http://cdn.example/live/1.ts") == "http://cdn.example/live/1.ts"


# ---- B8. yt-dlp 교체 안내 ----
def test_ytdlp_update_notice_says_code_runs_during_check():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "live_recorder" / "platforms" / "youtube" / "vendor.py").read_text(encoding="utf-8")
    assert "받는 즉시 이 점검 과정에서 실행" in text


# ---- B9. 짝 없는 서러게이트 ----
def test_lone_surrogates_are_cleaned_at_the_boundary():
    import json as _json

    from live_recorder.platforms.base import LiveInfo, clean_text

    title = _json.loads('"방송 \\ud83d 제목"')  # 반쪽 이모지
    info = LiveInfo(live_id="1", title=title, category="c", adult=False, open_date="", image_url="", started_at=None)
    info.title.encode("utf-8")
    assert info.title == "방송 � 제목" and clean_text("보통 😀") == "보통 😀"


# ---- B11. 지난 수신기 정리에서 PID 재사용 구분 ----
def test_reap_checks_process_start_time():
    import subprocess
    import sys
    import time
    from datetime import datetime, timedelta, timezone

    from live_recorder.recording import engine

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(0.2)
        began = engine._process_start(proc.pid)
        assert began is not None and abs(began - time.time()) < 30
        now = datetime.now(timezone.utc)
        assert engine._same_run(proc.pid, now.isoformat())
        assert not engine._same_run(proc.pid, (now - timedelta(hours=3)).isoformat())  # 예전 세션의 PID를 다른 프로세스가 받음
        assert engine._same_run(proc.pid, None)
    finally:
        proc.kill()
        proc.wait()


# ---- B12. 422 응답은 보낸 값을 되돌려 주지 않는다, 깊은 구조 ----
def test_validation_errors_do_not_echo_input(tmp_path, server, tools):
    with Web(tmp_path, server, tools) as w:
        r = w.client.post("/api/login", json={"username": "admin", "password": ["Secret-Value-123"]})
        assert r.status_code == 422 and "Secret-Value-123" not in r.text and "input" not in r.json()["detail"][0]
        w.login()
        r = w.call("POST", "/api/account/password", json={"current": 12345678901, "new": "Secret-Value-456"})
        assert r.status_code == 422 and "Secret-Value-456" not in r.text and "12345678901" not in r.text


def test_deeply_nested_input_is_refused_cleanly(tmp_path, server, tools):
    with Web(tmp_path, server, tools) as w:
        w.login()
        r = w.call("POST", "/api/cookies/upload", content=b'{"text":' + b"[" * 200000 + b"]" * 200000 + b"}",
                   headers={"Content-Type": "application/json"})
        assert r.status_code in (400, 422), r.text


def test_cookie_upload_with_deep_json_text(tmp_path, server, tools):
    with Web(tmp_path, server, tools) as w:
        w.login()
        r = w.call("POST", "/api/cookies/upload", json={"text": "[" * 100000 + "]" * 100000})
        assert r.status_code == 400 and "error" in r.json(), r.text


# ---- B10. 구간 받기의 ffmpeg는 로컬 파일을 열지 않는다 ----
def test_section_from_file_excludes_file_protocol(monkeypatch, tmp_path):
    import subprocess

    from live_recorder.ext.downloads import media

    seen = {}

    class Stop(Exception):
        pass

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        raise Stop

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(media, "_check_url", lambda u: u)
    job = media.FFmpegJob("ffmpeg", None, None)
    with pytest.raises(Stop):
        job.section_from_file("https://cdn.example/v.mp4", str(tmp_path / "o.mp4"), 1.0, 2.0)
    cmd = seen["cmd"]
    protocols = cmd[cmd.index("-protocol_whitelist") + 1].split(",")
    assert "file" not in protocols and cmd[cmd.index("-format_whitelist") + 1] == "concat,mov,mpegts"


# ---- C1. 맞는 키의 동시 요청은 막지 않는다 ----
def test_concurrent_correct_keys_all_pass(tmp_path, server, tools, monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    from live_recorder.core import apikeys

    monkeypatch.delenv(apikeys.ENV_SWITCH, raising=False)
    with Web(tmp_path, server, tools) as w:
        w.login()
        value = w.call("POST", "/api/keys", json={"name": "k", "scope": "view", "expiry": "none"}).json()["value"]
        bare = TestClient(w.app, base_url="http://testserver")
        codes, barrier = [], threading.Barrier(20)

        def hit():
            barrier.wait()
            codes.append(bare.get("/api/channels", headers={"Authorization": "Bearer " + value}).status_code)

        threads = [threading.Thread(target=hit) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert codes.count(200) == 20, codes
        # 틀린 키는 여전히 주소별로 막힌다
        wrong = [bare.get("/api/channels", headers={"Authorization": "Bearer vr_" + "x" * 40}).status_code for _ in range(8)]
        assert wrong[:5] == [401] * 5 and set(wrong[5:]) == {429}
        assert bare.get("/api/channels", headers={"Authorization": "Bearer " + value}).status_code == 429  # 잠긴 주소


# ---- C2. 다른 곳에서 폐기한 키가 flush로 되살아나지 않는다 ----
def test_flush_does_not_resurrect_revoked_key(tmp_path):
    from live_recorder.core import apikeys

    path = tmp_path / "secrets" / "api-keys.json"
    server = apikeys.KeyStore(path)
    v1, k1 = server.create("a", "view", "none")
    v2, k2 = server.create("b", "view", "none")
    assert server.verify(v1) and server.verify(v2)
    server.touch(k1["id"])
    server.touch(k2["id"])  # 메모리에만 있는 사용 기록
    cli = apikeys.KeyStore(path)  # 서버가 도는 동안 CLI가 폐기
    assert cli.revoke(k1["id"])
    server.flush()
    after = apikeys.KeyStore(path)
    ids = {k["id"]: k for k in after.list()}
    assert k1["id"] not in ids and ids[k2["id"]]["uses"] >= 1
    assert server.verify(v1) is None


# ---- C5. 로그인 대기열 ----
def _make_busy(app):
    for i in range(40):
        app.state.limiter.fail(f"192.0.2.{i}")
    assert app.state.limiter.busy()


def test_slowdown_happens_outside_the_verify_lock(site, monkeypatch):
    import time as _time

    from live_recorder.web import auth

    monkeypatch.setattr(auth, "GLOBAL_SLOWDOWN", 0.5)
    _make_busy(site.app)

    async def wrong(i):
        async with _client(site.app, f"198.51.100.{i + 10}") as c:
            return (await c.post("/api/login", json={"username": USER, "password": "wrong"})).status_code

    async def right():
        await asyncio.sleep(0.05)
        t = _time.monotonic()
        async with _client(site.app, "203.0.113.200") as c:
            code = (await c.post("/api/login", json={"username": USER, "password": PASSWORD})).status_code
        return code, _time.monotonic() - t

    async def run():
        return await asyncio.gather(*[wrong(i) for i in range(10)], right())

    *codes, (code, took) = asyncio.run(run())
    assert set(codes) == {401} and code == 200
    assert took < 0.5 * 10 / 2, took  # 잠금 안에서 쉬면 0.5초×10 넘게 기다린다


def test_verify_queue_has_a_cap(site, monkeypatch):
    from live_recorder.web import auth

    monkeypatch.setattr(auth, "GLOBAL_SLOWDOWN", 0.5)
    _make_busy(site.app)

    async def wrong(i):
        async with _client(site.app, f"198.51.100.{i + 10}") as c:
            r = await c.post("/api/login", json={"username": USER, "password": "wrong"})
            return r.status_code, r.json().get("error")

    async def run():
        return await asyncio.gather(*[wrong(i) for i in range(30)])

    out = asyncio.run(run())
    busy = [c for c, e in out if c == 429 and e == auth.BUSY]
    assert len(busy) == 30 - auth.VERIFY_QUEUE and sum(1 for c, _ in out if c == 401) == auth.VERIFY_QUEUE


def test_ipv6_is_counted_per_64(site):
    from live_recorder.web import auth

    assert auth.limit_key("2001:db8::1") == auth.limit_key("2001:db8::ffff:1") == "2001:db8::/64"
    assert auth.limit_key("::ffff:198.51.100.7") == "198.51.100.7" and auth.limit_key("198.51.100.7") == "198.51.100.7"

    async def run():
        out = []
        for i in range(6):
            async with _client(site.app, f"2001:db8::{i + 1}") as c:
                out.append((await c.post("/api/login", json={"username": USER, "password": "wrong"})).status_code)
        async with _client(site.app, "2001:db8:1::1") as c:  # 다른 /64
            out.append((await c.post("/api/login", json={"username": USER, "password": PASSWORD})).status_code)
        return out

    assert asyncio.run(run()) == [401] * 5 + [429, 200]


# ---- C3. 재생목록·주소의 제어문자 ----
@pytest.mark.parametrize("bad", ["#EXTINF:5,\x00file:/etc/passwd", "#EXTINF:5,\x1b[2J", "#EXT-X-PROGRAM-DATE-TIME:2026\x7f"])
def test_playlists_with_control_chars_are_refused(bad):
    from live_recorder.ext.downloads import hls_transport, media, segments
    from live_recorder.ext.downloads.errors import ReplayFailure
    from live_recorder.recording import receiver

    text = f"#EXTM3U\n#EXT-X-TARGETDURATION:5\n{bad}\na.ts\n#EXTINF:5,\nb.ts\n#EXT-X-ENDLIST\n"
    with pytest.raises(ReplayFailure):
        hls_transport.rewrite_media_playlist(text.encode(), PARENT)
    with pytest.raises(ReplayFailure):
        segments.rewrite_plain(text.encode(), "https://cdn.example/v/media.m3u8", lambda u: u)
    with pytest.raises(ReplayFailure):
        segments.Playlist(text.replace("a.ts", "https://cdn.example/a.ts").encode())
    with pytest.raises(receiver.Unsupported):
        receiver.parse_media(text, "https://cdn.example/live/media.m3u8")
    with pytest.raises(receiver.Unsupported):
        receiver.parse_master("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\x00\nv.m3u8\n", "https://cdn.example/m.m3u8")
    # 고치기 전과 같은 꼴의 정상 재생목록은 그대로 받는다
    good = text.replace(bad, "#EXTINF:5,")
    assert hls_transport.rewrite_media_playlist(good.encode(), PARENT)
    assert receiver.parse_media(good, "https://cdn.example/live/media.m3u8").segments


@pytest.mark.parametrize("url", ["https://vod.pstatic.net/a\x00.mp4", "https://vod.pstatic.net/a b.mp4",
                                 "https://vod.pstatic.net/a\t.mp4", "https://vod.pstatic.net/a\x7f.mp4"])
def test_urls_with_control_chars_or_spaces_are_refused(url):
    from live_recorder.ext.downloads import media
    from live_recorder.ext.downloads.errors import ReplayFailure
    from live_recorder.platforms.base import host_allowed

    with pytest.raises(ReplayFailure):
        media._check_url(url)
    assert not host_allowed(url, ("pstatic.net",))
    assert host_allowed("https://vod.pstatic.net/a.mp4", ("pstatic.net",))


# ---- C6. 깨진 비밀 파일: 관문(로그인 경로·Bearer)에서도 알아볼 수 있는 오류 ----
def test_broken_secret_file_in_the_guard(tmp_path, server, tools, monkeypatch):
    from fastapi.testclient import TestClient

    from live_recorder.core import apikeys

    monkeypatch.delenv(apikeys.ENV_SWITCH, raising=False)
    with Web(tmp_path, server, tools) as w:
        w.login()
        value = w.call("POST", "/api/keys", json={"name": "k", "scope": "view", "expiry": "none"}).json()["value"]
        bare = TestClient(w.app, base_url="http://testserver", raise_server_exceptions=False)
        w.h.cfg.api_keys_path.write_text("{broken")
        r = bare.get("/api/channels", headers={"Authorization": "Bearer " + value})
        assert r.status_code == 500 and r.json()["code"] == "secret_file_broken", r.text
        w.h.cfg.account_path.write_text("{broken")
        r = TestClient(w.app, base_url="http://testserver", raise_server_exceptions=False).get("/api/channels")
        assert r.status_code == 500 and r.json()["code"] == "secret_file_broken", r.text


def test_secret_read_modify_write_is_locked(tmp_path):
    import threading

    from live_recorder.core import secretstore

    path = tmp_path / "secrets" / "telegram.json"
    errors = []

    def save(n):
        try:
            for i in range(20):
                secretstore.telegram_save(path, bot_token=f"{n}:" + "a" * 35) if n % 2 else secretstore.telegram_save(path, chat_id=str(1000 + i))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=save, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = secretstore.telegram_load(path)
    assert not errors and data.get("bot_token") and data.get("chat_id")  # 한쪽 쓰기가 다른 쪽 값을 지우지 않는다


def test_child_env_passes_proxy_and_ca_settings():
    from live_recorder.core import childenv

    src = {n: "x" for n in ("HTTP_PROXY", "https_proxy", "NO_PROXY", "all_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")}
    assert set(src) <= set(childenv.child_env(None, src))


def test_ytdlp_module_disables_plugins_in_this_process(monkeypatch):
    import importlib

    monkeypatch.delenv("YTDLP_NO_PLUGINS", raising=False)
    from live_recorder.platforms.youtube import ytdlp

    importlib.reload(ytdlp)
    assert os.environ["YTDLP_NO_PLUGINS"] == "1"
    monkeypatch.setenv("YTDLP_NO_PLUGINS", "keep")
    importlib.reload(ytdlp)
    assert os.environ["YTDLP_NO_PLUGINS"] == "keep"


def test_path_signature_with_slash_in_acl_is_masked():
    from live_recorder import logmask

    out = logmask.Masker(False).text("GET https://a.akamaized.net/hls/hdntl=exp=17~acl=/live/*~hmac=deadbeefcafe0123/seg1.ts")
    assert "deadbeefcafe0123" not in out and "/live/*" not in out and out.endswith("/seg1.ts")


def test_failure_records_are_bounded():
    from live_recorder.web import auth

    lim = auth.LoginLimiter(global_limit=None)
    for i in range(20000):
        lim.fail(f"k{i % 3}")
    assert len(lim._all) <= 100000 and all(len(q) <= 64 for q in lim._fails.values())
    for i in range(12000):
        lim.fail(f"addr{i}")
    assert len(lim._fails) <= 10001
