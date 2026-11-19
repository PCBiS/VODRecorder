"""채널 다시보기 목록 수집과 년·월 보기의 공통 부분. 플랫폼을 모른다.

목록을 모으는 일은 "목록 공급자"(`api.ListProvider`, 확장이 등록한다)가 한다. 공급자는 한 쪽을 받아(`fetch`) 영상 줄과
다음 쪽 표시를 돌려줄(`parse`) 뿐이다. 모으기 일정·재시도·저장·년월 조회·고르기는 여기서 한다.

- 쪽과 쪽 사이에 3~5초를 쉬고, 429·5xx·연결 실패는 30·60·120초 뒤 다시 부른다. 403은 바로 실패다.
- 실패·취소해도 받은 줄은 남긴다. 전체 수집이 끝까지 성공했을 때에만 이번에 보이지 않은 영상을 지운다.
- 받은 파일·받은 기록(downloads 표)·작업은 건드리지 않는다.
- 저장은 작업 저장소의 catalog_channels·catalog_videos·replay_channels·replay_channels_removed 표(열 `platform`으로 가른다).

수집은 다운로드 대기열과 따로 돌고 일시정지·시간대·녹화 중 멈춤에 걸리지 않는다. 한 번에 한 채널만 수집한다.
저장소 연결은 다운로드 관리자와 같은 것을 쓴다. 모든 접근은 이벤트 루프 스레드에서 하고, 트랜잭션 안에서 기다리지 않는다.
"""

from __future__ import annotations

import asyncio
import random
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ...platforms.base import clean_text

PAGE_PAUSE = 3.0  # 쪽 사이 기본 쉬는 시간
PAGE_JITTER = 2.0  # 여기에 0~2초를 무작위로 더한다
BACKOFF = (30.0, 60.0, 120.0)  # 429·5xx·연결 실패 뒤 다시 부르기 전 쉬는 시간. 세 번 다 안 되면 실패
AUTO_LATEST_AFTER = timedelta(minutes=30)
SECONDS_PER_PAGE = 4  # 전체 다시 수집 확인 문구의 어림값
MAX_CHANNELS = 50
MAX_PAGES = 5000  # 응답이 이상해 끝없이 도는 일을 막는다
SEARCH_LIMIT = 200
MONTH_LIMIT = 3000
NO_MONTH = "none"  # 게시 날짜가 빠진 줄을 모은 자리
ACTIVE = ("queued", "running")
STATUS_LABELS = {"queued": "차례 기다림", "running": "수집 중", "done": "수집 완료", "failed": "실패",
                 "cancelled": "취소함", "interrupted": "중단"}
RESTART_ERROR = "서버가 다시 시작돼 수집이 멈췄습니다. 화면에서 다시 시작하십시오"
STOP_ERROR = "다시보기 설정이 바뀌거나 서버가 멈춰 수집을 멈췄습니다. 화면에서 다시 시작하십시오"
CANCEL_ERROR = "사용자가 취소했습니다"
COLUMNS = ("video", "title", "video_type", "publish_date", "month", "duration", "adult", "thumbnail")


class FetchError(Exception):
    """연결 실패·시간 초과. 다시 부를 수 있다."""


class CollectFailed(Exception):
    pass


class ListError(Exception):
    """쪽 전체를 읽을 수 없다. 문장에 응답 본문을 넣지 않는다. 공급자가 낸다."""


class ChannelMissing(ListError):
    """없는 채널."""


class ChannelError(Exception):
    """다시보기 채널 추가·지우기 거부. status는 HTTP 응답 코드로 쓴다."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def now() -> datetime:
    return datetime.now(timezone.utc)


Providers = Callable[[], dict[str, Any]]


class Collector:
    """공급자(플랫폼 ID → api.ListProvider)를 받아 목록을 모은다. 채널은 (플랫폼, 채널 ID)다.
    `platform`을 주지 않는 호출은 `default` 플랫폼이다(예전 호출 꼴 그대로)."""

    def __init__(self, db, providers: Providers | dict[str, Any], *, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 jitter: Callable[[], float] | None = None, clock: Callable[[], datetime] = now,
                 aclose: Callable[[], Awaitable[None]] | None = None, default: str | None = None,
                 gate: Callable[[str], Any] | None = None):
        self.db = db
        self._providers = providers if callable(providers) else (lambda: providers)
        self.sleep = sleep
        self.jitter = jitter or (lambda: random.uniform(0.0, PAGE_JITTER))
        self.clock = clock
        self.aclose = aclose
        self.default = default
        self.gate = gate  # 플랫폼 ID → 요청 창구(없으면 None)
        self.queue: deque[tuple[str, str]] = deque()
        self.wake = asyncio.Event()
        self.loop_task: asyncio.Task | None = None
        self.current: tuple[tuple[str, str], asyncio.Task] | None = None
        self.cancel_reason: str | None = None
        self.stopping = False
        with self.db:
            # 재시작 때 수집 중이던 것은 중단으로 남기고 스스로 다시 시작하지 않는다
            self.db.execute("UPDATE catalog_channels SET status='interrupted', error=?, finished_at=? WHERE status IN ('queued','running')",
                            (RESTART_ERROR, self._now()))

    # ---- 공급자 ----
    def providers(self) -> dict[str, Any]:
        return dict(self._providers())

    def provider(self, platform: str | None = None):
        pid = platform or self.default
        found = self.providers().get(pid or "")
        if found is None:
            raise ChannelError(404, "목록을 볼 수 없는 플랫폼입니다")
        return found

    def _pid(self, platform: str | None) -> str:
        return platform or self.default or ""

    async def _gated(self, platform: str, fn, *args):
        gate = self.gate(platform) if self.gate else None
        if gate is None:
            return await fn(*args)
        async with gate.slot(2):  # core.gate.EXTRA. 자기 간격은 수집기가 지킨다
            return await fn(*args)

    async def _sleep(self, platform: str, seconds: float) -> None:
        override = getattr(self.providers().get(platform), "sleep", None)
        await (override or self.sleep)(seconds)

    def _jitter(self, platform: str) -> float:
        override = getattr(self.providers().get(platform), "jitter", None)
        return float((override or self.jitter)())

    def _now(self) -> str:
        return self.clock().isoformat()

    # ---- 상태 ----
    def _row(self, channel: str, platform: str | None = None):
        return self.db.execute("SELECT * FROM catalog_channels WHERE platform=? AND channel=?", (self._pid(platform), channel)).fetchone()

    def summary(self, channel: str, platform: str | None = None) -> dict[str, Any]:
        """영상 수, 마지막 수집 시각, 수집 상태."""
        pid = self._pid(platform)
        row = self._row(channel, pid)
        count = self.db.execute("SELECT COUNT(*) FROM catalog_videos WHERE platform=? AND channel=?", (pid, channel)).fetchone()[0]
        if row is None:
            return {"count": count, "status": None, "status_label": "수집 전", "mode": None, "pages_done": 0, "total_pages": None,
                    "fetched": 0, "error": None, "started_at": None, "finished_at": None, "checked_at": None, "full_at": None,
                    "estimate_seconds": None}
        status = row["status"]
        pages = row["total_pages"]
        return {"count": count, "status": status, "status_label": STATUS_LABELS.get(status, status), "mode": row["mode"],
                "pages_done": row["pages_done"], "total_pages": pages, "fetched": row["fetched"], "error": row["error"],
                "started_at": row["started_at"], "finished_at": row["finished_at"], "checked_at": row["checked_at"],
                "full_at": row["full_at"], "estimate_seconds": pages * SECONDS_PER_PAGE if pages else None}

    # ---- 다시보기 채널 ----
    def channels(self, platform: str | None = None, *, every: bool = False) -> list[dict[str, Any]]:
        """다시보기 채널. every면 모든 플랫폼(공급자가 있는 것만), 아니면 그 플랫폼."""
        if every:
            known = self.providers()
            rows = self.db.execute("SELECT platform, channel, name, added_at FROM replay_channels ORDER BY added_at, channel").fetchall()
            return [{"platform": r["platform"], "channel_id": r["channel"], "name": r["name"], "added_at": r["added_at"]}
                    for r in rows if r["platform"] in known]
        rows = self.db.execute("SELECT channel, name, added_at FROM replay_channels WHERE platform=? ORDER BY added_at, channel",
                               (self._pid(platform),)).fetchall()
        return [{"channel_id": r["channel"], "name": r["name"], "added_at": r["added_at"]} for r in rows]

    def has_channel(self, channel: str, platform: str | None = None) -> bool:
        return self.db.execute("SELECT 1 FROM replay_channels WHERE platform=? AND channel=?",
                               (self._pid(platform), channel)).fetchone() is not None

    def _count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM replay_channels").fetchone()[0]

    def _insert(self, channel: str, name: str, platform: str | None = None) -> bool:
        pid = self._pid(platform)
        if self.has_channel(channel, pid) or self._count() >= MAX_CHANNELS:
            return False
        with self.db:
            self.db.execute("INSERT INTO replay_channels(platform,channel,name,added_at) VALUES(?,?,?,?)",
                            (pid, channel, clean_text(name), self._now()))
            self.db.execute("DELETE FROM replay_channels_removed WHERE platform=? AND channel=?", (pid, channel))
        return True

    async def lookup(self, channel: str, platform: str | None = None) -> str:
        """채널 정보를 한 번 불러 이름을 받는다. 없는 채널이면 404, 이름을 받지 못했으면 502."""
        pid = self._pid(platform)
        provider = self.provider(pid)
        if provider.lookup is None:
            raise ChannelError(503, "채널 정보를 받을 수 없습니다")
        try:
            return await self._gated(pid, provider.lookup, channel)
        except ChannelMissing:
            raise ChannelError(404, f"{provider.name}에 없는 채널입니다. 주소나 ID를 확인하십시오") from None
        except (FetchError, CollectFailed, ListError):
            raise ChannelError(502, f"{provider.name}에서 채널 이름을 받지 못해 추가하지 않았습니다. 잠시 뒤 다시 하십시오") from None

    async def add_channel(self, channel: str, platform: str | None = None, name: str | None = None) -> dict[str, Any]:
        """화면에서 추가한다. 이미 있으면 409, 50개를 넘으면 409. name을 주면(조회로 이미 받았으면) 다시 묻지 않는다."""
        pid = self._pid(platform)
        if self.has_channel(channel, pid):
            raise ChannelError(409, "이미 있는 다시보기 채널입니다")
        if self._count() >= MAX_CHANNELS:
            raise ChannelError(409, f"다시보기 채널은 {MAX_CHANNELS}개까지입니다")
        name = name or await self.lookup(channel, pid)
        if not self._insert(channel, name, pid):  # 이름을 받는 동안 다른 요청이 넣었다
            raise ChannelError(409, "이미 있거나 개수 제한에 걸렸습니다")
        return {"channel_id": channel, "name": name}

    async def remove_channel(self, channel: str, platform: str | None = None) -> None:
        """수집 중이면 먼저 취소하고 수집 목록과 함께 지운다. 녹화 채널이라도 스스로 다시 넣지 않게 기억한다."""
        pid = self._pid(platform)
        if not self.has_channel(channel, pid):
            raise ChannelError(404, "다시보기 채널이 아닙니다")
        await self.forget(channel, pid)
        with self.db:
            self.db.execute("DELETE FROM replay_channels WHERE platform=? AND channel=?", (pid, channel))
            self.db.execute("INSERT OR REPLACE INTO replay_channels_removed(platform,channel,removed_at) VALUES(?,?,?)",
                            (pid, channel, self._now()))

    def add_recording(self, channel: str, name: str, platform: str | None = None) -> bool:
        """녹화 채널을 추가할 때. 전에 지웠던 채널이어도 넣는다(녹화 채널을 다시 추가한 것이다)."""
        pid = self._pid(platform)
        with self.db:
            self.db.execute("DELETE FROM replay_channels_removed WHERE platform=? AND channel=?", (pid, channel))
        return self._insert(channel, name, pid)

    def recording_deleted(self, channel: str, platform: str | None = None) -> None:
        """녹화 채널을 지우면 '지운 채널' 기억을 풀어, 다시 추가할 때 넣게 한다. 수집 목록은 건드리지 않는다."""
        with self.db:
            self.db.execute("DELETE FROM replay_channels_removed WHERE platform=? AND channel=?", (self._pid(platform), channel))

    def sync_recording(self, recording: list[tuple[str, str]], platform: str | None = None) -> list[str]:
        """녹화 채널 가운데 다시보기 채널에 없고 관리자가 지운 적도 없는 것을 넣는다."""
        pid = self._pid(platform)
        removed = {r[0] for r in self.db.execute("SELECT channel FROM replay_channels_removed WHERE platform=?", (pid,))}
        added = []
        for channel, name in recording:
            if channel not in removed and not self.has_channel(channel, pid) and self._insert(channel, name or channel, pid):
                added.append(channel)
        return added

    # ---- 요청 ----
    def request(self, channel: str, mode: str, platform: str | None = None) -> bool:
        """수집을 차례에 넣는다. 이미 수집 중이거나 차례를 기다리면 넣지 않고 거짓."""
        if mode not in ("full", "latest"):
            raise ValueError("mode")
        pid = self._pid(platform)
        row = self._row(channel, pid)
        if row is not None and row["status"] in ACTIVE:
            return False
        with self.db:
            self.db.execute("INSERT INTO catalog_channels(platform,channel,status,mode) VALUES(?,?,?,?) "
                            "ON CONFLICT(platform,channel) DO UPDATE SET status=excluded.status, mode=excluded.mode, error=NULL",
                            (pid, channel, "queued", mode))
        self.queue.append((pid, channel))
        self.wake.set()
        return True

    def auto(self, channel: str, platform: str | None = None) -> str | None:
        """처음 여는 채널은 전체 수집, 지난 확인이 성공했고 30분이 지났으면 새 영상 확인. 시작한 방식을 돌려준다."""
        pid = self._pid(platform)
        row = self._row(channel, pid)
        if row is None:
            return "full" if self.request(channel, "full", pid) else None
        if row["status"] == "done" and row["checked_at"]:
            try:
                checked = datetime.fromisoformat(row["checked_at"])
            except ValueError:
                checked = None
            if checked is None or self.clock() - checked >= AUTO_LATEST_AFTER:
                return "latest" if self.request(channel, "latest", pid) else None
        return None

    def cancel(self, channel: str, platform: str | None = None) -> bool:
        key = (self._pid(platform), channel)
        if key in self.queue:
            self.queue.remove(key)
            with self.db:
                self.db.execute("UPDATE catalog_channels SET status='cancelled', error=?, finished_at=? WHERE platform=? AND channel=?",
                                (CANCEL_ERROR, self._now(), *key))
            return True
        if self.current and self.current[0] == key and not self.current[1].done():
            self.cancel_reason = "user"
            self.current[1].cancel()
            return True
        return False

    async def forget(self, channel: str, platform: str | None = None) -> None:
        """다시보기 채널에서 지운 채널의 수집 목록을 지운다. 받은 파일·받은 기록·작업은 남긴다."""
        key = (self._pid(platform), channel)
        if key in self.queue:
            self.queue.remove(key)
        if self.current and self.current[0] == key and not self.current[1].done():
            self.cancel_reason = "forget"
            task = self.current[1]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with self.db:
            self.db.execute("DELETE FROM catalog_videos WHERE platform=? AND channel=?", key)
            self.db.execute("DELETE FROM catalog_channels WHERE platform=? AND channel=?", key)

    # ---- 보기 ----
    @staticmethod
    def _type_filter(video_type: str | None) -> tuple[str, list]:
        if video_type in ("replay", "upload"):
            return " AND video_type=?", [video_type]
        return "", []

    def months(self, channel: str, video_type: str | None = None, platform: str | None = None) -> list[dict[str, Any]]:
        """영상이 있는 달과 달마다 영상 수(최근 달부터). 게시 날짜가 없는 줄은 맨 끝의 NO_MONTH에 모은다."""
        extra, args = self._type_filter(video_type)
        rows = self.db.execute(f"SELECT month, COUNT(*) AS n FROM catalog_videos WHERE platform=? AND channel=?{extra} "
                               "GROUP BY month ORDER BY month IS NULL, month DESC", [self._pid(platform), channel, *args]).fetchall()
        return [{"month": row["month"] or NO_MONTH, "count": row["n"]} for row in rows]

    @staticmethod
    def _public(row) -> dict[str, Any]:
        return {"video_no": row["video"], "title": row["title"], "video_type": row["video_type"], "publish_date": row["publish_date"],
                "month": row["month"], "duration": row["duration"], "adult": bool(row["adult"]), "thumbnail": row["thumbnail"]}

    ORDER = " ORDER BY publish_date IS NULL, publish_date DESC, CAST(video AS INTEGER) DESC"

    def videos(self, channel: str, month: str, video_type: str | None = None, platform: str | None = None) -> list[dict[str, Any]]:
        """고른 달의 영상 전부(게시 날짜 내림차순)."""
        extra, args = self._type_filter(video_type)
        where = "month IS NULL" if month == NO_MONTH else "month=?"
        params = [self._pid(platform), channel] + ([] if month == NO_MONTH else [month]) + args
        rows = self.db.execute(f"SELECT * FROM catalog_videos WHERE platform=? AND channel=? AND {where}{extra}{self.ORDER} "
                               f"LIMIT {MONTH_LIMIT}", params).fetchall()
        return [self._public(r) for r in rows]

    def search(self, channel: str, query: str, video_type: str | None = None,
               platform: str | None = None) -> tuple[list[dict[str, Any]], bool]:
        """제목 검색. 그 채널의 수집 목록 전체에서 최근 것부터 200개까지. 넘치면 참을 함께 돌려준다."""
        extra, args = self._type_filter(video_type)
        rows = self.db.execute(f"SELECT * FROM catalog_videos WHERE platform=? AND channel=? AND instr(lower(title), lower(?)) > 0{extra}"
                               f"{self.ORDER} LIMIT {SEARCH_LIMIT + 1}", [self._pid(platform), channel, query, *args]).fetchall()
        return [self._public(r) for r in rows[:SEARCH_LIMIT]], len(rows) > SEARCH_LIMIT

    # ---- 수집 ----
    def start(self) -> None:
        if self.loop_task is None:
            self.loop_task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        self.stopping = True
        if self.current and not self.current[1].done():
            self.cancel_reason = "stop"
            self.current[1].cancel()
            await asyncio.gather(self.current[1], return_exceptions=True)
        if self.queue:
            with self.db:
                for pid, channel in self.queue:
                    self.db.execute("UPDATE catalog_channels SET status='interrupted', error=?, finished_at=? WHERE platform=? AND channel=?",
                                    (STOP_ERROR, self._now(), pid, channel))
            self.queue.clear()
        if self.loop_task is not None:
            self.loop_task.cancel()
            await asyncio.gather(self.loop_task, return_exceptions=True)
            self.loop_task = None
        if self.aclose is not None:
            await self.aclose()

    async def _loop(self) -> None:
        while not self.stopping:
            if not self.queue:
                self.wake.clear()
                await self.wake.wait()
                continue
            pid, channel = self.queue.popleft()
            row = self._row(channel, pid)
            if row is None or row["status"] != "queued":
                continue
            self.cancel_reason = None
            task = asyncio.create_task(self.collect(channel, row["mode"], pid))
            self.current = ((pid, channel), task)
            await asyncio.gather(task, return_exceptions=True)
            self.current = None
            # 시작하기 전에 취소된 경우에도 상태가 남지 않게 한다
            left = self._row(channel, pid)
            if left is not None and left["status"] in ACTIVE and self.cancel_reason != "forget":
                if self.cancel_reason == "user":
                    self._finish(channel, "cancelled", CANCEL_ERROR, pid)
                else:
                    self._finish(channel, "interrupted", STOP_ERROR, pid)

    async def _page(self, pid: str, channel: str, cursor: Any) -> dict:
        """한 쪽. 429·5xx·연결 실패는 30·60·120초 뒤 다시 부른다. 403은 바로 실패."""
        provider = self.provider(pid)
        for attempt in range(len(BACKOFF) + 1):
            if attempt:
                await self._sleep(pid, BACKOFF[attempt - 1])
            try:
                status, payload = await self._gated(pid, provider.fetch, channel, cursor)
            except FetchError:
                reason = f"{provider.name}에 연결하지 못했습니다"
                continue
            if status == 403:
                raise CollectFailed(f"{provider.subject} 목록 요청을 거부했습니다(HTTP 403). 시간을 두고 다시 시작하십시오")
            if status == 429 or 500 <= status <= 599:
                reason = f"{provider.subject} 목록 요청에 답하지 않았습니다(HTTP {status})"
                continue
            if status != 200:
                raise CollectFailed(f"목록을 받지 못했습니다(HTTP {status})")
            try:
                return provider.parse(status, payload, cursor, channel)
            except ListError as e:
                raise CollectFailed(str(e)) from None  # 응답 본문은 넣지 않는다
        raise CollectFailed(f"{reason}. {len(BACKOFF)}번 다시 불러도 안 됐습니다")

    async def collect(self, channel: str, mode: str, platform: str | None = None) -> None:
        """첫 쪽부터 다음 쪽 표시가 없을 때까지 차례로 받는다. 받은 쪽은 그때그때 저장한다."""
        pid = self._pid(platform)
        provider = self.provider(pid)
        row = self._row(channel, pid)
        run = (row["run"] if row else 0) + 1
        with self.db:
            self.db.execute("INSERT INTO catalog_channels(platform,channel,status,mode,run,pages_done,total_pages,fetched,error,started_at,finished_at) "
                            "VALUES(?,?,?,?,?,0,NULL,0,NULL,?,NULL) ON CONFLICT(platform,channel) DO UPDATE SET status=excluded.status, mode=excluded.mode, "
                            "run=excluded.run, pages_done=0, total_pages=NULL, fetched=0, error=NULL, started_at=excluded.started_at, finished_at=NULL",
                            (pid, channel, "running", mode, run, self._now()))
        seen: set[str] = set()
        cursor, done = provider.first, 0
        try:
            while True:
                if done:
                    await self._sleep(pid, PAGE_PAUSE + max(0.0, min(PAGE_JITTER, self._jitter(pid))))
                data = await self._page(pid, channel, cursor)
                if data.get("channel_name"):
                    # 목록 응답에 이름이 있으면 그것으로 새로 고친다(이름만을 위해 따로 부르지 않는다)
                    with self.db:
                        self.db.execute("UPDATE replay_channels SET name=? WHERE platform=? AND channel=?", (data["channel_name"], pid, channel))
                total = data.get("total_pages")
                if (total or 0) > MAX_PAGES or done >= MAX_PAGES:
                    raise CollectFailed("목록 쪽 수가 너무 많습니다")
                nxt = data.get("next")
                videos = data["videos"]
                nos = [v["video_no"] for v in videos]
                known = {r[0] for r in self.db.execute(
                    f"SELECT video FROM catalog_videos WHERE platform=? AND channel=? AND video IN ({','.join('?' * len(nos))})",
                    [pid, channel, *nos])} if nos else set()
                with self.db:
                    for v in videos:
                        values = [v["video_no"], clean_text(v["title"]), v["video_type"], v["publish_date"], v["month"], v["duration"],
                                  1 if v["adult"] else 0, v["thumbnail"]]
                        # 전체 수집은 이번 회차에 본 영상을 표시한다. 새 영상 확인은 표시를 바꾸지 않는다(새 줄은 0)
                        self.db.execute(
                            f"INSERT INTO catalog_videos(platform,channel,{','.join(COLUMNS)},seen) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(platform,channel,video) DO UPDATE SET title=excluded.title, video_type=excluded.video_type, "
                            "publish_date=excluded.publish_date, month=excluded.month, duration=excluded.duration, adult=excluded.adult, "
                            "thumbnail=excluded.thumbnail" + (", seen=excluded.seen" if mode == "full" else ""),
                            [pid, channel, *values, run if mode == "full" else 0])
                    seen.update(nos)
                    done += 1
                    self.db.execute("UPDATE catalog_channels SET pages_done=?, total_pages=?, fetched=? WHERE platform=? AND channel=?",
                                    (done, total, len(seen), pid, channel))
                if not videos and data.get("skipped", 0) == 0:
                    if mode == "full" and nxt is not None:
                        raise CollectFailed("목록이 끝나기 전에 빈 쪽이 왔습니다")
                    break
                if mode == "latest" and nos and set(nos) <= known:
                    break  # 한 쪽이 모두 이미 있는 영상이면 멈춘다
                if nxt is None:
                    break
                cursor = nxt
            finished = self._now()
            with self.db:
                if mode == "full":
                    # 끝까지 성공했을 때에만 이번에 보이지 않은 영상(그 플랫폼에서 지워진 영상)을 목록에서 지운다
                    self.db.execute("DELETE FROM catalog_videos WHERE platform=? AND channel=? AND seen<>?", (pid, channel, run))
                self.db.execute("UPDATE catalog_channels SET status='done', error=NULL, finished_at=?, checked_at=?"
                                + (", full_at=?" if mode == "full" else "") + " WHERE platform=? AND channel=?",
                                (finished, finished, finished, pid, channel) if mode == "full" else (finished, finished, pid, channel))
        except CollectFailed as e:
            self._finish(channel, "failed", str(e), pid)
        except asyncio.CancelledError:
            reason = self.cancel_reason
            if reason == "user":
                self._finish(channel, "cancelled", CANCEL_ERROR, pid)
            elif reason != "forget":
                self._finish(channel, "interrupted", STOP_ERROR, pid)
            raise
        except Exception:  # noqa: BLE001 - 예상하지 못한 오류도 받은 줄은 남기고 실패로 끝낸다
            self._finish(channel, "failed", "수집 중 예상하지 못한 오류가 났습니다", pid)

    def _finish(self, channel: str, status: str, error: str, platform: str | None = None) -> None:
        with self.db:
            self.db.execute("UPDATE catalog_channels SET status=?, error=?, finished_at=? WHERE platform=? AND channel=?",
                            (status, error, self._now(), self._pid(platform), channel))
