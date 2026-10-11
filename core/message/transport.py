"""通过 AstrBot 事件与 OneBot 接口发送消息。"""

import asyncio
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..utils import run_thread
from .stage import Stage
from .types import ONEBOT, SendFailed


async def send_direct(
    event: AstrMessageEvent, chain: list, stage: Stage | None = None
) -> tuple[bool, str | None]:
    """QQ（OneBot）上直接调用发送接口，拿到消息 ID 供 /pdf 按回复查图集。

    返回 (是否已发送, 消息 ID)。其他平台或组装消息出错时返回未发送，由 AstrBot 照常发送；
    发送接口本身报错时抛出 SendFailed。stage 是共享目录（可选）。
    """
    bot = getattr(event, "bot", None)
    if event.get_platform_name() != ONEBOT or bot is None:
        return False, None
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    self_id = raw.get("self_id") if hasattr(raw, "get") else None
    delivered, message_id = await send_onebot(
        bot,
        chain,
        str(event.get_group_id() or ""),
        str(event.get_sender_id()),
        self_id,
        stage,
    )
    if delivered:
        event._has_send_oper = True  # 避免 AstrBot 认为插件没有回复
    return delivered, message_id


async def send_onebot(
    bot,
    chain: list,
    group_id: str,
    user_id: str,
    self_id=None,
    stage: Stage | None = None,
) -> tuple[bool, str | None]:
    """调用 OneBot 接口发到群 group_id，group_id 为空时私聊发给 user_id。返回值同 send_direct。

    有共享目录时图片以文件路径发送，否则由 AstrBot 编码成 base64。
    """
    copies: list[Path] = []
    try:
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

        target = {"group_id": int(group_id)} if group_id else {"user_id": int(user_id)}
        if self_id:
            target["self_id"] = self_id
        staged = stage is not None and Stage.supports(chain)
        if len(chain) == 1 and isinstance(chain[0], Comp.Nodes):
            if staged:
                nodes = await run_thread(stage.forward, chain[0], copies)
                payload = {"messages": nodes}
            else:
                payload = await chain[0].to_dict()
            action = (
                "send_group_forward_msg" if group_id else "send_private_forward_msg"
            )
        else:
            if staged:
                message = await run_thread(stage.segments, chain, copies)
            else:
                message = await AiocqhttpMessageEvent._parse_onebot_json(
                    MessageChain(chain)
                )
            payload = {"message": message}
            action = "send_group_msg" if group_id else "send_private_msg"
        Stage.protect(copies)
    except asyncio.CancelledError:
        # 线程已经退出，暂存路径此时才能完整、可靠地清理。
        Stage.remove(copies)
        raise
    except Exception as e:
        Stage.remove(copies)
        logger.warning(f"[random_pic] 组装消息失败，改由 AstrBot 发送: {e!r}")
        return False, None
    try:
        ret = await bot.call_action(action, **payload, **target)
    except asyncio.CancelledError:
        # 接口已提交，取消等待无法确认机器人是否还在上传，与超时同样延迟删除。
        Stage.remove_later(copies)
        copies = []
        raise
    except Exception as e:
        logger.warning(f"[random_pic] 发送失败（{action}）: {e!r}")
        failed = SendFailed.of(e)
        if not failed.rejected:
            # 可能只是等回应超时，QQ 机器人还在读文件，晚些再删
            Stage.remove_later(copies)
            copies = []
        raise failed from e
    finally:
        Stage.remove(copies)
    message_id = ret.get("message_id") if isinstance(ret, dict) else None
    if message_id is None:
        logger.warning(f"[random_pic] 发送接口没有返回消息 ID: {ret!r}")
        return True, None
    return True, str(message_id)
