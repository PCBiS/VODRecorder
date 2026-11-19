"""API 안내 글을 사람이 읽는 문서 화면(HTML)으로 바꾼다.

원문은 마크다운(`api_guide.md`) 그대로다. 프로그램과 AI 도구는 원문을 받고, 브라우저는 이 화면을 받는다.
바깥 라이브러리를 쓰지 않는다. 안내 글이 쓰는 문법만 안다: 제목(#~###), 문단, 번호·점 목록(이어지는 줄 포함),
표, 코드 블록(```), 줄 안의 `코드`·**굵게**·[글](주소). 글자는 모두 HTML로 바꿔 넣는다(태그를 그대로 통과시키지 않는다).
"""

from __future__ import annotations

import html
import re

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_INLINE = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)\s]+)\)")


def _inline(text: str) -> str:
    """줄 안의 코드·굵게·링크. 나머지 글자는 그대로 HTML 글자로 바꾼다."""
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        if m.group(1) is not None:
            out.append(f"<code>{html.escape(m.group(1))}</code>")
        elif m.group(2) is not None:
            out.append(f"<strong>{html.escape(m.group(2))}</strong>")
        else:
            href = m.group(4)
            safe = href if href.startswith(("/", "https://", "http://", "#")) else "#"
            out.append(f'<a href="{html.escape(safe, quote=True)}">{html.escape(m.group(3))}</a>')
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _badge(cell: str) -> str:
    if cell in METHODS:
        return f'<span class="wiki-method m-{cell.lower()}">{cell}</span>'
    if cell in ("보기", "운영"):
        return f'<span class="wiki-scope s-{"view" if cell == "보기" else "operate"}">{cell}</span>'
    return _inline(cell)


def _table(head: list[str], rows: list[list[str]]) -> str:
    thead = "".join(f"<th>{_inline(c)}</th>" for c in head)
    body = []
    for row in rows:
        cells = []
        for index, cell in enumerate(row):
            is_path = cell.startswith("/") and " " not in cell
            cells.append(f"<td>{f'<code>{html.escape(cell)}</code>' if is_path else _badge(cell)}</td>" if index else f"<td>{_badge(cell)}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return f'<div class="wiki-table"><table><thead><tr>{thead}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _route_tables(head: list[str], rows: list[list[str]]) -> str:
    """방법·경로·권한 표는 경로의 앞 두 마디(`/api/…`)가 같은 것끼리 작은 표로 나눈다. 한 줄뿐인 묶음은 "그 밖"으로 모은다.
    어떤 경로가 있는지는 글에서만 읽는다(이 화면은 경로를 알지 못한다)."""
    groups: dict[str, list[list[str]]] = {}
    for row in rows:
        parts = (row[1] if len(row) > 1 else "").split("/")
        groups.setdefault("/".join(parts[:3]) if len(parts) > 2 else "", []).append(row)
    rest = [row for key, items in groups.items() if len(items) < 2 or not key for row in items]
    out = []
    if rest:
        out.append(("그 밖", rest, False))
    out.extend((key, items, True) for key, items in groups.items() if key and len(items) >= 2)
    parts_html = []
    for name, items, is_path in out:
        label = f"<code>{html.escape(name)}/…</code>" if is_path else html.escape(name)
        parts_html.append(f'<h4 class="wiki-group">{label} <span class="wiki-count">{len(items)}</span></h4>')
        parts_html.append(_table(head, items))
    return "".join(parts_html)


def _slug(index: int) -> str:
    return f"s{index}"


def render(markdown: str, program_name: str) -> str:
    """안내 글(마크다운)을 문서 화면 HTML로."""
    lines = markdown.splitlines()
    body: list[str] = []
    toc: list[tuple[int, str, str]] = []  # (단계, 닻, 제목)
    title = "API 안내"
    lead_done = False
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("```"):
            lang = line[3:].strip()
            block = []
            i += 1
            while i < n and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            label = f'<span class="wiki-lang">{html.escape(lang)}</span>' if lang else ""
            body.append(f'<div class="wiki-code">{label}<pre><code>{html.escape(chr(10).join(block))}</code></pre></div>')
            continue
        heading = re.match(r"(#{1,3}) (.+)", line)
        if heading:
            level, text = len(heading.group(1)), heading.group(2).strip()
            if level == 1:
                title = text
            else:
                anchor = _slug(len(toc) + 1)
                toc.append((level, anchor, text))
                body.append(f'<h{level} id="{anchor}">{_inline(text)}</h{level}>')
            i += 1
            continue
        if line.startswith("|") and i + 1 < n and re.match(r"\|[\s:|-]+\|\s*$", lines[i + 1]):
            head = _cells(line)
            i += 2
            rows = []
            while i < n and lines[i].startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            is_routes = len(head) == 3 and rows and all(r and r[0] in METHODS for r in rows)
            body.append(_route_tables(head, rows) if is_routes else _table(head, rows))
            continue
        item = re.match(r"(\d+\.|-) (.*)", line)
        if item:
            ordered = item.group(1) != "-"
            items: list[str] = []
            while i < n:
                m = re.match(r"(\d+\.|-) (.*)", lines[i])
                if m and (m.group(1) != "-") == ordered:
                    items.append(m.group(2))
                elif lines[i].startswith("   ") and lines[i].strip() and items:
                    items[-1] += "\n" + lines[i].strip()  # 목록 항목의 이어지는 줄
                else:
                    break
                i += 1
            tag = "ol" if ordered else "ul"
            body.append(f"<{tag}>" + "".join(f"<li>{'<br>'.join(_inline(p) for p in it.split(chr(10)))}</li>" for it in items) + f"</{tag}>")
            continue
        para = [line]
        i += 1
        while i < n and lines[i].strip() and not re.match(r"(#{1,3} |```|\||\d+\. |- )", lines[i]):
            para.append(lines[i])
            i += 1
        cls = "" if lead_done else ' class="wiki-lead"'
        lead_done = True
        body.append(f"<p{cls}>{_inline(' '.join(s.strip() for s in para))}</p>")

    nav = []
    for level, anchor, text in toc:
        nav.append(f'<a class="wiki-nav-{level}" href="#{anchor}">{_inline(text)}</a>')
    name = html.escape(program_name)
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="/static/style.css">
<script src="/static/theme.js"></script>
</head>
<body class="wiki-body">
<header class="topbar wiki-top">
  <a class="brand" href="/"><span class="brand-dot"></span>{name}</a>
  <span class="wiki-crumb">문서 · API 안내</span>
  <span class="wiki-links"><a class="btn small" href="/api/guide?format=md">원문(마크다운)</a>
    <a class="btn small" href="/api/openapi.json">OpenAPI</a><a class="btn small" href="/">화면으로</a></span>
</header>
<div class="wiki">
  <nav class="wiki-nav" aria-label="목차"><p class="wiki-nav-title">목차</p>{''.join(nav)}</nav>
  <main class="wiki-main"><article class="wiki-article"><h1>{_inline(title)}</h1>{''.join(body)}</article></main>
</div>
</body>
</html>
"""
