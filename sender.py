"""把图集排成要发送的消息、逐条发出并登记，QQ（OneBot）上直接调用发送接口拿到消息 ID。"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .history import History, SentAlbum, clean_title, header_text, page_text
from .models import Album

FORWARD = "合并转发"
MIXED = "图文混合"
SEPARATE = "逐条发送"
ONEBOT = "aiocqhttp"
# 图文混合时每条消息最多放这么多张图，太多时 QQ 可能发送失败
MIXED_PER_MESSAGE = 10
# 一条合并转发最多这么多个节点（QQ 的上限）
FORWARD_MAX_NODES = 100
# 两条消息之间的间隔（秒），和 AstrBot 分段发送一致
SEND_INTERVAL = 0.5

# 一条消息：(消息段, 其中的图集序号)
Message = tuple[list, list[int]]
# 发送一条消息，返回消息 ID（拿不到时为 None），QQ 拒发时抛出 SendFailed
Send = Callable[[list], Awaitable[str | None]]


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
            content += self.picture_content(idx, album, page, path, i == 0)
        if last:
            content += self.caption_content(album)
        return content

    def picture_content(
        self, idx: int, album: Album, page: int, path: Path, full_header: bool
    ) -> list:
        """一张图的消息段：标题行（full_header 时是完整标题，否则只有「第 x/y 张」）和图片。"""
        content = []
        if self.header:
            text = (
                header_text(idx, album.title, page, album.total, album.source)
                if full_header
                else page_text(page, album.total)
            )
            content.append(Comp.Plain(text + "\n"))
        content.append(Comp.Image.fromFileSystem(str(path)))
        return content

    def caption_content(self, album: Album) -> list:
        if self.caption and album.details:
            return [Comp.Plain("\n" + "\n".join(album.details))]
        return []

    def forward_album(self, album: Album, uin: str) -> list[Message]:
        """整个图集用合并转发发送：每张图一个节点，超过 FORWARD_MAX_NODES 张时拆成几条。

        每条的第一张图带完整标题，说明文字跟在整个图集最后一张图后面。图集序号固定为 1。
        """
        pictures = album.pictures
        messages = []
        for start in range(0, len(pictures), FORWARD_MAX_NODES):
            nodes = []
            for i, (page, path) in enumerate(
                pictures[start : start + FORWARD_MAX_NODES]
            ):
                content = self.picture_content(1, album, page, path, i == 0)
                if start + i == len(pictures) - 1:
                    content += self.caption_content(album)
                nodes.append(Comp.Node(content=content, uin=uin, name="抽图"))
            messages.append(([Comp.Nodes(nodes)], [1]))
        return messages

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


class Dispatcher:
    """发送抽到的图集：登记到会话、排成消息逐条发送，并按消息 ID 登记其中的图集（/pdf 按回复查）。"""

    def __init__(self, history: History, composer: Composer, mode: str):
        self.history = history
        self.composer = composer
        self.mode = mode

    def record(self, session: str, albums: list[Album]) -> list[SentAlbum]:
        """登记本会话这次抽到的图集，序号从 1 开始。"""
        sent = [
            SentAlbum(idx, clean_title(album.title), album.source, album.work)
            for idx, album in enumerate(albums, 1)
        ]
        self.history.record_draw(session, sent)
        return sent

    async def deliver(
        self, albums: list[Album], session: str, platform: str, uin: str, send: Send
    ) -> tuple[int, int]:
        """登记并发送抽到的图集，uin 是合并转发节点的发送者。返回 (失败条数, 总条数)。"""
        sent = self.record(session, albums)
        mode = resolve_mode(self.mode, platform, len(albums))
        messages = self.composer.compose(albums, mode, uin)
        return await self.send_all(messages, sent, send), len(messages)

    async def send_all(
        self, messages: list[Message], sent: list[SentAlbum], send: Send
    ) -> int:
        """依次发送消息并按消息 ID 登记其中的图集，返回失败条数。"""
        failed = 0
        for i, (chain, idxs) in enumerate(messages):
            if i:
                await asyncio.sleep(SEND_INTERVAL)
            try:
                message_id = await send(chain)
            except SendFailed:
                failed += 1
                continue
            if message_id and idxs:
                self.history.record_message(message_id, [sent[idx - 1] for idx in idxs])
        return failed
