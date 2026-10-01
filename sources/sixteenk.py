"""16K（16k.club）三次元图源：服务器端随机，没有标签和分级。

站点前端调用一个没有文档的 JSON 接口 api.php：
- type=index&size=N&p=页码：最新帖子列表；
- type=post&id=N：帖子的标题、全部图片，以及 rand（服务器随机挑的另一个帖子 id，
  同一个 id 每次请求都不一样）。已删除的帖子返回 {}。

随机抽取就是沿着 rand 走：每次请求帖子时记下响应里的 rand 作为下一次的帖子，
所以连抽时每个帖子只需要一次请求。实测 rand 覆盖全部帖子，没有指向已删除的帖子。

站点没有关键词搜索（首页的搜索框跳转到外部的番号站），带关键词时不用这个图源。
"""

import asyncio
import json

import aiohttp

from astrbot.api import logger

from ..filters import ContentFilter
from ..models import REAL, Album, DrawOptions
from ..net import HttpClient, HttpError, ImageCache
from ..util import fill, pick_pages
from . import DrawContext

API_URL = "https://16k.club/api.php"
POST_URL = "https://16k.club/post/{}/"
# 每个图集最多看这么多个帖子（全是视频、命中黑名单或下载失败时换下一个）
POSTS_PER_ALBUM = 4
# 单次请求失败（偶发 TLS 握手中断）时的重试次数
REQUEST_RETRIES = 2


class SixteenKError(Exception):
    pass


def post_images(data: dict) -> list[str]:
    """帖子里的图片地址，跳过视频。"""
    return [
        str(im["path"])
        for im in data.get("images") or []
        if im.get("path") and not str(im["path"]).lower().endswith(".mp4")
    ]


class SixteenK:
    name = "16K"

    def __init__(
        self,
        http: HttpClient,
        cache: ImageCache,
        content: ContentFilter,
        opts: DrawOptions,
    ):
        self.http = http
        self.cache = cache
        self.content = content
        self.opts = opts
        self._next: int | None = None
        # rand 链只能一个接一个地走，并发抽取时只并发下载
        self._chain = asyncio.Lock()

    def accepts(self, ctx: DrawContext) -> bool:
        """不能搜索、没有分级：只用于不带关键词的三次元，且这次请求允许未分级的图源。"""
        req = ctx.req
        return (
            req.style == REAL
            and not req.keywords
            and not req.random_character
            and ctx.allow_unrated
        )

    async def _api(self, **params) -> dict:
        for attempt in range(REQUEST_RETRIES + 1):
            try:
                text = await self.http.get_text(
                    API_URL, params={k: str(v) for k, v in params.items()}
                )
                data = json.loads(text)
                return data if isinstance(data, dict) else {}
            except (HttpError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == REQUEST_RETRIES:
                    raise SixteenKError(f"16K 请求失败：{e!r}") from e
                logger.info(f"[random_pic] 16K 请求失败，重试: {e!r}")
            except ValueError as e:
                raise SixteenKError("16K 返回的不是 JSON") from e
        raise AssertionError("unreachable")

    async def _seed(self) -> int:
        """第一次抽取时没有 rand 可用，从最新的帖子出发。"""
        data = await self._api(type="index", size=1, p=1)
        posts = data.get("list") or []
        if not posts:
            raise SixteenKError("16K 帖子列表为空")
        return int(posts[0]["id"])

    async def random_post(self) -> tuple[int, dict]:
        """返回 (帖子 id, 帖子数据)。帖子可能已删除（数据为空）。"""
        async with self._chain:
            if self._next is None:
                data = await self._api(type="post", id=await self._seed())
                if not data.get("rand"):
                    raise SixteenKError("16K 接口没有返回随机帖子")
                self._next = int(data["rand"])
            pid = self._next
            data = await self._api(type="post", id=pid)
            # 已删除的帖子没有 rand，下次重新从最新帖子出发
            self._next = int(data["rand"]) if data.get("rand") else None
            return pid, data

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        """抽 n 个帖子，每个帖子取至多 per_album 张。帖子 API 依次请求，图片并发下载。"""
        albums: list[Album] = []
        errors: list[str] = []
        seen: set[int] = set()
        skipped = 0
        semaphore = asyncio.Semaphore(self.opts.concurrency)

        async def attempt() -> Album | None:
            nonlocal skipped
            pid, data = await self.random_post()
            if pid in seen:
                return None
            seen.add(pid)
            reason = self._reject(data)
            if reason:
                logger.info(f"[random_pic] 丢弃 16K 帖子 {pid}: {reason}")
                skipped += 1
                return None
            urls = post_images(data)
            indices = pick_pages(
                len(urls), ctx.req.per_album, from_start=self.opts.from_start
            )

            async def download(index: int):
                async with semaphore:
                    path = await self.cache.download(urls[index])
                return (index + 1, path) if path else None

            pictures = [p for p in await asyncio.gather(*map(download, indices)) if p]
            if not pictures:
                skipped += 1
                return None
            post_url = POST_URL.format(pid)
            return Album(
                source=self.name,
                title=str(data.get("title") or "").strip(),
                total=len(urls),
                pictures=sorted(pictures),
                details=[f"帖子：{post_url}"],
            )

        try:
            await fill(albums, n, POSTS_PER_ALBUM * n, self.opts.concurrency, attempt)
        except SixteenKError as e:
            logger.warning(f"[random_pic] {e}")
            errors.append(str(e))
        if skipped:
            errors.append(f"{skipped} 个 16K 帖子被过滤或下载失败")
        return albums, errors

    def _reject(self, data: dict) -> str | None:
        if not data:
            return "帖子已删除"
        if not post_images(data):
            return "只有视频"
        # 没有标签，只能用标题和简介过滤未成年内容
        return self.content.text_reason(
            [data.get("title") or "", data.get("content") or ""]
        )
