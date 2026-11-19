"""다운로드 입장 판단.

새 다운로드를 시작해도 되는지(열림), 받는 중인 작업을 멈춰야 하는지(멈춤)를 정한다.

이유의 우선순위: 전체 일시정지 → 여유 공간 부족 → 실시간 녹화 중 → 시간대 밖.
- 전체 일시정지·여유 공간 부족·녹화 중이면 새 작업을 시작하지 않고 받는 중인 작업도 멈춘다.
- 시간대 밖이면 새 작업을 시작하지 않는다. 받는 중인 작업은 설정(끝날 때 동작)에 따라 멈추거나 끝까지 받는다.

녹화 엔진은 읽기만 한다(`engine_activity`). 엔진의 공개 속성(`workers`의 `recording`·`in_broadcast`)과
저장소의 공개 조회(`open_sessions`·`unfinalized_sessions`)만 쓴다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from . import schedule

REASON_LABELS = {
    "user": "사용자 일시정지",
    "global": "전체 일시정지",
    "space": "여유 공간 부족",
    "recording": "실시간 녹화 중",
    "window": "다운로드 시간대 밖",
    "queue": "재개 대기",
}
AUTO_REASONS = ("global", "space", "recording", "window")


def engine_activity(engine: Any) -> dict[str, int]:
    """녹화 중인 채널 수와 마무리할(하고 있는) 녹화 수. 엔진 상태를 바꾸지 않는다."""
    workers = list(getattr(engine, "workers", {}).values())
    busy = sum(1 for w in workers if getattr(w, "recording", False) or getattr(w, "in_broadcast", False))
    store = getattr(engine, "store", None)
    if store is None:
        return {"recording": busy, "finalizing": 0}
    return {"recording": max(busy, len(store.open_sessions())), "finalizing": len(store.unfinalized_sessions())}


def evaluate(settings: Any, now: datetime, activity: dict[str, int] | None = None, space_low: bool = False) -> dict[str, Any]:
    """설정·시각·녹화 상태·여유 공간으로 입장 상태를 만든다. 순수 함수다(시험은 시각을 넣어 본다)."""
    activity = activity or {"recording": 0, "finalizing": 0}
    state: dict[str, Any] = {
        "open": True, "reason": None, "suspend": False,
        "paused": bool(settings.replay_paused),
        "window_enabled": bool(settings.replay_window_enabled),
        "end_action": settings.replay_window_end_action,
        "pause_while_recording": bool(settings.replay_pause_while_recording),
        "recording": int(activity.get("recording") or 0), "finalizing": int(activity.get("finalizing") or 0),
        "closes_at": None, "next_open_at": None, "error": None,
    }
    reasons: list[str] = []
    if state["paused"]:
        reasons.append("global")
    if space_low:
        reasons.append("space")
    if state["pause_while_recording"] and (state["recording"] or state["finalizing"]):
        reasons.append("recording")  # 녹화가 모두 끝나고 마무리까지 끝나야 다시 연다
    if state["window_enabled"]:
        try:
            rules = schedule.parse_rules(list(settings.replay_window_rules))
        except ValueError as exc:
            rules = []  # 저장된 규칙이 깨졌으면 열지 않는다
            state["error"] = str(exc)
        window = schedule.window_state(rules, now)
        state["closes_at"] = window.closes_at.isoformat() if window.closes_at else None
        state["next_open_at"] = window.next_open_at.isoformat() if window.next_open_at else None
        if not window.open:
            reasons.append("window")
    if reasons:
        reason = reasons[0]
        state.update(open=False, reason=reason,
                     suspend=reason != "window" or settings.replay_window_end_action == "pause")
    state["reasons"] = reasons
    state["reason_label"] = REASON_LABELS.get(state["reason"]) if state["reason"] else None
    return state


def open_state() -> dict[str, Any]:
    """판단할 수 없을 때(시험에서 관리자만 쓸 때)의 기본값: 열림."""
    return {"open": True, "reason": None, "suspend": False, "reasons": [], "reason_label": None}


def with_space(state: dict[str, Any], space_low: bool) -> dict[str, Any]:
    """관리자가 잰 여유 공간을 입장 상태에 더한다. 전체 일시정지 다음 순위다."""
    if not space_low:
        return state
    reasons = [r for r in state.get("reasons", []) if r != "space"]
    reasons.insert(1 if reasons[:1] == ["global"] else 0, "space")
    reason = reasons[0]
    return dict(state, open=False, reason=reason, suspend=True, reasons=reasons, reason_label=REASON_LABELS[reason])
