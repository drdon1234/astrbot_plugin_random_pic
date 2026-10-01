"""HTTP 客户端、限速器与图片下载缓存。"""

import asyncio
import time
import urllib.request
import uuid
from http.cookies import SimpleCookie
from pathlib import Path

import aiohttp
from yarl import URL

from astrbot.api import logger

from .images import compress

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

# 这么多秒内下载的图片不清理：并发抽图时同一次抽卡的图片可能还没发出去
FRESH_SECONDS = 600

IMAGE_EXTS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


class HttpError(Exception):
    pass


def scoped_cookies(domain: str, values: dict[str, str]) -> SimpleCookie:
    cookies = SimpleCookie()
    for name, value in values.items():
        cookies[name] = value
        cookies[name]["domain"] = domain
        cookies[name]["path"] = "/"
    return cookies


class HttpClient:
    """共用一个 aiohttp 会话，所有请求都带超时、走同一个代理。

    cookies 为 {域名: {名称: 值}}，只发给该域名及其子域名，不会发给图片服务器等第三方。
    """

    def __init__(
        self,
        timeout: float,
        proxy: str | None = None,
        cookies: dict[str, dict[str, str]] | None = None,
    ):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.proxy = proxy or None
        self.cookies = cookies or {}
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar()
            for domain, values in self.cookies.items():
                jar.update_cookies(
                    scoped_cookies(domain, values), URL(f"https://{domain}/")
                )
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"User-Agent": USER_AGENT},
                cookie_jar=jar,
            )
        return self._session

    def request(self, method: str, url, **kwargs):
        return self.session.request(method, url, proxy=self.proxy, **kwargs)

    async def get_text(self, url: str, *, params=None) -> str:
        async with self.request("GET", url, params=params) as resp:
            text = await resp.text(errors="replace")
            if resp.status != 200:
                raise HttpError(f"HTTP {resp.status}: {text[:200]}")
            return text

    async def get_bytes(self, url: str) -> bytes:
        async with self.request("GET", url) as resp:
            if resp.status != 200:
                raise HttpError(f"HTTP {resp.status}")
            return await resp.read()

    async def post_json(self, url: str, payload: dict):
        async with self.request("POST", url, json=payload) as resp:
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
    """把图片下载到插件数据目录的缓存中，并按总大小自动清理。"""

    def __init__(
        self,
        http: HttpClient,
        cache_dir: Path,
        max_total_mb: float,
        max_image_mb: float,
        quality: int = 0,
    ):
        self.http = http
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_total = int(max_total_mb * 1024 * 1024)
        self.max_image = int(max_image_mb * 1024 * 1024)
        self.quality = quality

    async def download(self, url: str, dest: Path | None = None) -> Path | None:
        """下载单张图片，返回本地文件路径；失败返回 None。

        进缓存的图片按 quality 转成 JPEG（0 为原图）。dest 为不含扩展名的目标路径
        （例如整本下载时的「页码」），此时保留原图、不进缓存、不触发清理。
        """
        try:
            try:
                fetched = await self._fetch(url)
            except aiohttp.ClientPayloadError as e:
                # 部分 H@H 节点经代理时会不发 TLS close_notify 就断开，asyncio 的 SSL
                # 层会丢掉最后几 KB，同一地址重试也一样；阻塞式 ssl 能读全，所以退回 urllib
                logger.info(f"[random_pic] 响应不完整，改用 urllib 重新下载: {e!r}")
                fetched = await asyncio.to_thread(self._fetch_blocking, url)
            if fetched is None:
                logger.info(f"[random_pic] 图片过大，已跳过: {url}")
                return None
            path = self._save(*fetched, dest)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, HttpError) as e:
            logger.warning(f"[random_pic] 下载失败 {url}: {e!r}")
            return None
        if dest is None:
            path = await asyncio.to_thread(self._compress, path)
            self.cleanup()
        return path

    def _compress(self, path: Path) -> Path:
        try:
            return compress(path, self.quality)
        except Exception as e:
            # 解码失败的图片原样交给后面的流程（彩图检查会把它当黑白页丢弃）
            logger.warning(f"[random_pic] 图片转换失败，发送原图 {path}: {e!r}")
            return path

    async def _fetch(self, url: str) -> tuple[bytes, str] | None:
        """返回 (图片数据, Content-Type)，超过大小上限时返回 None。"""
        async with self.http.request("GET", url) as resp:
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

    def _fetch_blocking(self, url: str) -> tuple[bytes, str] | None:
        proxy = self.http.proxy
        proxies = {"http": proxy, "https": proxy} if proxy else {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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
