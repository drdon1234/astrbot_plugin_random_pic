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
from .filters import ContentFilter
from .history import History, SentAlbum, albums_from_text, pick
from .models import (
    ANIME,
    EXPLICIT,
    RATING_NAMES,
    RATING_WORDS,
    REAL,
    SENSITIVE,
    STYLE_NAMES,
    STYLE_WORDS,
    DrawOptions,
    DrawRequest,
)
from .net import NETWORK_ERRORS, HttpClient, ImageCache
from .pdf import PdfError, PdfStore
from .push import Pusher, parse_targets
from .sender import Composer, Dispatcher, send_direct
from .settings import Settings
from .sources import SourceSet
from .sources.base import SourceError
from .sources.ehentai_api import resolve_site, site_cookies
from .tags import TagDB

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
PDF_COMMANDS = ("pdf", "全集")
# 群聊不带唤醒前缀时也能触发的指令词（整条消息就是指令词，或后面跟空格和参数）
NO_PREFIX_RE = rf"(?i)^\s*({'|'.join([*DRAW_COMMANDS, *PDF_COMMANDS])})(?:\s|$)"


def parse_args(
    tokens: list[str], max_images: int, defaults: DrawRequest
) -> DrawRequest:
    """宽松解析：风格词、分级词、数量可任意顺序，其余当作关键词。

    数量写一个数字是图集数，写成「图集数x每集张数」（如 3x4）同时指定每集张数。
    二次元来自单图站，每集固定 1 张。
    总张数不超过 max_images：超出时先减少每集张数，再减少图集数。
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
        elif token.isdigit():
            req.albums = int(token)
        elif match := COUNT_RE.fullmatch(low):
            req.albums, req.per_album = int(match.group(1)), int(match.group(2))
        else:
            req.keywords.append(token)
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
        self.content = ContentFilter(s.draw.extra_blacklist, s.draw.heavy)

        cookies = s.ehentai.cookies
        site = resolve_site(s.ehentai.site, cookies)
        self.http = HttpClient(
            s.network.timeout, s.network.proxy, site_cookies(site, cookies)
        )
        cache = ImageCache(
            self.http, data_dir / "cache", s.network.cache_mb, s.network.max_image_mb
        )
        opts = DrawOptions(
            rating_enabled=s.access.content_rating,
            from_start=s.draw.from_start,
            explicit_skip=s.draw.explicit_skip,
            concurrency=s.network.concurrency,
        )
        self.sources = SourceSet(
            s, self.http, cache, self.content, opts, data_dir, site
        )
        self.tagdb = TagDB(self.http, data_dir / "ehtag.json.gz", s.network.tag_db_url)
        self.drawer = Drawer(self.sources, self.content, self.tagdb)
        self.history = History(data_dir / "sent_albums.json")
        self.dispatcher = Dispatcher(
            self.history, Composer(s.send.header, s.send.caption), s.send.mode
        )
        self.pdf = PdfStore(
            Path(s.pdf.output_dir) if s.pdf.output_dir else data_dir / "pdf",
            data_dir / "pdf_tmp",
            s.pdf.pages_per_file,
            s.pdf.keep_galleries,
            s.pdf.jpeg_quality,
            self.sources.keys,
        )
        targets = parse_targets(s.push.groups, s.push.users) if s.push.enabled else []
        self.pusher = (
            Pusher(
                s.push,
                targets,
                context,
                self.drawer,
                self.access,
                self.dispatcher,
                self.sources,
                self.pdf,
                lambda: self._request("抽图", []),
                data_dir / "push_tmp",
            )
            if targets
            else None
        )
        self._preload: asyncio.Task | None = None

    async def initialize(self):
        # 后台预加载标签库，避免第一次抽卡时等待下载
        self._preload = asyncio.create_task(self.tagdb.get())
        if self.pusher:
            self.pusher.start()

    async def terminate(self):
        if self._preload and not self._preload.done():
            self._preload.cancel()
        if self.pusher:
            self.pusher.stop()
        await self.http.close()
        await self.sources.close()

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
        """回复抽到的图片，获取整个作品的 PDF。也可以 /pdf <作品链接>"""
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

    def _args(self, event: AstrMessageEvent) -> list[str] | None:
        """指令后面的参数；用户或群没有权限时返回 None（不回复）。"""
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        if not self.access.allowed(user_id, group_id):
            return None
        return event.message_str.split()[1:]

    async def _draw(self, event: AstrMessageEvent, word: str):
        if word in ALIASES and not self.settings.command.aliases:
            return
        tokens = self._args(event)
        if tokens is None:
            return
        if tokens and tokens[0].lower() in HELP_WORDS:
            yield event.plain_result(self.help_text())
            return
        req = self._request(word, tokens)
        is_private = event.is_private_chat()
        user_id = str(event.get_sender_id())
        denied = self.access.gate(req.rating, is_private) or self.access.take(
            user_id, req
        )
        if denied:
            yield event.plain_result(denied)
            return

        result = await self.drawer.draw(req, is_private)
        if not result.albums:
            logger.warning(f"[random_pic] 获取失败 {req}: {result.reason()}")
            yield event.plain_result(
                f"获取{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}图片失败："
                f"{result.reason()}"
            )
            return

        self.access.used(user_id, result.images)
        # 插件发不出去的（其他平台、组装出错）交给 AstrBot 发
        fallback = []

        async def send(chain: list) -> str | None:
            delivered, message_id = await send_direct(event, chain)
            if not delivered:
                fallback.append(chain)
            return message_id

        failed, total = await self.dispatcher.deliver(
            result.albums,
            event.unified_msg_origin,
            event.get_platform_name(),
            str(event.get_self_id()),
            send,
        )
        for chain in fallback:
            yield event.chain_result(chain)
        if failed:
            yield event.plain_result(self._send_failed_text(failed, total, is_private))
        if len(result.albums) < req.albums:
            yield event.plain_result(
                f"仅获取到 {len(result.albums)}/{req.albums} 个图集。"
            )

    def _request(self, word: str, tokens: list[str]) -> DrawRequest:
        """按抽图指令词和参数生成请求。"""
        command = self.settings.command
        preset = DRAW_COMMANDS[word]
        defaults = DrawRequest(
            preset.get("style", command.style),
            preset.get("rating", command.rating),
            albums=command.album_count,
            per_album=command.images_per_album,
            random_character=preset.get("random_character", False),
        )
        return parse_args(tokens, command.max_images, defaults)

    def _send_failed_text(self, failed: int, total: int, is_private: bool) -> str:
        what = "这条消息" if total == 1 else f"其中 {failed}/{total} 条消息"
        text = f"发送失败：QQ 拒发了{what}。"
        if not is_private and not self.settings.access.content_rating:
            return (
                text
                + "目前没有开启内容分级，裸露较多的结果会被 QQ 拒发，建议私聊重新抽图。"
            )
        return text + "可能是图片内容被 QQ 拦截，可换个关键词或稍后重试。"

    async def _pdf(self, event: AstrMessageEvent):
        tokens = self._args(event)
        if tokens is None:
            return
        if tokens and tokens[0].lower() in HELP_WORDS:
            yield event.plain_result(self.help_text())
            return
        if not self.settings.pdf.enabled:
            yield event.plain_result("整本 PDF 功能未开启。")
            return

        album, hint = self._pdf_target(event, tokens)
        if album is None:
            yield event.plain_result(hint)
            return
        ref = album.work
        if ref is None:
            yield event.plain_result(
                f"第 {album.idx} 个图集没有识别到作品，请回复抽图发出的那条消息，"
                "或使用 /pdf <作品链接>。"
            )
            return
        source, why = self.sources.find(ref.source)
        if source is None:
            yield event.plain_result(f"{why}，无法打包。")
            return
        try:
            work = await source.work(ref)
        except Exception as e:  # 登录失败、网络错误、站点改版等都要回复用户
            expected = isinstance(e, (SourceError, *NETWORK_ERRORS))
            logger.warning(
                f"[random_pic] 查询作品 {ref} 失败: {e!r}", exc_info=not expected
            )
            yield event.plain_result(f"查询作品失败：{str(e) or repr(e)}")
            return
        if work is None:
            yield event.plain_result("作品不存在或已被删除。")
            return
        denied = self.access.work_gate(work, event.is_private_chat())
        if denied:
            yield event.plain_result(denied)
            return

        files = self.pdf.cached(work)
        if files is None:
            # 排队等待不提示；同一作品已在打包时直接等它的结果
            if not self.pdf.building(work):
                parts = self.pdf.parts(work.pages)
                split = f"，分 {parts} 个文件发送" if parts > 1 else ""
                yield event.plain_result(
                    f"《{work.title or '无标题'}》共 {work.pages} 页{split}，打包完成后发送 PDF……"
                )
            try:
                files, missing = await self.pdf.get(
                    work, str(event.get_sender_id()), source.download_work
                )
            except (PdfError, SourceError) as e:
                yield event.plain_result(f"打包失败：{e}")
                return
            except Exception as e:
                logger.exception(f"[random_pic] 作品 {ref} 打包失败")
                yield event.plain_result(f"打包失败：{e!r}")
                return
            if missing:
                yield event.plain_result(f"有 {missing} 页下载失败，PDF 中缺少这些页。")
        for path, name in files:
            yield event.chain_result([Comp.File(name=name, file=str(path.resolve()))])

    def _pdf_target(
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
        command = s.command
        on = {True: "已开启", False: "未开启"}
        lines = [
            "【抽图用法】",
            "/抽图 [二次元|三次元] [擦边|r18] [关键词...] [图集数 或 图集数x每集张数]",
            f"· 参数顺序随意；不写时默认 {STYLE_NAMES[command.style]}·"
            f"{RATING_NAMES[command.rating]}、{command.album_count} 个图集、每集 "
            f"{command.images_per_album} 张，每次最多 {command.max_images} 张",
            "· 一个图集是同一画廊 / 帖子 / 本子里的几张图；例如 /抽图 3x4 抽 3 个图集、每集 4 张",
            "· 关键词可写中文角色、作品名（如 芙莉莲、原神）或标签，前缀 - 表示排除",
            "/随机角色 [同上]：每个图集先随机抽一个角色",
        ]
        if s.pdf.enabled:
            lines.append(
                f"/pdf 或 /全集：回复抽到的图片，获取整个作品的 PDF，每 {s.pdf.pages_per_file} 页一个文件"
                "（回复的消息里有多个图集时加序号，即【】里的数字，如 /pdf 2；也可以 /pdf <作品链接>）"
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
        lines += self.sources.help_lines()
        lines += ["示例：/抽图 原神 2　/抽图 二次元 芙莉莲", "/抽图 帮助：显示本说明"]
        return "\n".join(lines)
