"""抽卡调度：翻译并检查关键词，二次元交给 Danbooru，三次元按权重给每个图集分配图源，各图源并发抽取。"""

import asyncio
import random
from dataclasses import dataclass, field

from astrbot.api import logger

from .filters import ContentFilter
from .models import ANIME, Album, DrawContext, DrawRequest
from .sources import SourceSet
from .tags import TagDB


@dataclass
class DrawResult:
    albums: list[Album] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def images(self) -> int:
        return sum(len(album.pictures) for album in self.albums)

    def reason(self) -> str:
        """抽不到时给用户看的原因。"""
        return "；".join(self.errors[:6]) or "未知原因"


class Drawer:
    def __init__(self, sources: SourceSet, content: ContentFilter, tagdb: TagDB):
        self.sources = sources
        self.content = content
        self.tagdb = tagdb

    def assign(self, ctx: DrawContext) -> dict:
        """二次元全部由 Danbooru 抽；三次元每个图集按权重选一个这次能用的图源。返回 {图源: 图集数}。

        能用的三次元图源权重全为 0 时全部由 E-Hentai 抽。
        """
        sources, n = self.sources, ctx.req.albums
        if ctx.req.style == ANIME:
            return {sources.danbooru: n}
        usable = [
            (s, w) for s, w in sources.real if s is sources.ehentai or s.accepts(ctx)
        ]
        if len(usable) == 1 or sum(w for _, w in usable) <= 0:
            return {sources.ehentai: n}
        picked, weights = zip(*usable)
        counts = dict.fromkeys(picked, 0)
        for source in random.choices(picked, weights, k=n):
            counts[source] += 1
        return {s: k for s, k in counts.items() if k}

    async def draw(self, req: DrawRequest, allow_explicit: bool) -> DrawResult:
        """抽 req.albums 个图集，图集之间打乱顺序。allow_explicit：这个会话能否出 R18 结果。"""
        index = await self.tagdb.get()
        terms = [index.translate(t) for t in req.keywords] if index else req.keywords
        reason = self.content.keyword_reason(req.keywords, terms)
        if reason:
            return DrawResult(errors=[reason])
        # 二次元的随机角色来自 Danbooru 的角色列表，三次元的来自标签库
        if (
            req.random_character
            and req.style != ANIME
            and (index is None or not index.characters)
        ):
            return DrawResult(errors=["标签库不可用，无法随机角色"])

        ctx = DrawContext(req, allow_explicit, list(terms), index)
        fallback = self.sources.ehentai

        async def run(source, n: int) -> tuple[list[Album], list[str]]:
            """抽一个图源；其他三次元图源没抽够时马上由 E-Hentai 补，不等别的图源。"""
            try:
                albums, errors = await source.draw(ctx, n)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[random_pic] {source.name} 抽取出错: {e!r}", exc_info=e)
                albums, errors = [], [f"{source.name} 出错：{e!r}"]
            short = n - len(albums)
            if short > 0 and source is not fallback and fallback.accepts(ctx):
                more, more_errors = await fallback.draw(ctx, short)
                albums, errors = albums + more, errors + more_errors
            return albums, errors

        result = DrawResult()
        jobs = [run(source, n) for source, n in self.assign(ctx).items()]
        for albums, errors in await asyncio.gather(*jobs):
            result.albums.extend(albums)
            result.errors.extend(errors)
        random.shuffle(result.albums)
        return result
