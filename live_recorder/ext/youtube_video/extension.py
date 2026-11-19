"""youtube_video 확장: 유튜브 영상 주소를 받는다. downloads 확장이 필요하고 유튜브가 켜져 있어야 쓴다.

downloads는 공개 접점(api: 작업 종류 등록, media: 받는 방법)으로만 쓰고 replay·clips를 모른다.
유튜브 전용 코드는 `handler`에 있다. yt-dlp는 플랫폼 모듈의 `ytdlp.py`로만 부른다.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ...core.config import ConfigError
from ...core.extensions import Extension, ExtensionContext, SettingSpec
from ...platforms import registry
from ...platforms.youtube.platform import backend as yt_backend
from ..downloads import api

PLATFORM = "youtube"
NEED_ENABLE = "유튜브를 쓰려면 설정에서 먼저 켜 주십시오"
MESSAGES = {  # 이 종류의 오류 문장
    "live_video": "방송 중인 영상은 채널을 등록해 녹화해 주십시오.",
    "rate_limited": "유튜브가 요청을 제한했습니다. 잠시 뒤 다시 시도해 주십시오.",
    "login_required": "로그인이 필요한 영상입니다(멤버십·연령 제한·비공개). 쿠키 화면에서 유튜브 쿠키를 넣어 주십시오.",
    "login_rejected": "로그인이 필요한 영상인데 넣은 쿠키가 거부됐습니다. 쿠키가 만료됐거나 권한이 없습니다.",
    "unsupported_url": "재생목록·채널 주소는 받지 않습니다. 영상 하나의 주소를 넣어 주십시오.",
    "invalid_url": "지원하는 영상 주소가 아닙니다.",
    "extension_disabled": NEED_ENABLE,
}
KIND = api.JobKind("youtube", "youtube_video", "유튜브 영상", "live_recorder.ext.youtube_video.handler", platform=PLATFORM,
                   example="https://www.youtube.com/watch?v=aqz-KE-bpKQ", unavailable_message=NEED_ENABLE,
                   messages=MESSAGES, analysis_gap=5.0)


def _codec(value: Any) -> str:
    if value not in ("vp9", "av1"):
        raise ConfigError("1080p 초과 화질의 코덱은 vp9나 av1입니다")
    return value


SETTINGS = {"youtube_video_codec": SettingSpec("vp9", _codec, "1080p 초과 화질의 코덱")}


def platform_on(store) -> bool:
    """유튜브가 켜져 있고 막혀 있지 않은가(저장소의 켠 목록과 플랫폼의 막힘)."""
    platform = registry.find(PLATFORM)
    if platform is None or store is None:
        return False
    return PLATFORM in store.enabled_platforms() and not platform.enable_blocked()


class YoutubeVideoExtension(Extension):
    name = "youtube_video"
    title = "유튜브 영상"
    description = "유튜브 영상 주소로 영상을 받습니다. 유튜브를 켜야 쓸 수 있습니다."
    version = "1.0"
    requires = ("downloads",)
    requires_platforms = (PLATFORM,)
    job_kinds = (KIND,)
    settings = SETTINGS
    ui_dir = Path(__file__).resolve().parent / "ui"
    html = {"downloads-settings": "settings.html"}

    def open_storage(self, ctx: ExtensionContext) -> None:
        def context() -> dict:
            backend = yt_backend()
            return {"ytdlp": backend.select_source().token(), "codec": ctx.setting("youtube_video_codec")}

        api.register_kind(replace(KIND, available=lambda: platform_on(ctx.store), context=context))

    async def stop(self, ctx: ExtensionContext) -> None:
        api.unregister_kind(KIND.kind)


EXTENSION = YoutubeVideoExtension()
