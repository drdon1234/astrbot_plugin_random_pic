"""视频抽取与下载文件回收。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..message.types import FORWARD, MIXED, ONEBOT
from ..models import RATING_NAMES, STYLE_NAMES, DrawRequest
from ..services.drawing import DrawResult
from .common import event_args, event_sender, send_failed_text
from .help import help_text
from .parser import HELP_WORDS

if TYPE_CHECKING:
    from ..runtime import Runtime


class VideoCommands:
    """检查视频请求并在发送后回收下载文件。"""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def draw(self, event: AstrMessageEvent):
        if not self.runtime.settings.video.enabled:
            return
        tokens = event_args(self.runtime, event)
        if tokens is None:
            return
        if tokens and tokens[0].lower() in HELP_WORDS:
            yield event.plain_result(help_text(self.runtime))
            return
        req = self.runtime.requests.video(tokens)
        if req.whole:
            yield event.plain_result("视频没有完整作品，请去掉「全集」「pdf」。")
            return
        if req.style not in self.runtime.videos.styles(req.rating):
            kind = f"{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}"
            yield event.plain_result(f"没有能抽{kind}视频的视频源。")
            return
        is_private = event.is_private_chat()
        user_id = str(event.get_sender_id())
        denied = self.runtime.access.gate(
            req.rating, is_private, user_id
        ) or self.runtime.access.take(user_id, req)
        if denied:
            yield event.plain_result(denied)
            return
        async with self.runtime.video_reserve.draw(
            req,
            self.runtime.access.explicit_allowed(is_private, user_id),
            self.runtime.video_drawer.draw,
        ) as result:
            async for out in self.send(event, req, result):
                yield out

    async def send(self, event: AstrMessageEvent, req: DrawRequest, result: DrawResult):
        """按全局发送方式发出抽到的视频，发完删掉下载的文件（预备池的桶由预备池删）。"""
        is_private = event.is_private_chat()
        user_id = str(event.get_sender_id())
        paths = [path for album in result.albums for _, path in album.pictures]
        try:
            if not result.albums:
                logger.warning(f"[random_pic] 获取视频失败 {req}: {result.reason()}")
                yield event.plain_result(f"获取视频失败：{result.reason()}")
                return
            self.runtime.access.used(user_id, len(result.albums))
            staged = (
                self.runtime.dispatcher.stage is not None
                or event.get_platform_name() != ONEBOT
            )
            mode = self.runtime.dispatcher.mode
            if mode == FORWARD and event.get_platform_name() != ONEBOT:
                mode = MIXED
            messages = self.runtime.dispatcher.composer.video_messages(
                result.albums, staged, mode, str(event.get_self_id())
            )
            sender, fallback = event_sender(self.runtime, event)
            failed = await self.runtime.dispatcher.send_all(messages, [], sender)
            for chain in fallback:
                yield event.chain_result(chain)
            if failed:
                yield event.plain_result(
                    send_failed_text(failed, len(messages), is_private)
                )
            if len(result.albums) < req.albums:
                yield event.plain_result(
                    f"仅获取到 {len(result.albums)}/{req.albums} 个视频。"
                )
        finally:
            self.runtime.videos.files.remove(paths)
