"""화질 값 해석과 트랙 선택."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import codec as codec_mod

HEIGHTS = (144, 240, 360, 480, 720, 1080, 1440, 2160)
_NUM = re.compile(r"^(\d{3,4})(?:p(\d{1,3})?)?$")


@dataclass(frozen=True)
class Track:
    track_id: str
    height: int | None
    fps: float | None
    bitrate: int | None
    audio_only: bool = False
    codec: str = ""  # 짧은 이름(h264·hevc·av1). 플랫폼이 알려 주지 않으면 빈 문자열

    @property
    def name(self) -> str:
        return f"{self.height}p" if self.height else self.track_id


def normalize(value: object) -> str:
    """`1080`·`1080p60`은 `1080p`로, 해석할 수 없으면 `best`로 읽는다."""
    text = str(value or "").strip().lower()
    if text in ("best", "worst"):
        return text
    m = _NUM.match(text)
    if m and int(m.group(1)) in HEIGHTS:
        return f"{int(m.group(1))}p"
    return "best"


def candidates(tracks: list[Track], prefer: str | None = None) -> list[Track]:
    """오디오 전용을 빼고 높이마다 하나만 남긴다.

    같은 높이가 여럿이면 먼저 코덱 우선순위(`prefer`)에 맞는 것을 고른다. 맞는 것이 없으면 있는 것 가운데서 고른다.
    그 안에서는 (프레임레이트, 비트레이트)가 가장 높은 것, 그것도 같으면 방송이 먼저 내놓은 것이다."""
    by_height: dict[int, list[Track]] = {}
    for t in tracks:
        if t.audio_only or not t.height:
            continue
        by_height.setdefault(t.height, []).append(t)
    out: list[Track] = []
    for height in sorted(by_height):
        group = by_height[height]
        if prefer and prefer != codec_mod.ANY:
            wanted = [t for t in group if t.codec == prefer]
            group = wanted or group
        best = group[0]
        for t in group[1:]:
            if (t.fps or 0.0, t.bitrate or 0) > (best.fps or 0.0, best.bitrate or 0):
                best = t
        out.append(best)
    return out


def select(tracks: list[Track], wanted: str, prefer: str | None = None) -> Track | None:
    """후보가 없으면 None. `prefer`는 코덱 우선순위."""
    cands = candidates(tracks, prefer)
    if not cands:
        return None
    raw = str(wanted or "").strip()
    for t in cands:
        if t.track_id == raw:
            return t
    want = normalize(raw)
    if want == "best":
        return cands[-1]
    if want == "worst":
        return cands[0]
    for t in cands:
        if t.name == want:
            return t
    target = int(want[:-1])
    lower = [t for t in cands if (t.height or 0) <= target]
    return lower[-1] if lower else cands[-1]


def label(track: Track | None, wanted: str) -> tuple[str, str]:
    """파일명에 쓸 (화질, 프레임레이트). 모르면 화질은 요청값, 프레임레이트는 빈 값."""
    if track is None or not track.height:
        return normalize(wanted), ""
    fps = ""
    if track.fps:
        fps = str(int(round(track.fps)))
    return f"{track.height}p", fps
