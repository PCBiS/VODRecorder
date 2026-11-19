"""인증 (뼈대): 세션, 로그인 횟수 제한, 접속 주소, 로그인·첫 설정·비밀번호 경로와 화면 문서."""

from __future__ import annotations

from html import escape as html_escape

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import logging
import secrets
import sys
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, StringConstraints

from .. import NAME_SLOT, PROGRAM_NAME
from ..core import secretstore

if TYPE_CHECKING:
    from .context import WebContext

log = logging.getLogger("live_recorder.web")

def named_page(path):
    """화면 문서의 이름 자리를 표시 이름으로 바꿔 내려 준다."""
    from fastapi.responses import HTMLResponse

    return HTMLResponse(path.read_text(encoding="utf-8").replace(NAME_SLOT, html_escape(PROGRAM_NAME)))


SESSION_COOKIE = "lr_session"
GLOBAL_SLOWDOWN = 1.0  # 전체 실패가 많을 때 검증 한 번마다 쉬는 시간(초). 검증 잠금 밖에서 쉰다
VERIFY_QUEUE = 16  # 검증을 기다릴 수 있는 요청 수. 넘으면 곧바로 429
BUSY = "로그인 시도가 몰려 있습니다. 잠시 뒤 다시 시도하십시오"


class QueueFull(Exception):
    pass


class VerifyQueue:
    """비밀번호·설정 코드 검증의 대기열. 검증(scrypt)은 한 번에 하나, 늦추기는 잠금 밖에서, 기다리는 수에는 상한."""

    def __init__(self, cap: int = VERIFY_QUEUE):
        self.cap, self.waiting = cap, 0
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self, slow: bool):
        if self.waiting >= self.cap:
            raise QueueFull
        self.waiting += 1
        try:
            if slow:
                await asyncio.sleep(GLOBAL_SLOWDOWN)  # 잠금 밖: 늦추는 동안에도 다른 요청의 검증은 돈다
            async with self.lock:
                yield
        finally:
            self.waiting -= 1


def limit_key(addr: str) -> str:
    """시도 제한을 세는 단위. IPv6는 /64로 묶는다(한 사용자가 주소를 바꿔 가며 세기를 피하지 못하게)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)
COOKIE_MAX_AGE = 30 * 24 * 3600  # 쿠키 수명. 실제 끝은 서버의 쓰지 않은 시간이 정한다
VIEW_ONLY_GUIDE = ("관리자 계정이 아직 없어 채널 상태를 보는 것만 됩니다. 첫 설정 화면(/setup)에서 계정을 만드십시오. "
                   "설정 코드는 컨테이너 로그나 python -m live_recorder account setup-code 명령으로 볼 수 있습니다")
VIEW_ONLY_GUIDE_NO_CODE = "관리자 계정이 아직 없어 채널 상태를 보는 것만 됩니다. 첫 설정 화면(/setup)에서 계정을 만드십시오"
def view_only_guide() -> str:
    """보기 전용 모드의 안내. 설정 코드를 끈 상태면 코드 이야기를 빼고 말한다."""
    return VIEW_ONLY_GUIDE if secretstore.setup_code_enabled() else VIEW_ONLY_GUIDE_NO_CODE


SETUP_GUIDE = (f"설정 코드는 서버의 컨테이너 로그('{PROGRAM_NAME} 첫 설정 코드' 줄)나 "
               "python -m live_recorder account setup-code 명령으로 볼 수 있습니다")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 정해진 키만 받는다


# 글의 길이 상한(본문 크기 상한과 함께 큰 값을 받지 않는다). 비밀번호 규칙(최대 1024자)은 따로 400으로 알린다
Name = Annotated[StrictStr, StringConstraints(max_length=256)]
Secret = Annotated[StrictStr, StringConstraints(max_length=4096)]


class LoginBody(Strict):
    username: Name
    password: Secret


class SetupBody(Strict):
    username: Name
    password: Secret
    password_confirm: Secret
    code: Name | None = None  # 설정 코드를 켠 상태(기본)에서는 꼭 있어야 한다. 끈 상태에서는 보지 않는다


class PasswordBody(Strict):
    current: Secret
    new: Secret
    revoke_keys: StrictBool = False  # 접근 키도 모두 폐기(기본 꺼짐)


class Sessions:
    """세션은 메모리에만 둔다. 쓰지 않은 시간이 정한 분을 넘으면 끝난다(0이면 제한 없음).

    비밀번호를 바꾸면(CLI든 웹이든) 계정 파일의 세대 값이 바뀌고, 그 전에 만든 세션은 모두 끊긴다.
    """

    def __init__(self):
        self._items: dict[str, dict[str, Any]] = {}

    def create(self, username: str, generation: str | None) -> tuple[str, dict[str, Any]]:
        token = secrets.token_urlsafe(32)
        item = {"username": username, "csrf": secrets.token_urlsafe(32), "last": time.monotonic(),
                "generation": generation}
        self._items[token] = item
        return token, item

    def get(self, token: str | None, idle_minutes: int, generation: str | None) -> dict[str, Any] | None:
        item = self._items.get(token or "")
        if item is None:
            return None
        now = time.monotonic()
        expired = idle_minutes > 0 and now - item["last"] > idle_minutes * 60
        if expired or item["generation"] != generation:
            self._items.pop(token or "", None)
            return None
        item["last"] = now
        return item

    def drop(self, token: str | None) -> None:
        self._items.pop(token or "", None)

    def drop_all(self) -> None:
        self._items.clear()

    def secrets(self) -> list[str]:
        """로그 내려받기가 가릴 값(세션 토큰과 CSRF 토큰)."""
        return [v for token, item in self._items.items() for v in (token, item.get("csrf"))]

    def __len__(self) -> int:
        return len(self._items)


class LoginLimiter:
    """같은 주소 10분에 5번 틀리면 그 주소를 잠근다. 전체 10분에 `global_limit`번을 넘으면 잠그지 않고 늦춘다
    (`busy`: 검증 한 번마다 쉬게 한다. 남이 틀린 시도로 맞는 비밀번호까지 막지 못하게). 설정으로 끌 수 있다.

    경합을 막으려고 검증 전에 자리를 먼저 센다(`reserve`). 맞으면 그 자리를 되돌린다(`release`)."""

    def __init__(self, limit: int = 5, window: float = 600.0, global_limit: int | None = 30):
        self.limit, self.window, self.global_limit = limit, window, global_limit
        self._fails: dict[str, deque[float]] = {}
        self._all: deque[float] = deque(maxlen=100000)  # 전체 기록에도 크기 상한

    def _trim(self, q: deque[float]) -> None:
        cutoff = time.monotonic() - self.window
        while q and q[0] < cutoff:
            q.popleft()

    def blocked(self, key: str) -> bool:
        """그 주소가 잠겼는가(주소별만. 전체 한도는 잠그지 않는다)."""
        q = self._fails.get(key, deque())
        self._trim(q)
        return len(q) >= self.limit

    def busy(self) -> bool:
        """전체 실패가 많은가(늦출 때)."""
        self._trim(self._all)
        return self.global_limit is not None and len(self._all) >= self.global_limit

    def fail(self, key: str) -> float:
        now = time.monotonic()
        self._fails.setdefault(key, deque(maxlen=max(self.limit * 4, 64))).append(now)
        self._all.append(now)
        if len(self._fails) > 10000:  # 주소를 바꿔 가며 메모리를 채우지 못하게
            self._fails.pop(next(iter(self._fails)))
        return now

    def reserve(self, key: str) -> float:
        """검증하기 전에 실패 한 번으로 미리 센다(동시에 온 요청이 모두 검증까지 가지 못하게)."""
        return self.fail(key)

    def release(self, key: str, stamp: float) -> None:
        """맞았으면 미리 센 자리를 되돌린다."""
        for q in (self._fails.get(key), self._all):
            if q is not None:
                try:
                    q.remove(stamp)
                except ValueError:
                    pass

    def success(self, key: str) -> None:
        self._fails.pop(key, None)

    def clear(self) -> None:
        self._fails.clear()
        self._all.clear()


def is_trusted_peer(request: Request, trusted: tuple[str, ...]) -> bool:
    """연결의 상대 주소가 신뢰할 프록시 목록에 있는가."""
    if not trusted or not request.client:
        return False
    try:
        peer_ip = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return any(peer_ip in ipaddress.ip_network(net) for net in trusted)


def client_address(request: Request, trusted: tuple[str, ...]) -> str:
    """기본은 연결의 상대 주소다. 상대가 신뢰할 프록시일 때만 X-Forwarded-For의 마지막 값을 쓴다."""
    peer = request.client.host if request.client else "?"
    if not is_trusted_peer(request, trusted):
        return peer
    values = [v.strip() for v in request.headers.get("x-forwarded-for", "").split(",") if v.strip()]
    if not values:
        return peer
    try:
        return str(ipaddress.ip_address(values[-1]))
    except ValueError:
        return peer


def announce_setup_code(code: str) -> None:
    """코드는 표준 오류(컨테이너 로그)에만 한 번 찍는다. 구조화 로그 파일에는 코드 없이 사실만 남긴다."""
    try:
        sys.stderr.write(f"[{PROGRAM_NAME}] 첫 설정 코드: {code}  화면 /setup 에 넣으십시오. "
                         "다시 보기: python -m live_recorder account setup-code\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    log.info("첫 설정 코드를 만들었다. 코드는 컨테이너 로그에만 찍었다", extra={"event": "setup_code_created"})


async def ensure_setup_code(ctx: "WebContext") -> bool:
    """계정이 없으면 코드가 있게 한다(없으면 만들고 알린다). 코드를 끈 상태면 만들지 않는다."""
    code, created = await asyncio.to_thread(secretstore.ensure_setup_code, ctx.cfg.setup_code_path, ctx.cfg.account_path)
    if created and code:
        announce_setup_code(code)
    return code is not None


def request_is_https(request: Request, trusted: tuple[str, ...]) -> bool:
    """주소의 스킴 판정은 여기서만 한다. X-Forwarded-Proto는 신뢰할 프록시에서 온 요청일 때만 본다."""
    if request.url.scheme == "https":
        return True
    if not is_trusted_peer(request, trusted):
        return False
    values = [v.strip().lower() for v in request.headers.get("x-forwarded-proto", "").split(",") if v.strip()]
    return bool(values) and values[-1] == "https"


def set_session_cookie(response, request: Request, token: str, trusted: tuple[str, ...] = ()) -> None:
    secure = request_is_https(request, trusted)
    response.set_cookie(SESSION_COOKIE, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="strict",
                        secure=secure, path="/")


def build(ctx: "WebContext", render_index) -> APIRouter:
    """로그인·첫 설정·비밀번호 경로와 화면 문서(/, /login, /setup)."""
    r = APIRouter()
    cfg = ctx.cfg
    sessions, limiter, setup_limiter = ctx.sessions, ctx.limiter, ctx.setup_limiter
    setup_lock = asyncio.Lock()
    ui_dir = render_index.ui_dir

    @r.get("/login")
    async def login_page():
        if await ctx.account_generation() is None:
            return RedirectResponse("/setup", status_code=303)  # 계정이 없으면 로그인할 수 없다. 첫 설정으로
        return named_page(ui_dir / "login.html")

    @r.get("/setup")
    async def setup_page():
        """계정이 없을 때만 보인다. 계정이 있으면(로그인한 상태) 첫 화면으로 보낸다."""
        if await ctx.account_generation() is not None:
            return RedirectResponse("/", status_code=303)
        await ensure_setup_code(ctx)
        return named_page(ui_dir / "setup.html")

    @r.post("/api/setup")
    async def setup_submit(body: SetupBody, request: Request):
        """코드가 맞고 계정이 아직 없을 때만 계정을 하나 만든다. 만들면 코드를 지우고 로그인시킨다."""
        if await ctx.account_generation() is not None:
            return JSONResponse({"error": "관리자 계정이 이미 있습니다"}, status_code=409)
        need_code = secretstore.setup_code_enabled()
        if need_code and body.code is None:
            return JSONResponse({"detail": [{"type": "missing", "loc": ["body", "code"], "msg": "Field required"}]}, status_code=422)
        settings = await ctx.current_settings()
        client = client_address(request, settings.trusted_proxies)
        key = limit_key(client)
        if setup_limiter.blocked(key) or (settings.login_limit_enabled and limiter.blocked(key)):
            log.warning("첫 설정 잠금", extra={"event": "setup_locked", "client": client})
            return JSONResponse({"error": "설정 코드 실패가 많습니다. 잠시 뒤 다시 시도하십시오"}, status_code=429)
        # 형식 오류는 코드를 보기 전에 거른다(코드 맞춤 여부를 알려 주지 않고, 실패로 세지 않는다)
        try:
            username = secretstore.check_username(body.username)
            secretstore._check_password(body.password)
        except secretstore.AccountError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if body.password != body.password_confirm:
            return JSONResponse({"error": "비밀번호가 서로 다릅니다"}, status_code=400)
        await ensure_setup_code(ctx)
        if need_code:
            # 로그인과 같이 한 번에 하나씩, 검증 전에 자리를 먼저 센다(동시 요청으로 제한을 넘지 못하게)
            try:
                async with ctx.verify_queue.slot(setup_limiter.busy()):
                    if setup_limiter.blocked(key) or (settings.login_limit_enabled and limiter.blocked(key)):
                        log.warning("첫 설정 잠금", extra={"event": "setup_locked", "client": client})
                        return JSONResponse({"error": "설정 코드 실패가 많습니다. 잠시 뒤 다시 시도하십시오"}, status_code=429)
                    stamps = [(setup_limiter, setup_limiter.reserve(key))]
                    if settings.login_limit_enabled:
                        stamps.append((limiter, limiter.reserve(key)))  # 로그인 실패 제한에도 센다
                    matched = await ctx.db(secretstore.setup_code_matches, cfg.setup_code_path, body.code)
                    if matched:
                        for lim, stamp in stamps:
                            lim.release(key, stamp)
            except QueueFull:
                return JSONResponse({"error": BUSY}, status_code=429)
            if not matched:
                log.warning("첫 설정 코드 실패", extra={"event": "setup_code_failed", "client": client})  # 입력값은 남기지 않는다
                return JSONResponse({"error": "설정 코드가 틀립니다. " + SETUP_GUIDE}, status_code=403)
        async with setup_lock:
            try:
                await ctx.db(secretstore.create_account, cfg.account_path, username, body.password)
            except secretstore.AccountError as e:
                return JSONResponse({"error": str(e)}, status_code=409)
        await ctx.db(secretstore.clear_setup_code, cfg.setup_code_path)
        ctx.forget_account()
        setup_limiter.success(key)
        log.info("첫 설정 화면에서 관리자 계정을 만들었다", extra={"event": "account_created_setup", "client": key})
        token, _ = sessions.create(username, await ctx.account_generation())
        response = JSONResponse({"ok": True}, status_code=201)
        set_session_cookie(response, request, token, (await ctx.current_settings()).trusted_proxies)
        return response

    @r.get("/api/mode")
    async def mode():
        """화면이 보기 전용인지 알려 준다. 계정이 있는지 말고는 아무것도 드러내지 않는다."""
        # 비밀번호 규칙은 화면이 숫자를 따로 적지 않도록 서버 상수 하나에서 내려준다
        rule = {"min": secretstore.MIN_PASSWORD, "max": secretstore.MAX_PASSWORD, "text": secretstore.PASSWORD_RULE}
        # 화면의 날짜·시각 모양. 첫 화면을 그리기 전에 받아 예전 모양이 잠깐 보이지 않게 한다(비밀 아님)
        current = await ctx.current_settings()
        display = {"date": current.display_date_format, "time": current.display_time_format}
        if await ctx.account_generation() is None:
            await ensure_setup_code(ctx)  # 계정을 지운 뒤 첫 요청이면 새 코드를 만든다
            if not secretstore.setup_code_enabled():  # 첫 설정 화면은 코드 칸을 감추고 경고 한 줄을 보인다
                return {"mode": "view", "guide": VIEW_ONLY_GUIDE_NO_CODE, "setup": "/setup", "password": rule, "display": display,
                        "setup_code": False, "setup_warning": secretstore.SETUP_CODE_WARNING}
            return {"mode": "view", "guide": VIEW_ONLY_GUIDE, "setup": "/setup", "password": rule, "display": display,
                    "setup_code": True}
        return {"mode": "login", "password": rule, "display": display}

    async def password_attempt(request: Request, username: str, password: str, event: str) -> JSONResponse | None:
        """로그인과 비밀번호 변경의 현재 비밀번호 확인이 같은 횟수 제한을 쓴다. 통과하면 None."""
        settings = await ctx.current_settings()
        client = client_address(request, settings.trusted_proxies)
        key = limit_key(client)
        if settings.login_limit_enabled and not ctx.limiter_state["enabled"]:
            limiter.clear()  # 다시 켤 때 그때까지의 기록을 지운다(CLI로 바꾼 경우도 여기서 알아챈다)
        ctx.limiter_state["enabled"] = settings.login_limit_enabled
        limited = settings.login_limit_enabled
        locked = JSONResponse({"error": "비밀번호 실패가 많습니다. 잠시 뒤 다시 시도하십시오"}, status_code=429)
        if limited and limiter.blocked(key):
            log.warning("로그인 잠금", extra={"event": "login_locked", "client": client})
            return locked
        # 검증은 한 번에 하나씩(대기 수 상한). 전체 실패가 많으면 잠금 밖에서 늦춘다.
        # 잠금 안에서 주소 잠금을 다시 보고, 검증 전에 자리를 먼저 센다(동시 요청으로 제한을 넘지 못하게)
        try:
            async with ctx.verify_queue.slot(limited and limiter.busy()):
                if limited and limiter.blocked(key):
                    log.warning("로그인 잠금", extra={"event": "login_locked", "client": client})
                    return locked
                stamp = limiter.reserve(key) if limited else None  # 꺼 둔 동안에는 세지 않는다
                ok = await ctx.db(secretstore.verify_account, cfg.account_path, username, password)
                if ok and stamp is not None:
                    limiter.release(key, stamp)
        except QueueFull:
            return JSONResponse({"error": BUSY}, status_code=429)
        if not ok:
            log.warning("비밀번호 확인 실패", extra={"event": event, "client": client})  # 비밀번호는 남기지 않는다
            return JSONResponse({"error": "사용자 이름이나 비밀번호가 틀립니다"}, status_code=401)
        limiter.success(key)
        return None

    @r.post("/api/login")
    async def login(body: LoginBody, request: Request):
        denied = await password_attempt(request, body.username, body.password, "login_failed")
        if denied is not None:
            return denied
        token, session = sessions.create(body.username, await ctx.account_generation())
        response = JSONResponse({"ok": True})
        set_session_cookie(response, request, token, (await ctx.current_settings()).trusted_proxies)
        log.info("로그인", extra={"event": "login", "client": client_address(request, (await ctx.current_settings()).trusted_proxies)})
        return response

    @r.post("/api/account/password")
    async def change_password(body: PasswordBody, request: Request):
        """현재 비밀번호를 다시 확인한다. 바꾸면 모든 세션이 끊긴다."""
        username = request.state.session["username"]
        denied = await password_attempt(request, username, body.current, "password_check_failed")
        if denied is not None:
            return denied
        await ctx.db(secretstore.change_password, cfg.account_path, body.new)
        sessions.drop_all()
        ctx.forget_account()
        log.info("비밀번호 변경", extra={"event": "password_changed"})
        if body.revoke_keys and ctx.keys is not None:
            count = await ctx.db(ctx.keys.revoke_all)
            log.info("접근 키 모두 폐기 %s개", count, extra={"event": "api_keys_revoked_all"})
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @r.get("/")
    async def index(request: Request):
        # 계정이 없으면 첫 화면은 첫 설정이다. 보기 전용 화면은 /?view=1로 연다
        if getattr(request.state, "view_only", False) and request.query_params.get("view") != "1":
            return RedirectResponse("/setup", status_code=303)
        return render_index()

    @r.get("/api/session")
    async def session_info(request: Request):
        s = request.state.session
        return {"username": s["username"], "csrf": s["csrf"]}

    @r.post("/api/logout")
    async def logout(request: Request):
        sessions.drop(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return r
