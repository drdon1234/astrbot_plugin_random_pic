"""图片下载、缓存清理与作品分页并发下载。"""

import asyncio
import time
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp
from astrbot.api import logger

from ..utils import run_thread
from .http import USER_AGENT, HttpClient, HttpError

# 图片缓存总大小、单张图片的上限（MB）：超出缓存时删除最旧的图片，超过单张上限的图片丢弃换一张
CACHE_MB = 200
MAX_IMAGE_MB = 10
# 这么多秒内下载的图片不清理：并发抽图时同一次抽卡的图片可能还没发出去
FRESH_SECONDS = 600
# 图片下载连接失败（例如代理重置连接）后等这么多秒重试一次
RETRY_DELAY = 1.0

IMAGE_EXTS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def sniff_ext(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    return None


class ImageCache:
    """把图片下载到缓存目录（共享目录或插件数据目录下），并按总大小自动清理。"""

    def __init__(
        self,
        http: HttpClient,
        cache_dir: Path,
        max_total_mb: float = CACHE_MB,
        max_image_mb: float = MAX_IMAGE_MB,
    ):
        self.http = http
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_total = int(max_total_mb * 1024 * 1024)
        self.max_image = int(max_image_mb * 1024 * 1024)

    async def download(
        self, url: str, dest: Path | None = None, headers: dict | None = None
    ) -> Path | None:
        """下载单张图片，返回本地文件路径；失败返回 None。

        dest 为不含扩展名的目标路径（例如整本下载时的「页码」），此时不进缓存、不触发清理。
        headers 覆盖默认请求头（例如图床只认特定的 User-Agent）。
        """
        try:
            try:
                try:
                    fetched = await self._fetch(url, headers)
                except aiohttp.ConnectionTimeoutError:
                    # 连不上（握手超时）的服务器重试多半还是超时，直接算失败，由调用方换页或换服务器
                    raise
                except aiohttp.ClientConnectionError as e:
                    # 代理偶尔会重置新建的连接，稍等再试一次
                    logger.info(f"[random_pic] 连接失败，稍后重试 {url}: {e!r}")
                    await asyncio.sleep(RETRY_DELAY)
                    fetched = await self._fetch(url, headers)
            except aiohttp.ClientPayloadError as e:
                # 部分 H@H 节点经代理时会不发 TLS close_notify 就断开，asyncio 的 SSL
                # 层会丢掉最后几 KB，同一地址重试也一样；阻塞式 ssl 能读全，所以退回 urllib
                logger.info(f"[random_pic] 响应不完整，改用 urllib 重新下载: {e!r}")
                fetched = await run_thread(self._fetch_blocking, url, headers)
            if fetched is None:
                logger.info(f"[random_pic] 图片过大，已跳过: {url}")
                return None
            path = self._save(*fetched, dest)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, HttpError) as e:
            logger.warning(f"[random_pic] 下载失败 {url}: {e!r}")
            return None
        if dest is None:
            self.cleanup()
        return path

    async def _fetch(
        self, url: str, headers: dict | None = None
    ) -> tuple[bytes, str] | None:
        """返回 (图片数据, Content-Type)，超过大小上限时返回 None。"""
        async with self.http.request("GET", url, headers=headers) as resp:
            if resp.status != 200:
                raise HttpError(f"HTTP {resp.status}")
            if resp.content_length and resp.content_length > self.max_image:
                return None
            data = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > self.max_image:
                    return None
            return bytes(data), resp.content_type.lower()

    def _fetch_blocking(
        self, url: str, headers: dict | None = None
    ) -> tuple[bytes, str] | None:
        proxy = self.http.proxy
        proxies = {"http": proxy, "https": proxy} if proxy else {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, **(headers or {})}
        )
        with opener.open(request, timeout=self.http.timeout.total) as resp:
            if resp.status != 200:
                raise HttpError(f"HTTP {resp.status}")
            data = resp.read(self.max_image + 1)
            ctype = resp.headers.get_content_type().lower()
        return None if len(data) > self.max_image else (data, ctype)

    def _save(self, data: bytes, ctype: str, dest: Path | None = None) -> Path:
        ext = sniff_ext(data[:16]) or IMAGE_EXTS.get(ctype)
        if not ext:
            raise HttpError(f"不是图片（{ctype}）")
        if dest is None:
            path = self.dir / f"{uuid.uuid4().hex}{ext}"
        else:
            path = dest.with_name(dest.name + ext)
        path.write_bytes(data)
        return path

    def cleanup(self):
        files = []
        for path in self.dir.iterdir():
            try:
                stat = path.stat()
            except OSError:
                continue  # 并发清理时已被删除
            if path.is_file():
                files.append((stat.st_mtime, stat.st_size, path))
        files.sort(key=lambda f: f[0], reverse=True)
        fresh_after = time.time() - FRESH_SECONDS
        total = 0
        for mtime, size, path in files:
            total += size
            # 刚下载的图片保留，保证同一次抽卡的图片都能发出去
            if mtime < fresh_after and total > self.max_total:
                path.unlink(missing_ok=True)
                total -= size


async def download_all(
    items: list,
    dest: Path,
    concurrency: int,
    fetch: Callable[[object, Path], Awaitable[Path | None]],
) -> tuple[list[Path], int]:
    """整本下载：items 依次是第 1、2……页，fetch(页, 不含扩展名的目标路径) 下载一页。

    文件以页码命名存到 dest，返回 (按页码排序的图片路径, 失败页数)。
    fetch 抛出的异常在所有页结束后向上传递。
    """
    dest.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(number: int, item) -> Path | None:
        async with semaphore:
            return await fetch(item, dest / f"{number:05d}")

    results = await asyncio.gather(
        *(one(n, item) for n, item in enumerate(items, 1)), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    paths = [p for p in results if p]
    return paths, len(items) - len(paths)
