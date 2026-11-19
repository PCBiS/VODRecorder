"""다운로드 허용 시간대 규칙.

- 오류 문장은 설정 저장의 400 응답에 그대로 나간다.
- 시간대는 `Asia/Seoul`로 고정한다. 시스템에 시간대 자료가 없으면 같은 값인 고정 +09:00을 쓴다(한국은 일광 절약 시간이 없다).

규칙: `{"days": [월=0 … 일=6], "start": "HH:MM", "end": "HH:MM"}`. 끝이 시작보다 이르면 다음 날까지이고,
그 구간은 시작한 요일에 속한다(금 23:00–02:00은 토 02:00까지). 시작과 끝이 같으면 거부한다. 규칙은 7개까지다.
규칙마다 `"end_action"`(`pause`·`finish`)을 둘 수 있다: 그 규칙의 시간대가 끝날 때 받는 중인 작업을 멈출지 끝까지 받을지다.
없으면 전체 설정(시간대 끝 동작)을 따른다.
규칙마다 `"target"`(`플랫폼:채널 ID`)을 둘 수 있다: 그 채널의 작업에만 쓰는 규칙이다. 채널에 자기 규칙이 하나라도 있으면
그 채널의 작업은 자기 규칙만 따르고(시간대 사용 설정과 상관없이), 없으면 대상이 없는 규칙(전체)을 따른다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, tzinfo
from typing import Any, NamedTuple

from ...core import naming
from ...core.naming import KST

MAX_RULES = 7
_TIME = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")
_TARGET = re.compile(r"[a-z0-9_]{1,20}:[^\s:]{1,100}\Z")


def target_of(platform: object, channel: object) -> str | None:
    """작업이나 규칙의 대상 표기(`플랫폼:채널 ID`). 표기할 수 없으면 None."""
    value = f"{platform}:{channel}" if platform and channel else ""
    return value if _TARGET.fullmatch(value) else None


seoul = naming.seoul  # 한국 시각(core)


class Rule(NamedTuple):
    days: frozenset[int]  # 월=0 … 일=6
    start: int  # 자정부터 분
    end: int
    end_action: str | None = None  # 이 규칙의 시간대가 끝날 때: pause·finish. None이면 전체 설정
    target: str | None = None  # 이 규칙을 쓰는 채널(플랫폼:채널 ID). None이면 전체


class WindowState(NamedTuple):
    open: bool
    closes_at: datetime | None
    next_open_at: datetime | None


def _minutes(value: object) -> int:
    if not isinstance(value, str):
        raise ValueError("시각은 HH:MM 형식이어야 합니다")
    match = _TIME.fullmatch(value)
    if not match:
        raise ValueError("시각은 HH:MM 형식이어야 합니다")
    return int(match.group(1)) * 60 + int(match.group(2))


def parse_rules(value: object) -> list[Rule]:
    """JSON 문자열이나 풀어 놓은 목록을 검사한다."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("규칙이 JSON이 아닙니다") from exc
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        raise ValueError("규칙은 목록이어야 합니다")
    if len(value) > MAX_RULES:
        raise ValueError(f"규칙은 {MAX_RULES}개까지입니다")
    rules: list[Rule] = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"days", "start", "end", "end_action", "target"}:
            raise ValueError("규칙에는 days·start·end·end_action·target만 있어야 합니다")
        target = item.get("target") or None
        if target is not None and not (isinstance(target, str) and _TARGET.fullmatch(target)):
            raise ValueError("규칙의 대상은 플랫폼:채널 ID입니다")
        action = item.get("end_action")
        if action not in (None, "pause", "finish"):
            raise ValueError("규칙의 끝 동작은 pause나 finish입니다")
        days = item.get("days")
        if (not isinstance(days, (list, tuple)) or not days
                or any(type(day) is not int or not 0 <= day <= 6 for day in days)):
            raise ValueError("요일은 0~6 가운데 하나 이상이어야 합니다")
        start, end = _minutes(item.get("start")), _minutes(item.get("end"))
        if start == end:
            raise ValueError("시작과 끝 시각이 같을 수 없습니다")
        rules.append(Rule(frozenset(days), start, end, action, target))
    return rules


def rules_to_json(rules: list[Rule]) -> list[dict[str, Any]]:
    return [{"days": sorted(rule.days), "start": f"{rule.start // 60:02d}:{rule.start % 60:02d}",
             "end": f"{rule.end // 60:02d}:{rule.end % 60:02d}",
             **({"end_action": rule.end_action} if rule.end_action else {}),
             **({"target": rule.target} if rule.target else {})} for rule in rules]


def rules_for(rules: list[Rule], target: str | None) -> tuple[list[Rule], bool]:
    """target(작업의 채널)에 쓸 규칙과, 그것이 그 채널의 자기 규칙인지. 자기 규칙이 없으면 대상이 없는 규칙(전체)이다."""
    own = [rule for rule in rules if target and rule.target == target]
    if own:
        return own, True
    return [rule for rule in rules if not rule.target], False


def _action_at(rules: list[Rule], local: datetime, edge: datetime) -> str | None:
    """edge(벽시계 시각)에 끝나는 규칙의 끝 동작. 여러 규칙이 같은 시각에 끝나면 멈춤(pause)이 이긴다."""
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    actions = set()
    for offset in range(-9, 9):
        day = midnight + timedelta(days=offset)
        for rule in rules:
            if day.weekday() not in rule.days:
                continue
            finish = day + timedelta(minutes=rule.end) + (timedelta(days=1) if rule.end < rule.start else timedelta())
            if finish == edge:
                actions.add(rule.end_action)
    if "pause" in actions:
        return "pause"
    if None in actions:
        return None
    return "finish" if "finish" in actions else None


def _intervals(rules: list[Rule], day_start: datetime) -> list[tuple[datetime, datetime]]:
    """day_start 날짜에 시작하는 규칙의 열린 구간(벽시계 시각)."""
    result = []
    for rule in rules:
        if day_start.weekday() not in rule.days:
            continue
        begin = day_start + timedelta(minutes=rule.start)
        finish = day_start + timedelta(minutes=rule.end)
        if rule.end < rule.start:  # 자정을 넘는다: 시작한 요일에 속한다
            finish += timedelta(days=1)
        result.append((begin, finish))
    return result


def _merged(rules: list[Rule], now: datetime) -> list[tuple[datetime, datetime]]:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    spans: list[tuple[datetime, datetime]] = []
    for offset in range(-8, 9):  # 지난 구간도 본다(마지막으로 끝난 구간의 끝 동작을 알려고)
        spans.extend(_intervals(rules, midnight + timedelta(days=offset)))
    spans.sort()
    merged: list[tuple[datetime, datetime]] = []
    for begin, finish in spans:
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    return merged


def window_state(rules: list[Rule], now: datetime, zone: tzinfo | None = None) -> WindowState:
    """now 시각에 규칙을 따져 본다. 규칙이 없으면 늘 닫혀 있다."""
    zone = zone or seoul()
    local = now.astimezone(zone)
    wall = local.replace(tzinfo=None)

    def aware(value: datetime) -> datetime:
        return value.replace(tzinfo=zone)

    for begin, finish in _merged(rules, local):
        if begin <= wall < finish:
            return WindowState(True, aware(finish), None)
        if begin > wall:
            return WindowState(False, None, aware(begin))
    return WindowState(False, None, None)


def end_action(rules: list[Rule], now: datetime, zone: tzinfo | None = None) -> str | None:
    """now 시각에 따를 끝 동작. 열려 있으면 지금 구간을 끝낼 규칙의 것, 닫혀 있으면 마지막으로 끝난 규칙의 것.
    그 규칙에 끝 동작이 없거나 끝난 구간이 없으면 None(전체 설정을 따른다)."""
    local = now.astimezone(zone or seoul())
    wall = local.replace(tzinfo=None)
    last_end: datetime | None = None
    for begin, finish in _merged(rules, local):
        if begin <= wall < finish:
            return _action_at(rules, local, finish)
        if begin > wall:
            break
        last_end = finish
    return _action_at(rules, local, last_end) if last_end else None
