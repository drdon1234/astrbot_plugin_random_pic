"""随机抽图插件入口：装配各模块、注册指令、解析参数、检查权限。"""

import asyncio
import re
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .access import AccessControl
from .drawer import Drawer
from .filters import HEAVY_TAGS, ContentFilter
from .history import History, SentAlbum, albums_from_text, pick
from .models import (
    ANIME,
    CONCURRENCY,
    EXPLICIT,
    EXPLICIT_SKIP,
    RATING_NAMES,
    RATING_WORDS,
    REAL,
    SENSITIVE,
    STYLE_NAMES,
    STYLE_WORDS,
    Album,
    DrawOptions,
    DrawRequest,
    Work,
)
from .net import HttpClient, ImageCache
from .pdf import PdfError, PdfStore
from .pool import Reserve
from .push import Pusher
from .sender import (
    FORWARD_MAX_NODES,
    ONEBOT,
    Composer,
    Dispatcher,
    send_direct,
)
from .settings import FORWARD_FORMAT, PDF_FORMAT, Settings
from .sources import SourceSet
from .sources.base import Source, SourceError
from .sources.ehentai_api import resolve_site, site_cookies
from .tags import TagDB
from .works import WorkService, describe

PLUGIN_NAME = "astrbot_plugin_random_pic"
HELP_WORDS = {"help", "帮助"}
# 数量写成「图集数x每集张数」
COUNT_RE = re.compile(r"(\d+)\s*[x*×]\s*(\d+)")
# 抽图指令 → 预设参数；除 /抽图、/随机角色 外都是别名
DRAW_COMMANDS = {
    "抽图": {},
    "随机角色": {"random_character": True},
    "二次元": {"style": ANIME},
    "三次元": {"style": REAL},
    "擦边": {"rating": SENSITIVE},
    "色图": {"rating": EXPLICIT},
}
ALIASES = {"二次元", "三次元", "擦边", "色图"}
# 完整作品指令 → 发送方式（None 为配置的默认方式）
WHOLE_COMMANDS = {"全集": None, "pdf": PDF_FORMAT}
# 抽图参数里表示「随机抽一个完整作品」的词 → 发送方式（None 为配置的默认方式）
WHOLE_WORDS = {"全集": None, "pdf": PDF_FORMAT}
# 群聊不带唤醒前缀时也能触发的指令词（整条消息就是指令词，或后面跟空格和参数）
NO_PREFIX_RE = rf"(?i)^\s*({'|'.join([*DRAW_COMMANDS, *WHOLE_COMMANDS])})(?:\s|$)"


def parse_args(
    tokens: list[str], max_images: int, defaults: DrawRequest, whole_format: str
) -> DrawRequest:
    """宽松解析：风格词、分级词、数量、全集 / pdf 可任意顺序，其余当作关键词。

    数量写一个数字是图集数，写成「图集数x每集张数」（如 3x4）同时指定每集张数。
    二次元来自单图站，每集固定 1 张。
    总张数不超过 max_images：超出时先减少每集张数，再减少图集数。
    带「全集」「pdf」时随机抽一个完整作品，「全集」用 whole_format 发送，「pdf」发 PDF。
    """
    req = DrawRequest(
        defaults.style,
        defaults.rating,
        albums=defaults.albums,
        per_album=defaults.per_album,
        random_character=defaults.random_character,
    )
    for token in tokens:
        low = token.lower()
        if token in STYLE_WORDS:
            req.style = STYLE_WORDS[token]
        elif low in RATING_WORDS:
            req.rating = RATING_WORDS[low]
        elif low in WHOLE_WORDS:
            req.whole = WHOLE_WORDS[low] or whole_format
        elif token.isdigit():
            req.albums = int(token)
        elif match := COUNT_RE.fullmatch(low):
            req.albums, req.per_album = int(match.group(1)), int(match.group(2))
        else:
            req.keywords.append(token)
    if req.whole:
        req.albums = req.per_album = 1
    if req.style == ANIME:
        req.per_album = 1
    req.per_album = max(1, min(req.per_album, max_images))
    req.albums = max(1, min(req.albums, max_images // req.per_album))
    return req


class RandomPicPlugin(Star):
    """随机抽图：/抽图 [二次元|三次元] [擦边|r18] [关键词...] [数量]"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = s = Settings.load(config)
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.access = AccessControl(s.access)
        self.content = ContentFilter(
            s.filter.extra_blacklist,
            HEAVY_TAGS if s.filter.block_heavy else frozenset(),
            s.filter.block_ai,
            s.filter.block_trans,
        )

        eh = s.sources.ehentai
        site = resolve_site(eh.site, eh.cookies)
        self.http = HttpClient(s.sources.proxy, site_cookies(site, eh.cookies))
        cache = ImageCache(self.http, data_dir / "cache")
        opts = DrawOptions(
            from_start=s.draw.from_start,
            explicit_skip=EXPLICIT_SKIP,
            concurrency=CONCURRENCY,
        )
        self.sources = SourceSet(
            s, self.http, cache, self.content, opts, data_dir, site
        )
        self.tagdb = TagDB(self.http, data_dir / "ehtag.json.gz")
        self.drawer = Drawer(self.sources, self.content, self.tagdb)
        self.reserve = self._reserve(data_dir / "reserve")
        self.history = History(data_dir / "sent_albums.json")
        self.dispatcher = Dispatcher(
            self.history, Composer(s.send.header, s.send.caption), s.send.mode
        )
        self.works = WorkService(
            self.sources,
            self.access,
            self.drawer,
            data_dir / "work_tmp",
            s.whole.max_pages,
        )
        self.pdf = PdfStore(
            Path(s.whole.pdf_dir) if s.whole.pdf_dir else data_dir / "pdf",
            data_dir / "pdf_tmp",
            self.sources.keys,
        )
        self.pusher = Pusher(
            s.push.tasks,
            context,
            self.drawer,
            self.access,
            self.dispatcher,
            self.works,
            self.pdf,
            lambda tokens: self._request("抽图", tokens),
            self.reserve,
        )
        self._preload: asyncio.Task | None = None

    async def initialize(self):
        # 后台预加载标签库，避免第一次抽卡时等待下载
        self._preload = asyncio.create_task(self.tagdb.get())
        self.reserve.start()
        self.pusher.start()

    async def terminate(self):
        if self._preload and not self._preload.done():
            self._preload.cancel()
        self.pusher.stop()
        self.reserve.stop()
        await self.http.close()
        await self.sources.close()

    def _reserve(self, root: Path) -> Reserve:
        """按默认抽图参数维护擦边、R18 预备池（R18 群聊私聊都不允许时不维护）。"""
        s = self.settings
        default = self._request("抽图", [])
        ratings = [SENSITIVE]
        if s.access.group_r18 or s.access.private_r18:
            ratings.append(EXPLICIT)
        allowed = {
            rating: {
                source.key
                for source, _ in self.sources.drawing
                if source.style == default.style and rating in source.usable_ratings
            }
            for rating in ratings
        }
        return Reserve(
            lambda req, allow: self.drawer.draw(req, allow),
            root,
            s.draw.reserve_batches,
            default.style,
            default.albums,
            default.per_album,
            ratings,
            allowed,
        )

    @filter.command("抽图")
    async def draw_pic(self, event: AstrMessageEvent):
        """随机抽图。用法：/抽图 [二次元|三次元] [擦边|r18] [关键词...] [数量]，/抽图 帮助 查看说明"""
        async for result in self._draw(event, "抽图"):
            yield result

    @filter.command("随机角色")
    async def random_character(self, event: AstrMessageEvent):
        """每个图集先随机抽一个角色再抽图。用法同 /抽图"""
        async for result in self._draw(event, "随机角色"):
            yield result

    @filter.command("二次元")
    async def alias_anime(self, event: AstrMessageEvent):
        """随机二次元图，等同于 /抽图 二次元"""
        async for result in self._draw(event, "二次元"):
            yield result

    @filter.command("三次元")
    async def alias_real(self, event: AstrMessageEvent):
        """随机三次元图，等同于 /抽图 三次元"""
        async for result in self._draw(event, "三次元"):
            yield result

    @filter.command("擦边")
    async def alias_sensitive(self, event: AstrMessageEvent):
        """随机擦边图，等同于 /抽图 擦边"""
        async for result in self._draw(event, "擦边"):
            yield result

    @filter.command("色图")
    async def alias_explicit(self, event: AstrMessageEvent):
        """随机 R18 图（仅私聊），等同于 /抽图 r18"""
        async for result in self._draw(event, "色图"):
            yield result

    @filter.command("全集")
    async def whole_work(self, event: AstrMessageEvent):
        """回复抽到的图片，获取整个作品（默认用合并转发）。也可以 /全集 <作品链接>"""
        async for result in self._whole(event, "全集"):
            yield result

    @filter.command("pdf")
    async def whole_pdf(self, event: AstrMessageEvent):
        """回复抽到的图片，获取整个作品的 PDF。也可以 /pdf <作品链接>"""
        async for result in self._whole(event, "pdf"):
            yield result

    @filter.regex(NO_PREFIX_RE)
    async def no_prefix(self, event: AstrMessageEvent):
        """群聊里不带 / 也能触发本插件的指令。"""
        # 带了唤醒前缀、@ 了机器人或私聊时，已经由上面的指令处理
        if getattr(event, "is_at_or_wake_command", False):
            return
        if not self.settings.draw.no_prefix:
            return
        word = event.message_str.split()[0].lower()
        handler = (
            self._whole(event, word)
            if word in WHOLE_COMMANDS
            else self._draw(event, word)
        )
        async for result in handler:
            yield result

    def _args(self, event: AstrMessageEvent) -> list[str] | None:
        """指令后面的参数；用户或群没有权限时返回 None（不回复）。"""
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        if not self.access.allowed(user_id, group_id):
            return None
        return event.message_str.split()[1:]

    def _request(self, word: str, tokens: list[str]) -> DrawRequest:
        """按抽图指令词和参数生成请求。"""
        draw = self.settings.draw
        preset = DRAW_COMMANDS[word]
        defaults = DrawRequest(
            preset.get("style", draw.style),
            preset.get("rating", draw.rating),
            albums=draw.album_count,
            per_album=draw.images_per_album,
            random_character=preset.get("random_character", False),
        )
        return parse_args(
            tokens, draw.max_images, defaults, self.settings.whole.default_format
        )

    # ---- 抽图 ----

    async def _draw(self, event: AstrMessageEvent, word: str):
        if word in ALIASES and not self.settings.draw.aliases:
            return
        tokens = self._args(event)
        if tokens is None:
            return
        if tokens and tokens[0].lower() in HELP_WORDS:
            yield event.plain_result(self.help_text())
            return
        req = self._request(word, tokens)
        if req.whole and not self.settings.whole.enabled:
            yield event.plain_result("完整作品功能未开启。")
            return
        is_private = event.is_private_chat()
        user_id = str(event.get_sender_id())
        denied = self.access.gate(req.rating, is_private, user_id) or self.access.take(
            user_id, req
        )
        if denied:
            yield event.plain_result(denied)
            return
        if req.whole:
            async for result in self._draw_whole(event, req, user_id):
                yield result
            return

        async with self.reserve.draw(
            req, self.access.explicit_allowed(is_private, user_id), self.drawer.draw
        ) as result:
            if not result.albums:
                logger.warning(f"[random_pic] 获取失败 {req}: {result.reason()}")
                yield event.plain_result(
                    f"获取{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}图片失败："
                    f"{result.reason()}"
                )
                return

            self.access.used(user_id, result.images)
            sender, fallback = self._sender(event)
            failed, total = await self.dispatcher.deliver(
                result.albums,
                event.unified_msg_origin,
                event.get_platform_name(),
                str(event.get_self_id()),
                sender,
            )
            for chain in fallback:
                yield event.chain_result(chain)
            if failed:
                yield event.plain_result(self._send_failed_text(failed, total, is_private))
            if len(result.albums) < req.albums:
                yield event.plain_result(
                    f"仅获取到 {len(result.albums)}/{req.albums} 个图集。"
                )

    async def _draw_whole(
        self, event: AstrMessageEvent, req: DrawRequest, user_id: str
    ):
        """/抽图 全集：随机抽一个完整作品发送。"""
        picked = await self.works.random(req, event.is_private_chat(), user_id)
        if isinstance(picked, str):
            logger.warning(f"[random_pic] 获取完整作品失败 {req}: {picked}")
            yield event.plain_result(f"获取完整作品失败：{picked}")
            return
        album, source, work = picked
        self.access.used(user_id, work.pages)
        async for result in self._send_work(event, source, work, req.whole, album):
            yield result

    def _sender(self, event: AstrMessageEvent):
        """QQ 上直接发送并拿到消息 ID；发不出去的（其他平台、组装出错）收集起来交给 AstrBot 发。"""
        fallback: list[list] = []

        async def send(chain: list) -> str | None:
            delivered, message_id = await send_direct(event, chain)
            if not delivered:
                fallback.append(chain)
            return message_id

        return send, fallback

    def _send_failed_text(self, failed: int, total: int, is_private: bool) -> str:
        what = "这条消息" if total == 1 else f"其中 {failed}/{total} 条消息"
        text = f"发送失败：QQ 拒发了{what}。"
        if not is_private:
            return text + "群聊里裸露较多的图容易被 QQ 拦截，可以私聊重新抽图。"
        return text + "可能是图片内容被 QQ 拦截，可换个关键词或稍后重试。"

    # ---- 完整作品 ----

    async def _whole(self, event: AstrMessageEvent, word: str):
        """/全集、/pdf：回复抽到的图片或给出作品链接，发送整个作品。"""
        tokens = self._args(event)
        if tokens is None:
            return
        if tokens and tokens[0].lower() in HELP_WORDS:
            yield event.plain_result(self.help_text())
            return
        whole = self.settings.whole
        if not whole.enabled:
            yield event.plain_result("完整作品功能未开启。")
            return
        album, hint = self._work_target(event, tokens)
        if album is None:
            yield event.plain_result(hint)
            return
        if album.work is None:
            yield event.plain_result(
                f"第 {album.idx} 个图集没有识别到作品，请回复抽图发出的那条消息，"
                f"或使用 /{word} <作品链接>。"
            )
            return
        source, work, why = await self.works.lookup(
            album.work, event.is_private_chat(), str(event.get_sender_id())
        )
        if source is None:
            yield event.plain_result(why)
            return
        fmt = WHOLE_COMMANDS[word] or whole.default_format
        async for result in self._send_work(event, source, work, fmt):
            yield result

    async def _send_work(
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
            async for result in self._send_pdf(event, source, work, album):
                yield result
            return

        parts = -(-work.pages // FORWARD_MAX_NODES)
        split = f"，分 {parts} 条" if parts > 1 else ""
        yield event.plain_result(
            describe(source, work, f"下载完成后用合并转发发送{split}……")
        )
        sender, fallback = self._sender(event)
        try:
            async with self.works.download(source, work) as (paths, missing):
                if not paths:
                    yield event.plain_result("下载失败：没有下载到任何图片。")
                    return
                whole = self.works.whole_album(source, work, paths, album)
                sent = self.dispatcher.record(event.unified_msg_origin, [whole])
                messages = self.dispatcher.composer.forward_album(
                    whole, str(event.get_self_id())
                )
                failed = await self.dispatcher.send_all(messages, sent, sender)
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
                self._send_failed_text(failed, len(messages), event.is_private_chat())
            )
        if missing:
            yield event.plain_result(f"有 {missing} 页下载失败，没有发出。")

    async def _send_pdf(
        self,
        event: AstrMessageEvent,
        source: Source,
        work: Work,
        album: Album | None,
    ):
        files = self.pdf.cached(work)
        if album is not None:
            # 随机抽到的作品登记到会话，之后可以 /全集 换方式再要
            self.dispatcher.record(
                event.unified_msg_origin,
                [self.works.whole_album(source, work, [], album)],
            )
        if files is None:
            # 排队等待不提示；同一作品已在打包时直接等它的结果
            if not self.pdf.building(work):
                parts = self.pdf.parts(work.pages)
                split = f"，分 {parts} 个文件" if parts > 1 else ""
                yield event.plain_result(
                    describe(source, work, f"打包完成后发送 PDF{split}……")
                )
            try:
                files, missing = await self.pdf.get(
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

    def _work_target(
        self, event: AstrMessageEvent, tokens: list[str]
    ) -> tuple[SentAlbum | None, str]:
        """依次看参数里的作品链接、被回复的消息、本会话上一次抽卡。

        回复的消息里有多个图集时必须带序号，不带就列出来提示。
        """
        for token in tokens:
            refs = self.sources.links(token)
            if refs:
                return SentAlbum(0, "", "", refs[0]), ""
        index = next((int(t) for t in tokens if t.isdigit()), None)
        reply = next(
            (c for c in event.message_obj.message if isinstance(c, Comp.Reply)), None
        )
        if reply is not None:
            albums = self.history.by_message(str(reply.id)) if reply.id else None
            if albums is None:
                text = getattr(reply, "message_str", "") or ""
                albums = albums_from_text(text, self.sources.links)
            return pick(albums, index, strict=True)
        return pick(self.history.last(event.unified_msg_origin), index, strict=False)

    def help_text(self) -> str:
        s = self.settings
        draw, access = s.draw, s.access
        on = {True: "允许", False: "不允许"}
        lines = [
            "【抽图用法】",
            "/抽图 [二次元|三次元] [擦边|r18] [关键词...] [图集数 或 图集数x每集张数]",
            f"· 参数顺序随意；不写时默认 {STYLE_NAMES[draw.style]}·"
            f"{RATING_NAMES[draw.rating]}、{draw.album_count} 个图集、每集 "
            f"{draw.images_per_album} 张，每次最多 {draw.max_images} 张",
            "· 一个图集是同一作品里的几张图；例如 /抽图 3x4 抽 3 个图集、每集 4 张",
            "· 关键词可写中文角色、作品名（如 芙莉莲、原神）或标签，前缀 - 表示排除",
            "/随机角色 [同上]：每个图集先随机抽一个角色",
        ]
        if s.whole.enabled:
            lines += [
                f"/全集：回复抽到的图片，获取整个作品（{s.whole.default_format}）；"
                "/pdf：同上，打包成 PDF",
                "· 回复的消息里有多个图集时加序号（【】里的数字），如 /全集 2；也可以 /全集 <作品链接>",
                "· /抽图 全集 [参数]：直接随机抽一个完整作品，写 pdf 则打包成 PDF",
            ]
        if draw.aliases:
            lines.append("别名：/二次元 /三次元 /擦边 /色图")
        lines.append(f"R18：群聊{on[access.group_r18]}，私聊{on[access.private_r18]}")
        lines += self.sources.help_lines()
        lines += [
            "示例：/抽图 原神 2　/抽图 二次元 芙莉莲　/抽图 全集 原神",
            "/抽图 帮助：显示本说明",
        ]
        return "\n".join(lines)
