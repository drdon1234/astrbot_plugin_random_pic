"""调度消息发送，并登记会话和消息对应的图集。"""

import asyncio

from ..models import Album
from ..storage.history import History, SentAlbum, clean_title
from .composer import Composer, resolve_mode
from .stage import Stage
from .types import Message, Send, SendFailed

# 两条消息之间的间隔（秒），和 AstrBot 分段发送一致
SEND_INTERVAL = 0.5


class Dispatcher:
    """发送抽到的图集：登记到会话、排成消息逐条发送，并按消息 ID 登记其中的图集（/pdf 按回复查）。"""

    def __init__(
        self,
        history: History,
        composer: Composer,
        mode: str,
        stage: Stage | None = None,
    ):
        """stage：共享目录，QQ 上直接发送时使用（可选）。"""
        self.history = history
        self.composer = composer
        self.mode = mode
        self.stage = stage

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
    ) -> tuple[list[SendFailed], int]:
        """登记并发送抽到的图集，uin 是合并转发节点的发送者。返回 (各条失败消息的错误, 总条数)。"""
        sent = self.record(session, albums)
        mode = resolve_mode(self.mode, platform, len(albums))
        messages = self.composer.compose(albums, mode, uin)
        return await self.send_all(messages, sent, send), len(messages)

    async def send_all(
        self, messages: list[Message], sent: list[SentAlbum], send: Send
    ) -> list[SendFailed]:
        """依次发送消息并按消息 ID 登记其中的图集，返回各条失败消息的错误。"""
        failed = []
        for i, (chain, idxs) in enumerate(messages):
            if i:
                await asyncio.sleep(SEND_INTERVAL)
            try:
                message_id = await send(chain)
            except SendFailed as e:
                failed.append(e)
                continue
            if message_id and idxs and sent:
                self.history.record_message(message_id, [sent[idx - 1] for idx in idxs])
        return failed
