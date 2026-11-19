"""파일명 만들기.

`{date}`·`{time}`의 날짜·시각 모양은 설정에서 고른다. 목록에 있는 모양만 쓴다.
`{recording_time}`·`{broadcast_time}`·`{start_time}`은 고정된 모양이다.
"""

from __future__ import annotations

import os
import re
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..platforms import registry

KST = timezone(timedelta(hours=9), "KST")


def seoul():
    """Asia/Seoul 시간대(여럿이 쓰는 한국 시각). 시간대 자료가 없는 이미지에서는 고정 +9시간."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Seoul")
    except Exception:  # noqa: BLE001 - 시간대 자료가 없는 이미지
        return KST
# 쓸 수 있는 토큰과 뜻. 서버가 받아 주는 토큰 목록(TOKENS)과 설정 화면의 도움말 표가 모두 이 목록에서 나온다
TOKEN_HELP: tuple[tuple[str, str], ...] = (
    ("date", "파일을 만드는 날짜(모양은 아래에서 고름)"),
    ("time", "파일을 만드는 시각(모양은 아래에서 고름)"),
    ("recording_time", "녹화 시작 날짜_시각(yymmdd_HHMMSS 고정)"),
    ("broadcast_time", "방송 시작 날짜_시각(yymmdd_HHMMSS 고정)"),
    ("start_time", "방송 시작 날짜(YYYY-MM-DD 고정)"),
    ("channel_name", "채널 이름"),
    ("platform", "플랫폼(예: chzzk)"),
    ("safe_live_title", "방송 제목(파일 이름에 못 쓰는 글자는 _로 바뀜)"),
    ("record_quality", "해상도(예: 1080p)"),
    ("frame_rate", "프레임(예: 60)"),
    ("file_extension", "확장자(점 포함. 패턴에 없으면 끝에 붙음)"),
)
TOKENS = frozenset(name for name, _ in TOKEN_HELP)
# 파일 이름의 날짜·시각 모양. 화면의 이름 → strftime 형식. `/`와 `:`는 파일 이름에 쓸 수 없어 없다
DATE_SHAPES: dict[str, str] = {
    "YYYY-MM-DD": "%Y-%m-%d", "YYYY.MM.DD": "%Y.%m.%d", "YYYY_MM_DD": "%Y_%m_%d", "YYYYMMDD": "%Y%m%d",
    "YY-MM-DD": "%y-%m-%d", "YY.MM.DD": "%y.%m.%d", "YYMMDD": "%y%m%d",
}
TIME_SHAPES: dict[str, str] = {
    "HHhMMmSSs": "%Hh%Mm%Ss", "HH-MM-SS": "%H-%M-%S", "HH.MM.SS": "%H.%M.%S", "HHMMSS": "%H%M%S",
    "HH시MM분SS초": "%H시%M분%S초", "HHhMMm": "%Hh%Mm", "HH-MM": "%H-%M",
}
DEFAULT_DATE_SHAPE = "YYYY-MM-DD"
DEFAULT_TIME_SHAPE = "HHhMMmSSs"  # 기본 둘로 `{date} {time}`은 `YYYY-MM-DD HHhMMmSSs`다
DEFAULT_STEM = "recording"
NAME_MAX_BYTES = 255
# 녹화·변환 중에 붙는 꼬리표(`.part`, `.remux.part`, `.raw.mp4`)와 중복 번호(` (999)`) 몫을 남긴다.
RESERVED_BYTES = 24
PART_SUFFIX = ".part"
# 영상과 음성을 따로 받는 방송의 음성 원본. `<최종이름>.audio.part`
AUDIO_PART_SUFFIX = ".audio" + PART_SUFFIX


def audio_part(part) -> Path:
    """녹화 원본(`<최종이름>.part`) 옆의 음성 원본 경로."""
    text = str(part)
    if text.endswith(PART_SUFFIX):
        text = text[: -len(PART_SUFFIX)]
    return Path(text + AUDIO_PART_SUFFIX)

_BAD = re.compile(r'[\\/*?:"<>|+]')
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_SPACES = re.compile(r"\s+")


def literal_problem(pattern: str) -> str | None:
    """패턴에 직접 적은 글자(토큰 밖)의 금지 글자·줄바꿈·제어 문자. 없으면 None."""
    try:
        parsed = list(string.Formatter().parse(pattern))
    except ValueError:
        return None  # 깨진 패턴은 validate_tokens가 따로 거부한다
    for literal, _, _, _ in parsed:
        if _CTRL.search(literal):
            return "줄바꿈이나 제어 문자"
        bad = _BAD.search(literal)
        if bad and bad.group(0) != "+":
            return f"'{bad.group(0)}'"
    return None


def safe_literals(pattern: str) -> str:
    """이미 저장된 패턴의 토큰 밖 금지 글자·줄바꿈·제어 문자를 `_`로 바꾼다(폴더 밖 경로를 만들지 않게)."""
    out = []
    for literal, field, spec, conv in string.Formatter().parse(pattern):
        literal = _CTRL.sub("_", literal)
        literal = re.sub(r'[\\/*?:"<>|]', "_", literal)
        out.append(literal.replace("{", "{{").replace("}", "}}"))
        if field is not None:
            out.append("{" + field + (f"!{conv}" if conv else "") + (f":{spec}" if spec else "") + "}")
    return "".join(out)


def validate_pattern(pattern: str) -> None:
    """모르는 토큰, 깨진 패턴, 토큰 밖의 금지 글자를 거부한다(설정을 저장할 때)."""
    validate_tokens(pattern)
    problem = literal_problem(pattern)
    if problem:
        raise ValueError(f"파일명 패턴에 파일 이름에 쓸 수 없는 글자가 있습니다: {problem}. 토큰 밖에는 \\ / * ? : \" < > | 와 줄바꿈을 쓸 수 없습니다")


def validate_tokens(pattern: str) -> None:
    """모르는 토큰과 깨진 패턴을 거부한다."""
    if not pattern or not pattern.strip():
        raise ValueError("파일명 패턴이 비었습니다")
    try:
        parsed = list(string.Formatter().parse(pattern))
    except ValueError as e:
        raise ValueError(f"파일명 패턴이 깨졌습니다: {e}") from None
    for _, field, spec, conv in parsed:
        if field is None:
            continue
        if field not in TOKENS:
            raise ValueError(f"모르는 토큰: {{{field}}}")
        if spec or conv:
            raise ValueError(f"토큰에 형식 지정을 쓸 수 없습니다: {{{field}}}")


def sanitize(value: object) -> str:
    """파일 이름에 못 쓰는 글자를 바꾼다."""
    text = str(value if value is not None else "")
    text = text.replace("\r", " ").replace("\n", " ")
    text = _CTRL.sub(" ", text)
    text = _BAD.sub("_", text)
    text = _SPACES.sub(" ", text)
    return text.strip(" ._")


def _truncate_bytes(text: str, limit: int) -> str:
    """UTF-8 바이트 수로 자르되 글자 중간에서 자르지 않는다."""
    if limit <= 0:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore")


def fmt_shape(dt: datetime, date_shape: str | None, time_shape: str | None) -> tuple[str, str]:
    """고른 모양의 (날짜, 시각). 시각은 늘 24시간제, KST다. 목록에 없는 모양은 거부한다."""
    if date_shape not in DATE_SHAPES:
        raise ValueError(f"파일 이름 날짜 모양이 목록에 없습니다: {date_shape}")
    if time_shape not in TIME_SHAPES:
        raise ValueError(f"파일 이름 시각 모양이 목록에 없습니다: {time_shape}")
    local = dt.astimezone(KST)
    return local.strftime(DATE_SHAPES[date_shape]), local.strftime(TIME_SHAPES[time_shape])


def fmt_compact(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%y%m%d_%H%M%S")


def token_values(*, title: str, channel_name: str, quality: str, frame_rate: str, extension: str,
                 broadcast_start: datetime, recording_start: datetime, now: datetime,
                 date_shape: str = DEFAULT_DATE_SHAPE, time_shape: str = DEFAULT_TIME_SHAPE,
                 platform: str | None = None) -> dict[str, str]:
    """토큰마다 들어가는 값. 파일명 만들기와 설정 화면의 도움말 표가 같이 쓴다."""
    day, clock = fmt_shape(now, date_shape, time_shape)
    values = {
        "date": day,
        "time": clock,
        "recording_time": fmt_compact(recording_start),
        "broadcast_time": fmt_compact(broadcast_start),
        "start_time": broadcast_start.astimezone(KST).strftime("%Y-%m-%d"),
        "channel_name": sanitize(channel_name),
        "platform": sanitize(platform or registry.DEFAULT),
        "safe_live_title": sanitize(title),
        "record_quality": sanitize(quality),
        "frame_rate": sanitize(frame_rate),
        "file_extension": extension if extension.startswith(".") else "." + extension,
    }
    assert set(values) == TOKENS
    return values


def render(
    pattern: str,
    *,
    title: str,
    channel_name: str,
    quality: str,
    frame_rate: str,
    extension: str,
    broadcast_start: datetime,
    recording_start: datetime,
    now: datetime,
    max_bytes: int = NAME_MAX_BYTES - RESERVED_BYTES,
    date_shape: str = DEFAULT_DATE_SHAPE,
    time_shape: str = DEFAULT_TIME_SHAPE,
    platform: str | None = None,
) -> str:
    """패턴으로 파일명(확장자 포함)을 만든다. 폴더 구분자는 들어가지 않는다."""
    validate_tokens(pattern)
    pattern = safe_literals(pattern)  # 이미 저장된 패턴의 금지 글자는 `_`로
    ext = extension if extension.startswith(".") else "." + extension
    fixed = token_values(title=title, channel_name=channel_name, quality=quality, frame_rate=frame_rate, extension=ext,
                         broadcast_start=broadcast_start, recording_start=recording_start, now=now,
                         date_shape=date_shape, time_shape=time_shape, platform=platform)
    fixed["file_extension"] = ""  # 확장자는 맨 끝에 한 번만 붙인다
    clean_title = fixed.pop("safe_live_title")

    def build(t: str) -> str:
        stem = pattern.format(safe_live_title=t, **fixed)
        stem = _SPACES.sub(" ", stem).strip(" ._")
        return stem

    budget = max_bytes - len(ext.encode("utf-8"))
    stem = build(clean_title)
    if len(stem.encode("utf-8")) > budget:
        over = len(stem.encode("utf-8")) - budget
        keep = len(clean_title.encode("utf-8")) - over
        stem = build(sanitize(_truncate_bytes(clean_title, keep)))
        if len(stem.encode("utf-8")) > budget:  # 제목을 다 지워도 길다
            stem = _truncate_bytes(stem, budget).strip(" ._")
    if not stem:
        stem = DEFAULT_STEM
    # 토큰이 있든 없든 확장자는 끝에 한 번만 붙는다
    return stem + ext


def unique_path(folder: Path, name: str) -> Path:
    """같은 이름(완성본·녹화 중·남겨 둔 원본)이 있으면 ` (1)`, ` (2)`를 붙인다."""
    stem, ext = os.path.splitext(name)
    n = 0
    while True:
        cand_stem = stem if n == 0 else f"{stem} ({n})"
        cand = folder / (cand_stem + ext)
        clash = cand.exists() or Path(str(cand) + PART_SUFFIX).exists()
        if not clash:
            try:
                clash = any(p.name.startswith(cand_stem + ".raw.") for p in folder.glob(glob_escape(cand_stem) + ".raw.*"))
            except OSError:
                clash = False
        if not clash:
            return cand
        n += 1


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[\]])", r"[\1]", text)
