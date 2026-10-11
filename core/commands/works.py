"""完整作品定位、随机抽取和发送。"""

from __future__ import annotations

from contextlib import aclosing
from typing import TYPE_CHECKING

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..config.settings import FORWARD_FORMAT, PDF_FORMAT
from ..message.types import FORWARD_MAX_NODES, ONEBOT, SendFailed
from ..models import Album, DrawRequest, Work
from ..services.works import describe
from ..sources.base import Source, SourceError
from ..storage.history import SentAlbum, albums_from_text, pick
from ..storage.pdf import PdfError
from .common import delayed_notice, event_args, event_sender, send_failed_text
from .help import help_text
from .parser import WHOLE_COMMANDS

if TYPE_CHECKING:
    from ..policy.access import Quota
    from ..runtime import Runtime


class WorkCommands:
    """处理完整作品和 PDF 命令。"""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    async def draw_random(
        self, event: AstrMessageEvent, req: DrawRequest, user_id: str, *, retry: str
    ):
        """/抽图 全集：随机抽一个完整作品发送。"""
        quota, denied = self.runtime.access.begin(user_id)
        if denied:
            yield event.plain_result(denied)
            return
        with quota:
            retry = f"请发送 {retry} 重试。"
            async with delayed_notice(event, "正在寻找符合条件的完整作品，请稍候。"):
                picked = await self.runtime.works.random(
                    req, event.is_private_chat(), user_id
                )
            if isinstance(picked, str):
                logger.warning(f"[random_pic] 获取完整作品失败 {req}: {picked}")
                yield event.plain_result(f"获取完整作品失败：{picked}\n{retry}")
                return
            album, source, work = picked
            async with aclosing(self.send(
                event, source, work, req.whole, album, retry=retry, quota=quota
            )) as stream:
                async for result in stream:
                    yield result

    async def whole(self, event: AstrMessageEvent, word: str):
        """/全集、/pdf：回复抽到的图片或给出作品链接，发送整个作品。"""
        tokens = event_args(self.runtime, event)
        if tokens is None:
            return
        if self.runtime.commands.is_help(tokens):
            yield event.plain_result(
                help_text(
                    self.runtime,
                    is_admin=self.runtime.access.is_admin(str(event.get_sender_id())),
                )
            )
            return
        whole = self.runtime.settings.whole
        if not whole.enabled:
            yield event.plain_result("完整作品功能未开启。")
            return
        album, hint = self.target(event, tokens, word)
        if album is None:
            yield event.plain_result(hint)
            return
        if album.work is None:
            command = self.runtime.commands.command(word, "<作品链接>")
            yield event.plain_result(
                f"第 {album.idx} 个图集没有可用的单个作品引用；跨帖精选不能整体获取。"
                f"请复制图集说明中的某一帖链接，发送 {command}；"
                "没有链接时请重新抽图，再引用新图片消息。"
            )
            return
        quota, denied = self.runtime.access.begin(str(event.get_sender_id()))
        if denied:
            yield event.plain_result(denied)
            return
        with quota:
            retry = self._retry(word, tokens)
            async with delayed_notice(event, "正在查询作品信息，请稍候。"):
                source, work, why = await self.runtime.works.lookup(
                    album.work, event.is_private_chat(), str(event.get_sender_id())
                )
            if source is None:
                yield event.plain_result(
                    f"{why}\n可换一个符合条件的作品；"
                    f"当前可用操作见 {self.runtime.commands.command('帮助')}。"
                )
                return
            fmt = WHOLE_COMMANDS[word] or whole.default_format
            async with aclosing(self.send(
                event, source, work, fmt, retry=retry, quota=quota
            )) as stream:
                async for result in stream:
                    yield result

    async def send(
        self,
        event: AstrMessageEvent,
        source: Source,
        work: Work,
        fmt: str,
        album: Album | None = None,
        *,
        retry: str,
        quota: Quota,
    ):
        """用合并转发或 PDF 发送整个作品。合并转发只在 QQ 上可用，其他平台改发 PDF。"""
        denied = quota.reserve(work.pages)
        if denied:
            yield event.plain_result(denied)
            return
        if fmt == FORWARD_FORMAT and event.get_platform_name() != ONEBOT:
            fmt = PDF_FORMAT
        if fmt == PDF_FORMAT:
            async with aclosing(self.send_pdf(
                event, source, work, album, retry=retry, quota=quota
            )) as stream:
                async for result in stream:
                    yield result
            return

        parts = -(-work.pages // FORWARD_MAX_NODES)
        split = f"，分 {parts} 条" if parts > 1 else ""
        yield event.plain_result(
            describe(
                source,
                work,
                f"已接收，准备下载，可能需要排队；完成后用合并转发发送{split}。",
            )
        )
        sender, fallback = event_sender(self.runtime, event, quota=quota)
        try:
            async with self.runtime.works.download(source, work) as (paths, missing):
                if not paths:
                    yield event.plain_result(f"下载失败：没有下载到任何图片。\n{retry}")
                    return
                denied = quota.reserve(len(paths))
                if denied:
                    yield event.plain_result(denied)
                    return
                whole = self.runtime.works.whole_album(source, work, paths, album)
                sent = (
                    self.runtime.dispatcher.record(event.unified_msg_origin, [whole])
                    if album is not None
                    else [SentAlbum(1, whole.title, whole.source, whole.work)]
                )
                messages = self.runtime.dispatcher.composer.forward_album(
                    whole, str(event.get_self_id())
                )
                failed = await self.runtime.dispatcher.send_all(messages, sent, sender)
                for chain in fallback:
                    yield event.chain_result(chain)
                    sender.confirm(chain)
        except SourceError as e:
            yield event.plain_result(f"下载失败：{e}\n{retry}")
            return
        except Exception as e:
            logger.exception(f"[random_pic] 下载作品 {work.ref} 失败")
            yield event.plain_result(f"下载失败：{e!r}\n{retry}")
            return
        if failed:
            yield event.plain_result(
                send_failed_text(failed, len(messages), event.is_private_chat())
                + f"\n{retry}"
            )
        if missing:
            yield event.plain_result(f"有 {missing} 页下载失败，没有发出。\n{retry}")

    async def send_pdf(
        self,
        event: AstrMessageEvent,
        source: Source,
        work: Work,
        album: Album | None,
        *,
        retry: str,
        quota: Quota,
    ):
        files = self.runtime.pdf.cached(work)
        missing = 0
        if album is not None:
            # 随机抽到的作品登记到会话，之后可以 /全集 换方式再要
            self.runtime.dispatcher.record(
                event.unified_msg_origin,
                [self.runtime.works.whole_album(source, work, [], album)],
            )
        if files is None:
            if self.runtime.pdf.building(work):
                yield event.plain_result(
                    describe(
                        source,
                        work,
                        "该作品正在处理，将复用已有任务的结果，请等待文件发送。",
                    )
                )
            else:
                parts = self.runtime.pdf.parts(work.pages)
                split = f"，分 {parts} 个文件" if parts > 1 else ""
                yield event.plain_result(
                    describe(
                        source,
                        work,
                        f"已接收，准备下载，可能需要排队；打包完成后发送 PDF{split}。",
                    )
                )
            try:
                files, missing = await self.runtime.pdf.get(
                    work, str(event.get_sender_id()), source.download_work
                )
            except (PdfError, SourceError) as e:
                yield event.plain_result(f"打包失败：{e}\n{retry}")
                return
            except Exception as e:
                logger.exception(f"[random_pic] 作品 {work.ref} 打包失败")
                yield event.plain_result(f"打包失败：{e!r}\n{retry}")
                return
            if missing:
                yield event.plain_result(
                    f"有 {missing} 页下载失败，PDF 中缺少这些页。\n{retry}"
                )
        pages = sum(file.pages for file in files)
        denied = self.runtime.works.page_limit(pages + missing) or quota.reserve(pages)
        if denied:
            yield event.plain_result(denied)
            return
        sender, fallback = event_sender(self.runtime, event)
        failed, pending = [], []
        for file in files:
            chain = [Comp.File(name=file.name, file=str(file.path.resolve()))]
            before = sender.sent
            try:
                await sender(chain)
            except SendFailed as e:
                failed.append(e)
                continue
            if sender.sent > before:
                quota.charge(file.pages)
            else:
                pending.append(file.pages)
        for chain, pages in zip(fallback, pending):
            yield event.chain_result(chain)
            sender.confirm(chain)
            quota.charge(pages)
        if failed:
            yield event.plain_result(
                send_failed_text(failed, len(files), event.is_private_chat())
                + f"\n{retry}"
            )

    def target(
        self, event: AstrMessageEvent, tokens: list[str], word: str
    ) -> tuple[SentAlbum | None, str]:
        """依次看参数里的作品链接、被回复的消息、本会话上一次抽卡。

        回复的消息里有多个图集时必须带序号，不带就列出来提示。
        """
        command = self.runtime.commands.command(word)
        usage = f"请只提供一个作品链接，或引用原图片消息发送 {command} <序号>。"
        if len(tokens) > 1:
            return None, f"一次只能选择一个作品，不能混用链接、序号或其他参数。{usage}"
        index = None
        if tokens:
            token = tokens[0]
            if token.isdecimal():
                try:
                    index = int(token)
                except ValueError:
                    return None, f"序号无效。{usage}"
                if index < 1:
                    return None, f"序号必须从 1 开始。{usage}"
            else:
                if not token.lower().startswith(("https://", "http://")):
                    return None, f"无法识别这个参数。{usage}"
                refs = self.runtime.sources.links(token)
                if len(refs) == 1:
                    return SentAlbum(0, "", "", refs[0]), ""
                if refs:
                    return None, f"识别到多个作品链接，无法确定目标。{usage}"
                return None, f"无法识别这个参数或作品链接。{usage}"
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
            return pick(albums, index, strict=True, command=command)
        return pick(
            self.runtime.history.last(event.unified_msg_origin),
            index,
            strict=False,
            command=command,
        )

    def _retry(self, word: str, tokens: list[str]) -> str:
        command = self.runtime.commands.command(word, " ".join(tokens))
        if not any(self.runtime.sources.links(t) for t in tokens):
            return f"请引用原图片消息，发送 {command} 重试。"
        return f"请发送 {command} 重试。"
