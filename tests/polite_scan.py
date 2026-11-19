"""경어체 검사 도구. 사람이 읽는 문구에서 반말(한다체) 문장 끝을 찾는다.

찾는 곳
- 파이썬(`live_recorder/`): 문자열 상수와 f-문자열. 빼는 것: 독스트링, 로그 호출(`log.info(...)` 등)의 인자와 `extra`,
  `logging.getLogger(...)`의 이름, 주석(파서가 읽지 않는다), SQL 문(안의 `--` 주석 포함).
- 자바스크립트(`webui/*.js`): 따옴표·백틱 문자열. 빼는 것: 주석, `console.*(...)`의 인자(개발자용 콘솔).
- HTML(`webui/*.html`): 글자 마디와 `placeholder`·`title`·`aria-label`·`alt` 속성 값. 주석은 뺀다.

반말 판정
- 한글 낱말이 `다`로 끝나고 바로 뒤가 문장 끝(문자열 끝·줄바꿈, `.`·`!`·`?`·`…`·`:`·`,`·괄호·따옴표)이면 반말이다.
  다만 받침 ㅂ 뒤의 `니다`(합니다·습니다·입니다·됩니다)로 끝나면 경어체라서 아니다. `아니다`는 반말로 본다.
- 물음은 `까?`·`나?`·`니?`로 끝나는데 앞이 `요`·`니`(습니까)가 아니면 반말이다.
- 허용 목록(ALLOW): 문장이 아닌 이름표·낱말 가운데 `다`로 끝나는 것. 지금은 `바다`·`가나다`처럼 우연히 걸리는 낱말만 둔다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "live_recorder"
WEBUI = PKG / "webui"

LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
ALLOW = {"바다", "가나다"}
_END = r"(?=\s*(?:[.!?…:,()\]」』\"'`]|$)|\s+\[)"
PLAIN = re.compile(r"([가-힣]*)다" + _END, re.M)


def _polite_word(word: str) -> bool:
    """`ㅂ니다`(합니다·습니다·입니다·됩니다·깁니다…)로 끝나면 경어체다. `아니다`처럼 받침 ㅂ이 없는 `니다`는 반말이다."""
    if not word.endswith("니다") or len(word) < 3:
        return False
    code = ord(word[-3]) - 0xAC00
    return 0 <= code < 11172 and code % 28 == 17  # 받침 ㅂ
ASK = re.compile(r"([가-힣]*)(?<![요니])[까나니]\?", re.M)


def plain_endings(text: str) -> list[str]:
    hits = []
    for m in PLAIN.finditer(text):
        word = m.group(0)
        if word in ALLOW or _polite_word(word):
            continue
        hits.append(word)
    for m in ASK.finditer(text):
        hits.append(m.group(0))
    return hits


# ---- 파이썬 ----
def _log_call(node: ast.Call) -> bool:
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr in LOG_METHODS:
        target = ast.unparse(f.value)
        return "log" in target.lower() or target.endswith("logger")
    if isinstance(f, ast.Attribute) and f.attr == "getLogger":
        return True
    return False


def _docstring_nodes(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def _skip_nodes(tree: ast.AST) -> set[int]:
    skip = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _log_call(node):
            for sub in ast.walk(node):
                skip.add(id(sub))
    return skip


def python_strings(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _skip_nodes(tree)
    out = []
    joined_parts = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr) and id(node) not in skip:
            text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            for v in node.values:
                joined_parts.add(id(v))
            out.append((node.lineno, text))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
                and id(node) not in joined_parts):
            out.append((node.lineno, node.value))
    # SQL 문(주석 포함)은 사람이 읽는 문구가 아니다
    return [(n, s) for n, s in out if re.search(r"[가-힣]", s) and not re.match(r"\s*(PRAGMA|CREATE|SELECT|INSERT|UPDATE|DELETE)\b", s)]


# ---- 자바스크립트 ----
_JS_TOKEN = re.compile(r"//[^\n]*|/\*.*?\*/|`(?:\\.|[^`\\])*`|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'", re.S)


def js_strings(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    # console.*(...) 안은 뺀다(개발자용)
    masked = re.sub(r"console\.\w+\((?:[^()]|\([^()]*\))*\)", lambda m: " " * len(m.group(0)), src)
    out = []
    for m in _JS_TOKEN.finditer(masked):
        tok = m.group(0)
        if tok.startswith("//") or tok.startswith("/*"):
            continue
        body = tok[1:-1]
        if tok[0] == "`":
            body = re.sub(r"\$\{(?:[^{}]|\{[^{}]*\})*\}", "{}", body)
        if re.search(r"[가-힣]", body):
            out.append((src.count("\n", 0, m.start()) + 1, body))
    return out


# ---- HTML ----
def html_strings(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"<!--.*?-->", lambda m: " " * len(m.group(0)), src, flags=re.S)
    out = []
    for m in re.finditer(r'(?:placeholder|title|aria-label|alt)="([^"]*)"', src):
        if re.search(r"[가-힣]", m.group(1)):
            out.append((src.count("\n", 0, m.start()) + 1, m.group(1)))
    # 글자 마디: 태그를 빈칸으로 바꾸고 블록 요소 경계에서 나눈다
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", src, flags=re.S)
    for block in re.split(r"<(?:/?(?:p|div|li|h\d|label|button|option|section|form|span|td|th|legend|dialog|main|header|nav|code|strong|a)\b)[^>]*>", text):
        plain = re.sub(r"<[^>]+>", " ", block)
        plain = re.sub(r"\s+", " ", plain).strip()
        if re.search(r"[가-힣]", plain):
            out.append((0, plain))
    return out


def scan() -> list[tuple[str, int, str, list[str]]]:
    """(파일, 줄, 문자열, 걸린 끝) 목록."""
    hits = []
    for path in sorted(PKG.rglob("*.py")):
        for line, s in python_strings(path):
            found = plain_endings(s)
            if found:
                hits.append((str(path.relative_to(ROOT)), line, s, found))
    for path in sorted(WEBUI.rglob("*.js")) + sorted((PKG / "ext").rglob("static/*.js")):  # 화면별 파일과 확장의 화면 파일
        for line, s in js_strings(path):
            found = plain_endings(s)
            if found:
                hits.append((str(path.relative_to(ROOT)), line, s, found))
    for path in sorted(WEBUI.glob("*.html")) + sorted((PKG / "ext").rglob("ui/*.html")):  # 확장의 HTML 조각까지
        for line, s in html_strings(path):
            found = plain_endings(s)
            if found:
                hits.append((str(path.relative_to(ROOT)), line, s, found))
    return hits


if __name__ == "__main__":
    import collections

    res = scan()
    by = collections.Counter(h[0] for h in res)
    for h in res:
        print(f"{h[0]}:{h[1]}: {h[2][:160]!r} -> {h[3]}")
    print(by, len(res))
