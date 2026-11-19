"""패키징 점검. 이미지는 소스를 root 소유로 넣고 uid 10001로 실행한다(0600 파일 때문에 기동하지 못하는 일을 막는다)."""

import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_package_files_are_world_readable():
    """패키지의 모든 파일은 다른 사용자도 읽을 수 있고, 모든 폴더는 들어갈 수 있다."""
    bad = []
    for path in [ROOT / "live_recorder", *(ROOT / "live_recorder").rglob("*"), ROOT / "requirements.txt"]:
        if "__pycache__" in path.parts:
            continue
        mode = path.stat().st_mode
        need = stat.S_IROTH | (stat.S_IXOTH if path.is_dir() else 0)
        if mode & need != need:
            bad.append(f"{oct(mode & 0o777)} {path.relative_to(ROOT)}")
    assert bad == [], "다른 사용자가 읽지 못하는 파일: " + ", ".join(bad)


def test_dockerfile_normalizes_app_permissions():
    text = (ROOT / "Dockerfile").read_text()
    copy = text.index("COPY live_recorder /app/live_recorder")
    fix = text.index("RUN chmod -R u=rwX,go=rX /app")
    user = text.index("USER 10001:10001")
    assert copy < fix < user and not any(line.startswith("COPY --chmod") for line in text.splitlines())
