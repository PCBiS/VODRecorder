"""영상·음성을 따로 두는 HLS. 가짜 서버로만 시험한다."""

import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import duration, fast_timings, Harness, probe
from live_recorder.core.naming import audio_part
from live_recorder.recording import receiver
from live_recorder.recording.finalize import Finalizer, raw_audio_of

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="hi",NAME="en",DEFAULT=NO,URI="a/hi-en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="hi",NAME="ko",DEFAULT=YES,URI="a/hi-ko.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="lo",NAME="x",URI="a/lo.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="mux",NAME="m",DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=256x144,AUDIO="lo"
v/144.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,AUDIO="hi"
v/1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360,AUDIO="mux"
v/360.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=700000,RESOLUTION=640x360
v/360b.m3u8
"""


def test_parse_audio_groups_and_choose():
    base = "https://cdn.example/m/master.m3u8?sig=1"
    variants = receiver.parse_master(MASTER, base)
    groups = receiver.parse_audio_groups(MASTER, base)
    by_height = {v.height: v for v in variants if v.audio}
    assert by_height[1080].audio == "hi" and by_height[144].audio == "lo"
    # DEFAULT=YES, 없으면 첫 항목
    assert receiver.choose_audio(groups, "hi") == "https://cdn.example/m/a/hi-ko.m3u8"
    assert receiver.choose_audio(groups, "lo") == "https://cdn.example/m/a/lo.m3u8"
    # URI가 없는 묶음과 AUDIO가 없는 variant는 음성이 영상에 섞여 있다
    assert receiver.choose_audio(groups, "mux") is None
    assert receiver.choose_audio(groups, None) is None
    v = receiver.choose_variant(variants, 1080)
    assert v.height == 1080 and receiver.choose_audio(groups, v.audio).endswith("hi-ko.m3u8")


def _segs(first: int, n: int, t0: datetime | None) -> list:
    return [receiver.Segment(first + i, f"s{first + i}", 5.0, t0 + timedelta(seconds=5 * i) if t0 else None, None)
            for i in range(n)]


def test_align_index():
    t0 = datetime(2026, 11, 13, 3, 0, tzinfo=timezone.utc)
    audio = _segs(100, 10, t0)
    # 영상 시작 시각을 덮는 조각
    assert receiver.align_index(audio, seq=None, pdt=t0 + timedelta(seconds=12)) == 2
    assert receiver.align_index(audio, seq=None, pdt=t0 + timedelta(seconds=15)) == 3
    assert receiver.align_index(audio, seq=None, pdt=t0 - timedelta(seconds=30)) == 0
    assert receiver.align_index(audio, seq=None, pdt=t0 + timedelta(seconds=60)) == 10  # 음성이 아직 거기까지 없다
    # 시각이 없으면 같은 번호
    plain = _segs(100, 10, None)
    assert receiver.align_index(plain, seq=104, pdt=t0) == 4
    assert receiver.align_index(plain, seq=120, pdt=None) == 10
    assert receiver.align_index(plain, seq=None, pdt=None) is None
    assert receiver.align_index([], seq=1, pdt=t0) is None


def test_audio_part_name():
    assert audio_part(Path("/r/a b.mp4.part")) == Path("/r/a b.mp4.audio.part")


def _split_harness(tmp_path, server, tools, split_media, **timings):
    server.split = split_media
    h = Harness(tmp_path, server, tools, timings=fast_timings(**timings))
    h.add_channel(quality="1080p")
    h.store.set_settings({"rewind_seconds": 0})
    return h


def test_split_record_and_merge(tmp_path, server, tools, split_media):
    """한 수신 프로세스가 두 재생목록을 따로 받고, 마무리가 다시 인코딩 없이 합친다."""
    h = _split_harness(tmp_path, server, tools, split_media)
    b = server.go_live(title="따로 음성", total=110, initial=20, rate=10.0)

    async def scenario():
        async with h:
            rec = await h.until(h.recording, what="녹화 시작")
            part = Path(rec["part_path"])
            await h.until(lambda: audio_part(part).exists() and audio_part(part).stat().st_size > 0, what="음성 원본")
            await asyncio.sleep(1.5)
            b.end()
            return (await h.until(lambda: h.finalized(1), timeout=60, what="마무리"))[0]

    s = asyncio.run(scenario())
    assert s["finalize_status"] == "done", h.brief()
    assert "음성 합침" in s["finalize_detail"] and "경고" not in s["finalize_detail"], s["finalize_detail"]
    out = Path(s["output_path"])
    info = probe(tools, out)
    kinds = [x["codec_type"] for x in info["streams"]]
    assert kinds.count("video") == 1 and kinds.count("audio") == 1, kinds
    codecs = {x["codec_type"]: x["codec_name"] for x in info["streams"]}
    assert codecs == {"video": "h264", "audio": "aac"}  # 다시 인코딩하지 않았다
    assert abs(duration(tools, out) - s["media_seconds"]) <= 2.0
    left = sorted(p.name for p in out.parent.iterdir())
    assert left == [out.name], left  # 영상·음성 원본 모두 지웠다(기본 설정: 원본 삭제)
    # 따로 받는 마스터에서 기본 음성만 받았다(DEFAULT=YES)
    assert server.count("/hls/") and not any("/alow/" in p for p, _ in server.calls)
    assert any("/aud/a" in p for p, _ in server.calls)


def test_split_audio_stall_ends_session(tmp_path, server, tools, split_media):
    """음성만 멈춰도 녹화 전체가 멈춘 것으로 본다."""
    h = _split_harness(tmp_path, server, tools, split_media, stream_timeout=2.0)
    server.go_live(title="음성 멈춤", total=110, initial=20, rate=10.0)

    async def scenario():
        async with h:
            rec = await h.until(h.recording, what="녹화 시작")
            part = Path(rec["part_path"])
            await h.until(lambda: audio_part(part).exists() and audio_part(part).stat().st_size > 0, what="음성 원본")
            server.audio_frozen = 30
            return await h.until(lambda: next((x for x in h.sessions() if x["ended_at"]), None), timeout=30,
                                 what="녹화 끝")

    s = asyncio.run(scenario())
    assert "stream_timeout" in (s["end_detail"] or ""), s


def _ff(tools, *args):
    subprocess.run([tools[0], "-nostdin", "-hide_banner", "-loglevel", "error", *args], check=True)


class _Settings:
    remux_enabled = False  # 꺼 두어도 음성을 붙이려면 합친다
    delete_original = False
    finalize_low_priority = False


def test_finalize_merge_length_warning_and_retry(tmp_path, tools, split_media):
    """길이 차이가 2초를 넘으면 기록에 경고. 실패하면 두 원본을 함께 남기고 다시 마무리가 함께 찾는다."""
    from live_recorder.core.config import AppConfig

    folder = tmp_path / "rec"
    folder.mkdir()
    final = folder / "방송.mp4"
    part = Path(str(final) + ".part")
    with open(part, "wb") as f:
        for i in range(10):
            f.write((split_media / f"v{i:05d}.ts").read_bytes())
    audio = audio_part(part)
    with open(audio, "wb") as f:
        for i in range(5):
            f.write((split_media / f"a{i:05d}.aac").read_bytes())
    cfg = AppConfig(data_dir=tmp_path / "d", record_root=folder, ffmpeg=tools[0], ffprobe=tools[1])
    fz = Finalizer(cfg, store=None)
    status, detail, out = asyncio.run(fz.finalize_file(part, final, _Settings(), audio=audio))
    assert status == "done", detail
    assert "음성 합침" in detail and "경고" in detail and "길이가 다릅니다" in detail, detail
    kept = sorted(p.name for p in folder.iterdir())
    assert kept == ["방송.mp4", "방송.raw.audio.aac", "방송.raw.ts"], kept
    assert raw_audio_of(folder / "방송.raw.ts") == folder / "방송.raw.audio.aac"

    # 변환 실패: 망가진 음성 원본이면 둘 다 그대로 남는다
    final2 = folder / "둘째.mp4"
    part2 = Path(str(final2) + ".part")
    part2.write_bytes((split_media / "v00000.ts").read_bytes())
    audio2 = audio_part(part2)
    audio2.write_bytes(b"\x00" * 4096)
    status, detail, out = asyncio.run(fz.finalize_file(part2, final2, _Settings(), audio=audio2))
    assert status == "failed" and "둘째.raw.ts" in detail and "둘째.raw.audio" in detail, detail
    assert not part2.exists() and not audio2.exists()


def test_merge_failure_keeps_both_and_notifies(tmp_path, server, tools, split_media):
    """합치기에 실패하면 두 원본을 지우지 않고 남기고 마무리 실패로 알린다."""
    h = Harness(tmp_path, server, tools)
    h.enable_telegram(notify_finalize_failed=True)
    h.add_channel()
    folder = h.root / "ch1"
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / "합치기 실패.mp4"
    part = Path(str(final) + ".part")
    part.write_bytes(b"".join((split_media / f"v{i:05d}.ts").read_bytes() for i in range(5)))
    audio_part(part).write_bytes(b"\x00" * 8192)  # 음성 원본이 깨졌다
    sid = h.store.create_session(channel_id="0123456789abcdef0123456789abcdef", live_id="1", title="합치기 실패",
                                 source="live", part_path=str(part), final_path=str(final))
    h.store.update_session(sid, ended_at="2026-11-13T00:00:00+00:00", finalize_status="pending")

    async def scenario():
        async with h:
            await h.until(lambda: h.finalized(1), what="마무리")
            await h.until(lambda: server.tg_messages, what="알림")

    asyncio.run(scenario())
    s = h.store.get_session(sid)
    assert s["finalize_status"] == "failed" and "원본 보존" in s["finalize_detail"], s
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["합치기 실패.raw.audio.ts", "합치기 실패.raw.ts"], names
    assert any("마무리 실패" in m["text"] for m in server.tg_messages)
