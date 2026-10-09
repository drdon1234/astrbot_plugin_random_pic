"""HTTP 客户端、限速器与图片下载缓存。"""

import asyncio
import time
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from http.cookies import SimpleCookie
from pathlib import Path

import aiohttp
from yarl import URL

from astrbot.api import logger

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

# 请求超时（秒）
TIMEOUT = 20
# 建立连接（连上代理、经代理的 TLS 握手）的超时（秒）：个别 H@H 图片节点经代理握手会一直卡住，
# 正常握手 1 秒左右，不能让它占满整个请求超时
CONNECT_TIMEOUT = 8
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


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# 网络抖动、站点出错一类可以换一次再试的错误
NETWORK_ERRORS = (HttpError, aiohttp.ClientError, asyncio.TimeoutError)


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
        proxy: str | None = None,
        cookies: dict[str, dict[str, str]] | None = None,
        timeout: float = TIMEOUT,
        connect_timeout: float = CONNECT_TIMEOUT,
    ):
        self.timeout = aiohttp.ClientTimeout(
            total=timeout, sock_connect=min(connect_timeout, timeout)
        )
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

    async def get_text(self, url: str, *, params=None, headers=None) -> str:
        text, _ = await self.get_text_headers(url, params=params, headers=headers)
        return text

    async def get_text_headers(
        self, url: str, *, params=None, headers=None
    ) -> tuple[str, dict[str, str]]:
        """返回 (响应文本, 响应头)，响应头的键为小写。headers 覆盖默认请求头。"""
        async with self.request("GET", url, params=params, headers=headers) as resp:
            text = await resp.text(errors="replace")
            if resp.status != 200:
                raise HttpError(f"HTTP {resp.status}: {text[:200]}", resp.status)
            return text, {k.lower(): v for k, v in resp.headers.items()}

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
                fetched = await asyncio.to_thread(self._fetch_blocking, url, headers)
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
