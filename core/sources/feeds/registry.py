"""订阅：X 博主和 Telegram 公开频道的图片、视频。

FeedSet 按配置装配各订阅，给出要加进抽图（SourceSet）和抽视频（VideoSet）的图源和比例。
"""

from pathlib import Path

from astrbot.api import logger

from ...config.settings import TELEGRAM, Feeds
from ...download.http import HttpClient, RateLimiter
from ...download.images import ImageCache
from ...download.videos import VideoFiles
from ...models import DrawOptions
from ...policy.content import ContentFilter
from ...storage.recent import Recent
from ..base import Source
from .base import Feed
from .images import FeedImageSource
from .telegram import TelegramFeed
from .videos import FeedVideoSource
from .x import XFeed

# 同一平台两次请求至少间隔这么多秒（各订阅共用）
REQUEST_INTERVAL = 0.5


class FeedSet:
    def __init__(
        self,
        conf: Feeds,
        http: HttpClient,
        cache: ImageCache,
        content: ContentFilter,
        opts: DrawOptions,
        data_dir: Path,
        files: VideoFiles,
        video_recent: Recent,
    ):
        """data_dir 下保存 X 博主的推文索引（feeds/）和最近抽过的帖子；视频和其他视频源共用
        下载目录（files）与最近抽过的视频（video_recent）。"""
        x_limiter = RateLimiter(REQUEST_INTERVAL)
        tg_limiter = RateLimiter(REQUEST_INTERVAL)
        recent = Recent(data_dir / "feed_recent.json")
        self.feeds: list[Feed] = []
        self.images: list[tuple[Source, int]] = []
        self.videos: list[tuple[Source, int]] = []
        for sub in conf.subscriptions:
            if sub.platform == TELEGRAM:
                feed: Feed = TelegramFeed(sub, http, tg_limiter)
            else:
                feed = XFeed(
                    sub, http, x_limiter, data_dir / "feeds" / f"{sub.key}.json"
                )
            self.feeds.append(feed)
            if sub.images:
                source = FeedImageSource(feed, cache, content, opts, recent)
                self.images.append((source, sub.weight))
            if sub.videos:
                source = FeedVideoSource(
                    feed, cache, content, opts, video_recent, files
                )
                self.videos.append((source, sub.weight))

    def named(self, tokens: list[str]) -> Feed | None:
        """参数里指名的第一个订阅。"""
        for token in tokens:
            for feed in self.feeds:
                if feed.is_name(token):
                    return feed
        return None

    async def warm_up(self):
        """后台先取各订阅的帖子（X 博主的推文索引、频道的最新消息），第一次抽时不用等。"""
        for feed in self.feeds:
            try:
                await feed.warm_up()
            except Exception as e:
                logger.warning(f"[random_pic] 订阅 {feed.name} 预热失败: {e!r}")

    async def close(self):
        for feed in self.feeds:
            await feed.close()
