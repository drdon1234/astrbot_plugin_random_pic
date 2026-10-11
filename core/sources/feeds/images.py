"""订阅图片：单帖图集、跨帖补齐和完整帖子的图片下载。"""

import asyncio
from pathlib import Path

from astrbot.api import logger

from ...download.http import NETWORK_ERRORS
from ...models import Album, DrawContext, Work, WorkRef
from ...storage.recent import Recent
from ...utils import MIN_SPARE_PAGES, fetch_pages, fill, page_order
from ..base import SourceError, note
from .base import Feed
from .models import Post
from .source import FeedSource, title_of


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
                        if (
                            post.id in used_posts
                            or f"{self.key}:{post.id}" not in self.recent
                        ):
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
                await fill(
                    selected, per_album, attempts, self.opts.concurrency, attempt
                )
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
