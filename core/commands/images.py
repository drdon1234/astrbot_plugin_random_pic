"""图片抽取命令。"""

from __future__ import annotations

from contextlib import aclosing
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..models import RATING_NAMES, STYLE_NAMES
from .common import delayed_notice, event_args, event_sender, send_failed_text
from .help import help_text
from .parser import ALIASES
from .works import WorkCommands

if TYPE_CHECKING:
    from ..runtime import Runtime


class ImageCommands:
    """检查图片请求并将抽取结果交给消息服务。"""

    def __init__(self, runtime: Runtime, works: WorkCommands):
        self.runtime = runtime
        self.works = works

    async def draw(self, event: AstrMessageEvent, word: str):
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
        if word in ALIASES and not self.runtime.settings.access.aliases:
            yield event.plain_result(
                f"快捷入口未开启，请使用 {self.runtime.commands.command('抽图')}；"
                f"用法见 {self.runtime.commands.command('帮助')}。"
            )
            return
        try:
            req = self.runtime.requests.image(word, tokens)
        except ValueError as exc:
            yield event.plain_result(
                f"参数有误：{exc}。用法见 {self.runtime.commands.command('帮助')}。"
            )
            return
        if req.whole and not self.runtime.settings.whole.enabled:
            yield event.plain_result("完整作品功能未开启。")
            return
        is_private = event.is_private_chat()
        user_id = str(event.get_sender_id())
        denied = self.runtime.access.gate(
            req.rating, is_private, user_id
        )
        if denied:
            yield event.plain_result(denied)
            return
        if req.whole:
            async with aclosing(self.works.draw_random(
                event,
                req,
                user_id,
                retry=self.runtime.commands.command(word, " ".join(tokens)),
            )) as stream:
                async for result in stream:
                    yield result
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
                    event, "已收到抽图请求，正在获取或发送图片，请稍候，无需重复发送。"
                ),
                self.runtime.reserve.draw(
                    req,
                    self.runtime.access.explicit_allowed(is_private, user_id),
                    self.runtime.drawer.draw,
                ) as result,
            ):
                if not result.albums:
                    logger.warning(f"[random_pic] 获取失败 {req}: {result.reason()}")
                    yield event.plain_result(
                        f"获取{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}图片失败："
                        f"{result.reason()}。可减少关键词后重试；"
                        f"用法见 {self.runtime.commands.command('帮助')}。"
                    )
                    return

                sender, fallback = event_sender(self.runtime, event, quota=quota)
                failed, total = await self.runtime.dispatcher.deliver(
                    result.albums,
                    event.unified_msg_origin,
                    event.get_platform_name(),
                    str(event.get_self_id()),
                    sender,
                )
                for chain in fallback:
                    yield event.chain_result(chain)
                    sender.confirm(chain)
                if failed:
                    yield event.plain_result(
                        send_failed_text(
                            failed,
                            total,
                            is_private,
                            self.runtime.commands.command(word, " ".join(tokens)),
                        )
                    )
                if len(result.albums) < req.albums:
                    yield event.plain_result(
                        f"仅获取到 {len(result.albums)}/{req.albums} 个图集。"
                    )
                short = []
                for index, album in enumerate(result.albums, 1):
                    expected = req.per_album
                    if (
                        album.work and album.work.source == "danbooru"
                    ) or album.source_key == "danbooru":
                        expected = 1
                    if len(album.pictures) < expected:
                        short.append(f"第 {index} 集 {len(album.pictures)}/{expected} 张")
                if short:
                    yield event.plain_result(
                        "本次图片未取满："
                        + "；".join(short)
                        + "。作品图片不足或部分图片未能下载。"
                    )
