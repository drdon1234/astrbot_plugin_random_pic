"""HTTP 客户端、限速器与图片下载缓存。"""

import asyncio
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger

USER_AGENT = "astrbot_plugin_random_pic (by drdon1234; AstrBot plugin)"

IMAGE_EXTS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


class HttpError(Exception):
    pass


class HttpClient:
    """所有图源共用一个 aiohttp 会话，所有请求都带超时。"""

    def __init__(self, timeout: float):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout, headers={"User-Agent": USER_AGENT}
            )
        return self._session

    async def get_json(
        self,
        url: str,
        *,
        params=None,
        headers: dict | None = None,
        proxy: str | None = None,
        auth: aiohttp.BasicAuth | None = None,
    ):
        async with self.session.get(
            url, params=params, headers=headers, proxy=proxy, auth=auth
        ) as resp:
            if resp.status != 200:
                text = (await resp.text(errors="replace"))[:200]
                raise HttpError(f"HTTP {resp.status}: {text}")
            return await resp.json(content_type=None)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


class RateLimiter:
    """保证两次调用之间至少间隔 interval 秒。"""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self):
        async with self._lock:
            delay = self._last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


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
    """把图片下载到插件数据目录的缓存中，并按数量和总大小自动清理。"""

    def __init__(
        self,
        http: HttpClient,
        cache_dir: Path,
        max_files: int,
        max_total_mb: float,
        max_image_mb: float,
        pixiv_hosts: set[str],
    ):
        self.http = http
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_files = max(1, max_files)
        self.max_total = int(max_total_mb * 1024 * 1024)
        self.max_image = int(max_image_mb * 1024 * 1024)
        self.pixiv_hosts = pixiv_hosts

    def _headers(self, url: str) -> dict:
        host = (urlparse(url).hostname or "").lower()
        if host.endswith("pximg.net") or host in self.pixiv_hosts:
            return {"Referer": "https://www.pixiv.net/"}
        return {}

    async def download(self, urls: list[str], proxy: str | None) -> Path | None:
        """依次尝试 urls（原图、降级尺寸），返回本地文件路径。"""
        for url in urls:
            if not url:
                continue
            try:
                path = await self._fetch(url, proxy)
            except (aiohttp.ClientError, asyncio.TimeoutError, HttpError) as e:
                logger.warning(f"[random_pic] 下载失败 {url}: {e!r}")
                continue
            if path:
                self.cleanup()
                return path
        return None

    async def _fetch(self, url: str, proxy: str | None) -> Path | None:
        async with self.http.session.get(
            url, headers=self._headers(url), proxy=proxy
        ) as resp:
            if resp.status != 200:
                raise HttpError(f"HTTP {resp.status}")
            if resp.content_length and resp.content_length > self.max_image:
                logger.info(f"[random_pic] 图片过大，尝试降级尺寸: {url}")
                return None
            data = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > self.max_image:
                    logger.info(f"[random_pic] 图片过大，尝试降级尺寸: {url}")
                    return None
            ctype = resp.content_type.lower()
        ext = sniff_ext(bytes(data[:16])) or IMAGE_EXTS.get(ctype)
        if not ext:
            raise HttpError(f"不是图片（{ctype}）")
        path = self.dir / f"{uuid.uuid4().hex}{ext}"
        path.write_bytes(data)
        return path

    def cleanup(self):
        files = [p for p in self.dir.iterdir() if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        total = 0
        for index, path in enumerate(files):
            size = path.stat().st_size
            total += size
            # 最新的一张永远保留，保证刚下载的图片能被发送
            if index > 0 and (index >= self.max_files or total > self.max_total):
                path.unlink(missing_ok=True)
                total -= size
