"""E-Hentai 随机抽卡插件：指令与参数解析。"""

import asyncio
import time
from datetime import date

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .drawer import Drawer, build_pools
from .ehentai import EHentai
from .filters import TagBlacklist, request_gate
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
from .net import HttpClient, ImageCache
from .tags import DEFAULT_DB_URL, TagDB

PLUGIN_NAME = "astrbot_plugin_random_pic"
EH_SITE = "https://e-hentai.org"
EH_API = "https://api.e-hentai.org/api.php"

STYLE_WORDS = {"二次元": ANIME, "三次元": REAL}
RATING_WORDS = {"擦边": SENSITIVE, "r18": EXPLICIT, "色图": EXPLICIT}


def parse_args(
    tokens: list[str],
    max_count: int,
    style: str = ANIME,
    rating: str = SENSITIVE,
) -> PicRequest:
    """宽松解析：风格词、分级词、数字（数量）可任意顺序，其余当作搜索关键词。"""
    req = PicRequest(style=style, rating=rating)
    for token in tokens:
        low = token.lower()
        if token in STYLE_WORDS:
            req.style = STYLE_WORDS[token]
        elif low in RATING_WORDS:
            req.rating = RATING_WORDS[low]
        elif token.isdigit():
            req.count = int(token)
        else:
            req.tags.append(token)
    req.count = max(1, min(req.count, max(1, max_count)))
    return req


def format_caption(item: ImageItem) -> str:
    lines = []
    if item.title:
        lines.append(f"标题：{item.title}")
    if item.author:
        lines.append(f"作者：{item.author}")
    if item.parodies:
        lines.append(f"作品：{'、'.join(item.parodies)}")
    if item.characters:
        lines.append(f"角色：{'、'.join(item.characters)}")
    info = [item.category, f"第 {item.page}/{item.pages} 页"]
    if item.stars:
        info.append(f"★{item.stars:.1f}")
    lines.append(" · ".join(i for i in info if i))
    lines.append(f"画廊：{item.gallery_url}")
    return "\n".join(lines)


class RandomPicPlugin(Star):
    """E-Hentai 随机抽卡：/随机图 [二次元|三次元] [擦边|r18] [关键词...] [数量]"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_count = max(1, int(config.get("max_count", 5)))
        self.access = config.get("access", {})
        self._last_use: dict[str, float] = {}
        self._daily: dict[str, int] = {}
        self._daily_date = date.today()

        eh_conf = config.get("ehentai", {})
        # nw=1 跳过画廊的内容警告页
        self.http = HttpClient(float(config.get("request_timeout", 20)), {"nw": "1"})
        cache_conf = config.get("cache", {})
        cache = ImageCache(
            self.http,
            StarTools.get_data_dir(PLUGIN_NAME) / "cache",
            max_files=max(int(cache_conf.get("max_files", 100)), self.max_count),
            max_total_mb=float(cache_conf.get("max_total_mb", 200)),
            max_image_mb=float(cache_conf.get("max_image_mb", 10)),
        )
        proxy = (eh_conf.get("proxy") or "").strip() or None
        eh = EHentai(
            self.http,
            EH_SITE,
            EH_API,
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
            TagBlacklist(config.get("extra_blacklist", [])),
            cache,
            int(config.get("max_retries", 3)),
            exclude_ai=bool(eh_conf.get("exclude_ai", True)),
            min_stars=int(eh_conf.get("min_rating", 4)),
            min_pages=int(eh_conf.get("min_pages", 0)),
            cover_only=eh_conf.get("page_pick", "随机页") == "封面",
            explicit_skip=float(eh_conf.get("explicit_skip_ratio", 0.3)),
            tags=self.tagdb,
        )

    async def initialize(self):
        # 后台预加载标签库，避免第一次抽卡时等待下载
        if self.tagdb:
            self._preload = asyncio.create_task(self.tagdb.get())

    @filter.command("随机图")
    async def random_pic(self, event: AstrMessageEvent):
        """E-Hentai 随机抽卡。用法：/随机图 [二次元|三次元] [擦边|r18] [关键词...] [数量]"""
        async for result in self._handle(event):
            yield result

    @filter.command("随机角色")
    async def random_character(self, event: AstrMessageEvent):
        """每张图先随机抽一个角色再抽图。用法同 /随机图"""
        async for result in self._handle(event, random_character=True):
            yield result

    @filter.command("二次元")
    async def alias_anime(self, event: AstrMessageEvent):
        """随机二次元擦边图，等同于 /随机图 二次元"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, style=ANIME):
                yield result

    @filter.command("三次元")
    async def alias_real(self, event: AstrMessageEvent):
        """随机三次元（Cosplay）擦边图，等同于 /随机图 三次元"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, style=REAL):
                yield result

    @filter.command("擦边")
    async def alias_sensitive(self, event: AstrMessageEvent):
        """随机擦边图，等同于 /随机图 擦边"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, rating=SENSITIVE):
                yield result

    @filter.command("色图")
    async def alias_explicit(self, event: AstrMessageEvent):
        """随机 R18 图（仅私聊），等同于 /随机图 r18"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, rating=EXPLICIT):
                yield result

    async def _handle(
        self,
        event: AstrMessageEvent,
        style: str = ANIME,
        rating: str = SENSITIVE,
        random_character: bool = False,
    ):
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        if not self._allowed(user_id, group_id):
            return

        tokens = event.message_str.split()[1:]
        req = parse_args(tokens, self.max_count, style, rating)
        req.random_character = random_character
        is_private = event.is_private_chat()

        denied = request_gate(
            req.rating,
            is_private,
            bool(self.config.get("r18_enabled", False)),
            bool(self.config.get("group_sensitive_enabled", False)),
        )
        if denied:
            yield event.plain_result(denied)
            return

        limited = self._check_limits(user_id, req)
        if limited:
            yield event.plain_result(limited)
            return
        self._last_use[user_id] = time.monotonic()

        result = await self.drawer.draw(req, is_private)
        if not result.images:
            detail = "；".join(result.errors[:6]) or "未知原因"
            logger.warning(f"[random_pic] 获取失败 {req}: {detail}")
            yield event.plain_result(
                f"获取{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}图片失败：{detail}"
            )
            return

        self._daily[user_id] = self._daily.get(user_id, 0) + len(result.images)
        for item, path in result.images:
            yield event.chain_result(
                [Comp.Image.fromFileSystem(str(path)), Comp.Plain(format_caption(item))]
            )
        if len(result.images) < req.count:
            yield event.plain_result(
                f"仅获取到 {len(result.images)}/{req.count} 张图片。"
            )

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
            req.count = min(req.count, daily_limit - used)
        return None

    async def terminate(self):
        if self._preload and not self._preload.done():
            self._preload.cancel()
        await self.http.close()
