"""X（推特）博主：经 fxtwitter 的公开接口读博主的媒体页，不用登录。

X 官方接口要付费，网页版不登录看不到时间线，嵌入用的 syndication 接口匿名请求直接 429。
fxtwitter（api.fxtwitter.com，开源的推文嵌入服务）的接口匿名可用（2026-10 实测）：
- GET /2/profile/<用户名>/media[?cursor=]：博主带图或视频的推文，每页 10~17 条，从新到旧，
  cursor.bottom 翻下一页；一个 cosplayer 翻 40 页（约 1 分钟）回到了 15 个月前，没遇到限流；
  用户不存在时 404。R18 画师的推文也能取到（都标了 possibly_sensitive，擦边画师、coser 的基本没标，
  擦边订阅据此排除标了的）。条目里有 author、reposted_by、likes、created_timestamp、possibly_sensitive、
  media.photos（pbs.twimg.com 原图 name=orig）、media.videos（formats 里有各清晰度的 mp4 和码率，
  type 为 gif 的是循环动图）；
- GET /2/status/<id>：单条推文，/pdf 用。
媒体页只能从新往旧一页页翻，所以每个博主维护一份推文索引（存到插件数据目录，重启后沿用）：
第一次用时先取第一页，其余的在后台每隔一会儿往前翻，最多保留 MAX_POSTS 条；每 REFRESH 秒从第一页起更新。
"""

import asyncio
import json
import random
import re
import time
from pathlib import Path

from astrbot.api import logger

from ...config.settings import Feed as FeedConf
from ...download.http import NETWORK_ERRORS, HttpError, RateLimiter
from ..base import SourceError
from .base import Feed
from .models import Clip, Post

API = "https://api.fxtwitter.com/2"
# 每个博主最多保留这么多条推文（约 70 页）
MAX_POSTS = 1000
REFRESH = 6 * 3600
# 更新时最多翻这么多页（遇到已有的推文就停）
REFRESH_PAGES = 5
# 后台往前翻失败后隔这么久再试
RETRY_AFTER = 600
SIZE_RE = re.compile(r"/(\d+)x(\d+)/")
LINK_HOSTS = r"(?:x|twitter|fxtwitter|vxtwitter|fixupx|fixvx)\.com"


def parse_clip(video: dict) -> Clip | None:
    """fxtwitter 的一个视频 → Clip，循环动图（gif）和没有 mp4 的返回 None。"""
    if video.get("type") != "video":
        return None
    variants = []
    for fmt in video.get("formats") or []:
        url = fmt.get("url")
        if fmt.get("container") != "mp4" or not url:
            continue
        size = SIZE_RE.search(url)
        side = max(int(size.group(1)), int(size.group(2))) if size else 0
        variants.append((str(url), side, int(fmt.get("bitrate") or 0)))
    if not variants and video.get("url"):
        side = max(int(video.get("width") or 0), int(video.get("height") or 0))
        variants.append((str(video["url"]), side, 0))
    if not variants:
        return None
    duration = video.get("duration")
    return Clip(variants, float(duration) if duration else None)


def parse_status(status: dict, account: str) -> Post | None:
    """一条推文 → Post；转推、别人的推文和没有图片、视频的返回 None。"""
    author = (status.get("author") or {}).get("screen_name") or ""
    if status.get("reposted_by") or author.lower() != account.lower():
        return None
    media = status.get("media") or {}
    photos = [str(p["url"]) for p in media.get("photos") or [] if p.get("url")]
    clips = [c for c in map(parse_clip, media.get("videos") or []) if c]
    if not photos and not clips:
        return None
    return Post(
        id=str(status.get("id")),
        text=str(status.get("text") or ""),
        photos=photos,
        clips=clips,
        time=status.get("created_timestamp"),
        likes=int(status.get("likes") or 0),
        sensitive=bool(status.get("possibly_sensitive")),
    )


class XFeed(Feed):
    platform_name = "X"
    intro = "订阅的 X 博主"
    whole = True

    def __init__(
        self, conf: FeedConf, http, limiter: RateLimiter, store: Path | None = None
    ):
        """store：保存推文索引的文件（None 时只在内存里）。"""
        super().__init__(conf)
        self.http = http
        self.limiter = limiter
        self.store = store
        self.account = conf.account
        self.link_re = re.compile(
            rf"{LINK_HOSTS}/{re.escape(self.account)}/status/(\d+)", re.I
        )
        self._posts: list[Post] = []  # 从新到旧
        self._cursor: str | None = None  # 往前翻的下一页
        self._done = False  # 已经翻到头
        self._refreshed = 0.0
        self._loaded = False
        self._retry_at = 0.0
        self._lock = asyncio.Lock()
        self._backfill: asyncio.Task | None = None

    def link(self, post: Post) -> str:
        return f"https://x.com/{self.account}/status/{post.id}"

    def photo_urls(self, url: str) -> list[str]:
        """先下原图，失败（例如超过单张大小上限）时改下 large（长边 2048）。"""
        return list(dict.fromkeys([url, url.replace("name=orig", "name=large")]))

    # ---- 接口 ----

    async def _api(self, path: str, params: dict | None = None) -> dict:
        await self.limiter.wait()
        try:
            text = await self.http.get_text(f"{API}{path}", params=params)
        except HttpError as e:
            if e.status == 404:
                raise SourceError(f"{self.name} 不存在或不可见") from e
            raise
        data = json.loads(text)
        return data if isinstance(data, dict) else {}

    async def _page(self, cursor: str | None) -> tuple[list[Post], str | None, int]:
        """媒体页的一页：(博主自己的推文, 下一页的 cursor, 这页的条目数)。"""
        data = await self._api(
            f"/profile/{self.account}/media", {"cursor": cursor} if cursor else None
        )
        results = [r for r in data.get("results") or [] if isinstance(r, dict)]
        posts = [p for p in (parse_status(r, self.account) for r in results) if p]
        bottom = (data.get("cursor") or {}).get("bottom")
        return posts, bottom or None, len(results)

    # ---- 索引 ----

    def _load(self):
        self._loaded = True
        if self.store is None:
            return
        try:
            data = json.loads(self.store.read_text(encoding="utf-8"))
            self._posts = [Post.from_dict(p) for p in data.get("posts") or []]
            self._cursor = data.get("cursor")
            self._done = bool(data.get("done"))
            self._refreshed = float(data.get("refreshed") or 0)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as e:
            logger.warning(f"[random_pic] 读取 {self.name} 的推文索引失败: {e!r}")

    def _save(self):
        if self.store is None:
            return
        data = {
            "posts": [p.to_dict() for p in self._posts],
            "cursor": self._cursor,
            "done": self._done,
            "refreshed": self._refreshed,
        }
        try:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.store)
        except OSError as e:
            logger.warning(f"[random_pic] 保存 {self.name} 的推文索引失败: {e!r}")

    async def _refresh(self):
        """从第一页起取新的推文，遇到已有的就停；索引是空的时只取第一页，其余交给后台往前翻。"""
        known = {p.id for p in self._posts}
        fresh: list[Post] = []
        cursor = None
        for _ in range(REFRESH_PAGES if known else 1):
            posts, bottom, count = await self._page(cursor)
            new = [p for p in posts if p.id not in known]
            fresh.extend(new)
            if not known:
                self._cursor, self._done = bottom, not (bottom and count)
            if len(new) < len(posts) or not bottom or not count:
                break
            cursor = bottom
        self._posts = fresh + self._posts
        self._refreshed = time.time()
        self._save()
        if fresh:
            logger.info(f"[random_pic] {self.name} 新增 {len(fresh)} 条推文")

    async def _fill(self):
        """后台往前翻，直到翻到头或攒够 MAX_POSTS 条。"""
        try:
            while not self._done and self._cursor and len(self._posts) < MAX_POSTS:
                posts, bottom, count = await self._page(self._cursor)
                async with self._lock:
                    known = {p.id for p in self._posts}
                    new = [p for p in posts if p.id not in known]
                    self._posts.extend(new)
                    self._cursor = bottom
                    # 翻到头：没有下一页、空页，或整页都是已有的（cursor 绕回来了）
                    self._done = not (bottom and count) or (bool(posts) and not new)
                    self._save()
            logger.info(f"[random_pic] {self.name} 推文索引共 {len(self._posts)} 条")
        except asyncio.CancelledError:
            raise
        except (*NETWORK_ERRORS, SourceError, ValueError) as e:
            logger.warning(f"[random_pic] {self.name} 往前翻推文失败，稍后再试: {e!r}")
            self._retry_at = time.time() + RETRY_AFTER

    def _start_fill(self):
        running = self._backfill is not None and not self._backfill.done()
        if (
            running
            or self._done
            or not self._cursor
            or len(self._posts) >= MAX_POSTS
            or time.time() < self._retry_at
        ):
            return
        self._backfill = asyncio.create_task(self._fill())

    async def posts(self) -> list[Post]:
        """推文索引（从新到旧），过期时先更新；更新失败时沿用已有的。"""
        async with self._lock:
            if not self._loaded:
                self._load()
            if not self._posts or time.time() - self._refreshed > REFRESH:
                try:
                    await self._refresh()
                except (*NETWORK_ERRORS, ValueError) as e:
                    if not self._posts:
                        raise
                    logger.warning(f"[random_pic] {self.name} 更新推文失败: {e!r}")
        self._start_fill()
        return self._posts

    # ---- 抽取 ----

    async def sample(self) -> list[Post]:
        posts = await self.posts()
        return random.sample(posts, len(posts))

    async def post(self, post_id: str) -> Post | None:
        for post in self._posts:
            if post.id == post_id:
                return post
        try:
            data = await self._api(f"/status/{post_id}")
        except SourceError:
            return None
        status = data.get("status")
        return parse_status(status, self.account) if isinstance(status, dict) else None

    async def warm_up(self):
        await self.posts()

    async def close(self):
        if self._backfill is not None:
            if not self._backfill.done():
                self._backfill.cancel()
            await asyncio.gather(self._backfill, return_exceptions=True)
