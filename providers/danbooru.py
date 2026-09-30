"""Danbooru：二次元，rating:g/s/e 分别对应全年龄/擦边/R18。

标签额度（已在 Danbooru 实测核实）：
- 匿名与普通 Member 最多 2 个计数标签；rating: 是免费元标签，不计数；
- order:random 与 random=true（会被改写为 random:1）都计入额度，这里用 random=true；
- source: 计入额度。
因此额度 = random 1 个 + 推特 source 1 个（可选）+ 用户标签，剩余额度才追加 -loli -shota。
"""

import random
import re

import aiohttp

from astrbot.api import logger

from ..models import ANIME, EXPLICIT, GENERAL, SENSITIVE, ImageItem, PicRequest
from ..net import RateLimiter
from .base import Provider

API = "https://danbooru.donmai.us/posts.json"
POST_URL = "https://danbooru.donmai.us/posts/{id}"
FXTWITTER_API = "https://api.fxtwitter.com/status/{id}"
RATING_MAP = {"g": GENERAL, "s": SENSITIVE, "q": EXPLICIT, "e": EXPLICIT}
QUERY_EXCLUDES = ("-loli", "-shota")
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}
TWEET_RE = re.compile(r"(?:twitter|x)\.com/[^/]+/status(?:es)?/(\d+)", re.I)

# Danbooru 读取限速 10 次/秒
_limiter = RateLimiter(0.1)


class DanbooruProvider(Provider):
    name = "danbooru"
    display = "Danbooru"

    def build_combos(self):
        explicit = "rating:q,e" if self.conf.get("include_questionable") else "rating:e"
        return {
            (ANIME, GENERAL): "rating:g",
            (ANIME, SENSITIVE): "rating:s",
            (ANIME, EXPLICIT): explicit,
        }

    @property
    def tag_limit(self) -> int:
        return max(1, int(self.conf.get("tag_limit", 2)))

    def unsupported(self, req: PicRequest) -> str | None:
        used = 1 + (1 if req.twitter else 0) + len(req.tags)
        if used > self.tag_limit:
            return f"标签数超出 Danbooru 额度（{self.tag_limit}）"
        return None

    def build_tags(self, req: PicRequest) -> str:
        tags = [self.combos[(req.style, req.rating)], *req.tags]
        if req.twitter:
            tags.append(random.choice(("source:*x.com*", "source:*twitter.com*")))
        remaining = self.tag_limit - 1 - (1 if req.twitter else 0) - len(req.tags)
        tags += list(QUERY_EXCLUDES[: max(0, remaining)])
        return " ".join(tags)

    async def fetch(self, req: PicRequest, n: int) -> list[ImageItem]:
        auth = None
        if self.conf.get("login") and self.conf.get("api_key"):
            auth = aiohttp.BasicAuth(self.conf["login"], self.conf["api_key"])
        params = {"tags": self.build_tags(req), "limit": str(n), "random": "true"}
        await _limiter.wait()
        data = await self.http.get_json(API, params=params, auth=auth, proxy=self.proxy)
        if isinstance(data, dict):
            raise ValueError(data.get("message") or "Danbooru 返回错误")
        items = [item for post in data if (item := self.parse(post))]
        if req.twitter and self.conf.get("fxtwitter", True):
            for item in items:
                await self.enrich_tweet(item)
        return items

    def parse(self, post: dict) -> ImageItem | None:
        file_url = post.get("file_url")
        rating = RATING_MAP.get(post.get("rating", ""))
        if not file_url or not rating or post.get("file_ext") not in IMAGE_EXTS:
            return None
        source = post.get("source") or ""
        if post.get("pixiv_id"):
            source = f"https://www.pixiv.net/artworks/{post['pixiv_id']}"
        large = post.get("large_file_url") or ""
        return ImageItem(
            image_url=file_url,
            alt_url=large if large != file_url else "",
            rating=rating,
            style=ANIME,
            provider=self.display,
            author=post.get("tag_string_artist", "").replace(" ", ", "),
            source_url=source,
            post_url=POST_URL.format(id=post.get("id")),
            tags=(post.get("tag_string") or "").split(),
        )

    async def enrich_tweet(self, item: ImageItem):
        """用 fxtwitter 补全原推作者和正文，失败时忽略。"""
        match = TWEET_RE.search(item.source_url)
        if not match:
            return
        try:
            data = await self.http.get_json(
                FXTWITTER_API.format(id=match.group(1)), proxy=self.proxy
            )
            tweet = data.get("tweet") or {}
            author = tweet.get("author") or {}
            if author.get("screen_name"):
                item.author = f"{author.get('name', '')} (@{author['screen_name']})"
            if tweet.get("text"):
                item.title = " ".join(tweet["text"].split())[:200]
        except Exception as e:
            logger.debug(f"[random_pic] fxtwitter 补全失败: {e!r}")
