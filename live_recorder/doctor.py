"""점검. `vod-recorder doctor`가 실행 환경을 보고 부족한 것과 고치는 방법을 알린다.

- 파이썬 판, ffmpeg·ffprobe(있는지, 판, 이 도구가 쓰는 구성요소), 플랫폼이 쓰는 외부 도구(플랫폼 모듈이 밝힌다),
  저장 위치의 쓰기 권한과 여유 공간, 포트
- 서버를 시작할 때는 꼭 필요한 것(ffmpeg·ffprobe, 저장 위치의 쓰기 권한)만 보고 없으면 뜨지 않는다(`startup_problems`)
- 플랫폼용 도구가 없어도 서버는 뜬다(그 플랫폼을 켤 때 그 점검이 알린다)
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_PYTHON = (3, 13)
MIN_FREE = 1024 ** 3  # 1 GiB 미만이면 알린다
# 이 도구가 쓰는 FFmpeg 구성요소(수신·마무리·다시보기 받기)
NEEDS = {
    "protocols": ("https", "tls", "tcp", "file", "pipe"),
    "demuxers": ("hls", "mov", "mpegts", "concat"),
    "muxers": ("mp4", "mpegts"),
    "bsfs": ("aac_adtstoasc",),
    "decoders": ("aac",),
}
INSTALL_FFMPEG = {
    "linux": "배포판의 꾸러미로 설치해 주십시오(예: sudo apt install ffmpeg, sudo dnf install ffmpeg). "
             "다른 곳에 두었다면 LIVE_RECORDER_FFMPEG·LIVE_RECORDER_FFPROBE에 경로를 적어 주십시오",
    "macos": "Homebrew로 설치해 주십시오(brew install ffmpeg). "
             "다른 곳에 두었다면 LIVE_RECORDER_FFMPEG·LIVE_RECORDER_FFPROBE에 경로를 적어 주십시오",
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True

    def line(self) -> str:
        mark = "정상" if self.ok else ("필요" if self.required else "선택")
        text = f"[{mark}] {self.name}: {self.detail}"
        if not self.ok and self.fix:
            text += f"\n        고치는 방법: {self.fix}"
        return text


def _system() -> str:
    from .core.locations import system_name

    return system_name()


def resolve_tool(name: str) -> str | None:
    """경로(또는 PATH의 이름)를 실행할 수 있는 파일로."""
    if os.sep in name:
        return name if os.path.isfile(name) and os.access(name, os.X_OK) else None
    return shutil.which(name)


def _run(cmd: list[str], timeout: float = 15.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "") + (out.stderr or "")


def _names(text: str, kind: str) -> set[str]:
    """`ffmpeg -protocols|-demuxers|-muxers|-bsfs|-decoders`의 출력에서 이름을 모은다."""
    out: set[str] = set()
    lines = text.splitlines()
    if kind == "protocols":
        for line in lines:
            word = line.strip()
            if word and " " not in word and not word.endswith(":"):
                out.add(word)
        return out
    if kind == "bsfs":
        return {line.strip() for line in lines if line.strip() and " " not in line.strip() and not line.strip().endswith(":")}
    past = False
    for line in lines:
        if line.strip().startswith("--") or line.strip() == "------":
            past = True
            continue
        if not past:
            continue
        parts = line.split()
        if len(parts) >= 2:
            for name in parts[1].split(","):
                out.add(name)
    return out


def check_ffmpeg(ffmpeg: str, ffprobe: str, *, features: bool = True) -> list[Check]:
    system = _system()
    fix = INSTALL_FFMPEG.get(system, INSTALL_FFMPEG["linux"])
    out: list[Check] = []
    paths = {}
    for label, name in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        path = resolve_tool(name)
        paths[label] = path
        if path is None:
            out.append(Check(label, False, f"찾지 못했습니다({name})", fix))
            continue
        version = _run([path, "-hide_banner", "-version"]).splitlines()[:1] if features else []
        out.append(Check(label, True, f"{path}" + (f" ({version[0].strip()})" if version else "")))
    if features and paths.get("ffmpeg"):
        missing = []
        for kind, names in NEEDS.items():
            have = _names(_run([paths["ffmpeg"], "-hide_banner", f"-{kind}"]), kind)
            missing += [f"{kind[:-1]} {n}" for n in names if n not in have]
        if missing:
            out.append(Check("ffmpeg 구성요소", False, "빠진 것: " + ", ".join(missing),
                             "https(OpenSSL 등)와 hls·mov·mp4·mpegts·aac를 포함해 빌드한 ffmpeg를 설치해 주십시오. " + fix))
        else:
            out.append(Check("ffmpeg 구성요소", True, "https, hls, mp4·mpegts, aac 관련 구성요소가 있습니다"))
    return out


def check_platform_tools() -> list[Check]:
    """플랫폼이 쓰는 외부 도구(예: 동영상 사이트용 도구). 플랫폼 모듈이 `doctor_checks()`로 밝힌다(선택 항목).
    없어도 서버는 뜬다. 그 플랫폼을 켤 때 그 플랫폼의 점검이 알린다."""
    from .platforms import registry

    out = []
    for platform in registry.all_platforms():
        fn = getattr(platform, "doctor_checks", None)
        if not callable(fn):
            continue
        for item in fn() or ():
            out.append(Check(f"{item['name']}({platform.name}용)", bool(item["ok"]), str(item["detail"]),
                             str(item.get("fix") or ""), required=False))
    return out


def _writable(path: Path) -> tuple[bool, str]:
    """있으면 쓸 수 있는지, 없으면 만들 수 있는 가장 가까운 부모가 쓸 수 있는지."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK | os.X_OK):
        return False, f"쓸 수 없습니다({probe})"
    return True, "있습니다" if path.exists() else "처음 시작할 때 만듭니다"


def check_storage(cfg: Any) -> list[Check]:
    out = []
    for label, path in (("자료 폴더", cfg.data_dir), ("로그 폴더", cfg.log_dir), ("녹화 폴더", cfg.record_root)):
        ok, how = _writable(Path(path))
        detail = f"{path} — {how}"
        if ok:
            probe = Path(path)
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            try:
                free = shutil.disk_usage(probe).free
                detail += f", 여유 {free / 1024 ** 3:.1f} GiB"
                if label == "녹화 폴더" and free < MIN_FREE:
                    out.append(Check(label, False, detail + "(부족)", "녹화 폴더를 여유 있는 디스크로 옮겨 주십시오", required=False))
                    continue
            except OSError:
                pass
        out.append(Check(label, ok, detail, "쓸 수 있는 폴더를 --data-dir·--log-dir·--record-root나 "
                                                 "LIVE_RECORDER_DATA_DIR·LIVE_RECORDER_LOG_DIR·LIVE_RECORDER_RECORD_ROOT로 정해 주십시오"))
    return out


def check_port(host: str, port: int) -> Check:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError as e:
            return Check("포트", False, f"{host}:{port}을(를) 쓸 수 없습니다({e.strerror or e})",
                         "다른 프로그램이 쓰고 있으면 --port나 LIVE_RECORDER_PORT로 다른 포트를 정해 주십시오", required=False)
    return Check("포트", True, f"{host}:{port}을(를) 쓸 수 있습니다", required=False)


def check_python() -> Check:
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro} ({sys.executable})"
    if (v.major, v.minor) < MIN_PYTHON:
        return Check("파이썬", False, text, f"파이썬 {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 이상으로 설치해 주십시오")
    return Check("파이썬", True, text)


def run(cfg: Any, host: str, port: int) -> tuple[list[Check], int]:
    checks = [check_python()] + check_ffmpeg(cfg.ffmpeg, cfg.ffprobe) + check_platform_tools() + check_storage(cfg)
    checks.append(check_port(host, port))
    bad = [c for c in checks if not c.ok and c.required]
    return checks, (1 if bad else 0)


def startup_problems(cfg: Any) -> list[str]:
    """서버를 시작할 때 꼭 필요한 것. 없으면 그 까닭(경어체) 목록."""
    problems = []
    for c in check_ffmpeg(cfg.ffmpeg, cfg.ffprobe, features=False) + check_storage(cfg):
        if not c.ok and c.required:
            problems.append(f"{c.name}: {c.detail}. {c.fix}".rstrip(". ") + ".")
    return problems
