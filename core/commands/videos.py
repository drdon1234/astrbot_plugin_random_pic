"""视频抽取与下载文件回收。"""

from __future__ import annotations

from contextlib import aclosing
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..message.types import FORWARD, MIXED, ONEBOT
from ..models import RATING_NAMES, STYLE_NAMES, DrawRequest
from ..services.drawing import DrawResult
from .common import delayed_notice, event_args, event_sender, send_failed_text
from .help import help_text

if TYPE_CHECKING:
    from ..policy.access import Quota
    from ..runtime import Runtime


class VideoCommands:
    """检查视频请求并在发送后回收下载文件。"""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def draw(self, event: AstrMessageEvent):
        tokens = event_args(self.runtime, event)
        if tokens is None:
            return
        if self.runtime.commands.is_help(tokens):
            yield event.plain_result(
                help_text(
                    self.runtime,
                    is_admin=self.runtime.access.is_admin(str(event.get_sender_id())),
                )
            )
            return
        if not self.runtime.settings.video.enabled:
            yield event.plain_result(
                f"视频功能未开启。当前可用操作见 {self.runtime.commands.command('帮助')}。"
            )
            return
        try:
            req = self.runtime.requests.video(tokens)
        except ValueError as exc:
            yield event.plain_result(
                f"参数有误：{exc}。用法见 {self.runtime.commands.command('帮助')}。"
            )
            return
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
        )
        if denied:
            yield event.plain_result(denied)
            return
        quota, denied = self.runtime.access.begin(user_id, req)
        if denied:
            yield event.plain_result(denied)
            return
        with quota:
            if req.notices:
                yield event.plain_result("\n".join(req.notices))
            async with (
                delayed_notice(
                    event, "已收到视频请求，正在获取或发送视频，请稍候，无需重复发送。"
                ),
                self.runtime.video_reserve.draw(
                    req,
                    self.runtime.access.explicit_allowed(is_private, user_id),
                    self.runtime.video_drawer.draw,
                ) as result,
            ):
                async with aclosing(self.send(event, req, result, quota=quota)) as stream:
                    async for out in stream:
                        yield out

    async def send(
        self, event: AstrMessageEvent, req: DrawRequest, result: DrawResult, *, quota: Quota
    ):
        """按全局发送方式发出抽到的视频，发完删掉下载的文件（预备池的桶由预备池删）。"""
        is_private = event.is_private_chat()
        paths = [path for album in result.albums for _, path in album.pictures]
        try:
            if not result.albums:
                logger.warning(f"[random_pic] 获取视频失败 {req}: {result.reason()}")
                yield event.plain_result(f"获取视频失败：{result.reason()}")
                return
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
            sender, fallback = event_sender(self.runtime, event, quota=quota)
            failed = await self.runtime.dispatcher.send_all(messages, [], sender)
            for chain in fallback:
                yield event.chain_result(chain)
                sender.confirm(chain)
            if failed:
                yield event.plain_result(
                    send_failed_text(
                        failed,
                        len(messages),
                        is_private,
                        self.runtime.commands.command(
                            "抽视频", " ".join(event_args(self.runtime, event) or [])
                        ),
                    )
                )
            if len(result.albums) < req.albums:
                yield event.plain_result(
                    f"仅获取到 {len(result.albums)}/{req.albums} 个视频。"
                )
        finally:
            self.runtime.videos.files.remove(paths)
