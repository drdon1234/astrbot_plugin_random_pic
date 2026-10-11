"""HTTP 会话、请求错误、站点 Cookie 与限速。"""

import asyncio
import time
from http.cookies import SimpleCookie

import aiohttp
from yarl import URL

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

# 请求超时（秒）
TIMEOUT = 20
# 建立连接（连上代理、经代理的 TLS 握手）的超时（秒）：个别 H@H 图片节点经代理握手会一直卡住，
# 正常握手 1 秒左右，不能让它占满整个请求超时
CONNECT_TIMEOUT = 8


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
