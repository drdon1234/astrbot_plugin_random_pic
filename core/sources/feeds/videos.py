"""订阅视频：挑选清晰度并将帖子视频转换为图集。"""

import random

from ...download.videos import VideoFiles
from ...models import Album, DrawContext, Work, WorkRef
from ...storage.recent import Recent
from .base import Feed
from .models import Clip, Post
from .source import FeedSource, title_of


class FeedVideoSource(FeedSource):
    """订阅里的视频：一个视频是一个只有一页的图集，帖子里有几个视频时随机取一个。"""

    def __init__(
        self, feed: Feed, cache, content, opts, recent: Recent, files: VideoFiles
    ):
        super().__init__(feed, cache, content, opts, recent)
        self.files = files

    def _clips(self, post: Post) -> list[Clip]:
        return [
            c
            for c in post.clips
            if c.variants and self.files.reason(c.duration) is None
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
