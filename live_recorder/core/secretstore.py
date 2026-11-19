"""비밀 저장소. 쿠키·계정·텔레그램 값을 권한 0600 파일에 따로 둔다.

설정·채널 저장소(SQLite), 로그, 진단 자료에는 이 값을 넣지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # 파일 잠금이 없는 체제에서는 프로세스 안 잠금만
    fcntl = None  # type: ignore[assignment]


class SecretFileError(ValueError):
    """비밀 파일을 읽을 수 없다(JSON이 깨짐). 문장에 값은 넣지 않는다."""


_LOCK = threading.RLock()
_HELD = threading.local()


@contextmanager
def file_lock(path: Path):
    """비밀 파일의 읽고-고쳐-쓰기 잠금. 같은 프로세스 안(스레드)과 다른 프로세스(CLI) 사이 모두.
    잠금 파일은 따로 만들지 않고 비밀 폴더에 flock을 건다. 같은 스레드에서 겹쳐 잡아도 된다(바깥에서 한 번만 건다).
    비밀 파일은 작고 드물게 쓰므로 프로세스 안에서는 잠금 하나를 함께 쓴다."""
    with _LOCK:
        depth = getattr(_HELD, "depth", 0)
        dfd = None
        if depth == 0:
            try:
                dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            except OSError:
                dfd = None
            if dfd is not None and fcntl is not None:
                fcntl.flock(dfd, fcntl.LOCK_EX)
        _HELD.depth = depth + 1
        try:
            yield
        finally:
            _HELD.depth = depth
            if dfd is not None:
                os.close(dfd)  # 닫으면 flock도 풀린다


def read_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeDecodeError):
        raise SecretFileError(f"비밀 파일 {path.name}을 읽을 수 없습니다(내용이 깨졌습니다). "
                              f"백업에서 되살리거나 파일을 지운 뒤 다시 설정해 주십시오") from None
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    """무작위 임시 이름(O_EXCL|O_NOFOLLOW)에 쓴 뒤 바꿔 넣는다. 같은 파일 쓰기는 잠금으로 한 번에 하나."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with file_lock(path):
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---- 관리자 계정 ----
SCRYPT = {"n": 2**15, "r": 8, "p": 1}
MIN_PASSWORD = 8
MAX_PASSWORD = 1024  # 상한이 아니라 처리 부담을 막는 제한
PASSWORD_RULE = f"비밀번호는 {MIN_PASSWORD}자 이상입니다(최대 {MAX_PASSWORD}자)"


class AccountError(ValueError):
    pass


def _hash(password: str, salt: bytes, params: dict[str, int]) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=params["n"], r=params["r"], p=params["p"],
                          maxmem=128 * 1024 * 1024, dklen=32)


def account_exists(path: Path) -> bool:
    return bool(read_json(path).get("username"))


def account_name(path: Path) -> str | None:
    return read_json(path).get("username") or None


def _check_password(password: str) -> None:
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise AccountError(PASSWORD_RULE)


def check_username(username: str) -> str:
    username = (username or "").strip()
    if not username or len(username) > 64 or any(c.isspace() for c in username):
        raise AccountError("사용자 이름은 공백 없는 1~64자입니다")
    return username


def account_generation(path: Path) -> str | None:
    """비밀번호를 바꿀 때마다 바뀌는 값. 세션은 로그인 때의 값과 다르면 끊긴다."""
    data = read_json(path)
    return data.get("salt") if data.get("username") else None


def change_password(path: Path, new_password: str) -> None:
    """사용자 이름은 그대로 두고 비밀번호만 바꾼다. 소금도 새로 만들어 세대 값이 바뀐다."""
    _check_password(new_password)
    salt = secrets.token_bytes(16)
    digest = _hash(new_password, salt, SCRYPT).hex()
    with file_lock(path):  # 읽고-고쳐-쓰기를 한 번에
        data = read_json(path)
        if not data.get("username"):
            raise AccountError("관리자 계정이 없습니다")
        data.update(salt=salt.hex(), hash=digest, scrypt=SCRYPT)
        write_json(path, data)


def create_account(path: Path, username: str, password: str) -> None:
    """계정은 하나다. 이미 있으면 만들지 않는다.

    임시 파일을 다 쓴 뒤 `link()`로 계정 파일 이름을 붙인다. 이름이 이미 있으면 파일 시스템이 거부하므로
    웹 첫 설정·CLI·환경변수가 동시에 만들어도 계정은 하나만 생긴다.
    """
    username = check_username(username)
    _check_password(password)
    if account_exists(path):
        raise AccountError("관리자 계정이 이미 있습니다")
    salt = secrets.token_bytes(16)
    data = {"username": username, "salt": salt.hex(), "hash": _hash(password, salt, SCRYPT).hex(), "scrypt": SCRYPT}
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            # 비어 있거나 깨진 계정 파일(이름이 없는 것)은 계정이 없는 것으로 본다. 그때만 바꿔 넣는다
            if account_exists(path):
                raise AccountError("관리자 계정이 이미 있습니다") from None
            os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


# ---- 첫 설정 코드 ----
SETUP_CODE_BYTES = 20  # 160비트


def format_code(raw: bytes) -> str:
    text = base64.b32encode(raw).decode().rstrip("=")
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def normalize_code(value: object) -> str:
    if not isinstance(value, str) or len(value) > 200:
        return ""
    return "".join(c for c in value.upper() if c.isalnum())


SETUP_CODE_ENV = "LIVE_RECORDER_SETUP_CODE"
SETUP_CODE_DISABLED = "설정 코드를 끈 상태입니다"
SETUP_CODE_WARNING = ("설정 코드를 끈 상태입니다. 이 주소에 접속할 수 있는 누구나 관리자 계정을 만들 수 있습니다. "
                      "계정을 먼저 만드십시오")


def setup_code_enabled(env: dict | None = None) -> bool:
    """환경변수가 `off`(대소문자 무시)면 첫 설정에서 설정 코드를 받지 않는다. 그 밖은 켬(기본)."""
    value = (os.environ if env is None else env).get(SETUP_CODE_ENV, "")
    return value.strip().lower() != "off"


def read_setup_code(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="ascii").strip()
    except (FileNotFoundError, UnicodeDecodeError):
        return None
    return text or None


def ensure_setup_code(path: Path, account_path: Path) -> tuple[str | None, bool]:
    if not setup_code_enabled():
        clear_setup_code(path)  # 꺼져 있으면 만들지도 두지도 않는다(있던 파일은 지운다)
        return None, False
    return _ensure_setup_code(path, account_path)


def _ensure_setup_code(path: Path, account_path: Path) -> tuple[str | None, bool]:
    """계정이 없으면 설정 코드를 돌려준다. 없으면 만든다(0600, 덮어쓰지 않음). (코드, 새로 만들었나).
    계정이 있으면 남은 코드 파일을 지우고 (None, False)."""
    if account_exists(account_path):
        clear_setup_code(path)
        return None, False
    current = read_setup_code(path)
    if current:
        return current, False
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    code = format_code(secrets.token_bytes(SETUP_CODE_BYTES))
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return read_setup_code(path), False  # 다른 프로세스가 먼저 만들었다
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(code + "\n")
        f.flush()
        os.fsync(f.fileno())
    return code, True


def setup_code_matches(path: Path, supplied: object) -> bool:
    """시간이 일정한 비교. 코드 파일이 없으면 언제나 거짓이다."""
    want = normalize_code(read_setup_code(path) or "")
    got = normalize_code(supplied)
    return bool(want) and hmac.compare_digest(want.encode(), got.encode())


def clear_setup_code(path: Path) -> None:
    remove(path)


def verify_account(path: Path, username: str, password: str) -> bool:
    data = read_json(path)
    try:
        salt = bytes.fromhex(data["salt"])
        want = bytes.fromhex(data["hash"])
        params = data.get("scrypt") or SCRYPT
        stored_user = str(data["username"])
    except (KeyError, ValueError, TypeError):
        # 계정이 없어도 같은 시간을 쓴다
        _hash(password or "", b"\0" * 16, SCRYPT)
        return False
    got = _hash(password or "", salt, params)
    user_ok = hmac.compare_digest(stored_user.encode(), (username or "").encode())
    return hmac.compare_digest(got, want) and user_ok


# ---- 텔레그램 ----
def telegram_load(path: Path) -> dict[str, str]:
    data = read_json(path)
    return {k: str(data[k]) for k in ("bot_token", "chat_id") if data.get(k)}


def telegram_save(path: Path, bot_token: str | None = None, chat_id: str | None = None) -> dict[str, str]:
    """넘긴 값만 바꾼다. 빈 문자열은 그 값을 지운다. 읽고-고쳐-쓰기를 잠금 하나로 감싼다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        return _telegram_save(path, bot_token, chat_id)


def _telegram_save(path: Path, bot_token: str | None, chat_id: str | None) -> dict[str, str]:
    data = telegram_load(path)
    for key, value in (("bot_token", bot_token), ("chat_id", chat_id)):
        if value is None:
            continue
        value = value.strip()
        if any(c.isspace() or c in '/?#"' for c in value):
            raise ValueError(f"{key}에 쓸 수 없는 문자가 있습니다")
        if value:
            data[key] = value
        else:
            data.pop(key, None)
    write_json(path, data)
    return data
