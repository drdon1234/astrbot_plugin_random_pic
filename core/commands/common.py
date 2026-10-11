"""命令共用的权限入口和消息发送反馈。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..message.transport import send_direct
from ..message.types import SendFailed, media_count

if TYPE_CHECKING:
    from ..policy.access import Quota
    from ..runtime import Runtime


def event_args(runtime: Runtime, event: AstrMessageEvent) -> list[str] | None:
    """指令后面的参数；用户或群没有权限时返回 None（不回复）。"""
    user_id = str(event.get_sender_id())
    group_id = str(event.get_group_id() or "")
    if not runtime.access.allowed(user_id, group_id):
        return None
    match = runtime.commands.resolve(event.message_str.strip().removeprefix("/"))
    return match[1] if match is not None else None


NOTICE_DELAY = 2.0


@asynccontextmanager
async def delayed_notice(event: AstrMessageEvent, text: str):
    """快速请求不发额外消息；慢请求只提醒一次，退出或取消时回收提醒任务。"""

    async def notify():
        await asyncio.sleep(NOTICE_DELAY)
        try:
            await event.send(event.plain_result(text))
        except Exception as exc:
            logger.warning(f"[random_pic] 等待提示发送失败: {exc!r}")

    task = (
        asyncio.create_task(notify())
        if callable(getattr(event, "send", None))
        else None
    )
    try:
        yield
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class EventSender:
    """记录发送接口成功接受的消息和媒体，不把失败或待交付的消息计入额度。"""

    def __init__(self, runtime: Runtime, event: AstrMessageEvent, quota: Quota | None):
        self.runtime = runtime
        self.event = event
        self.quota = quota
        self.fallback: list[list] = []
        self.media = 0
        self.sent = 0

    def confirm(self, chain: list):
        count = media_count(chain)
        self.media += count
        self.sent += 1
        if self.quota is not None and count:
            self.quota.charge(count)

    async def __call__(self, chain: list) -> str | None:
        delivered, message_id = await send_direct(
            self.event, chain, self.runtime.dispatcher.stage
        )
        if not delivered:
            if not callable(getattr(self.event, "send", None)):
                self.fallback.append(chain)
                return None
            try:
                await self.event.send(self.event.chain_result(chain))
            except Exception as exc:
                raise SendFailed.of(exc) from exc
        self.confirm(chain)
        return message_id


def event_sender(runtime: Runtime, event: AstrMessageEvent, *, quota: Quota | None = None):
    sender = EventSender(runtime, event, quota)
    return sender, sender.fallback


def send_failed_text(
    failed: list[SendFailed], total: int, is_private: bool, retry: str = ""
) -> str:
    """按失败原因分别说明：QQ 拒发，或超时、连接中断。"""

    def what(n: int) -> str:
        return "这条消息" if total == 1 else f"其中 {n}/{total} 条消息"

    lines = []
    rejected = sum(e.rejected for e in failed)
    if rejected:
        text = f"发送失败：QQ 拒发了{what(rejected)}。"
        if not is_private:
            text += "可在允许使用此功能的私聊重试本次操作。"
        else:
            text += "可换个关键词或稍后重试本次操作。"
        if retry:
            text += f"重试命令：{retry}。"
        lines.append(text)
    lost = len(failed) - rejected
    if lost:
        lines.append(
            f"发送失败：{what(lost)}未能确认发送结果，可能超时或连接中断。"
            "消息可能延迟到达，请先检查，避免立即重复操作。"
        )
    return "\n".join(lines)
