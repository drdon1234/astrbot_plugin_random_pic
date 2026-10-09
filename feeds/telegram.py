"""Telegram 公开频道：读频道的网页预览，不用登录。

网页预览（2026-10 实测）：
- GET t.me/s/<频道>：最新的约 20 条消息；?before=<id> 是 id 小于它的约 20 条，所以随机取一个 id
  就能抽到频道历史里任意一段，不用从头翻。比最早的消息还早时返回空列表（页面照常有 tgme_channel_history）；
- 没有开放网页预览的频道、不存在的频道和用户会跳到 t.me/<名字> 的联系页，没有 tgme_channel_history；
- 每条消息是带 data-post="<频道>/<id>" 的块。相册（一组图片、视频）是一个块，id 是第一项的；
  图片是 tgme_widget_message_photo_wrap 的 background-image（cdn*.telesco.pe 的 jpg，长边约 800~1280，
  没有防盗链）；视频是 tgme_widget_message_video_player 里的 <video src>（带 token 的 mp4，有时效，
  抓到后马上下载），太大的视频没有 src、写着「Media is too big」；时长在 message_video_duration 里，
  GIF 动图也是 <video>，但没有时长；
- GET t.me/<频道>/<id>?embed=1：单条消息（相册里任意一项的 id 都给出整个相册），/pdf 用。
"""

import html
import random
import re
from datetime import datetime

from ..net import RateLimiter
from ..settings import Feed as FeedConf
from ..sources.base import SourceError
from ..util import TTLCache
from .base import Clip, Feed, Post

BASE = "https://t.me"
# 频道的最新消息 id 缓存这么久
LATEST_TTL = 1800
# 随机的一页落在最早的消息之前（空页）时最多重试几次
EMPTY_RETRIES = 3
POST_RE = re.compile(r'data-post="([^"/]+)/(\d+)"')
PHOTO_RE = re.compile(
    r'<a class="tgme_widget_message_photo_wrap[^>]*?background-image:url\(\'([^\']+)\'\)'
)
VIDEO_RE = re.compile(
    r'<a class="tgme_widget_message_video_player[^"]*"(.*?)</a>', re.S
)
VIDEO_SRC_RE = re.compile(r'<video[^>]*\ssrc="([^"]+)"')
DURATION_RE = re.compile(r'message_video_duration[^>]*>\s*([\d:]+)\s*<')
TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
# 回复的消息在块里会带上被回复消息的文字，先去掉
REPLY_RE = re.compile(r'<a class="tgme_widget_message_reply.*?</a>', re.S)
TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
TAG_RE = re.compile(r"<[^>]+>")
BR_RE = re.compile(r"<br\s*/?>", re.I)
HISTORY_MARK = "tgme_channel_history"


def seconds(text: str) -> float | None:
    """「0:29」「1:02:03」→ 秒数。"""
    try:
        parts = [int(p) for p in text.split(":")]
    except ValueError:
        return None
    total = 0
    for part in parts:
        total = total * 60 + part
    return float(total)


def plain_text(fragment: str) -> str:
    return html.unescape(TAG_RE.sub("", BR_RE.sub("\n", fragment))).strip()


def timestamp(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def parse_posts(page: str, channel: str) -> list[Post]:
    """页面里这个频道的消息，按 id 从小到大。"""
    marks = [
        m for m in POST_RE.finditer(page) if m.group(1).lower() == channel.lower()
    ]
    posts = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(page)
        block = REPLY_RE.sub("", page[mark.end() : end])
        photos = [html.unescape(url) for url in PHOTO_RE.findall(block)]
        clips = []
        for player in VIDEO_RE.findall(block):
            src = VIDEO_SRC_RE.search(player)
            duration = DURATION_RE.search(player)
            # 没有地址的太大，网页预览不提供；没有时长的是 GIF 动图
            if not src or not duration:
                continue
            clips.append(
                Clip([(html.unescape(src.group(1)), 0, 0)], seconds(duration.group(1)))
            )
        texts = TEXT_RE.findall(block)
        posted = TIME_RE.search(block)
        posts.append(
            Post(
                id=mark.group(2),
                text=plain_text(texts[0]) if texts else "",
                photos=list(dict.fromkeys(photos)),
                clips=clips,
                time=timestamp(posted.group(1)) if posted else None,
            )
        )
    return posts


class TelegramFeed(Feed):
    platform_name = "Telegram"
    intro = "订阅的 Telegram 频道"
    per_batch = 4

    def __init__(self, conf: FeedConf, http, limiter: RateLimiter):
        super().__init__(conf)
        self.http = http
        self.limiter = limiter
        self.channel = conf.account
        self.link_re = re.compile(
            rf"t\.me/(?:s/)?{re.escape(self.channel)}/(\d+)", re.I
        )
        self._latest = TTLCache(LATEST_TTL, 1)
        # 已知比最早的消息还早的 id：随机的 id 只在它之后取
        self._floor = 0

    def link(self, post: Post) -> str:
        return f"{BASE}/{self.channel}/{post.id}"

    async def _get(self, url: str) -> str:
        await self.limiter.wait()
        return await self.http.get_text(url)

    async def page(self, before: int | None = None) -> list[Post]:
        url = f"{BASE}/s/{self.channel}" + (f"?before={before}" if before else "")
        text = await self._get(url)
        if HISTORY_MARK not in text:
            raise SourceError(f"{self.name} 不是公开频道或没有开放网页预览")
        return parse_posts(text, self.channel)

    async def latest(self) -> int:
        """最新一条消息的 id。"""

        async def load() -> int:
            posts = await self.page()
            if not posts:
                raise SourceError(f"{self.name} 没有消息")
            return max(int(p.id) for p in posts)

        return await self._latest.load("latest", load)

    async def sample(self) -> list[Post]:
        latest = await self.latest()
        for _ in range(EMPTY_RETRIES):
            before = random.randint(min(self._floor, latest) + 1, latest + 1)
            posts = await self.page(before)
            if posts:
                random.shuffle(posts)
                return posts
            # 这个 id 之前没有消息了
            self._floor = max(self._floor, before - 1)
        return []

    async def post(self, post_id: str) -> Post | None:
        text = await self._get(f"{BASE}/{self.channel}/{post_id}?embed=1")
        posts = parse_posts(text, self.channel)
        return posts[0] if posts else None

    async def warm_up(self):
        await self.latest()

