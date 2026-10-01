"""哔咔漫画（PicACG）三次元图源：只用 Cosplay 分类，需要账号登录。

接口是 App 用的 picaapi.picacomic.com，每个请求带 HMAC-SHA256 签名
（路径 + 时间 + nonce + 方法 + api-key，转小写，密钥写死在 App 里），登录后带 token。
- auth/sign-in：邮箱（用户名）+ 密码换 token，实测有效期 7 天，过期后接口返回 401；
- comics?page=N&c=Cosplay&s=dd：分类列表，每页 20 本，带标签；
- comics/advanced-search?page=N（POST 关键词和分类）：关键词搜索，结果格式同分类列表；
- comics/{id}：本子详情（搜索结果缺标签时补标签）；
- comics/{id}/order/{第几话}/pages?page=N：一话的图片，每页若干张，带总张数。

随机抽取 = 随机翻一页分类列表（带关键词时翻搜索结果），再随机挑一本、随机取一张。
关键词只支持普通词，E-Hentai 的标签语法（带 : $ "）交给 E-Hentai。哔咔没有分级，
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
from .workers import fill

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
# 搜索结果的总页数缓存 10 分钟
SEARCH_TTL = 600
SEARCH_CACHE_SIZE = 256
# 每张图最多看这么多页列表（一页里没有符合分级、黑名单的本子或下载失败时换一页）
LISTINGS_PER_IMAGE = 4
# 搜索结果不带标签时，每页最多查这么多本的详情
DETAIL_TRIES = 3
# 登录失败（密码错误、被封、限流）后这么久内不再尝试，避免每次抽卡都去登录
LOGIN_BACKOFF = 600
# 关键词里出现这些字符时是 E-Hentai 的标签语法，哔咔搜不了
TAG_SYNTAX = frozenset(':$"')


class PicaError(Exception):
    pass


def sign(path: str, ts: str, nonce: str, method: str) -> str:
    raw = (path + ts + nonce + method + API_KEY).lower()
    return hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


def comic_rating(tags: list[str]) -> str:
    return SENSITIVE if NON_H_TAG in tags else EXPLICIT


def media_url(media: dict) -> str:
    return f"{str(media['fileServer']).rstrip('/')}/static/{media['path']}"


def supports_terms(terms: list[str]) -> bool:
    """关键词都是普通词时哔咔才能搜索。"""
    return not any(TAG_SYNTAX & set(t) for t in terms)


def split_terms(terms: list[str]) -> tuple[str, list[str]]:
    """返回 (搜索用的关键词, 要排除的词)。哔咔搜索不支持排除，排除词在本地过滤。"""
    positive = [t for t in terms if t and not t.startswith("-")]
    negative = [t[1:].lower() for t in terms if t.startswith("-") and len(t) > 1]
    return " ".join(positive), negative


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
        token_path: Path | None = None,
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
        # 关键词 → (过期时间, 搜索结果总页数)
        self._search_pages: dict[str, tuple[float, int]] = {}
        # token 存到磁盘，插件重载后不必重新登录（登录接口限流很严）
        self._token_path = token_path
        self._load_token()

    def _account_key(self) -> str:
        return hashlib.sha256(self.email.encode()).hexdigest()

    def _load_token(self):
        if self._token_path is None:
            return
        try:
            data = json.loads(self._token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("account") == self._account_key():
            self._token = data.get("token") or None

    def _save_token(self):
        if self._token_path is None:
            return
        try:
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            self._token_path.write_text(
                json.dumps({"account": self._account_key(), "token": self._token}),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"[random_pic] 保存哔咔 token 失败: {e!r}")

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
            self._save_token()
            logger.info("[random_pic] 哔咔登录成功")

    async def _request(self, path: str, body: dict | None = None) -> dict:
        """带 token 的请求（有 body 时为 POST），返回响应里的 data。token 过期时重新登录一次。"""
        if self._token is None:
            await self._login(None)
        method = "GET" if body is None else "POST"
        for attempt in range(2):
            token = self._token
            status, data = await self._send(method, path, body)
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
        data = await self._request(f"comics?page={page}&c={CATEGORY}&s=dd")
        return data.get("comics") or {}

    async def _search(self, keyword: str, page: int) -> dict:
        data = await self._request(
            f"comics/advanced-search?page={page}",
            {"keyword": keyword, "categories": [CATEGORY], "sort": "dd"},
        )
        return data.get("comics") or {}

    async def _random_search(self, keyword: str) -> list[dict]:
        cached = self._search_pages.get(keyword)
        first = None
        if cached and cached[0] > time.monotonic():
            pages = cached[1]
        else:
            first = await self._search(keyword, 1)
            pages = int(first.get("pages") or 0)
            self._search_pages[keyword] = (time.monotonic() + SEARCH_TTL, pages)
            while len(self._search_pages) > SEARCH_CACHE_SIZE:
                self._search_pages.pop(next(iter(self._search_pages)))
        if pages <= 0:
            raise PicaError(f"哔咔 Cosplay 分类里搜不到「{keyword}」")
        page = random.randint(1, pages)
        listing = first if first is not None and page == 1 else None
        if listing is None:
            listing = await self._search(keyword, page)
        return listing.get("docs") or []

    async def _random_listing(self, keyword: str = "") -> list[dict]:
        if keyword:
            return await self._random_search(keyword)
        if time.monotonic() - self._pages_at > LISTING_TTL:
            first = await self._listing(1)
            self._pages = int(first.get("pages") or 0)
            self._pages_at = time.monotonic()
            if self._pages <= 0:
                raise PicaError("哔咔 Cosplay 分类为空")
        listing = await self._listing(random.randint(1, self._pages))
        return listing.get("docs") or []

    def _reject(
        self, comic: dict, rating: str, exclude: list[str] = ()
    ) -> str | None:
        categories = comic.get("categories")
        if categories and CATEGORY not in categories:
            return "不在 Cosplay 分类"
        tags = [str(t) for t in comic.get("tags") or []]
        if not tags:
            return "缺少标签，无法做未成年过滤"
        if self.rating_enabled and comic_rating(tags) != rating:
            return "分级不符"
        words = [*tags, str(comic.get("title") or ""), str(comic.get("author") or "")]
        term = self.blacklist.hit(words)
        if term:
            return f"命中黑名单 {term}"
        text = "\n".join(words).lower()
        if any(word in text for word in exclude):
            return "命中排除的关键词"
        if not comic.get("pagesCount"):
            return "没有图片"
        return None

    async def _detail(self, comic: dict) -> dict:
        data = await self._request(f"comics/{comic['_id']}")
        return {**comic, **(data.get("comic") or {})}

    async def _pick(
        self, keyword: str, exclude: list[str], rating: str, seen: set[str]
    ) -> dict | None:
        """随机翻一页，挑一本符合分级、黑名单的本子。"""
        docs = [
            c
            for c in await self._random_listing(keyword)
            if c.get("_id") and c["_id"] not in seen
        ]
        random.shuffle(docs)
        details = 0
        for comic in docs:
            if "tags" not in comic:
                if details >= DETAIL_TRIES:
                    break
                details += 1
                comic = await self._detail(comic)
            if comic["_id"] in seen or self._reject(comic, rating, exclude):
                continue
            seen.add(comic["_id"])
            return comic
        return None

    async def draw(
        self,
        n: int,
        rating: str,
        same_comic: bool,
        terms: list[str] = (),
        concurrency: int = 1,
    ) -> tuple[list[tuple[ImageItem, Path]], list[str]]:
        """抽 n 张图。same_comic 时只取一本，按页码顺序取至多 n 张；否则每本取一张，并发抽取。

        terms 是关键词（只支持普通词，见 supports_terms），以 - 开头的词表示排除。
        """
        keyword, exclude = split_terms(list(terms))
        images: list[tuple[ImageItem, Path]] = []
        errors: list[str] = []
        seen: set[str] = set()
        skipped = 0

        async def attempt(count: int) -> list[tuple[ImageItem, Path]]:
            nonlocal skipped
            try:
                comic = await self._pick(keyword, exclude, rating, seen)
                items = await self._fetch(comic, count, concurrency) if comic else []
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"[random_pic] 哔咔请求失败: {e!r}")
                if "哔咔请求失败" not in errors:
                    errors.append("哔咔请求失败")
                return []
            if not items:
                skipped += 1
            return items

        async def one() -> tuple[ImageItem, Path] | None:
            items = await attempt(1)
            return items[0] if items else None

        try:
            if same_comic:
                for _ in range(LISTINGS_PER_IMAGE):
                    images.extend(await attempt(n))
                    if images:
                        break
            else:
                await fill(images, n, LISTINGS_PER_IMAGE * n, concurrency, one)
        except PicaError as e:
            logger.warning(f"[random_pic] {e}")
            errors.append(str(e))
        if skipped:
            errors.append(f"{skipped} 次哔咔抽取没有符合条件的本子或下载失败")
        return images, errors

    async def _episode_page(self, cid: str, order: int, page: int) -> dict:
        data = await self._request(f"comics/{cid}/order/{order}/pages?page={page}")
        return data.get("pages") or {}

    async def _fetch(
        self, comic: dict, n: int, concurrency: int = 1
    ) -> list[tuple[ImageItem, Path]]:
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
        # 用到的图片列表页一次性并发取回
        wanted = sorted({index // limit + 1 for index in indices} - {1})
        fetched = await asyncio.gather(
            *(self._episode_page(cid, order, page) for page in wanted)
        )
        pages = {1: first.get("docs") or []}
        pages.update((page, data.get("docs") or []) for page, data in zip(wanted, fetched))
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def download(index: int) -> tuple[ImageItem, Path] | None:
            docs = pages[index // limit + 1]
            if index % limit >= len(docs):
                return None
            url = media_url(docs[index % limit]["media"])
            async with semaphore:
                path = await self.cache.download(url, self.proxy)
            if path is None:
                return None
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
            return item, path

        results = await asyncio.gather(*(download(i) for i in indices))
        return [r for r in results if r is not None]
