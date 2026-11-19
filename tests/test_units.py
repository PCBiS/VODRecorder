"""순수 함수와 저장소 단위 시험."""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_recorder.core import naming
from live_recorder.platforms import credentials, registry
from live_recorder.platforms.chzzk import api as chzzk
from live_recorder.core import quality
from live_recorder.recording import receiver
from live_recorder.core import store as st
from live_recorder.core.config import ConfigError, Settings
from live_recorder.recording.engine import jitter_factor
from live_recorder.core.quality import Track

KST = timezone(timedelta(hours=9))
T = datetime(2026, 11, 11, 19, 3, 27, tzinfo=KST)
CH = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("raw,want", [("best", "best"), ("WORST", "worst"), ("1080", "1080p"), ("1080p60", "1080p"),
                                      ("720p", "720p"), ("999", "best"), ("", "best"), ("abc", "best"), (None, "best")])
def test_quality_normalize(raw, want):
    assert quality.normalize(raw) == want


TRACKS = [Track("720p", 720, 60.0, 3000000), Track("480p", 480, 30.0, 1500000), Track("1080p", 1080, 30.0, 6000000),
          Track("1080p", 1080, 60.0, 8192000), Track("1080p", 1080, 60.0, 5000000), Track("audioOnly", None, None, None, True)]


def test_quality_candidates_and_select():
    cands = quality.candidates(TRACKS)
    assert [(t.height, t.fps, t.bitrate) for t in cands] == [(480, 30.0, 1500000), (720, 60.0, 3000000), (1080, 60.0, 8192000)]
    assert quality.select(TRACKS, "best").height == 1080
    assert quality.select(TRACKS, "worst").height == 480
    assert quality.select(TRACKS, "720p").height == 720
    assert quality.select(TRACKS, "1440p").height == 1080  # 목표 이하에서 가장 높은 것
    assert quality.select(TRACKS, "144p").height == 1080  # 없으면 best
    assert quality.select([], "best") is None
    assert quality.label(quality.select(TRACKS, "best"), "best") == ("1080p", "60")
    assert quality.label(None, "1080p60") == ("1080p", "")


def render(pattern, title="제목", **kw):
    args = dict(title=title, channel_name="채널", quality="1080p", frame_rate="60", extension=".mp4",
                broadcast_start=T - timedelta(hours=1), recording_start=T, now=T)
    args.update(kw)
    return naming.render(pattern, **args)


def test_naming_tokens():
    name = render("[{date} {time}] {channel_name} {safe_live_title} {record_quality}{frame_rate}{file_extension}")
    assert name == "[2026-11-11 19h03m27s] 채널 제목 1080p60.mp4"  # 기본 패턴
    assert render("{recording_time}_{broadcast_time}_{start_time}") == "261111_190327_261111_180327_2026-11-11.mp4"
    assert render("{channel_name}") == "채널.mp4"  # 확장자 토큰이 없으면 붙인다
    assert render("{safe_live_title}", title="") == "recording.mp4"


def test_naming_sanitize():
    assert naming.sanitize('a\\b/c*d?e:f"g<h>i|j+k') == "a_b_c_d_e_f_g_h_i_j_k"
    assert naming.sanitize("  ..줄\n바꿈   여러  칸_. ") == "줄 바꿈 여러 칸"
    assert "/" not in render("{safe_live_title}", title="../../etc/passwd")


@pytest.mark.parametrize("pattern", ["{unknown}", "{safe_live_title", "{channel_name!r}", "{frame_rate:>5}", "", "}{"])
def test_naming_rejects_bad_pattern(pattern):
    with pytest.raises(ValueError):
        naming.validate_pattern(pattern)
    with pytest.raises(ConfigError):
        Settings.validate("filename_pattern", pattern)


def test_naming_long_korean_title(tmp_path):
    """255바이트를 넘는 제목. 글자 중간에서 자르지 않고 파일이 만들어진다."""
    title = "가나다라마바사아자차카타파하" * 30  # 1260바이트
    name = render("[{date} {time}] {channel_name} {safe_live_title} {record_quality}{frame_rate}{file_extension}", title=title)
    raw = name.encode("utf-8")
    assert len(raw) <= 255 - naming.RESERVED_BYTES
    assert raw.decode("utf-8") == name and name.endswith(" 1080p60.mp4") and "가나다" in name
    for suffix in ("", ".part", ".remux.part"):
        (tmp_path / (name + suffix)).write_bytes(b"x")  # 실제 파일 시스템에서 만들어진다
    stem = name[: -len(".mp4")]
    (tmp_path / f"{stem} (123).raw.mp4").write_bytes(b"x")
    # 제목을 다 지워도 긴 경우
    long_channel = render("{channel_name}{safe_live_title}", channel_name="채" * 200, title=title)
    assert len(long_channel.encode()) <= 255 - naming.RESERVED_BYTES


def test_naming_unique(tmp_path):
    assert naming.unique_path(tmp_path, "a.mp4").name == "a.mp4"
    (tmp_path / "a.mp4").write_bytes(b"")
    assert naming.unique_path(tmp_path, "a.mp4").name == "a (1).mp4"
    (tmp_path / "a (1).mp4.part").write_bytes(b"")
    assert naming.unique_path(tmp_path, "a.mp4").name == "a (2).mp4"
    (tmp_path / "b.raw.ts").write_bytes(b"")
    assert naming.unique_path(tmp_path, "b.mp4").name == "b (1).mp4"


def detail(**content):
    return {"code": 200, "content": content or None}


def test_live_detail_verdicts():
    V = chzzk.Verdict
    assert chzzk.parse_live_detail(404, None).verdict is V.OFFLINE
    assert chzzk.parse_live_detail(410, None).verdict is V.OFFLINE
    assert chzzk.parse_live_detail(500, None).verdict is V.UNKNOWN
    assert chzzk.parse_live_detail(429, {}).verdict is V.UNKNOWN
    assert chzzk.parse_live_detail(200, None).verdict is V.UNKNOWN
    assert chzzk.parse_live_detail(200, {"code": 200, "content": None}).verdict is V.OFFLINE  # 방송한 적 없는 채널
    assert chzzk.parse_live_detail(200, {"code": 200}).verdict is V.UNKNOWN
    assert chzzk.parse_live_detail(200, {"code": 200, "content": {}}).verdict is V.UNKNOWN
    assert chzzk.parse_live_detail(200, detail(status="CLOSE", liveId=1)).verdict is V.OFFLINE
    assert chzzk.parse_live_detail(200, detail(status="WEIRD", liveId=1)).verdict is V.UNKNOWN


def test_live_detail_fields():
    playback = {"meta": {"liveId": "22129492"}, "live": {"open": "2026-11-10T14:58:17", "start": "2026-11-10T14:00:00"},
                "media": [{"mediaId": "LLHLS", "protocol": "HLS", "path": "https://x/ll.m3u8", "encodingTrack": []},
                          {"mediaId": "HLS", "protocol": "HLS", "path": "https://x/hls.m3u8", "encodingTrack": [
                              {"encodingTrackId": "1080p", "videoHeight": 1080, "videoFrameRate": "60.0", "videoBitRate": 8192000},
                              {"encodingTrackId": "audioOnly", "audioOnly": True}]}]}
    r = chzzk.parse_live_detail(200, detail(status="OPEN", liveId=22129492, liveTitle=" 제목 ", liveCategoryValue="게임",
                                            adult=False, openDate="2026-11-10 14:58:17", liveImageUrl="u_{type}.jpg",
                                            livePlaybackJson=json.dumps(playback), timeMachineActive=True))
    assert r.verdict is chzzk.Verdict.LIVE
    i = r.info
    assert (i.live_id, i.title, i.category, i.hls_url) == ("22129492", "제목", "게임", "https://x/hls.m3u8")
    assert i.started_at == datetime(2026, 11, 10, 14, 58, 17, tzinfo=KST) and i.time_machine_active is True
    assert [(t.height, t.audio_only) for t in i.tracks] == [(1080, False), (None, True)]
    assert i.playback_live_id == "22129492" and i.complete()
    # 재생 정보가 다른 방송의 것이면(meta.liveId 불일치) 아직 확정이 아니다
    stale = dict(playback, meta={"liveId": "111"})
    r_stale = chzzk.parse_live_detail(200, detail(status="OPEN", liveId=22129492, liveTitle="제목", openDate="2026-11-10 14:58:17",
                                                  livePlaybackJson=json.dumps(stale)))
    assert r_stale.verdict is chzzk.Verdict.LIVE and not r_stale.info.complete()
    no_meta = {k: v for k, v in playback.items() if k != "meta"}
    assert not chzzk.parse_live_detail(200, detail(status="OPEN", liveId=22129492, liveTitle="제목",
                                                   livePlaybackJson=json.dumps(no_meta))).info.complete()
    # live.open이 없으면 live.start, 그것도 없으면 openDate
    playback["live"].pop("open")
    r2 = chzzk.parse_live_detail(200, detail(status="OPEN", liveId=1, livePlaybackJson=json.dumps(playback)))
    assert r2.info.started_at.hour == 14 and r2.info.started_at.minute == 0
    r3 = chzzk.parse_live_detail(200, detail(status="OPEN", liveId=1, openDate="2026-11-10 09:00:00"))
    assert r3.info.started_at.hour == 9 and r3.info.hls_url is None and not r3.info.complete()


def test_time_machine_parse():
    pb = {"media": [{"mediaId": "LLHLS", "protocol": "HLS", "path": "ll"}, {"mediaId": "HLS", "protocol": "HLS", "path": "hls"}]}
    assert chzzk.parse_time_machine(200, {"content": {"playback": pb}}) == "hls"
    assert chzzk.parse_time_machine(200, {"content": {"playback": json.dumps({"media": pb["media"][:1]})}}) == "ll"
    for status, body in [(500, None), (200, {"content": None}), (200, {"content": {"playback": {"media": []}}}), (404, {})]:
        with pytest.raises(chzzk.ApiError):
            chzzk.parse_time_machine(status, body)


def test_host_allow_list_and_redaction():
    allowed = ("naver.com", "pstatic.net")
    assert chzzk.host_allowed("https://api.chzzk.naver.com/x", allowed)
    assert chzzk.host_allowed("https://light-slit.pstatic.net/a", allowed)
    assert not chzzk.host_allowed("http://api.chzzk.naver.com/x", allowed)
    assert not chzzk.host_allowed("https://evilnaver.com/x", allowed)
    assert not chzzk.host_allowed("https://naver.com.evil.example/x", allowed)
    assert not chzzk.host_allowed("file:///etc/passwd", allowed)
    assert chzzk.valid_channel_id(CH) and not chzzk.valid_channel_id("https://x/" + CH) and not chzzk.valid_channel_id("../a")
    # 쓰이지 않던 chzzk.redact_url은 지웠다. 로그에서는 공통 가리기가 같은 값을 가린다
    from live_recorder import logmask

    red = logmask.Masker(False).text("https://h/a/hdntl=exp=1~hmac=SECRET/1080p/x.m3u8?hdnts=SECRET2")
    assert "SECRET" not in red


def test_jitter_is_fixed_and_bounded():
    for i in range(200):
        cid = f"{i:032x}"
        f = jitter_factor(cid)
        assert 0.85 <= f <= 1.15 and f == jitter_factor(cid)
    assert len({jitter_factor(f"{i:032x}") for i in range(50)}) > 10


# ---- 저장소 ----
def test_channel_rules(tmp_path):
    root = tmp_path / "rec"
    root.mkdir()
    store = st.Store(tmp_path / "db.sqlite3", root)
    ch = store.add_channel(CH, "이름", "a/b", quality="1080p60", extension="mp4")
    assert ch.output_dir == str(root / "a" / "b") and ch.quality == "1080p" and ch.extension == ".mp4" and ch.auto_record
    with pytest.raises(st.ValidationError):
        store.add_channel(CH, "또", "a")
    for bad in ("../x", "a/../../x", "/etc", str(tmp_path / "other")):
        with pytest.raises(st.ValidationError):
            store.add_channel("f" * 32, "x", bad)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(st.ValidationError):
        store.add_channel("f" * 32, "x", "link/sub")
    for bad_id in ("", "abc", "https://chzzk.naver.com/" + CH, "G" * 32):
        with pytest.raises(st.ValidationError):
            store.add_channel(bad_id, "x", "a")
    with pytest.raises(st.ValidationError):
        store.add_channel("e" * 32, "x", "a", extension=".mkv")
    with pytest.raises(st.ValidationError):
        store.update_channel(CH, created_at="x")  # 정해진 항목만 바꾼다
    assert store.update_channel(CH, auto_record=False).auto_record is False
    assert store.add_channel("e" * 32, "절대경로", str(root / "abs")).output_dir == str(root / "abs")


def test_ts_extension_requires_remux(tmp_path):
    """변환을 끈 채 확장자 `.ts`를 고르는 조합은 저장할 때 거부한다."""
    store = st.Store(tmp_path / "db.sqlite3", tmp_path)
    store.add_channel(CH, "ts채널", "a", extension=".ts")  # 변환 켬 + .ts는 된다
    with pytest.raises(ConfigError, match="ts채널"):
        store.set_settings({"remux_enabled": False})
    assert store.get_settings().remux_enabled is True
    store.update_channel(CH, extension=".mp4")
    store.set_settings({"remux_enabled": False})
    with pytest.raises(st.ValidationError):
        store.update_channel(CH, extension=".ts")
    with pytest.raises(st.ValidationError):
        store.add_channel("e" * 32, "x", "b", extension=".ts")
    store.add_channel("e" * 32, "x", "b", extension=".mp4")


def test_schema_v1_is_migrated(tmp_path):
    path = tmp_path / "db.sqlite3"
    st.Store(path, tmp_path).close()
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE sessions DROP COLUMN time_machine_active")
    db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    db.commit()
    db.close()
    store = st.Store(path, tmp_path)
    sid = store.create_session(channel_id=CH, time_machine_active=1)
    assert store.get_session(sid)["time_machine_active"] == 1


def test_settings_validation(tmp_path):
    store = st.Store(tmp_path / "db.sqlite3", tmp_path)
    assert store.get_settings() == Settings()
    assert store.set_settings({"rewind_seconds": "600", "remux_enabled": "off"}).rewind_seconds == 600
    assert store.get_settings().remux_enabled is False
    assert store.get_settings().check_interval == 60  # 넘기지 않은 키는 그대로
    for key, value in [("rewind_seconds", 3601), ("rewind_seconds", -1), ("check_interval", 9), ("nope", 1),
                       ("remux_enabled", "maybe"), ("check_interval", "abc"), ("filename_pattern", "{x}")]:
        with pytest.raises(ConfigError):
            store.set_settings({key: value})
    with pytest.raises(ConfigError):  # 하나라도 틀리면 아무것도 쓰지 않는다
        store.set_settings({"check_interval": 30, "rewind_seconds": 9999})
    assert store.get_settings().check_interval == 60


def test_corrupt_store_stops_startup(tmp_path):
    """깨진 저장소는 빈 상태로 조용히 뜨지 않는다."""
    path = tmp_path / "db.sqlite3"
    store = st.Store(path, tmp_path)
    store.add_channel(CH, "이름", "a")
    for i in range(300):
        store.create_session(channel_id=CH, title="x" * 200)
    store.close()
    for extra in ("-wal", "-shm"):
        Path(str(path) + extra).unlink(missing_ok=True)
    data = bytearray(path.read_bytes())
    data[100:4000] = os.urandom(3900)
    path.write_bytes(bytes(data))
    with pytest.raises(st.StoreCorrupt):
        st.Store(path, tmp_path)
    path.write_bytes(b"this is not a database at all" * 100)
    with pytest.raises(st.StoreCorrupt):
        st.Store(path, tmp_path)
    assert path.read_bytes().startswith(b"this is not")  # 덮어쓰지 않는다


def test_cookie_file(tmp_path):
    class Cfg:
        data_dir = tmp_path
    path = credentials.path(Cfg, "chzzk")
    credentials.save(Cfg, "chzzk", {"NID_AUT": "aut", "NID_SES": "ses", "OTHER": "x"})
    assert oct(os.stat(path).st_mode & 0o777) == "0o600" and oct(os.stat(path.parent).st_mode & 0o777) == "0o700"
    assert credentials.load(Cfg, "chzzk") == {"NID_AUT": "aut", "NID_SES": "ses"}
    names = registry.get("chzzk").credential_spec.names
    assert credentials.cookie_header(names, {"NID_SES": "s"}) == "NID_SES=s" and credentials.cookie_header(names, {}) is None
    assert "Cookie" not in chzzk.api_headers({}) and chzzk.api_headers({"NID_AUT": "a"})["Cookie"] == "NID_AUT=a"
    assert "Cookie" not in chzzk.stream_headers()
    with pytest.raises(ValueError):
        credentials.save(Cfg, "chzzk", {"NID_AUT": "a; b"})
    credentials.clear(Cfg, "chzzk")
    assert credentials.load(Cfg, "chzzk") == {}


# ---- 수신기 해석 ----
PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-MAP:URI="init.m4s?type=hls"
#EXT-X-PROGRAM-DATE-TIME:2026-11-11T02:00:00.000Z
#EXTINF:2.000000,
a_100.m4v
#EXTINF:2.000000,
a_101.m4v
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-11-11T02:00:10.000Z
#EXTINF:1.500000,
a_102.m4v
"""


def test_receiver_parse_media():
    pl = receiver.parse_media(PLAYLIST, "https://h/p/1080p/list.m3u8?tok=1")
    assert [s.seq for s in pl.segments] == [100, 101, 102] and not pl.endlist and pl.target_duration == 2
    assert pl.segments[0].uri == "https://h/p/1080p/a_100.m4v" and pl.segments[0].map_uri == "https://h/p/1080p/init.m4s?type=hls"
    assert pl.segments[1].pdt == pl.segments[0].pdt + timedelta(seconds=2)  # 태그가 없으면 이어서 계산
    assert pl.segments[2].pdt.second == 10 and pl.segments[2].discontinuity and pl.segments[2].duration == 1.5
    assert receiver.parse_media(PLAYLIST + "#EXT-X-ENDLIST\n", "https://h/x").endlist
    with pytest.raises(receiver.Unsupported):
        receiver.parse_media('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:1,\na.ts\n', "https://h/x")
    assert receiver.parse_media('#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:1,\na.ts\n', "https://h/x").segments


def test_receiver_start_index():
    base = datetime(2026, 11, 11, tzinfo=timezone.utc)
    segs = [receiver.Segment(i, f"u{i}", 2.0, base + timedelta(seconds=2 * i), None) for i in range(1800)]
    idx = lambda **kw: receiver.start_index(segs, **{"rewind": 0, "start_after": None, "live_edge": 3, **kw})  # noqa: E731
    assert idx() == 1797  # 라이브 끝
    assert idx(rewind=600) == 1500  # 끝에서 600초 전
    assert idx(rewind=3600) == 0  # 목록 전체
    assert idx(rewind=99999) == 0  # 목록보다 길면 목록 처음
    assert idx(start_after=base + timedelta(seconds=2000)) == 1000  # 이어받기: 그 시각에 시작하는 조각부터
    assert idx(start_after=base + timedelta(seconds=2001)) == 1001
    assert idx(start_after=base + timedelta(days=1)) == 1800  # 아직 새 조각이 없다
    # 앞 파일의 끝 지점이 창 밖으로 밀려났으면 창의 첫 조각부터 받는다(되감기 값과 무관)
    assert idx(start_after=base - timedelta(days=1), rewind=600) == 0
    assert idx(start_after=base - timedelta(seconds=1)) == 0
    assert receiver.start_index([], rewind=10, start_after=None, live_edge=3) == 0


def test_receiver_variants():
    master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=3192000,CODECS=\"avc1.640028,mp4a.40.2\",RESOLUTION=1280x720\nhdntl=a/720p/l.m3u8\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=8384000,RESOLUTION=1920x1080\nhdntl=a/1080p/l.m3u8\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=192000,RESOLUTION=256x144\nhdntl=a/144p/l.m3u8\n")
    assert receiver.is_master(master) and not receiver.is_master(PLAYLIST)
    variants = receiver.parse_master(master, "https://h/dvr/x_playlist.m3u8?hdnts=t")
    assert [(v.height, v.bandwidth) for v in variants] == [(720, 3192000), (1080, 8384000), (144, 192000)]
    assert variants[0].uri == "https://h/dvr/hdntl=a/720p/l.m3u8"
    assert receiver.choose_variant(variants, 144).height == 144
    assert receiver.choose_variant(variants, 480).height == 1080  # 같은 화질이 없으면 대역폭이 가장 높은 것
    assert receiver.choose_variant(variants, None).height == 1080
