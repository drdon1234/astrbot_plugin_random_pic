"""命令共用的权限入口和消息发送反馈。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

from ..message.transport import send_direct
from ..message.types import SendFailed

if TYPE_CHECKING:
    from ..runtime import Runtime


def event_args(runtime: Runtime, event: AstrMessageEvent) -> list[str] | None:
    """指令后面的参数；用户或群没有权限时返回 None（不回复）。"""
    user_id = str(event.get_sender_id())
    group_id = str(event.get_group_id() or "")
    if not runtime.access.allowed(user_id, group_id):
        return None
    return event.message_str.split()[1:]


def event_sender(runtime: Runtime, event: AstrMessageEvent):
    """QQ 上直接发送并拿到消息 ID；发不出去的（其他平台、组装出错）收集起来交给 AstrBot 发。"""
    fallback: list[list] = []

    async def send(chain: list) -> str | None:
        delivered, message_id = await send_direct(
            event, chain, runtime.dispatcher.stage
        )
        if not delivered:
            fallback.append(chain)
        return message_id

    return send, fallback


def send_failed_text(failed: list[SendFailed], total: int, is_private: bool) -> str:
    """按失败原因分别说明：QQ 拒发，或超时、连接中断。"""

    def what(n: int) -> str:
        return "这条消息" if total == 1 else f"其中 {n}/{total} 条消息"

    lines = []
    rejected = sum(e.rejected for e in failed)
    if rejected:
        text = f"发送失败：QQ 拒发了{what(rejected)}。"
        if not is_private:
            text += "群聊里裸露较多的图容易被 QQ 拦截，可以私聊重新抽图。"
        else:
            text += "可能是图片内容被 QQ 拦截，可换个关键词或稍后重试。"
        lines.append(text)
    lost = len(failed) - rejected
    if lost:
        lines.append(f"发送失败：{what(lost)}发送超时或与 QQ 的连接中断，请稍后重试。")
    return "\n".join(lines)
