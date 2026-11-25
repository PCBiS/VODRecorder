"""영상 코덱 이름과 판별.

코덱은 짧은 이름으로 다룬다: `h264`, `hevc`, `av1`, `vp9`. 모르면 빈 문자열이다.
판별 근거는 셋이다. 믿는 순서대로:
    1. 받은 초기화 조각(fMP4 `moov`)의 영상 샘플 엔트리
    2. 마스터 재생목록 변형의 `CODECS`
    3. 플랫폼 API가 알려 준 이름
"""

from __future__ import annotations

import os
from pathlib import Path

H264, HEVC, AV1, VP9 = "h264", "hevc", "av1", "vp9"
ANY = "any"  # 우선순위 설정에서만 쓴다: 방송이 내놓은 순서대로
KNOWN = (H264, HEVC, AV1, VP9)
PREFERENCES = (H264, HEVC, AV1, ANY)  # 채널 설정 `video_codec`에 넣을 수 있는 값
DEFAULT_PREFERENCE = H264
LABELS = {H264: "H.264", HEVC: "HEVC", AV1: "AV1", VP9: "VP9"}

# 샘플 엔트리·CODECS 앞머리 → 코덱
_FOURCC = {
    "avc1": H264, "avc3": H264,
    "hvc1": HEVC, "hev1": HEVC, "dvh1": HEVC, "dvhe": HEVC,
    "av01": AV1,
    "vp09": VP9,
}
_NAMES = {
    "h264": H264, "h.264": H264, "avc": H264, "avc1": H264,
    "hevc": HEVC, "h265": HEVC, "h.265": HEVC, "hvc1": HEVC, "hev1": HEVC,
    "av1": AV1, "av01": AV1,
    "vp9": VP9, "vp09": VP9,
}


def label(codec: str | None) -> str:
    """화면·알림에 쓰는 이름. 모르는 값은 그대로, 비면 `알 수 없음`."""
    if not codec:
        return "알 수 없음"
    return LABELS.get(codec, codec.upper())


def from_name(value: object) -> str:
    """플랫폼 API·ffprobe가 준 이름(`HEVC`, `H264`, `h265` …)."""
    return _NAMES.get(str(value or "").strip().lower(), "")


def from_codecs_attr(value: object) -> str:
    """`CODECS="hvc1.1.2.L123.90,mp4a.40.2"`에서 영상 코덱. 영상 항목이 없으면 빈 문자열."""
    for item in str(value or "").split(","):
        head = item.strip().split(".", 1)[0].lower()
        if head in _FOURCC:
            return _FOURCC[head]
    return ""


def normalize_preference(value: object) -> str:
    """채널 설정값. 모르는 값이면 ValueError."""
    text = str(value or "").strip().lower()
    if not text:
        return DEFAULT_PREFERENCE
    if text in PREFERENCES:
        return text
    raise ValueError("영상 코덱 우선순위는 h264, hevc, av1, any 가운데 하나입니다")


def order(items: list, prefer: str | None, codec_of=lambda x: x.codec) -> list:
    """우선순위에 맞는 것을 앞에 둔다. 나머지와 같은 것끼리는 원래 순서(방송이 내놓은 순서)를 지킨다.
    `any`나 빈 값이면 그대로다."""
    if not prefer or prefer == ANY:
        return list(items)
    return [x for x in items if codec_of(x) == prefer] + [x for x in items if codec_of(x) != prefer]


# ---- fMP4 ----
_CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl")
_MAX_INIT = 4 * 1024 * 1024  # 초기화 조각 하나로 읽을 상한


def _boxes(data: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        size = int.from_bytes(data[i:i + 4], "big")
        typ = data[i + 4:i + 8]
        head = 8
        if size == 1:
            if i + 16 > end:
                return
            size = int.from_bytes(data[i + 8:i + 16], "big")
            head = 16
        elif size == 0:
            size = end - i
        if size < head:
            return
        yield typ, i + head, min(i + size, end)
        i += size


def from_init(data: bytes) -> str:
    """fMP4 초기화 조각(또는 `moov`가 든 앞머리)에서 영상 코덱. 영상 트랙이 없거나 fMP4가 아니면 빈 문자열."""
    found = ""

    def walk(start: int, end: int, handler: list[str]) -> None:
        nonlocal found
        for typ, bs, be in _boxes(data, start, end):
            if found:
                return
            if typ == b"trak":
                walk(bs, be, [""])
            elif typ in _CONTAINERS:
                walk(bs, be, handler)
            elif typ == b"hdlr" and be - bs >= 12:
                handler[0] = data[bs + 8:bs + 12].decode("latin1")
            elif typ == b"stsd" and handler[0] == "vide":
                for entry, _, _ in _boxes(data, bs + 8, be):
                    name = entry.decode("latin1").lower()
                    found = _FOURCC.get(name, name.strip() or "")
                    return

    walk(0, len(data), [""])
    return found


def scan_file(path: Path | str, limit: int = 8) -> list[tuple[int, str]]:
    """조각난 MP4 파일을 훑어 `moov`마다 (파일 위치, 영상 코덱)을 돌려준다.

    상자 머리만 읽고 `mdat`는 건너뛴다. 잘린 끝이나 모르는 꼴을 만나면 거기서 멈춘다.
    코덱이 앞의 것과 같으면 넣지 않는다. 그래서 돌려준 목록의 길이가 2 이상이면 한 파일에 코덱이 섞인 것이다.
    fMP4가 아니면 빈 목록이다. `limit`개를 찾으면 멈춘다."""
    out: list[tuple[int, str]] = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            pos = 0
            first = True
            while pos + 8 <= size and len(out) < limit:
                f.seek(pos)
                head = f.read(16)
                if len(head) < 8:
                    break
                box = int.from_bytes(head[:4], "big")
                typ = head[4:8]
                hdr = 8
                if box == 1:
                    if len(head) < 16:
                        break
                    box = int.from_bytes(head[8:16], "big")
                    hdr = 16
                elif box == 0:
                    box = size - pos
                if box < hdr:
                    break
                if first:
                    if typ not in (b"ftyp", b"styp", b"moov", b"moof", b"sidx"):
                        return []
                    first = False
                if typ == b"moov":
                    if box > _MAX_INIT or pos + box > size:
                        break
                    f.seek(pos)
                    codec = from_init(f.read(box))
                    if codec and (not out or out[-1][1] != codec):
                        out.append((pos, codec))
                pos += box
    except OSError:
        return out
    return out
