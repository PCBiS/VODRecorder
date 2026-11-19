"""배포 환경 설정(AppConfig)과 운영 설정(Settings).

AppConfig는 환경변수나 CLI 인자로만 정한다(도구 경로, 데이터 폴더, 허용 호스트).
Settings는 저장소에 두고 저장할 때 검증한다.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import naming

MIN_CHECK_INTERVAL = 10


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    record_root: Path
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    receiver_cmd: tuple[str, ...] = (sys.executable, "-m", "live_recorder.recording.receiver")
    # 기본 플랫폼의 API 주소·허용 호스트·자격 확인 주소. 비어 있으면 플랫폼 모듈의 기본값을 쓴다.
    # 환경변수 이름(LIVE_RECORDER_API_BASE, LIVE_RECORDER_ALLOWED_HOSTS, LIVE_RECORDER_USER_STATUS_URL)은 그대로다
    api_base: str | None = None
    allowed_hosts: tuple[str, ...] | None = None
    allow_http: bool = False  # 시험용 로컬 서버에만 쓴다
    telegram_api_base: str = "https://api.telegram.org"
    user_status_url: str | None = None
    log_dir_override: Path | None = None  # LIVE_RECORDER_LOG_DIR

    @property
    def db_path(self) -> Path:
        return self.data_dir / "recorder.sqlite3"

    @property
    def account_path(self) -> Path:
        return self.data_dir / "secrets" / "account.json"

    @property
    def setup_code_path(self) -> Path:
        """계정이 없는 동안만 있는 첫 설정 코드(0600)."""
        return self.data_dir / "secrets" / "setup-code"

    @property
    def api_keys_path(self) -> Path:
        """접근 키의 해시(0600)."""
        return self.data_dir / "secrets" / "api-keys.json"

    @property
    def telegram_path(self) -> Path:
        return self.data_dir / "secrets" / "telegram.json"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "instance.lock"

    @property
    def log_dir(self) -> Path:
        """따로 지정하지 않으면 데이터 폴더 아래 logs다."""
        return self.log_dir_override or self.data_dir / "logs"

    @classmethod
    def from_env(cls, data_dir: str | None = None, record_root: str | None = None,
                 log_dir: str | None = None) -> "AppConfig":
        """명령줄 → 환경변수 → 운영체제 관례의 기본값(core.locations). 컨테이너 이미지는 환경변수로 준다."""
        from . import locations

        env = os.environ
        data = data_dir or env.get("LIVE_RECORDER_DATA_DIR")
        root = record_root or env.get("LIVE_RECORDER_RECORD_ROOT")
        logs_given = log_dir or env.get("LIVE_RECORDER_LOG_DIR")
        defaults = None
        if not data or not root or (not logs_given and not data):
            defaults = locations.defaults()
        kwargs: dict[str, Any] = {}
        if not data:
            data = str(defaults.data)
            if not logs_given:
                kwargs["log_dir_override"] = defaults.logs  # 자료 폴더를 정하지 않았으면 로그도 관례 위치
        if not root:
            root = str(defaults.recordings)
        if env.get("LIVE_RECORDER_FFMPEG"):
            kwargs["ffmpeg"] = env["LIVE_RECORDER_FFMPEG"]
        if env.get("LIVE_RECORDER_FFPROBE"):
            kwargs["ffprobe"] = env["LIVE_RECORDER_FFPROBE"]
        if env.get("LIVE_RECORDER_RECEIVER_CMD"):
            cmd = json.loads(env["LIVE_RECORDER_RECEIVER_CMD"])
            if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
                raise ConfigError("LIVE_RECORDER_RECEIVER_CMD는 문자열 JSON 배열이어야 합니다")
            kwargs["receiver_cmd"] = tuple(cmd)
        if env.get("LIVE_RECORDER_API_BASE"):
            kwargs["api_base"] = env["LIVE_RECORDER_API_BASE"].rstrip("/")
        if env.get("LIVE_RECORDER_ALLOWED_HOSTS"):
            kwargs["allowed_hosts"] = tuple(h.strip() for h in env["LIVE_RECORDER_ALLOWED_HOSTS"].split(",") if h.strip())
        if env.get("LIVE_RECORDER_ALLOW_HTTP") == "1":
            kwargs["allow_http"] = True
        if env.get("LIVE_RECORDER_TELEGRAM_API_BASE"):  # 시험용 가짜 서버
            kwargs["telegram_api_base"] = env["LIVE_RECORDER_TELEGRAM_API_BASE"].rstrip("/")
        if logs_given:
            kwargs["log_dir_override"] = Path(logs_given).expanduser().resolve()
        if env.get("LIVE_RECORDER_USER_STATUS_URL"):
            kwargs["user_status_url"] = env["LIVE_RECORDER_USER_STATUS_URL"]
        return cls(data_dir=Path(data).expanduser().resolve(), record_root=Path(root).expanduser().resolve(), **kwargs)


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("true", "on", "1", "yes"):
        return True
    if isinstance(v, str) and v.lower() in ("false", "off", "0", "no"):
        return False
    raise ConfigError(f"참/거짓 값이 아닙니다: {v!r}")


def _int_range(lo: int, hi: int | None):
    def conv(v: Any) -> int:
        if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
            raise ConfigError(f"정수가 아닙니다: {v!r}")
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ConfigError(f"정수가 아닙니다: {v!r}") from None
        if n < lo or (hi is not None and n > hi):
            raise ConfigError(f"범위를 벗어났습니다({lo}~{hi if hi is not None else '∞'}): {n}")
        return n

    return conv


def _proxies(v: Any) -> tuple[str, ...]:
    """신뢰할 프록시 주소(IP나 CIDR) 목록."""
    items = _str_list(v)
    out = []
    for item in items:
        try:
            out.append(str(ipaddress.ip_network(item, strict=False)))
        except ValueError:
            raise ConfigError(f"IP 주소나 CIDR가 아닙니다: {item}") from None
    return tuple(out)


def _str_list(v: Any) -> tuple[str, ...]:
    if isinstance(v, str):
        parts = [x.strip() for x in v.replace(",", "\n").splitlines()]
    elif isinstance(v, (list, tuple)) and all(isinstance(x, str) for x in v):
        parts = [x.strip() for x in v]
    else:
        raise ConfigError("문자열 목록이어야 합니다")
    parts = [x for x in parts if x]
    if len(parts) > 32 or any(len(x) > 1024 or "\x00" in x for x in parts):
        raise ConfigError("목록이 너무 깁니다")
    return tuple(dict.fromkeys(parts))


def _abs_paths(v: Any) -> tuple[str, ...]:
    """절대 경로 목록. 존재 여부와 금지 경로는 쓸 때 다시 본다(files.py)."""
    out = []
    for item in _str_list(v):
        p = Path(item)
        if not p.is_absolute() or ".." in p.parts:
            raise ConfigError(f"절대 경로여야 하고 '..'을 쓸 수 없습니다: {item}")
        out.append(os.path.normpath(item))
    return tuple(dict.fromkeys(out))


def _rel_dir(v: Any) -> str:
    """녹화 루트 아래 폴더(상대 경로). 실제 위치 검사는 저장소가 한다."""
    if not isinstance(v, str) or not v.strip() or "\x00" in v or ".." in Path(v).parts:
        raise ConfigError("폴더는 녹화 루트 아래 경로여야 합니다")
    return v.strip()


# 확장의 설정 검증(SettingSpec)이 같이 쓰는 검사 함수
def bool_value(v: Any) -> bool:
    return _bool(v)


def int_range(lo: int | None, hi: int | None):
    return _int_range(lo, hi)


def rel_dir(v: Any) -> str:
    return _rel_dir(v)


def stored_pattern_problem(v: Any) -> str | None:
    """저장된 파일명 패턴을 쓸 수 없는 까닭(모르는 토큰, 깨진 패턴). 쓸 수 있으면 None."""
    if not isinstance(v, str):
        return "파일명 패턴은 문자열이어야 합니다"
    try:
        naming.validate_tokens(v)
    except ValueError as e:
        return str(e)
    return None


def _pattern(v: Any) -> str:
    if not isinstance(v, str):
        raise ConfigError("파일명 패턴은 문자열이어야 합니다")
    try:
        naming.validate_pattern(v)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    return v


def _choice(choices: Any, what: str):
    """목록에서만 고르는 값."""
    allowed = tuple(choices)

    def conv(v: Any) -> str:
        if v not in allowed:
            raise ConfigError(f"{what}은 목록에서 고릅니다: {', '.join(allowed)}")
        return v

    return conv


# 화면의 날짜·시각 모양(파일 이름 모양과 따로다). 화면(app.js의 formatWhen)이 같은 이름을 안다
DISPLAY_DATE_SHAPES = ("YYYY-MM-DD", "YYYY.MM.DD", "YYYY/MM/DD", "YY.MM.DD", "MM/DD", "M월 D일")
DISPLAY_TIME_SHAPES = ("HH:MM", "HH:MM:SS", "오전/오후 H:MM")


# 기본값은 실제 방송을 녹화해 본 값에 맞췄다.
@dataclass(frozen=True)
class Settings:
    check_interval: int = 60
    filename_pattern: str = "[{date} {time}] {channel_name} {safe_live_title} {record_quality}{frame_rate}{file_extension}"
    # 파일 이름의 날짜·시각 모양. 이전 빌드의 설정에는 없으므로 없으면 이 기본값이다
    filename_date_format: str = naming.DEFAULT_DATE_SHAPE
    filename_time_format: str = naming.DEFAULT_TIME_SHAPE
    # 화면의 날짜·시각 모양
    display_date_format: str = DISPLAY_DATE_SHAPES[0]
    display_time_format: str = DISPLAY_TIME_SHAPES[0]
    timemachine_enabled: bool = True
    rewind_seconds: int = 3600
    remux_enabled: bool = True
    delete_original: bool = True
    min_free_bytes: int = 5 * 1024**3
    stall_seconds: int = 120
    finalize_concurrency: int = 2
    finalize_low_priority: bool = True  # 변환과 전체 길이 측정(확장의 받기 프로세스도 따른다)을 낮은 CPU·입출력 우선순위로
    url_refresh_seconds: int = 5 * 3600
    endlist_wait_seconds: int = 15  # 종료 표시로 끝난 뒤 상태 재확인까지 기다리는 초(0이면 기다리지 않음)
    # 텔레그램 알림. 토큰과 chat_id는 여기 두지 않는다(비밀 저장소)
    notify_enabled: bool = False
    notify_dedupe_seconds: int = 300
    notify_start_failed: bool = True
    notify_abnormal_end: bool = True
    notify_timemachine_fallback: bool = True
    notify_finalize_failed: bool = True
    notify_disk_low: bool = True
    notify_cookie_expired: bool = True
    notify_record_start: bool = True
    notify_record_end: bool = True
    notify_user_stop: bool = True
    notify_finalize_done: bool = True
    # 웹
    session_minutes: int = 60  # 쓰지 않은 시간 기준. 0이면 제한 없음
    login_limit_enabled: bool = True
    trusted_proxies: tuple[str, ...] = ()
    # 파일 관리자
    fm_enabled: bool = False
    fm_roots: tuple[str, ...] = ()
    fm_readonly: bool = False
    fm_trash: bool = False
    fm_show_hidden: bool = False

    VALIDATORS = {
        "check_interval": _int_range(MIN_CHECK_INTERVAL, 86400),
        "filename_pattern": _pattern,
        "filename_date_format": _choice(naming.DATE_SHAPES, "파일 이름 날짜 모양"),
        "filename_time_format": _choice(naming.TIME_SHAPES, "파일 이름 시각 모양"),
        "display_date_format": _choice(DISPLAY_DATE_SHAPES, "화면 날짜 모양"),
        "display_time_format": _choice(DISPLAY_TIME_SHAPES, "화면 시각 모양"),
        "timemachine_enabled": _bool,
        "rewind_seconds": _int_range(0, 3600),
        "remux_enabled": _bool,
        "delete_original": _bool,
        "min_free_bytes": _int_range(0, None),
        "stall_seconds": _int_range(5, 3600),
        "finalize_concurrency": _int_range(1, 8),
        "finalize_low_priority": _bool,
        "url_refresh_seconds": _int_range(60, 86400),
        "endlist_wait_seconds": _int_range(0, 600),
        "notify_enabled": _bool,
        "notify_dedupe_seconds": _int_range(0, 86400),
        **{f"notify_{name}": _bool for name in (
            "start_failed", "abnormal_end", "timemachine_fallback", "finalize_failed", "disk_low", "cookie_expired",
            "record_start", "record_end", "user_stop", "finalize_done")},
        "session_minutes": _int_range(0, 525600),
        "login_limit_enabled": _bool,
        "trusted_proxies": _proxies,
        "fm_enabled": _bool,
        "fm_roots": _abs_paths,
        "fm_readonly": _bool,
        "fm_trash": _bool,
        "fm_show_hidden": _bool,
    }

    @classmethod
    def keys(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    @classmethod
    def validate(cls, key: str, value: Any) -> Any:
        """정해진 키와 타입만 받는다. 모르는 키와 범위 밖 값은 거부한다."""
        conv = cls.VALIDATORS.get(key)
        if conv is None:
            raise ConfigError(f"모르는 설정 키: {key}")
        return conv(value)

    @classmethod
    def from_mapping(cls, stored: dict[str, Any]) -> "Settings":
        kwargs = {}
        for key in cls.keys():
            if key in stored:
                if key == "filename_pattern":
                    # 예전에 저장된 패턴의 토큰 밖 금지 글자는 읽을 때 거부하지 않고 파일명을 만들 때 `_`로 바꾼다.
                    # 모르는 토큰이 있거나 깨진 패턴이면 서버를 멈추지 않고 기본 패턴을 쓴다(저장된 값은 그대로 둔다)
                    if stored_pattern_problem(stored[key]) is None:
                        kwargs[key] = stored[key]
                    continue
                kwargs[key] = cls.validate(key, stored[key])
        return cls(**kwargs)
