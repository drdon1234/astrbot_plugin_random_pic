"""哔咔漫画（PicACG）三次元图源：只用 Cosplay 分类，需要账号登录。

接口是 App 用的 picaapi.picacomic.com，每个请求带 HMAC-SHA256 签名
（路径 + 时间 + nonce + 方法 + api-key，转小写，密钥写死在 App 里），登录后带 token。
- auth/sign-in：邮箱（用户名）+ 密码换 token，实测有效期 7 天，过期后接口返回 401；
- comics?page=N&c=Cosplay&s=dd：分类列表，每页 20 本，带标签；
- comics/{id}/order/{第几话}/pages?page=N：一话的图片，每页若干张，带总张数。

随机抽取 = 随机翻一页分类列表，再随机挑一本、随机取一张。哔咔没有分级，
上传者给无露点的写真打「無H內容」标签（实测 Cosplay 分类约 83% 带这个标签），
带它的算擦边，不带的算 R18。
"""

import asyncio
import hashlib
import hmac
import json
import random
import time
import uuid
from pathlib import Path

import aiohttp
from yarl import URL

from astrbot.api import logger

from .filters import TagBlacklist
from .models import EXPLICIT, REAL, SENSITIVE, ImageItem
from .net import HttpClient, ImageCache

API_BASE = "https://picaapi.picacomic.com/"
API_KEY = "C69BAF41DA5ABD1FFEDC6D2FEA56B"
SECRET = r"~d}$Q7$eIni=V)9\RK/P.RM4;9[7|@/CA}b~OW!3?EV`:<>M7pddUBL5n|0/*Cn"
APP_HEADERS = {
    "api-key": API_KEY,
    "accept": "application/vnd.picacomic.com.v1+json",
    "app-channel": "2",
    "app-version": "2.2.1.2.3.3",
    "app-uuid": "defaultUuid",
    "app-platform": "android",
    "app-build-version": "44",
    "User-Agent": "okhttp/3.8.1",
    "image-quality": "original",
}
SOURCE = "pica"
CATEGORY = "Cosplay"
NON_H_TAG = "無H內容"
# 说明文字里不列出的标签
PLAIN_TAGS = frozenset({"COSPLAY", NON_H_TAG})
# 分类的总页数一小时刷新一次
LISTING_TTL = 3600
# 每张图最多看这么多页列表（一页里没有符合分级、黑名单的本子或下载失败时换一页）
LISTINGS_PER_IMAGE = 4
# 登录失败（密码错误、被封）后这么久内不再尝试，避免每次抽卡都去登录
LOGIN_BACKOFF = 600


class PicaError(Exception):
    pass


def sign(path: str, ts: str, nonce: str, method: str) -> str:
    raw = (path + ts + nonce + method + API_KEY).lower()
    return hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


def comic_rating(tags: list[str]) -> str:
    return SENSITIVE if NON_H_TAG in tags else EXPLICIT


def media_url(media: dict) -> str:
    return f"{str(media['fileServer']).rstrip('/')}/static/{media['path']}"


class Picacomic:
    def __init__(
        self,
        http: HttpClient,
        cache: ImageCache,
        proxy: str | None,
        blacklist: TagBlacklist,
        email: str,
        password: str,
        *,
        rating_enabled: bool = True,
        explicit_skip: float = 0.0,
    ):
        self.http = http
        self.cache = cache
        self.proxy = proxy
        self.blacklist = blacklist
        self.email = email
        self.password = password
        self.rating_enabled = rating_enabled
        self.explicit_skip = min(max(explicit_skip, 0.0), 0.9)
        self._token: str | None = None
        self._login_failed = -LOGIN_BACKOFF
        self._login_lock = asyncio.Lock()
        self._pages = 0
        self._pages_at = -LISTING_TTL

    async def _send(
        self, method: str, path: str, body: dict | None
    ) -> tuple[int, dict]:
        ts, nonce = str(int(time.time())), uuid.uuid4().hex
        headers = {
            **APP_HEADERS,
            "time": ts,
            "nonce": nonce,
            "signature": sign(path, ts, nonce, method),
        }
        if self._token:
            headers["authorization"] = self._token
        # 签名按原样的路径计算，不能让 aiohttp 重新编码
        url = URL(API_BASE + path, encoded=True)
        async with self.http.session.request(
            method, url, json=body, headers=headers, proxy=self.proxy
        ) as resp:
            text = await resp.text(errors="replace")
            try:
                data = json.loads(text)
            except ValueError:
                data = {"message": text[:200]}
            return resp.status, data if isinstance(data, dict) else {}

    async def _login(self, stale: str | None):
        async with self._login_lock:
            if self._token != stale:
                return  # 等锁期间别的请求已经重新登录过
            if time.monotonic() - self._login_failed < LOGIN_BACKOFF:
                raise PicaError("哔咔登录失败，稍后再试")
            self._token = None
            status, data = await self._send(
                "POST", "auth/sign-in", {"email": self.email, "password": self.password}
            )
            token = (data.get("data") or {}).get("token")
            if status != 200 or not token:
                self._login_failed = time.monotonic()
                raise PicaError(f"哔咔登录失败：{data.get('message') or status}")
            self._token = token
            logger.info("[random_pic] 哔咔登录成功")

    async def _get(self, path: str) -> dict:
        """带 token 的 GET 请求，返回响应里的 data。token 过期时重新登录一次。"""
        if self._token is None:
            await self._login(None)
        for attempt in range(2):
            token = self._token
            status, data = await self._send("GET", path, None)
            if status == 401 and attempt == 0:
                await self._login(token)
                continue
            if status != 200:
                raise PicaError(
                    f"哔咔请求失败：HTTP {status} {data.get('message', '')}"
                )
            return data.get("data") or {}
        raise AssertionError("unreachable")

    async def _listing(self, page: int) -> dict:
        data = await self._get(f"comics?page={page}&c={CATEGORY}&s=dd")
        return data.get("comics") or {}

    async def _random_listing(self) -> list[dict]:
        if time.monotonic() - self._pages_at > LISTING_TTL:
            first = await self._listing(1)
            self._pages = int(first.get("pages") or 0)
            self._pages_at = time.monotonic()
            if self._pages <= 0:
                raise PicaError("哔咔 Cosplay 分类为空")
        listing = await self._listing(random.randint(1, self._pages))
        return listing.get("docs") or []

    def _reject(self, comic: dict, rating: str) -> str | None:
        tags = [str(t) for t in comic.get("tags") or []]
        if not tags:
            return "缺少标签，无法做未成年过滤"
        if self.rating_enabled and comic_rating(tags) != rating:
            return "分级不符"
        term = self.blacklist.hit(
            [*tags, str(comic.get("title") or ""), str(comic.get("author") or "")]
        )
        if term:
            return f"命中黑名单 {term}"
        if not comic.get("pagesCount"):
            return "没有图片"
        return None

    async def draw(
        self, n: int, rating: str, same_comic: bool
    ) -> tuple[list[tuple[ImageItem, Path]], list[str]]:
        """抽 n 张图。same_comic 时只取一本，按页码顺序取至多 n 张；否则每本取一张。"""
        images: list[tuple[ImageItem, Path]] = []
        errors: list[str] = []
        seen: set[str] = set()
        skipped = 0
        for _ in range(LISTINGS_PER_IMAGE * (1 if same_comic else n)):
            need = n - len(images)
            if need <= 0 or (same_comic and images):
                break
            try:
                docs = await self._random_listing()
                comics = [
                    c
                    for c in docs
                    if c.get("_id") not in seen and not self._reject(c, rating)
                ]
                if not comics:
                    skipped += 1
                    continue
                comic = random.choice(comics)
                seen.add(comic["_id"])
                items = await self._fetch(comic, need if same_comic else 1)
            except PicaError as e:
                logger.warning(f"[random_pic] {e}")
                errors.append(str(e))
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"[random_pic] 哔咔请求失败: {e!r}")
                if "哔咔请求失败" not in errors:
                    errors.append("哔咔请求失败")
                continue
            if not items:
                skipped += 1
            images.extend(items)
        if skipped:
            errors.append(f"{skipped} 次哔咔抽取没有符合条件的本子或下载失败")
        return images, errors

    async def _episode_page(self, cid: str, order: int, page: int) -> dict:
        data = await self._get(f"comics/{cid}/order/{order}/pages?page={page}")
        return data.get("pages") or {}

    async def _fetch(self, comic: dict, n: int) -> list[tuple[ImageItem, Path]]:
        """从本子随机的一话里取 n 张，按页码排序。R18 跳过开头穿着完整的一段。"""
        cid = comic["_id"]
        tags = [str(t) for t in comic.get("tags") or []]
        rating = comic_rating(tags)
        order = random.randint(1, max(1, int(comic.get("epsCount") or 1)))
        first = await self._episode_page(cid, order, 1)
        total, limit = int(first.get("total") or 0), int(first.get("limit") or 0)
        if total <= 0 or limit <= 0:
            return []
        start = int(total * self.explicit_skip) if rating == EXPLICIT else 0
        indices = sorted(random.sample(range(start, total), min(n, total - start)))
        pages = {1: first.get("docs") or []}
        items = []
        for index in indices:
            page = index // limit + 1
            if page not in pages:
                pages[page] = (await self._episode_page(cid, order, page)).get(
                    "docs"
                ) or []
            docs = pages[page]
            if index % limit >= len(docs):
                continue
            url = media_url(docs[index % limit]["media"])
            path = await self.cache.download(url, self.proxy)
            if path is None:
                continue
            item = ImageItem(
                image_url=url,
                rating=rating,
                style=REAL,
                title=str(comic.get("title") or "").strip(),
                author=str(comic.get("author") or "").strip(),
                category=f"哔咔 {CATEGORY}",
                page=index + 1,
                pages=total,
                tags=tags,
                characters=[t for t in tags if t not in PLAIN_TAGS],
                source=SOURCE,
            )
            items.append((item, path))
        return items
