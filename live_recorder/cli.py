"""명령줄.

    python -m live_recorder --data-dir D --record-root R serve
    python -m live_recorder ... channel add <채널ID> --name 이름 --dir 폴더
    python -m live_recorder ... start|stop <채널ID>
    python -m live_recorder ... status | sessions | settings show|set | cookie set|status|clear
"""

from __future__ import annotations

import argparse
from pathlib import Path
import asyncio
import getpass
import json
import sys
import time

import os

from .core import apikeys, logs, secretstore
from .core import store as st
from .core.config import AppConfig, ConfigError, Settings
from .platforms import credentials, registry
from .recording.engine import Engine, InstanceLock, StartupError


def _store(cfg: AppConfig) -> st.Store:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return st.Store(cfg.db_path, cfg.record_root, cfg.telegram_path)


def _control(cfg: AppConfig, store: st.Store, channel_id: str, action: str, wait: float = 15.0) -> int:
    if store.get_channel(channel_id) is None:
        print("없는 채널입니다", file=sys.stderr)
        return 1
    if not InstanceLock.held(cfg.lock_path):
        print("서버(serve)가 돌고 있지 않습니다", file=sys.stderr)
        return 1
    cid = store.push_control(channel_id, action)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        row = store.get_control(cid)
        if row and row["done_at"]:
            print(row["result"])
            return 0
        time.sleep(0.2)
    print("요청을 넣었지만 서버가 아직 답하지 않았습니다", file=sys.stderr)
    return 1


def _prepare(cfg: AppConfig) -> int | None:
    """저장 위치를 만들고(없으면, 0700) 꼭 필요한 것을 본다. 모자라면 까닭을 알리고 뜨지 않는다."""
    from . import doctor

    for path in (cfg.data_dir, cfg.log_dir, cfg.record_root):
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # 아래 점검이 까닭을 알린다
    problems = doctor.startup_problems(cfg)
    if problems:
        print("기동 실패: 꼭 필요한 것이 없습니다. `vod-recorder doctor`로 자세히 볼 수 있습니다.", file=sys.stderr)
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        return 2
    return None


def cmd_serve(cfg: AppConfig, args: argparse.Namespace) -> int:
    failed = _prepare(cfg)
    if failed is not None:
        return failed
    logs.setup(cfg.log_dir, console=not args.quiet)
    try:
        asyncio.run(Engine(cfg).serve())
    except (StartupError, st.StoreError) as e:
        print(f"기동 실패: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_web(cfg: AppConfig, args: argparse.Namespace) -> int:
    from . import web

    failed = _prepare(cfg)
    if failed is not None:
        return failed
    logs.setup(cfg.log_dir, console=not args.quiet)
    if not secretstore.account_exists(cfg.account_path) and not os.environ.get("LIVE_RECORDER_ADMIN_USER"):
        # 계정이 없으면 보기 전용 모드로 뜬다. 녹화는 그대로 한다
        print("관리자 계정이 없어 보기 전용 모드로 뜹니다. 웹의 첫 설정 화면(/setup)에서 설정 코드로 계정을 만드십시오", file=sys.stderr)
    try:
        web.run(cfg, args.host, args.port)
    except (StartupError, st.StoreError, secretstore.AccountError, ConfigError) as e:
        print(f"기동 실패: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_doctor(cfg: AppConfig, args: argparse.Namespace) -> int:
    """실행 환경 점검. 꼭 필요한 것이 모자라면 1."""
    from . import PROGRAM_NAME, __version__, doctor

    checks, code = doctor.run(cfg, args.host, args.port)
    print(f"{PROGRAM_NAME} {__version__} 점검")
    for c in checks:
        print(c.line())
    print("꼭 필요한 것이 모두 있습니다." if code == 0 else "꼭 필요한 것이 모자랍니다. 위의 고치는 방법을 따라 주십시오.")
    return code


def cmd_compare(cfg: AppConfig | None, args: argparse.Namespace) -> int:
    """다른 녹화 프로그램이 만든 파일과 새 녹화 파일 비교. 두 파일을 읽기만 한다."""
    from . import compare

    ffprobe = cfg.ffprobe if cfg else os.environ.get("LIVE_RECORDER_FFPROBE", "ffprobe")
    try:
        result = compare.compare(Path(args.baseline), Path(args.new), cfg, ffprobe, args.rewind, args.full)
    except (OSError, ValueError) as e:
        print(f"비교 실패: {e}", file=sys.stderr)
        return 2
    print(compare.to_json(result) if args.json else compare.render(result))
    return 0 if result["pass"] else 1


def cmd_diagnostics(cfg: AppConfig, args: argparse.Namespace) -> int:
    """진단팩을 로그 폴더 아래에 만든다. 외부로 보내지 않는다."""
    from . import diagnostics

    path = diagnostics.create(cfg, args.symptom)
    print(f"진단팩: {path} ({path.stat().st_size} 바이트)")
    return 0


def cmd_migrate(cfg: AppConfig, args: argparse.Namespace) -> int:
    """MIG. 기존 설정 이전. 기본은 미리 보기다."""
    from .migrate import MigrationError, run

    try:
        print(run(cfg, args.source, args.map or [], args.apply))
    except MigrationError as e:
        print(f"멈춤: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_account(cfg: AppConfig, args: argparse.Namespace) -> int:
    """비밀번호는 인자로 받지 않는다. 입력을 받기 전에 규칙을 알려 준다."""
    if args.action in ("create", "passwd"):
        print(secretstore.PASSWORD_RULE, file=sys.stderr)
    if args.action == "create":
        secretstore.check_username(args.username)  # 비밀번호를 받기 전에 이름부터 확인한다
        if secretstore.account_exists(cfg.account_path):
            raise secretstore.AccountError("관리자 계정이 이미 있습니다")
        if sys.stdin.isatty():
            password = getpass.getpass("비밀번호: ")
            if getpass.getpass("한 번 더: ") != password:
                raise ConfigError("비밀번호가 서로 다릅니다")
        else:
            password = sys.stdin.readline().rstrip("\n")
        secretstore.create_account(cfg.account_path, args.username, password)
        secretstore.clear_setup_code(cfg.setup_code_path)  # 계정이 생기면 첫 설정 코드는 지운다
        print("관리자 계정을 만들었습니다")
    elif args.action == "passwd":
        if sys.stdin.isatty():
            password = getpass.getpass("새 비밀번호: ")
            if getpass.getpass("한 번 더: ") != password:
                raise ConfigError("비밀번호가 서로 다릅니다")
        else:
            password = sys.stdin.readline().rstrip("\n")
        secretstore.change_password(cfg.account_path, password)
        print("비밀번호를 바꿨습니다. 열려 있던 웹 세션은 모두 끊깁니다")
    elif args.action == "delete":
        secretstore.remove(cfg.account_path)
        revoked = apikeys.KeyStore(cfg.api_keys_path).revoke_all()  # 계정을 지우면 접근 키도 모두 폐기한다
        print("관리자 계정을 지웠습니다. 웹은 보기 전용이 되고, 첫 설정 화면(/setup)에서 새 계정을 만들 수 있습니다. "
              "설정 코드는 `account setup-code`로 볼 수 있습니다")
        if revoked:
            print(f"접근 키 {revoked}개도 모두 폐기했습니다")
    elif args.action == "setup-code":
        # 서버에 접근할 수 있는 사람만 실행할 수 있다. 코드는 이 출력과 컨테이너 로그에만 나온다
        if not secretstore.setup_code_enabled():
            secretstore.clear_setup_code(cfg.setup_code_path)
            print(secretstore.SETUP_CODE_DISABLED, file=sys.stderr)
            return 1
        code, _ = secretstore.ensure_setup_code(cfg.setup_code_path, cfg.account_path)
        if code is None:
            print("관리자 계정이 이미 있어 설정 코드가 없습니다", file=sys.stderr)
            return 1
        print(f"첫 설정 코드: {code}")
        print("웹의 첫 설정 화면(/setup)에 넣으십시오. 계정을 만들면 코드는 지워집니다", file=sys.stderr)
    else:
        name = secretstore.account_name(cfg.account_path)
        print(f"관리자 계정: {name if name else '없음'}")
    return 0


def cmd_channel(cfg: AppConfig, args: argparse.Namespace) -> int:
    store = _store(cfg)
    if args.action == "add":
        ch = store.add_channel(args.channel_id, args.name, args.dir, quality=args.quality, extension=args.ext, auto_record=not args.no_auto,
                               platform=args.platform)
        print(f"추가: {ch.channel_id} {ch.name} → {ch.output_dir}")
    elif args.action == "list":
        for ch in store.list_channels():
            print(f"{ch.channel_id}  {ch.name}  {ch.quality} {ch.extension}  자동={'켬' if ch.auto_record else '끔'}  {ch.output_dir}")
    elif args.action == "set":
        changes = {}
        for key, attr in (("name", "name"), ("output_dir", "dir"), ("quality", "quality"), ("extension", "ext")):
            if getattr(args, attr) is not None:
                changes[key] = getattr(args, attr)
        if args.auto is not None:
            changes["auto_record"] = args.auto == "on"
        ch = store.update_channel(args.channel_id, **changes)
        print(f"수정: {ch.channel_id} 자동={'켬' if ch.auto_record else '끔'}")
    elif args.action == "remove":
        if InstanceLock.held(cfg.lock_path):
            return _control(cfg, store, args.channel_id, "remove", wait=60)
        store.delete_channel(args.channel_id)
        print("삭제함")
    return 0


def cmd_settings(cfg: AppConfig, args: argparse.Namespace) -> int:
    store = _store(cfg)
    if args.action == "set":
        changes = {}
        for item in args.pairs:
            if "=" not in item:
                raise ConfigError(f"키=값 형식이 아닙니다: {item}")
            key, value = item.split("=", 1)
            changes[key] = value
        store.set_settings(changes)
    current = store.get_settings()
    for key in Settings.keys():
        print(f"{key} = {getattr(current, key)}")
    return 0


def cmd_cookie(cfg: AppConfig, args: argparse.Namespace) -> int:
    pid = args.platform or registry.DEFAULT
    if registry.find(pid) is None:
        raise ConfigError(f"모르는 플랫폼입니다: {pid}")
    keys = credentials.names(pid)
    if args.action == "set":
        # 값은 인자로 받지 않는다. 터미널이면 숨겨서 묻고, 아니면 표준 입력의 JSON을 읽는다.
        if sys.stdin.isatty():
            values = {k: getpass.getpass(f"{k}: ") for k in keys}
        else:
            values = json.loads(sys.stdin.read())
            if not isinstance(values, dict):
                raise ConfigError("쿠키 입력은 JSON 객체여야 합니다")
        credentials.save(cfg, pid, values)
    elif args.action == "clear":
        credentials.clear(cfg, pid)
    current = credentials.load(cfg, pid)
    for key in keys:
        print(f"{key}: {'설정됨' if current.get(key) else '없음'}")  # 값은 다시 내보내지 않는다
    return 0


def cmd_status(cfg: AppConfig, args: argparse.Namespace) -> int:
    store = _store(cfg)
    running = InstanceLock.held(cfg.lock_path)
    rows = []
    for ch in store.list_channels():
        state = store.get_state(ch.channel_id, ch.platform)
        session = store.get_session(state["session_id"]) if state.get("session_id") else None
        rows.append({
            "channel_id": ch.channel_id, "platform": ch.platform, "name": ch.name, "auto_record": ch.auto_record,
            "state": store.display_state(state) if running else st.STOPPED,
            "live_title": state.get("live_title") if session else None,
            "file": session["part_path"] if session else None,
            "started_at": session["started_at"] if session else None,
            "error": state.get("error"), "error_flag": bool(state.get("error_flag")),
            "last_check": state.get("last_check"), "last_check_at": state.get("last_check_at"),
        })
    if args.json:
        print(json.dumps({"server_running": running, "channels": rows}, ensure_ascii=False, indent=2))
        return 0
    print(f"서버: {'실행 중' if running else '정지'}")
    for r in rows:
        line = f"{r['name']} [{st.STATE_LABELS.get(r['state'], r['state'])}] 자동={'켬' if r['auto_record'] else '끔'}"
        if r["file"]:
            line += f" 파일={r['file']}"
        if r["error"]:
            line += f" 오류{'(표시)' if r['error_flag'] else ''}={r['error']}"
        print(line)
    return 0


def cmd_sessions(cfg: AppConfig, args: argparse.Namespace) -> int:
    store = _store(cfg)
    rows = store.list_sessions(args.channel_id, args.limit)
    if args.json:
        for r in rows:
            r.pop("pid", None)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for r in rows:
        print(f"#{r['id']} {r['started_at']} → {r['ended_at'] or '진행 중'} 사유={r['end_reason']} rc={r['exit_code']} "
              f"수신={r['source']} 크기={r['bytes']} 마무리={r['finalize_status']} "
              f"파일={r['output_path'] or r['part_path'] or '-'}"
              + (f" ({r['end_detail']})" if r["end_detail"] else "") + (f" [{r['note']}]" if r["note"] else ""))
    return 0


def cmd_refinalize(cfg: AppConfig, args: argparse.Namespace) -> int:
    """마무리 실패로 남은 `.raw.*` 원본을 세션 번호로 다시 마무리한다.
    서버가 돌고 있으면 서버에 맡기고, 아니면 이 프로세스가 잠금을 잡고 직접 한다."""
    from .recording.finalize import Finalizer, retry_source

    store = _store(cfg)
    session = store.get_session(args.session_id)
    if session is None:
        print("없는 세션입니다", file=sys.stderr)
        return 1
    if retry_source(session) is None:
        print("다시 마무리할 원본이 없습니다(마무리 실패 기록과 .raw 원본이 있어야 합니다)", file=sys.stderr)
        return 1
    if InstanceLock.held(cfg.lock_path):
        return _control(cfg, store, session["channel_id"], f"refinalize:{args.session_id}")
    lock = InstanceLock(cfg.lock_path)
    lock.acquire()
    try:
        status = asyncio.run(Finalizer(cfg, store).finalize_session(args.session_id, retry=True))
    finally:
        lock.release()
    row = store.get_session(args.session_id) or {}
    print(f"마무리={status} {row.get('finalize_detail') or ''}".rstrip())
    return 0 if status == "done" else 1


def build_parser() -> argparse.ArgumentParser:
    from . import PROGRAM_NAME, __version__

    p = argparse.ArgumentParser(prog="vod-recorder", description=f"{PROGRAM_NAME}: 실시간 녹화와 다시보기·영상 받기")
    p.add_argument("--version", action="version", version=f"{PROGRAM_NAME} {__version__}")
    p.add_argument("--data-dir", help="상태·세션·쿠키 폴더 (LIVE_RECORDER_DATA_DIR, 없으면 운영체제 관례 위치)")
    p.add_argument("--record-root", help="녹화 루트 (LIVE_RECORDER_RECORD_ROOT, 없으면 운영체제 관례 위치)")
    p.add_argument("--log-dir", help="로그 폴더 (LIVE_RECORDER_LOG_DIR, 없으면 자료 폴더 아래 logs나 운영체제 관례 위치)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="감시·녹화 서버를 돌립니다")
    s.add_argument("--quiet", action="store_true", help="콘솔 로그를 끕니다")
    s.set_defaults(fn=cmd_serve)

    dr = sub.add_parser("doctor", help="실행 환경을 점검합니다(ffmpeg, 파이썬, 저장 위치, 포트)")
    dr.add_argument("--host", default=os.environ.get("LIVE_RECORDER_HOST", "127.0.0.1"))
    dr.add_argument("--port", type=int, default=int(os.environ.get("LIVE_RECORDER_PORT", "8080")))
    dr.set_defaults(fn=cmd_doctor)

    w = sub.add_parser("web", help="웹 화면과 함께 서버를 돌립니다")
    w.add_argument("--host", default=os.environ.get("LIVE_RECORDER_HOST", "127.0.0.1"))
    w.add_argument("--port", type=int, default=int(os.environ.get("LIVE_RECORDER_PORT", "8080")))
    w.add_argument("--quiet", action="store_true")
    w.set_defaults(fn=cmd_web)

    cp = sub.add_parser("compare", help="기준 녹화본과 이 프로그램의 녹화본 비교(읽기만)")
    cp.add_argument("baseline", help="기준 파일(다른 녹화 프로그램이 만든 mp4)")
    cp.add_argument("new", help="새 파일(이 도구가 만든 mp4)")
    cp.add_argument("--rewind", type=float, default=0.0, help="기준 쪽 되감기 초(예: 3600). 첫 시각 추정에 씁니다")
    cp.add_argument("--full", action="store_true", help="영상 패킷을 끝까지 읽어 길이를 잽니다(큰 파일은 오래 걸립니다)")
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(fn=cmd_compare, needs_config=False)

    dg = sub.add_parser("diagnostics", help="진단팩 만들기(로그 폴더 아래, 최근 3개)")
    dg.add_argument("--symptom", default="other", choices=("start", "recording", "postprocessing", "ui", "storage",
                                                          "network", "other"))
    dg.set_defaults(fn=cmd_diagnostics)

    mg = sub.add_parser("migrate", help="기존 설정 가져오기(기본은 미리 보기)")
    mg.add_argument("--from", dest="source", required=True, help="channels.json·config.json·cookie.json·notification.json이 든 폴더")
    mg.add_argument("--map", action="append", metavar="옛경로=새경로", help="옛 컨테이너 경로를 새 경로로 바꿉니다(여러 번)")
    mg.add_argument("--apply", action="store_true", help="실제로 씁니다. 없으면 미리 보기만 합니다")
    mg.set_defaults(fn=cmd_migrate)

    ac = sub.add_parser("account", help="관리자 계정(하나)")
    acsub = ac.add_subparsers(dest="action", required=True)
    acc = acsub.add_parser("create")
    acc.add_argument("username")
    acsub.add_parser("passwd", help="비밀번호 변경(인자로 받지 않습니다)")
    acsub.add_parser("setup-code", help="계정이 없을 때 첫 설정 화면에 넣을 코드를 보여 줍니다")
    acsub.add_parser("status")
    acsub.add_parser("delete")
    ac.set_defaults(fn=cmd_account)

    c = sub.add_parser("channel", help="채널 관리")
    csub = c.add_subparsers(dest="action", required=True)
    a = csub.add_parser("add")
    a.add_argument("channel_id")
    a.add_argument("--name", required=True)
    a.add_argument("--dir", help="저장 폴더(녹화 루트 아래). 없으면 녹화 루트 아래 플랫폼 식별자 폴더")
    a.add_argument("--quality", default="best")
    a.add_argument("--ext", default=".mp4")
    a.add_argument("--no-auto", action="store_true")
    a.add_argument("--platform", help="플랫폼(기본: 치지직)")
    csub.add_parser("list")
    e = csub.add_parser("set")
    e.add_argument("channel_id")
    e.add_argument("--name")
    e.add_argument("--dir")
    e.add_argument("--quality")
    e.add_argument("--ext")
    e.add_argument("--auto", choices=("on", "off"))
    r = csub.add_parser("remove")
    r.add_argument("channel_id")
    c.set_defaults(fn=cmd_channel)

    for name, text in (("start", "지금 방송 중이면 한 번 녹화합니다"), ("stop", "진행 중인 녹화를 중지합니다")):
        x = sub.add_parser(name, help=text)
        x.add_argument("channel_id")
        x.set_defaults(fn=lambda cfg, args, _n=name: _control(cfg, _store(cfg), args.channel_id, _n))

    g = sub.add_parser("settings", help="설정 보기·바꾸기")
    gsub = g.add_subparsers(dest="action", required=True)
    gsub.add_parser("show")
    gs = gsub.add_parser("set")
    gs.add_argument("pairs", nargs="+", metavar="키=값")
    g.set_defaults(fn=cmd_settings)

    k = sub.add_parser("cookie", help="치지직 쿠키(값은 표준 입력으로만 받습니다)")
    k.add_argument("action", choices=("set", "status", "clear"))
    k.add_argument("--platform", help="플랫폼(기본: 치지직)")
    k.set_defaults(fn=cmd_cookie)

    t = sub.add_parser("status", help="채널 상태")
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_status)

    rf = sub.add_parser("refinalize", help="마무리 실패로 남은 원본을 다시 마무리합니다")
    rf.add_argument("session_id", type=int)
    rf.set_defaults(fn=cmd_refinalize)

    n = sub.add_parser("sessions", help="녹화 세션 기록")
    n.add_argument("channel_id", nargs="?")
    n.add_argument("--limit", type=int, default=20)
    n.add_argument("--json", action="store_true")
    n.set_defaults(fn=cmd_sessions)
    return p


OWNER_ONLY_UMASK = 0o077


def main(argv: list[str] | None = None) -> int:
    # 이 프로세스와 자식(수신기, ffmpeg, 다시보기 작업)이 만드는 파일·폴더를 소유자 전용(0600·0700)으로 한다.
    os.umask(OWNER_ONLY_UMASK)
    args = build_parser().parse_args(argv)
    try:
        try:
            cfg = AppConfig.from_env(args.data_dir, args.record_root, args.log_dir)
        except ConfigError:
            if getattr(args, "needs_config", True):
                raise
            cfg = None  # compare는 데이터 폴더 없이도 돈다(새 파일의 첫 시각만 알 수 없다)
        return int(args.fn(cfg, args) or 0)
    except (ConfigError, st.ValidationError, secretstore.AccountError) as e:
        print(f"거부: {e}", file=sys.stderr)
        return 1
    except st.StoreError as e:
        print(f"저장소 오류: {e}", file=sys.stderr)
        return 2
