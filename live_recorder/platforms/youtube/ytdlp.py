"""yt-dlp를 가져다 쓰는 유일한 파일.

- yt-dlp는 처음 쓸 때 이 파일에서만 불러온다(모듈 맨 위에서 import하지 않는다). 서버·수신기 프로세스가 같다.
- 어느 폴더의 yt-dlp를 쓸지는 서버가 시작할 때 `vendor.choose`로 한 번 정하고(`use_source`), `select_source`가 그 값을 낸다.
  서버가 고른 값(`Source.token()`)을 수신기 작업에 실어 보내면 수신기는 `Source.from_token`으로 같은 것을 쓴다.
- `python -m live_recorder.platforms.youtube.ytdlp --self-check <폴더> <판>`은 받은 판의 자체 점검이다.
- 호출은 동기 함수다. 코어는 스레드에서 부른다. 한 번에 30초 제한, 동시에 2건까지.
- 쿠키는 글 그대로 메모리의 글 흐름으로 넘긴다. 파일을 만들지 않으므로 yt-dlp가 저장본을 고쳐 쓸 수 없다.
- yt-dlp가 내는 글은 우리 로그(`live_recorder.youtube`)로 받는다. 내보낼 때 가리기를 거친다.

시험은 이 파일 대신 같은 함수를 가진 가짜 모듈을 끼운다(`LIVE_RECORDER_YTDLP_BACKEND`).
"""

from __future__ import annotations

import importlib
import io
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

# 이 프로세스(서버 본체 포함)에서 yt-dlp를 불러 쓰기 전에 사용자 폴더의 플러그인을 읽지 않게 한다(이미 정했으면 그대로)
os.environ.setdefault("YTDLP_NO_PLUGINS", "1")

log = logging.getLogger("live_recorder.youtube")

TIMEOUT = 30.0  # 한 번에
MAX_CONCURRENT = 2  # 동시에
DIST = "yt-dlp"

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_lock = threading.Lock()
_module: Any = None
_js_noted = False


@dataclass(frozen=True)
class Source:
    """쓸 yt-dlp. path가 None이면 설치된 기본 판."""

    path: str | None = None
    label: str = "설치된 기본 판"

    def token(self) -> dict[str, Any]:
        """수신기 작업에 실어 보내는 값(JSON)."""
        return {"path": self.path, "label": self.label}

    @staticmethod
    def from_token(value: Any) -> "Source":
        if isinstance(value, dict) and (value.get("path") is None or isinstance(value.get("path"), str)):
            return Source(value.get("path"), str(value.get("label") or "설치된 기본 판"))
        return DEFAULT


DEFAULT = Source()
_selected = DEFAULT


def use_source(source: Source) -> None:
    """서버가 시작할 때 고른 판. 이 프로세스는 끝까지 이 판을 쓴다."""
    global _selected
    _selected = source


def select_source() -> Source:
    """어느 폴더의 yt-dlp를 쓸지 정하는 한 자리."""
    return _selected


@dataclass
class Outcome:
    """해석 결과. kind: ok | offline | login | bot | rate | notfound | timeout | error."""

    kind: str
    info: dict[str, Any] | None = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def version(source: Source | None = None) -> str:
    """판 번호. 불러오지 않고 묶음 정보에서 읽는다(시작 로그·진단 요약이 yt-dlp를 먼저 불러오지 않는다)."""
    from importlib import metadata

    source = source or select_source()
    try:
        if source.path:
            found = next(iter(metadata.distributions(name=DIST, path=[source.path])), None)
            return found.version if found else "알 수 없음"
        return metadata.version(DIST)
    except metadata.PackageNotFoundError:
        return "없음"


def describe(source: Source | None = None) -> str:
    """시작 로그·진단 요약의 "판 + 출처"."""
    source = source or select_source()
    return f"{version(source)} ({source.label})"


def extra_hosts() -> tuple[str, ...]:
    """수신기가 더 허용할 호스트. 실제 yt-dlp에는 없다(가짜 모듈이 시험 서버를 더한다)."""
    return ()


def deno_path() -> str | None:
    """설치된 deno(파이썬 묶음 `deno`가 깔린 실행 파일 폴더). PATH 탐색에 기대지 않는다."""
    import os
    import sysconfig

    path = os.path.join(sysconfig.get_path("scripts"), "deno")
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def deno_version() -> str:
    """deno 판(묶음 정보에서 읽는다. 실행하지 않는다)."""
    from importlib import metadata

    try:
        return metadata.version("deno")
    except metadata.PackageNotFoundError:
        return "없음"


def js_runtimes() -> dict[str, Any]:
    """yt-dlp에 넘기는 실행기 설정. deno 경로만 일러 주고 허용 옵션(파일·네트워크·환경변수)은 더하지 않는다.
    deno를 찾지 못하면 빈 설정으로 실행기 없이 쓴다(yt-dlp가 PATH를 뒤지지 않게)."""
    path = deno_path()
    return {"deno": {"path": path}} if path else {}


def build_opts(cookies_text: str | None = None, *, flat: bool = False) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True, "no_warnings": False, "logger": _Logger(), "skip_download": True, "noplaylist": True,
        "socket_timeout": TIMEOUT, "cachedir": False, "noprogress": True, "color": "no_color",
        "js_runtimes": js_runtimes(),
    }
    if flat:
        opts.update({"extract_flat": "in_playlist", "playlist_items": "0"})
    if cookies_text:
        opts["cookiefile"] = io.StringIO(cookies_text)  # 파일을 만들지 않는다. yt-dlp가 쓰는 것도 이 흐름에만 간다
    return opts


def note_startup() -> None:
    """deno를 찾지 못했을 때만 "실행기 없이 쓴다"는 경고를 시작할 때 한 번 남긴다."""
    global _js_noted
    if deno_path():
        return
    with _lock:
        if _js_noted:
            return
        _js_noted = True
    log.warning("yt-dlp를 자바스크립트 실행기 없이 씁니다(deno를 찾지 못했습니다). 생방송 확인에는 없어도 됩니다",
                extra={"event": "ytdlp_no_js_runtime"})


def _load(source: Source):
    """처음 쓸 때 한 번 불러온다. 교체한 판을 불러오지 못하면 기본 판으로 내려간다."""
    global _module, _selected
    with _lock:
        if _module is None:
            if source.path:
                if source.path not in sys.path:
                    sys.path.insert(0, source.path)
                try:
                    _module = importlib.import_module("yt_dlp")
                except Exception as e:  # noqa: BLE001 - 깨진 교체 판
                    sys.path[:] = [p for p in sys.path if p != source.path]
                    for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
                        del sys.modules[name]
                    log.error("교체한 yt-dlp를 불러오지 못해 기본 판으로 내려갑니다: %s", type(e).__name__,
                              extra={"event": "ytdlp_fallback"})
                    from . import vendor
                    vendor.fall_back(type(e).__name__)
                    _selected = DEFAULT
            if _module is None:
                _module = importlib.import_module("yt_dlp")
        return _module


class _Logger:
    """yt-dlp의 글을 우리 로그로 받는다. 실행기 경고는 시작할 때 한 번 남겼으므로 넘긴다."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        if "JavaScript runtime" in msg or "JS runtime" in msg or "js_runtimes" in msg:
            return
        if "ffmpeg not found" in msg:
            return  # 이 도구는 yt-dlp에게 받기·합치기를 맡기지 않는다(주소 해석만)
        log.warning("yt-dlp: %s", str(msg)[:300], extra={"event": "ytdlp_message"})

    def error(self, msg: str) -> None:
        log.info("yt-dlp: %s", str(msg)[:300], extra={"event": "ytdlp_message"})  # 결과는 Outcome으로 따로 다룬다


# 오류 문장으로 가른다. yt-dlp가 판마다 문장을 바꿀 수 있어 낱말 몇 개만 본다
_OFFLINE = ("is not currently live", "will begin in", "premieres in", "this live event will begin", "not currently live",
            "live event has ended", "no live", "is offline")
_BOT = ("confirm you're not a bot", "confirm you’re not a bot", "not a bot")
_RATE = ("http error 429", "too many requests", "try again later", "content isn't available", "content isn’t available")
_LOGIN = ("sign in to confirm your age", "members-only", "join this channel", "available to this channel's members",
          "private video", "login required", "sign in", "age-restricted", "requires payment")
_NOTFOUND = ("does not exist", "http error 404", "not found", "unable to recognize", "unsupported url")


def classify(message: str) -> str:
    text = (message or "").lower()
    for kind, words in (("bot", _BOT), ("rate", _RATE), ("offline", _OFFLINE), ("login", _LOGIN), ("notfound", _NOTFOUND)):
        if any(w in text for w in words):
            return kind
    return "error"


_FORMAT_KEYS = ("format_id", "height", "width", "fps", "tbr", "abr", "vcodec", "acodec", "audio_channels", "protocol",
                "manifest_url", "url", "ext", "filesize", "filesize_approx", "http_headers")
_INFO_KEYS = ("id", "title", "is_live", "live_status", "release_timestamp", "timestamp", "upload_date", "duration",
              "age_limit", "thumbnail", "channel_id", "channel", "uploader", "uploader_id", "_type")


def _trim(info: dict[str, Any]) -> dict[str, Any]:
    out = {k: info.get(k) for k in _INFO_KEYS}
    out["formats"] = [{k: f.get(k) for k in _FORMAT_KEYS} for f in info.get("formats") or [] if isinstance(f, dict)]
    return out


def _run(url: str, cookies_text: str | None, flat: bool, source: Source) -> Outcome:
    yt_dlp = _load(source)
    opts = build_opts(cookies_text, flat=flat)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001 - yt-dlp의 오류 형식은 판마다 다르다
        message = str(e)[:500]
        return Outcome(classify(message), None, message)
    if not isinstance(info, dict):
        return Outcome("error", None, "yt-dlp가 결과를 내지 않았습니다")
    return Outcome("ok", _trim(info))


def extract(url: str, cookies_text: str | None = None, *, flat: bool = False, source: Source | None = None) -> Outcome:
    """주소 하나를 해석한다. 30초 안에 끝나지 않으면 timeout(돌던 호출은 끝날 때까지 자리를 쥔다)."""
    source = source or select_source()
    if not _slots.acquire(timeout=TIMEOUT):
        return Outcome("timeout", None, "yt-dlp 호출이 밀려 있습니다")
    box: dict[str, Outcome] = {}

    def work() -> None:
        try:
            box["out"] = _run(url, cookies_text, flat, source)
        except Exception as e:  # noqa: BLE001
            box["out"] = Outcome("error", None, f"{type(e).__name__}: {str(e)[:300]}")
        finally:
            _slots.release()

    thread = threading.Thread(target=work, name="yt-dlp", daemon=True)
    thread.start()
    thread.join(TIMEOUT)
    if thread.is_alive():
        return Outcome("timeout", None, f"yt-dlp가 {TIMEOUT:g}초 안에 끝나지 않았습니다")
    return box.get("out") or Outcome("error", None, "yt-dlp 결과가 없습니다")


# ---- 자체 점검. 따로 띄운 프로세스에서만 돈다 ----
DENO_PROBE = "console.log(40 + 2)"


def check_deno(path: str | None) -> str | None:
    """deno가 실행되는지. yt-dlp가 띄우는 것과 같은 꼴(권한 허용 옵션 없음)로 짧은 글을 돌린다. 실패면 까닭."""
    import subprocess

    if not path:
        return "deno를 찾지 못했습니다"
    cmd = [path, "run", "--ext=js", "--no-code-cache", "--no-prompt", "--no-remote", "-"]
    try:
        proc = subprocess.run(cmd, input=DENO_PROBE, capture_output=True, text=True, timeout=30,
                              env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "NO_COLOR": "1"})
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"deno를 실행하지 못했습니다: {type(e).__name__}"
    if proc.returncode != 0 or proc.stdout.strip() != "42":
        return f"deno 실행 결과가 다릅니다(rc={proc.returncode}): {proc.stderr.strip()[-200:]}"
    return None


def _self_check(folder: str, want: str, *, deno: bool = False) -> dict[str, Any]:
    """새 폴더(`-`이면 설치된 기본 판)에서 불러와지는지, 판 문자열이 같은지, 이 파일이 쓰는 이름과 호출 꼴이 있는지
    (네트워크 요청 없음). deno면 deno가 실행되는지도 본다."""
    import os

    if deno:
        why = check_deno(deno_path())
        if why:
            return {"ok": False, "reason": why}
    if folder != "-":
        sys.path.insert(0, folder)
    try:
        yt_dlp = importlib.import_module("yt_dlp")
        from yt_dlp import version as yv  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"불러오지 못했습니다: {type(e).__name__}: {str(e)[:200]}"}
    where = os.path.realpath(getattr(yt_dlp, "__file__", "") or "")
    if folder != "-" and not where.startswith(os.path.realpath(folder) + os.sep):
        return {"ok": False, "reason": "새 폴더가 아닌 곳의 yt-dlp를 불러왔습니다"}
    got = getattr(sys.modules.get("yt_dlp.version"), "__version__", None)
    if got != want:
        return {"ok": False, "reason": f"판 문자열이 다릅니다({got})"}
    ydl_cls = getattr(yt_dlp, "YoutubeDL", None)
    if ydl_cls is None or not callable(getattr(ydl_cls, "extract_info", None)):
        return {"ok": False, "reason": "YoutubeDL.extract_info가 없습니다"}
    try:
        opts = build_opts("# Netscape HTTP Cookie File\n", flat=True)
        with ydl_cls(opts) as ydl:
            if not callable(getattr(ydl, "extract_info", None)):
                return {"ok": False, "reason": "extract_info를 부를 수 없습니다"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"YoutubeDL을 만들지 못했습니다: {type(e).__name__}: {str(e)[:200]}"}
    return {"ok": True, "reason": "", "version": got}


def main(argv: list[str]) -> int:
    import json

    deno = "--deno" in argv
    argv = [a for a in argv if a != "--deno"]
    if len(argv) == 3 and argv[0] == "--self-check":
        result = _self_check(argv[1], argv[2], deno=deno)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    print(json.dumps({"ok": False, "reason": "사용법: --self-check <폴더|-> <판> [--deno]"}, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    sys.exit(main(sys.argv[1:]))
