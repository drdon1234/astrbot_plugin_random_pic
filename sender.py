"""把图集排成要发送的消息，QQ（OneBot）上直接调用发送接口拿到消息 ID。"""

from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .history import header_text, page_text
from .models import Album

FORWARD = "合并转发"
MIXED = "图文混合"
SEPARATE = "逐条发送"
ONEBOT = "aiocqhttp"
# 图文混合时每条消息最多放这么多张图，太多时 QQ 可能发送失败
MIXED_PER_MESSAGE = 10

# 一条消息：(消息段, 其中的图集序号)
Message = tuple[list, list[int]]


def resolve_mode(mode: str, platform: str, albums: int) -> str:
    """合并转发只在 QQ / OneBot 上可用；只有一个图集时也不用合并转发。"""
    if mode == FORWARD and (platform != ONEBOT or albums == 1):
        return MIXED
    return mode


class Composer:
    def __init__(self, header: bool, caption: bool):
        self.header = header
        self.caption = caption

    def album_content(
        self, idx: int, album: Album, pictures: list[tuple[int, Path]], last: bool
    ) -> list:
        """图集（或其中一段）的消息段：每张图上方的标题行、图片，last 时最后附说明文字。

        这一段的第一张图上方是「【序号】标题」和「第 x/y 张 · 来源」，后面的图只有「第 x/y 张」。
        """
        content = []
        for i, (page, path) in enumerate(pictures):
            if self.header:
                text = (
                    header_text(idx, album.title, page, album.total, album.source)
                    if i == 0
                    else page_text(page, album.total)
                )
                content.append(Comp.Plain(text + "\n"))
            content.append(Comp.Image.fromFileSystem(str(path)))
        if last and self.caption and album.details:
            content.append(Comp.Plain("\n" + "\n".join(album.details)))
        return content

    def compose(self, albums: list[Album], mode: str, uin: str) -> list[Message]:
        numbered = list(enumerate(albums, 1))
        if mode == FORWARD:
            # 每个图集一个节点，节点顺序即序号
            nodes = [
                Comp.Node(
                    content=self.album_content(idx, album, album.pictures, True),
                    uin=uin,
                    name="抽图",
                )
                for idx, album in numbered
            ]
            return [([Comp.Nodes(nodes)], [idx for idx, _ in numbered])]
        if mode == SEPARATE:
            # 每张图一条消息、都带完整标题，说明文字跟在图集最后一张后面
            return [
                (
                    self.album_content(
                        idx, album, [picture], i == len(album.pictures) - 1
                    ),
                    [idx],
                )
                for idx, album in numbered
                for i, picture in enumerate(album.pictures)
            ]
        # 图文混合：图集依次排在一条消息里，每条最多 MIXED_PER_MESSAGE 张图；
        # 超过的图集拆开，后一段重新带上完整标题，说明文字只跟在最后一段
        messages, chain, idxs, count = [], [], [], 0
        for idx, album in numbered:
            for start in range(0, len(album.pictures), MIXED_PER_MESSAGE):
                part = album.pictures[start : start + MIXED_PER_MESSAGE]
                if chain and count + len(part) > MIXED_PER_MESSAGE:
                    messages.append((chain, idxs))
                    chain, idxs, count = [], [], 0
                last = start + MIXED_PER_MESSAGE >= len(album.pictures)
                content = self.album_content(idx, album, part, last)
                if chain and isinstance(content[0], Comp.Plain):
                    content[0] = Comp.Plain("\n\n" + content[0].text)
                chain.extend(content)
                if idx not in idxs:
                    idxs.append(idx)
                count += len(part)
        if chain:
            messages.append((chain, idxs))
        return messages


class SendFailed(Exception):
    """OneBot 发送接口报错（通常是 QQ 拒发），交给 AstrBot 重发也会同样失败。"""


async def send_direct(event: AstrMessageEvent, chain: list) -> tuple[bool, str | None]:
    """QQ（OneBot）上直接调用发送接口，拿到消息 ID 供 /pdf 按回复查图集。

    返回 (是否已发送, 消息 ID)。其他平台或组装消息出错时返回未发送，由 AstrBot 照常发送；
    发送接口本身报错时抛出 SendFailed。
    """
    bot = getattr(event, "bot", None)
    if event.get_platform_name() != ONEBOT or bot is None:
        return False, None
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    self_id = raw.get("self_id") if hasattr(raw, "get") else None
    delivered, message_id = await send_onebot(
        bot, chain, str(event.get_group_id() or ""), str(event.get_sender_id()), self_id
    )
    if delivered:
        event._has_send_oper = True  # 避免 AstrBot 认为插件没有回复
    return delivered, message_id


async def send_onebot(
    bot, chain: list, group_id: str, user_id: str, self_id=None
) -> tuple[bool, str | None]:
    """调用 OneBot 接口发到群 group_id，group_id 为空时私聊发给 user_id。返回值同 send_direct。"""
    try:
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

        target = {"group_id": int(group_id)} if group_id else {"user_id": int(user_id)}
        if self_id:
            target["self_id"] = self_id
        if len(chain) == 1 and isinstance(chain[0], Comp.Nodes):
            payload = await chain[0].to_dict()
            action = (
                "send_group_forward_msg" if group_id else "send_private_forward_msg"
            )
        else:
            payload = {
                "message": await AiocqhttpMessageEvent._parse_onebot_json(
                    MessageChain(chain)
                )
            }
            action = "send_group_msg" if group_id else "send_private_msg"
    except Exception as e:
        logger.warning(f"[random_pic] 组装消息失败，改由 AstrBot 发送: {e!r}")
        return False, None
    try:
        ret = await bot.call_action(action, **payload, **target)
    except Exception as e:
        logger.warning(f"[random_pic] 发送失败（{action}）: {e!r}")
        raise SendFailed(str(e)) from e
    message_id = ret.get("message_id") if isinstance(ret, dict) else None
    if message_id is None:
        logger.warning(f"[random_pic] 发送接口没有返回消息 ID: {ret!r}")
        return True, None
    return True, str(message_id)
