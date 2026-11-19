"""접근 키로 온 요청의 처리. 관문(app.guard)이 `Authorization: Bearer` 머리글을 보면 여기로 넘긴다.

- 세션 쿠키를 보지 않고 CSRF 검사를 하지 않는다(브라우저 세션이 아니다). 응답에 쿠키를 심지 않는다.
- 키가 없거나 틀리거나 만료·폐기됐으면 401(까닭을 구분하지 않는다). 틀린 키는 로그인 실패와 같은 규칙으로 주소별로 늦추되
  셈은 따로 한다(틀린 키가 많아도 화면 로그인은 막히지 않고, 그 반대도 같다).
- 권한: 경로의 키 권한(접근 규칙 표)이 없으면 403, 운영 경로를 보기 키로 부르면 403.
- 요청 제한: 키마다 1분에 읽기 60번, 바꾸는 요청 20번. 넘으면 429와 Retry-After.
- 기록: 바꾸는 요청마다 로그 사건 `api_key_request`(키 이름·앞 8자·방법·경로의 꼴·결과 코드. 본문과 키 값은 남기지 않는다).
  읽기는 키의 마지막 사용 시각과 횟수만 고친다.
- 오류 응답의 꼴: `{"detail": 문장, "code": 짧은 이름}`(화면용 응답의 `{"error": …}` 꼴은 키 요청에서만 바꿔 낸다).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from .access import KEY_NONE, KEY_OPERATE
from .auth import client_address, limit_key

if TYPE_CHECKING:
    from .context import WebContext

log = logging.getLogger("live_recorder.web")

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
READ_LIMIT, WRITE_LIMIT, WINDOW = 60, 20, 60.0
CODES = {400: "invalid_input", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
         409: "conflict", 413: "too_large", 422: "invalid_input", 429: "rate_limited"}
UNAUTHORIZED = "접근 키가 없거나 맞지 않습니다"
NOT_FOR_KEYS = "이 경로는 접근 키로 열 수 없습니다. 화면에서 해 주십시오"
NEED_OPERATE = "이 키의 권한(보기)으로는 할 수 없습니다. 운영 권한의 키가 필요합니다"
LIMITED = "요청이 너무 많습니다. 잠시 뒤 다시 시도해 주십시오"
NO_ROUTE = "(없는 경로)"
TOO_MANY_FAILS = "접근 키 실패가 많습니다. 잠시 뒤 다시 시도해 주십시오"


class RateLimiter:
    """키마다 지난 1분의 요청 시각."""

    def __init__(self, read: int = READ_LIMIT, write: int = WRITE_LIMIT, window: float = WINDOW, clock=time.monotonic):
        self.limits = {"read": read, "write": write}
        self.window, self.clock = window, clock
        self._seen: dict[tuple[str, str], deque[float]] = {}

    def take(self, key_id: str, kind: str) -> float | None:
        """자리가 있으면 None(그리고 센다). 없으면 기다릴 초."""
        now = self.clock()
        q = self._seen.setdefault((key_id, kind), deque())
        while q and q[0] <= now - self.window:
            q.popleft()
        if len(q) >= self.limits[kind]:
            return max(1.0, q[0] + self.window - now)
        q.append(now)
        return None

    def forget(self, key_id: str) -> None:
        for kind in self.limits:
            self._seen.pop((key_id, kind), None)


def error(status: int, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": CODES.get(status, "error"), **extra}, status_code=status)


def bearer_value(request: Request) -> str | None:
    """`Authorization: Bearer` 머리글의 값. 머리글이 없거나 다른 방식(Basic 등)이면 None(세션 경로로 본다)."""
    raw = request.headers.get("authorization")
    if raw is None:
        return None
    scheme, _, value = raw.strip().partition(" ")
    return value.strip() if scheme.lower() == "bearer" else None


async def normalize(response: Response, status_override: int | None = None) -> Response:
    """키 요청의 오류 응답을 한 꼴로. 쿠키는 심지 않는다."""
    if "set-cookie" in response.headers:
        del response.headers["set-cookie"]
    if response.status_code < 400:
        return response
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else chunk.encode()
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = {}
    status = response.status_code
    extra: dict[str, Any] = {}
    if isinstance(data, dict):
        detail = data.get("detail", data.get("error"))
        extra = {k: v for k, v in data.items() if k not in ("detail", "error", "code")}
    else:
        detail = None
    if isinstance(detail, list):  # FastAPI의 입력 검사 오류
        extra["errors"] = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in detail if isinstance(e, dict)]
        detail = "입력이 맞지 않습니다"
    if status == 400:
        status = 422  # 입력 오류는 422
    if not isinstance(detail, str) or not detail:
        detail = {401: UNAUTHORIZED, 403: "권한이 없습니다", 404: "찾을 수 없습니다", 405: "허용하지 않는 방법입니다"}.get(
            status, "요청을 처리하지 못했습니다")
    out = error(status, detail, **extra)
    for name in ("retry-after", "content-security-policy"):
        if name in response.headers:
            out.headers[name] = response.headers[name]
    return out


def flat_routes(app) -> list[tuple[str, str]]:
    """앱의 모든 API 경로(방법, 경로 꼴). 묶음 안의 묶음까지 편다. 서버 한 번 동안 바뀌지 않으므로 한 번만 센다."""
    cached = app.state.__dict__.get("_flat_routes")
    if cached is None:
        from fastapi.routing import APIRoute, iter_route_contexts

        cached = []
        for c in iter_route_contexts(app.routes):
            if isinstance(c.original_route, APIRoute):
                cached += [(m, c.path) for m in sorted(c.methods or ()) if m != "HEAD"]
        app.state._flat_routes = cached
    return cached


def route_shape(app, scope) -> str:
    """경로의 꼴(`/api/channels/{channel_id}`). 값(채널 ID 등)은 기록에 남기지 않는다. 화면 자산은 `/static/`."""
    from .access import rule_matches

    path = scope.get("path", "")
    for _, shape in flat_routes(app):
        if rule_matches(shape, path):
            return shape
    if path.startswith("/static/"):
        return "/static/*"
    return NO_ROUTE


async def handle(ctx: "WebContext", request: Request, call_next, value: str, access) -> Response:
    """관문이 부른다. 키를 확인하고 권한·요청 제한을 본 뒤 경로로 넘긴다."""
    settings = await ctx.current_settings()
    addr = client_address(request, settings.trusted_proxies)
    limited = settings.login_limit_enabled
    limiter = ctx.key_limiter  # 로그인 실패와 같은 규칙, 셈은 따로
    if limited and not ctx.key_limiter_state["enabled"]:
        limiter.clear()
    ctx.key_limiter_state["enabled"] = limited
    if limited and limiter.blocked(limit_key(addr)):
        log.warning("접근 키 잠금", extra={"event": "api_key_locked", "client": addr})
        return error(429, TOO_MANY_FAILS)
    # 키 검증은 해시 비교라 빠르므로 이벤트 루프 안에서 바로 한다(검증과 실패 기록 사이에 다른 요청이 끼지 않는다).
    # 그래서 자리를 미리 잡지 않고 틀렸을 때만 센다(맞는 키의 동시 요청을 막지 않는다)
    key = ctx.keys.verify(value) if value else None
    if key is None and limited:
        limiter.fail(limit_key(addr))
    elif key is not None and await ctx.account_generation() is None:
        key = None  # 관리자 계정이 없으면(지웠으면) 키도 받지 않는다. 실패로 세지 않는다
    if key is None:
        log.warning("접근 키 실패", extra={"event": "api_key_failed", "client": addr})  # 값은 남기지 않는다
        response = error(401, UNAUTHORIZED)
        response.headers["WWW-Authenticate"] = "Bearer"
        return response
    method, path = request.method, request.url.path
    perm = access.key(method, path)
    unsafe = method not in SAFE_METHODS
    shape = route_shape(ctx.app, request.scope)
    if perm == KEY_NONE and shape == NO_ROUTE:
        response: Response = await normalize(await call_next(request))  # 없는 경로(꺼진 확장의 경로 포함)는 404 그대로
    elif perm == KEY_NONE:
        response = error(403, NOT_FOR_KEYS)
    elif perm == KEY_OPERATE and key["scope"] != "operate":
        response = error(403, NEED_OPERATE)
    else:
        wait = ctx.key_rates.take(key["id"], "write" if unsafe else "read")
        if wait is not None:
            response = error(429, LIMITED)
            response.headers["Retry-After"] = str(int(math.ceil(wait)))
        else:
            request.state.api_key = key
            response = await normalize(await call_next(request))
    await asyncio.to_thread(ctx.keys.touch, key["id"])
    if unsafe:
        log.info("접근 키 요청 %s %s → %s", method, shape, response.status_code,
                 extra={"event": "api_key_request", "key_name": key["name"], "key_prefix": key["prefix"],
                        "method": method, "route": shape, "status": response.status_code})
    return response
