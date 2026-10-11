"""完整作品定位、随机抽取和发送。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..config.settings import FORWARD_FORMAT, PDF_FORMAT
from ..message.types import FORWARD_MAX_NODES, ONEBOT
from ..models import Album, DrawRequest, Work
from ..services.works import describe
from ..sources.base import Source, SourceError
from ..storage.history import SentAlbum, albums_from_text, pick
from ..storage.pdf import PdfError
from .common import event_args, event_sender, send_failed_text
from .help import help_text
from .parser import HELP_WORDS, WHOLE_COMMANDS

if TYPE_CHECKING:
    from ..runtime import Runtime


class WorkCommands:
    """处理完整作品和 PDF 命令。"""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def draw_random(
        self, event: AstrMessageEvent, req: DrawRequest, user_id: str
    ):
        """/抽图 全集：随机抽一个完整作品发送。"""
        picked = await self.runtime.works.random(req, event.is_private_chat(), user_id)
        if isinstance(picked, str):
            logger.warning(f"[random_pic] 获取完整作品失败 {req}: {picked}")
            yield event.plain_result(f"获取完整作品失败：{picked}")
            return
        album, source, work = picked
        self.runtime.access.used(user_id, work.pages)
        async for result in self.send(event, source, work, req.whole, album):
            yield result

    async def whole(self, event: AstrMessageEvent, word: str):
        """/全集、/pdf：回复抽到的图片或给出作品链接，发送整个作品。"""
        tokens = event_args(self.runtime, event)
        if tokens is None:
            return
        if tokens and tokens[0].lower() in HELP_WORDS:
            yield event.plain_result(help_text(self.runtime))
            return
        whole = self.runtime.settings.whole
        if not whole.enabled:
            yield event.plain_result("完整作品功能未开启。")
            return
        album, hint = self.target(event, tokens)
        if album is None:
            yield event.plain_result(hint)
            return
        if album.work is None:
            yield event.plain_result(
                f"第 {album.idx} 个图集没有识别到作品，请回复抽图发出的那条消息，"
                f"或使用 /{word} <作品链接>。"
            )
            return
        source, work, why = await self.runtime.works.lookup(
            album.work, event.is_private_chat(), str(event.get_sender_id())
        )
        if source is None:
            yield event.plain_result(why)
            return
        fmt = WHOLE_COMMANDS[word] or whole.default_format
        async for result in self.send(event, source, work, fmt):
            yield result

    async def send(
        self,
        event: AstrMessageEvent,
        source: Source,
        work: Work,
        fmt: str,
        album: Album | None = None,
    ):
        """用合并转发或 PDF 发送整个作品。合并转发只在 QQ 上可用，其他平台改发 PDF。"""
        if fmt == FORWARD_FORMAT and event.get_platform_name() != ONEBOT:
            fmt = PDF_FORMAT
        if fmt == PDF_FORMAT:
            async for result in self.send_pdf(event, source, work, album):
                yield result
            return

        parts = -(-work.pages // FORWARD_MAX_NODES)
        split = f"，分 {parts} 条" if parts > 1 else ""
        yield event.plain_result(
            describe(source, work, f"下载完成后用合并转发发送{split}……")
        )
        sender, fallback = event_sender(self.runtime, event)
        try:
            async with self.runtime.works.download(source, work) as (paths, missing):
                if not paths:
                    yield event.plain_result("下载失败：没有下载到任何图片。")
                    return
                whole = self.runtime.works.whole_album(source, work, paths, album)
                sent = self.runtime.dispatcher.record(event.unified_msg_origin, [whole])
                messages = self.runtime.dispatcher.composer.forward_album(
                    whole, str(event.get_self_id())
                )
                failed = await self.runtime.dispatcher.send_all(messages, sent, sender)
        except SourceError as e:
            yield event.plain_result(f"下载失败：{e}")
            return
        except Exception as e:
            logger.exception(f"[random_pic] 下载作品 {work.ref} 失败")
            yield event.plain_result(f"下载失败：{e!r}")
            return
        for chain in fallback:
            yield event.chain_result(chain)
        if failed:
            yield event.plain_result(
                send_failed_text(failed, len(messages), event.is_private_chat())
            )
        if missing:
            yield event.plain_result(f"有 {missing} 页下载失败，没有发出。")

    async def send_pdf(
        self,
        event: AstrMessageEvent,
        source: Source,
        work: Work,
        album: Album | None,
    ):
        files = self.runtime.pdf.cached(work)
        if album is not None:
            # 随机抽到的作品登记到会话，之后可以 /全集 换方式再要
            self.runtime.dispatcher.record(
                event.unified_msg_origin,
                [self.runtime.works.whole_album(source, work, [], album)],
            )
        if files is None:
            # 排队等待不提示；同一作品已在打包时直接等它的结果
            if not self.runtime.pdf.building(work):
                parts = self.runtime.pdf.parts(work.pages)
                split = f"，分 {parts} 个文件" if parts > 1 else ""
                yield event.plain_result(
                    describe(source, work, f"打包完成后发送 PDF{split}……")
                )
            try:
                files, missing = await self.runtime.pdf.get(
                    work, str(event.get_sender_id()), source.download_work
                )
            except (PdfError, SourceError) as e:
                yield event.plain_result(f"打包失败：{e}")
                return
            except Exception as e:
                logger.exception(f"[random_pic] 作品 {work.ref} 打包失败")
                yield event.plain_result(f"打包失败：{e!r}")
                return
            if missing:
                yield event.plain_result(f"有 {missing} 页下载失败，PDF 中缺少这些页。")
        for path, name in files:
            yield event.chain_result([Comp.File(name=name, file=str(path.resolve()))])

    def target(
        self, event: AstrMessageEvent, tokens: list[str]
    ) -> tuple[SentAlbum | None, str]:
        """依次看参数里的作品链接、被回复的消息、本会话上一次抽卡。

        回复的消息里有多个图集时必须带序号，不带就列出来提示。
        """
        for token in tokens:
            refs = self.runtime.sources.links(token)
            if refs:
                return SentAlbum(0, "", "", refs[0]), ""
        index = next((int(t) for t in tokens if t.isdigit()), None)
        reply = next(
            (c for c in event.message_obj.message if isinstance(c, Comp.Reply)), None
        )
        if reply is not None:
            albums = (
                self.runtime.history.by_message(str(reply.id)) if reply.id else None
            )
            if albums is None:
                text = getattr(reply, "message_str", "") or ""
                albums = albums_from_text(text, self.runtime.sources.links)
            return pick(albums, index, strict=True)
        return pick(
            self.runtime.history.last(event.unified_msg_origin), index, strict=False
        )
