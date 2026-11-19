"""화질 값 해석과 트랙 선택."""

from __future__ import annotations

import re
from dataclasses import dataclass

HEIGHTS = (144, 240, 360, 480, 720, 1080, 1440, 2160)
_NUM = re.compile(r"^(\d{3,4})(?:p(\d{1,3})?)?$")


@dataclass(frozen=True)
class Track:
    track_id: str
    height: int | None
    fps: float | None
    bitrate: int | None
    audio_only: bool = False

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


def candidates(tracks: list[Track]) -> list[Track]:
    """오디오 전용을 빼고 높이마다 (프레임레이트, 비트레이트)가 가장 높은 것만 남긴다."""
    best: dict[int, Track] = {}
    for t in tracks:
        if t.audio_only or not t.height:
            continue
        cur = best.get(t.height)
        key = (t.fps or 0.0, t.bitrate or 0)
        if cur is None or key > (cur.fps or 0.0, cur.bitrate or 0):
            best[t.height] = t
    return sorted(best.values(), key=lambda t: t.height or 0)


def select(tracks: list[Track], wanted: str) -> Track | None:
    """후보가 없으면 None."""
    cands = candidates(tracks)
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
