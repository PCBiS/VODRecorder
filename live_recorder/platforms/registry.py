"""플랫폼 등록. 들어 있는 플랫폼은 늘 등록돼 있고 채널을 추가할 때 고른다.

녹화 중심부와 웹 서버는 여기서 플랫폼 객체를 얻는다. 시험은 `register`로 가짜 플랫폼을 더할 수 있다.
"""

from __future__ import annotations

import importlib

from .base import Platform

# 기본 플랫폼. 플랫폼을 적지 않은 옛 자료(플랫폼 열이 없던 저장소, 쿠키 파일, API 요청)는 이 플랫폼의 것이다
DEFAULT = "chzzk"
# 제품에 들어 있는 플랫폼 모듈. 처음 찾을 때 불러온다
BUILTIN = {"chzzk": "live_recorder.platforms.chzzk", "youtube": "live_recorder.platforms.youtube",
           "cime": "live_recorder.platforms.cime"}

_registered: dict[str, Platform] = {}


def register(platform: Platform) -> None:
    _registered[platform.id] = platform


def unregister(platform_id: str) -> None:
    _registered.pop(platform_id, None)


def _load_builtin(platform_id: str) -> Platform | None:
    module = BUILTIN.get(platform_id)
    if module is None:
        return None
    platform = importlib.import_module(module).PLATFORM
    _registered.setdefault(platform.id, platform)
    return _registered[platform.id]


def get(platform_id: str | None) -> Platform:
    """없는 플랫폼이면 KeyError."""
    pid = platform_id or DEFAULT
    platform = _registered.get(pid) or _load_builtin(pid)
    if platform is None:
        raise KeyError(pid)
    return platform


def find(platform_id: str | None) -> Platform | None:
    try:
        return get(platform_id)
    except KeyError:
        return None


def all_platforms() -> list[Platform]:
    """등록 순서. 기본 플랫폼이 맨 앞이다."""
    for pid in BUILTIN:
        find(pid)
    # 들어 있는 플랫폼은 BUILTIN 순서(불러온 차례와 상관없이), 그 뒤에 시험 등이 등록한 것
    ordered = [_registered[pid] for pid in BUILTIN if pid in _registered]
    return ordered + [p for pid, p in _registered.items() if pid not in BUILTIN]


def module_name(platform: Platform) -> str:
    """수신기 프로세스가 불러올 모듈 이름. 그 모듈의 `PLATFORM`이 이 객체다."""
    return type(platform).__module__


def load_module_platform(module: str) -> Platform:
    """수신기 쪽: 작업에 적힌 모듈 이름으로 플랫폼을 불러온다."""
    return importlib.import_module(module).PLATFORM
