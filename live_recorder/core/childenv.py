"""하위 프로세스(녹화 수신기·다운로드 작업·ffmpeg·외부 도구 점검)에 넘기는 환경변수.

부모의 환경을 통째로 넘기지 않고 필요한 것만 고른다. 관리자 비밀번호처럼 시작할 때만 쓰는 값은 넘기지 않는다.
"""

from __future__ import annotations

import os

# 그대로 넘기는 이름
PASS_NAMES = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP",
              "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "FONTCONFIG_FILE", "FONTCONFIG_PATH", "SYSTEMROOT", "PYTHONPATH")
# 이 앞붙임으로 시작하는 이름도 넘긴다
PASS_PREFIXES = ["LIVE_RECORDER_", "LC_", "SSL_", "REQUESTS_CA_", "CURL_CA_"]
# 나가는 프록시와 사설 인증서(대소문자 둘 다). 이 값을 쓰는 환경에서 하위 프로세스가 연결하지 못하는 일을 막는다
PASS_NAMES += tuple(n for base in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY") for n in (base, base.lower())) + (
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")
# 넘기지 않는 이름(시작할 때만 쓰는 값)
DROP_NAMES = ("LIVE_RECORDER_ADMIN_PASSWORD", "LIVE_RECORDER_ADMIN_USER")
# 늘 붙이는 값. 외부 도구가 사용자 폴더의 플러그인을 읽지 않게 한다
FIXED = {"YTDLP_NO_PLUGINS": "1", "PYTHONNOUSERSITE": "1"}


def child_env(pythonpath: str | None = None, source: dict[str, str] | None = None) -> dict[str, str]:
    src = os.environ if source is None else source
    out = {k: v for k, v in src.items()
           if (k in PASS_NAMES or any(k.startswith(p) for p in PASS_PREFIXES)) and k not in DROP_NAMES}
    out.update(FIXED)
    if pythonpath:
        out["PYTHONPATH"] = pythonpath + (os.pathsep + out["PYTHONPATH"] if out.get("PYTHONPATH") else "")
    return out


def forget_startup_secrets() -> None:
    """시작할 때 읽은 관리자 비밀번호를 이 프로세스의 환경에서 지운다."""
    os.environ.pop("LIVE_RECORDER_ADMIN_PASSWORD", None)
