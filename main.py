"""随机抽图插件入口：注册指令、解析参数、检查权限，抽卡与 /pdf 交给各模块。"""

import asyncio
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .access import AccessControl
from .drawer import Drawer
from .filters import ContentFilter
from .history import (
    History,
    SentAlbum,
    albums_from_text,
    clean_title,
    pick,
    work_from_text,
)
from .models import (
    ANIME,
    EXPLICIT,
    RATING_NAMES,
    REAL,
    SENSITIVE,
    STYLE_NAMES,
    Album,
    DrawOptions,
    DrawRequest,
    Work,
)
from .net import HttpClient, HttpError, ImageCache
from .pdf import PdfError, PdfStore
from .push import PushScheduler, Target, parse_targets
from .sender import (
    FORWARD_MAX_NODES,
    ONEBOT,
    Composer,
    SendFailed,
    resolve_mode,
    send_direct,
    send_onebot,
)
from .settings import Settings
from .sources.danbooru import DanbooruSource
from .sources.ehentai import EHentaiSource, build_pools
from .sources.ehentai_api import SITES, EHentai, EHentaiError, resolve_site
from .sources.pica import Picacomic
from .sources.wordpress import SITES as WP_SITES
from .sources.wordpress import WordPressSource
from .tags import TagDB

try:
    from .sources.jm import JMComicSource
except ImportError as e:  # 依赖 jmcomic 没装上时其他图源照常可用
    JMComicSource = None
    JM_IMPORT_ERROR = e

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
# 定时推送的推送内容、完整图集发送方式（与 _conf_schema.json 的选项一致）
PUSH_FULL_ALBUM = "随机完整图集"
PUSH_PDF = "PDF"
# 随机完整图集：抽到的作品不能推送（被过滤、分级不符）时最多抽这么多次
PUSH_ATTEMPTS = 3
# 图源键 → 显示名，图源没有启用时提示用
SOURCE_NAMES = {
    "ehentai": "E-Hentai",
    "pica": "哔咔",
    "cosplaytele": "CosplayTele",
    "xiuren": "XiuRen",
    "jmcomic": "禁漫天堂",
    "danbooru": "Danbooru",
}
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
    二次元来自单图站，每集固定 1 张。
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
    if req.style == ANIME:
        req.per_album = 1
    req.per_album = max(1, min(req.per_album, max_images))
    req.albums = max(1, min(req.albums, max_images // req.per_album))
    return req


def push_title(when: str) -> str:
    """推送说明的开头，写明是本插件在 when（时:分）的定时推送。"""
    return f"【定时推送】这是抽图插件 {when} 的定时推送"


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
            self.http,
            data_dir / "cache",
            s.network.cache_mb,
            s.network.max_image_mb,
        )
        self.content = ContentFilter(s.draw.extra_blacklist, s.draw.heavy)
        opts = DrawOptions(
            rating_enabled=s.access.content_rating,
            from_start=s.draw.from_start,
            explicit_skip=s.draw.explicit_skip,
            concurrency=s.network.concurrency,
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
        source_weights = s.sources.weights
        weights = [(self.ehentai, source_weights["ehentai"])]
        # 图源键 → 图源，/pdf 按作品所属的图源查询和下载
        self.work_sources: dict = {self.ehentai.key: self.ehentai}
        self.pica_enabled = bool(
            s.pica.email and s.pica.password and source_weights["pica"]
        )
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
            weights.append((pica, source_weights["pica"]))
            self.work_sources[pica.key] = pica
        wordpress = [
            WordPressSource(site, self.http, cache, self.content, opts)
            for site in WP_SITES
        ]
        weights += [(source, source_weights[source.key]) for source in wordpress]
        self.jm = None
        if JMComicSource is None:
            logger.warning(
                f"[random_pic] 禁漫天堂图源不可用，缺少依赖 jmcomic：{JM_IMPORT_ERROR!r}"
            )
        else:
            self.jm = JMComicSource(
                cache,
                self.content,
                opts,
                domain=s.jmcomic.domain,
                proxy=s.network.proxy,
                timeout=s.network.timeout,
                min_likes=s.jmcomic.min_likes,
                exclude_tags=s.jmcomic.exclude_tags,
            )
            weights.append((self.jm, source_weights["jmcomic"]))
            self.work_sources[self.jm.key] = self.jm
        self.tagdb = (
            TagDB(self.http, data_dir / "ehtag.json.gz", s.tag_db.url)
            if s.tag_db.enabled
            else None
        )
        danbooru = DanbooruSource(
            self.http,
            cache,
            self.content,
            opts,
            min_score=s.danbooru.min_score,
            exclude_tags=s.danbooru.exclude_tags,
        )
        self.drawer = Drawer(self.ehentai, danbooru, weights, self.content, self.tagdb)
        for source in [*wordpress, danbooru]:
            self.work_sources[source.key] = source
        self.pdf = PdfStore(
            Path(s.pdf.output_dir) if s.pdf.output_dir else data_dir / "pdf",
            data_dir / "pdf_tmp",
            s.pdf.pages_per_file,
            s.pdf.keep_galleries,
            s.pdf.quality,
        )
        self.push_tmp = data_dir / "push_tmp"
        self._preload: asyncio.Task | None = None
        self.push_targets = self._push_targets()
        self.push = (
            PushScheduler(s.push.interval_minutes, self._push_all)
            if s.push.enabled and self.push_targets
            else None
        )

    def _push_targets(self) -> list[Target]:
        push = self.settings.push
        targets, invalid = parse_targets(push.groups, push.users)
        for item in invalid:
            logger.warning(
                f"[random_pic] 定时推送：不是有效的群号或 QQ 号，已忽略：{item}"
            )
        return targets

    async def initialize(self):
        # 后台预加载标签库，避免第一次抽卡时等待下载
        if self.tagdb:
            self._preload = asyncio.create_task(self.tagdb.get())
        if self.push:
            self.push.start()

    async def terminate(self):
        if self._preload and not self._preload.done():
            self._preload.cancel()
        if self.push:
            self.push.stop()
        await self.http.close()
        if self.jm:
            await self.jm.close()

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
            detail = "；".join(result.errors[:6]) or "未知原因"
            logger.warning(f"[random_pic] 获取失败 {req}: {detail}")
            yield event.plain_result(
                f"获取{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}图片失败：{detail}"
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

        failed, total = await self._deliver(
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
        req = parse_args(
            tokens,
            command.max_images,
            preset.get("style", self.default_style),
            preset.get("rating", self.default_rating),
            command.album_count,
            command.images_per_album,
        )
        req.random_character = preset.get("random_character", False)
        return req

    async def _deliver(
        self,
        albums: list,
        session: str,
        platform: str,
        self_id: str,
        send: Callable[[list], Awaitable[str | None]],
    ) -> tuple[int, int]:
        """登记并逐条发送抽到的图集。send 发送一条消息、返回消息 ID，失败时抛出 SendFailed。

        返回 (失败条数, 总条数)。
        """
        sent = [
            SentAlbum(idx, clean_title(album.title), album.source, album.work)
            for idx, album in enumerate(albums, 1)
        ]
        self.history.record_draw(session, sent)
        mode = resolve_mode(self.settings.send.mode, platform, len(albums))
        messages = self.composer.compose(albums, mode, self_id)
        return await self._send_all(messages, sent, send), len(messages)

    async def _send_all(
        self,
        messages: list,
        sent: list[SentAlbum],
        send: Callable[[list], Awaitable[str | None]],
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

    async def _push_all(self, fire: datetime | None = None):
        """定时推送：每个群、每个用户单独抽一次，依次推送。fire 是这次的触发时刻。"""
        when = (fire or datetime.now()).strftime("%H:%M")
        platform = self._onebot_platform()
        if platform is None:
            logger.warning("[random_pic] 定时推送：没有找到 QQ（aiocqhttp）平台适配器")
            return
        for target in self.push_targets:
            try:
                await self._push(platform, target, when)
            except Exception:
                logger.exception(f"[random_pic] 定时推送到{target}出错")

    def _onebot_platform(self):
        """QQ（aiocqhttp）平台适配器，有多个时用第一个。"""
        return next(
            (
                p
                for p in self.context.platform_manager.platform_insts
                if p.meta().name == ONEBOT
            ),
            None,
        )

    async def _push(self, platform, target: Target, when: str):
        umo = target.umo(platform.meta().id)
        is_private = not target.is_group
        full = self.settings.push.content == PUSH_FULL_ALBUM
        req = self._request("抽图", [])
        if full:
            req.albums = req.per_album = 1
        denied = self.access.gate(req.rating, is_private)
        if denied:
            logger.warning(f"[random_pic] 定时推送到{target}被拒绝：{denied}")
            return
        bot = getattr(platform, "bot", None)
        group_id = target.chat_id if target.is_group else ""

        async def send(chain: list) -> str | None:
            if bot is not None:
                delivered, message_id = await send_onebot(
                    bot, chain, group_id, target.chat_id
                )
                if delivered:
                    return message_id
            try:
                await self.context.send_message(umo, MessageChain(chain))
            except Exception as e:
                logger.warning(f"[random_pic] 定时推送到{target}发送失败: {e!r}")
                raise SendFailed(str(e)) from e
            return None

        if full:
            failed, total = await self._push_work(target, umo, req, bot, send, when)
        else:
            result = await self.drawer.draw(req, is_private)
            if not result.albums:
                detail = "；".join(result.errors[:6]) or "未知原因"
                logger.warning(
                    f"[random_pic] 定时推送到{target}抽图失败 {req}: {detail}"
                )
                return
            await self._announce(
                send,
                f"{push_title(when)}：{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}，"
                f"{len(result.albums)} 个图集共 {result.images} 张。",
            )
            failed, total = await self._deliver(
                result.albums, umo, ONEBOT, await self._self_id(bot), send
            )
        if failed:
            logger.warning(
                f"[random_pic] 定时推送到{target}：{failed}/{total} 条消息发送失败"
            )

    async def _push_work(
        self,
        target: Target,
        umo: str,
        req: DrawRequest,
        bot,
        send: Callable[[list], Awaitable[str | None]],
        when: str,
    ) -> tuple[int, int]:
        """随机完整图集：抽一个图集，先发作品信息，再推送作品的全部图片（合并转发或 PDF）。

        返回 (失败条数, 总条数)。
        """
        picked = await self._pick_work(target, req)
        if picked is None:
            return 0, 0
        album, source, work = picked
        sent = [SentAlbum(1, clean_title(album.title), album.source, work.ref)]
        self.history.record_draw(umo, sent)
        await self._announce(send, self._work_notice(when, album, work))
        if self.settings.push.album_format == PUSH_PDF:
            files, missing = await self.pdf.get(
                work, f"push:{umo}", source.download_work
            )
            if missing:
                logger.warning(
                    f"[random_pic] 定时推送《{work.title}》有 {missing} 页下载失败"
                )
            messages = [
                ([Comp.File(name=name, file=str(path.resolve()))], [])
                for path, name in files
            ]
            return await self._send_all(messages, sent, send), len(messages)

        tmp = self.push_tmp / work.ref.key
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            paths, missing = await source.download_work(work, tmp)
            if not paths:
                logger.warning(
                    f"[random_pic] 定时推送《{work.title}》没有下载到任何图片"
                )
                return 0, 0
            if missing:
                logger.warning(
                    f"[random_pic] 定时推送《{work.title}》有 {missing} 页下载失败"
                )
            # 下载的图片以页码命名
            whole = replace(
                album,
                total=work.pages,
                pictures=[(int(path.stem), path) for path in paths],
            )
            messages = self.composer.forward_album(whole, await self._self_id(bot))
            return await self._send_all(messages, sent, send), len(messages)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _announce(
        self, send: Callable[[list], Awaitable[str | None]], text: str
    ) -> None:
        """推送前单独发一条说明；发不出去时照常推送（发送失败已记日志）。"""
        try:
            await send([Comp.Plain(text)])
        except SendFailed:
            return
        await asyncio.sleep(SEND_INTERVAL)

    def _work_notice(self, when: str, album: Album, work: Work) -> str:
        """随机完整图集的推送说明：作品的全部信息和发送方式。"""
        if self.settings.push.album_format == PUSH_PDF:
            parts = self.pdf.parts(work.pages)
            how = "PDF" + (f"，分 {parts} 个文件" if parts > 1 else "")
        else:
            parts = -(-work.pages // FORWARD_MAX_NODES)
            how = "合并转发" + (f"，分 {parts} 条" if parts > 1 else "")
        lines = [
            f"{push_title(when)}：随机完整图集",
            f"标题：{work.title or album.title or '无标题'}",
            f"来源：{album.source} · {RATING_NAMES[work.rating or EXPLICIT]}",
            f"页数：{work.pages} 页",
            f"发送：{how}",
            *album.details,
        ]
        return "\n".join(lines)

    async def _pick_work(self, target: Target, req: DrawRequest):
        """抽一个图集并查询它所在的作品，返回 (图集, 图源, 作品)。

        作品查不到或不能推送（被过滤、分级不符）时重抽，最多 PUSH_ATTEMPTS 次；都不行时返回 None。
        """
        is_private = not target.is_group
        for _ in range(PUSH_ATTEMPTS):
            result = await self.drawer.draw(req, is_private)
            if not result.albums:
                detail = "；".join(result.errors[:6]) or "未知原因"
                logger.warning(
                    f"[random_pic] 定时推送到{target}抽图失败 {req}: {detail}"
                )
                return None
            album = result.albums[0]
            ref = album.work
            source = self.work_sources.get(ref.source) if ref else None
            if source is None:
                logger.warning(
                    f"[random_pic] 定时推送：《{album.title}》无法获取完整作品，重抽"
                )
                continue
            work = await source.work(ref)
            denied = (
                "作品不存在或已被删除"
                if work is None
                else self._pdf_gate(work, is_private)
            )
            if denied:
                logger.warning(f"[random_pic] 定时推送：《{album.title}》{denied}，重抽")
                continue
            return album, source, work
        logger.warning(
            f"[random_pic] 定时推送到{target}：连续 {PUSH_ATTEMPTS} 次没有抽到可推送的完整图集"
        )
        return None

    async def _self_id(self, bot) -> str:
        """机器人自己的 QQ 号，用作合并转发节点的发送者；拿不到时用 0。"""
        if bot is None:
            return "0"
        try:
            info = await bot.call_action("get_login_info")
            return str(info["user_id"])
        except Exception as e:
            logger.warning(f"[random_pic] 获取机器人 QQ 号失败: {e!r}")
            return "0"

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
        ref = album.work
        if ref is None:
            yield event.plain_result(
                f"第 {album.idx} 个图集没有识别到作品，请回复抽图发出的那条消息，"
                "或使用 /pdf <作品链接>。"
            )
            return
        source = self.work_sources.get(ref.source)
        if source is None:
            name = SOURCE_NAMES.get(ref.source, ref.source)
            yield event.plain_result(f"{name} 没有启用，无法打包。")
            return
        try:
            work = await source.work(ref)
        except (HttpError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"[random_pic] 查询作品 {ref} 失败: {e!r}")
            yield event.plain_result(f"查询作品失败：{e!r}")
            return
        except Exception as e:  # 各图源自己的错误（登录失败、返回格式不对等）
            logger.warning(f"[random_pic] 查询作品 {ref} 失败: {e!r}")
            yield event.plain_result(f"查询作品失败：{e}")
            return
        if work is None:
            yield event.plain_result("作品不存在或已被删除。")
            return
        denied = self._pdf_gate(work, event.is_private_chat())
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
            except (PdfError, EHentaiError) as e:
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
            ref = work_from_text(token)
            if ref:
                return SentAlbum(0, "", SOURCE_NAMES[ref.source], ref), ""
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

    def _pdf_gate(self, work: Work, is_private: bool) -> str | None:
        """整本打包和抽图走同样的过滤和分级闸门；无法判定分级的按 R18 处理。"""
        if work.blocked:
            return f"作品{work.blocked}，不予打包。"
        return self.access.gate(work.rating or EXPLICIT, is_private)

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
            "· 二次元来自 Danbooru，一张图就是一个图集；三次元来自 E-Hentai 等写真图源",
            "· 关键词可写中文角色、作品名（如 芙莉莲、原神）或标签，前缀 - 表示排除；二次元最多 2 个关键词",
            "/随机角色 [同上]：每个图集先随机抽一个角色",
        ]
        if s.pdf.enabled:
            lines.append(
                f"/pdf 或 /全集：回复抽到的图片，获取整个作品（画廊、帖子、本子）的 PDF，每 {s.pdf.pages_per_file} 页一个文件"
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
        if self.pica_enabled:
            lines.append("三次元部分图集来自哔咔的 Cosplay 分类（可搜普通关键词）")
        weights = s.sources.weights
        if weights["cosplaytele"]:
            lines.append(
                "三次元部分图集来自 CosplayTele 的 Cosplay 写真（可搜角色、作品名）"
            )
        if weights["xiuren"]:
            lines.append("三次元擦边部分图集来自 XiuRen 的工作室写真")
        if self.jm and weights["jmcomic"]:
            lines.append(
                "三次元 R18 部分图集来自禁漫天堂的 Cosplay 分类（可搜普通关键词）"
            )
        lines += ["示例：/抽图 原神 2　/抽图 二次元 芙莉莲", "/抽图 帮助：显示本说明"]
        return "\n".join(lines)
