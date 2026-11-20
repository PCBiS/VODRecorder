"""웹 서버 뼈대: 앱 만들기, 인증, 접근 관문, 정적 파일, 공통 오류 형식, 화면 문서.

기능별 경로는 `routes/`의 묶음이 만든다(채널·녹화, 기록, 쿠키, 설정, 파일 관리자, 로그 보기·내려받기, 다시보기).
묶음끼리는 서로 부르지 않고 `WebContext`만 쓴다. 켜진 확장의 경로·접근 규칙·화면 파일도 여기서 붙인다.
엔진은 같은 프로세스에서 돌고, 웹 API는 `Engine`의 메서드를 직접 부른다.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import html
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse

from ..core import apikeys, childenv, secretstore
from ..core import store as st
from .. import NAME_SLOT, PROGRAM_NAME
from ..core.config import AppConfig, ConfigError
from ..core.extensions import ExtensionError, ExtensionHost, read_enabled
from ..platforms import registry
from ..recording.engine import Engine, Timings
from . import auth, keyauth
from .access import KEY_NONE, KEY_OPERATE, KEY_VIEW, LOGIN, PUBLIC, VIEW, AccessTable, rule_matches
from .auth import SESSION_COOKIE, VIEW_ONLY_GUIDE, announce_setup_code
from .context import WebContext
from .routes import apikeys as apikey_routes
from .routes import channels, cookies, files, history, logs, settings

log = logging.getLogger("live_recorder.web")

UI_DIR = Path(__file__).resolve().parent.parent / "webui"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
STATIC_CACHE_CONTROL = "no-cache"  # 쓸 때마다 서버에 묻는다(저장은 해도 된다)
# 화면 뼈대(app.js)가 순서대로 불러오는 화면별 스크립트. index.html의 lr-scripts와 같아야 한다(시험이 본다)
CORE_SCRIPTS = ("/static/views/channels.js", "/static/views/history.js",
                "/static/views/files.js", "/static/views/cookies.js", "/static/views/logs.js", "/static/views/settings.js")
CORE_STYLES: tuple[str, ...] = ()


MAX_BODY = 1024 * 1024  # 요청 본문의 기본 상한(1 MiB)
CONCURRENCY_ENV = "LIVE_RECORDER_MAX_CONNECTIONS"  # 동시 연결·요청 상한(uvicorn limit_concurrency)
DEFAULT_CONCURRENCY = 200


def concurrency_limit() -> int:
    raw = os.environ.get(CONCURRENCY_ENV, "").strip()
    if not raw:
        return DEFAULT_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{CONCURRENCY_ENV}는 정수입니다") from None
    if value < 20:
        raise ConfigError(f"{CONCURRENCY_ENV}는 20 이상입니다(화면·다운로드·API를 함께 쓰면 연결이 여럿 열립니다)")
    return value


HOSTS_ENV = "LIVE_RECORDER_ALLOWED_WEB_HOSTS"  # 받을 Host 이름(쉼표 구분). 비면 모두 받는다


def allowed_web_hosts() -> tuple[str, ...]:
    raw = os.environ.get(HOSTS_ENV, "")
    return tuple(h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip())


def host_allowed(host_header: str | None, allowed: tuple[str, ...]) -> bool:
    """Host 머리글 검사(DNS 리바인딩 막기). 목록이 비면 모두, IP 주소와 localhost는 늘 받는다."""
    if not allowed:
        return True
    import ipaddress

    host = (host_header or "").strip().lower()
    if host.startswith("["):  # [IPv6]:포트
        name = host[1:host.find("]")] if "]" in host else ""
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.rstrip(".")
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name in allowed
BODY_LIMITS: dict[str, int] = {"/api/cookies/upload": 2 * 1024 * 1024}  # 더 큰 본문이 필요한 경로(쿠키 파일 글 256 KiB를 JSON에 싣는다)
TOO_LARGE = "요청 본문이 너무 큽니다"


class BodyLimit:
    """요청 본문 크기 상한. 본문을 읽기 전에 Content-Length를 보고, 길이를 밝히지 않은(chunked) 본문은 받은 양을 세다가
    넘으면 끊는다. 넘으면 413. 관문(guard)보다 바깥에서 돈다."""

    def __init__(self, app, limit: int = MAX_BODY, per_path: dict[str, int] | None = None):
        self.app, self.limit, self.per_path = app, limit, dict(per_path or {})

    async def _reject(self, send) -> None:
        body = json.dumps({"error": TOO_LARGE, "detail": TOO_LARGE, "code": "too_large"}, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.per_path.get(scope.get("path", ""), self.limit)
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        return await self._reject(send)
                except ValueError:
                    return await self._reject(send)
        received = 0
        state = {"started": False, "rejected": False}

        async def counted():
            nonlocal received
            if state["rejected"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > limit:
                    # 넘으면 곧바로 413을 보내고 연결을 끊은 것으로 알린다(앱이 그 뒤에 보내는 응답은 버린다)
                    if not state["started"]:
                        state["rejected"] = True
                        await self._reject(send)
                    return {"type": "http.disconnect"}
            return message

        async def tracked(message):
            if state["rejected"]:
                return
            if message.get("type") == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except Exception:
            if not state["rejected"]:
                raise


class UIStaticFiles(StaticFiles):
    """화면 자산(`/static/`). 빌드가 파일 시각을 고정하므로 Starlette 기본 ETag(수정 시각·크기)로는
    크기가 같은 채 내용만 바뀐 파일을 가려내지 못한다. ETag를 내용의 SHA-256으로 만들고, 304는 ETag가 같을 때만 준다
    (If-Modified-Since만 온 요청에는 늘 새 내용을 준다). 파일이 작아서 요청마다 다시 계산한다."""

    def file_response(self, full_path, stat_result, scope, status_code: int = 200) -> Response:
        response = FileResponse(full_path, status_code=status_code, stat_result=stat_result)
        with open(full_path, "rb") as fh:
            digest = hashlib.file_digest(fh, "sha256").hexdigest()
        response.headers["etag"] = f'"{digest[:32]}"'
        response.headers["cache-control"] = STATIC_CACHE_CONTROL
        if self.is_not_modified(response.headers, Headers(scope=scope)):
            return NotModifiedResponse(response.headers)
        return response

    def is_not_modified(self, response_headers: Headers, request_headers: Headers) -> bool:
        if not request_headers.get("if-none-match"):
            return False  # 시각 비교(If-Modified-Since)는 쓰지 않는다. 시각이 빌드마다 같다
        return super().is_not_modified(response_headers, request_headers)


def content_security_policy() -> str:
    """썸네일은 브라우저가 영상 CDN에서 직접 부른다. img-src는 등록된 플랫폼이 밝힌 썸네일 호스트뿐이다
    (다시보기 썸네일 호스트도 치지직 모듈의 목록에 들어 있다)."""
    hosts: list[str] = []
    for platform in registry.all_platforms():
        hosts += [h for h in platform.thumbnail_hosts if h not in hosts]
    img = " ".join(["'self'", "data:"] + [f"https://*.{h}" for h in hosts])
    return (f"default-src 'self'; script-src 'self'; style-src 'self'; img-src {img}; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


SLOT = re.compile(r"<!-- lr-slot:([a-z0-9-]+) -->\n?")


def render_slots(text: str, fragments: dict[str, str]) -> str:
    """켜진 확장의 HTML 조각을 자리에 넣는다(조각 안의 자리도 채운다). 빈 자리는 지운다."""
    for _ in range(4):
        new = SLOT.sub(lambda m: fragments.get(m.group(1), ""), text)
        if new == text:
            break
        text = new
    return text


class IndexPage:
    """화면 문서(index.html). 불러올 스크립트·스타일 목록을 meta 두 개에 넣고, 켜진 확장의 조각을 자리에 넣어 내려 준다."""

    def __init__(self, ui_dir: Path, scripts: list[str], styles: list[str], fragments: dict[str, str] | None = None):
        self.ui_dir = ui_dir
        self.scripts, self.styles = scripts, styles
        self.fragments = fragments or {}

    def __call__(self) -> Response:
        text = render_slots((self.ui_dir / "index.html").read_text(encoding="utf-8"), self.fragments)
        text = text.replace(NAME_SLOT, html.escape(PROGRAM_NAME))
        for name, values in (("lr-scripts", self.scripts), ("lr-styles", self.styles)):
            start = text.index(f'<meta id="{name}" name="{name}" content="')
            end = text.index('">', start)
            text = text[:start] + f'<meta id="{name}" name="{name}" content="{html.escape(" ".join(values))}' + text[end:]
        return HTMLResponse(text)


def _route_keys(router) -> list[tuple[str, str]]:
    """묶음의 모든 경로(방법, 경로 꼴). 묶음 안에 또 넣은 묶음(include_router)까지 편다(keyauth.flat_routes와 같은 방식)."""
    from fastapi.routing import iter_route_contexts

    out = []
    for c in iter_route_contexts(getattr(router, "routes", [])):
        if isinstance(c.original_route, APIRoute):
            out += [(m, c.path) for m in sorted(c.methods or ())]
    return out


def _check_extension_routes(host: ExtensionHost, core_routes: list[tuple[str, str]]) -> tuple[list, list]:
    """확장 경로에 접근 규칙이 없거나 다른 경로와 겹치면 서버를 띄우지 않는다."""
    routers, rules, key_rules = [], [], []
    taken = {k: "중심부" for k in core_routes}
    for ext in host.active:
        router = ext.routes(host.ctx(ext))
        if router is None:
            continue
        for rule in ext.access_rules:
            if rule.level not in (PUBLIC, VIEW, LOGIN):
                raise ExtensionError(f"{ext.name}: 모르는 접근 수준 {rule.level!r}")
            rules.append((rule.method, rule.path, rule.level))
            if rule.key not in (KEY_NONE, KEY_VIEW, KEY_OPERATE):
                raise ExtensionError(f"{ext.name}: 모르는 키 권한 {rule.key!r}")
            key_rules.append((rule.method, rule.path, rule.key))  # 확장의 경로는 확장이 밝힌다
        for method, path in _route_keys(router):
            if path.startswith("/static"):
                raise ExtensionError(f"{ext.name}: /static 아래 경로는 등록할 수 없습니다: {path}")
            if not any(rule.method == method and rule_matches(rule.path, path) for rule in ext.access_rules):
                raise ExtensionError(f"{ext.name}: 접근 규칙이 없는 경로 {method} {path}")
            if (method, path) in taken:
                raise ExtensionError(f"{ext.name}: 경로 {method} {path}가 {taken[(method, path)]}와 겹칩니다")
            taken[(method, path)] = f"{ext.name} 확장"
        routers.append(router)
    # 한 경로를 가리키는 규칙이 묶음 규칙(*)보다 먼저 맞도록(다른 확장의 묶음 규칙에 가리지 않게) 앞에 둔다
    key_rules.sort(key=lambda r: r[1].endswith("*"))
    return routers, rules, key_rules


def _upgrade_store(cfg: AppConfig) -> None:
    """이전 빌드의 저장소면 켠 확장을 정하기 전에 먼저 옮긴다(사본을 남기고 한 번). 깨진 저장소는 엔진이 알린다."""
    if cfg.db_path.exists():
        try:
            st.Store(cfg.db_path, cfg.record_root, cfg.telegram_path).close()
        except st.StoreError:
            pass


def create_app(cfg: AppConfig, timings: Timings | None = None, disk_free: Callable[[Path], int] | None = None,
               files_after_check: Callable | None = None) -> FastAPI:
    engine = Engine(cfg, timings, disk_free=disk_free)
    csp = content_security_policy()
    _upgrade_store(cfg)
    # 켜진 확장은 서버가 뜰 때 정한다(도는 동안 바꾸지 않는다). 꺼진 확장은 아무것도 등록하지 않는다
    extensions = ExtensionHost.load(cfg, read_enabled(cfg.db_path), engine.events, disk_free=engine.disk_free)

    # 계정은 웹 밖에서 만든다: CLI `account create` 또는 첫 기동 때의 환경변수
    env_user, env_pass = os.environ.get("LIVE_RECORDER_ADMIN_USER"), os.environ.get("LIVE_RECORDER_ADMIN_PASSWORD")
    childenv.forget_startup_secrets()  # 읽은 비밀번호는 환경에서 지운다(하위 프로세스·진단에 남지 않게)
    if env_user and env_pass and not secretstore.account_exists(cfg.account_path):
        secretstore.create_account(cfg.account_path, env_user, env_pass)
        secretstore.clear_setup_code(cfg.setup_code_path)
        log.info("환경변수로 관리자 계정을 만들었다", extra={"event": "account_created"})

    def open_extensions(store) -> None:
        """엔진이 저장소를 연 뒤, 녹화 감시와 server_started보다 먼저: 켜진 확장의 저장소를 열고 사건 듣기를 건다."""
        extensions.bind(store, engine.gate_for)  # 확장도 플랫폼 요청 창구를 함께 쓴다
        extensions.open()

    engine.store_ready.append(open_extensions)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await engine.start()
        if not await asyncio.to_thread(secretstore.account_exists, cfg.account_path):
            code, created = await asyncio.to_thread(secretstore.ensure_setup_code, cfg.setup_code_path, cfg.account_path)
            if not secretstore.setup_code_enabled():
                # 코드 없이 누구나 계정을 만들 수 있다. 시작할 때마다 한 번 알린다
                log.warning(secretstore.SETUP_CODE_WARNING, extra={"event": "setup_code_disabled"})
                sys.stderr.write(f"[{PROGRAM_NAME}] {secretstore.SETUP_CODE_WARNING}. 화면 /setup 에서 만드십시오\n")
            elif created and code:
                announce_setup_code(code)
            else:
                sys.stderr.write(f"[{PROGRAM_NAME}] 관리자 계정이 없습니다. 화면 /setup 에서 만드십시오. "
                                 "설정 코드는 python -m live_recorder account setup-code 로 볼 수 있습니다\n")
        else:
            await asyncio.to_thread(secretstore.clear_setup_code, cfg.setup_code_path)
        proxies = os.environ.get("LIVE_RECORDER_TRUSTED_PROXIES")
        if proxies is not None:
            # 컨테이너 환경변수로 신뢰할 프록시를 정한다. 기동할 때마다 저장값을 이 값으로 바꾼다(빈 값이면 비운다)
            try:
                await asyncio.to_thread(engine.store.set_settings, {"trusted_proxies": proxies})
            except ConfigError:
                await engine.stop()
                raise
            log.info("환경변수로 신뢰할 프록시를 정했다", extra={"event": "trusted_proxies_env"})
        try:
            await extensions.start()
        except BaseException:
            await extensions.stop()
            await engine.stop()  # 확장 저장소를 옮기지 못하면 서버는 뜨지 않는다(녹화도 멈춘다)
            raise
        try:
            yield
        finally:
            if ctx.keys is not None:
                await asyncio.to_thread(ctx.keys.flush)  # 마지막 사용 시각·횟수
            await extensions.stop()
            await engine.stop()

    # 자동 API 문서 경로를 끈다
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, title=PROGRAM_NAME)
    st.reserve_dirs(cfg.data_dir, cfg.log_dir)  # 저장 폴더로 쓸 수 없는 폴더
    ctx = WebContext(cfg=cfg, engine=engine, app=app, extensions=extensions)
    ctx.file_manager = files.make_manager(ctx, after_check=files_after_check)
    ctx.keys = apikeys.KeyStore(cfg.api_keys_path) if apikeys.enabled() else None
    app.state.engine = engine
    app.state.files = ctx.file_manager
    app.state.sessions = ctx.sessions
    app.state.limiter = ctx.limiter
    app.state.keys = ctx.keys
    app.state.key_rates = ctx.key_rates
    app.state.extensions = extensions

    manifest = extensions.ui_manifest()
    index_page = IndexPage(UI_DIR, list(CORE_SCRIPTS) + manifest["scripts"], list(CORE_STYLES) + manifest["styles"],
                           extensions.html_fragments())

    core_routers = [auth.build(ctx, index_page), channels.build(ctx), history.build(ctx), cookies.build(ctx),
                    settings.build(ctx), files.build(ctx), logs.build(ctx), apikey_routes.build(ctx)]
    ext_routers, ext_rules, ext_key_rules = _check_extension_routes(extensions, [k for r in core_routers for k in _route_keys(r)])
    access = AccessTable(tuple(ext_rules), tuple(ext_key_rules))
    app.state.access = access

    def deny(request: Request, status: int, message: str) -> Response:
        if request.url.path.startswith("/api/") or request.method not in ("GET", "HEAD"):
            return JSONResponse({"error": message}, status_code=status)
        return RedirectResponse("/login", status_code=303)

    async def same_origin(request: Request) -> bool:
        """Origin을 이 서버의 Host와 견준다. 신뢰할 프록시에서 온 요청이고 X-Forwarded-Host가 있으면
        그 값(마지막 값)과 견준다. 그 밖의 요청에서는 X-Forwarded-Host를 읽지 않는다."""
        site = request.headers.get("sec-fetch-site")
        if site and site not in ("same-origin", "none"):
            return False
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            forwarded = request.headers.get("x-forwarded-host")
            if forwarded and auth.is_trusted_peer(request, (await ctx.current_settings()).trusted_proxies):
                values = [v.strip() for v in forwarded.split(",") if v.strip()]
                host = values[-1] if values else host
            if origin.split("://", 1)[-1].lower() != host.lower():
                return False
        return True

    web_hosts = allowed_web_hosts()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """접근 관문. 확장 경로도 같은 관문을 거친다."""
        path = request.url.path
        if not host_allowed(request.headers.get("host"), web_hosts):
            return JSONResponse({"error": "허용하지 않는 Host입니다", "code": "bad_host"}, status_code=400)
        unsafe = request.method not in SAFE_METHODS
        level = access.level(request.method, path)
        response: Response
        bearer = keyauth.bearer_value(request) if ctx.keys is not None else None
        try:
            if unsafe and not await same_origin(request):
                response = JSONResponse({"error": "다른 출처의 요청"}, status_code=403)
            elif bearer is not None:
                # 접근 키 요청: 세션 쿠키·CSRF를 보지 않는다. 키 권한은 접근 규칙 표가 정한다
                response = await keyauth.handle(ctx, request, call_next, bearer, access)
            elif level == PUBLIC:
                response = await call_next(request)
            elif await ctx.account_generation() is None:
                # 보기 전용 모드(계정 없음). 계정을 만들거나 지우면 다음 요청부터 바뀐다(재시작 불필요)
                if level == VIEW:
                    request.state.view_only = True
                    response = await call_next(request)
                else:
                    response = JSONResponse({"error": auth.view_only_guide(), "view_only": True}, status_code=403)
            else:
                current = await ctx.current_settings()
                session = ctx.sessions.get(request.cookies.get(SESSION_COOKIE), current.session_minutes,
                                           await ctx.account_generation())
                if session is None:
                    response = deny(request, 401, "로그인이 필요합니다")  # 기본 거부
                elif unsafe and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
                    response = JSONResponse({"error": "CSRF 토큰이 없거나 틀립니다"}, status_code=403)
                else:
                    request.state.session = session
                    response = await call_next(request)
        except secretstore.SecretFileError as e:
            # 관문에서 계정·키 파일을 읽다 깨진 것을 만나도 알아볼 수 있는 오류로(로그인이 필요한 경로, Bearer 요청)
            log.error("비밀 파일을 읽지 못함", extra={"event": "secret_file_broken"})
            response = JSONResponse({"error": str(e), "code": "secret_file_broken"}, status_code=500)
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if not path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"  # 화면 문서·API는 저장하지 않는다. /static/은 UIStaticFiles가 정한다
        return response

    # 본문 크기 상한. Starlette는 나중에 더한 미들웨어가 바깥에서 먼저 돈다: 관문 뒤에 더해 관문보다 먼저 본문 크기를 본다
    app.add_middleware(BodyLimit, limit=MAX_BODY, per_path=BODY_LIMITS)

    # 확장이 밝힌 오류(status·detail·extra)도 같은 꼴로 낸다
    async def extension_error(request: Request, exc: Exception):
        return JSONResponse({**getattr(exc, "extra", {}), "error": exc.detail}, status_code=exc.status)

    for ext in extensions.active:
        for error in ext.http_errors:
            app.add_exception_handler(error, extension_error)

    # 공통 오류 형식: {"error": 문장}
    @app.exception_handler(st.ValidationError)
    @app.exception_handler(ConfigError)
    @app.exception_handler(secretstore.AccountError)
    async def bad_request(request: Request, exc: Exception):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        """입력 검사 오류. 보낸 값(input)은 되돌려 주지 않는다(비밀번호·쿠키가 응답에 실리지 않게)."""
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(RecursionError)
    async def too_deep(request: Request, exc: Exception):
        return JSONResponse({"error": "입력의 구조가 너무 깊습니다", "code": "too_deep"}, status_code=400)

    @app.exception_handler(secretstore.SecretFileError)
    async def broken_secret(request: Request, exc: Exception):
        log.error("비밀 파일을 읽지 못함", extra={"event": "secret_file_broken"})
        return JSONResponse({"error": str(exc), "code": "secret_file_broken"}, status_code=500)

    # 확장의 화면 파일(/static/ext/<이름>/...). 중심부 화면 자산보다 먼저 붙인다
    for ext in extensions.active:
        if ext.static_dir is not None:
            app.mount(f"/static/ext/{ext.name}", UIStaticFiles(directory=ext.static_dir), name=f"ext-{ext.name}")
    app.mount("/static", UIStaticFiles(directory=UI_DIR), name="static")
    for router in core_routers + ext_routers:
        app.include_router(router)
    return app


def run(cfg: AppConfig, host: str = "127.0.0.1", port: int = 8080) -> None:
    """기본 바인딩은 127.0.0.1이다."""
    import signal

    import uvicorn

    # uvicorn은 정리를 끝낸 뒤 받은 신호를 원래 처리기로 다시 보낸다. 기본 처리기면 종료코드가 143이 되므로
    # 아무것도 하지 않는 처리기를 먼저 걸어 정상 종료(0)로 끝나게 한다.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: None)
    server = uvicorn.Server(server_config(create_app(cfg), host, port))
    server.run()


def server_config(app, host: str, port: int):
    import uvicorn

    return uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False,
                          timeout_graceful_shutdown=5, server_header=False,
                          limit_concurrency=concurrency_limit(),  # 동시 연결 상한(넘으면 503)
                          proxy_headers=False)  # 전달 머리글은 앱이 신뢰 프록시 설정으로만 본다
