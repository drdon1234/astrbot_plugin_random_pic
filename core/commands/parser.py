"""统一解析交互命令、预备池和定时推送的抽取参数。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..config.settings import PDF_FORMAT, Settings
from ..models import (
    ANIME,
    EXPLICIT,
    RATING_WORDS,
    REAL,
    SENSITIVE,
    STYLE_WORDS,
    DrawRequest,
)

if TYPE_CHECKING:
    from ..sources.feeds.registry import FeedSet

# 数量写成「图集数x每集张数」
COUNT_RE = re.compile(r"(\d+)[x*×](\d+)")
INVALID_COUNT_RE = re.compile(r"[+-]?\d+(?:\.\d+)?(?:[x*×].*)?$")
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
VIDEO_COMMAND = "抽视频"


def parse_args(
    tokens: list[str], max_images: int, defaults: DrawRequest, whole_format: str
) -> DrawRequest:
    """宽松解析：风格词、分级词、数量、全集 / pdf 可任意顺序，其余当作关键词。

    数量写一个数字是图集数，写成「图集数x每集张数」（如 3x4）同时指定每集张数。
    每集张数也适用于二次元订阅；单图站由图源自身保证一集一张。
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
    for index, token in enumerate(tokens):
        low = token.lower()
        if token in STYLE_WORDS:
            req.style = STYLE_WORDS[token]
        elif low in RATING_WORDS:
            req.rating = RATING_WORDS[low]
        elif low in WHOLE_WORDS:
            req.whole = WHOLE_WORDS[low] or whole_format
        elif token.isdecimal():
            req.albums = int(token)
            if req.albums < 1:
                raise ValueError("数量必须是正整数，例如 3 或 3x4。")
        elif match := COUNT_RE.fullmatch(low):
            req.albums, req.per_album = int(match.group(1)), int(match.group(2))
            if req.albums < 1 or req.per_album < 1:
                raise ValueError("数量必须是正整数，例如 3 或 3x4。")
        elif (
            token.isnumeric()
            or INVALID_COUNT_RE.fullmatch(low)
            or (low in {"x", "*", "×"} and index > 0 and tokens[index - 1].isdecimal())
        ):
            raise ValueError(f"数量「{token}」写法不对，请使用正整数或不带空格的 3x4。")
        else:
            req.keywords.append(token)
    if req.whole:
        req.albums = req.per_album = 1
    requested = (req.albums, req.per_album)
    req.per_album = max(1, min(req.per_album, max_images // req.albums))
    req.albums = max(1, min(req.albums, max_images // req.per_album))
    if requested != (req.albums, req.per_album):
        req.notices.append(
            f"单次最多 {max_images} 张，已从 {requested[0]} 集×{requested[1]} 张"
            f"调整为 {req.albums} 集×{req.per_album} 张。"
        )
    return req


class RequestParser:
    """配置和订阅共同决定默认参数。"""

    def __init__(self, settings: Settings, feeds: FeedSet):
        self.settings = settings
        self.feeds = feeds

    def _defaults(self, tokens: list[str]) -> tuple[str, str]:
        """不写风格、分级时的默认值：参数指名了订阅时是这个订阅的，否则是配置的。"""
        feed = self.feeds.named(tokens) if self.settings.sources.feeds.enabled else None
        if feed:
            return feed.conf.style, feed.conf.rating
        return self.settings.draw.style, self.settings.draw.rating

    def image(self, word: str, tokens: list[str]) -> DrawRequest:
        """按抽图指令词和参数生成请求。"""
        draw = self.settings.draw
        preset = DRAW_COMMANDS[word]
        style, rating = self._defaults(tokens)
        defaults = DrawRequest(
            preset.get("style", style),
            preset.get("rating", rating),
            albums=draw.album_count,
            per_album=draw.images_per_album,
            random_character=preset.get("random_character", False),
        )
        return parse_args(
            tokens, draw.max_images, defaults, self.settings.whole.default_format
        )

    def video(self, tokens: list[str]) -> DrawRequest:
        """视频请求：风格、分级默认和抽图一样，每个「图集」就是一个视频。"""
        video = self.settings.video
        if any(COUNT_RE.fullmatch(token.lower()) for token in tokens):
            raise ValueError("视频数量请使用一个正整数，例如 2；视频没有每集张数。")
        style, rating = self._defaults(tokens)
        defaults = DrawRequest(style, rating, albums=video.default_count)
        req = parse_args(
            tokens, video.max_count, defaults, self.settings.whole.default_format
        )
        req.per_album = 1
        if req.notices:
            req.notices[:] = [
                f"单次最多 {video.max_count} 个视频，已调整为 {req.albums} 个。"
            ]
        return req
