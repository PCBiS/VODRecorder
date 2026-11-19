"""쿠키 파일 해석. 브라우저 확장이 내보낸 Netscape `cookies.txt`와 JSON 배열을 읽는다.

이 파일은 쿠키 이름을 모른다. 어떤 쿠키를 고를지는 플랫폼 모듈의 `pick_credentials`가 정한다(중심부에 쿠키 이름 없음).
파일 내용과 값은 오류 문장·로그에 넣지 않는다.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class Cookie:
    domain: str
    name: str
    value: str
    expires: float | None  # epoch 초. None이면 브라우저를 닫으면 끝나는 쿠키
    path: str = "/"
    secure: bool = True
    http_only: bool = False

    def host(self) -> str:
        return self.domain.lstrip(".").lower()

    def expired(self, now: float | None = None) -> bool:
        return self.expires is not None and self.expires > 0 and self.expires <= (now or time.time())


def _netscape(text: str) -> list[Cookie]:
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r")
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) != 7 or not cols[0] or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", cols[4] or "0"):
            raise ValueError(f"Netscape 쿠키 파일의 {n}번째 줄 꼴이 맞지 않습니다")
        exp = float(cols[4] or 0)
        out.append(Cookie(cols[0], cols[5], cols[6], exp if exp > 0 else None, cols[2] or "/", cols[3] == "TRUE", http_only))
    return out


def _json_array(data: list) -> list[Cookie]:
    out = []
    for n, item in enumerate(data, 1):
        if not isinstance(item, dict) or not isinstance(item.get("domain"), str) or not isinstance(item.get("name"), str) \
                or not isinstance(item.get("value"), str):
            raise ValueError(f"JSON 쿠키 파일의 {n}번째 항목에 domain·name·value가 없습니다")
        exp = item.get("expirationDate", item.get("expires"))
        expires = float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp > 0 else None
        if item.get("session") is True:
            expires = None
        out.append(Cookie(item["domain"], item["name"], item["value"], expires, str(item.get("path") or "/"),
                          bool(item.get("secure", True)), bool(item.get("httpOnly", False))))
    return out


def parse(text: str) -> list[Cookie]:
    """두 꼴 가운데 맞는 것으로 읽는다. 알아보지 못하면 ValueError(까닭, 내용은 넣지 않는다)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("쿠키 파일 내용이 비었습니다")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"쿠키 파일이 너무 큽니다({MAX_BYTES // 1024}KB 이하만 받습니다)")
    body = text.lstrip("﻿").strip()
    if body.startswith("["):
        try:
            data = json.loads(body)
        except ValueError:
            raise ValueError("JSON 쿠키 파일을 읽지 못했습니다") from None
        if not isinstance(data, list):
            raise ValueError("JSON 쿠키 파일은 배열이어야 합니다")
        cookies = _json_array(data)
    elif "\t" in body:
        cookies = _netscape(body)
    else:
        raise ValueError("쿠키 파일 꼴을 알아보지 못했습니다(Netscape cookies.txt 또는 JSON 배열)")
    if not cookies:
        raise ValueError("쿠키 파일에 쿠키가 없습니다")
    return cookies


def on_domain(cookie: Cookie, domains: tuple[str, ...]) -> bool:
    host = cookie.host()
    return any(host == d or host.endswith("." + d) for d in domains)


def pick(cookies: list[Cookie], domains: tuple[str, ...], names: tuple[str, ...], now: float | None = None) -> dict[str, Cookie]:
    """정한 도메인의 정한 이름 쿠키를 하나씩 고른다. 같은 이름이 여럿이면 도메인이 더 맞는 것(정한 도메인과
    같은 것, 그다음 더 짧은 하위 도메인), 그다음 만료가 늦은 것. 하나라도 없거나 만료된 것만 있으면 ValueError."""
    now = now or time.time()
    chosen: dict[str, Cookie] = {}
    expired: list[str] = []
    missing: list[str] = []
    for name in names:
        same = [c for c in cookies if c.name == name and on_domain(c, domains)]
        live = [c for c in same if not c.expired(now)]
        if not live:
            (expired if same else missing).append(name)
            continue

        def score(c: Cookie) -> tuple:
            host = c.host()
            return (host in domains, -len(host), c.expires if c.expires is not None else float("inf"))

        chosen[name] = max(live, key=score)
    if missing:
        raise ValueError(f"파일에 {', '.join(missing)} 쿠키가 없습니다. 로그인한 상태에서 내보냈는지 확인해 주십시오")
    if expired:
        raise ValueError(f"파일에 든 {', '.join(expired)} 쿠키가 이미 만료됐습니다. 다시 로그인한 뒤 내보내 주십시오")
    return chosen


def earliest_expiry(cookies) -> str | None:
    values = [c.expires for c in cookies if c.expires]
    return datetime.fromtimestamp(min(values), timezone.utc).isoformat() if values else None


def to_netscape(cookies) -> str:
    """골라낸 쿠키로 Netscape 글을 다시 만든다(유튜브처럼 파일 글 전체를 자격으로 쓰는 플랫폼)."""
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in cookies:
        dom = c.domain if c.domain.startswith(".") or not c.domain else c.domain
        flag = "TRUE" if dom.startswith(".") else "FALSE"
        lines.append(("#HttpOnly_" if c.http_only else "") + "\t".join(
            [dom, flag, c.path or "/", "TRUE" if c.secure else "FALSE", str(int(c.expires or 0)), c.name, c.value]))
    return "\n".join(lines) + "\n"
