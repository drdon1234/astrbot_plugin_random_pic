"""订阅图片与视频共用的过滤、近期去重和帖子抽取。"""

import asyncio
import random
import re

from astrbot.api import logger

from ...models import SENSITIVE, Album, DrawContext
from ...storage.recent import Recent
from ..base import Source, split_terms
from ..videos.common import young_word
from .base import Feed
from .models import Post

# 帖子文字里标明 AI 生成的话题标签（「屏蔽 AI 作品」开启时排除）
AI_TAG_RE = re.compile(
    r"#(?:ai|aiart|aiartwork|aigenerated|aiイラスト|ai生成|ai绘画|ai繪圖|ai作画|"
    r"aigirl|stablediffusion|novelai|midjourney|nijijourney)(?![0-9a-z_])",
    re.I,
)


URL_RE = re.compile(r"https?://\S+")


TITLE_CHARS = 40


# 每个图集、视频被过滤或下载失败时最多换这么多次
TRIES = 3


def title_of(feed: Feed, post: Post) -> str:
    """帖子文字的第一行（去掉链接），太长时截断；没有文字时用订阅名和日期。"""
    for line in post.text.splitlines():
        line = " ".join(URL_RE.sub("", line).split())
        if line:
            return line if len(line) <= TITLE_CHARS else line[:TITLE_CHARS] + "…"
    return f"{feed.name} {post.date or post.id}".strip()


class FeedSource(Source):
    """订阅的图源和视频源共用的抽取：从订阅的帖子里挑符合条件的，近期抽过的放到最后用。"""

    def __init__(self, feed: Feed, cache, content, opts, recent: Recent):
        super().__init__(cache, content, opts)
        self.feed = feed
        self.key = feed.key
        self.name = feed.name
        self.style = feed.conf.style
        self.ratings = frozenset({feed.conf.rating})
        self.intro = feed.intro
        self.link_re = feed.link_re
        self.recent = recent

    def named(self, keywords: list[str]) -> bool:
        positive, _ = split_terms(keywords)
        return any(self.feed.is_name(word) for word in positive)

    def accepts(self, ctx: DrawContext) -> bool:
        """没指名订阅时只参与不带要搜的关键词、不随机角色的抽取（只带排除词可以）。"""
        positive, _ = split_terms(ctx.req.keywords)
        return super().accepts(ctx) and not positive and not ctx.req.random_character

    def _usable(self, post: Post) -> bool:
        raise NotImplementedError

    async def _album(
        self, post: Post, ctx: DrawContext, used_urls: set[str]
    ) -> Album | None:
        raise NotImplementedError

    def _words(self, ctx: DrawContext) -> tuple[list[str], list[str]]:
        """(帖子文字里要有的词, 不能有的词)，都转成小写；指名订阅的词不算。"""
        positive, negative = split_terms(ctx.req.keywords)
        words = [w.lower() for w in positive if not self.feed.is_name(w)]
        return words, [w.lower() for w in negative]

    def reject(
        self, post: Post, words: list[str] = (), excludes: list[str] = ()
    ) -> str | None:
        """帖子不能用的原因（关键词、黑名单、低龄词、AI、点赞数），可以用时返回 None。

        擦边订阅不要平台标了敏感内容的帖子（X 上露点的图基本都标了；擦边博主的帖子多数没标）。
        """
        if self.feed.conf.rating == SENSITIVE and post.sensitive:
            return "标了敏感内容"
        low = post.text.lower()
        missing = next((w for w in words if w not in low), None)
        if missing:
            return f"不含关键词 {missing}"
        hit = next((w for w in excludes if w in low), None)
        if hit:
            return f"含排除词 {hit}"
        reason = self.content.text_reason([post.text])
        if reason:
            return reason
        term = young_word([post.text])
        if term:
            return f"文字命中 {term}"
        if self.content.block_ai and AI_TAG_RE.search(post.text):
            return "标了 AI 生成"
        if self.feed.conf.min_likes and (post.likes or 0) < self.feed.conf.min_likes:
            return "点赞太少"
        return None

    def details(self, post: Post) -> list[str]:
        lines = []
        if post.date:
            lines.append(f"发布：{post.date}")
        if post.likes is not None:
            lines.append(f"点赞：{post.likes}")
        lines.append(f"链接：{self.feed.link(post)}")
        return lines

    def max_fetches(self, n: int) -> int:
        return 1 if self.feed.whole else n + 4

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        why = self.rating_reason(self.feed.conf.rating, ctx)
        if why:
            return [], [f"{self.name}：{why}"]
        words, excludes = self._words(ctx)
        limit = self.max_fetches(n)
        seen: set[str] = set()
        used_urls: set[str] = set()
        queue: list[Post] = []
        backup: list[Post] = []  # 近期抽过的，没有别的可抽时再用
        fetches = 0
        lock = asyncio.Lock()

        async def pick() -> Post | None:
            nonlocal fetches
            async with lock:
                while True:
                    while queue:
                        post = queue.pop()
                        if post.id in seen:
                            continue
                        seen.add(post.id)
                        post = await self.feed.resolve(post)
                        if post is None or not self._usable(post):
                            continue
                        reason = self.reject(post, words, excludes)
                        if reason:
                            logger.debug(
                                f"[random_pic] 跳过 {self.name} {post.id}: {reason}"
                            )
                            continue
                        key = f"{self.key}:{post.id}"
                        if key in self.recent:
                            backup.append(post)
                            continue
                        self.recent.add(key)
                        return post
                    if fetches >= limit:
                        if not backup:
                            return None
                        post = backup.pop(random.randrange(len(backup)))
                        self.recent.add(f"{self.key}:{post.id}")
                        return post
                    fetches += 1
                    batch = await self.feed.sample()
                    if self.feed.per_batch:
                        batch = batch[: self.feed.per_batch]
                    queue.extend(batch)

        async def attempt() -> Album | None:
            post = await pick()
            return await self._album(post, ctx, used_urls) if post else None

        albums, errors = await self.collect(n, TRIES, attempt)
        if not albums and not seen:
            errors.append(f"{self.name} 没有抓到帖子")
        elif len(albums) < n and fetches >= limit:
            errors.append(f"{self.name} 没有更多符合条件的帖子")
        return albums, errors
