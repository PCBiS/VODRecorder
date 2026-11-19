"""플랫폼별 자격 저장소. 권한 0600인 플랫폼별 파일에만 둔다.

파일은 `<데이터 폴더>/secrets/<플랫폼 ID>-cookies.json`이다. 예전 쿠키 파일(같은 폴더의 기본 플랫폼 파일)은
이름이 같아서 그대로 기본 플랫폼의 몫으로 읽힌다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core import secretstore
from . import registry


def path(cfg: Any, platform_id: str | None = None) -> Path:
    return cfg.data_dir / "secrets" / f"{platform_id or registry.DEFAULT}-cookies.json"


def names(platform_id: str | None = None) -> tuple[str, ...]:
    return registry.get(platform_id).credential_spec.names


SAVED_AT = "_saved_at"  # 파일 글 꼴 자격을 저장한 시각(값이 아니다)


def load(cfg: Any, platform_id: str | None = None) -> dict[str, str]:
    data = secretstore.read_json(path(cfg, platform_id))
    return {k: str(data[k]) for k in names(platform_id) if data.get(k)}


def saved_at(cfg: Any, platform_id: str | None = None) -> str | None:
    value = secretstore.read_json(path(cfg, platform_id)).get(SAVED_AT)
    return str(value) if value else None


def save(cfg: Any, platform_id: str | None, values: dict[str, str]) -> None:
    platform = registry.get(platform_id)
    spec = platform.credential_spec
    if spec.kind == "file":
        # 글 전체 하나. 플랫폼이 꼴·크기·도메인·로그인 쿠키를 검사하고 정리한 글만 둔다
        key = spec.names[0]
        raw = str(values.get(key) or "")
        if not raw.strip():
            secretstore.remove(path(cfg, platform_id))
            return
        if spec.max_bytes and len(raw.encode("utf-8")) > spec.max_bytes:
            raise ValueError(f"쿠키 파일이 너무 큽니다({spec.max_bytes // 1024}KB 이하만 받습니다)")
        text = platform.clean_credential_file(raw)
        secretstore.write_json(path(cfg, platform_id), {key: text, SAVED_AT: datetime.now(timezone.utc).isoformat()})
        return
    clean = {}
    for k in spec.names:
        v = str(values.get(k) or "").strip()
        if any(c in v for c in spec.forbidden):
            raise ValueError(f"{k} 값에 쓸 수 없는 문자가 있습니다")
        if v:
            clean[k] = v
    if spec.summary and clean:
        clean[SAVED_AT] = datetime.now(timezone.utc).isoformat()  # 화면에 저장 시각을 보인다
    secretstore.write_json(path(cfg, platform_id), clean)


def clear(cfg: Any, platform_id: str | None = None) -> None:
    secretstore.remove(path(cfg, platform_id))


def exists(cfg: Any) -> bool:
    """진단 요약용. 어느 플랫폼이든 자격 파일이 있으면 참."""
    return any(path(cfg, p.id).exists() for p in registry.all_platforms())


def cookie_header(spec_names: tuple[str, ...], values: dict[str, str] | None) -> str | None:
    """값이 있는 것만 정해진 순서로 넣는다."""
    values = values or {}
    parts = [f"{k}={values[k]}" for k in spec_names if values.get(k)]
    return "; ".join(parts) if parts else None
