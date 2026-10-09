"""Danbooru 视频（二次元 R18）：和图片同样按分级、最低评分、关键词、随机角色和儿童角色过滤，只取 mp4。

filetype: 和 rating:、score: 一样不占匿名搜索的 2 个标签额度（2026-10 实测）。score >= 150 时
rating:q,e 的 mp4 约 1.34 万个，截帧目检 8/8 都是 R18 动画；rating:s 的视频只有约 700 个、
多是宣传片和日常动画，达不到擦边，所以只用于 R18。帖子的 media_asset 里有视频时长。

random:N 加上帖子很多的标签（如 genshin_impact）和 filetype: 时数据库会超时（500），游标翻页不会，
所以 random:N 超时过的搜索条件之后都改用游标。
"""

from dataclasses import replace

from astrbot.api import logger


from ..models import EXPLICIT, Album, DrawContext, WorkRef
from ..net import HttpError
from ..sources.danbooru import (
    EXCLUDE_TAGS,
    FIELDS,
    HEADERS,
    POST_RATINGS,
    DanbooruSource,
    Pool,
)
from .base import MMD_TAGS, Recent, VideoFiles

# 视频都带 animated，3D 动画也要；其余排除的标签和图片相同
VIDEO_EXCLUDE_TAGS = EXCLUDE_TAGS - {"animated", "3d"}
VIDEO_EXTS = frozenset({"mp4"})


class DanbooruVideoSource(DanbooruSource):
    key = "danbooru"
    name = "Danbooru"
    intro = "高分动画短片，可搜中文角色、作品名"
    link_re = None
    ratings = frozenset({EXPLICIT})
    fields = FIELDS + ",media_asset[duration]"

    def __init__(
        self,
        http,
        cache,
        content,
        opts,
        files: VideoFiles,
        *,
        min_score: int,
        allow_mmd: bool = True,
    ):
        super().__init__(
            http,
            cache,
            content,
            opts,
            min_score=min_score,
            exclude_tags=VIDEO_EXCLUDE_TAGS | (frozenset() if allow_mmd else MMD_TAGS),
        )
        self.files = files
        self.recent = Recent()
        # random:N 超时过的搜索条件
        self._slow: set[str] = set()

    async def _batch(self, pool: Pool, want: int) -> list[dict]:
        if pool.uniform and pool.query in self._slow:
            pool = replace(pool, uniform=False)
        try:
            return await super()._batch(pool, want)
        except HttpError as e:
            if not pool.uniform or e.status != 500:
                raise
            logger.info(f"[random_pic] Danbooru 视频随机查询超时，改用游标: {pool.query}")
            self._slow.add(pool.query)
            return await super()._batch(replace(pool, uniform=False), want)

    def _query(self, rating: str, tags: list[str], score: int) -> str:
        return f"{super()._query(rating, tags, score)} filetype:mp4"

    @staticmethod
    def _duration(post: dict) -> float | None:
        asset = post.get("media_asset")
        return asset.get("duration") if isinstance(asset, dict) else None

    def _reject(self, post: dict, ctx: DrawContext, local: set[str]) -> str | None:
        if not post.get("file_url"):
            return "没有原文件"
        if str(post.get("file_ext") or "").lower() not in VIDEO_EXTS:
            return f"不是 mp4（{post.get('file_ext')}）"
        tags = str(post.get("tag_string") or "").split()
        reason = self.rating_reason(
            POST_RATINGS.get(str(post.get("rating"))), ctx
        ) or self.content.plain_tags_reason(tags)
        if reason:
            return reason
        hit = next((t for t in tags if t in self.exclude or t in local), None)
        if hit:
            return f"带排除的标签 {hit}"
        if f"{self.key}:{post.get('id')}" in self.recent:
            return "最近抽过"
        return self.files.reason(self._duration(post), int(post.get("file_size") or 0))

    async def _album(self, post: dict, ctx: DrawContext) -> Album | None:
        self.recent.add(f"{self.key}:{post.get('id')}")
        path = await self.files.download(post["file_url"], headers=HEADERS)
        if path is None:
            return None
        title, details = self.describe(post, ctx.index)
        return Album(
            source=self.name,
            title=title,
            total=1,
            pictures=[(1, path)],
            details=details,
            work=WorkRef(self.key, str(post.get("id"))),
            duration=self._duration(post),
        )
