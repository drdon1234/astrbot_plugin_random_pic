"""随机图片插件：指令与参数解析。"""

import time
from datetime import date

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .filters import TagBlacklist, request_gate
from .models import (
    ANIME,
    EXPLICIT,
    GENERAL,
    RATING_NAMES,
    REAL,
    SENSITIVE,
    STYLE_NAMES,
    ImageItem,
    PicRequest,
)
from .net import HttpClient, ImageCache
from .providers import build_providers
from .router import Router

PLUGIN_NAME = "astrbot_plugin_random_pic"

STYLE_WORDS = {"二次元": ANIME, "三次元": REAL}
RATING_WORDS = {"全年龄": GENERAL, "擦边": SENSITIVE, "r18": EXPLICIT, "色图": EXPLICIT}
TWITTER_TAG = "推特"


def parse_args(
    tokens: list[str],
    max_count: int,
    style: str = ANIME,
    rating: str = GENERAL,
) -> PicRequest:
    """宽松解析：风格词、分级词、数字（数量）可任意顺序，其余当作标签。"""
    req = PicRequest(style=style, rating=rating)
    for token in tokens:
        low = token.lower()
        if token in STYLE_WORDS:
            req.style = STYLE_WORDS[token]
        elif low in RATING_WORDS:
            req.rating = RATING_WORDS[low]
        elif token.isdigit():
            req.count = int(token)
        elif token == TWITTER_TAG:
            req.twitter = True
        else:
            req.tags.append(token)
    req.count = max(1, min(req.count, max(1, max_count)))
    return req


def format_caption(item: ImageItem) -> str:
    lines = []
    if item.author:
        lines.append(f"作者：{item.author}")
    if item.title:
        lines.append(f"标题：{item.title}")
    if item.source_url:
        lines.append(f"来源：{item.source_url}")
    if item.post_url:
        lines.append(f"图站：{item.post_url}")
    if not item.source_url and not item.post_url:
        lines.append("来源：来源未知")
    lines.append(f"图源：{item.provider}")
    return "\n".join(lines)


class RandomPicPlugin(Star):
    """随机图片：/随机图 [二次元|三次元] [全年龄|擦边|r18] [标签...] [数量]"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_count = max(1, int(config.get("max_count", 5)))
        self.access = config.get("access", {})
        self._last_use: dict[str, float] = {}
        self._daily: dict[str, int] = {}
        self._daily_date = date.today()

        self.http = HttpClient(float(config.get("request_timeout", 20)))
        cache_conf = config.get("cache", {})
        lolicon_host = config.get("lolicon", {}).get("proxy_host") or "i.pixiv.re"
        cache = ImageCache(
            self.http,
            StarTools.get_data_dir(PLUGIN_NAME) / "cache",
            max_files=max(int(cache_conf.get("max_files", 100)), self.max_count),
            max_total_mb=float(cache_conf.get("max_total_mb", 200)),
            max_image_mb=float(cache_conf.get("max_image_mb", 10)),
            pixiv_hosts={lolicon_host.lower()},
        )
        self.router = Router(
            build_providers(config, self.http),
            config.get("routes", {}),
            TagBlacklist(config.get("extra_blacklist", [])),
            cache,
            int(config.get("max_retries", 3)),
        )

    @filter.command("随机图")
    async def random_pic(self, event: AstrMessageEvent):
        """随机图片。用法：/随机图 [二次元|三次元] [全年龄|擦边|r18] [标签...] [数量]"""
        async for result in self._handle(event):
            yield result

    @filter.command("二次元")
    async def alias_anime(self, event: AstrMessageEvent):
        """随机二次元图片，等同于 /随机图 二次元"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, style=ANIME):
                yield result

    @filter.command("三次元")
    async def alias_real(self, event: AstrMessageEvent):
        """随机三次元图片，等同于 /随机图 三次元"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, style=REAL):
                yield result

    @filter.command("擦边")
    async def alias_sensitive(self, event: AstrMessageEvent):
        """随机擦边图片，等同于 /随机图 擦边"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, rating=SENSITIVE):
                yield result

    @filter.command("色图")
    async def alias_explicit(self, event: AstrMessageEvent):
        """随机 R18 图片（仅私聊），等同于 /随机图 r18"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, rating=EXPLICIT):
                yield result

    async def _handle(
        self, event: AstrMessageEvent, style: str = ANIME, rating: str = GENERAL
    ):
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        if not self._allowed(user_id, group_id):
            return

        tokens = event.message_str.split()[1:]
        req = parse_args(tokens, self.max_count, style, rating)
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

        result = await self.router.fetch(req, is_private)
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
        await self.http.close()
