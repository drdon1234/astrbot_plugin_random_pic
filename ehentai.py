"""E-Hentai 访问层：搜索参数、随机游标跳转、元数据 API 与单页图片解析。

E-Hentai 的列表页只能用游标翻页（next=<gid> 返回 gid 更小的下一页），没有页码，
也没有随机排序。随机抽卡的做法是：先取得搜索结果中最新和最旧画廊的 gid，再在
两者之间随机取一个游标跳过去，从该页的画廊中随机挑选。gid 随时间递增，所以这是
一种按时间近似均匀的抽样。
"""

import random
import re
import time
from dataclasses import dataclass
from html import unescape

from .net import HttpClient, HttpError, RateLimiter

# f_cats 是「排除」位掩码：位为 1 表示不显示该分类
CATEGORY_BITS = {
    "Misc": 1,
    "Doujinshi": 2,
    "Manga": 4,
    "Artist CG": 8,
    "Game CG": 16,
    "Image Set": 32,
    "Cosplay": 64,
    "Asian Porn": 128,
    "Non-H": 256,
    "Western": 512,
}
ALL_CATEGORIES = 1023

GALLERY_RE = re.compile(r"https?://[^/\"'\s]+/g/(\d+)/([0-9a-f]{10})/")
IMAGE_RE = re.compile(r'<img id="img" src="([^"]+)"')
RELOAD_RE = re.compile(r"nl\('([^']+)'\)")
BANNED_MARK = "Your IP address has been temporarily banned"
# 匿名访问时画廊页每页 20 张缩略图
THUMBS_PER_PAGE = 20
GDATA_BATCH = 25
# 游标范围缓存的条目上限（随机角色模式会产生大量不同的搜索条件）
RANGE_CACHE_SIZE = 4096


class EHentaiError(Exception):
    pass


class BlockedError(EHentaiError):
    """IP 被封禁或图片额度用尽，继续请求没有意义。"""


class NoHitsError(EHentaiError):
    def __init__(self):
        super().__init__("没有符合条件的画廊")


@dataclass
class Gallery:
    gid: int
    token: str
    title: str
    category: str
    uploader: str
    filecount: int
    stars: float
    tags: list[str]
    expunged: bool


def category_mask(categories: list[str]) -> int:
    """把要包含的分类名转换为 f_cats 排除掩码。未知分类名会抛出 ValueError。"""
    included = 0
    for name in categories:
        name = str(name).strip()
        if name not in CATEGORY_BITS:
            raise ValueError(f"未知分类 {name!r}")
        included |= CATEGORY_BITS[name]
    if not included:
        raise ValueError("至少需要一个分类")
    return ALL_CATEGORIES & ~included


def build_search(
    categories: list[str],
    search: str,
    user_tags: list[str],
    *,
    exclude_ai: bool,
    min_stars: int,
    min_pages: int,
) -> dict[str, str]:
    """组合搜索参数。返回的 dict 同时作为游标范围缓存的键。"""
    words = [search.strip(), *user_tags]
    if exclude_ai:
        words.append('-other:"ai generated$"')
    params = {"f_cats": str(category_mask(categories))}
    query = " ".join(w for w in words if w)
    if query:
        params["f_search"] = query
    if min_stars >= 2 or min_pages > 0:
        params["advsearch"] = "1"
    if min_stars >= 2:
        params["f_sr"] = "on"
        params["f_srdd"] = str(min(min_stars, 5))
    if min_pages > 0:
        params["f_sp"] = "on"
        params["f_spf"] = str(min_pages)
    return params


def parse_listing(html: str) -> tuple[list[tuple[int, str]], bool]:
    """解析列表页，返回 (按出现顺序去重的 [(gid, token)], 是否还有下一页)。"""
    seen = set()
    galleries = []
    for gid, token in GALLERY_RE.findall(html):
        if gid not in seen:
            seen.add(gid)
            galleries.append((int(gid), token))
    return galleries, 'id="unext" href=' in html


def parse_page_links(html: str, gid: int) -> dict[int, str]:
    """解析画廊页，返回 {页码: 单页 URL}。"""
    pattern = re.compile(rf"https?://[^/\"'\s]+/s/[0-9a-f]{{10}}/{gid}-(\d+)")
    return {int(m.group(1)): m.group(0) for m in pattern.finditer(html)}


def parse_image(html: str) -> tuple[str | None, str | None]:
    """解析单页，返回 (图片 URL, 换服务器用的 nl 参数)。"""
    image = IMAGE_RE.search(html)
    reload = RELOAD_RE.search(html)
    return (image.group(1) if image else None, reload.group(1) if reload else None)


def parse_gallery(meta: dict) -> Gallery:
    return Gallery(
        gid=int(meta["gid"]),
        token=str(meta["token"]),
        # API 返回的标题和上传者是 HTML 转义过的
        title=unescape(meta.get("title_jpn") or meta.get("title") or ""),
        category=meta.get("category", ""),
        uploader=unescape(meta.get("uploader") or ""),
        filecount=int(meta.get("filecount") or 0),
        stars=float(meta.get("rating") or 0),
        tags=[str(t) for t in meta.get("tags", [])],
        expunged=bool(meta.get("expunged")),
    )


class EHentai:
    def __init__(
        self,
        http: HttpClient,
        base_url: str,
        api_url: str,
        proxy: str | None,
        interval: float,
        range_ttl: float = 3600,
    ):
        self.http = http
        self.base = base_url.rstrip("/")
        self.api_url = api_url
        self.proxy = proxy
        self.limiter = RateLimiter(interval)
        self.range_ttl = range_ttl
        # 搜索参数 → (过期时间, 最旧 gid, 最新 gid, 单页结果)；
        # 结果只有一页时缓存这一页，没有结果时缓存空列表
        self._ranges: dict[tuple, tuple[float, int, int, list | None]] = {}

    def gallery_url(self, gid: int, token: str) -> str:
        return f"{self.base}/g/{gid}/{token}/"

    async def _get(self, url: str, params=None) -> str:
        await self.limiter.wait()
        try:
            html = await self.http.get_text(url, params=params, proxy=self.proxy)
        except HttpError as e:
            if BANNED_MARK in str(e):
                raise BlockedError("IP 被 E-Hentai 临时封禁，请稍后再试") from e
            raise
        if BANNED_MARK in html:
            raise BlockedError("IP 被 E-Hentai 临时封禁，请稍后再试")
        return html

    async def listing(self, params: dict) -> tuple[list[tuple[int, str]], bool]:
        return parse_listing(await self._get(self.base + "/", params))

    async def _range(self, params: dict) -> tuple[int, int, list | None]:
        key = tuple(sorted(params.items()))
        cached = self._ranges.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1:]
        newest, has_next = await self.listing(params)
        if has_next and newest:
            oldest, _ = await self.listing({**params, "prev": "1"})
            lo = min(g for g, _ in oldest or newest)
            entry = (lo, max(g for g, _ in newest), None)
        else:
            entry = (0, 0, newest)
        if len(self._ranges) >= RANGE_CACHE_SIZE:
            self._ranges.pop(next(iter(self._ranges)))
        self._ranges[key] = (time.monotonic() + self.range_ttl, *entry)
        return entry

    async def random_listing(self, params: dict) -> list[tuple[int, str]]:
        """随机跳转到搜索结果中的某一页，返回该页的画廊。"""
        lo, hi, single = await self._range(params)
        if single is not None:
            if not single:
                raise NoHitsError()
            return list(single)
        cursor = random.randint(lo + 1, hi + 1)
        galleries, _ = await self.listing({**params, "next": str(cursor)})
        if not galleries:
            galleries, _ = await self.listing(params)
        return galleries

    async def gdata(self, pairs: list[tuple[int, str]]) -> dict[int, Gallery]:
        out = {}
        for i in range(0, len(pairs), GDATA_BATCH):
            batch = pairs[i : i + GDATA_BATCH]
            await self.limiter.wait()
            data = await self.http.post_json(
                self.api_url,
                {
                    "method": "gdata",
                    "gidlist": [[gid, token] for gid, token in batch],
                    "namespace": 1,
                },
                proxy=self.proxy,
            )
            for meta in data.get("gmetadata", []):
                if "error" in meta:
                    continue
                gallery = parse_gallery(meta)
                out[gallery.gid] = gallery
        return out

    async def page_url(self, gallery: Gallery, index: int) -> tuple[int, str]:
        """取画廊第 index 张（从 0 开始）的单页 URL，返回 (实际页码, URL)。"""
        page = index // THUMBS_PER_PAGE
        params = {"p": str(page)} if page else None
        html = await self._get(self.gallery_url(gallery.gid, gallery.token), params)
        links = parse_page_links(html, gallery.gid)
        if not links:
            raise EHentaiError(f"画廊 {gallery.gid} 没有可用的图片页")
        number = index + 1
        if number not in links:
            # 缩略图分页与预期不符（例如登录后改了每页数量），退而在当前页随机挑一张
            number = random.choice(list(links))
        return number, links[number]

    async def image_url(self, page_url: str, reload: str | None = None):
        """解析单页上的图片地址，返回 (图片 URL, nl 参数)。"""
        params = {"nl": reload} if reload else None
        image, nl = parse_image(await self._get(page_url, params))
        if not image:
            raise EHentaiError("单页中没有图片")
        if image.endswith("/509.gif"):
            raise BlockedError("E-Hentai 图片额度已用尽（509），请稍后再试")
        return image, nl
