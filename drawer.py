"""抽卡调度：翻译并检查关键词，按权重给每个图集分配图源，各图源并发抽取。"""

import asyncio
import random
from dataclasses import dataclass, field

from astrbot.api import logger

from .filters import ContentFilter
from .models import Album, DrawRequest
from .sources import DrawContext
from .sources.ehentai import EHentaiSource
from .tags import TagDB


@dataclass
class DrawResult:
    albums: list[Album] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def images(self) -> int:
        return sum(len(album.pictures) for album in self.albums)


class Drawer:
    def __init__(
        self,
        ehentai: EHentaiSource,
        weights: list[tuple[object, int]],
        content: ContentFilter,
        tagdb: TagDB | None,
    ):
        """weights：[(图源, 三次元权重)]，必须包含 ehentai。其他图源没抽够时由 E-Hentai 补。"""
        self.ehentai = ehentai
        self.weights = [(s, w) for s, w in weights if w > 0 or s is ehentai]
        self.content = content
        self.tagdb = tagdb

    def assign(self, ctx: DrawContext) -> dict:
        """每个图集按权重选一个这次能用的图源，返回 {图源: 图集数}。

        能用的图源权重全为 0 时全部由 E-Hentai 抽。
        """
        usable = [
            (s, w) for s, w in self.weights if s is self.ehentai or s.accepts(ctx)
        ]
        total = sum(w for _, w in usable)
        if total <= 0 or len(usable) == 1:
            return {self.ehentai: ctx.req.albums}
        sources, weights = zip(*usable)
        counts = dict.fromkeys(sources, 0)
        for source in random.choices(sources, weights, k=ctx.req.albums):
            counts[source] += 1
        return {s: n for s, n in counts.items() if n}

    async def draw(
        self, req: DrawRequest, is_private: bool, allow_unrated: bool
    ) -> DrawResult:
        """抽 req.albums 个图集，图集之间打乱顺序。"""
        index = await self.tagdb.get() if self.tagdb else None
        terms = (
            [index.translate(t) for t in req.keywords] if index else list(req.keywords)
        )
        reason = self.content.keyword_reason(req.keywords, terms)
        if reason:
            return DrawResult(errors=[reason])
        if req.random_character and (index is None or not index.characters):
            return DrawResult(errors=["标签库不可用，无法随机角色"])

        ctx = DrawContext(req, is_private, allow_unrated, terms, index)

        async def run(source, n: int) -> tuple[list[Album], list[str]]:
            """抽一个图源；其他图源没抽够时马上由 E-Hentai 补，不等别的图源。"""
            try:
                albums, errors = await source.draw(ctx, n)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[random_pic] {source.name} 抽取出错: {e!r}", exc_info=e)
                albums, errors = [], [f"{source.name} 出错：{e!r}"]
            short = n - len(albums)
            if source is not self.ehentai and short > 0:
                more, more_errors = await self.ehentai.draw(ctx, short)
                albums, errors = albums + more, errors + more_errors
            return albums, errors

        result = DrawResult()
        jobs = [run(source, n) for source, n in self.assign(ctx).items()]
        for albums, errors in await asyncio.gather(*jobs):
            result.albums.extend(albums)
            result.errors.extend(errors)
        random.shuffle(result.albums)
        return result
