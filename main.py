"""随机抽图插件入口：注册指令、解析参数、检查权限，抽卡与 /pdf 交给各模块。"""

import asyncio
import re
from pathlib import Path

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .access import AccessControl
from .drawer import Drawer
from .filters import ContentFilter, classify
from .history import (
    History,
    SentAlbum,
    albums_from_text,
    clean_title,
    gallery_from_text,
    pick,
)
from .models import (
    ANIME,
    EXPLICIT,
    RATING_NAMES,
    REAL,
    SENSITIVE,
    STYLE_NAMES,
    DrawOptions,
    DrawRequest,
)
from .net import HttpClient, HttpError, ImageCache
from .pdf import PdfError, PdfStore
from .sender import Composer, resolve_mode, send_direct
from .settings import Settings
from .sources.ehentai import EHentaiSource, build_pools
from .sources.ehentai_api import EH_NAMES, SITES, EHentai, EHentaiError, resolve_site
from .sources.pica import Picacomic
from .sources.sixteenk import SixteenK
from .tags import TagDB

PLUGIN_NAME = "astrbot_plugin_random_pic"
STYLE_WORDS = {"二次元": ANIME, "三次元": REAL}
RATING_WORDS = {"擦边": SENSITIVE, "r18": EXPLICIT, "色图": EXPLICIT}
HELP_WORDS = {"help", "帮助"}
# 数量写成「图集数x每集张数」
COUNT_RE = re.compile(r"(\d+)\s*[x*×]\s*(\d+)")
# 逐条发送时两条消息之间的间隔（秒），和 AstrBot 分段发送一致
SEND_INTERVAL = 0.5
# 抽图指令 → 预设参数
DRAW_COMMANDS = {
    "抽图": {},
    "随机角色": {"random_character": True},
    "二次元": {"style": ANIME},
    "三次元": {"style": REAL},
    "擦边": {"rating": SENSITIVE},
    "色图": {"rating": EXPLICIT},
}
ALIASES = {"二次元", "三次元", "擦边", "色图"}
PDF_COMMANDS = ("pdf", "全集")
# 群聊不带唤醒前缀时也能触发的指令词（整条消息就是指令词，或后面跟空格和参数）
NO_PREFIX_RE = rf"(?i)^\s*({'|'.join([*DRAW_COMMANDS, *PDF_COMMANDS])})(?:\s|$)"


def parse_args(
    tokens: list[str],
    max_images: int,
    style: str,
    rating: str,
    albums: int,
    per_album: int,
) -> DrawRequest:
    """宽松解析：风格词、分级词、数量可任意顺序，其余当作关键词。

    数量写一个数字是图集数，写成「图集数x每集张数」（如 3x4）同时指定每集张数。
    总张数不超过 max_images：超出时先减少每集张数，再减少图集数。
    """
    req = DrawRequest(style, rating, albums=albums, per_album=per_album)
    for token in tokens:
        low = token.lower()
        if token in STYLE_WORDS:
            req.style = STYLE_WORDS[token]
        elif low in RATING_WORDS:
            req.rating = RATING_WORDS[low]
        elif token.isdigit():
            req.albums = int(token)
        elif match := COUNT_RE.fullmatch(low):
            req.albums, req.per_album = int(match.group(1)), int(match.group(2))
        else:
            req.keywords.append(token)
    req.per_album = max(1, min(req.per_album, max_images))
    req.albums = max(1, min(req.albums, max_images // req.per_album))
    return req


class RandomPicPlugin(Star):
    """随机抽图：/抽图 [二次元|三次元] [擦边|r18] [关键词...] [数量]"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = s = Settings.load(config)
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.default_style = STYLE_WORDS.get(s.command.default_style, REAL)
        self.default_rating = RATING_WORDS.get(
            s.command.default_rating.lower(), SENSITIVE
        )
        self.access = AccessControl(s.access)
        self.history = History(data_dir / "sent_albums.json")
        self.composer = Composer(s.send.header, s.send.caption)

        cookies = s.ehentai.cookies
        site = resolve_site(s.ehentai.site, cookies)
        # nw=1 跳过画廊的内容警告页；cookie 只发给当前站点的域名
        self.http = HttpClient(
            s.network.timeout, s.network.proxy, {SITES[site][2]: {"nw": "1", **cookies}}
        )
        cache = ImageCache(
            self.http, data_dir / "cache", s.network.cache_mb, s.network.max_image_mb
        )
        self.content = ContentFilter(s.draw.extra_blacklist, s.draw.heavy)
        opts = DrawOptions(
            rating_enabled=s.access.content_rating,
            from_start=s.draw.from_start,
            explicit_skip=s.draw.explicit_skip,
            concurrency=s.network.concurrency,
            color_only=s.draw.color_only,
        )
        self.ehentai = EHentaiSource(
            EHentai(self.http, site, s.ehentai.request_interval),
            build_pools(s.pools),
            cache,
            self.content,
            opts,
            exclude_ai=s.ehentai.exclude_ai,
            min_stars=s.ehentai.min_rating,
            min_pages=s.ehentai.min_pages,
        )
        weights = [
            (self.ehentai, s.sources.ehentai),
            (SixteenK(self.http, cache, self.content, opts), s.sources.sixteenk),
        ]
        self.pica_enabled = bool(s.pica.email and s.pica.password and s.sources.pica)
        if self.pica_enabled:
            pica = Picacomic(
                self.http,
                cache,
                self.content,
                opts,
                s.pica.email,
                s.pica.password,
                token_path=data_dir / "pica_token.json",
            )
            weights.append((pica, s.sources.pica))
        self.tagdb = (
            TagDB(self.http, data_dir / "ehtag.json.gz", s.tag_db.url)
            if s.tag_db.enabled
            else None
        )
        self.drawer = Drawer(self.ehentai, weights, self.content, self.tagdb)
        self.pdf = PdfStore(
            Path(s.pdf.output_dir) if s.pdf.output_dir else data_dir / "pdf",
            data_dir / "pdf_tmp",
            s.pdf.pages_per_file,
            s.pdf.keep_galleries,
        )
        self._preload: asyncio.Task | None = None

    async def initialize(self):
        # 后台预加载标签库，避免第一次抽卡时等待下载
        if self.tagdb:
            self._preload = asyncio.create_task(self.tagdb.get())

    async def terminate(self):
        if self._preload and not self._preload.done():
            self._preload.cancel()
        await self.http.close()

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

    @filter.command("pdf", alias={"全集"})
    async def gallery_pdf(self, event: AstrMessageEvent):
        """回复抽到的图片，获取整个画廊的 PDF。也可以 /pdf <画廊链接>"""
        async for result in self._pdf(event):
            yield result

    @filter.regex(NO_PREFIX_RE)
    async def no_prefix(self, event: AstrMessageEvent):
        """群聊里不带 / 也能触发本插件的指令。"""
        # 带了唤醒前缀、@ 了机器人或私聊时，已经由上面的指令处理
        if getattr(event, "is_at_or_wake_command", False):
            return
        if not self.settings.command.no_prefix:
            return
        word = event.message_str.split()[0].lower()
        handler = self._pdf(event) if word in PDF_COMMANDS else self._draw(event, word)
        async for result in handler:
            yield result

    def _entry(self, event: AstrMessageEvent) -> tuple[bool, list[str], bool]:
        """通用入口检查，返回 (是否响应, 参数, 是否求助)。"""
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        if not self.access.allowed(user_id, group_id):
            return False, [], False
        tokens = event.message_str.split()[1:]
        return True, tokens, bool(tokens) and tokens[0].lower() in HELP_WORDS

    async def _draw(self, event: AstrMessageEvent, word: str):
        command = self.settings.command
        if word in ALIASES and not command.aliases:
            return
        ok, tokens, wants_help = self._entry(event)
        if not ok:
            return
        if wants_help:
            yield event.plain_result(self.help_text())
            return
        preset = DRAW_COMMANDS[word]
        req = parse_args(
            tokens,
            command.max_images,
            preset.get("style", self.default_style),
            preset.get("rating", self.default_rating),
            command.album_count,
            command.images_per_album,
        )
        req.random_character = preset.get("random_character", False)
        is_private = event.is_private_chat()
        user_id = str(event.get_sender_id())
        denied = self.access.gate(req.rating, is_private) or self.access.take(
            user_id, req
        )
        if denied:
            yield event.plain_result(denied)
            return

        result = await self.drawer.draw(
            req, is_private, self.access.unrated_allowed(is_private)
        )
        if not result.albums:
            detail = "；".join(result.errors[:6]) or "未知原因"
            logger.warning(f"[random_pic] 获取失败 {req}: {detail}")
            yield event.plain_result(
                f"获取{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}图片失败：{detail}"
            )
            return

        self.access.used(user_id, result.images)
        sent = [
            SentAlbum(idx, clean_title(album.title), album.source, album.gallery)
            for idx, album in enumerate(result.albums, 1)
        ]
        self.history.record_draw(event.unified_msg_origin, sent)
        mode = resolve_mode(
            self.settings.send.mode, event.get_platform_name(), len(result.albums)
        )
        messages = self.composer.compose(result.albums, mode, str(event.get_self_id()))
        for i, (chain, idxs) in enumerate(messages):
            if i:
                await asyncio.sleep(SEND_INTERVAL)
            delivered, message_id = await send_direct(event, chain)
            if message_id:
                self.history.record_message(message_id, [sent[idx - 1] for idx in idxs])
            if not delivered:
                yield event.chain_result(chain)
        if len(result.albums) < req.albums:
            yield event.plain_result(
                f"仅获取到 {len(result.albums)}/{req.albums} 个图集。"
            )

    async def _pdf(self, event: AstrMessageEvent):
        ok, tokens, wants_help = self._entry(event)
        if not ok:
            return
        if wants_help:
            yield event.plain_result(self.help_text())
            return
        if not self.settings.pdf.enabled:
            yield event.plain_result("整本 PDF 功能未开启。")
            return

        album, hint = self._pdf_target(event, tokens)
        if album is None:
            yield event.plain_result(hint)
            return
        if album.gallery is None:
            yield event.plain_result(
                f"第 {album.idx} 个图集没有识别到画廊，请改用 /pdf <画廊链接>。"
                if album.source in EH_NAMES
                else f"第 {album.idx} 个图集来自 {album.source}，只有 E-Hentai 画廊支持整本 PDF。"
            )
            return
        try:
            gallery = await self.ehentai.gallery(album.gallery)
        except (
            EHentaiError,
            HttpError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as e:
            logger.warning(f"[random_pic] 查询画廊 {album.gallery.gid} 失败: {e!r}")
            yield event.plain_result(f"查询画廊失败：{e}")
            return
        if gallery is None or gallery.expunged:
            yield event.plain_result("画廊不存在或已被删除。")
            return
        denied = self._pdf_gate(gallery, event.is_private_chat())
        if denied:
            yield event.plain_result(denied)
            return

        files = self.pdf.cached(gallery)
        if files is None:
            if self.pdf.lock.locked():
                yield event.plain_result("正在打包另一个画廊，请稍后再试。")
                return
            async with self.pdf.lock:
                parts = self.pdf.parts(gallery.filecount)
                split = f"，分 {parts} 个文件发送" if parts > 1 else ""
                yield event.plain_result(
                    f"开始下载《{gallery.title}》共 {gallery.filecount} 页{split}，完成后发送 PDF……"
                )
                try:
                    files, missing = await self.pdf.build(
                        gallery, self.ehentai.download_gallery
                    )
                except (PdfError, EHentaiError) as e:
                    yield event.plain_result(f"打包失败：{e}")
                    return
                except Exception as e:
                    logger.exception(f"[random_pic] 画廊 {gallery.gid} 打包失败")
                    yield event.plain_result(f"打包失败：{e!r}")
                    return
                if missing:
                    yield event.plain_result(
                        f"有 {missing} 页下载失败，PDF 中缺少这些页。"
                    )
        for path, name in files:
            yield event.chain_result([Comp.File(name=name, file=str(path.resolve()))])

    def _pdf_target(
        self, event: AstrMessageEvent, tokens: list[str]
    ) -> tuple[SentAlbum | None, str]:
        """依次看参数里的画廊链接、被回复的消息、本会话上一次抽卡。

        回复的消息里有多个图集时必须带序号，不带就列出来提示。
        """
        for token in tokens:
            ref = gallery_from_text(token)
            if ref:
                return SentAlbum(0, "", self.ehentai.name, ref), ""
        index = next((int(t) for t in tokens if t.isdigit()), None)
        reply = next(
            (c for c in event.message_obj.message if isinstance(c, Comp.Reply)), None
        )
        if reply is not None:
            albums = self.history.by_message(str(reply.id)) if reply.id else None
            if albums is None:
                albums = albums_from_text(getattr(reply, "message_str", "") or "")
            return pick(albums, index, strict=True)
        return pick(self.history.last(event.unified_msg_origin), index, strict=False)

    def _pdf_gate(self, gallery, is_private: bool) -> str | None:
        """整本打包和抽图走同样的过滤和分级闸门；无法判定分级的按 R18 处理。"""
        reason = self.content.tags_reason(gallery.tags)
        if reason:
            return f"画廊{reason}，不予打包。"
        _, rating = classify(gallery.category, gallery.tags)
        return self.access.gate(rating or EXPLICIT, is_private)

    def help_text(self) -> str:
        s = self.settings
        command = s.command
        on = {True: "已开启", False: "未开启"}
        lines = [
            "【抽图用法】",
            "/抽图 [二次元|三次元] [擦边|r18] [关键词...] [图集数 或 图集数x每集张数]",
            f"· 参数顺序随意；不写时默认 {STYLE_NAMES[self.default_style]}·"
            f"{RATING_NAMES[self.default_rating]}、{command.album_count} 个图集、每集 "
            f"{command.images_per_album} 张，每次最多 {command.max_images} 张",
            "· 一个图集是同一画廊 / 帖子 / 本子里的几张图；例如 /抽图 3x4 抽 3 个图集、每集 4 张",
            "· 关键词可写中文角色、作品名（如 芙莉莲、原神）或 E-Hentai 标签，前缀 - 表示排除",
            "/随机角色 [同上]：每个图集先随机抽一个角色",
        ]
        if s.pdf.enabled:
            lines.append(
                f"/pdf 或 /全集：回复抽到的图片，获取整个画廊的 PDF，每 {s.pdf.pages_per_file} 页一个文件"
                "（回复的消息里有多个图集时加序号，即【】里的数字，如 /pdf 2；也可以 /pdf <画廊链接>）"
            )
        if command.aliases:
            lines.append("别名：/二次元 /三次元 /擦边 /色图")
        if s.access.content_rating:
            lines.append(
                f"R18：仅限私聊（{on[s.access.r18_enabled]}）；"
                f"群聊擦边：{on[s.access.group_sensitive]}"
            )
        else:
            lines.append(
                f"内容分级已关闭，群聊和私聊内容相同；R18：{on[s.access.r18_enabled]}"
            )
        if s.sources.sixteenk:
            where = "私聊且开启 R18 时，" if s.access.content_rating else ""
            lines.append(
                f"三次元不带关键词时，{where}部分图集来自 16K（没有分级，擦边和 R18 都可能抽到）"
            )
        if self.pica_enabled:
            lines.append("三次元部分图集来自哔咔的 Cosplay 分类（可搜普通关键词）")
        lines += ["示例：/抽图 原神 2　/抽图 二次元 芙莉莲", "/抽图 帮助：显示本说明"]
        return "\n".join(lines)
