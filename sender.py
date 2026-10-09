"""把图集排成要发送的消息、逐条发出并登记，QQ（OneBot）上直接调用发送接口拿到消息 ID。"""

import asyncio
import base64
import shutil
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .history import History, SentAlbum, clean_title, header_text, page_text
from .models import Album
from .util import duration_text

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
# 图片编码（base64）进消息时，一条消息里图片原文件的总字节数上限。
# NapCat 的 WebSocket 单条上限是 50 MB，超过时连接被断开、发送要等到超时才失败；base64 会大三分之一
INLINE_BUDGET = 30 * 1024 * 1024
# 插件复制到中转目录的图片的文件名前缀
STAGE_PREFIX = "rp_"
# 发送超时、连接中断时 QQ 机器人可能还在读中转目录里的文件（大视频上传要很久），这么多秒后再删
STAGE_KEEP_SECONDS = 600

# 一条消息：(消息段, 其中的图集序号)
Message = tuple[list, list[int]]
# 发送一条消息，返回消息 ID（拿不到时为 None），发送接口报错时抛出 SendFailed
Send = Callable[[list], Awaitable[str | None]]


def resolve_mode(mode: str, platform: str, albums: int) -> str:
    """合并转发只在 QQ / OneBot 上可用；只有一个图集时也不用合并转发。"""
    if mode == FORWARD and (platform != ONEBOT or albums == 1):
        return MIXED
    return mode


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class Composer:
    def __init__(self, header: bool, caption: bool, budget: int | None = INLINE_BUDGET):
        """budget：一条消息里图片的总字节数上限，None 为不限（图片以文件路径发送时）。"""
        self.header = header
        self.caption = caption
        self.budget = budget

    def _bytes(self, pictures: list[tuple[int, Path]]) -> int:
        return sum(_size(path) for _, path in pictures) if self.budget else 0

    def _over(self, size: int) -> bool:
        return self.budget is not None and size > self.budget

    def _chunks(
        self, pictures: list[tuple[int, Path]], count: int | None = None
    ) -> list[list[tuple[int, Path]]]:
        """把图片按张数（count，None 为不限）和总字节数切成连续的几段，每段至少一张。"""
        chunks, chunk, size = [], [], 0
        for picture in pictures:
            n = self._bytes([picture])
            if chunk and (
                (count is not None and len(chunk) >= count) or self._over(size + n)
            ):
                chunks.append(chunk)
                chunk, size = [], 0
            chunk.append(picture)
            size += n
        if chunk:
            chunks.append(chunk)
        return chunks

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

    def video_messages(self, albums: list[Album], staged: bool) -> list[Message]:
        """视频：QQ 的视频消息不能带文字，每个视频先发一条标题和说明，再单独发视频。

        staged 为 False（没有中转目录）时视频编码进消息，超过大小上限的不发。
        """
        messages = []
        for idx, album in enumerate(albums, 1):
            _, path = album.pictures[0]
            text = []
            if self.header:
                text.append(video_header(idx, album))
            if self.caption and album.details:
                text += album.details
            if text:
                messages.append(([Comp.Plain("\n".join(text))], [idx]))
            if staged:
                video = Comp.Video.fromFileSystem(str(path))
            elif self._over(_size(path)):
                messages.append(([Comp.Plain("视频太大，没有中转目录时发不出去。")], []))
                continue
            else:
                video = Comp.Video.fromBase64(
                    base64.b64encode(path.read_bytes()).decode()
                )
            messages.append(([video], [idx]))
        return messages

    def forward_album(self, album: Album, uin: str) -> list[Message]:
        """整个图集用合并转发发送：每张图一个节点，超过 FORWARD_MAX_NODES 张或大小上限时拆成几条。

        每条的第一张图带完整标题，说明文字跟在整个图集最后一张图后面。图集序号固定为 1。
        """
        messages, done = [], 0
        for part in self._chunks(album.pictures, FORWARD_MAX_NODES):
            nodes = []
            for i, (page, path) in enumerate(part):
                content = self.picture_content(1, album, page, path, i == 0)
                if done + i == len(album.pictures) - 1:
                    content += self.caption_content(album)
                nodes.append(Comp.Node(content=content, uin=uin, name="抽图"))
            done += len(part)
            messages.append(([Comp.Nodes(nodes)], [1]))
        return messages

    def compose(self, albums: list[Album], mode: str, uin: str) -> list[Message]:
        numbered = list(enumerate(albums, 1))
        if mode == FORWARD:
            # 每个图集一个节点，节点顺序即序号；超过大小上限的图集拆成几个节点（后面的重新带上完整标题），
            # 节点数或大小超过上限时拆成几条合并转发
            messages, nodes, idxs, size = [], [], [], 0
            for idx, album in numbered:
                parts = self._chunks(album.pictures)
                for i, part in enumerate(parts):
                    n = self._bytes(part)
                    if nodes and (
                        len(nodes) >= FORWARD_MAX_NODES or self._over(size + n)
                    ):
                        messages.append(([Comp.Nodes(nodes)], idxs))
                        nodes, idxs, size = [], [], 0
                    content = self.album_content(idx, album, part, i == len(parts) - 1)
                    nodes.append(Comp.Node(content=content, uin=uin, name="抽图"))
                    if idx not in idxs:
                        idxs.append(idx)
                    size += n
            if nodes:
                messages.append(([Comp.Nodes(nodes)], idxs))
            return messages
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
        # 图文混合：图集依次排在一条消息里，每条最多 MIXED_PER_MESSAGE 张图、不超过大小上限；
        # 超过的图集拆开，后一段重新带上完整标题，说明文字只跟在最后一段
        messages, chain, idxs, count, size = [], [], [], 0, 0
        for idx, album in numbered:
            parts = self._chunks(album.pictures, MIXED_PER_MESSAGE)
            for i, part in enumerate(parts):
                n = self._bytes(part)
                if chain and (
                    count + len(part) > MIXED_PER_MESSAGE or self._over(size + n)
                ):
                    messages.append((chain, idxs))
                    chain, idxs, count, size = [], [], 0, 0
                content = self.album_content(idx, album, part, i == len(parts) - 1)
                if chain and isinstance(content[0], Comp.Plain):
                    content[0] = Comp.Plain("\n\n" + content[0].text)
                chain.extend(content)
                if idx not in idxs:
                    idxs.append(idx)
                count += len(part)
                size += n
        if chain:
            messages.append((chain, idxs))
        return messages


def video_header(idx: int, album: Album) -> str:
    """视频上方的两行标题：「【序号】标题」和「时长 · 来源」。"""
    length = duration_text(album.duration)
    second = f"{length} · {album.source}" if length else album.source
    return f"【{idx}】{clean_title(album.title)}\n{second}"


class SendFailed(Exception):
    """OneBot 发送接口报错，交给 AstrBot 重发也会同样失败。

    rejected：QQ（OneBot 实现）收到请求后拒绝发送；否则是超时、连接中断等，请求可能没送到。
    """

    def __init__(self, message: str, rejected: bool):
        super().__init__(message)
        self.rejected = rejected

    @classmethod
    def of(cls, error: Exception) -> "SendFailed":
        # aiocqhttp 的 ActionFailed 是 OneBot 实现返回了失败；NetworkError 等是请求没拿到回应
        return cls(str(error), type(error).__name__ == "ActionFailed")


class Stage:
    """图片中转目录：QQ 机器人（如 NapCat）也能以同一路径读取的目录。

    发送前把图片复制过去，消息里只放 file:// 路径，不再把整张图编码进消息；发完删掉。
    """

    def __init__(self, root: Path):
        self.root = root

    def prepare(self):
        """建好目录并删掉上次没删掉的图片。"""
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.glob(f"{STAGE_PREFIX}*"):
            path.unlink(missing_ok=True)

    @staticmethod
    def supports(chain: list) -> bool:
        """消息里只有文字、本地图片和视频以及（只含这些的）合并转发时才能改用文件路径发送。"""
        for comp in chain:
            if isinstance(comp, Comp.Nodes):
                if not all(Stage.supports(node.content) for node in comp.nodes):
                    return False
            elif isinstance(comp, (Comp.Image, Comp.Video)):
                if not getattr(comp, "path", ""):
                    return False
            elif not isinstance(comp, Comp.Plain):
                return False
        return True

    def segments(self, content: list, copies: list[Path]) -> list[dict]:
        """消息段 → OneBot 消息段：图片复制到中转目录，复制出的文件记进 copies。"""
        out = []
        for comp in content:
            if isinstance(comp, (Comp.Image, Comp.Video)):
                src = Path(comp.path)
                dest = self.root / f"{STAGE_PREFIX}{uuid.uuid4().hex}{src.suffix}"
                shutil.copyfile(src, dest)
                copies.append(dest)
                kind = "video" if isinstance(comp, Comp.Video) else "image"
                out.append({"type": kind, "data": {"file": dest.resolve().as_uri()}})
            elif comp.text.strip():
                out.append({"type": "text", "data": {"text": comp.text}})
        return out

    def forward(self, nodes, copies: list[Path]) -> list[dict]:
        return [
            {
                "type": "node",
                "data": {
                    "user_id": str(node.uin),
                    "nickname": node.name,
                    "content": self.segments(node.content, copies),
                },
            }
            for node in nodes.nodes
        ]

    @staticmethod
    def remove(copies: list[Path]):
        for path in copies:
            path.unlink(missing_ok=True)

    @staticmethod
    def remove_later(copies: list[Path], delay: float = STAGE_KEEP_SECONDS):
        if copies:
            asyncio.get_running_loop().call_later(delay, Stage.remove, list(copies))


async def send_direct(
    event: AstrMessageEvent, chain: list, stage: Stage | None = None
) -> tuple[bool, str | None]:
    """QQ（OneBot）上直接调用发送接口，拿到消息 ID 供 /pdf 按回复查图集。

    返回 (是否已发送, 消息 ID)。其他平台或组装消息出错时返回未发送，由 AstrBot 照常发送；
    发送接口本身报错时抛出 SendFailed。stage 是图片中转目录（可选）。
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

    有中转目录时图片以文件路径发送，否则由 AstrBot 编码成 base64。
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
                nodes = await asyncio.to_thread(stage.forward, chain[0], copies)
                payload = {"messages": nodes}
            else:
                payload = await chain[0].to_dict()
            action = (
                "send_group_forward_msg" if group_id else "send_private_forward_msg"
            )
        else:
            if staged:
                message = await asyncio.to_thread(stage.segments, chain, copies)
            else:
                message = await AiocqhttpMessageEvent._parse_onebot_json(
                    MessageChain(chain)
                )
            payload = {"message": message}
            action = "send_group_msg" if group_id else "send_private_msg"
    except Exception as e:
        Stage.remove(copies)
        logger.warning(f"[random_pic] 组装消息失败，改由 AstrBot 发送: {e!r}")
        return False, None
    try:
        ret = await bot.call_action(action, **payload, **target)
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


class Dispatcher:
    """发送抽到的图集：登记到会话、排成消息逐条发送，并按消息 ID 登记其中的图集（/pdf 按回复查）。"""

    def __init__(
        self,
        history: History,
        composer: Composer,
        mode: str,
        stage: Stage | None = None,
    ):
        """stage：图片中转目录，QQ 上直接发送时使用（可选）。"""
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
