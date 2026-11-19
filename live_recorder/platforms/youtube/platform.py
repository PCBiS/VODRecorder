"""유튜브 플랫폼 모듈의 접점 객체. 13.1절의 요구는 이 모듈의 요구다.

채널 주소 인식, 방송 확인과 재생 주소, 쿠키 파일 검사, 허용 호스트,
로그 가리기 이름이 모두 여기 있다. yt-dlp는 `ytdlp.py` 한 파일만 쓴다. 밖에서는 `PLATFORM`으로만 쓴다.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from ...core.quality import Track
from .. import cookiefile
from . import vendor
from ..base import (ApiError, BroadcastChanged, ChannelLookupNeeded, ChannelParseError, CheckResult, CredentialSpec,
                    Features, LiveInfo, Playback, Verdict, host_allowed)

# 이 환경변수가 off(또는 0·false·no)면 유튜브를 켤 수 없다(켠 기록이 있어도 꺼진 것으로 본다)
ENV_FORBID = "LIVE_RECORDER_YOUTUBE"

# 시험이 yt-dlp를 부르는 파일을 가짜로 바꿔 끼우는 자리(수신기 프로세스도 같은 환경변수를 받는다)
BACKEND_ENV = "LIVE_RECORDER_YTDLP_BACKEND"
DEFAULT_BACKEND = "live_recorder.platforms.youtube.ytdlp"


def backend():
    return importlib.import_module(os.environ.get(BACKEND_ENV) or DEFAULT_BACKEND)


CHANNEL_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")  # 24자, 대소문자를 가린다
HANDLE_RE = re.compile(r"^@[^\s/?#@]{1,100}$")
CHANNEL_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com")
LIVE_URL = "https://www.youtube.com/channel/{}/live"
HANDLE_URL = "https://www.youtube.com/{}"
STREAM_HOSTS = ("googlevideo.com",)  # 수신기가 직접 요청하는 호스트
THUMB_HOSTS = ("ytimg.com",)
STREAM_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"

# 쿠키 파일
COOKIE_KEY = "cookies_txt"
MAX_COOKIE_BYTES = 256 * 1024
COOKIE_DOMAINS = ("youtube.com", "google.com")
LOGIN_SID = ("SID", "__Secure-3PSID")
LOGIN_SAPISID = ("SAPISID", "__Secure-3PAPISID")
NETSCAPE_HEADER = "# Netscape HTTP Cookie File"

COOKIE_HELP = (
    "공개 방송은 쿠키 없이 녹화됩니다. 멤버십 전용·연령 제한 방송에만 쿠키를 넣으십시오. "
    "가져오는 순서: 브라우저의 시크릿 창에서 유튜브에 로그인합니다 → 같은 탭에서 youtube.com/robots.txt로 이동합니다 → "
    "쿠키 내보내기 확장 기능으로 cookies.txt(Netscape 꼴)를 내보낸 뒤 시크릿 창을 닫습니다. "
    "평소 쓰는 창에서 뽑은 쿠키는 그 창을 계속 쓰면 금방 무효가 됩니다. "
    "계정을 이 도구에 쓰면 유튜브가 그 계정을 제한할 수 있습니다. 녹화용으로 따로 만든 계정을 권합니다. "
    "저장한 쿠키는 화면에 다시 나오지 않습니다."
)


def parse_channel(text: str) -> str:
    """`UC…`, `youtube.com/channel/UC…`, `@이름`, `youtube.com/@이름`. `@이름`은 ChannelLookupNeeded."""
    value = str(text or "").strip()
    if CHANNEL_ID_RE.match(value):
        return value
    if HANDLE_RE.match(value):
        raise ChannelLookupNeeded(value)
    candidate = value if re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I) else "https://" + value
    try:
        parts = urlsplit(candidate)
    except ValueError:
        raise ChannelParseError(PLATFORM.channel_rule["error"]) from None
    if parts.scheme.lower() not in ("https", "http") or (parts.hostname or "").lower() not in CHANNEL_HOSTS:
        raise ChannelParseError(PLATFORM.channel_rule["error"])
    segs = [unquote(s) for s in parts.path.split("/") if s]
    if len(segs) >= 2 and segs[0] == "channel" and CHANNEL_ID_RE.match(segs[1]):
        return segs[1]
    if segs and HANDLE_RE.match(segs[0]):
        raise ChannelLookupNeeded(segs[0])
    raise ChannelParseError(PLATFORM.channel_rule["error"])


# ---- 쿠키 파일 ----
def _cookie_rows(text: str) -> list[list[str]]:
    rows = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("#HttpOnly_"):
            body = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        else:
            body = line
        cols = body.split("\t")
        if len(cols) != 7 or not cols[0] or cols[1] not in ("TRUE", "FALSE") or cols[3] not in ("TRUE", "FALSE") \
                or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", cols[4] or "0"):
            raise ValueError("쿠키 파일 꼴이 아닙니다. 브라우저에서 내보낸 cookies.txt(Netscape 꼴) 글 전체를 넣으십시오")
        rows.append([line[: len(line) - len(body)]] + cols)
    return rows


def _domain_ok(domain: str) -> bool:
    d = domain.lstrip(".").lower()
    return any(d == x or d.endswith("." + x) for x in COOKIE_DOMAINS)


def clean_cookie_file(text: str) -> str:
    if len(text.encode("utf-8")) > MAX_COOKIE_BYTES:
        raise ValueError(f"쿠키 파일이 너무 큽니다({MAX_COOKIE_BYTES // 1024}KB 이하만 받습니다)")
    rows = [r for r in _cookie_rows(text) if _domain_ok(r[1])]
    names = {r[6] for r in rows}
    if not rows or not (names & set(LOGIN_SID)) or not (names & set(LOGIN_SAPISID)):
        raise ValueError("로그인한 쿠키가 아닙니다. 유튜브에 로그인한 창에서 내보낸 쿠키를 넣으십시오")
    lines = [NETSCAPE_HEADER, ""] + [r[0] + "\t".join(r[1:]) for r in rows]
    return "\n".join(lines) + "\n"


def cookie_summary(text: str) -> dict[str, Any]:
    """화면용: 쿠키 수와 로그인 쿠키의 가장 이른 만료일(값은 넣지 않는다)."""
    try:
        rows = [r for r in _cookie_rows(text) if _domain_ok(r[1])]
    except ValueError:
        return {"count": 0, "login_expires": None}
    expiries = []
    for r in rows:
        if r[6] in LOGIN_SID + LOGIN_SAPISID:
            try:
                value = float(r[5])
            except ValueError:
                continue
            if value > 0:
                expiries.append(value)
    earliest = datetime.fromtimestamp(min(expiries), timezone.utc).isoformat() if expiries else None
    return {"count": len(rows), "login_expires": earliest}


def cookie_text(credentials: dict[str, str] | None) -> str | None:
    return (credentials or {}).get(COOKIE_KEY) or None


# ---- 방송 확인 ----
def live_info(info: dict[str, Any]) -> LiveInfo | None:
    """yt-dlp 결과에서 방송 정보. 방송 중이 아니면 None."""
    if not info.get("is_live") and info.get("live_status") != "is_live":
        return None
    vid = str(info.get("id") or "")
    stamp = info.get("release_timestamp") or info.get("timestamp")
    try:
        started = datetime.fromtimestamp(float(stamp), timezone.utc) if stamp else datetime.now(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        started = datetime.now(timezone.utc)
    tracks, manifest = [], None
    for f in info.get("formats") or []:
        if not isinstance(f, dict) or not str(f.get("protocol") or "").startswith("m3u8"):
            continue
        manifest = manifest or f.get("manifest_url")
        if f.get("height") and (f.get("vcodec") or "none") != "none":
            tbr = f.get("tbr")
            tracks.append(Track(str(f.get("format_id") or f["height"]), int(f["height"]),
                                float(f["fps"]) if f.get("fps") else None, int(float(tbr) * 1000) if tbr else None))
    return LiveInfo(live_id=vid, title=str(info.get("title") or ""), category="",
                    adult=int(info.get("age_limit") or 0) >= 18, open_date=started.isoformat(),
                    image_url=str(info.get("thumbnail") or ""), started_at=started, tracks=tracks,
                    hls_url=manifest, time_machine_active=None, playback_live_id=vid)


def outcome_result(outcome, has_cookies: bool) -> CheckResult:
    kind, message = outcome.kind, outcome.message or ""
    short = message[:200]
    if kind == "ok":
        info = live_info(outcome.info or {})
        return CheckResult(Verdict.LIVE, info) if info else CheckResult(Verdict.OFFLINE)
    if kind == "offline":
        return CheckResult(Verdict.OFFLINE)  # 방송 없음, 예정된 방송만 있음
    if kind in ("bot", "rate"):
        detail = "봇 확인 요구" if kind == "bot" else "요청이 너무 많습니다(HTTP 429)"
        return CheckResult(Verdict.UNKNOWN, detail=f"youtube: {detail}", throttled=True)
    if kind == "login":
        return CheckResult(Verdict.UNKNOWN, detail="youtube: 로그인이 필요한 방송",
                           credential_problem="rejected" if has_cookies else "needed")
    return CheckResult(Verdict.UNKNOWN, detail=f"youtube: {kind}: {short}".strip())


class YouTubeClient:
    """코어용 비동기 클라이언트. yt-dlp 호출은 스레드에서 돈다."""

    def __init__(self):
        self.backend = backend()
        self.source = self.backend.select_source()

    async def _extract(self, url: str, credentials: dict[str, str] | None, *, flat: bool = False):
        return await asyncio.to_thread(self.backend.extract, url, cookie_text(credentials), flat=flat, source=self.source)

    async def check_live(self, channel_id: str, credentials: dict[str, str] | None) -> CheckResult:
        outcome = await self._extract(LIVE_URL.format(channel_id), credentials)
        return outcome_result(outcome, bool(cookie_text(credentials)))

    async def playback(self, channel_id: str, info: LiveInfo, credentials: dict[str, str] | None, *,
                       timemachine: bool) -> Playback:
        # 타임머신 API가 없다. 일반 재생목록의 창을 그대로 쓴다
        refresh = {"channel_id": channel_id, "video_id": info.live_id, "ytdlp": self.source.token()}
        return Playback(info.hls_url or "", "live", refresh)

    async def check_credentials(self, credentials: dict[str, str] | None) -> str:
        return "missing" if not cookie_text(credentials) else "valid"  # 확인 방법이 없다. 저장 여부만

    async def lookup_channel(self, query: str, credentials: dict[str, str] | None) -> tuple[str, str]:
        """`@이름` → (채널 ID, 채널 이름)."""
        outcome = await self._extract(HANDLE_URL.format(query), credentials, flat=True)
        if outcome.kind == "ok":
            info = outcome.info or {}
            cid = str(info.get("channel_id") or info.get("id") or "")
            name = str(info.get("channel") or info.get("uploader") or info.get("title") or query).strip()
            if CHANNEL_ID_RE.match(cid):
                return cid, name.removesuffix(" - Videos").removesuffix(" - Home") or query
            raise ChannelParseError("채널 ID를 알아내지 못했습니다")
        if outcome.kind == "notfound":
            raise ChannelParseError(f"{query} 채널을 찾지 못했습니다")
        raise ApiError(f"youtube: {outcome.kind}: {(outcome.message or '')[:200]}")

    async def aclose(self) -> None:
        pass


class YouTubePlatform:
    id = "youtube"
    name = "유튜브"
    features = Features(timemachine=False, rewind=True, live_rewind=True)
    credential_spec = CredentialSpec(names=(COOKIE_KEY,), title="유튜브 쿠키", help=COOKIE_HELP, checkable=False,
                                     kind="file", max_bytes=MAX_COOKIE_BYTES)
    thumbnail_hosts = THUMB_HOSTS
    channel_hint = "https://www.youtube.com/@… · /channel/UC… 또는 @이름"
    channel_id_error = "채널 ID는 UC로 시작하는 24자입니다"
    channel_rule = {"id_pattern": "^UC[0-9A-Za-z_-]{22}$", "hosts": list(CHANNEL_HOSTS), "path_prefix": "channel",
                    "case_sensitive": True, "lookup_pattern": "^@[^\\s/?#@]{1,100}$",
                    "error": "유튜브 채널 주소(youtube.com/@… 또는 /channel/UC…)나 UC로 시작하는 채널 ID가 아닙니다"}
    # 쿠키 이름, 재생 주소의 서명·만료 등 질의와 경로 값, 영상 ID(더 가리기에서만)
    log_mask = {
        "cookie_patterns": (r"(?:__Secure-[0-9A-Z]+-)?(?:SID|HSID|SSID|APISID|SAPISID|SIDCC|SIDTS|PSID|PAPISID|PSIDTS|PSIDCC)",
                            "LOGIN_INFO", "VISITOR_INFO1_LIVE", "VISITOR_PRIVACY_METADATA", "YSC"),
        "query_params": ("expire", "ei", "ip", "id", "sparams", "lsig", "lsparams", "pot", "n", "mn", "ms", "mv", "pl",
                         "initcwndbps", "vprv", "xpc", "bui", "spc", "rqh", "hls_chunk_host", "mt", "sgoap", "sgovp",
                         "pcm2cms", "met", "mh", "mm", "cps", "ns"),
        "path_params": ("expire", "ei", "ip", "id", "sig", "lsig", "sparams", "lsparams", "pot", "n", "mn", "ms", "mv",
                        "pl", "initcwndbps", "vprv", "xpc", "bui", "spc", "rqh", "hls_chunk_host", "mt", "sgoap", "sgovp",
                        "pcm2cms", "met", "mh", "mm", "cps", "ns", "playlist_duration", "manifest_duration", "itag",
                        "source", "requiressl", "ratebypass", "live", "goi", "sq", "dover", "pacing", "keepalive",
                        "txp", "c", "gcr", "siu", "susc", "aitags", "pfa", "playlist_type", "dur", "lmt", "clen"),
        "path_hosts": STREAM_HOSTS,
        "live_id_keys": ("video_id", "v"),
        "live_id_patterns": (r"[A-Za-z0-9_-]{11}",),
    }
    min_check_interval = 60.0
    tool_names = ("yt-dlp",)
    opt_in = True
    url_refresh_max = 4 * 3600.0

    def parse_channel(self, text: str) -> str:
        return parse_channel(text)

    def valid_channel_id(self, value: str) -> bool:
        return isinstance(value, str) and bool(CHANNEL_ID_RE.match(value))

    def allowed_hosts(self, cfg: Any) -> tuple[str, ...]:
        return STREAM_HOSTS + tuple(backend().extra_hosts())

    def request_headers(self, kind: str, credentials: dict[str, str] | None) -> dict[str, str]:
        # 쿠키는 yt-dlp 호출에만 쓴다. 재생목록·조각 요청에는 싣지 않는다
        return {"User-Agent": STREAM_UA} if kind == "stream" else {}

    def thumbnail_url(self, raw: str | None) -> str | None:
        return raw if raw and host_allowed(raw, THUMB_HOSTS) else None

    def client(self, cfg: Any) -> YouTubeClient:
        return YouTubeClient()

    def clean_credential_file(self, text: str) -> str:
        return clean_cookie_file(text)

    def credential_summary(self, values: dict[str, str]) -> dict[str, Any]:
        text = cookie_text(values)
        return cookie_summary(text) if text else {}

    def pick_credentials(self, text: str) -> tuple[dict[str, str], dict[str, Any]]:
        """지금처럼 유튜브·구글 도메인의 줄 전체(검사). 만료된 줄은 뺀다."""
        cookies = [c for c in cookiefile.parse(text) if cookiefile.on_domain(c, COOKIE_DOMAINS) and not c.expired()]
        login = [c for c in cookies if c.name in LOGIN_SID + LOGIN_SAPISID]
        if not cookies:
            raise ValueError("파일에 유튜브·구글 쿠키가 없습니다. 로그인한 상태에서 내보냈는지 확인해 주십시오")
        clean = clean_cookie_file(cookiefile.to_netscape(cookies))
        return {COOKIE_KEY: clean}, {"names": sorted({c.name for c in login}), "expires": cookiefile.earliest_expiry(login)}

    def tool_versions(self) -> dict[str, str]:
        """시작 로그·진단 요약에 남길 판(판 + 출처)."""
        mod = backend()
        mod.note_startup()
        return {"yt-dlp": mod.describe(mod.select_source())}

    def enable_blocked(self) -> str | None:
        if os.environ.get(ENV_FORBID, "").strip().lower() in ("off", "0", "false", "no"):
            return "이 서버에서는 유튜브를 켤 수 없게 해 두었습니다(환경변수)"
        return None

    def opt_in_notice(self, cfg: Any) -> dict[str, Any]:
        """판은 묶음 정보에서 읽는다(불러오거나 실행하지 않는다)."""
        from importlib import metadata

        def ver(name: str) -> str:
            try:
                return metadata.version(name)
            except metadata.PackageNotFoundError:
                return "없음"

        return {
            "title": "유튜브 사용",
            "enable_label": "유튜브 사용", "disable_label": "유튜브 끄기",
            "off_label": "유튜브가 꺼져 있습니다",
            "need_enable": "유튜브를 쓰려면 설정에서 먼저 켜 주십시오",
            "lines": [
                f"설치된 yt-dlp {ver('yt-dlp')}, yt-dlp-ejs {ver('yt-dlp-ejs')}, deno {ver('deno')}를 쓰게 됩니다. "
                "받아 둔 교체 판이 있으면 그 yt-dlp를 씁니다.",
                "방송을 확인할 때 deno가 유튜브에서 받은 스크립트를 실행합니다. deno에는 파일·네트워크·환경변수 권한을 주지 않습니다.",
                "계정 쿠키를 넣어 쓰면 유튜브가 그 계정을 제한할 수 있습니다. 녹화용으로 따로 만든 계정을 권합니다.",
                "공개 방송은 쿠키 없이 녹화됩니다.",
                "켜기 전에 도구가 실행되는지 따로 띄운 프로세스에서 점검합니다. 서버를 다시 시작하지 않아도 바로 쓸 수 있습니다.",
            ],
            "disable_confirm": "유튜브를 끕니다. 유튜브 채널의 감시를 멈추고 채널은 남겨 둡니다. 받아 둔 교체 판과 쿠키는 지우지 않습니다. 끌까요?",
            "blocked": self.enable_blocked(),
        }

    def prepare_enable(self, cfg: Any) -> None:
        """쓸 판을 고르고(교체한 판이면 그 점검 포함), 그 판과 deno가 실행되는지 따로 띄운 프로세스에서 본다."""
        why = self.enable_blocked()
        if why:
            raise ValueError(why)
        picked = vendor.choose(cfg)
        ok, reason = vendor.self_check(Path(picked["path"]) if picked["path"] else None, str(picked["version"] or ""),
                                       deno=True)
        if not ok:
            raise ValueError(f"자체 점검에 실패했습니다: {reason}")
        mod = backend()
        mod.use_source(mod.Source(picked["path"], picked["label"]))

    def startup(self, cfg: Any) -> None:
        """쓸 yt-dlp 판을 한 번 고른다. 이 서버와 그 수신기는 끝까지 이 판을 쓴다."""
        picked = vendor.choose(cfg)
        mod = backend()
        mod.use_source(mod.Source(picked["path"], picked["label"]))

    def tool_status(self, cfg: Any) -> list[dict[str, Any]]:
        return [vendor.status(cfg)]

    def doctor_checks(self) -> list[dict[str, Any]]:
        """`vod-recorder doctor`가 보일 유튜브용 도구(실행하지 않고 묶음 정보와 경로만 본다)."""
        from importlib import metadata

        fix = "이 프로그램을 다시 설치해 주십시오. 유튜브만 쓸 수 없고 나머지는 됩니다"
        out = []
        try:
            out.append({"name": "yt-dlp", "ok": True, "detail": metadata.version("yt-dlp")})
        except metadata.PackageNotFoundError:
            out.append({"name": "yt-dlp", "ok": False, "detail": "설치돼 있지 않습니다", "fix": fix})
        import sysconfig

        path = os.path.join(sysconfig.get_path("scripts"), "deno")  # ytdlp.deno_path와 같은 자리
        found = os.path.isfile(path) and os.access(path, os.X_OK)
        try:
            version = metadata.version("deno")
        except metadata.PackageNotFoundError:
            version = "판 모름"
        out.append({"name": "deno", "ok": found, "detail": f"{path} ({version})" if found else "찾지 못했습니다",
                    "fix": "" if found else fix})
        return out

    def tool_action(self, cfg: Any, name: str, action: str) -> dict[str, Any]:
        if name != vendor.MAIN or action not in ("update", "rollback"):
            raise KeyError(f"{name}/{action}")
        if not vendor.enabled():
            raise PermissionError("yt-dlp 교체 기능이 꺼져 있습니다")
        try:
            return vendor.update(cfg) if action == "update" else vendor.rollback(cfg)
        except vendor.UpdateError as e:
            raise ValueError(str(e)) from None

    # ---- 수신기 프로세스 쪽 ----
    def refresh_stream(self, http: httpx.Client, spec: dict[str, Any], credentials: dict[str, str] | None) -> str:
        """yt-dlp로 새 주소를 받는다. 방송 ID가 바뀌었으면 BroadcastChanged."""
        mod = backend()
        source = mod.Source.from_token(spec.get("ytdlp"))  # 서버가 고른 yt-dlp를 그대로 쓴다(자리)
        outcome = mod.extract(LIVE_URL.format(spec.get("channel_id")), cookie_text(credentials), source=source)
        if outcome.kind != "ok":
            raise ApiError(f"youtube: {outcome.kind}")
        info = live_info(outcome.info or {})
        if info is None:
            raise ApiError("youtube: 방송 중이 아닙니다")
        if spec.get("video_id") and info.live_id != spec["video_id"]:
            raise BroadcastChanged("youtube: 방송 ID가 바뀌었습니다")
        if not info.hls_url:
            raise ApiError("youtube: 재생 주소가 없습니다")
        return info.hls_url

    def segment_token(self, master_url: str) -> tuple[str, str] | None:
        return None


PLATFORM = YouTubePlatform()
