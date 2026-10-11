"""图片抽取命令。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..models import RATING_NAMES, STYLE_NAMES
from .common import event_args, event_sender, send_failed_text
from .help import help_text
from .parser import ALIASES, HELP_WORDS
from .works import WorkCommands

if TYPE_CHECKING:
    from ..runtime import Runtime


class ImageCommands:
    """检查图片请求并将抽取结果交给消息服务。"""

    def __init__(self, runtime: Runtime, works: WorkCommands):
        self.runtime = runtime
        self.works = works

    async def draw(self, event: AstrMessageEvent, word: str):
        if word in ALIASES and not self.runtime.settings.access.aliases:
            return
        tokens = event_args(self.runtime, event)
        if tokens is None:
            return
        if tokens and tokens[0].lower() in HELP_WORDS:
            yield event.plain_result(help_text(self.runtime))
            return
        req = self.runtime.requests.image(word, tokens)
        if req.whole and not self.runtime.settings.whole.enabled:
            yield event.plain_result("完整作品功能未开启。")
            return
        is_private = event.is_private_chat()
        user_id = str(event.get_sender_id())
        denied = self.runtime.access.gate(
            req.rating, is_private, user_id
        ) or self.runtime.access.take(user_id, req)
        if denied:
            yield event.plain_result(denied)
            return
        if req.whole:
            async for result in self.works.draw_random(event, req, user_id):
                yield result
            return

        async with self.runtime.reserve.draw(
            req,
            self.runtime.access.explicit_allowed(is_private, user_id),
            self.runtime.drawer.draw,
        ) as result:
            if not result.albums:
                logger.warning(f"[random_pic] 获取失败 {req}: {result.reason()}")
                yield event.plain_result(
                    f"获取{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}图片失败："
                    f"{result.reason()}"
                )
                return

            self.runtime.access.used(user_id, result.images)
            sender, fallback = event_sender(self.runtime, event)
            failed, total = await self.runtime.dispatcher.deliver(
                result.albums,
                event.unified_msg_origin,
                event.get_platform_name(),
                str(event.get_self_id()),
                sender,
            )
            for chain in fallback:
                yield event.chain_result(chain)
            if failed:
                yield event.plain_result(send_failed_text(failed, total, is_private))
            if len(result.albums) < req.albums:
                yield event.plain_result(
                    f"仅获取到 {len(result.albums)}/{req.albums} 个图集。"
                )
