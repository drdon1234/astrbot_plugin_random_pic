"""E-Hentai / 16K / 哔咔随机抽卡插件：指令与参数解析。"""

import asyncio
import re
import shutil
import time
from datetime import date
from pathlib import Path

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .drawer import Album, Drawer, build_pools
from .ehentai import EHentai, EHentaiError
from .filters import (
    HEAVY_TAGS,
    TagBlacklist,
    classify,
    heavy_hit,
    request_gate,
    unrated_allowed,
)
from .models import (
    ANIME,
    EXPLICIT,
    RATING_NAMES,
    REAL,
    SENSITIVE,
    STYLE_NAMES,
    ImageItem,
    PicRequest,
)
from .net import HttpClient, HttpError, ImageCache
from .pdf import write_pdf
from .picacomic import SOURCE as PICA, Picacomic
from .refs import (
    Unit,
    gallery_from_text,
    header_text,
    image_label,
    last_candidates,
    message_candidates,
    message_units,
    page_text,
    pick,
    resolve_units,
)
from .registry import GalleryRef, SentRegistry
from .sixteenk import SOURCE as SIXTEENK, SixteenK
from .tags import DEFAULT_DB_URL, TagDB

PLUGIN_NAME = "astrbot_plugin_random_pic"
# 站点 → (首页, API, cookie 域名)
SITES = {
    "e-hentai": (
        "https://e-hentai.org",
        "https://api.e-hentai.org/api.php",
        "e-hentai.org",
    ),
    "exhentai": (
        "https://exhentai.org",
        "https://exhentai.org/api.php",
        "exhentai.org",
    ),
}
COOKIE_KEYS = ("ipb_member_id", "ipb_pass_hash", "igneous")
EX_REQUIRED_COOKIES = ("ipb_member_id", "ipb_pass_hash", "igneous")

STYLE_WORDS = {"二次元": ANIME, "三次元": REAL}
RATING_WORDS = {"擦边": SENSITIVE, "r18": EXPLICIT, "色图": EXPLICIT}
DEFAULT_RATING_WORDS = {"擦边": SENSITIVE, "R18": EXPLICIT}
HELP_WORDS = {"help", "帮助"}
FORWARD = "合并转发"
MIXED = "图文混合"
SEPARATE = "逐条发送"
FORWARD_PLATFORMS = {"aiocqhttp"}
# 图文混合时每条消息最多放这么多张图，太多时 QQ 可能发送失败
MIXED_PER_MESSAGE = 10
# 数量写成「图集数x每集张数」
COUNT_RE = re.compile(r"(\d+)\s*[x*×]\s*(\d+)")
# 逐条发送时两条消息之间的间隔（秒），和 AstrBot 逐条发送一致
SEND_INTERVAL = 0.5
# 本插件存储的 PDF：画廊号[-incomplete][-第几卷of共几卷].pdf
OWN_PDF = re.compile(r"(\d+)(?:-incomplete)?(?:-\d+of\d+)?\.pdf")
# 群聊不带唤醒前缀时也能触发的指令词（整条消息就是指令词，或后面跟空格和参数）
NO_PREFIX_RE = r"(?i)^\s*(抽图|随机角色|二次元|三次元|擦边|色图|pdf|全集)(?:\s|$)"
ALIAS_WORDS = {"二次元", "三次元", "擦边", "色图"}
# 哔咔说明文字里最多列出的标签数
MAX_CAPTION_TAGS = 4
UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def resolve_site(eh_conf: dict) -> tuple[str, dict[str, str]]:
    """返回 (站点名, 登录 cookie)。ExHentai 缺少必需 cookie 时退回表站。"""
    cookies = {k: str(eh_conf.get(k) or "").strip() for k in COOKIE_KEYS}
    cookies = {k: v for k, v in cookies.items() if v}
    site = eh_conf.get("site", "e-hentai")
    if site not in SITES:
        site = "e-hentai"
    if site == "exhentai":
        missing = [k for k in EX_REQUIRED_COOKIES if k not in cookies]
        if missing:
            logger.warning(
                f"[random_pic] 使用 ExHentai 需要 cookie {', '.join(missing)}，已改用 E-Hentai"
            )
            site = "e-hentai"
    return site, cookies


def parse_args(
    tokens: list[str],
    max_count: int,
    style: str = ANIME,
    rating: str = SENSITIVE,
    count: int = 1,
    per_album: int = 1,
) -> PicRequest:
    """宽松解析：风格词、分级词、数量可任意顺序，其余当作搜索关键词。

    数量写一个数字是图集数，写成「图集数x每集张数」（如 3x4）同时指定每个图集几张。
    总张数不超过 max_count：超出时先减少每集张数，再减少图集数。
    """
    req = PicRequest(style=style, rating=rating, count=count, per_album=per_album)
    for token in tokens:
        low = token.lower()
        if token in STYLE_WORDS:
            req.style = STYLE_WORDS[token]
        elif low in RATING_WORDS:
            req.rating = RATING_WORDS[low]
        elif token.isdigit():
            req.count = int(token)
        elif match := COUNT_RE.fullmatch(low):
            req.count, req.per_album = int(match.group(1)), int(match.group(2))
        else:
            req.tags.append(token)
    max_count = max(1, max_count)
    req.per_album = max(1, min(req.per_album, max_count))
    req.count = max(1, min(req.count, max_count // req.per_album))
    return req


def item_label(item: ImageItem) -> str:
    return image_label(item.page, item.pages, item.title)


def album_ref(album: Album) -> GalleryRef:
    item = album[0][0]
    return GalleryRef(item.gid, item.token, item.title, item.pages)


def source_name(item: ImageItem) -> str:
    if item.source == PICA:
        return "哔咔"
    if item.source == SIXTEENK:
        return "16K"
    return "ExHentai" if "exhentai.org" in item.gallery_url else "E-Hentai"


def format_caption(item: ImageItem) -> str:
    """图片下方的说明文字。标题、第几张和来源已经在图片上方的标题行里。"""
    if item.source == PICA:
        # 哔咔没有公开的网页地址，不附链接
        lines = [f"作者：{item.author}"] if item.author else []
        if item.characters:
            lines.append(f"标签：{'、'.join(item.characters[:MAX_CAPTION_TAGS])}")
        return "\n".join(lines)
    if item.source == SIXTEENK:
        return f"帖子：{item.gallery_url}"
    lines = []
    if item.author:
        lines.append(f"作者：{item.author}")
    if item.parodies:
        lines.append(f"作品：{'、'.join(item.parodies)}")
    if item.characters:
        lines.append(f"角色：{'、'.join(item.characters)}")
    info = [item.category]
    if item.stars:
        info.append(f"★{item.stars:.1f}")
    if any(info):
        lines.append(" · ".join(i for i in info if i))
    lines.append(f"画廊：{item.gallery_url}")
    return "\n".join(lines)


def pdf_filename(
    title: str, gid: int, pages: tuple[int, int] | None = None, parts: int = 1
) -> str:
    """发送给用户的文件名；分卷时带上页码范围。"""
    name = UNSAFE_FILENAME.sub(" ", title).strip()[:80] or str(gid)
    if parts > 1 and pages:
        name += f" ({pages[0]}-{pages[1]})"
    return f"{name}.pdf"


def pdf_stored_name(gid: int, part: int, parts: int, incomplete: bool = False) -> str:
    """存储用的文件名，只含画廊号，便于识别缓存和清理。"""
    name = f"{gid}-incomplete" if incomplete else str(gid)
    if parts > 1:
        name += f"-{part}of{parts}"
    return f"{name}.pdf"


class RandomPicPlugin(Star):
    """E-Hentai 随机抽卡：/抽图 [二次元|三次元] [擦边|r18] [关键词...] [数量]"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_count = max(1, int(config.get("max_count", 5)))
        self.access = config.get("access", {})
        self._last_use: dict[str, float] = {}
        self._daily: dict[str, int] = {}
        self._daily_date = date.today()
        self.default_style = STYLE_WORDS.get(config.get("default_style"), REAL)
        self.default_rating = DEFAULT_RATING_WORDS.get(
            config.get("default_rating"), SENSITIVE
        )
        # 2.7 以前只有 default_count（张数，每个画廊一张）
        self.default_count = max(
            1, int(config.get("album_count", config.get("default_count", 1)))
        )
        self.default_per_album = max(1, int(config.get("images_per_album", 1)))
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.registry = SentRegistry(data_dir / "sent_images.json")
        pdf_conf = config.get("pdf", {})
        self.pdf_conf = pdf_conf
        self.pdf_dir = Path(
            (pdf_conf.get("output_dir") or "").strip() or data_dir / "pdf"
        )
        self.pdf_tmp = data_dir / "pdf_tmp"
        self._pdf_lock = asyncio.Lock()
        self.rating_enabled = bool(config.get("content_rating", True))
        heavy = config.get("heavy_tags")
        self.heavy = (
            frozenset(
                str(t).strip().lower()
                for t in (HEAVY_TAGS if heavy is None else heavy)
                if str(t).strip()
            )
            if config.get("block_heavy", True)
            else frozenset()
        )

        eh_conf = config.get("ehentai", {})
        self.site, cookies = resolve_site(eh_conf)
        site_url, api_url, cookie_domain = SITES[self.site]
        # nw=1 跳过画廊的内容警告页；cookie 只发给当前站点的域名
        self.http = HttpClient(
            float(config.get("request_timeout", 20)),
            {cookie_domain: {"nw": "1", **cookies}},
        )
        cache_conf = config.get("cache", {})
        cache = ImageCache(
            self.http,
            StarTools.get_data_dir(PLUGIN_NAME) / "cache",
            max_files=max(int(cache_conf.get("max_files", 100)), self.max_count),
            max_total_mb=float(cache_conf.get("max_total_mb", 200)),
            max_image_mb=float(cache_conf.get("max_image_mb", 10)),
        )
        proxy = (eh_conf.get("proxy") or "").strip() or None
        blacklist = TagBlacklist(config.get("extra_blacklist", []))
        sk_conf = config.get("sixteenk", {})
        self.sixteenk_ratio = min(max(int(sk_conf.get("ratio", 50)), 0), 100)
        sixteenk = SixteenK(
            self.http,
            cache,
            proxy if sk_conf.get("use_proxy", True) else None,
            blacklist,
        )
        pica_conf = config.get("pica", {})
        pica_email = str(pica_conf.get("email") or "").strip()
        pica_password = str(pica_conf.get("password") or "")
        pica = None
        self.pica_ratio = 0
        if pica_email and pica_password:
            self.pica_ratio = min(max(int(pica_conf.get("ratio", 30)), 0), 100)
            pica = Picacomic(
                self.http,
                cache,
                proxy if pica_conf.get("use_proxy", True) else None,
                blacklist,
                pica_email,
                pica_password,
                rating_enabled=self.rating_enabled,
                explicit_skip=float(eh_conf.get("explicit_skip_ratio", 0.3)),
                token_path=data_dir / "pica_token.json",
            )
        eh = EHentai(
            self.http,
            site_url,
            api_url,
            proxy,
            float(eh_conf.get("request_interval", 0.5)),
        )
        tag_conf = config.get("tag_db", {})
        self.tagdb = None
        if tag_conf.get("enabled", True):
            self.tagdb = TagDB(
                self.http,
                StarTools.get_data_dir(PLUGIN_NAME) / "ehtag.json.gz",
                (tag_conf.get("url") or "").strip() or DEFAULT_DB_URL,
                proxy if tag_conf.get("use_proxy", True) else None,
                float(tag_conf.get("refresh_days", 7)),
            )
        self._preload: asyncio.Task | None = None
        self.drawer = Drawer(
            eh,
            build_pools(config.get("pools", {})),
            blacklist,
            cache,
            int(config.get("max_retries", 3)),
            exclude_ai=bool(eh_conf.get("exclude_ai", True)),
            min_stars=int(eh_conf.get("min_rating", 4)),
            min_pages=int(eh_conf.get("min_pages", 0)),
            cover_only=eh_conf.get("page_pick", "随机页") == "封面",
            explicit_skip=float(eh_conf.get("explicit_skip_ratio", 0.3)),
            color_only=bool(config.get("anime_color_only", True)),
            heavy=self.heavy,
            tags=self.tagdb,
            rating_enabled=self.rating_enabled,
            sixteenk=sixteenk,
            sixteenk_ratio=self.sixteenk_ratio,
            pica=pica,
            pica_ratio=self.pica_ratio,
            concurrency=int(config.get("concurrency", 4)),
        )

    async def initialize(self):
        # 后台预加载标签库，避免第一次抽卡时等待下载
        if self.tagdb:
            self._preload = asyncio.create_task(self.tagdb.get())

    @filter.command("抽图")
    async def draw_pic(self, event: AstrMessageEvent):
        """E-Hentai 随机抽卡。用法：/抽图 [二次元|三次元] [擦边|r18] [关键词...] [数量]，/抽图 帮助 查看说明"""
        async for result in self._handle(event):
            yield result

    @filter.command("随机角色")
    async def random_character(self, event: AstrMessageEvent):
        """每张图先随机抽一个角色再抽图。用法同 /抽图"""
        async for result in self._handle(event, random_character=True):
            yield result

    @filter.command("二次元")
    async def alias_anime(self, event: AstrMessageEvent):
        """随机二次元图，等同于 /抽图 二次元"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, style=ANIME):
                yield result

    @filter.command("三次元")
    async def alias_real(self, event: AstrMessageEvent):
        """随机三次元图，等同于 /抽图 三次元"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, style=REAL):
                yield result

    @filter.command("擦边")
    async def alias_sensitive(self, event: AstrMessageEvent):
        """随机擦边图，等同于 /抽图 擦边"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, rating=SENSITIVE):
                yield result

    @filter.command("色图")
    async def alias_explicit(self, event: AstrMessageEvent):
        """随机 R18 图（仅私聊），等同于 /抽图 r18"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, rating=EXPLICIT):
                yield result

    @filter.command("pdf", alias={"全集"})
    async def gallery_pdf(self, event: AstrMessageEvent):
        """回复抽到的图片，获取整个画廊的 PDF。也可以 /pdf <画廊链接>"""
        async for result in self._handle_pdf(event):
            yield result

    @filter.regex(NO_PREFIX_RE)
    async def no_prefix(self, event: AstrMessageEvent):
        """群聊里不带 / 也能触发本插件的指令。"""
        # 带了唤醒前缀、@ 了机器人或私聊时，已经由上面的指令处理
        if getattr(event, "is_at_or_wake_command", False):
            return
        if not self.config.get("no_prefix_trigger", True):
            return
        word = event.message_str.split()[0].lower()
        if word in ALIAS_WORDS and not self.config.get("enable_aliases", True):
            return
        handlers = {
            "抽图": lambda: self._handle(event),
            "随机角色": lambda: self._handle(event, random_character=True),
            "二次元": lambda: self._handle(event, style=ANIME),
            "三次元": lambda: self._handle(event, style=REAL),
            "擦边": lambda: self._handle(event, rating=SENSITIVE),
            "色图": lambda: self._handle(event, rating=EXPLICIT),
            "pdf": lambda: self._handle_pdf(event),
            "全集": lambda: self._handle_pdf(event),
        }
        async for result in handlers[word]():
            yield result

    async def _handle(
        self,
        event: AstrMessageEvent,
        style: str | None = None,
        rating: str | None = None,
        random_character: bool = False,
    ):
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        if not self._allowed(user_id, group_id):
            return

        tokens = event.message_str.split()[1:]
        if tokens and tokens[0].lower() in HELP_WORDS:
            yield event.plain_result(self.help_text())
            return
        req = parse_args(
            tokens,
            self.max_count,
            style or self.default_style,
            rating or self.default_rating,
            self.default_count,
            self.default_per_album,
        )
        req.random_character = random_character
        is_private = event.is_private_chat()

        r18_enabled = bool(self.config.get("r18_enabled", False))
        denied = request_gate(
            req.rating,
            is_private,
            r18_enabled,
            bool(self.config.get("group_sensitive_enabled", False)),
            self.rating_enabled,
        )
        if denied:
            yield event.plain_result(denied)
            return

        limited = self._check_limits(user_id, req)
        if limited:
            yield event.plain_result(limited)
            return
        self._last_use[user_id] = time.monotonic()

        result = await self.drawer.draw(
            req,
            is_private,
            unrated_allowed(is_private, r18_enabled, self.rating_enabled),
        )
        if not result.images:
            detail = "；".join(result.errors[:6]) or "未知原因"
            logger.warning(f"[random_pic] 获取失败 {req}: {detail}")
            yield event.plain_result(
                f"获取{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}图片失败：{detail}"
            )
            return

        self._daily[user_id] = self._daily.get(user_id, 0) + len(result.images)
        refs = [album_ref(album) for album in result.albums]
        self.registry.record(
            event.unified_msg_origin,
            [
                (path.stat().st_size, item_label(item), ref)
                for album, ref in zip(result.albums, refs)
                for item, path in album
            ],
            refs,
        )
        messages = self._compose(
            result.albums,
            self._send_mode(event, result.albums),
            str(event.get_self_id()),
        )
        for i, (chain, idxs) in enumerate(messages):
            if i:
                await asyncio.sleep(SEND_INTERVAL)
            sent, message_id = await self._send_direct(event, chain)
            if message_id:
                self.registry.record_message(
                    message_id, [(idx, refs[idx - 1]) for idx in idxs]
                )
            if not sent:
                yield event.chain_result(chain)
        if len(result.albums) < req.count:
            yield event.plain_result(
                f"仅获取到 {len(result.albums)}/{req.count} 个图集。"
            )

    def _album_content(self, idx: int, album: Album, caption: bool = True) -> list:
        """一个图集的消息段：每张图上方的标题行、图片，最后是说明文字。

        第一张图上方是「【序号】标题」和「第 x/y 张 · 来源」，同一图集后面的图只有「第 x/y 张」。
        """
        show_header = self.config.get("send_header", True)
        content = []
        for i, (item, path) in enumerate(album):
            if show_header:
                if i == 0:
                    text = header_text(
                        idx, item.page, item.pages, item.title, source_name(item)
                    )
                else:
                    text = page_text(item.page, item.pages)
                content.append(Comp.Plain(text + "\n"))
            content.append(Comp.Image.fromFileSystem(str(path)))
        text = (
            format_caption(album[0][0])
            if caption and self.config.get("send_caption", True)
            else ""
        )
        if text:
            content.append(Comp.Plain("\n" + text))
        return content

    def _compose(
        self, albums: list[Album], mode: str, uin: str
    ) -> list[tuple[list, list[int]]]:
        """把图集排成要发送的消息：[(消息段, 这条消息里的图集序号)]。"""
        numbered = list(enumerate(albums, 1))
        if mode == FORWARD:
            # 合并转发：每个图集一个节点，节点顺序即序号
            nodes = [
                Comp.Node(content=self._album_content(idx, album), uin=uin, name="抽图")
                for idx, album in numbered
            ]
            return [([Comp.Nodes(nodes)], [idx for idx, _ in numbered])]
        if mode == SEPARATE:
            # 逐条发送：每张图一条消息、都带完整标题，图集的说明文字跟在最后一张后面
            return [
                (self._album_content(idx, [image], i == len(album) - 1), [idx])
                for idx, album in numbered
                for i, image in enumerate(album)
            ]
        # 图文混合：图集依次排在一条消息里，每条最多 MIXED_PER_MESSAGE 张图；
        # 超过的图集拆开，后半段重新带上完整标题，说明文字只跟在最后一段
        messages, chain, idxs, count = [], [], [], 0
        for idx, album in numbered:
            for start in range(0, len(album), MIXED_PER_MESSAGE):
                part = album[start : start + MIXED_PER_MESSAGE]
                if chain and count + len(part) > MIXED_PER_MESSAGE:
                    messages.append((chain, idxs))
                    chain, idxs, count = [], [], 0
                last = start + MIXED_PER_MESSAGE >= len(album)
                content = self._album_content(idx, part, last)
                if chain and content and isinstance(content[0], Comp.Plain):
                    content[0] = Comp.Plain("\n\n" + content[0].text)
                chain.extend(content)
                if idx not in idxs:
                    idxs.append(idx)
                count += len(part)
        if chain:
            messages.append((chain, idxs))
        return messages

    def _send_mode(self, event: AstrMessageEvent, albums: list[Album]) -> str:
        """合并转发只在 QQ / OneBot 上可用，其他平台改用图文混合；只有一个图集时也不用合并转发。"""
        mode = self.config.get("send_mode", FORWARD)
        if mode not in (FORWARD, MIXED, SEPARATE):
            mode = FORWARD
        if mode == FORWARD and (
            event.get_platform_name() not in FORWARD_PLATFORMS or len(albums) == 1
        ):
            mode = MIXED
        return mode

    async def _send_direct(
        self, event: AstrMessageEvent, chain: list
    ) -> tuple[bool, str | None]:
        """QQ（OneBot）上直接调用发送接口，拿到消息 ID 供 /pdf 按回复查图集。

        返回 (是否已发送, 消息 ID)。其他平台或发送出错时返回未发送，由 AstrBot 照常发送。
        """
        bot = getattr(event, "bot", None)
        if event.get_platform_name() != "aiocqhttp" or bot is None:
            return False, None
        try:
            from astrbot.core.message.message_event_result import MessageChain
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            group_id = event.get_group_id()
            target = (
                {"group_id": int(group_id)}
                if group_id
                else {"user_id": int(event.get_sender_id())}
            )
            raw = getattr(event.message_obj, "raw_message", None)
            if hasattr(raw, "get") and raw.get("self_id"):
                target["self_id"] = raw["self_id"]
            if len(chain) == 1 and isinstance(chain[0], Comp.Nodes):
                payload = await chain[0].to_dict()
                action = "send_group_forward_msg" if group_id else "send_private_forward_msg"
                ret = await bot.call_action(action, **payload, **target)
            else:
                message = await AiocqhttpMessageEvent._parse_onebot_json(
                    MessageChain(chain)
                )
                action = "send_group_msg" if group_id else "send_private_msg"
                ret = await bot.call_action(action, message=message, **target)
        except Exception as e:
            logger.warning(f"[random_pic] 直接发送失败，改由 AstrBot 发送: {e!r}")
            return False, None
        event._has_send_oper = True  # 避免 AstrBot 认为插件没有回复
        message_id = ret.get("message_id") if isinstance(ret, dict) else None
        if message_id is None:
            logger.warning(f"[random_pic] 发送接口没有返回消息 ID: {ret!r}")
        return True, None if message_id is None else str(message_id)

    async def _handle_pdf(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        if not self._allowed(user_id, group_id):
            return
        tokens = event.message_str.split()[1:]
        if tokens and tokens[0].lower() in HELP_WORDS:
            yield event.plain_result(self.help_text())
            return
        if not self.pdf_conf.get("enabled", True):
            yield event.plain_result("整本 PDF 功能未开启。")
            return

        ref, hint = await self._pdf_target(event, tokens)
        if ref is None:
            yield event.plain_result(hint)
            return
        if not ref.gid:
            yield event.plain_result(
                "16K、哔咔的图片没有 E-Hentai 画廊，不支持整本 PDF。"
            )
            return
        try:
            gallery = await self.drawer.eh.gallery(ref.gid, ref.token)
        except (
            EHentaiError,
            HttpError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as e:
            logger.warning(f"[random_pic] 查询画廊 {ref.gid} 失败: {e!r}")
            yield event.plain_result(f"查询画廊失败：{e}")
            return
        if gallery is None or gallery.expunged:
            yield event.plain_result("画廊不存在或已被删除。")
            return
        denied = self._pdf_gate(gallery, event.is_private_chat())
        if denied:
            yield event.plain_result(denied)
            return
        files = self._cached_pdfs(gallery)
        if files is None:
            if self._pdf_lock.locked():
                yield event.plain_result("正在打包另一个画廊，请稍后再试。")
                return
            async with self._pdf_lock:
                per_file = self._pages_per_file()
                parts = -(-gallery.filecount // per_file)
                split = f"，分 {parts} 个文件发送" if parts > 1 else ""
                yield event.plain_result(
                    f"开始下载《{gallery.title}》共 {gallery.filecount} 页{split}，完成后发送 PDF……"
                )
                try:
                    files, missing = await self._build_pdf(gallery)
                except EHentaiError as e:
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

    async def _pdf_target(
        self, event: AstrMessageEvent, tokens: list[str]
    ) -> tuple[GalleryRef | None, str]:
        """依次看参数里的画廊链接、被回复的消息、本会话上一次抽卡。

        回复的消息里有多个图集（合并转发、图文混合）时必须带序号，不带就列出来提示。
        被回复的消息先按消息 ID 查登记表，查不到再解析消息内容。
        """
        for token in tokens:
            ref = gallery_from_text(token)
            if ref:
                return ref, ""
        index = next((int(t) for t in tokens if t.isdigit()), None)
        reply = next(
            (c for c in event.message_obj.message if isinstance(c, Comp.Reply)), None
        )
        if reply is not None:
            albums = self.registry.by_message(str(reply.id)) if reply.id else None
            if albums is not None:
                return pick(message_candidates(albums), index, strict=True)
            units = await self._reply_units(event, reply)
            return pick(resolve_units(units, self.registry), index, strict=True)
        candidates = last_candidates(self.registry.last(event.unified_msg_origin))
        return pick(candidates, index, strict=False)

    async def _reply_units(self, event: AstrMessageEvent, reply) -> list[Unit]:
        """读取被回复消息的原始消息段（需要文件大小和合并转发内容），失败时退回纯文本。"""
        bot = getattr(event, "bot", None)
        if bot is not None and reply.id:
            try:
                raw = await bot.call_action("get_msg", message_id=int(reply.id))

                async def get_forward(forward_id: str) -> list:
                    data = await bot.call_action("get_forward_msg", id=forward_id)
                    return data.get("messages") or data.get("message") or []

                return await message_units(raw.get("message"), get_forward)
            except Exception as e:
                logger.warning(f"[random_pic] 读取被回复的消息失败: {e!r}")
        return [Unit(reply.message_str or "")]

    def _pdf_gate(self, gallery, is_private: bool) -> str | None:
        """整本打包和抽图走同样的分级闸门；无法判定分级的按 R18 处理。"""
        if not gallery.tags:
            return "画廊缺少标签，无法做未成年过滤，不予打包。"
        heavy = heavy_hit(gallery.tags, self.heavy)
        if heavy:
            return f"画廊带重口标签（{heavy}），不予打包。"
        if self.drawer.blacklist.hit(gallery.tags):
            return "画廊命中黑名单标签，不予打包。"
        _, rating = classify(gallery.category, gallery.tags)
        return request_gate(
            rating or EXPLICIT,
            is_private,
            bool(self.config.get("r18_enabled", False)),
            bool(self.config.get("group_sensitive_enabled", False)),
            self.rating_enabled,
        )

    def _pages_per_file(self) -> int:
        return max(1, int(self.pdf_conf.get("pages_per_file", 200)))

    def _cached_pdfs(self, gallery) -> list[tuple[Path, str]] | None:
        """完整打包过的画廊直接复用；缺任何一个分卷都视为没有缓存。"""
        per_file = self._pages_per_file()
        ranges = [
            (start, min(start + per_file - 1, gallery.filecount))
            for start in range(1, gallery.filecount + 1, per_file)
        ]
        files = [
            (self.pdf_dir / pdf_stored_name(gallery.gid, i, len(ranges)), r)
            for i, r in enumerate(ranges, 1)
        ]
        if not files or not all(path.exists() for path, _ in files):
            return None
        return [
            (path, pdf_filename(gallery.title, gallery.gid, r, len(files)))
            for path, r in files
        ]

    async def _build_pdf(self, gallery) -> tuple[list[tuple[Path, str]], int]:
        """下载整个画廊，每 pages_per_file 页写成一个 PDF。

        返回 ([(PDF 路径, 发送文件名)], 失败页数)。有缺页时存储名带 -incomplete，
        不会被当成完整缓存复用。
        """
        tmp = self.pdf_tmp / str(gallery.gid)
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            concurrency = int(self.pdf_conf.get("concurrency", 3))
            paths, missing = await self.drawer.download_gallery(
                gallery, tmp, concurrency
            )
            if not paths:
                raise EHentaiError("没有下载到任何图片")
            self.pdf_dir.mkdir(parents=True, exist_ok=True)
            per_file = self._pages_per_file()
            chunks = [paths[i : i + per_file] for i in range(0, len(paths), per_file)]
            files = []
            for i, chunk in enumerate(chunks, 1):
                stored = pdf_stored_name(gallery.gid, i, len(chunks), bool(missing))
                out = self.pdf_dir / stored
                await asyncio.to_thread(write_pdf, chunk, out)
                # 下载的图片以页码命名，分卷文件名标出实际页码范围
                pages = (int(chunk[0].stem), int(chunk[-1].stem))
                files.append(
                    (out, pdf_filename(gallery.title, gallery.gid, pages, len(chunks)))
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._prune_pdfs(keep=gallery.gid)
        return files, missing

    def _prune_pdfs(self, keep: int):
        """按画廊清理旧 PDF，只动本插件生成的文件（输出目录可能是共享目录）。"""
        limit = max(1, int(self.pdf_conf.get("keep_files", 10)))
        groups: dict[int, list[Path]] = {}
        for path in self.pdf_dir.glob("*.pdf"):
            match = OWN_PDF.fullmatch(path.name)
            if match:
                groups.setdefault(int(match.group(1)), []).append(path)
        newest = {
            gid: max(p.stat().st_mtime for p in paths) for gid, paths in groups.items()
        }
        others = sorted(
            (gid for gid in groups if gid != keep), key=newest.get, reverse=True
        )
        for gid in others[limit - 1 :]:
            for path in groups[gid]:
                path.unlink(missing_ok=True)

    def help_text(self) -> str:
        on_off = {True: "已开启", False: "未开启"}
        lines = [
            "【抽图用法】",
            "/抽图 [二次元|三次元] [擦边|r18] [关键词...] [图集数 或 图集数x每集张数]",
            f"· 参数顺序随意；不写时默认 {STYLE_NAMES[self.default_style]}·"
            f"{RATING_NAMES[self.default_rating]}、{self.default_count} 个图集、每集 "
            f"{self.default_per_album} 张，每次最多 {self.max_count} 张",
            "· 一个图集是同一画廊 / 帖子 / 本子里的几张图；例如 /抽图 3x4 抽 3 个图集、每集 4 张",
            "· 关键词可写中文角色、作品名（如 芙莉莲、原神）或 E-Hentai 标签，前缀 - 表示排除",
            "/随机角色 [同上]：每个图集先随机抽一个角色",
        ]
        if self.pdf_conf.get("enabled", True):
            lines.append(
                f"/pdf 或 /全集：回复抽到的图片，获取整个画廊的 PDF，每 {self._pages_per_file()} 页一个文件"
                "（回复的消息里有多个图集时必须加序号，即【】里的数字，如 /pdf 2；"
                "也可以 /pdf <画廊链接>）"
            )
        if self.config.get("enable_aliases", True):
            lines.append("别名：/二次元 /三次元 /擦边 /色图")
        r18 = on_off[bool(self.config.get("r18_enabled", False))]
        if self.rating_enabled:
            lines.append(
                f"R18：仅限私聊（{r18}）；"
                f"群聊擦边：{on_off[bool(self.config.get('group_sensitive_enabled', False))]}"
            )
        else:
            lines.append(f"内容分级已关闭，群聊和私聊内容相同；R18：{r18}")
        if self.sixteenk_ratio:
            where = "私聊且开启 R18 时" if self.rating_enabled else ""
            lines.append(
                f"三次元不带关键词时，{where}部分图片来自 16K（没有分级，擦边和 R18 都可能抽到）"
            )
        if self.pica_ratio:
            lines.append(
                "三次元部分图片来自哔咔的 Cosplay 分类（可搜普通关键词，E-Hentai 标签语法除外）"
            )
        lines += [
            "示例：/抽图 原神 2　/抽图 二次元 芙莉莲",
            "/抽图 帮助：显示本说明",
        ]
        return "\n".join(lines)

    def _allowed(self, user_id: str, group_id: str) -> bool:
        blacklist = {str(u) for u in self.access.get("user_blacklist", [])}
        if user_id in blacklist:
            return False
        whitelist = {str(g) for g in self.access.get("group_whitelist", [])}
        if group_id and whitelist and group_id not in whitelist:
            return False
        return True

    def _check_limits(self, user_id: str, req: PicRequest) -> str | None:
        cooldown = float(self.access.get("cooldown_seconds", 0))
        last = self._last_use.get(user_id)
        if cooldown > 0 and last is not None:
            remain = cooldown - (time.monotonic() - last)
            if remain > 0:
                return f"冷却中，请 {remain:.0f} 秒后再试。"

        daily_limit = int(self.access.get("daily_limit", 0))
        if daily_limit > 0:
            if self._daily_date != date.today():
                self._daily_date = date.today()
                self._daily.clear()
            used = self._daily.get(user_id, 0)
            if used >= daily_limit:
                return f"今日次数已用完（{daily_limit} 张）。"
            # 剩余额度不够时先减少图集数，只剩不到一个图集的额度时减少每集张数
            remain = daily_limit - used
            req.per_album = min(req.per_album, remain)
            req.count = max(1, min(req.count, remain // req.per_album))
        return None

    async def terminate(self):
        if self._preload and not self._preload.done():
            self._preload.cancel()
        await self.http.close()
