"""화면 스크립트·문서 모음. 뼈대(app.js)와 index.html의 lr-scripts에 적힌 화면별 파일,
들어 있는 확장(downloads·replay·clips)의 화면 파일을 순서대로 읽는다. 화면 문서는 세 확장을 모두 켠 것으로 그린다.

요소 ID·경어체·날짜 함수 같은 화면 시험은 나뉜 파일 전체를 본다.
"""

import re
from pathlib import Path

WEBUI = Path(__file__).resolve().parents[1] / "live_recorder" / "webui"


def extension_list():
    from live_recorder.core import extensions

    return [e for e in extensions.known() if e.name in extensions.BUILTIN]


def script_paths() -> list[Path]:
    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    listed = re.search(r'<meta id="lr-scripts" name="lr-scripts" content="([^"]*)">', html).group(1).split()
    core = [WEBUI / "app.js"] + [WEBUI / src.removeprefix("/static/") for src in listed]
    ext = [Path(e.static_dir) / name for e in extension_list() for name in e.scripts]
    return core + ext


def all_js() -> str:
    """뼈대와 화면별 파일, 확장의 화면 파일을 불러오는 순서대로 이어 붙인 것."""
    return "\n".join(p.read_text(encoding="utf-8") for p in script_paths())


def index_html(enabled=None) -> str:
    """서버가 내려 주는 화면 문서. enabled(기본: 들어 있는 확장 전부)의 조각을 자리에 넣는다."""
    from live_recorder.web.app import render_slots

    fragments: dict[str, str] = {}
    seen = set()
    for e in extension_list():
        if enabled is not None and e.name not in enabled:
            continue
        for slot, name in e.html.items():
            path = (Path(e.ui_dir) / name).resolve()
            if (slot, path) in seen:
                continue  # 서버(ExtensionHost.html_fragments)와 같이 같은 조각은 한 번만
            seen.add((slot, path))
            fragments[slot] = fragments.get(slot, "") + path.read_text(encoding="utf-8")
    return render_slots((WEBUI / "index.html").read_text(encoding="utf-8"), fragments)
