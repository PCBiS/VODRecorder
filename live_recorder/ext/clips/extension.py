"""clips 확장: 치지직 클립의 분석과 받기. downloads 확장이 필요하다.

downloads는 공개 접점(api: 작업 종류 등록, media: 받는 방법)으로만 쓴다. 치지직 전용 코드는 `chzzk_clip`·`clips`에 있다.
"""

from __future__ import annotations

from ...core.extensions import Extension, ExtensionContext
from ..downloads import api
from . import chzzk_clip

CLIP_KIND = api.JobKind("clip", "clips", "클립", "live_recorder.ext.clips.chzzk_clip", platform="chzzk",
                        example=chzzk_clip.clip_url("abcdEFGH1234"))


class ClipsExtension(Extension):
    name = "clips"
    title = "클립"
    description = "치지직 클립을 주소로 받습니다."
    version = "1.0"
    requires = ("downloads",)
    legacy_enabled_key = "replay_enabled"
    legacy_enabled_default = True
    job_kinds = (CLIP_KIND,)
    log_mask = {"video_paths": ("/clips?/",)}  # 로그 내려받기가 클립 번호를 가린다

    def open_storage(self, ctx: ExtensionContext) -> None:
        api.register_kind(CLIP_KIND)

    async def stop(self, ctx: ExtensionContext) -> None:
        api.unregister_kind(CLIP_KIND.kind)


EXTENSION = ClipsExtension()
