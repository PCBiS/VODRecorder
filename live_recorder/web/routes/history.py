"""경로 묶음: 기록. 다른 묶음을 부르지 않는다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from ...platforms import registry

if TYPE_CHECKING:
    from ..context import WebContext

SESSION_KEYS = ("id", "channel_id", "live_id", "title", "source", "note", "started_at", "ended_at", "end_reason",
                "end_detail", "exit_code", "bytes", "media_seconds", "finalize_status", "finalize_detail",
                "first_media_time", "last_media_time", "quality_label", "url_refreshes", "platform", "video_codec")
VIEW_SESSION_KEYS = ("id", "ch", "channel_name", "title", "source", "started_at", "ended_at", "end_reason",
                     "exit_code", "bytes", "media_seconds", "finalize_status", "first_media_time", "last_media_time",
                     "url_refreshes", "file", "timemachine", "video_codec")  # 화면이 수신 방식 표시를 감출지


def build(ctx: "WebContext") -> APIRouter:
    r = APIRouter()
    engine = ctx.engine

    async def session_rows(start: str | None, end: str | None, limit: int) -> list[dict[str, Any]]:
        """start·end(ISO)가 있으면 그 구간과 겹치는 세션, 없으면 최근 limit개. 최신이 앞."""
        if start or end:
            try:
                lo = datetime.fromisoformat(start or "1970-01-01T00:00:00+00:00").astimezone(timezone.utc)
                hi = datetime.fromisoformat(end or "2999-01-01T00:00:00+00:00").astimezone(timezone.utc)
            except ValueError:
                raise HTTPException(400, "시각 형식이 틀립니다") from None
            if hi <= lo or hi - lo > timedelta(days=40):
                raise HTTPException(400, "구간이 틀립니다(40일까지)")
            rows = await ctx.db(engine.store.sessions_between, lo.isoformat(timespec="milliseconds"),
                                hi.isoformat(timespec="milliseconds"))
            rows = list(reversed(rows[-2000:]))
        else:
            rows = list(reversed(await ctx.db(engine.store.list_sessions, None, max(1, min(limit, 200)))))
        order = {(ch.platform, ch.channel_id): (i, ch.name) for i, ch in enumerate(await ctx.db(engine.store.list_channels), 1)}
        out = []
        for row in rows:
            item = {k: row.get(k) for k in SESSION_KEYS}
            path = row["output_path"] or row["final_path"]
            item["file"] = Path(path).name if path else None
            pid = row.get("platform") or registry.DEFAULT
            index, name = order.get((pid, row["channel_id"]), (0, None))
            item["ch"], item["channel_name"] = index, name
            found = registry.find(pid)
            item["timemachine"] = bool(found and found.features.timemachine)  # 화면이 수신 방식 표시를 감출지
            # 같은 방송 묶음. 기본 플랫폼은 예전과 같은 값이고 다른 플랫폼은 앞에 플랫폼을 붙인다
            prefix = "" if pid == registry.DEFAULT else f"{pid}:"
            item["group"] = f"{prefix}{row['channel_id']}:{row['live_id']}" if row.get("live_id") else None
            out.append(item)
        return out

    @r.get("/api/view/sessions")
    async def view_sessions(start: str | None = None, end: str | None = None, limit: int = 50):
        """보기 전용 기록. 채널 ID·방송 ID(live_id)·종료·마무리 상세 문장(경로가 들어갈 수 있다)은 넣지 않는다.
        같은 방송 묶음은 이 응답 안에서만 쓰는 번호로 준다."""
        groups: dict[str, int] = {}
        out = []
        for item in await session_rows(start, end, limit):
            g = item.pop("group")
            view_item = {k: item[k] for k in VIEW_SESSION_KEYS}
            view_item["group"] = groups.setdefault(g, len(groups) + 1) if g else None
            out.append(view_item)
        return {"sessions": out, "disk": await ctx.disk_info(), "earliest": await ctx.db(engine.store.history_start)}

    @r.get("/api/sessions")
    async def session_list(limit: int = 30, start: str | None = None, end: str | None = None):
        # earliest: 이보다 앞선 주에는 기록이 있을 수 없다(설치 시각과 가장 이른 녹화 중 앞선 것)
        return {"sessions": await session_rows(start, end, limit), "disk": await ctx.disk_info(),
                "earliest": await ctx.db(engine.store.history_start)}

    return r
