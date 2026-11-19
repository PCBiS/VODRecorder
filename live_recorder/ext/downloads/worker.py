"""다시보기 수신 프로세스. 관리자(manager)가 작업마다 따로 띄운다.

    python -m live_recorder.ext.downloads.worker --operation analyze|download --work-dir DIR < task.json

- 작업은 표준 입력의 JSON으로 받는다. 쿠키는 여기에만 있다. 인자·환경변수에 싣지 않는다.
- 표준 출력에 한 줄에 JSON 하나씩 이벤트를 낸다: stage, progress, result, error.
  오류는 정해진 코드만 낸다. 도구의 진단 문장이나 주소는 내보내지 않는다.
- 관리자는 표준 오류를 버린다. 그래도 이 프로세스는 표준 오류에 아무것도 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import sys
import threading
from pathlib import Path

import importlib

from . import hls_transport, media
from .errors import ReplayFailure, classify_exception, safe_code


_EMIT = threading.Lock()  # 여러 연결(스레드)이 함께 알린다. 줄이 섞이지 않게 한다


def emit(event: str, **data) -> None:
    data["event"] = event
    line = json.dumps(data, ensure_ascii=False) + "\n"
    with _EMIT:
        sys.stdout.write(line)
        sys.stdout.flush()


def _parent_death() -> None:
    from ...core.procutil import die_with_parent

    die_with_parent()  # 리눅스는 PR_SET_PDEATHSIG, macOS는 부모 번호를 보는 스레드


def handler(name: str | None):
    """작업 종류의 분석 모듈(downloads 공개 접점으로 등록한 것). 이 패키지 밖의 이름은 받지 않는다."""
    if not isinstance(name, str) or not name.startswith("live_recorder.ext."):
        raise ReplayFailure("extension_disabled")
    return importlib.import_module(name)


def _api_args(task: dict) -> tuple:
    return (task.get("cookies"),) + ((task["api_base"],) if task.get("api_base") else ())


def _context(value) -> dict:
    """작업 종류가 넘긴 값(JobKind.context, 예: 서버가 고른 도구 판·설정). 있을 때만 handler에 준다."""
    return {"context": value} if isinstance(value, dict) and value else {}


def run_analyze(task: dict) -> dict:
    items = []
    handlers = task.get("handlers") or {}
    for url in task["urls"][: media.MAX_ITEMS]:
        module = None
        try:
            module = handler(handlers.get(url))
            items.append(module.analyze(url, *_api_args(task), **_context((task.get("contexts") or {}).get(url))).public())
        except Exception as e:  # noqa: BLE001 - 항목 하나의 실패가 나머지를 막지 않는다
            parsed = None
            if module is not None:
                try:
                    parsed = module.parse(url)
                except ReplayFailure:
                    parsed = None
            parsed = parsed or {"url": url[:200], "platform": "", "kind": "", "video_id": ""}
            items.append(dict(parsed, error_code=safe_code(classify_exception(e))))
    return {"items": items}


def run_download(task: dict, work: Path) -> dict:
    emit("stage", stage="analyzing")
    module = handler(task.get("handler"))
    extra = _context(task.get("context"))
    video = module.analyze(task["url"], *_api_args(task), **extra)  # 새 주소를 받는다
    section = media.parse_section(task.get("section"))
    quality = task.get("quality")
    if quality is None:
        # 선택 다운로드에서 분석에 실패해 남긴 작업을 다시 시도할 때: 화질은 규칙으로 고른다
        chosen = media.select_by_rule(media.sort_variants(video.variants), task.get("quality_rule") or "best")
        if chosen is None:
            raise ReplayFailure("not_ready")
        quality = chosen.id
        public = video.public()
        emit("meta", quality=quality, quality_label=next((q["label"] for q in public["qualities"] if q["id"] == quality), ""),
             filename_hint=module.filename(public), title=public["title"])
    out = work / "video.mp4"
    last = {"p": -1.0}

    def progress(value: float | None) -> None:
        if value is not None and value - last["p"] >= 0.5:
            last["p"] = value
            emit("progress", progress=round(value, 1))

    def received(amount: int, total: int | None) -> None:  # 받은 양과 전체 크기
        emit("progress", bytes=int(amount), total=int(total) if total else None)

    def event(name: str, **data) -> None:  # 이어받기·처음부터 받기
        emit(name, **data)

    media.download(video, quality, section, str(out), task.get("ffmpeg") or "ffmpeg",
                 int(task.get("connections") or 16), progress, lambda stage: emit("stage", stage=stage),
                 on_event=event, on_bytes=received,
                 reanalyze=lambda: module.analyze(task["url"], *_api_args(task), **extra))
    return {"name": out.name}


def main() -> int:
    os.umask(0o077)  # 받은 영상과 ffmpeg 결과는 소유자 전용
    _parent_death()
    ap = argparse.ArgumentParser()
    ap.add_argument("--operation", choices=("analyze", "download"), required=True)
    ap.add_argument("--work-dir", required=True)
    args = ap.parse_args()
    try:
        task = json.loads(sys.stdin.read())
        if not isinstance(task, dict):
            raise ValueError()
        if task.get("allow_http"):
            media.ALLOW_LOCAL_HTTP = True
            hls_transport.ALLOW_LOCAL_HTTP = True
        work = Path(args.work_dir)
        result = run_analyze(task) if args.operation == "analyze" else run_download(task, work)
        emit("result", data=result)
        return 0
    except Exception as e:  # noqa: BLE001
        emit("error", code=safe_code(classify_exception(e)))
        return 1


if __name__ == "__main__":
    sys.stderr = open(os.devnull, "w")  # noqa: SIM115 - 진단 문장이 새어 나가지 않게 한다
    sys.exit(main())
