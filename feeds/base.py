"""订阅源共用的部分：帖子、订阅（Feed），以及把订阅接进抽图、抽视频的图源。

一个订阅同时是一个图源（帖子里的图片，可跨帖子抽齐一个图集）和一个视频源（帖子里的视频，一个视频是一个
只有一页的图集），两者共用订阅抓到的帖子。帖子没有可靠的分级，全部按配置的风格、分级抽取，
所以每个图源只有一种风格、一种分级；过滤只能看帖子的文字（正文、话题标签）。

关键词写订阅的账号或显示名称时只抽这个订阅（Drawer 见 Source.named），其余的关键词要出现在帖子文字里；
不指名订阅的关键词搜索不交给订阅（帖子没有标签，搜不准）。
"""

import asyncio
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import logger

from ..models import SENSITIVE, Album, DrawContext, Work, WorkRef
from ..net import NETWORK_ERRORS
from ..settings import Feed as FeedConf
from ..sources.base import Source, SourceError, note, split_terms
from ..util import MIN_SPARE_PAGES, fetch_pages, fill, page_order
from ..videos.base import Recent, VideoFiles, young_word

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
# 视频按清晰度挑选时长边不超过这么多像素的优先（更大的发 QQ 也只是被压缩）
MAX_VIDEO_SIDE = 1920


@dataclass
class Clip:
    """帖子里的一个视频：variants 是可下载的 mp4 [地址, 长边像素, 码率]（未知为 0）。"""

    variants: list[tuple[str, int, int]]
    duration: float | None = None

    def order(self, max_bytes: int | None) -> list[str]:
        """下载时依次尝试的地址：长边不超过 MAX_VIDEO_SIDE、估计大小不超过上限的优先，其中码率高的优先。"""

        def too_big(bitrate: int) -> bool:
            return bool(
                max_bytes and bitrate and self.duration
                and bitrate * self.duration / 8 > max_bytes
            )

        ranked = sorted(
            self.variants,
            key=lambda v: (v[1] > MAX_VIDEO_SIDE, too_big(v[2]), -v[2]),
        )
        return list(dict.fromkeys(url for url, _, _ in ranked))


@dataclass
class Post:
    """一个帖子（一条推文、一条频道消息或一组相册）。"""

    id: str
    text: str = ""
    photos: list[str] = field(default_factory=list)
    clips: list[Clip] = field(default_factory=list)
    time: float | None = None  # 发布时间（Unix 秒）
    likes: int | None = None
    sensitive: bool = False  # 平台标成敏感内容（X 的 possibly_sensitive）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Post":
        clips = [
            Clip([tuple(v) for v in c.get("variants") or []], c.get("duration"))
            for c in data.get("clips") or []
        ]
        return cls(
            id=str(data["id"]),
            text=str(data.get("text") or ""),
            photos=[str(p) for p in data.get("photos") or []],
            clips=clips,
            time=data.get("time"),
            likes=data.get("likes"),
            sensitive=bool(data.get("sensitive")),
        )

    @property
    def date(self) -> str:
        if not self.time:
            return ""
        return datetime.fromtimestamp(self.time, timezone.utc).strftime("%Y-%m-%d")


class Feed:
    """一个订阅：抓帖子。子类实现 sample()、post()，可选 warm_up()、close()。"""

    platform_name: str  # 说明里的平台名
    intro: str  # 帮助里的一行介绍
    # sample() 一次就给出全部帖子（抽取时不用再翻），否则每次给一批随机的帖子
    whole = False
    # 每批随机帖子最多用几个（同一页的帖子发布时间相近，换着页抽）
    per_batch: int | None = None

    def __init__(self, conf: FeedConf):
        self.conf = conf
        self.key = conf.key
        self.name = conf.name or f"{self.platform_name} @{conf.account}"
        self.link_re: re.Pattern | None = None

    def is_name(self, word: str) -> bool:
        """关键词是不是在指名这个订阅（账号、显示名称或图源键，不分大小写，账号可以带 @）。"""
        word = word.strip().lower()
        names = {self.conf.account.lower(), f"@{self.conf.account.lower()}", self.key}
        if self.conf.name:
            names.add(self.conf.name.lower())
        return word in names

    def link(self, post: Post) -> str:
        raise NotImplementedError

    def photo_urls(self, url: str) -> list[str]:
        """下载一张图片时依次尝试的地址。"""
        return [url]

    async def sample(self) -> list[Post]:
        """一批随机顺序的帖子。"""
        raise NotImplementedError

    async def post(self, post_id: str) -> Post | None:
        """按 id 查一个帖子，不存在时返回 None。"""
        raise NotImplementedError

    async def warm_up(self):
        pass

    async def close(self):
        pass


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
                        if not self._usable(post):
                            continue
                        reason = self.reject(post, words, excludes)
                        if reason:
                            logger.debug(f"[random_pic] 跳过 {self.name} {post.id}: {reason}")
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


class FeedImageSource(FeedSource):
    """订阅里的图片：可保持单帖图集，也可跨帖抽齐所需张数。"""

    def __init__(self, feed: Feed, cache, content, opts, recent: Recent):
        super().__init__(feed, cache, content, opts, recent)
        self._inflight_posts: set[str] = set()

    def _usable(self, post: Post) -> bool:
        return bool(post.photos)

    async def _download(self, url: str, dest=None):
        for candidate in self.feed.photo_urls(url):
            path = await self.cache.download(candidate, dest)
            if path:
                return path
        return None

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        if not self.feed.conf.fill_album or ctx.req.whole:
            return await super().draw(ctx, n)
        why = self.rating_reason(self.feed.conf.rating, ctx)
        if why:
            return [], [f"{self.name}：{why}"]
        words, excludes = self._words(ctx)
        per_album = ctx.req.per_album
        limit = self.max_fetches(n * per_album)
        fetches = 0
        seen_posts: set[str] = set()
        attempted_urls: set[str] = set()
        used_posts: set[str] = set()
        # 同次抽取可以并发下载同帖的不同页，最后一页下载结束才释放该帖。
        pending: dict[str, int] = {}
        # 每项保存一个帖子的剩余候选页；选取时再检查其他请求的占用与近期记录。
        queue: list[tuple[Post, list[int]]] = []
        lock = asyncio.Lock()
        errors: list[str] = []

        async def load():
            nonlocal fetches
            fetches += 1
            batch = await self.feed.sample()
            if self.feed.per_batch:
                batch = batch[: self.feed.per_batch]
            for post in batch:
                if post.id in seen_posts:
                    continue
                seen_posts.add(post.id)
                if not self._usable(post):
                    continue
                reason = self.reject(post, words, excludes)
                if reason:
                    logger.debug(f"[random_pic] 跳过 {self.name} {post.id}: {reason}")
                    continue
                pages = page_order(len(post.photos), from_start=self.opts.from_start)
                queue.append((post, pages))

        async def pick() -> tuple[Post, int] | None:
            async with lock:
                while True:
                    fresh = backup = None
                    for index, (post, _) in enumerate(queue):
                        if post.id in self._inflight_posts and post.id not in pending:
                            continue
                        if post.id in used_posts or f"{self.key}:{post.id}" not in self.recent:
                            fresh = index
                            break
                        if backup is None:
                            backup = index
                    if fresh is None and fetches < limit:
                        await load()
                        continue
                    index = fresh if fresh is not None else backup
                    if index is None:
                        return None
                    post, pages = queue[index]
                    while pages:
                        page = pages.pop(0)
                        url = post.photos[page]
                        if url in attempted_urls:
                            continue
                        attempted_urls.add(url)
                        if not pages:
                            queue.pop(index)
                        elif not self.feed.conf.prefer_same_post:
                            queue.append(queue.pop(index))
                        self._inflight_posts.add(post.id)
                        pending[post.id] = pending.get(post.id, 0) + 1
                        return post, page
                    queue.pop(index)

        albums: list[Album] = []
        for _ in range(n):
            selected: list[tuple[int, Post, int, Path]] = []
            order = 0

            async def attempt():
                nonlocal order
                post = None
                try:
                    item = await pick()
                    if item is None:
                        return None
                    post, page = item
                    rank = order
                    order += 1
                    path = await self._download(post.photos[page])
                    if path is None:
                        return None
                    self.recent.add(f"{self.key}:{post.id}")
                    used_posts.add(post.id)
                    return rank, post, page, path
                except NETWORK_ERRORS as e:
                    logger.warning(f"[random_pic] {self.name} 请求失败: {e!r}")
                    note(errors, f"{self.name} 请求失败")
                    return None
                finally:
                    if post is not None:
                        pending[post.id] -= 1
                        if not pending[post.id]:
                            del pending[post.id]
                            self._inflight_posts.remove(post.id)

            attempts = per_album + max(per_album, MIN_SPARE_PAGES)
            try:
                await fill(selected, per_album, attempts, self.opts.concurrency, attempt)
            except SourceError as e:
                logger.warning(f"[random_pic] {e}")
                note(errors, str(e))
            if not selected:
                break
            selected.sort(key=lambda item: item[0])
            posts = list({post.id: post for _, post, _, _ in selected}.values())
            if len(posts) == 1:
                post = posts[0]
                album = Album(
                    source=self.name,
                    title=title_of(self.feed, post),
                    total=len(post.photos),
                    pictures=sorted((page + 1, path) for _, _, page, path in selected),
                    details=self.details(post),
                    work=WorkRef(self.key, post.id),
                    source_key=self.key,
                )
            else:
                album = Album(
                    source=self.name,
                    title=f"{self.name} · {len(posts)} 个帖子精选",
                    total=len({url for post in posts for url in post.photos}),
                    pictures=[(i, item[3]) for i, item in enumerate(selected, 1)],
                    details=[line for post in posts for line in self.details(post)],
                    source_key=self.key,
                )
            albums.append(album)
            if len(selected) < per_album:
                note(errors, f"{self.name} 没有更多符合条件的图片或图片下载失败")
        if len(albums) < n:
            note(errors, f"{self.name} 没有更多符合条件的帖子")
        return albums, errors

    async def _album(
        self, post: Post, ctx: DrawContext, used_urls: set[str]
    ) -> Album | None:
        photos = post.photos

        async def fetch(index: int):
            url = photos[index]
            if url in used_urls:
                return None
            used_urls.add(url)
            path = await self._download(url)
            return (index + 1, path) if path else None

        # 帖子只有几张图，不受「图集最少页数」限制，也不跳过开头
        pictures = await fetch_pages(
            len(photos),
            ctx.req.per_album,
            self.opts.concurrency,
            fetch,
            from_start=self.opts.from_start,
        )
        if not pictures:
            return None
        return Album(
            source=self.name,
            title=title_of(self.feed, post),
            total=len(photos),
            pictures=sorted(pictures),
            details=self.details(post),
            work=WorkRef(self.key, post.id),
            source_key=self.key,
        )

    async def work(self, ref: WorkRef) -> Work | None:
        post = await self.feed.post(ref.id)
        if post is None or not post.photos:
            return None
        return Work(
            ref=WorkRef(self.key, post.id),
            title=title_of(self.feed, post),
            pages=len(post.photos),
            rating=self.feed.conf.rating,
            blocked=self.reject(post),
            data=post.photos,
        )

    async def download_page(self, item, dest):
        return await self._download(item, dest)


class FeedVideoSource(FeedSource):
    """订阅里的视频：一个视频是一个只有一页的图集，帖子里有几个视频时随机取一个。"""

    def __init__(self, feed: Feed, cache, content, opts, recent: Recent, files: VideoFiles):
        super().__init__(feed, cache, content, opts, recent)
        self.files = files

    def _clips(self, post: Post) -> list[Clip]:
        return [
            c for c in post.clips if c.variants and self.files.reason(c.duration) is None
        ]

    def _usable(self, post: Post) -> bool:
        return bool(self._clips(post))

    async def _album(
        self, post: Post, ctx: DrawContext, used_urls: set[str]
    ) -> Album | None:
        clip = random.choice(self._clips(post))
        path = None
        for url in clip.order(self.files.max_bytes):
            path = await self.files.download(url)
            if path:
                break
        if path is None:
            return None
        return Album(
            source=self.name,
            title=title_of(self.feed, post),
            total=1,
            pictures=[(1, path)],
            details=self.details(post),
            work=WorkRef(self.key, post.id),
            duration=clip.duration,
            source_key=self.key,
        )

    async def work(self, ref: WorkRef) -> Work | None:
        return None
