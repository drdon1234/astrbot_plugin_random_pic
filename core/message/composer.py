"""按发送模式将图集和视频组装为消息。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING

import astrbot.api.message_components as Comp

from ..models import Album
from ..storage.history import clean_title, header_text, page_text
from ..utils import duration_text
from .types import (
    FORWARD,
    FORWARD_MAX_NODES,
    INLINE_BUDGET,
    MIXED,
    MIXED_PER_MESSAGE,
    ONEBOT,
    SEPARATE,
    Message,
)

if TYPE_CHECKING:
    from ..commands.catalog import CommandCatalog


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
    def __init__(
        self,
        header: bool,
        caption: bool,
        budget: int | None = INLINE_BUDGET,
        commands: CommandCatalog | None = None,
    ):
        """budget：一条消息里图片的总字节数上限，None 为不限（图片以文件路径发送时）。"""
        self.header = header
        self.caption = caption
        self.budget = budget
        self.commands = commands

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
            if self.commands and album.work and (self.header or self.caption):
                # 关闭标题后，引用文字无法恢复批次序号；多作品交给选择提示确认。
                target = str(idx) if self.header else ""
                choices = [
                    self.commands.command(action, target)
                    for action in ("全集", "pdf")
                    if self.commands.enabled(action)
                ]
                if choices:
                    content.append(
                        Comp.Plain(
                            "\n获取完整作品：回复此消息发送 "
                            + " 或 ".join(choices)
                            + "。"
                        )
                    )
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

    def video_messages(
        self, albums: list[Album], staged: bool, mode: str, uin: str
    ) -> list[Message]:
        """视频的消息。QQ 的视频消息不能带文字，所以每个视频的标题和说明单独一条（或一个节点）。

        合并转发（QQ 上，只有一个视频时也用）：标题、视频各一个节点，节点数或大小超过上限时拆成几条；
        其他方式：每个视频先发一条标题和说明，再单独发视频。
        staged 为 False（没有共享目录）时视频编码进消息，单个超过大小上限的不发。
        """
        parts: list[tuple[int, list]] = []  # (序号, 消息段)，依次是标题、视频
        for idx, album in enumerate(albums, 1):
            _, path = album.pictures[0]
            text = []
            if self.header:
                text.append(video_header(idx, album))
            if self.caption and album.details:
                text += album.details
            if text:
                parts.append((idx, [Comp.Plain("\n".join(text))]))
            if staged:
                parts.append((idx, [Comp.Video.fromFileSystem(str(path))]))
            elif self._over(_size(path)):
                parts.append((0, [Comp.Plain("视频太大，没有共享目录时发不出去。")]))
            else:
                video = Comp.Video.fromBase64(
                    base64.b64encode(path.read_bytes()).decode()
                )
                parts.append((idx, [video]))
        if mode != FORWARD:
            return [(content, [idx] if idx else []) for idx, content in parts]
        messages, nodes, idxs, size = [], [], [], 0
        for idx, content in parts:
            n = sum(
                _size(Path(c.path)) if getattr(c, "path", "") else len(c.file)
                for c in content
                if isinstance(c, Comp.Video)
            )
            if nodes and (len(nodes) >= FORWARD_MAX_NODES or self._over(size + n)):
                messages.append(([Comp.Nodes(nodes)], idxs))
                nodes, idxs, size = [], [], 0
            nodes.append(Comp.Node(content=content, uin=uin, name="抽视频"))
            if idx and idx not in idxs:
                idxs.append(idx)
            size += n if self.budget else 0
        if nodes:
            messages.append(([Comp.Nodes(nodes)], idxs))
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
