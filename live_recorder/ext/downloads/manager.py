"""분석 한 자리와 다운로드 한 자리를 가진 영속 대기열.

- 수신 프로세스는 `live_recorder.ext.downloads.worker`다.
- 작업에는 쿠키·API 주소·ffmpeg 경로·연결 수를 표준 입력으로만 넘긴다. 저장소에는 넣지 않는다.
- 완료 파일은 `completed/<작업ID>.mp4`가 아니라 다시보기 폴더에 이름으로 둔다. 같은 이름이 있으면 번호를 붙인다.
- 진행률을 수신 프로세스가 계산해 보낸다. 받은 바이트도 함께 센다.
- 분석 실패 항목은 정해진 오류 코드의 문장을 보여 준다(로그인 필요를 알 수 있게).
- 파일 관리자 잠금을 위해 진행 중인 작업의 경로를 알려 준다(`active_paths`).

일시정지·이어받기와 다운로드 시간대:
- 상태 `paused`(일시정지). 이유 `pause_reason`: user(사용자)·global(전체)·window(시간대)·recording(녹화 중)·
  space(여유 공간 부족)·queue(사용자가 재개해 차례를 기다림). user가 아닌 일시정지는 입장이 열리면 대기열 순서대로 다시 시작한다.
- 일시정지한 작업은 작업 폴더(부분 파일)를 남긴다. 재시작해도 일시정지로 남는다(`interrupted`와 다르다).
- 입장(admission)이 닫히면 새 작업을 시작하지 않고, 멈춰야 하는 이유면 받는 중인 작업을 일시정지한다.
- 전송 속도와 남은 시간은 최근 10초의 받은 양·진행률로 추정한다.
- 선택 다운로드에서 분석에 실패한 영상은 실패한 작업으로 남긴다.
"""

import asyncio
import json
import math
import os
import re
import shutil
import signal
import stat
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from collections import deque

from ... import lowprio
from ...core import naming
from . import admission as adm
from .errors import ERROR_MESSAGES, ReplayError, ReplayFailure, classify_exception, safe_code
from .resume import has_prior
from .store import Store, canonical, directory, safe_path
from . import api
from .media import MAX_ITEMS, parse_section

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
EXTENSION_OFF = 'extension'  # 작업 종류의 확장이 꺼져 멈춘 작업(멈춘 이유)
SAFE_STAGES = {'analyzing', 'downloading', 'validating', 'finalizing'}
OWNER = 'admin'  # 관리자 계정은 하나다. 저장 형식의 owner 열은 그대로 둔다
WORK_DIR = '.work'


def now():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value, length=500):
    """표시 문자열 정리. 화면용 문자열이 주소를 옮기는 통로가 되지 않게 한다."""
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', str(value or ''))
    text = re.sub(r'https?://\S+', '[주소]', text, flags=re.IGNORECASE)
    return text[:length]


ACTIVE = ('queued', 'running', 'finalizing', 'cancelling', 'paused')
MAX_SELECT = 30
# 작업 목록 상태 필터. 확인 필요가 기본이고 완료는 완료 필터에서만 보인다. 일시정지는 확인 필요·진행에 든다
JOB_FILTERS = {'attention': ('queued', 'running', 'finalizing', 'cancelling', 'paused', 'failed', 'interrupted'),
               'active': ACTIVE, 'failed': ('failed', 'interrupted', 'cancelled'), 'completed': ('completed',)}
SPEED_WINDOW = 10.0  # 속도·남은 시간을 재는 구간(초)
RESUME_MESSAGES = {  # 이어받기·처음부터 받기를 작업에 적는 문장
    'not_resumable': '구간 받기는 이어받을 수 없어 처음부터 다시 받습니다.',
    'no_range': '영상 서버가 범위 요청을 받지 않아 처음부터 다시 받습니다.',
    'changed': '영상 정보가 바뀌어 처음부터 다시 받습니다.',
    'mismatch': '부분 파일이 서버 내용과 맞지 않아 처음부터 다시 받습니다.',
    'no_state': '이어받을 기록이 없어 처음부터 다시 받습니다.',
    'unsupported': '이 재생목록 형식은 이어받을 수 없어 처음부터 다시 받습니다.',
}


class JobSuspended(Exception):
    """받는 중인 작업을 일시정지한다(이유: admission.REASON_LABELS)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def estimate(samples, total_bytes):
    """(시각, 받은 바이트, 진행률) 표본으로 (초당 바이트, 남은 초)를 추정한다. 모르면 남은 초는 None."""
    if len(samples) < 2:
        return 0, None
    t0, b0, p0 = samples[0]
    t1, b1, p1 = samples[-1]
    span = t1 - t0
    if span <= 0:
        return 0, None
    speed = max(0.0, (b1 - b0) / span) if b0 is not None and b1 is not None else 0.0
    eta = None
    if total_bytes and b1 is not None and speed > 0:
        eta = max(0.0, (total_bytes - b1) / speed)
    elif p0 is not None and p1 is not None and p1 > p0:
        eta = max(0.0, (100.0 - p1) * span / (p1 - p0))
    return speed, None if eta is None else round(eta)


def video_key(url):
    """중복 판단 열쇠. 작업 종류가 정한다(다시보기는 영상 번호, 클립은 `clip:` 접두사를 붙인 클립 ID).
    꺼진 확장의 종류도 알아본다(받은 기록은 남아 있다)."""
    try:
        p, kind = api.parse_any(url)
    except (ReplayFailure, TypeError, ValueError):
        return None
    return kind.module().key(p)


def parse_url(url):
    """켜진 작업 종류로 주소를 알아본다. 꺼진 확장의 주소면 ReplayError(400, "<이름> 확장이 꺼져 있습니다")."""
    try:
        return api.parse(url)
    except api.DisabledKind as e:
        raise ReplayError(400, e.message) from None


class ReplayManager:
    def __init__(self, state_dir, download_root, min_free_bytes=5 * 1024 ** 3, worker_command=None,
                 analysis_timeout=180, job_timeout=86400, download_base=None, *, ffprobe='ffprobe', ffmpeg='ffmpeg',
                 task_extras: Callable[[], dict] | None = None, disk_free: Callable[[Path], int] | None = None,
                 low_priority: Callable[[], bool] | None = None, admission: Callable[[], dict] | None = None,
                 poll: float = 1.0, monitor_interval: float = .5, request_gate: Callable[[str], Any] | None = None):
        self.request_gate = request_gate  # 플랫폼 요청 창구(중심부의 것을 확장 접점으로 받는다)
        self.root = safe_path(download_root)
        self.download_base = safe_path(download_base) if download_base is not None else self.root.parent
        if not self.root.is_relative_to(self.download_base):
            raise ValueError('outside_download_base')
        self.root = directory(self.root)
        self.work_root = directory(self.root / WORK_DIR)
        self.min_free_bytes = max(0, int(min_free_bytes))
        self.analysis_timeout = max(.05, float(analysis_timeout))
        self.job_timeout = max(.05, float(job_timeout))
        self.ffprobe = ffprobe
        self.ffmpeg = ffmpeg
        self.task_extras = task_extras or (lambda platform=None: {})
        self.disk_free = disk_free or (lambda path: shutil.disk_usage(path).free)
        self.low_priority = low_priority or (lambda: False)
        self.admission = admission or adm.open_state  # 전체 일시정지·시간대·녹화 중
        self.poll = max(.05, float(poll))  # 입장이 닫혀 있을 때 다시 보는 간격
        self.monitor_interval = max(.05, float(monitor_interval))
        self.command = list(worker_command) if worker_command is not None else [sys.executable, '-m', 'live_recorder.ext.downloads.worker']
        if not self.command or any(not isinstance(x, str) for x in self.command):
            raise ValueError('invalid_worker_command')
        self.store = Store(state_dir)
        self.db = self.store.db
        self.wake = {'analysis': asyncio.Event(), 'job': asyncio.Event(), 'retention': asyncio.Event()}
        self.tasks = []
        self.processes = {}
        self.done = {}
        self.publishing = {}
        self.cancel_requested = set()
        self.suspend_requests = {}  # 작업 ID → 일시정지 이유. 받는 중인 작업을 멈출 때 쓴다
        self.held = set()  # 부분 파일을 남겨 둔 일시정지 작업(잠금)
        self.reserve = {}  # 받는 중인 작업 ID → 앞으로 더 쓸 공간
        self.claim_need = 0  # 차례가 된 작업이 시작하려면 더 있어야 하는 공간
        self.close_task = None
        self.stopping = False
        self.closed = False
        with self.db:
            # 재시작하면 끝나지 않은 작업은 interrupted로 남긴다. 자동으로 다시 받지 않는다.
            # 일시정지한 작업은 일시정지로 남기고 부분 파일도 남긴다
            # 꺼진 확장의 작업은 지우거나 끊지 않는다: 부분 파일을 남긴 채 "확장 꺼짐"으로 멈춰 두고,
            # 그 확장을 다시 켜면 대기열 순서대로 이어서 받는다(일시정지·시간대 규칙을 따른다)
            for row in self.db.execute("SELECT owner,payload FROM records WHERE kind='job' AND status NOT IN ('completed','failed','cancelled','interrupted')").fetchall():
                data = json.loads(row['payload'])
                if api.find_kind(data.get('kind') or 'video') is None and not (data['status'] == 'paused' and data.get('pause_reason') == 'user'):
                    data.update(status='paused', stage='paused', pause_reason=EXTENSION_OFF, updated_at=now(),
                                speed_bps=0, eta_seconds=None)
                    self.store.save('job', row['owner'], data)
            for row in self.db.execute("SELECT kind,owner,payload FROM records WHERE status NOT IN ('completed','failed','cancelled','interrupted','paused')").fetchall():
                data = json.loads(row['payload'])
                data.update(status='interrupted', updated_at=now(), error='서버가 중단되었습니다. 다시 시도해 주세요.')
                if row['kind'] == 'job':
                    data.update(stage='interrupted', can_download=False, speed_bps=0, eta_seconds=None)
                self.store.save(row['kind'], row['owner'], data)
                if row['kind'] == 'analysis' and data.get('_auto'):
                    self._auto_fail(row['owner'], data, 'interrupted', None, '서버가 중단되었습니다. 다시 시도해 주세요.')
                self._cleanup(self.work_root / data['id'])
            # 받은 영상 기억이 없던 저장소는 남아 있는 완료 작업 내역에서 채운다
            for row in self.db.execute("SELECT payload FROM records WHERE kind='job' AND status='completed'").fetchall():
                self._remember(json.loads(row['payload']))
            self.held = {row['id'] for row in self.db.execute("SELECT id FROM records WHERE kind='job' AND status='paused'")}
        # 지난 실행이 남긴 작업 폴더도 지운다(임시 파일을 남기지 않는다). 일시정지한 작업의 폴더는 남긴다
        for entry in list(self.work_root.iterdir()):
            if re.fullmatch('[0-9a-f]{32}', entry.name) and entry.name not in self.held:
                self._cleanup(entry)

    @staticmethod
    def public(value):
        return {key: val for key, val in value.items() if not key.startswith('_')}

    def _get(self, kind, owner, ident):
        if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{32}', ident):
            raise ReplayError(404, '항목을 찾을 수 없습니다.')
        value = self.store.get(kind, owner, ident)
        if value is None:
            raise ReplayError(404, '항목을 찾을 수 없습니다.')
        return value

    def _save(self, kind, owner, value):
        with self.db:
            self.store.save(kind, owner, value)

    def _log(self, ident, message, level='info'):
        # 정해진 문장만 쓴다. 수신 프로세스나 도구의 예외 문장은 넣지 않는다
        with self.db:
            self.db.execute('INSERT INTO logs(job,at,level,message) VALUES(?,?,?,?)', (ident, now(), level, message))
            self.db.execute('DELETE FROM logs WHERE job=? AND seq NOT IN (SELECT seq FROM logs WHERE job=? ORDER BY seq DESC LIMIT 200)', (ident, ident))

    def _remember(self, job):
        key = video_key(job.get('url'))
        if key and isinstance(job.get('filename'), str):
            self.db.execute('INSERT OR REPLACE INTO downloads(video,quality,title,filename,completed_at) VALUES(?,?,?,?,?)',
                            (key, str(job.get('quality') or ''), str(job.get('title') or ''), job['filename'],
                             job.get('completed_at') or job.get('updated_at') or now()))

    def _pending_auto(self):
        """선택 다운로드로 분석 대기·진행 중인 영상. 작업이 만들어지기 전까지는 "대기"로 본다."""
        out = {}
        for row in self.db.execute("SELECT payload FROM records WHERE kind='analysis' AND status IN ('queued','running')"):
            data = json.loads(row[0])
            if data.get('_auto'):
                for url, title in zip(data['_urls'], data['_auto'].get('titles', [])):
                    out[video_key(url)] = title
        return out

    def video_states(self, keys):
        """목록 상태 칸: downloaded·missing·queued·running·failed 또는 None."""
        keys = set(keys)
        states = {}
        for row in self.db.execute("SELECT video,filename FROM downloads"):
            if row['video'] in keys:
                name = row['filename']
                exists = Path(name).name == name and (self.root / name).is_file()
                if exists or states.get(row['video']) != 'downloaded':
                    states[row['video']] = 'downloaded' if exists else 'missing'
        rank = {'failed': 1, 'queued': 2, 'running': 3}
        for row in self.db.execute("SELECT status,payload FROM records WHERE kind='job' AND status IN "
                                   "('queued','running','finalizing','cancelling','paused','failed','interrupted')"):
            key = video_key(json.loads(row['payload']).get('url'))
            if key not in keys:
                continue
            state = {'queued': 'queued', 'paused': 'queued', 'failed': 'failed', 'interrupted': 'failed'}.get(row['status'], 'running')
            current = states.get(key)
            if current in ('downloaded', 'missing') and state == 'failed':
                continue
            if current is None or rank.get(state, 0) > rank.get(current, 0):
                states[key] = state
        for key in self._pending_auto():
            if key in keys and states.get(key) not in ('running',):
                states[key] = 'queued'
        return states

    def duplicates(self, entries):
        """(열쇠, 제목) 목록 가운데 이미 받았거나 대기·진행 중인 것의 제목."""
        states = self.video_states(key for key, _ in entries)
        return [title for key, title in entries if states.get(key) in ('downloaded', 'missing', 'queued', 'running')]

    def _check_duplicates(self, entries, confirm):
        if confirm is True:
            return
        titles = self.duplicates(entries)
        if titles:
            raise ReplayError(409, '이미 받았거나 대기·진행 중인 영상이 있습니다. 확인한 뒤 다시 요청하십시오',
                              {'duplicates': titles[:MAX_SELECT]})

    async def select_download(self, owner, entries, confirm=False):
        """고른 영상(주소, 제목)을 분석한 뒤 기본 화질(best)로 작업을 만든다. 분석·다운로드는 하나씩 차례로.
        목록(채널 다시보기)에서 고르는 일은 그 종류를 내놓은 확장이 하고 주소만 넘긴다(downloads 공개 접점)."""
        self._available()
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_SELECT:
            raise ReplayError(400, f'한 번에 1~{MAX_SELECT}개를 고르십시오.')
        urls, titles = [], []
        for url, title in entries:
            url = parse_url(url)[0]['url']
            if url not in urls:
                urls.append(url)
                titles.append(clean_text(title, 300))
        self._check_duplicates([(video_key(u), t) for u, t in zip(urls, titles)], confirm)
        self._capacity(owner, len(urls))
        if self.db.execute("SELECT count(*) FROM records WHERE kind='analysis' AND status IN ('queued','running')").fetchone()[0] >= 20:
            raise ReplayError(429, '분석 대기열이 가득 찼습니다.')
        data = {'id': uuid.uuid4().hex, 'status': 'queued', 'items': [], '_urls': urls,
                '_auto': {'titles': titles, 'quality': 'best'}, 'created_at': now(), 'updated_at': now(),
                'error': None, 'warnings': []}
        self._save('analysis', owner, data)
        self.wake['analysis'].set()
        return {'queued': len(urls), 'analysis_id': data['id']}

    def _auto_jobs(self, owner, analysis):
        """선택 다운로드의 분석이 끝나면 영상마다 기본 화질로 작업을 만든다.

        분석에 실패한 영상(로그인 필요 등)은 조용히 빠지지 않게 그 이유를 적은 실패 작업으로 남긴다.
        이 작업을 재시도하면 수신 프로세스가 다시 분석하고 같은 규칙으로 화질을 고른다.
        """
        from ...core.quality import Track, select
        rule = analysis['_auto'].get('quality', 'best')
        titles = dict(zip(analysis['_urls'], analysis['_auto'].get('titles', [])))
        jobs, failed = [], []
        for item in analysis['items']:
            title = item.get('title') or titles.get(item.get('url')) or item.get('url') or ''
            if item.get('error') or not item.get('qualities'):
                code = item.get('error_code') or 'not_ready'
                failed.append(self._auto_failed_job(item.get('url') or '', title, rule, 'failed', code, item.get('error') or ERROR_MESSAGES[safe_code(code)]))
                continue
            tracks = [Track(q['id'], q.get('height'), None, None) for q in item['qualities']]
            chosen = select(tracks, rule)
            quality = chosen.track_id if chosen else item['qualities'][0]['id']
            try:
                spec = self.validate_options({'quality': quality, 'section': None}, item)
            except (ValueError, TypeError, ReplayFailure):
                failed.append(self._auto_failed_job(item['url'], title, rule, 'failed', 'engine_operation_failed',
                                                    ERROR_MESSAGES['engine_operation_failed']))
                continue
            jobs.append(self._new_job(spec))
        with self.db:
            for job in jobs + failed:
                self.store.save('job', owner, job)
        for job in jobs:
            self._log(job['id'], '선택 다운로드로 대기열에 등록했습니다.')
        for job in failed:
            self._log(job['id'], f"선택 다운로드의 영상 분석에 실패했습니다. {job['error']}", 'error')
        if jobs:
            self.wake['job'].set()
        return len(jobs)

    def _auto_failed_job(self, url, title, rule, status, code, message):
        """분석하지 못한 선택 다운로드 영상의 작업. 화질은 다시 시도할 때 규칙으로 고른다."""
        try:
            parsed, _ = api.parse_any(url)
        except (ReplayFailure, TypeError, ValueError):
            parsed = {'platform': '', 'kind': ''}
        job = self._new_job({'url': url, 'platform': parsed['platform'], 'kind': parsed['kind'], 'title': clean_text(title, 300),
                             'quality': None, 'quality_rule': rule, 'quality_label': '', 'section': None,
                             'duration_seconds': None, 'filename_hint': None})
        job.update(status=status, stage=status, error=message, error_code=safe_code(code) if status == 'failed' and code else None)
        return job

    def _auto_fail(self, owner, analysis, status, code, message):
        """선택 다운로드의 분석 자체가 실패·중단됐을 때: 고른 영상마다 실패(중단) 작업을 남긴다. 저장소 트랜잭션 안에서 부른다."""
        auto = analysis.get('_auto') or {}
        rule = auto.get('quality', 'best')
        jobs = [self._auto_failed_job(url, title, rule, status, code, message)
                for url, title in zip(analysis.get('_urls', []), auto.get('titles', []))]
        analysis['_auto'] = None
        self.store.save('analysis', owner, analysis)
        for job in jobs:
            self.store.save('job', owner, job)
            self.db.execute('INSERT INTO logs(job,at,level,message) VALUES(?,?,?,?)',
                            (job['id'], now(), 'error', f'선택 다운로드의 영상 분석에 실패했습니다. {message}'))
        return len(jobs)

    def active_count(self):
        """대기·진행 중인 작업 수(선택 다운로드로 분석을 기다리는 영상 포함). 일시정지한 작업은 넣지 않는다."""
        busy = tuple(s for s in ACTIVE if s != 'paused')
        jobs = self.db.execute(f"SELECT count(*) FROM records WHERE kind='job' AND status IN ({','.join('?' * len(busy))})", busy).fetchone()[0]
        return jobs + len(self._pending_auto())

    async def cancel_active(self, owner):
        """대기·진행 중인 작업과 선택 다운로드의 분석 대기를 모두 취소한다."""
        self._available()
        count = 0
        with self.db:
            for row in self.db.execute("SELECT payload FROM records WHERE kind='analysis' AND owner=? AND status IN ('queued','running')", (owner,)).fetchall():
                data = json.loads(row[0])
                if data.get('_auto'):
                    count += len(data['_urls'])
                    if data['status'] == 'queued':
                        data.update(status='failed', error='전체 취소로 취소했습니다.', updated_at=now())
                    data['_auto'] = None  # 분석 중이면 분석만 마치고 작업은 만들지 않는다
                    self.store.save('analysis', owner, data)
        # 일시정지한 작업은 넣지 않는다(부분 파일이 실수로 지워지지 않게. 줄에서 하나씩 취소한다)
        for row in self.db.execute("SELECT id FROM records WHERE kind='job' AND owner=? AND status IN ('queued','running','finalizing')", (owner,)).fetchall():
            await self.cancel(owner, row['id'])
            count += 1
        return {'cancelled': count}

    def has_active_work(self):
        return self.db.execute("SELECT 1 FROM records WHERE status NOT IN ('completed','failed','cancelled','interrupted') LIMIT 1").fetchone() is not None

    def active_paths(self):
        """진행 중인 다운로드의 작업 폴더와 내보내는 중인 파일.

        일시정지한 작업의 부분 파일(작업 폴더와 그 안의 파일)도 잠근다. 잠긴 파일의 상위 폴더도 함께 잠긴다."""
        paths = [self.work_root / ident for ident in self.processes]
        paths += [self.work_root / ident for ident in self.done]
        paths += list(self.publishing.values())
        for ident in list(self.held):
            work = self.work_root / ident
            paths.append(work)
            paths += [work / name for name in ('video.mp4', 'resume.json', 'seg')]
        return paths

    # ---- 입장과 일시정지 ----
    def gate(self, need=None):
        """지금의 입장 상태. 여유 공간(최소 여유 공간)은 관리자가 잰다.

        여유 공간 판단에는 받는 방식이 더 쓸 공간(need)을 더한다. HLS형은 조각과 담은 결과가 함께 있어
        영상 크기의 두 배가 든다. 크기를 모르면 더하지 않는다. need를 주지 않으면 받는 중이거나 차례를 기다리는 작업의 몫을 쓴다."""
        try:
            state = dict(self.admission())
        except Exception:  # noqa: BLE001 - 판단에 실패하면 열지 않는다
            state = dict(adm.open_state(), open=False, reason='global', suspend=False, error='입장 상태를 읽지 못했습니다')
        if need is None:
            need = max([self.claim_need, *self.reserve.values()])
        try:
            low = self.disk_free(safe_path(self.root)) < self.min_free_bytes + max(0, int(need))
        except (OSError, ValueError):
            low = True
        state['space_need'] = max(0, int(need))
        return adm.with_space(state, low)

    @staticmethod
    def space_need(job, on_disk=None):
        """작업이 앞으로 더 쓸 공간. HLS형(조각 받기)은 추정 크기의 두 배에서 이미 받은 조각을 뺀다."""
        estimate = job.get('size_estimate')
        if job.get('space_mode') != 'hls' or type(estimate) is not int or estimate <= 0:
            return 0
        have = on_disk if on_disk is not None else (job.get('partial_bytes') or job.get('downloaded_bytes') or 0)
        return max(0, 2 * estimate - int(have))

    def paused_count(self):
        return self.db.execute("SELECT count(*) FROM records WHERE kind='job' AND status='paused'").fetchone()[0]

    @staticmethod
    def _partial_size(work):
        """작업 폴더(부분 파일)의 크기. 조각 폴더(seg)까지 센다."""
        total = 0
        stack = [work]
        while stack:
            try:
                entries = list(os.scandir(stack.pop()))
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
        return total

    async def pause(self, owner, ident):
        """대기 중이면 바로 일시정지하고, 받는 중이면 수신 프로세스를 멈추고 부분 파일을 남긴다."""
        self._available()
        job = self._get('job', owner, ident)
        status = job['status']
        if status == 'queued':
            job.update(status='paused', stage='paused', pause_reason='user', paused_at=now(), updated_at=now(),
                       speed_bps=0, eta_seconds=None)
            self._save('job', owner, job)
            self._log(ident, '일시정지했습니다.')
        elif status == 'paused':
            if job.get('pause_reason') != 'user':
                job.update(pause_reason='user', updated_at=now())
                self._save('job', owner, job)
                self._log(ident, '일시정지했습니다. 재개할 때까지 다시 시작하지 않습니다.')
        elif status == 'running' and ident in self.done:
            finished = self.done[ident]
            self.suspend_requests[ident] = 'user'
            self._log(ident, '일시정지를 요청했습니다.')
            await self._terminate(self.processes.get(ident))
            try:
                await asyncio.wait_for(finished.wait(), 8)
            except asyncio.TimeoutError:
                pass
        else:
            raise ReplayError(409, '검증·취소 중이거나 끝난 작업은 일시정지할 수 없습니다.')
        return self.get_job(owner, ident)

    async def resume(self, owner, ident):
        """일시정지한 작업을 대기열 순서대로 다시 받는다. 부분 파일이 있으면 이어받는다."""
        self._available()
        job = self._get('job', owner, ident)
        if job['status'] != 'paused':
            raise ReplayError(409, '일시정지한 작업만 재개할 수 있습니다.')
        if api.find_kind(job.get('kind') or 'video') is None:
            job.update(pause_reason=EXTENSION_OFF, updated_at=now())  # 확장을 다시 켜면 차례대로 이어 받는다
        elif has_prior(self.work_root / ident):
            job.update(pause_reason='queue', updated_at=now())  # 부분 파일을 지키려고 일시정지 상태로 차례를 기다린다
        else:
            job.update(status='queued', stage='queued', pause_reason=None, paused_at=None, updated_at=now())
            self.held.discard(ident)
        self._save('job', owner, job)
        self._log(ident, '재개를 요청했습니다.')
        self.wake['job'].set()
        return self.get_job(owner, ident)

    def _available(self):
        if self.stopping or self.closed:
            raise ReplayError(503, '작업 관리자가 종료 중입니다.')

    async def start(self):
        self._available()
        if not self.tasks:
            self.tasks = [asyncio.create_task(self._loop(kind)) for kind in ('analysis', 'job')]
            self.tasks.append(asyncio.create_task(self._retention_loop()))
            for event in self.wake.values():
                event.set()

    def prune_completed(self, current_time=None):
        """완료된 작업의 내역은 1시간 뒤 정리한다. 영상 파일은 남긴다."""
        cutoff = (current_time or datetime.now(timezone.utc)) - timedelta(hours=1)
        with self.db:
            expired = set()
            for row in self.db.execute("SELECT id,payload FROM records WHERE kind='job' AND status='completed'").fetchall():
                job = json.loads(row['payload'])
                try:
                    completed = datetime.fromisoformat(job.get('completed_at') or job['updated_at'])
                    if completed.tzinfo is None:
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                if completed <= cutoff and row['id'] not in self.done:
                    expired.add(row['id'])
            if not expired:
                return
            for row in self.db.execute('SELECT owner,key,ids FROM requests').fetchall():
                ids = json.loads(row['ids'])
                retained = [ident for ident in ids if ident not in expired]
                if retained == ids:
                    continue
                if retained:
                    self.db.execute('UPDATE requests SET ids=? WHERE owner=? AND key=?', (canonical(retained), row['owner'], row['key']))
                else:
                    self.db.execute('DELETE FROM requests WHERE owner=? AND key=?', (row['owner'], row['key']))
            for ident in expired:
                self.db.execute('DELETE FROM logs WHERE job=?', (ident,))
                self.db.execute('DELETE FROM records WHERE id=?', (ident,))

    async def _retention_loop(self):
        while not self.stopping:
            self.prune_completed()
            try:
                await asyncio.wait_for(self.wake['retention'].wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            self.wake['retention'].clear()

    async def analyze(self, owner, urls):
        """한 요청에 URL 10개까지."""
        self._available()
        if not isinstance(urls, list) or not 1 <= len(urls) <= 10:
            raise ReplayError(400, 'URL은 한 번에 1~10개 입력해 주세요.')
        normalized = []
        for url in urls:
            try:
                normalized.append(parse_url(url)[0]['url'])
            except (TypeError, ValueError):
                raise ReplayError(400, api.unknown_url_message()) from None  # 켜진 종류에 맞춘 안내
        if self.db.execute("SELECT count(*) FROM records WHERE kind='analysis' AND status IN ('queued','running')").fetchone()[0] >= 20:
            raise ReplayError(429, '분석 대기열이 가득 찼습니다.')
        if self.db.execute("SELECT count(*) FROM records WHERE kind='analysis' AND owner=?", (owner,)).fetchone()[0] >= 200:
            self._prune_analyses(owner)
        data = {'id': uuid.uuid4().hex, 'status': 'queued', 'items': [], '_urls': list(dict.fromkeys(normalized)),
                'created_at': now(), 'updated_at': now(), 'error': None, 'warnings': []}
        self._save('analysis', owner, data)
        self.wake['analysis'].set()
        return self.public(data)

    def _prune_analyses(self, owner):
        """분석 이력은 최근 100개만 남긴다(관리자가 하나라 오래된 것부터 지운다)."""
        with self.db:
            self.db.execute("DELETE FROM records WHERE kind='analysis' AND owner=? AND status IN ('completed','failed','interrupted') "
                            "AND id NOT IN (SELECT id FROM records WHERE kind='analysis' AND owner=? ORDER BY rowid DESC LIMIT 100)",
                            (owner, owner))

    def get_analysis(self, owner, ident):
        return self.public(self._get('analysis', owner, ident))

    def _new_job(self, spec):
        return dict(spec, id=uuid.uuid4().hex, created_at=now(), attempt=1, **self._queued_fields())

    def _queued_fields(self):
        return dict(status='queued', stage='queued', progress=None, speed_bps=0, eta_seconds=None, downloaded_bytes=0,
                    total_bytes=None, updated_at=now(), queued_at=now(), error=None, error_code=None, filename=None,
                    can_download=False, pause_reason=None, paused_at=None, partial_bytes=None, resume_note=None,
                    _download_root=str(self.root))

    def _capacity(self, owner, amount, *, new_records=True):
        if self.db.execute("SELECT count(*) FROM records WHERE kind='job' AND status NOT IN ('completed','failed','cancelled','interrupted')").fetchone()[0] + amount > 100:
            raise ReplayError(429, '대기·진행 작업은 최대 100개입니다.')
        if new_records and self.db.execute("SELECT count(*) FROM records WHERE kind='job' AND owner=?", (owner,)).fetchone()[0] + amount > 1000:
            raise ReplayError(429, '작업 이력 한도에 도달했습니다.')

    @staticmethod
    def validate_options(item, metadata):
        """분석 결과에 있는 화질과, 지원하는 영상의 구간만 받는다."""
        if not isinstance(item, dict) or item.get('quality') not in {q['id'] for q in metadata['qualities']}:
            raise ValueError('quality')
        section = item.get('section') or None
        if section is not None:
            if not isinstance(section, str) or not metadata['supports']['section']:
                raise ValueError('section')
            start, end = parse_section(section)
            duration = metadata.get('duration_seconds')
            if duration is not None and end > duration + 1:
                raise ValueError('section')
        kind = api.find_kind(metadata.get('kind'))
        if kind is None:
            raise ValueError('kind')
        hint = kind.module().filename(metadata)
        return {'url': metadata['url'], 'platform': metadata.get('platform', ''), 'kind': metadata.get('kind'),
                'title': metadata['title'], 'quality': item['quality'],
                'quality_label': next((q['label'] for q in metadata['qualities'] if q['id'] == item['quality']), ''),
                'section': section, 'duration_seconds': metadata.get('duration_seconds'), 'filename_hint': hint}

    async def create_jobs(self, owner, analysis_id, items, idempotency_key, confirm=False):
        self._available()
        if not isinstance(idempotency_key, str) or not re.fullmatch('[A-Za-z0-9_-]{8,100}', idempotency_key):
            raise ReplayError(400, '올바른 요청 키가 필요합니다.')
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
            raise ReplayError(400, '작업은 한 번에 1~50개 등록해 주세요.')
        normalized = []
        for item in items:
            if not isinstance(item, dict) or set(item) - {'url', 'quality', 'section'}:
                raise ReplayError(400, '작업 옵션이 올바르지 않습니다.')
            try:
                url = parse_url(item.get('url'))[0]['url']
                normalized.append({'url': url, 'quality': item.get('quality'), 'section': item.get('section') or None})
            except (ValueError, TypeError):
                raise ReplayError(400, '분석한 영상만 등록할 수 있습니다.') from None
        try:
            body = canonical({'analysis_id': analysis_id, 'items': normalized})
        except (ValueError, TypeError):
            raise ReplayError(400, '작업 옵션이 올바르지 않습니다.') from None
        with self.db:
            previous = self.db.execute('SELECT body,ids FROM requests WHERE owner=? AND key=?', (owner, idempotency_key)).fetchone()
            if previous:
                if previous['body'] != body:
                    raise ReplayError(409, '동일한 요청 키의 내용이 변경되었습니다.')
                return {'jobs': [self.get_job(owner, ident) for ident in json.loads(previous['ids'])]}
            analysis = self._get('analysis', owner, analysis_id)
            if analysis['status'] != 'completed':
                raise ReplayError(409, '분석이 완료된 뒤 등록해 주세요.')
            known = {item['url']: item for item in analysis['items'] if not item.get('error')}
            specs = []
            for item in normalized:
                if item['url'] not in known:
                    raise ReplayError(400, '분석한 영상만 등록할 수 있습니다.')
                try:
                    specs.append(self.validate_options(item, known[item['url']]))
                except (ValueError, TypeError):
                    raise ReplayError(400, '화질과 구간 옵션을 확인해 주세요. 구간은 HH:MM:SS~HH:MM:SS이고 영상 길이 안이어야 합니다.') from None
            self._check_duplicates([(video_key(spec['url']), spec['title']) for spec in specs], confirm)
            self._capacity(owner, len(specs))
            jobs = [self._new_job(spec) for spec in specs]
            for job in jobs:
                self.store.save('job', owner, job)
            self.db.execute('INSERT INTO requests VALUES(?,?,?,?)', (owner, idempotency_key, body, canonical([job['id'] for job in jobs])))
        for job in jobs:
            self._log(job['id'], '작업을 대기열에 등록했습니다.')
        self.wake['job'].set()
        return {'jobs': [self.public(job) for job in jobs]}

    def list_jobs(self, owner, status_filter=None):
        """필터를 주면 그 상태만 보이고, 필터마다 건수를 함께 준다."""
        counts = {name: 0 for name in JOB_FILTERS}
        for row in self.db.execute("SELECT status,count(*) FROM records WHERE kind='job' AND owner=? GROUP BY status", (owner,)):
            for name, statuses in JOB_FILTERS.items():
                if row[0] in statuses:
                    counts[name] += row[1]
        if status_filter is None:
            rows = self.db.execute("SELECT payload FROM records WHERE kind='job' AND owner=? ORDER BY rowid DESC LIMIT 200", (owner,))
        elif status_filter in JOB_FILTERS:
            statuses = JOB_FILTERS[status_filter]
            rows = self.db.execute(f"SELECT payload FROM records WHERE kind='job' AND owner=? AND status IN ({','.join('?' * len(statuses))}) "
                                   "ORDER BY rowid DESC LIMIT 200", (owner, *statuses))
        else:
            raise ReplayError(400, '상태 필터가 올바르지 않습니다.')
        return {'jobs': [self._with_block(self.public(json.loads(row[0]))) for row in rows], 'counts': counts}

    def _active_kind_of(self, url):
        try:
            return parse_url(url)[1]
        except (ReplayError, ReplayFailure, TypeError, ValueError):
            return None

    @staticmethod
    def _with_block(job):
        """꺼진 확장의 작업: 목록에 두되 진행하지 않는다는 표시를 붙인다(취소·삭제는 된다)."""
        if job.get('status') not in TERMINAL and api.find_kind(job.get('kind') or 'video') is None:
            job['blocked'] = api.BLOCKED
        return job

    def get_job(self, owner, ident):
        job = self._get('job', owner, ident)
        if job['status'] == 'completed':
            try:
                self.resolve_file(owner, ident)
            except ReplayError:
                job['can_download'] = False
        return self._with_block(self.public(job))

    def logs(self, owner, ident, after=0):
        self._get('job', owner, ident)
        if type(after) is not int or after < 0:
            raise ReplayError(400, '로그 커서가 올바르지 않습니다.')
        entries = [dict(row) for row in self.db.execute('SELECT seq,at,level,message FROM logs WHERE job=? AND seq>? ORDER BY seq LIMIT 200', (ident, after))]
        return {'logs': entries, 'next_cursor': entries[-1]['seq'] if entries else after}

    async def cancel(self, owner, ident):
        """종료 신호 뒤 기다렸다가 강제 종료한다. 임시 파일은 지운다."""
        self._available()
        job = self._get('job', owner, ident)
        if job['status'] in TERMINAL:
            return self.get_job(owner, ident)
        finished = self.done.get(ident)
        active = finished is not None
        if active:
            self.cancel_requested.add(ident)
        job.update(status='cancelling' if active else 'cancelled', stage='cancelling' if active else 'cancelled', updated_at=now())
        self._save('job', owner, job)
        self._log(ident, '취소를 요청했습니다.')
        if not active:
            # 일시정지하며 남긴 부분 파일을 지운다
            self.held.discard(ident)
            self._cleanup(self.work_root / ident)
        await self._terminate(self.processes.get(ident))
        if active:
            try:
                await asyncio.wait_for(finished.wait(), 8)
            except asyncio.TimeoutError:
                pass
        return self.get_job(owner, ident)

    async def retry(self, owner, ident):
        """같은 작업을 다시 시도한다. 시도 횟수가 늘어난다."""
        self._available()
        previous = self._get('job', owner, ident)
        if previous['status'] not in ('failed', 'cancelled', 'interrupted'):
            raise ReplayError(409, '실패·취소·중단한 작업만 재시도할 수 있습니다.')
        if api.find_kind(previous.get('kind') or 'video') is None:
            raise ReplayError(409, api.BLOCKED)
        if ident in self.done:
            raise ReplayError(409, '이전 시도를 정리하고 있습니다.')
        with self.db:
            self._capacity(owner, 1, new_records=False)
            job = previous
            job.update(self._queued_fields(), attempt=previous.get('attempt', 1) + 1)
            self.store.save('job', owner, job)
        self._log(ident, f"{job['attempt']}번째 시도를 대기열에 등록했습니다.")
        self.wake['job'].set()
        return {'jobs': [self.public(job)]}

    def resolve_file(self, owner, ident):
        job = self._get('job', owner, ident)
        name = job.get('filename')
        if (job['status'] != 'completed' or not job.get('can_download') or not isinstance(name, str)
                or Path(name).name != name or name in ('.', '..') or not name.endswith('.mp4')):
            raise ReplayError(404, '완료 파일을 찾을 수 없습니다.')
        try:
            root_value = job.get('_download_root')
            if not isinstance(root_value, str):
                raise ValueError('missing_download_root')
            root = Path(root_value)
            if not root.is_absolute() or '..' in root.parts:
                raise ValueError('unsafe_download_root')
            root = safe_path(root)
            if not root.is_relative_to(self.download_base):
                raise ValueError('outside_download_base')
            path = safe_path(root / name)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise ValueError('invalid_file')
        except (OSError, ValueError):
            raise ReplayError(404, '완료 파일을 찾을 수 없습니다.') from None
        return path

    @staticmethod
    async def _terminate(proc):
        if proc is None:
            return
        # 우리가 띄운 프로세스의 그룹에만 신호를 보낸다. 자식(ffmpeg)이 남지 않게 그룹 전체에 보낸다
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 1.5)
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 1.5)
        except asyncio.TimeoutError:
            pass

    async def _spawn(self, ident, command, **kwargs):
        launch = asyncio.create_task(asyncio.create_subprocess_exec(*command, start_new_session=True, **kwargs))
        try:
            proc = await asyncio.shield(launch)
        except asyncio.CancelledError:
            proc = await launch
            await self._terminate(proc)
            raise
        self.processes[ident] = proc
        if self.stopping or ident in self.cancel_requested:
            await self._terminate(proc)
            raise ValueError('cancelled_during_spawn')
        return proc

    def _space(self):
        if self.disk_free(safe_path(self.root)) < self.min_free_bytes:
            raise ReplayFailure('insufficient_space')

    @staticmethod
    def _bytes(work):
        amount = 0
        with os.scandir(work) as entries:
            for count, item in enumerate(entries):
                if count >= 200 or item.is_symlink():
                    raise ValueError('unsafe_work_contents')
                if item.is_file(follow_symlinks=False):
                    amount += item.stat(follow_symlinks=False).st_size
        return amount

    async def _operation(self, kind, owner, data, work):
        ident = data['id']
        if kind == 'analysis':
            task = {'urls': data['_urls'], 'handlers': {}}
            for url in data['_urls']:
                job_kind = self._active_kind_of(url)
                if job_kind is not None:
                    task['handlers'][url] = job_kind.handler  # 꺼진 종류의 주소는 수신 프로세스가 extension_disabled로 남긴다
        else:
            task = {key: data.get(key) for key in ('url', 'quality', 'section', 'quality_rule')}
            job_kind = api.find_kind(data.get('kind'))
            if job_kind is None:
                raise ReplayFailure('extension_disabled')
            task['handler'] = job_kind.handler
            if job_kind.context:
                task['context'] = job_kind.context()  # 종류가 넘기는 값(예: 서버가 고른 도구 판). 표준 입력으로만
        if kind == 'analysis':
            contexts = {}
            for url, _handler in task['handlers'].items():
                k = self._active_kind_of(url)
                if k is not None and k.context:
                    contexts[url] = k.context()
            if contexts:
                task['contexts'] = contexts
            platform = next((self._active_kind_of(u).platform for u in task['handlers']), None)
        else:
            platform = job_kind.platform
        task.update(self.task_extras(platform))  # 쿠키·API 주소·연결 수. 표준 입력으로만 넘긴다
        task['ffmpeg'] = self.ffmpeg
        command = self.command + ['--operation', 'analyze' if kind == 'analysis' else 'download', '--work-dir', str(work)]
        if kind == 'job' and self.low_priority():
            command = lowprio.wrap(command)  # 다시보기 수신도 녹화 디스크를 쓰므로 우선순위를 낮춘다(README)
        proc = await self._spawn(ident, command,
                                 stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=65536,
                                 env=_child_env())
        if self.stopping or self._get(kind, owner, ident)['status'] == 'cancelling':
            await self._terminate(proc)
            raise ValueError('cancelled_before_start')
        proc.stdin.write(canonical(task).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        del task
        result = None
        worker_error = None
        reported = {}  # 수신 프로세스가 알린 받은 양과 전체 크기

        async def reader():
            nonlocal result, worker_error
            async for raw in proc.stdout:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError('invalid_worker_event')
                name = event.get('event')
                if name == 'result':
                    if result is not None:
                        raise ValueError('duplicate_result')
                    result = event.get('data')
                elif name == 'error':
                    worker_error = safe_code(event.get('code'))
                elif name == 'refresh' and kind == 'job':
                    # 주소가 만료됐거나 거부돼 다시 분석해 새 주소로 잇는다
                    self._log(ident, '영상 주소를 다시 받아 이어받습니다.', 'warning')
                elif name in ('stage', 'progress', 'resume', 'meta', 'space') and kind == 'job':
                    job = self._get('job', owner, ident)
                    if job['status'] == 'cancelling' or self.stopping:
                        continue
                    stage = event.get('stage')
                    if name == 'stage' and stage in SAFE_STAGES:
                        if stage != job.get('stage'):
                            self._log(ident, {'analyzing': '영상 정보를 다시 받았습니다.', 'downloading': '다운로드를 시작했습니다.',
                                              'finalizing': '받은 조각을 영상 파일 하나로 담고 있습니다.'}.get(stage, '단계를 바꿨습니다.'))
                        job['stage'] = stage
                    if name == 'progress' and 'progress' in event:
                        progress = event.get('progress')
                        if progress is None or type(progress) in (int, float) and math.isfinite(progress) and 0 <= progress < 100:
                            job['progress'] = progress
                    if name == 'progress' and 'bytes' in event:
                        amount, total = event.get('bytes'), event.get('total')
                        if type(amount) is int and amount >= 0:
                            reported['bytes'] = amount
                        if type(total) is int and total > 0:
                            reported['total'] = total
                    if name == 'space' and event.get('mode') == 'hls' and type(event.get('estimate')) is int and event['estimate'] > 0:
                        reported['estimate'] = event['estimate']  # 추정한 영상 크기
                        job.update(space_mode='hls', size_estimate=event['estimate'])
                    if name == 'resume':
                        self._resume_event(ident, job, event)
                    if name == 'meta' and job.get('quality') is None:
                        self._meta_event(job, event)
                    job['updated_at'] = now()
                    self._save('job', owner, job)
            return await proc.wait()

        async def monitor():
            samples = deque()
            while True:
                await asyncio.sleep(self.monitor_interval)
                if kind == 'job':
                    # 사용자 일시정지, 또는 멈춰야 하는 입장 상태(전체 정지·여유 공간·녹화 중·시간대 끝)
                    reason = self.suspend_requests.get(ident)
                    if reason is None and api.find_kind(data.get('kind') or 'video') is None:
                        # 받는 중에 그 종류를 쓸 수 없게 됐다(예: 플랫폼을 껐다): 부분 파일을 남기고 멈춘다
                        reason = self.suspend_requests[ident] = EXTENSION_OFF
                    if reason is None:
                        if 'estimate' in reported:  # HLS형은 영상 크기의 두 배가 든다
                            self.reserve[ident] = max(0, 2 * reported['estimate'] - reported.get('bytes', 0))
                        state = self.gate(self.reserve.get(ident, 0))
                        if state['suspend']:
                            reason = self.suspend_requests[ident] = state['reason']
                    if reason is not None:
                        raise JobSuspended(reason)
                    if 'bytes' in reported:
                        amount = reported['bytes']
                    elif (work / 'resume.json').exists():
                        amount = 0  # 미리 잡은 파일 크기는 받은 양이 아니다
                    else:
                        amount = self._bytes(work)
                    job = self._get('job', owner, ident)
                    if job['status'] != 'cancelling' and not self.stopping:
                        moment = time.monotonic()
                        if samples and amount < samples[-1][1]:
                            samples.clear()  # 폴더 크기에서 수신 프로세스가 알린 양으로 바뀌었다(미리 잡은 파일 크기는 받은 양이 아니다)
                        samples.append((moment, amount, job.get('progress')))
                        while len(samples) > 2 and moment - samples[0][0] > SPEED_WINDOW:
                            samples.popleft()
                        speed, eta = estimate(samples, reported.get('total'))
                        job.update(downloaded_bytes=amount, total_bytes=reported.get('total'), speed_bps=speed,
                                   eta_seconds=eta, updated_at=now())
                        self._save('job', owner, job)

        reading = asyncio.create_task(reader())
        monitoring = asyncio.create_task(monitor())
        try:
            finished, _ = await asyncio.wait((reading, monitoring), timeout=self.analysis_timeout if kind == 'analysis' else self.job_timeout, return_when=asyncio.FIRST_COMPLETED)
            if not finished:
                raise ValueError('worker_timeout')
            for finished_task in finished:
                finished_task.result()
            if worker_error is not None:
                raise ReplayFailure(worker_error)
            if reading not in finished or reading.result() != 0 or not isinstance(result, dict):
                raise ReplayFailure('engine_operation_failed')
            return result
        finally:
            await self._terminate(proc)
            for pending in (reading, monitoring):
                pending.cancel()
            await asyncio.gather(reading, monitoring, return_exceptions=True)

    def _resume_event(self, ident, job, event):
        """이어받았는지, 왜 처음부터 받는지 작업과 로그에 적는다."""
        mode = event.get('mode')
        amount = event.get('bytes') if type(event.get('bytes')) is int else 0
        if mode == 'range':
            note = f'받아 둔 {_size(amount)} 다음부터 이어받습니다.'
        elif mode == 'segments':
            count = event.get('count') if type(event.get('count')) is int else 0
            note = f'받아 둔 조각 {count}개({_size(amount)}) 다음부터 이어받습니다.'
        elif mode == 'restart':
            note = RESUME_MESSAGES.get(event.get('reason'), RESUME_MESSAGES['no_state'])
        else:
            return
        job['resume_note'] = note
        self._log(ident, note, 'warning' if mode == 'restart' else 'info')

    @staticmethod
    def _meta_event(job, event):
        """규칙으로 화질을 고른 작업(실패 작업을 재시도할 때): 고른 화질과 파일 이름 틀을 적는다."""
        quality = event.get('quality')
        if isinstance(quality, str) and re.fullmatch('[A-Za-z0-9_.-]{1,100}', quality):
            job['quality'] = quality
            job['quality_label'] = clean_text(event.get('quality_label'), 60)
        hint = event.get('filename_hint')
        if isinstance(hint, str) and Path(hint).name == hint and hint.endswith('.mp4') and not hint.startswith('.'):
            job['filename_hint'] = hint

    @staticmethod
    def _analysis_items(result):
        items = result.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
            raise ValueError('invalid_analysis_result')
        safe = []
        for item in items:
            try:
                p, item_kind = api.parse_any(item.get('url'))
            except (ReplayFailure, TypeError, ValueError):
                p, item_kind = {'url': '', 'kind': '', 'video_id': '', 'platform': ''}, None
            code = item.get('error_code')
            error = (item_kind.message(safe_code(code), ERROR_MESSAGES[safe_code(code)]) if item_kind
                     else ERROR_MESSAGES[safe_code(code)]) if code else None
            qs = item.get('qualities') or []
            if not isinstance(qs, list) or len(qs) > 200:
                raise ValueError('invalid_qualities')
            qualities = []
            for q in qs:
                ident = q.get('id')
                if not isinstance(ident, str) or not re.fullmatch('[A-Za-z0-9_.-]{1,100}', ident):
                    raise ValueError('invalid_quality')
                height = q.get('height') if type(q.get('height')) is int else None
                qualities.append({'id': ident, 'label': clean_text(q.get('label'), 60), 'height': height})
            if not error and not qualities:
                error = ERROR_MESSAGES['not_ready']
            duration = item.get('duration_seconds')
            if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0):
                duration = None
            thumb = item.get('thumbnail')
            if not (isinstance(thumb, str) and item_kind is not None and item_kind.module().thumbnail_ok(thumb)):
                thumb = None
            safe.append({'url': p['url'], 'platform': p.get('platform', ''), 'kind': p.get('kind', ''), 'video_id': p.get('video_id', ''),
                         'title': clean_text(item.get('title'), 300), 'channel': clean_text(item.get('channel'), 100),
                         'open_date': clean_text(item.get('open_date'), 19) if item.get('open_date') else None,
                         'duration_seconds': duration, 'adult': item.get('adult') is True, 'thumbnail': thumb,
                         'mode': item.get('mode') if item.get('mode') in ('dash', 'hls', 'clip', 'split') else None,
                         'qualities': qualities,
                         'supports': {'section': bool((item.get('supports') or {}).get('section'))},
                         'error': error, 'error_code': safe_code(code) if code else None})
        return safe

    async def _validate_output(self, ident, result, work):
        """ffprobe로 영상 스트림과 길이를 확인한다."""
        name = result.get('name')
        if not isinstance(name, str) or Path(name).name != name or name in ('.', '..'):
            raise ValueError('unsafe_result_name')
        source = safe_path(work / name)
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_nlink != 1:
            raise ValueError('invalid_media_file')
        if not shutil.which(self.ffprobe):
            raise ReplayFailure('tool_missing')
        proc = await self._spawn(ident, [self.ffprobe, '-v', 'error', '-show_entries', 'format=duration,format_name:stream=codec_type', '-of', 'json', str(source)],
                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=65536)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), 120)
            if proc.returncode != 0 or len(output) > 65536:
                raise ValueError('invalid_media')
            media = json.loads(output)
            duration = float(media.get('format', {}).get('duration', 0))
            if not math.isfinite(duration) or duration <= 0 or not any(s.get('codec_type') == 'video' for s in media.get('streams', [])):
                raise ValueError('invalid_media')
            containers = media.get('format', {}).get('format_name', '').split(',')
            if not any(x in containers for x in ('mov', 'mp4')):
                raise ValueError('unsupported_media_container')
            return source, duration
        finally:
            await self._terminate(proc)

    def _publish(self, ident, source, hint):
        """다시보기 폴더에 정한 이름으로 둔다. 같은 이름이 있으면 번호를 붙인다. 덮어쓰지 않는다."""
        if not isinstance(hint, str) or Path(hint).name != hint or not hint.endswith('.mp4'):
            hint = f'{ident}.mp4'
        for _ in range(1000):
            target = safe_path(naming.unique_path(self.root, hint))
            self.publishing[ident] = target
            try:
                os.link(source, target, follow_symlinks=False)  # 같은 파일 시스템의 하드 링크: 원자적이고 덮어쓰지 않는다
            except FileExistsError:
                continue
            source.unlink()
            return target
        raise ValueError('output_conflict')

    def _cleanup(self, work):
        try:
            if work.parent == self.work_root and re.fullmatch('[0-9a-f]{32}', work.name):
                if work.is_symlink():
                    work.unlink()
                elif work.exists():
                    shutil.rmtree(safe_path(work))
        except OSError:
            pass

    def _analysis_gate(self, data):
        if self.request_gate is None:
            return None
        kind = next((k for k in (self._active_kind_of(u) for u in data.get('_urls') or []) if k and k.platform), None)
        return self.request_gate(kind.platform) if kind else None

    def _next(self, kind):
        if kind == 'analysis':
            return self.db.execute("SELECT owner,payload FROM records WHERE kind='analysis' AND status='queued' ORDER BY json_extract(payload,'$.updated_at'), rowid LIMIT 1").fetchone()
        # 입장이 닫혀 있으면 새 작업을 시작하지 않는다. 열리면 대기열 순서대로(넣은 시각) 시작한다.
        # 사용자가 아닌 이유로 일시정지한 작업과 재개한 작업(queue)도 같은 순서로 다시 시작한다
        if not self.gate(0)['open']:
            return None
        # 꺼진 확장의 작업은 대기열에 남기고 시작하지 않는다(다시 켜면 이어서 진행한다)
        active = [k.kind for k in api.kinds()]
        if not active:
            return None
        row = self.db.execute(
            "SELECT owner,payload FROM records WHERE kind='job' AND (status='queued' OR (status='paused' AND "
            "COALESCE(json_extract(payload,'$.pause_reason'),'') != 'user')) "
            f"AND COALESCE(json_extract(payload,'$.kind'),'video') IN ({','.join('?' * len(active))}) "
            "ORDER BY COALESCE(json_extract(payload,'$.queued_at'), json_extract(payload,'$.updated_at')), rowid LIMIT 1",
            active).fetchone()
        # 크기를 아는 HLS형(이어받을 작업)은 앞으로 쓸 공간까지 있어야 시작한다. 모르면 0이다
        self.claim_need = self.space_need(json.loads(row['payload'])) if row is not None else 0
        if self.claim_need and not self.gate(self.claim_need)['open']:
            return None
        self.claim_need = 0
        return row

    async def _loop(self, kind):
        while not self.stopping:
            self.wake[kind].clear()
            row = self._next(kind)
            if row is None:
                if kind == 'job':
                    try:  # 입장 상태(시간대·녹화)는 시간에 따라 바뀌므로 깨우지 않아도 다시 본다
                        await asyncio.wait_for(self.wake[kind].wait(), self.poll)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await self.wake[kind].wait()
                continue
            owner, data = row['owner'], json.loads(row['payload'])
            ident = data['id']
            done = self.done[ident] = asyncio.Event()
            work = self.work_root / ident
            keep = False
            try:
                # 합치기에 실패해 남긴 두 파일(kept_parts)도 다음 시도가 이어 쓴다(받지 않고 합치기부터)
                resuming = kind == 'job' and (data['status'] == 'paused' or data.get('kept_parts')) and has_prior(work)
                if not resuming:
                    # 지난 시도가 남긴 부분 파일을 쓰지 않는다
                    self._cleanup(work)
                    safe_path(work).mkdir(mode=0o700)
                self.held.discard(ident)
                data.update(status='running', updated_at=now())
                if kind == 'job':
                    data['kept_parts'] = None
                    data.update(stage='analyzing', pause_reason=None, paused_at=None, resume_note=None, speed_bps=0, eta_seconds=None)
                    self._log(ident, '이어받기를 준비합니다. 전용 작업 프로세스를 시작했습니다.' if resuming else '전용 작업 프로세스를 시작했습니다.')
                self._save(kind, owner, data)
                gate = self._analysis_gate(data) if kind == 'analysis' else None
                if gate is not None:
                    async with gate.slot(2):  # 분석은 감시 확인 뒤(core.gate.EXTRA)에 그 플랫폼 창구를 쓴다
                        result = await self._operation(kind, owner, data, work)
                else:
                    result = await self._operation(kind, owner, data, work)
                self.suspend_requests.pop(ident, None)  # 끝난 뒤에 온 일시정지 요청은 버린다
                data = self._get(kind, owner, ident)
                if self.stopping or data['status'] == 'cancelling':
                    raise ValueError('cancelled')
                if kind == 'analysis':
                    data.update(status='completed', items=self._analysis_items(result))
                    if data.get('_auto'):
                        data['updated_at'] = now()
                        self._save(kind, owner, data)
                        self._auto_jobs(owner, data)
                        data['_auto'] = None
                else:
                    data.update(status='finalizing', stage='validating', updated_at=now())
                    self._save(kind, owner, data)
                    self._log(ident, '받은 파일을 검증하고 있습니다.')
                    source, duration = await self._validate_output(ident, result, work)
                    data = self._get(kind, owner, ident)
                    if self.stopping or data['status'] == 'cancelling':
                        raise ValueError('cancelled')
                    self._space()
                    target = self._publish(ident, source, data.get('filename_hint'))
                    data.update(status='completed', stage='completed', completed_at=now(), filename=target.name, can_download=True,
                                progress=100, downloaded_bytes=target.stat().st_size, speed_bps=0, eta_seconds=None,
                                partial_bytes=None, media_seconds=round(duration, 3))
                    with self.db:
                        self._remember(data)
                    self._log(ident, '검증을 마친 영상 파일을 저장했습니다.')
                data['updated_at'] = now()
                self._save(kind, owner, data)
            except (Exception, asyncio.CancelledError) as failure:
                await self._terminate(self.processes.get(ident))
                data = self._get(kind, owner, ident)
                reason = self.suspend_requests.pop(ident, None) if kind == 'job' else None
                if reason is None and isinstance(failure, JobSuspended):
                    reason = failure.reason
                if reason is not None and data['status'] == 'running':
                    # 일시정지: 부분 파일을 남기고 이유를 적는다. 취소가 함께 왔으면 취소가 이긴다
                    keep = True
                    partial = await asyncio.to_thread(self._partial_size, work)
                    data.update(status='paused', stage='paused', pause_reason=reason, paused_at=now(), updated_at=now(),
                                speed_bps=0, eta_seconds=None, partial_bytes=partial, error=None, error_code=None)
                    self.held.add(ident)
                    self._save(kind, owner, data)
                    self._log(ident, f"일시정지했습니다({adm.REASON_LABELS.get(reason, reason)}). 받은 부분({_size(partial)})을 남겼습니다.")
                else:
                    status = 'interrupted' if self.stopping else 'cancelled' if data['status'] == 'cancelling' else 'failed'
                    code = safe_code(classify_exception(failure))
                    owner_kind = api.registered_kind(data.get('kind')) if kind == 'job' else None
                    message = ('서버 종료로 작업이 중단되었습니다.' if status == 'interrupted' else
                               owner_kind.message(code, ERROR_MESSAGES[code]) if owner_kind else ERROR_MESSAGES[code])
                    if kind == 'job' and status == 'failed' and code == 'merge_failed':
                        keep = True  # 받은 두 파일을 남긴다. 재시도하면 합치기부터 한다
                        data['kept_parts'] = True
                    data.update(status=status, updated_at=now(), error=None if status == 'cancelled' else message,
                                error_code=code if status == 'failed' else None)
                    if kind == 'job':
                        data.update(stage=status, speed_bps=0, eta_seconds=None, can_download=False)
                        self._log(ident, {'interrupted': '서버 종료로 작업이 중단되었습니다.', 'cancelled': '작업을 취소했습니다.'}.get(status, message), 'info' if status == 'cancelled' else 'error')
                    if kind == 'analysis' and data.get('_auto') and status in ('failed', 'interrupted'):
                        with self.db:  # 고른 영상이 조용히 빠지지 않게 한다
                            self._auto_fail(owner, data, status, code if status == 'failed' else None, message)
                    self._save(kind, owner, data)
            finally:
                self.processes.pop(ident, None)
                self.reserve.pop(ident, None)
                if not keep:
                    self._cleanup(work)
                self.publishing.pop(ident, None)
                done.set()
                self.done.pop(ident, None)
                self.cancel_requested.discard(ident)
            if kind == 'analysis':
                # 종류가 정한 분석 간격(예: 유튜브는 앞 분석이 끝나고 5초 뒤)
                gap = max([0.0] + [k.analysis_gap for k in (self._active_kind_of(u) for u in data.get('_urls') or []) if k])
                if gap > 0 and not self.stopping:
                    await asyncio.sleep(gap)

    async def close(self):
        if self.close_task is None:
            self.stopping = True
            self.close_task = asyncio.create_task(self._close())
        await asyncio.shield(self.close_task)

    async def _close(self):
        if self.closed:
            return
        self.stopping = True
        for event in self.wake.values():
            event.set()
        await asyncio.gather(*(self._terminate(proc) for proc in list(self.processes.values())), return_exceptions=True)
        if self.tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*self.tasks), 10)
            except asyncio.TimeoutError:
                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
        with self.db:
            for row in self.db.execute("SELECT kind,owner,payload FROM records WHERE status NOT IN ('completed','failed','cancelled','interrupted','paused')").fetchall():
                data = json.loads(row['payload'])
                data.update(status='interrupted', updated_at=now(), error='서버 종료로 작업이 중단되었습니다.')
                if row['kind'] == 'job':
                    data.update(stage='interrupted', speed_bps=0, eta_seconds=None, can_download=False)
                self.store.save(row['kind'], row['owner'], data)
                if row['kind'] == 'analysis' and data.get('_auto'):
                    self._auto_fail(row['owner'], data, 'interrupted', None, '서버 종료로 작업이 중단되었습니다.')
        self.store.close()
        self.closed = True


def _size(n):
    """로그 문장용 크기 표시."""
    n = float(n or 0)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if n < 1024 or unit == 'GiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def _child_env():
    from ...core import childenv

    return childenv.child_env(str(Path(__file__).resolve().parent.parent.parent))  # 필요한 변수만 넘긴다
