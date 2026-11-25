"""영상 코덱. 코덱 우선순위로 고르기, 받는 코덱 고정, 녹화 중 코덱 전환, 섞인 원본 검증."""

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

from live_recorder.core import codec as codec_mod
from live_recorder.core import store as st
from live_recorder.core.config import AppConfig, Settings
from live_recorder.core.quality import Track, select
from live_recorder.notify import EVENTS
from live_recorder.platforms.chzzk import api
from live_recorder.recording.finalize import Finalizer
from live_recorder.recording.receiver import Variant, choose_variant, parse_master

from conftest import CHANNEL, fast_timings, parse_iso, probe, Harness


# ---- 판별 ----
def test_codec_names():
    assert codec_mod.from_codecs_attr("hvc1.1.2.L123.90,mp4a.40.2") == "hevc"
    assert codec_mod.from_codecs_attr("mp4a.40.2,avc1.64002A") == "h264"
    assert codec_mod.from_codecs_attr("av01.0.12M.08") == "av1"
    assert codec_mod.from_codecs_attr("mp4a.40.2") == "" and codec_mod.from_codecs_attr(None) == ""
    assert codec_mod.from_name("HEVC") == "hevc" and codec_mod.from_name("H264") == "h264" and codec_mod.from_name(None) == ""
    assert codec_mod.label("hevc") == "HEVC" and codec_mod.label("h264") == "H.264" and codec_mod.label("") == "알 수 없음"
    assert codec_mod.normalize_preference("") == "h264" and codec_mod.normalize_preference("ANY") == "any"
    with pytest.raises(ValueError):
        codec_mod.normalize_preference("h265")


def test_codec_from_init_segments(media, hevc_media):
    assert codec_mod.from_init((media / "init.mp4").read_bytes()) == "h264"
    assert codec_mod.from_init((hevc_media / "init.mp4").read_bytes()) == "hevc"
    assert codec_mod.from_init((media / "seg00000.m4s").read_bytes()) == ""  # 초기화 조각이 아니다
    assert codec_mod.from_init(b"") == "" and codec_mod.from_init(b"\x00\x00\x00\x08junk" * 3) == ""
    assert codec_mod.from_init(b"\x00\x00\x00\x01moov") == ""  # 잘린 64비트 길이


def test_live_detail_reads_track_codec():
    """방송 정보의 `videoCodec`을 트랙에 싣는다. 없으면 빈 값이다."""
    media = [{"mediaId": "HLS", "protocol": "HLS", "path": "https://x/m.m3u8", "encodingTrack": [
        {"encodingTrackId": "1080p-hevc", "videoHeight": 1080, "videoFrameRate": "60.0", "videoBitRate": 8000000, "videoCodec": "HEVC"},
        {"encodingTrackId": "1080p", "videoHeight": 1080, "videoFrameRate": "60.0", "videoBitRate": 8000000, "videoCodec": "H264"},
        {"encodingTrackId": "720p", "videoHeight": 720, "videoFrameRate": "60.0", "videoBitRate": 3000000},
        {"encodingTrackId": "audioOnly", "audioOnly": True}]}]
    tracks = api.parse_tracks(media)
    assert [(t.track_id, t.codec) for t in tracks] == [("1080p-hevc", "hevc"), ("1080p", "h264"), ("720p", ""), ("audioOnly", "")]


# ---- 고르기 ----
BOTH = [Track("1080p-hevc", 1080, 60.0, 8000000, codec="hevc"), Track("1080p", 1080, 60.0, 8000000, codec="h264"),
        Track("720p", 720, 60.0, 3000000, codec="h264")]


def test_track_selection_follows_preference():
    """같은 높이에 코덱이 둘이면 우선순위로 고른다. 화질 규칙은 그 뒤에 건다."""
    assert select(BOTH, "best", "h264").track_id == "1080p"
    assert select(BOTH, "1080p", "h264").track_id == "1080p"
    assert select(BOTH, "best", "hevc").track_id == "1080p-hevc"
    assert select(BOTH, "1080p", "hevc").track_id == "1080p-hevc"
    assert select(BOTH, "best", "any").track_id == "1080p-hevc"  # 방송이 먼저 내놓은 것
    assert select(BOTH, "best", None).track_id == "1080p-hevc"  # 우선순위가 없으면 예전과 같다
    assert select(BOTH, "720p", "hevc").track_id == "720p"  # 그 높이에 없으면 있는 것
    assert select(BOTH, "best", "av1").track_id == "1080p-hevc"  # 고른 코덱이 아예 없으면 있는 것(먼저 나온 것)


def test_track_selection_when_only_other_codec_exists():
    """HEVC만 주는 높이는 H.264 우선이어도 HEVC로 받는다(녹화를 거부하지 않는다)."""
    only = [Track("1080p-hevc", 1080, 60.0, 8000000, codec="hevc"), Track("720p", 720, 60.0, 3000000, codec="h264")]
    assert select(only, "best", "h264").track_id == "1080p-hevc"
    assert select(only, "1080p", "h264").track_id == "1080p-hevc"
    unknown = [Track("1080p", 1080, 60.0, 8000000), Track("720p", 720, 30.0, 3000000)]  # 코덱을 알려 주지 않는 플랫폼
    assert select(unknown, "best", "h264").track_id == "1080p"


def test_track_selection_keeps_fps_and_bitrate_rule_within_codec():
    """같은 코덱 안에서는 예전 규칙(프레임레이트, 비트레이트)이 그대로다."""
    tracks = [Track("a", 1080, 30.0, 9000000, codec="h264"), Track("b", 1080, 60.0, 8000000, codec="h264"),
              Track("c", 1080, 60.0, 9000000, codec="hevc")]
    assert select(tracks, "best", "h264").track_id == "b"
    assert select(tracks, "best", "any").track_id == "c"


MASTER = ('#EXTM3U\n'
          '#EXT-X-STREAM-INF:BANDWIDTH=8192000,CODECS="hvc1.1.2.L123.90,mp4a.40.2",RESOLUTION=1920x1080\nhevc.m3u8\n'
          '#EXT-X-STREAM-INF:BANDWIDTH=8192000,CODECS="avc1.64002A,mp4a.40.2",RESOLUTION=1920x1080\nh264.m3u8\n'
          '#EXT-X-STREAM-INF:BANDWIDTH=3192000,CODECS="avc1.640028,mp4a.40.2",RESOLUTION=1280x720\nv720.m3u8\n')


def test_variant_selection_matches_codec():
    """수신기는 코어가 정한 코덱의 변형을 `CODECS`로 고른다."""
    variants = parse_master(MASTER, "https://x/master.m3u8")
    assert [v.codec for v in variants] == ["hevc", "h264", "h264"]

    def pick(height, want="", prefer=""):
        return choose_variant(variants, height, want, prefer).uri.rsplit("/", 1)[1]

    assert pick(1080, "h264") == "h264.m3u8" and pick(1080, "hevc") == "hevc.m3u8"
    assert pick(1080) == "hevc.m3u8"  # 정한 것이 없으면 예전처럼 먼저 나온 것
    assert pick(1080, "", "h264") == "h264.m3u8"  # 정한 코덱이 없으면 우선순위
    assert pick(1080, "av1", "h264") == "h264.m3u8"  # 정한 코덱의 변형이 없으면 우선순위
    assert pick(1080, "av1", "any") == "hevc.m3u8"
    assert pick(720, "hevc") == "v720.m3u8"  # 그 높이에 없으면 있는 것
    assert pick(None, "h264") == "h264.m3u8" and pick(None, "hevc") == "hevc.m3u8"


def test_variant_selection_without_codecs_is_unchanged():
    """CODECS가 없는 재생목록은 예전 규칙 그대로다(같은 높이에서 대역폭이 가장 높은 것)."""
    variants = [Variant("a", 3192000, 720, None), Variant("b", 8384000, 1080, None), Variant("c", 9000000, 1080, None)]
    assert choose_variant(variants, 1080, "h264", "h264").uri == "c"
    assert choose_variant(variants, 480, "h264", "h264").uri == "c"


# ---- 저장소 ----
def test_channel_preference_is_stored_and_validated(tmp_path):
    store = st.Store(tmp_path / "db.sqlite3", tmp_path)
    ch = store.add_channel(CHANNEL, "가", "ch1")
    assert ch.video_codec == "h264"  # 기본값
    assert store.update_channel(CHANNEL, video_codec="hevc").video_codec == "hevc"
    assert store.update_channel(CHANNEL, video_codec="any").video_codec == "any"
    with pytest.raises(st.ValidationError):
        store.update_channel(CHANNEL, video_codec="h265")
    assert store.add_channel("f" * 32, "나", "ch2", video_codec="av1").video_codec == "av1"


def test_columns_are_added_without_raising_the_schema_version(tmp_path):
    """이 열이 없는 저장소(이전 빌드)를 열면 열만 더한다. 버전은 그대로라 이전 빌드로 다시 열 수 있다."""
    path = tmp_path / "db.sqlite3"
    store = st.Store(path, tmp_path)
    store.add_channel(CHANNEL, "가", "ch1", video_codec="hevc")
    sid = store.create_session(channel_id=CHANNEL, video_codec="hevc")
    store.close()
    db = sqlite3.connect(path)
    version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    db.execute("ALTER TABLE channels DROP COLUMN video_codec")  # 이전 빌드가 만든 저장소 꼴로 되돌린다
    db.execute("ALTER TABLE sessions DROP COLUMN video_codec")
    db.commit()
    db.close()
    again = st.Store(path, tmp_path)
    assert again.get_channel(CHANNEL).video_codec == "h264"  # 빈 값은 기본값으로 읽는다
    assert again.get_session(sid)["video_codec"] is None
    again.update_session(sid, video_codec="h264")
    again.close()
    db = sqlite3.connect(path)
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == version == str(st.SCHEMA_VERSION)
    # 이전 빌드가 쓰는 꼴의 INSERT(이 열을 모른다)가 그대로 된다
    db.execute("INSERT INTO channels (platform, channel_id, name, output_dir, quality, extension, auto_record, created_at)"
               " VALUES ('chzzk', ?, '다', '/x', 'best', '.mp4', 1, 'now')", ("e" * 32,))
    db.commit()
    db.close()


def test_notification_setting_exists():
    """코덱 전환 알림은 필수 알림이고 기본으로 켜져 있다."""
    assert EVENTS["codec_changed"][1] is True
    assert Settings().notify_codec_changed is True
    assert Settings.validate("notify_codec_changed", False) is False


# ---- 녹화 ----
def codec_of(tools, path: Path) -> str:
    return next(s["codec_name"] for s in probe(tools, path)["streams"] if s["codec_type"] == "video")


def decoded_frames(tools, path: Path) -> tuple[int, str]:
    """끝까지 풀어 본다. (풀린 영상 프레임 수, 오류 출력)."""
    r = subprocess.run([tools[1], "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return int((r.stdout.strip() or "0").split(",")[0]), r.stderr.strip()


def record_until_end(harness, b, seconds: float = 1.5):
    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화 시작")
            await asyncio.sleep(seconds)
            b.end()
            return (await harness.until(lambda: harness.finalized(1), what="마무리"))[0]

    return asyncio.run(scenario())


def test_default_preference_records_h264_when_both_offered(harness, server, hevc_media, tools):
    """HEVC가 먼저 나와도 기본값(H.264 우선)이면 H.264 변형을 받는다."""
    server.hevc_dir = hevc_media
    harness.add_channel(quality="best")
    harness.store.set_settings({"rewind_seconds": 0})
    s = record_until_end(harness, server.go_live(title="둘 다"))
    assert s["finalize_status"] == "done", s
    assert s["video_codec"] == "h264" and s["quality_label"] == "1080p60"
    assert codec_of(tools, Path(s["output_path"])) == "h264"
    assert not any("/v1080h/" in path for path, _ in server.calls)  # HEVC 변형은 건드리지 않았다
    assert any("/v1080/seg" in path for path, _ in server.calls)


def test_hevc_preference_records_hevc(harness, server, hevc_media, tools):
    """HEVC 우선이면 HEVC를 받고, 세션과 화질 표시에 코덱이 남는다. 변환 결과도 HEVC다."""
    server.hevc_dir = hevc_media
    harness.add_channel(quality="1080p", video_codec="hevc")
    harness.store.set_settings({"rewind_seconds": 0})
    s = record_until_end(harness, server.go_live(title="에이치"))
    assert s["finalize_status"] == "done", s
    assert s["video_codec"] == "hevc" and s["quality_label"] == "1080p60 HEVC"
    out = Path(s["output_path"])
    info = probe(tools, out)
    video = next(x for x in info["streams"] if x["codec_type"] == "video")
    assert video["codec_name"] == "hevc" and video["codec_tag_string"] == "hvc1"
    frames, errors = decoded_frames(tools, out)
    assert errors == "" and abs(frames - s["media_seconds"] * 10) <= 10  # 10fps 조각
    assert not any("/v1080/seg" in path for path, _ in server.calls)


def test_any_preference_takes_what_the_broadcast_lists_first(harness, server, hevc_media, tools):
    server.hevc_dir = hevc_media
    harness.add_channel(quality="best", video_codec="any")
    harness.store.set_settings({"rewind_seconds": 0})
    s = record_until_end(harness, server.go_live())
    assert s["finalize_status"] == "done" and s["video_codec"] == "hevc", s


def test_hevc_only_broadcast_is_recorded_under_h264_preference(harness, server, hevc_media, tools):
    """1080p가 HEVC뿐이면 H.264 우선이어도 그대로 녹화한다."""
    server.hevc_dir = hevc_media
    server.h264_1080 = False
    harness.add_channel(quality="best")
    harness.store.set_settings({"rewind_seconds": 0})
    s = record_until_end(harness, server.go_live())
    assert s["finalize_status"] == "done" and s["video_codec"] == "hevc", s
    assert codec_of(tools, Path(s["output_path"])) == "hevc"


def test_broadcast_without_codec_info_is_unchanged(harness, server, tools):
    """코덱을 알려 주지 않는 재생 정보(예전 꼴)에서도 받은 코덱은 초기화 조각에서 읽어 기록한다."""
    harness.add_channel(quality="1080p", video_codec="hevc")
    harness.store.set_settings({"rewind_seconds": 0})
    s = record_until_end(harness, server.go_live())
    assert s["finalize_status"] == "done" and s["video_codec"] == "h264" and s["quality_label"] == "1080p60", s


def switch_scenario(harness, server, *, timemachine: bool):
    if not timemachine:
        server.live_window = 60  # 일반 수신의 재생목록 창. 시험 방송은 1초에 조각 10개라 실제(조각 3개에 6초)만큼 넉넉하게 둔다
    harness.add_channel(quality="1080p", video_codec="hevc")
    harness.enable_telegram(rewind_seconds=0, timemachine_enabled=timemachine)
    b = server.go_live(title="전환")

    async def scenario():
        async with harness:
            first = await harness.until(harness.recording, what="녹화 시작")
            await asyncio.sleep(1.5)
            server.hevc_offered = False  # 방송이 HEVC 변형을 거둔다
            await harness.until(lambda: len(harness.sessions()) >= 2 and harness.recording()
                                and harness.recording()["id"] != first["id"], what="새 파일로 잇기")
            await asyncio.sleep(1.5)
            b.end()
            done = await harness.until(lambda: harness.finalized(2), what="두 파일 마무리")
            await harness.until(lambda: any("코덱" in m["text"] for m in server.tg_messages), what="코덱 전환 알림")
            return done

    return asyncio.run(scenario())


@pytest.mark.parametrize("timemachine", [True, False])
def test_codec_change_splits_the_recording(tmp_path, server, hevc_media, tools, timemachine):
    """받던 코덱의 변형이 사라지면 이어 붙이지 않고 새 파일로 나눈다. 빠진 조각 없이 이어지고 경고 알림이 간다."""
    server.hevc_dir = hevc_media
    harness = Harness(tmp_path, server, tools, timings=fast_timings())
    one, two = switch_scenario(harness, server, timemachine=timemachine)[:2]
    assert (one["video_codec"], two["video_codec"]) == ("hevc", "h264"), (one, two)
    assert one["end_detail"] == "codec_changed" and one["exit_code"] == 0 and one["end_reason"] == "broadcast_end"
    assert "코덱이 바뀌었습니다(HEVC → H.264)" in one["note"]
    assert "앞 파일에서 이어짐" in two["note"] and "HEVC → H.264" in two["note"]
    assert sum(1 for m in server.tg_messages if "코덱 바뀜" in m["text"]) == 1  # 같은 전환을 두 번 알리지 않는다
    assert one["finalize_status"] == "done" and two["finalize_status"] == "done", (one, two)
    a, b = Path(one["output_path"]), Path(two["output_path"])
    assert a != b and codec_of(tools, a) == "hevc" and codec_of(tools, b) == "h264"
    for s, path in ((one, a), (two, b)):  # 두 파일 모두 끝까지 풀린다(한 파일에 코덱이 섞이지 않았다)
        frames, errors = decoded_frames(tools, path)
        assert errors == "" and abs(frames - s["media_seconds"] * 10) <= 10, (path.name, frames, errors)
        assert len(codec_mod.scan_file(path)) <= 1
    # 둘째 파일은 첫 파일의 마지막 조각 바로 뒤에서 시작한다(겹치지도 비지도 않는다)
    assert parse_iso(two["first_media_time"]) == parse_iso(one["last_media_time"]), (one["last_media_time"], two["first_media_time"])
    text = next(m["text"] for m in server.tg_messages if "코덱" in m["text"])
    assert "녹화 중 영상 코덱 바뀜" in text and "HEVC → H.264" in text and "채널: 시험채널" in text and "파일: " in text
    assert "http" not in text and "tok" not in text.split("파일:")[0]
    assert "codec_changed" in harness.log_text()
    assert not list(a.parent.glob("*.raw.*")) and not list(a.parent.glob("*.part"))


def test_h264_recording_ignores_hevc_coming_and_going(tmp_path, server, hevc_media, tools):
    """H.264로 받는 동안에는 HEVC 변형이 생기거나 사라져도 파일이 나뉘지 않는다(주소를 새로 받아도 같은 코덱을 고른다)."""
    server.hevc_dir = hevc_media
    server.hevc_offered = False
    harness = Harness(tmp_path, server, tools, timings=fast_timings())
    harness.add_channel(quality="1080p")
    harness.store.set_settings({"rewind_seconds": 0, "url_refresh_seconds": 60})
    b = server.go_live()

    async def scenario():
        async with harness:
            await harness.until(harness.recording, what="녹화 시작")
            await asyncio.sleep(1.0)
            server.hevc_offered = True  # HEVC가 생긴다(재생목록 맨 앞에)
            server.expire_tokens()  # 주소를 새로 받게 한다
            await harness.until(lambda: (harness.recording() or {}).get("url_refreshes"), what="주소 갱신")
            await asyncio.sleep(1.0)
            server.hevc_offered = False
            await asyncio.sleep(0.5)
            b.end()
            return await harness.until(lambda: harness.finalized(1), what="마무리")

    done = asyncio.run(scenario())
    assert len(harness.sessions()) == 1 and done[0]["video_codec"] == "h264" and done[0]["finalize_status"] == "done", harness.brief()
    assert codec_of(tools, Path(done[0]["output_path"])) == "h264"
    assert not any("/v1080h/seg" in path for path, _ in server.calls)


def test_gap_between_split_files_is_recorded(tmp_path, server, hevc_media, tools):
    """일반 수신에서 새 파일을 여는 사이에 조각이 재생목록에서 지나가면, 빈 시간을 새 파일의 메모와 로그에 남긴다."""
    server.hevc_dir = hevc_media
    server.live_window = 1  # 조각 하나(0.1초)뿐인 재생목록. 새 수신기가 뜨는 사이에 반드시 지나간다
    harness = Harness(tmp_path, server, tools, timings=fast_timings())
    harness.add_channel(quality="1080p", video_codec="hevc")
    harness.enable_telegram(rewind_seconds=0, timemachine_enabled=False)
    b = server.go_live(title="틈")
    one, two = two_files(harness, server, b, lambda: setattr(server, "hevc_offered", False))[:2]
    assert (one["video_codec"], two["video_codec"]) == ("hevc", "h264"), harness.brief()
    hole = (parse_iso(two["first_media_time"]) - parse_iso(one["last_media_time"])).total_seconds()
    assert hole >= 1.0 and f"앞 파일과 {hole:.1f}초가 비었습니다" in two["note"], (hole, two["note"])
    assert "codec_change_gap" in harness.log_text()


def two_files(harness, server, b, trigger, *, timeout: float = 20.0):
    async def scenario():
        async with harness:
            first = await harness.until(harness.recording, what="녹화 시작")
            await asyncio.sleep(1.5)
            trigger()
            await harness.until(lambda: len(harness.sessions()) >= 2 and harness.recording()
                                and harness.recording()["id"] != first["id"], timeout=timeout, what="새 파일로 잇기")
            await asyncio.sleep(1.5)
            b.end()
            done = await harness.until(lambda: harness.finalized(2), what="두 파일 마무리")
            await harness.until(lambda: any("코덱" in m["text"] for m in server.tg_messages), what="코덱 전환 알림")
            return done

    return asyncio.run(scenario())


def test_codec_change_inside_one_playlist_splits_the_recording(tmp_path, server, hevc_media, tools):
    """같은 재생목록 안에서 초기화 조각이 다른 코덱으로 바뀌는 경우(이어 붙이면 깨진 파일이 되는 경로).
    바뀐 조각은 앞 파일에 쓰지 않고, 새 파일이 그 조각부터 받는다."""
    server.hevc_dir = hevc_media
    harness = Harness(tmp_path, server, tools, timings=fast_timings())
    harness.add_channel(quality="1080p", video_codec="hevc")
    harness.enable_telegram(rewind_seconds=0)
    b = server.go_live(title="안에서 전환")
    one, two = two_files(harness, server, b, lambda: setattr(server, "inline_switch", b.published() + 1))[:2]
    assert (one["video_codec"], two["video_codec"]) == ("hevc", "h264"), harness.brief()
    assert one["end_detail"] == "codec_changed" and one["finalize_status"] == two["finalize_status"] == "done", harness.brief()
    for s in (one, two):
        path = Path(s["output_path"])
        frames, errors = decoded_frames(tools, path)
        assert errors == "" and abs(frames - s["media_seconds"] * 10) <= 10, (path.name, frames, errors)
    assert codec_of(tools, Path(one["output_path"])) == "hevc" and codec_of(tools, Path(two["output_path"])) == "h264"
    assert parse_iso(two["first_media_time"]) == parse_iso(one["last_media_time"])
    # 조각 수: 앞 파일은 바뀌기 전 조각까지만 받았다
    assert parse_iso(one["last_media_time"]) == b.pdt(server.inline_switch)
    assert "재생목록이 밝힌 코덱" in harness.log_text()  # 둘째 파일: 재생목록은 HEVC라 했지만 받은 것을 따랐다


def test_codec_change_after_a_stall_is_still_reported(tmp_path, server, hevc_media, tools):
    """받던 변형이 404가 아니라 멈춰서 새 파일이 된 경우에도, 새 파일의 코덱이 앞 파일과 다르면 알린다."""
    server.hevc_dir = hevc_media
    harness = Harness(tmp_path, server, tools, timings=fast_timings(stream_timeout=1.5))
    harness.add_channel(quality="1080p", video_codec="hevc")
    harness.enable_telegram(rewind_seconds=0)
    b = server.go_live(title="멈춤")
    one, two = two_files(harness, server, b, lambda: setattr(server, "hevc_frozen", b.published()))[:2]
    assert (one["video_codec"], two["video_codec"]) == ("hevc", "h264"), harness.brief()
    assert one["end_detail"] != "codec_changed" and one["exit_code"] == 3, one  # 수신기는 정체로 끝났다
    assert "앞 파일에서 이어짐" in two["note"] and "HEVC → H.264" in two["note"]
    text = next(m["text"] for m in server.tg_messages if "코덱" in m["text"])
    assert "녹화 중 영상 코덱 바뀜" in text and "HEVC → H.264" in text
    assert one["finalize_status"] == two["finalize_status"] == "done", harness.brief()
    assert codec_of(tools, Path(one["output_path"])) == "hevc" and codec_of(tools, Path(two["output_path"])) == "h264"


# ---- 이어받기 시각 ----
def test_resume_point_tolerates_playlist_time_rounding():
    """조각 길이가 2.002초(59.94fps)면 앞 파일의 끝 시각이 다음 조각의 시각(밀리초로 적힌다)보다
    몇 마이크로초 뒤가 된다. 그 조각을 건너뛰지 않는다."""
    from datetime import datetime, timedelta, timezone

    from live_recorder.recording.receiver import Segment, start_index

    t0 = datetime(2026, 11, 24, 9, 55, 7, 192000, tzinfo=timezone.utc)
    jitter = [0, 3, -2, -6, 4, 0]  # 재생목록 시각의 마이크로초 흔들림
    segs = [Segment(100 + i, f"s{i}", 2.002, t0 + timedelta(seconds=2.002 * i, microseconds=jitter[i]), None) for i in range(6)]
    last_end = segs[2].pdt + timedelta(seconds=2.002)  # 앞 파일이 조각 102까지 받았다
    assert last_end > segs[3].pdt  # 끝 시각이 다음 조각의 시각보다 뒤다
    assert start_index(segs, rewind=0, start_after=last_end, live_edge=3) == 3
    assert start_index(segs, rewind=0, start_after=segs[3].pdt, live_edge=3) == 3  # 딱 맞는 시각
    assert start_index(segs, rewind=0, start_after=segs[5].pdt + timedelta(seconds=2.002), live_edge=3) == 6  # 아직 없다


# ---- AV1 ----
@pytest.fixture(scope="module")
def av1_part(tools, tmp_path_factory) -> Path:
    encoders = subprocess.run([tools[0], "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    enc = next((e for e in ("libsvtav1", "libaom-av1", "librav1e") if e in encoders), None)
    if enc is None:
        pytest.skip("AV1 인코더가 없는 ffmpeg다")
    out = tmp_path_factory.mktemp("av1") / "방송.mp4.part"
    extra = ["-cpu-used", "8"] if enc == "libaom-av1" else ["-preset", "12"] if enc == "libsvtav1" else ["-speed", "10"]
    subprocess.run([tools[0], "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050", "-t", "4", "-c:v", enc, *extra,
                    "-pix_fmt", "yuv420p", "-g", "10", "-c:a", "aac", "-b:a", "24k",
                    "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(out)], check=True)
    return out


@pytest.mark.parametrize("ext", [".mp4", ".ts"])
def test_av1_original_is_recognised_and_converted(tmp_path, av1_part, tools, ext):
    """AV1 원본(조각난 MP4)의 코덱을 읽고, 복사 변환한 결과도 AV1이다."""
    assert [c for _, c in codec_mod.scan_file(av1_part)] == ["av1"]
    part = tmp_path / "rec" / "방송.mp4.part"
    part.parent.mkdir()
    part.write_bytes(av1_part.read_bytes())
    final = part.parent / f"방송{ext}"
    cfg = AppConfig(data_dir=tmp_path / "data", record_root=tmp_path, ffmpeg=tools[0], ffprobe=tools[1])
    fin = Finalizer(cfg, st.Store(tmp_path / "data" / "db.sqlite3", tmp_path))
    status, detail, out = asyncio.run(fin.finalize_file(part, final, Settings(), expected=4.0))
    assert status == "done", detail
    assert codec_of(tools, out) == "av1"
    assert out.suffix == ".mp4"  # AV1은 .ts로 만들지 않는다. .ts 채널이어도 .mp4로 저장하고 까닭을 남긴다
    assert (".mp4로 저장" in detail) == (ext == ".ts")
    frames, errors = decoded_frames(tools, out) if "libdav1d" in subprocess.run(
        [tools[0], "-hide_banner", "-decoders"], capture_output=True, text=True).stdout else (40, "")
    assert errors == "" and frames == 40


# ---- 마무리 검증 ----
def mixed_part(media: Path, hevc_media: Path, folder: Path) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / "섞임.mp4"
    part = Path(str(final) + ".part")
    with open(part, "wb") as f:
        f.write((hevc_media / "init.mp4").read_bytes())
        for i in range(5):
            f.write((hevc_media / f"seg{i:05d}.m4s").read_bytes())
        f.write((media / "init.mp4").read_bytes())
        for i in range(5, 10):
            f.write((media / f"seg{i:05d}.m4s").read_bytes())
    return part, final


def test_scan_file_finds_codec_changes(tmp_path, media, hevc_media):
    part, _ = mixed_part(media, hevc_media, tmp_path / "x")
    found = codec_mod.scan_file(part)
    assert [c for _, c in found] == ["hevc", "h264"] and found[0][0] < found[1][0]
    assert part.read_bytes()[found[1][0] + 4:found[1][0] + 8] in (b"moov", b"ftyp") or found[1][0] > 0
    single = tmp_path / "single.part"
    single.write_bytes((media / "init.mp4").read_bytes() + (media / "seg00000.m4s").read_bytes()
                       + (media / "init.mp4").read_bytes() + (media / "seg00001.m4s").read_bytes())
    assert [c for _, c in codec_mod.scan_file(single)] == ["h264"]  # 같은 코덱의 초기화 조각이 또 나와도 하나로 센다
    cut = tmp_path / "cut.part"
    cut.write_bytes(part.read_bytes()[:-37])  # 끝이 잘린 파일
    assert [c for _, c in codec_mod.scan_file(cut)] == ["hevc", "h264"]
    ts = tmp_path / "x.ts"
    ts.write_bytes(b"G" + b"\x00" * 187)
    assert codec_mod.scan_file(ts) == [] and codec_mod.scan_file(tmp_path / "none") == []


def test_mixed_codec_original_is_not_converted(tmp_path, media, hevc_media, tools):
    """코덱이 섞인 원본은 변환하지 않고 남긴다. 변환하면 오류 없이 끝나고 길이도 맞지만 뒤쪽이 풀리지 않는다."""
    part, final = mixed_part(media, hevc_media, tmp_path / "rec")
    cfg = AppConfig(data_dir=tmp_path / "data", record_root=tmp_path, ffmpeg=tools[0], ffprobe=tools[1])
    fin = Finalizer(cfg, st.Store(tmp_path / "data" / "db.sqlite3", tmp_path))
    status, detail, kept = asyncio.run(fin.finalize_file(part, final, Settings(), expected=10.0))
    assert status == "failed" and "코덱이 섞여" in detail and "HEVC → H.264" in detail, detail
    assert kept is not None and kept.name == "섞임.raw.mp4" and kept.exists()
    assert not final.exists() and not part.exists()
    assert sorted(p.name for p in final.parent.iterdir()) == ["섞임.raw.mp4"]
    # 남긴 원본으로 다시 마무리해도 같은 까닭으로 거부하고 원본은 그대로다
    status, detail, kept2 = asyncio.run(fin.finalize_file(kept, final, Settings(), expected=10.0, source_is_raw=True))
    assert status == "failed" and "코덱이 섞여" in detail and kept2 == kept and kept.exists()


def test_scan_runs_only_for_sessions_without_a_recorded_codec():
    """코덱이 기록된 세션은 코덱을 고정하는 수신기가 받은 것이라 원본을 훑지 않는다."""
    assert Finalizer._needs_scan({"video_codec": None}) and Finalizer._needs_scan({})
    assert not Finalizer._needs_scan({"video_codec": "hevc"})
