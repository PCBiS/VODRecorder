"""접근 관문의 규칙. 어떤 경로가 로그인 없이 열리는지, 접근 키로 열리는지는 여기 한 곳에서 정한다.

표에 없는 경로는 로그인을 요구한다(기본 거부). PUBLIC: 언제나 열린다. VIEW: 관리자 계정이 없을 때(보기 전용 모드)만
로그인 없이 열린다. 계정이 있으면 VIEW 경로도 로그인을 요구한다. 계정이 없으면 PUBLIC·VIEW 밖은 모두 거부한다.
확장이 등록한 경로의 규칙은 서버가 뜰 때 이 표 뒤에 붙는다(`AccessTable`). 확장 경로에 규칙이 없으면 서버가 뜨지 않는다.

접근 키: `KEY_RULES`가 중심부의 모든 경로에 키 권한을 적는다(보기 KEY_VIEW, 운영 KEY_OPERATE, 키로는 불가 KEY_NONE).
확장의 경로는 확장의 `AccessRule.key`가 밝힌다. 어느 표에도 키 권한이 없는 경로는 키로 403이다(기본 거부).
경로 꼴의 `{이름}`은 `/`가 없는 한 마디에 맞는다.
"""

from __future__ import annotations

import re

from ..core.extensions import KEY_NONE, KEY_OPERATE, KEY_VIEW, LOGIN, PUBLIC, VIEW

__all__ = ["ACCESS_RULES", "KEY_RULES", "KEY_NONE", "KEY_OPERATE", "KEY_VIEW", "LOGIN", "PUBLIC", "VIEW", "AccessTable",
           "access_level", "key_permission", "rule_matches"]

ACCESS_RULES: tuple[tuple[str, str, str], ...] = (
    # (방식, 경로 또는 접두사(*로 끝남), 수준)
    ("GET", "/login", PUBLIC),
    ("GET", "/status", PUBLIC),
    ("GET", "/api/mode", PUBLIC),
    ("POST", "/api/login", PUBLIC),
    ("GET", "/static/*", PUBLIC),
    ("HEAD", "/static/*", PUBLIC),
    ("GET", "/", VIEW),
    ("GET", "/api/view/channels", VIEW),
    ("GET", "/api/view/sessions", VIEW),  # 기록 화면의 보기(줄인 필드)
    ("GET", "/setup", VIEW),  # 첫 설정 화면
    ("POST", "/api/setup", VIEW),  # 제출. 출처 검사(same_origin)는 그대로 거친다
    ("GET", "/api/logs", LOGIN),  # 로그 보기(읽기만)
    ("POST", "/api/logs/export", LOGIN),  # 로그 내려받기. CSRF 검사를 거친다
    ("GET", "/api/settings/filename-example", LOGIN),  # 파일 이름 예시(읽기만)
    ("GET", "/api/settings/filename-tokens", LOGIN),  # 토큰 설명 표(읽기만)
)

# 중심부 경로의 키 권한. 경로를 더하면 여기에도 적는다(적지 않으면 키로 열리지 않고 시험이 알린다)
KEY_RULES: tuple[tuple[str, str, str], ...] = (
    # 보기: 상태·채널·세션 기록·파일 목록·플랫폼과 확장의 상태·API 문서
    ("GET", "/status", KEY_VIEW),
    ("GET", "/api/mode", KEY_VIEW),
    ("GET", "/api/channels", KEY_VIEW),
    ("GET", "/api/view/channels", KEY_VIEW),
    ("GET", "/api/sessions", KEY_VIEW),
    ("GET", "/api/view/sessions", KEY_VIEW),
    ("GET", "/api/platforms", KEY_VIEW),
    ("GET", "/api/extensions", KEY_VIEW),
    ("GET", "/api/files/roots", KEY_VIEW),
    ("GET", "/api/files/list", KEY_VIEW),
    ("GET", "/api/files/download", KEY_VIEW),  # 녹화 파일을 읽는 것
    ("GET", "/api/openapi.json", KEY_VIEW),
    ("GET", "/api/guide", KEY_VIEW),
    # 운영: 채널 추가·수정·삭제, 녹화 시작·중지(지금 확인 포함)
    ("POST", "/api/channels/resolve", KEY_OPERATE),
    ("POST", "/api/channels", KEY_OPERATE),
    ("PATCH", "/api/channels/{channel_id}", KEY_OPERATE),
    ("DELETE", "/api/channels/{channel_id}", KEY_OPERATE),
    ("POST", "/api/channels/{channel_id}/start", KEY_OPERATE),
    ("POST", "/api/channels/{channel_id}/stop", KEY_OPERATE),
    # 키로는 불가(사람이 화면에서만): 로그인·화면 문서, 설정, 쿠키, 플랫폼·확장 켜고 끄기, 확인 간격, 파일 바꾸기,
    # 계정·비밀번호, 접근 키 관리, 로그 보기·내려받기, 도구 갱신, 텔레그램
    ("GET", "/", KEY_NONE),
    ("GET", "/login", KEY_NONE),
    ("GET", "/setup", KEY_NONE),
    ("POST", "/api/setup", KEY_NONE),
    ("POST", "/api/login", KEY_NONE),
    ("POST", "/api/logout", KEY_NONE),
    ("GET", "/api/session", KEY_NONE),
    ("POST", "/api/account/password", KEY_NONE),
    ("GET", "/api/settings", KEY_NONE),
    ("PATCH", "/api/settings", KEY_NONE),
    ("GET", "/api/settings/filename-example", KEY_NONE),
    ("GET", "/api/settings/filename-tokens", KEY_NONE),
    ("PUT", "/api/notify/secret", KEY_NONE),
    ("POST", "/api/notify/test", KEY_NONE),
    ("PUT", "/api/extensions", KEY_NONE),
    ("GET", "/api/extensions/{name}/settings", KEY_NONE),
    ("PATCH", "/api/extensions/{name}/settings", KEY_NONE),
    ("GET", "/api/tools", KEY_NONE),
    ("POST", "/api/tools/{name}/{action}", KEY_NONE),
    ("GET", "/api/cookies", KEY_NONE),
    ("PUT", "/api/cookies", KEY_NONE),
    ("POST", "/api/cookies/upload", KEY_NONE),
    ("DELETE", "/api/cookies", KEY_NONE),
    ("POST", "/api/cookies/check", KEY_NONE),
    ("GET", "/api/logs", KEY_NONE),
    ("POST", "/api/logs/export", KEY_NONE),
    ("PUT", "/api/platforms/{platform_id}/gate", KEY_NONE),
    ("POST", "/api/platforms/{platform_id}/enable", KEY_NONE),
    ("POST", "/api/platforms/{platform_id}/disable", KEY_NONE),
    ("POST", "/api/files/mkdir", KEY_NONE),
    ("POST", "/api/files/rename", KEY_NONE),
    ("POST", "/api/files/move", KEY_NONE),
    ("POST", "/api/files/delete", KEY_NONE),
    ("GET", "/api/keys", KEY_NONE),
    ("POST", "/api/keys", KEY_NONE),
    ("DELETE", "/api/keys/{key_id}", KEY_NONE),
    ("GET", "/static/*", KEY_NONE),
    ("HEAD", "/static/*", KEY_NONE),
)

_PATTERNS: dict[str, re.Pattern] = {}


def rule_matches(rule_path: str, path: str) -> bool:
    if rule_path.endswith("*"):
        return path.startswith(rule_path[:-1])
    if "{" in rule_path:
        pattern = _PATTERNS.get(rule_path)
        if pattern is None:
            parts = re.split(r"(\{[a-z_]+\})", rule_path)
            pattern = re.compile("".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts) + r"\Z")
            _PATTERNS[rule_path] = pattern
        return bool(pattern.match(path))
    return path == rule_path


def access_level(method: str, path: str, rules: tuple[tuple[str, str, str], ...] = ACCESS_RULES) -> str:
    for rule_method, rule_path, level in rules:
        if rule_method == method and rule_matches(rule_path, path):
            return level
    return LOGIN


def key_permission(method: str, path: str, rules: tuple[tuple[str, str, str], ...] = KEY_RULES) -> str | None:
    """키 권한. 표에 없으면 None(키로는 열리지 않는다)."""
    for rule_method, rule_path, perm in rules:
        if rule_method == method and rule_matches(rule_path, path):
            return perm
    return None


class AccessTable:
    """중심부 표 + 켜진 확장의 규칙. 서버 한 번 동안 바뀌지 않는다."""

    def __init__(self, extra: tuple[tuple[str, str, str], ...] = (), extra_keys: tuple[tuple[str, str, str], ...] = ()):
        self.rules = ACCESS_RULES + tuple(extra)
        self.key_rules = KEY_RULES + tuple(extra_keys)

    def level(self, method: str, path: str) -> str:
        return access_level(method, path, self.rules)

    def key(self, method: str, path: str) -> str:
        """KEY_VIEW | KEY_OPERATE | KEY_NONE(명시하지 않은 경로도 KEY_NONE)."""
        return key_permission(method, path, self.key_rules) or KEY_NONE

    def key_explicit(self, method: str, path: str) -> bool:
        return key_permission(method, path, self.key_rules) is not None
