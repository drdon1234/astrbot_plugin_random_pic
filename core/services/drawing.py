"""抽卡调度：翻译并检查关键词，按比例给每个图集分配这次能用的图源，各图源并发抽取。"""

import asyncio
import random
from dataclasses import dataclass, field
from typing import Protocol

from astrbot.api import logger

from ..models import ANIME, RATING_NAMES, STYLE_NAMES, Album, DrawContext, DrawRequest
from ..policy.content import ContentFilter
from ..sources.base import Source
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


class DrawSources(Protocol):
    """抽取调度只依赖可抽来源及补位来源，不装配具体站点。"""

    drawing: list[tuple[Source, int]]
    fallback: Source | None


class Drawer:
    def __init__(self, sources: DrawSources, content: ContentFilter, tagdb: TagDB):
        self.sources = sources
        self.content = content
        self.tagdb = tagdb

    def named(self, ctx: DrawContext) -> list:
        """关键词指名的图源（订阅）。"""
        return [s for s, _ in self.sources.drawing if s.named(ctx.req.keywords)]

    def assign(self, ctx: DrawContext) -> dict:
        """每个图集按比例选一个这次能用的图源，返回 {图源: 图集数}；没有能用的图源时为空。

        关键词指名了订阅时只从指名的、分级相符的订阅里抽。
        """
        named = self.named(ctx)
        if named:
            usable = [
                (s, w)
                for s, w in self.sources.drawing
                if s in named and ctx.req.rating in s.usable_ratings
            ]
        else:
            usable = [(s, w) for s, w in self.sources.drawing if s.accepts(ctx)]
        if not usable:
            return {}
        picked, weights = zip(*usable)
        counts = dict.fromkeys(picked, 0)
        for source in random.choices(picked, weights, k=ctx.req.albums):
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
        plan = self.assign(ctx)
        if not plan:
            named = self.named(ctx)
            if named:
                names = "、".join(s.name for s in named)
                return DrawResult(
                    errors=[f"{names} 没有{RATING_NAMES[req.rating]}内容"]
                )
            kind = f"{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}"
            return DrawResult(
                errors=[f"没有能抽{kind}的图源，请在源开关与详情里启用或调整适用分级"]
            )
        # 指名订阅时不补位（补位图源会把订阅名当关键词去搜）
        fallback = None if self.named(ctx) else self.sources.fallback

        async def attempt(source, n: int) -> tuple[list[Album], list[str]]:
            """主来源与补位来源使用相同的异常隔离，取消继续向上传播。"""
            try:
                return await source.draw(ctx, n)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[random_pic] {source.name} 抽取出错: {e!r}", exc_info=e)
                return [], [f"{source.name} 出错：{e!r}"]

        async def run(source, n: int) -> tuple[list[Album], list[str]]:
            """抽一个图源；没抽够时马上由补位图源补，不等别的图源。"""
            albums, errors = await attempt(source, n)
            short = n - len(albums)
            if (
                short > 0
                and fallback is not None
                and source is not fallback
                and fallback.accepts(ctx)
            ):
                more, more_errors = await attempt(fallback, short)
                albums, errors = albums + more, errors + more_errors
            return albums, errors

        result = DrawResult()
        jobs = [asyncio.create_task(run(source, n)) for source, n in plan.items()]
        try:
            outcomes = await asyncio.gather(*jobs)
        finally:
            for job in jobs:
                if not job.done():
                    job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
        for albums, errors in outcomes:
            result.albums.extend(albums)
            result.errors.extend(errors)
        random.shuffle(result.albums)
        return result
