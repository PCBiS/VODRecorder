"""내보내는 로그의 가리기 .

두 수준이다. 무엇이 들어가는지는 docs/LOG-EXPORT.md에 적는다.
- 기본(`strict=False`): 비밀과 접속 주소만 가린다. 쿠키 값(이름=값 꼴), 텔레그램 봇 토큰 꼴, 첫 설정 코드,
  세션·CSRF 토큰, `Authorization`·`Cookie` 머리글 값, 주소 질의의 서명·토큰 값, 영문·숫자가 섞인 40자 이상 덩어리는
  `[가림]`, 접속한 쪽의 주소(IPv4·IPv6, `client` 필드)는 `[주소]`, 관리자 사용자 이름은 `관리자`로 바꾼다.
  알고 있는 비밀 값(저장한 쿠키, 봇 토큰, 설정 코드, 열린 세션 토큰)은 모양과 상관없이 그대로 찾아 바꾼다.
  문장(`msg`)·오류 내용(`exc`)·채널 ID와 이름·제목·파일 이름과 경로·영상 번호·방송 ID·플랫폼 주소는 그대로 둔다.
- 더 가리기(`strict=True`, 선택): 위에 더해 채널 ID·이름은 `채널-N`, 알고 있는 제목은 `[제목]`, 파일 이름·경로는
  `[파일].확장자`, 주소(URL·호스트 이름)는 `[주소]`, 영상 번호는 `영상-N`, 방송 ID는 `방송-N`으로 바꾼다.
  로그 줄은 `msg`를 빼고 문장 틀(`tmpl`)과 아래 `FIELDS`에 적힌 필드만 남긴다. `exc`는 예외 형식 이름만 남긴다.
  대신 이름은 묶음 하나 안에서 일관된다(같은 채널은 모든 파일에서 같은 `채널-N`).

로그에 새 필드를 더하면 `FIELDS`와 `docs/LOG-EXPORT.md`를 함께 고친다(시험이 확인한다).
플랫폼마다 다른 이름(쿠키 이름 꼴, 서명 질의 이름, 방송 ID 키)은 플랫폼 모듈의 `log_mask`에서 받는다.
"""

from __future__ import annotations

import ast
import functools
import ipaddress
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .platforms import registry

SECRET = "[가림]"
ADDRESS = "[주소]"
FILE = "[파일]"
TITLE = "[제목]"
ADMIN = "관리자"

# 로그 줄의 필드와 "더 가리기"에서의 처리(기본 수준에서는 모든 필드가 남고 늘 가리는 것만 바뀐다. `client`는 늘 [주소]). code: 프로그램이 정한 값(늘 하는 가리기만), keep: 값(공유용 바꾸기도 건다), channel: 채널-N, live: 방송-N, video: 영상-N, address: [주소],
# template: 문장 틀(늘 같은 글), drop: 공유용에서 뺀다, exc: 예외 형식 이름만 남긴다
FIELDS: dict[str, str] = {
    # 모든 줄(logs.JsonFormatter)
    "ts": "code", "level": "code", "logger": "code", "msg": "drop", "tmpl": "template", "exc": "exc",
    # extra로 더하는 필드
    "event": "code", "channel_id": "channel", "session_id": "keep", "client": "address", "fails": "keep",
    "error_flag": "keep", "source": "code", "pid": "keep", "rewind": "keep", "kind": "code", "exit_code": "keep",
    "bytes": "keep", "end_reason": "code", "live_id": "live", "result": "code", "reason": "code",
    "notify_event": "code", "status": "code",
    # 플랫폼 식별자(예: chzzk)는 프로그램이 정한 값이라 더 가리기에서도 남긴다. 저장소 옮기기
    "platform": "code", "from_version": "keep", "db": "code",
    # 사건 알림과 확장
    "event_name": "code", "extension": "code", "keys": "keep",
    # 요청 창구
    "channels": "keep", "interval": "keep", "cycle": "keep",
    # 접근 키(이름은 관리자가 붙인 글, 앞 8자는 값의 일부지만 키를 쓸 수 없는 길이), 키 요청의 방법·경로의 꼴
    "key_name": "keep", "key_prefix": "code", "scope": "code", "method": "code", "route": "code",
}



def _platform_mask(key: str) -> list[str]:
    """들어 있는 플랫폼들이 밝힌 가리기 이름(정규식 조각). 순서를 지키고 겹친 것은 뺀다."""
    out: list[str] = []
    for platform in registry.all_platforms():
        out += [x for x in getattr(platform, "log_mask", {}).get(key, ()) if x not in out]
    return out


_COOKIE_NAMES = "|".join(_platform_mask("cookie_patterns") + [r"lr_session", r"[A-Za-z0-9_]*(?:SESS|SES|AUT|AUTH|TOKEN|SID|CSRF)[A-Za-z0-9_]*"])
_QUERY_NAMES = "|".join(_platform_mask("query_params") + ["token", "access_token", "auth", "sig", "signature", "policy",
                                                          "key-pair-id", "key", "in_?key", "hmac", "acl", "exp", "expires",
                                                          "session", "sid", "csrf"])
_LIVE_KEYS = "|".join(_platform_mask("live_id_keys") + ["live_id"])
# 주소 경로의 `/이름/값/` 꼴 서명·만료 값. 플랫폼이 밝힌 호스트(path_hosts)의 주소 안에서만 가린다
_PATH_PARAMS = "|".join(_platform_mask("path_params"))
_PATH_HOSTS = tuple(_platform_mask("path_hosts"))
# 주소 경로 전체를 가릴 호스트(호스트와 꼴만 남긴다), 글 속 `이름="값"` 꼴로 가릴 속성(예: 재생목록의 USER-IP)
_PATH_HOSTS_FULL = tuple(_platform_mask("path_hosts_full"))
_ATTR_PARAMS = "|".join(_platform_mask("attr_params"))
_ATTR = re.compile(r"(?i)\b((?:" + _ATTR_PARAMS + r")\s*=\s*\"?)([^\",\s]+)") if _ATTR_PARAMS else None
# 더 가리기에서 방송 ID로 바꿀 값의 꼴(숫자 3자리 이상에 더한다)
_LIVE_VALUES = "|".join([r"\d{3,}"] + _platform_mask("live_id_patterns"))


def _extension_mask(key: str) -> list[str]:
    """들어 있는 확장(켜고 끈 것과 상관없이)이 밝힌 가리기 조각. 꺼진 뒤에도 남은 로그 줄을 가린다."""
    from .core import extensions

    out: list[str] = []
    for ext in extensions.known():
        out += [x for x in getattr(ext, "log_mask", {}).get(key, ()) if x not in out]
    return out


# 영상 번호: 주소의 경로 조각(확장이 밝힌 것)과 정해진 키
_VIDEO_PATHS = "|".join(_extension_mask("video_paths")) or r"(?!)"
_VIDEO_KEYS = "|".join(_extension_mask("video_keys") + [r"video_no", r"영상\s*번호"])
_RULES_ALWAYS: list[tuple[str, re.Pattern, str]] = [
    # 머리글: 이름은 남기고 값은 줄 끝(또는 따옴표)까지 가린다
    ("header", re.compile(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-csrf-token)(\"?'?\s*[:=]\s*\"?'?)([^\"'\r\n]+)"),
     r"\1\2" + SECRET),
    # 쿠키·세션·토큰 이름=값(또는 "이름": "값")
    ("cookie", re.compile(r"(?i)\b(" + _COOKIE_NAMES + r")(\"?'?\s*[=:]\s*\"?'?)([^\s;,&\"'<>]{8,})"), r"\1\2" + SECRET),
    # "csrf 값", "token 값"처럼 빈칸으로 띄운 긴 값
    ("cookie", re.compile(r"(?i)\b(csrf|token|session|lr_session)(\s+)([A-Za-z0-9_\-]{16,})"), r"\1\2" + SECRET),
    # 접근 키 값(`vr_` 뒤 8자 이상). 기록에 남기는 앞 8자(`vr_` + 5자)는 가리지 않는다
    ("api_key", re.compile(r"\bvr_[A-Za-z0-9_-]{8,}"), "vr_" + SECRET),
    # 텔레그램 봇 토큰 꼴(숫자:35자 안팎). `/bot<토큰>/`처럼 글자 바로 뒤에 붙어 있어도 가린다
    ("telegram", re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"), SECRET),
    # 주소 경로에 든 서명 조각(`/hdntl=exp=…~hmac=…/`, `__gda__=…`)
    # (이름은 플랫폼 모듈의 `log_mask["path_signatures"]`)
    # 값은 `~`로 이어진 서명 묶음 전체다(`exp=…~acl=/live/*~hmac=…`). acl 조각은 `/`를 품을 수 있다
    *([("query", re.compile(r"(?i)((?:^|[/;?&\s\"'])(?:" + "|".join(_platform_mask("path_signatures")) + r")=)"
                            r"([^/?&#\s\"'<>~]*(?:~(?:acl=[^~?&#\s\"'<>]*|[^/~?&#\s\"'<>]*))*)"),
        r"\1" + SECRET)] if _platform_mask("path_signatures") else []),
    # 쿠키 파일(Netscape 꼴) 줄의 값: 도메인·플래그·경로·Secure·만료·이름 다음의 일곱째 칸
    ("cookie", re.compile(r"(?m)^([^\t\n]*\t(?:TRUE|FALSE)\t[^\t\n]*\t(?:TRUE|FALSE)\t\d+\t[^\t\n]+\t)([^\t\n]+)"), r"\1" + SECRET),
    # 첫 설정 코드 꼴(base32 4자씩 8묶음)과 "설정 코드: ..." 줄
    ("setup_code", re.compile(r"\b[A-Z2-7]{4}(?:-[A-Z2-7]{4}){5,}\b"), SECRET),
    ("setup_code", re.compile(r"(설정 코드\s*[:：]\s*)(\S+)"), r"\1" + SECRET),
    # 주소 질의의 서명·토큰 값
    ("query", re.compile(r"(?i)([?&;](?:" + _QUERY_NAMES + r")=)([^&\s\"'<>#]+)"), r"\1" + SECRET),
    # 그 밖의 긴 토큰 꼴(영문·숫자가 섞인 40자 이상)
    ("token", re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])"), SECRET),
]

_EXT = r"mp4|m4a|m4v|ts|mkv|webm|flv|aac|mp3|part|raw|remux|json|jsonl|log|txt|m3u8|mpd|jpe?g|png|gif|webp|zip|sqlite3?|db|py|lock|new|tmp"
_URL = re.compile(r"(?i)\b(?:https?|wss?|ftp)://[^\s\"'<>]+")
_PATH = re.compile(r"(?:(?<=[\s\"'(=:,])|^)(?!(?:" + _VIDEO_PATHS + r"))(?:~|\.{1,2})?(?:/[^/\s\"'<>]+)+/?")
_FILE = re.compile(r"(?i)(?<![\w.\[\]-])[^\s/\\\"'<>|:*?]{1,200}?\.(" + _EXT + r")\b")
_IPV4 = re.compile(r"\b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?::\d{1,5})?\b")
_IPV6 = re.compile(r"(?i)(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![0-9a-f:])")
_HOST = re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,24})\b")
_CHANNEL_ID = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
_VIDEO = re.compile(r"(?i)((?:" + _VIDEO_PATHS + r"|(?:" + _VIDEO_KEYS + r")[\"']?\s*[:=]?\s*[\"']?))(\d{3,})")
_LIVE = re.compile(r"(?i)((?:(?<![A-Za-z0-9_])" + _LIVE_KEYS.replace("|", "|(?<![A-Za-z0-9_])") + r")[\"']?\s*[:=]\s*[\"']?)("
                   + _LIVE_VALUES + r")(?![A-Za-z0-9_-])")
_SIGNED_URL = re.compile(r"(?i)\bhttps?://([^/\s\"'<>?#]+)(/[^\s\"'<>]*)")
_PATH_VALUE = re.compile(r"(/(?:" + (_PATH_PARAMS or r"(?!)") + r")/)([^/?#\s\"'<>]+)")


@functools.lru_cache(maxsize=1)
def constant_messages() -> frozenset[str]:
    """패키지 안의 로그 호출 가운데 값을 채우지 않는 고정 문장. 공유용에서 `tmpl`이 없는 줄도 이 문장은 남긴다."""
    out: set[str] = set()
    root = Path(__file__).resolve().parent
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("debug", "info", "warning", "error", "exception", "critical")
                    and len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                out.add(node.args[0].value)
    return frozenset(out)


class Masker:
    """묶음 하나를 만드는 동안 쓴다. 같은 값에는 늘 같은 대신 이름을 준다. 가린 건수를 센다."""

    def __init__(self, strict: bool, *, channels: Iterable[tuple[str, str]] = (), admin: str | None = None,
                 secrets: Iterable[str] = (), titles: Iterable[str] = (), extra_fields: dict[str, str] | None = None):
        self.strict = strict
        self.fields = {**FIELDS, **(extra_fields or {})}  # 켜진 확장이 밝힌 필드 규칙을 더한다(중심부 규칙이 먼저)
        self.fields.update(FIELDS)
        self.counts: Counter = Counter()
        self.channel_ids: dict[str, str] = {}
        self.channel_names: dict[str, str] = {}
        self.videos: dict[str, str] = {}
        self.lives: dict[str, str] = {}
        for cid, name in channels:
            label = self.channel(cid)
            if name and len(name.strip()) >= 2:
                self.channel_names.setdefault(name.strip(), label)
        self.admin = admin.strip() if admin and admin.strip() else None
        # 알고 있는 방송·영상 제목(세션 기록, 다시보기 작업). 글 속에서 찾아 [제목]으로 바꾼다. 짧은 제목은 다른 글과 겹쳐 4자 이상만
        known_titles = {t.strip() for t in titles if isinstance(t, str) and len(t.strip()) >= 4}
        self._title_re = re.compile("|".join(re.escape(t) for t in sorted(known_titles, key=len, reverse=True))) if known_titles else None
        # 알고 있는 비밀 값. 짧은 값은 다른 글자와 겹칠 수 있어 6자 이상만 그대로 찾는다
        values = {s.strip() for s in secrets if isinstance(s, str) and len(s.strip()) >= 6}
        for s in list(values):
            if "-" in s and re.fullmatch(r"[A-Z2-7-]+", s):
                values.add(s.replace("-", ""))  # 설정 코드는 대시 없이 적을 수도 있다
        self.secret_values = sorted(values, key=len, reverse=True)
        names = sorted(self.channel_names, key=len, reverse=True)
        self._name_re = re.compile("|".join(re.escape(n) for n in names)) if names else None
        self._admin_re = re.compile(r"(?<![\w])" + re.escape(self.admin) + r"(?![\w])") if self.admin else None

    # ---- 대신 이름 ----
    def channel(self, cid: str) -> str:
        cid = cid.lower()
        if cid not in self.channel_ids:
            self.channel_ids[cid] = f"채널-{len(self.channel_ids) + 1}"
        return self.channel_ids[cid]

    def video(self, no: str) -> str:
        if no not in self.videos:
            self.videos[no] = f"영상-{len(self.videos) + 1}"
        return self.videos[no]

    def live(self, no: str) -> str:
        if no not in self.lives:
            self.lives[no] = f"방송-{len(self.lives) + 1}"
        return self.lives[no]

    def _sub(self, kind: str, pattern: re.Pattern, repl, text: str) -> str:
        new, n = pattern.subn(repl, text)
        if n:
            self.counts[kind] += n
        return new

    # ---- 글 ----
    def secrets_only(self, text: str) -> str:
        """늘 하는 가리기."""
        for value in self.secret_values:
            if value in text:
                self.counts["known_secret"] += text.count(value)
                text = text.replace(value, SECRET)
        for kind, pattern, repl in _RULES_ALWAYS:
            text = self._sub(kind, pattern, repl, text)
        if _PATH_HOSTS and _PATH_PARAMS or _PATH_HOSTS_FULL:
            text = _SIGNED_URL.sub(self._signed_path, text)
        if _ATTR is not None:
            text = self._sub("query", _ATTR, r"\1" + SECRET, text)
        return text

    def _signed_path(self, match: re.Match) -> str:
        """서명 주소(플랫폼이 밝힌 호스트)는 호스트와 꼴만 남기고 경로의 값을 가린다."""
        host = match.group(1).split(":")[0].lower()
        if any(host == h or host.endswith("." + h) for h in _PATH_HOSTS_FULL):
            # 경로의 값은 모두 가리고 꼴(나눔과 끝의 확장자)만 남긴다
            path, _, query = match.group(2).partition("?")
            shaped = "/".join((SECRET + (m.group(0) if (m := re.search(r"\.(?:m3u8|mp4|m4s|ts)$", seg)) else ""))
                              if seg else seg for seg in path.split("/"))
            self.counts["query"] += 1
            return match.group(0)[: match.start(2) - match.start(0)] + shaped + ("?" + query if query else "")
        if not _PATH_PARAMS or not any(host == h or host.endswith("." + h) for h in _PATH_HOSTS):
            return match.group(0)
        path, n = _PATH_VALUE.subn(r"\1" + SECRET, match.group(2))
        if n:
            self.counts["query"] += n
        return match.group(0)[: match.start(2) - match.start(0)] + path

    def always(self, text: str) -> str:
        """늘 가리는 것: 비밀, 접속 주소(IPv4·IPv6), 관리자 사용자 이름."""
        text = self.secrets_only(text)
        if self._admin_re:
            text = self._sub("admin", self._admin_re, ADMIN, text)
        text = _IPV6.sub(self._ip, text)
        return self._sub("address", _IPV4, ADDRESS, text)

    def _ip(self, match: re.Match) -> str:
        raw = match.group(0)
        try:
            ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            return raw  # 시각(12:34:56) 같은 것은 그대로 둔다
        self.counts["address"] += 1
        return ADDRESS

    def _file(self, match: re.Match) -> str:
        self.counts["file"] += 1
        return f"{FILE}.{match.group(1).lower()}"

    def _path(self, match: re.Match) -> str:
        raw = match.group(0).rstrip("/")
        ext = re.search(r"\.(" + _EXT + r")$", raw, re.I)
        self.counts["file"] += 1
        return f"{FILE}.{ext.group(1).lower()}" if ext else FILE

    def text(self, text: str) -> str:
        """늘 가리는 것 뒤, 더 가리기면 식별 정보를 바꾼다."""
        text = self.always(text)
        if not self.strict:
            return text
        if self._title_re:
            text = self._sub("title", self._title_re, TITLE, text)
        if self._name_re:
            text = self._sub("channel", self._name_re, lambda m: self.channel_names[m.group(0)], text)
        text = self._sub("channel", _CHANNEL_ID, lambda m: self.channel(m.group(0)), text)
        text = self._sub("video", _VIDEO, lambda m: m.group(1) + self.video(m.group(2)), text)
        text = self._sub("live", _LIVE, lambda m: m.group(1) + self.live(m.group(2)), text)
        text = self._sub("address", _URL, ADDRESS, text)
        text = _PATH.sub(self._path, text)
        text = _FILE.sub(self._file, text)
        text = self._sub("address", _HOST, lambda m: m.group(0) if m.group(0).startswith(FILE) else ADDRESS, text)
        return text

    def value(self, value: Any) -> Any:
        """JSON 값 전체에 `text`를 건다(키는 그대로)."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        return value

    # ---- 로그 줄 ----
    def record(self, rec: dict[str, Any]) -> dict[str, Any]:
        """구조화 로그 한 줄. 기본은 모든 필드를 남기고, 더 가리기면 문장을 빼고 정해진 필드만 남긴다."""
        if not self.strict:
            out = {}
            for key, value in rec.items():
                if self.fields.get(key) == "address" and value:
                    out[key] = ADDRESS  # 접속한 쪽의 주소는 늘 가린다
                    self.counts["address"] += 1
                else:
                    out[key] = self.value(value)
            return out
        out: dict[str, Any] = {}
        tmpl = rec.get("tmpl") if isinstance(rec.get("tmpl"), str) else None
        msg = rec.get("msg") if isinstance(rec.get("msg"), str) else None
        if tmpl is None and msg is not None and msg in constant_messages():
            tmpl = msg  # 값이 채워지지 않은 고정 문장
        if msg is not None:
            self.counts["message"] += 1
        for key, value in rec.items():
            rule = self.fields.get(key)
            if rule is None:
                self.counts["unknown_field"] += 1
                continue
            if rule in ("drop", "template"):
                continue
            if rule == "exc":
                last = value.strip().splitlines()[-1] if isinstance(value, str) and value.strip() else ""
                out["exc_type"] = re.sub(r"[^\w.]", "", last.split(":", 1)[0])[:80] or None
                self.counts["exception"] += 1
            elif rule == "channel":
                out[key] = self.channel(value) if isinstance(value, str) and value else value
                self.counts["channel"] += 1
            elif rule == "live":
                out[key] = self.live(str(value)) if value not in (None, "") else value
                self.counts["live"] += 1
            elif rule == "video":
                out[key] = self.video(str(value)) if value not in (None, "") else value
                self.counts["video"] += 1
            elif rule == "code":
                out[key] = self.secrets_only(value) if isinstance(value, str) else value
            elif rule == "address":
                out[key] = ADDRESS if value else value
                self.counts["address"] += 1
            else:
                out[key] = self.value(value)
        if tmpl is not None:
            # 문장 틀은 코드에 적힌 글이라 값이 없다. 그래도 비밀 가리기는 한 번 더 건다
            out["tmpl"] = self.secrets_only(tmpl)
        return out

    def fixed_line(self, row: dict[str, Any], fields: tuple[str, ...], text: str = "message") -> dict[str, Any]:
        """확장이 내놓은 정해진 꼴의 줄(예: 작업 로그: 정해진 문장과 숫자·크기, 정해진 오류 문장). `fields`만 남긴다.
        문장(`text`)에는 늘 하는 가리기와 더 가리기의 바꾸기를, 나머지(프로그램이 정한 값)에는 비밀 가리기만 건다."""
        return {k: (self.value(row.get(k)) if k == text else
                    self.secrets_only(row[k]) if isinstance(row.get(k), str) else row.get(k)) for k in fields}

    def summary(self) -> dict[str, int]:
        return dict(sorted(self.counts.items()))
