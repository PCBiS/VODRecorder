"""cime_media 확장: 씨미 다시보기·클립 주소를 받는다. downloads 확장이 필요하고 씨미가 켜져 있어야 쓴다.

downloads는 공개 접점(api: 작업 종류 등록, media: 받는 방법)으로만 쓰고 다른 확장을 모른다.
씨미 전용 코드는 `common`·`vod`·`clip`에 있고 씨미 플랫폼 모듈(`platforms/cime`)의 함수를 함께 쓴다.
"""

from __future__ import annotations

from dataclasses import replace

from ...core.extensions import Extension, ExtensionContext
from ...platforms import registry
from ..downloads import api

PLATFORM = "cime"
NEED_ENABLE = "씨미를 쓰려면 설정에서 먼저 켜 주십시오"
MESSAGES = {  # 이 종류의 오류 문장
    "live_video": "방송 중인 영상은 채널을 등록해 녹화해 주십시오.",
    "unsupported_url": "채널·목록 주소는 받지 않습니다. 다시보기나 클립 하나의 주소를 넣어 주십시오.",
    "login_required": "로그인이 필요한 영상입니다. 쿠키 화면에서 씨미 쿠키를 넣어 주십시오.",
    "login_rejected": "쿠키가 만료됐거나 볼 권한이 없습니다.",
    "not_found": "영상을 찾지 못했습니다.",
    "not_ready": "지금 받을 수 있는 상태의 영상이 아닙니다.",
    "invalid_url": "지원하는 씨미 주소가 아닙니다.",
    "extension_disabled": NEED_ENABLE,
}
VOD = api.JobKind("cime_vod", "cime_media", "씨미 다시보기", "live_recorder.ext.cime_media.vod", platform=PLATFORM,
                  example="https://ci.me/@handle/vods/123", unavailable_message=NEED_ENABLE, messages=MESSAGES)
CLIP = api.JobKind("cime_clip", "cime_media", "씨미 클립", "live_recorder.ext.cime_media.clip", platform=PLATFORM,
                   example="https://ci.me/clips/123", unavailable_message=NEED_ENABLE, messages=MESSAGES)


def platform_on(store) -> bool:
    """씨미가 켜져 있고 막혀 있지 않은가(저장소의 켠 목록과 플랫폼의 막힘)."""
    platform = registry.find(PLATFORM)
    if platform is None or store is None:
        return False
    return PLATFORM in store.enabled_platforms() and not platform.enable_blocked()


class CimeMediaExtension(Extension):
    name = "cime_media"
    title = "씨미 다시보기·클립"
    description = "씨미 다시보기와 클립 주소로 영상을 받습니다. 채널의 다시보기 목록에서 골라 받을 수도 있습니다. 씨미를 켜야 쓸 수 있습니다."
    version = "1.0"
    requires = ("downloads",)
    requires_platforms = (PLATFORM,)
    job_kinds = (VOD, CLIP)
    # 채널 다시보기 목록의 공급자(화면 조각은 공급자가 있을 때만 들어간다)
    ui_dir = api.LIST_FRAGMENT.parent
    html = {"downloads-modes": api.LIST_FRAGMENT.name}

    def open_storage(self, ctx: ExtensionContext) -> None:
        from .channel_list import CimeLists

        for kind in (VOD, CLIP):
            api.register_kind(replace(kind, available=lambda: platform_on(ctx.store)))
        ctx.lists = CimeLists(ctx.cfg)
        api.register_list_provider(ctx.lists.provider(lambda: platform_on(ctx.store)))

    async def stop(self, ctx: ExtensionContext) -> None:
        for kind in (VOD, CLIP):
            api.unregister_kind(kind.kind)
        api.unregister_list_provider(PLATFORM)
        lists = getattr(ctx, "lists", None)
        if lists is not None:
            await lists.aclose()


EXTENSION = CimeMediaExtension()
