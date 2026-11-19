"""사람이 읽는 문구의 경어체. 검사 방법과 허용 목록은 `polite_scan.py`에 적었다."""

import re

import polite_scan as ps
from webui_files import all_js


def test_detector_catches_plain_endings_and_passes_polite_ones():
    plain = ["작업이 없다", "저장한다.", "취소했다(부분 파일은 지웠다)", "다시 시도한다\n", "옮길 수 없다: 경로", "지울까?",
             "값이 비었다,", "되돌릴 수 없다!", "\"채널이다\"", "그대로다", "UID가 아니다", "경로가 아니다."]
    polite = ["작업이 없습니다", "저장합니다.", "취소했습니다(부분 파일은 지웠습니다)", "다시 시도하십시오", "지울까요?",
              "켤까요? 되돌릴 수 없습니다.", "다시보기", "다운로드", "보다 길게", "다시 받기", "확인 필요", "채널 추가",
              "녹화 중", "바다", "다음 달 >", "1~64자입니다", "되돌릴 수 없습니까?"]
    for text in plain:
        assert ps.plain_endings(text), text
    for text in polite:
        assert not ps.plain_endings(text), text


def test_scanner_reads_every_kind_of_source_and_skips_logs_comments_and_sql(tmp_path):
    py = tmp_path / "x.py"
    py.write_text('"""모듈 설명이다."""\nimport logging\nlog = logging.getLogger("a")\n'
                  'def f(n):\n    """함수 설명이다."""\n    log.info("로그 문장이다 %s", n, extra={"event": "x"})\n'
                  '    # 주석이다\n    q = "CREATE TABLE t(a) -- 표를 더한다"\n'
                  '    return f"{n}개를 지웠다", "화면 문구다"\n', encoding="utf-8")
    found = {s for _, s in ps.python_strings(py) if ps.plain_endings(s)}
    assert found == {"{}개를 지웠다", "화면 문구다"}
    js = tmp_path / "x.js"
    js.write_text('// 주석이다\nconsole.error("콘솔 문장이다", e);\ntoast(`${n}개를 넣었다`);\nconst a = "없다";\n', encoding="utf-8")
    assert {s for _, s in ps.js_strings(js) if ps.plain_endings(s)} == {"{}개를 넣었다", "없다"}
    html = tmp_path / "x.html"
    html.write_text('<!-- 주석이다 --><p>안내다. <code>cmd</code></p><input placeholder="입력한다"><button>저장</button>', encoding="utf-8")
    assert {s for _, s in ps.html_strings(html) if ps.plain_endings(s)} == {"안내다.", "입력한다"}


def test_no_plain_form_remains_in_user_facing_text():
    """화면(HTML·JS), 서버가 화면에 내려 주는 문구, 텔레그램 알림, CLI 안내에 반말 끝이 없다."""
    hits = ps.scan()
    assert hits == [], "\n".join(f"{f}:{n}: {s[:120]!r} -> {e}" for f, n, s, e in hits)


def test_questions_use_kkayo():
    js = all_js()
    asks = re.findall(r"confirm\(([^;]+)\)\) ", js)
    assert len(asks) >= 8
    for text in ("버리고 옮길까요?", "그래도 넣을까요?", "기본 화질로 받을까요?", "다시 수집할까요?", "채널을 지울까요?",
                 "모두 취소할까요?", "저장한 쿠키를 지울까요?", "완전히 지울까요?", "그래도 켤까요?"):
        assert text in js, text
