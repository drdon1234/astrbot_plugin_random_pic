"""WordPress 写真站三次元图源：CosplayTele（Cosplay，擦边和 R18）与 XiuRen（棚拍写真，只有擦边）。

两个站都开放了 WordPress 的 REST 接口 /wp-json/wp/v2/posts：
- per_page=1&page=N：按发布时间排序的第 N 个帖子，响应头 X-WP-Total 是符合条件的帖子数，
  所以总数缓存后随机抽一个帖子只要一次请求；N 超出总数时返回 400；
- categories / categories_exclude / tags_exclude：按分类、标签筛选（分类和标签用数字 id）；
- search：在标题和正文里搜索，几个词要同时出现，带引号的词组整体匹配；
- _embed=wp:term：附带分类和标签的名称，用于黑名单过滤和说明文字；
- posts/{id}、posts?slug=：按 id 或链接里的 slug 取帖子，/pdf 整本打包用。
帖子正文里的 <img> 就是整套图（1600px 左右的 webp 或原图），图片没有防盗链。

分级来自站点的分类，2026-10 抽样目检：
- CosplayTele「Cosplay Ero」为擦边（24 张随机页 23 张达到擦边），「Cosplay Nude」为 R18
  （随机单页约 2/3 露点，其余是同一套里还穿着的页，和 E-Hentai 一样按整套定分级）；
  两个分类几乎不重叠，同时在两个分类时按 R18 处理。
- XiuRen 是秀人、尤蜜等工作室的写真（24 张随机页 21 张达到擦边），没有分级分类，全部按擦边。
两站都排除站点自己标出的 AI 生成分类。
"""

import asyncio
import html
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from astrbot.api import logger

from ..filters import ContentFilter, rating_reason
from ..models import EXPLICIT, REAL, SENSITIVE, Album, DrawOptions, Work, WorkRef
from ..net import HttpClient, HttpError, ImageCache, download_all
from ..util import fetch_pages, fill, shared
from . import DrawContext

# 帖子总数缓存：不带关键词时一小时，带关键词时 10 分钟
TOTAL_TTL = 3600
SEARCH_TTL = 600
TOTAL_CACHE_SIZE = 256
# 每个图集最多看这么多个帖子（没有图、命中黑名单、分级不符或下载失败时换下一个）
POSTS_PER_ALBUM = 4
# 说明文字里最多列出的标签数
MAX_CAPTION_TAGS = 4
# 声明的宽高都小于这个值的图片是缩略图（XiuRen 的部分帖子开头有一张小封面）
MIN_SIDE = 500
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# 取帖子时要的字段（_links 是 _embed 必需的）
POST_FIELDS = "id,link,title,content,categories,tags,_links,_embedded"

IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
ATTR_RE = re.compile(r"""\b(src|width|height)\s*=\s*["']([^"']*)["']""", re.I)
# E-Hentai 搜索词形式的标签，例如 character:"hu tao$"、female:swimsuit$
TAG_TERM_RE = re.compile(r'^(?:[a-z]+:)?"?([^":$]+?)\$?"?$', re.I)
TAG_SYNTAX = frozenset(':$"')


@dataclass(frozen=True)
class Site:
    key: str  # 配置里的权重名
    name: str  # 显示名
    host: str
    ratings: dict[int, str] = field(default_factory=dict)  # 分类 id → 分级
    default_rating: str | None = None  # 没有分级分类的站点：所有帖子的分级
    exclude_categories: tuple[int, ...] = ()
    exclude_tags: tuple[int, ...] = ()
    # 关键词先经标签库翻译成英文（站点的标题和标签是英文时）
    translate: bool = False

    @property
    def api(self) -> str:
        return f"https://{self.host}/wp-json/wp/v2/posts"


COSPLAYTELE = Site(
    key="cosplaytele",
    name="CosplayTele",
    host="cosplaytele.com",
    ratings={194: SENSITIVE, 193: EXPLICIT},  # Cosplay Ero / Cosplay Nude
    exclude_categories=(589,),  # AI Art
    translate=True,
)
XIUREN = Site(
    key="xiuren",
    name="XiuRen",
    host="xiuren.biz",
    default_rating=SENSITIVE,
    exclude_categories=(1558,),  # AI Generated
    exclude_tags=(1559, 1560),  # AI、AI Generated
)
SITES = (COSPLAYTELE, XIUREN)


class WordPressError(Exception):
    pass


def search_terms(terms: list[str]) -> tuple[list[str], list[str]] | None:
    """返回 (搜索词, 排除词)，都转成小写。

    E-Hentai 标签写法取出标签名（character:"hu tao$" → hu tao）；其他带 : $ " 的写法搜不了，返回 None。
    """
    positive, negative = [], []
    for term in terms:
        exclude = term.startswith("-")
        word = term[1:] if exclude else term
        if TAG_SYNTAX & set(word):
            match = TAG_TERM_RE.match(word.strip())
            if not match:
                return None
            word = match.group(1)
        word = " ".join(word.split()).lower()
        if word:
            (negative if exclude else positive).append(word)
    return positive, negative


def search_query(words: list[str]) -> str:
    """多个词的标签名整体搜索，加引号。"""
    return " ".join(f'"{w}"' if " " in w else w for w in words)


def post_images(content: str) -> list[str]:
    """正文里的图片地址（按出现顺序去重），跳过缩略图和非图片。"""
    urls: list[str] = []
    for tag in IMG_RE.findall(content):
        attrs = {k.lower(): v for k, v in ATTR_RE.findall(tag)}
        src = html.unescape(attrs.get("src", "")).strip()
        path = src.split("?", 1)[0].lower()
        if not src.startswith("http") or "/wp-content/uploads/" not in path:
            continue
        if not path.endswith(IMAGE_EXTS):
            continue
        sizes = [attrs.get("width", ""), attrs.get("height", "")]
        if all(s.isdigit() for s in sizes) and max(map(int, sizes)) < MIN_SIDE:
            continue
        if src not in urls:
            urls.append(src)
    return urls


def term_names(post: dict) -> tuple[list[str], list[str]]:
    """返回 (分类名称, 标签名称)，来自 _embed=wp:term。"""
    categories, tags = [], []
    for group in (post.get("_embedded") or {}).get("wp:term") or []:
        for term in group or []:
            name = html.unescape(str(term.get("name") or "")).strip()
            if not name:
                continue
            if term.get("taxonomy") == "category":
                categories.append(name)
            elif term.get("taxonomy") == "post_tag":
                tags.append(name)
    return categories, tags


class WordPressSource:
    def __init__(
        self,
        site: Site,
        http: HttpClient,
        cache: ImageCache,
        content: ContentFilter,
        opts: DrawOptions,
    ):
        self.site = site
        self.key = site.key
        self.name = site.name
        self.http = http
        self.cache = cache
        self.content = content
        self.opts = opts
        # 查询参数 → (过期时间, 帖子总数)
        self._totals: dict[str, tuple[float, int]] = {}
        self._inflight: dict = {}

    def _terms(self, ctx: DrawContext) -> list[str]:
        return ctx.terms if self.site.translate else ctx.req.keywords

    def query(self, rating: str) -> dict[str, str] | None:
        """筛选帖子的参数，站点没有这个分级的帖子时返回 None。关闭内容分级时不按分级筛选。"""
        site, enabled = self.site, self.opts.rating_enabled
        params: dict[str, str] = {}
        exclude_categories = list(site.exclude_categories)
        if site.ratings:
            wanted = [c for c, r in site.ratings.items() if not enabled or r == rating]
            if not wanted:
                return None
            params["categories"] = ",".join(map(str, wanted))
            # 同时在 R18 分类里的帖子按 R18 处理
            if enabled and rating != EXPLICIT:
                exclude_categories += [
                    c for c, r in site.ratings.items() if r == EXPLICIT
                ]
        elif enabled and site.default_rating != rating:
            return None
        if exclude_categories:
            params["categories_exclude"] = ",".join(map(str, exclude_categories))
        if site.exclude_tags:
            params["tags_exclude"] = ",".join(map(str, site.exclude_tags))
        return params

    def accepts(self, ctx: DrawContext) -> bool:
        """三次元，不是随机角色，站点有这个分级的帖子，关键词能搜索。"""
        req = ctx.req
        return (
            req.style == REAL
            and not req.random_character
            and self.query(req.rating) is not None
            and search_terms(self._terms(ctx)) is not None
        )

    def post_rating(self, post: dict) -> str | None:
        ratings = {
            self.site.ratings[c]
            for c in post.get("categories") or []
            if c in self.site.ratings
        }
        if EXPLICIT in ratings:
            return EXPLICIT
        if SENSITIVE in ratings:
            return SENSITIVE
        return self.site.default_rating

    async def _get(
        self, params: dict[str, str], path: str = ""
    ) -> tuple[list | dict, dict[str, str]]:
        """请求 posts 接口（path 为 /{id} 时取单个帖子），返回 (数据, 响应头)。"""
        text, headers = await self.http.get_text_headers(
            self.site.api + path, params=params
        )
        try:
            data = json.loads(text)
        except ValueError as e:
            raise WordPressError(f"{self.name} 返回的不是 JSON") from e
        if not isinstance(data, list | dict):
            data = {} if path else []
        return data, headers

    async def _total(self, params: dict[str, str]) -> int:
        key = json.dumps(params, sort_keys=True)
        cached = self._totals.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        async def count() -> int:
            _, headers = await self._get({**params, "per_page": "1", "_fields": "id"})
            try:
                total = int(headers.get("x-wp-total") or 0)
            except ValueError:
                total = 0
            ttl = SEARCH_TTL if "search" in params else TOTAL_TTL
            self._totals[key] = (time.monotonic() + ttl, total)
            while len(self._totals) > TOTAL_CACHE_SIZE:
                self._totals.pop(next(iter(self._totals)))
            return total

        return await shared(self._inflight, key, count)

    async def random_post(self, params: dict[str, str]) -> dict | None:
        """随机一个帖子；总数变少导致页码越界时清掉缓存的总数，返回 None。"""
        total = await self._total(params)
        if total <= 0:
            raise WordPressError(
                f"{self.name} 搜不到「{params['search']}」"
                if "search" in params
                else f"{self.name} 没有符合条件的帖子"
            )
        page = random.randint(1, total)
        try:
            posts, _ = await self._get(
                {
                    **params,
                    "per_page": "1",
                    "page": str(page),
                    "_embed": "wp:term",
                    "_fields": POST_FIELDS,
                }
            )
        except HttpError as e:
            if e.status != 400:
                raise
            self._totals.pop(json.dumps(params, sort_keys=True), None)
            return None
        if isinstance(posts, list) and posts and isinstance(posts[0], dict):
            return posts[0]
        return None

    def excluded(self, post: dict) -> bool:
        """帖子在站点的 AI 生成分类或标签里。"""
        return bool(
            set(post.get("categories") or []) & set(self.site.exclude_categories)
            or set(post.get("tags") or []) & set(self.site.exclude_tags)
        )

    def _reject(
        self, post: dict, ctx: DrawContext, exclude: list[str], images: list[str]
    ) -> str | None:
        if not images:
            return "没有图片"
        title = html.unescape(str((post.get("title") or {}).get("rendered") or ""))
        categories, tags = term_names(post)
        reason = rating_reason(
            self.post_rating(post),
            ctx.req.rating,
            ctx.is_private,
            self.opts.rating_enabled,
        ) or self.content.text_reason([title, *categories, *tags])
        if reason:
            return reason
        text = "\n".join([title, *categories, *tags]).lower()
        if any(word in text for word in exclude):
            return "命中排除的关键词"
        return None

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        """抽 n 个帖子，每个帖子取至多 per_album 张，帖子之间并发抽取。"""
        params = self.query(ctx.req.rating)
        terms = search_terms(self._terms(ctx))
        if params is None or terms is None:
            return [], [f"{self.name} 不支持这次请求"]
        positive, exclude = terms
        if positive:
            params["search"] = search_query(positive)
        albums: list[Album] = []
        errors: list[str] = []
        seen: set[int] = set()
        skipped = 0
        semaphore = asyncio.Semaphore(self.opts.concurrency)

        async def attempt() -> Album | None:
            nonlocal skipped
            try:
                post = await self.random_post(params)
            except (HttpError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"[random_pic] {self.name} 请求失败: {e!r}")
                message = f"{self.name} 请求失败"
                if message not in errors:
                    errors.append(message)
                return None
            if post is None or post.get("id") in seen:
                return None
            seen.add(post.get("id"))
            images = post_images(str((post.get("content") or {}).get("rendered") or ""))
            reason = self._reject(post, ctx, exclude, images)
            if reason:
                logger.info(
                    f"[random_pic] 丢弃 {self.name} 帖子 {post.get('id')}: {reason}"
                )
                skipped += 1
                return None
            album = await self._album(post, images, ctx.req.per_album, semaphore)
            if album is None:
                skipped += 1
            return album

        try:
            await fill(albums, n, POSTS_PER_ALBUM * n, self.opts.concurrency, attempt)
        except WordPressError as e:
            logger.warning(f"[random_pic] {e}")
            errors.append(str(e))
        if skipped:
            errors.append(f"{skipped} 个 {self.name} 帖子被过滤或下载失败")
        return albums, errors

    async def _album(
        self, post: dict, images: list[str], n: int, semaphore: asyncio.Semaphore
    ) -> Album | None:
        skip = self.opts.explicit_skip if self.post_rating(post) == EXPLICIT else 0.0

        async def download(index: int) -> tuple[int, Path] | None:
            async with semaphore:
                path = await self.cache.download(images[index])
            return (index + 1, path) if path else None

        pictures = await fetch_pages(
            len(images),
            n,
            self.opts.concurrency,
            download,
            from_start=self.opts.from_start,
            skip=skip,
        )
        if not pictures:
            return None
        _, tags = term_names(post)
        details = []
        if tags:
            details.append(f"标签：{'、'.join(tags[:MAX_CAPTION_TAGS])}")
        if post.get("link"):
            details.append(f"帖子：{post['link']}")
        title = html.unescape(str((post.get("title") or {}).get("rendered") or ""))
        return Album(
            source=self.name,
            title=title.strip(),
            total=len(images),
            pictures=sorted(pictures),
            details=details,
            work=WorkRef(self.key, str(post.get("id"))),
        )

    async def work(self, ref: WorkRef) -> Work | None:
        """按帖子 id 或链接里的 slug 取帖子，不存在时返回 None。"""
        params = {"_embed": "wp:term", "_fields": POST_FIELDS}
        if ref.id.isdigit():
            try:
                post, _ = await self._get(params, f"/{ref.id}")
            except HttpError as e:
                if e.status in (400, 404):
                    return None
                raise
        else:
            posts, _ = await self._get({**params, "slug": ref.id})
            post = posts[0] if isinstance(posts, list) and posts else None
        if not isinstance(post, dict) or not post.get("id"):
            return None
        images = post_images(str((post.get("content") or {}).get("rendered") or ""))
        title = html.unescape(str((post.get("title") or {}).get("rendered") or ""))
        categories, tags = term_names(post)
        blocked = (
            ("是 AI 生成的作品" if self.excluded(post) else None)
            or self.content.text_reason([title, *categories, *tags])
            or (None if images else "没有图片")
        )
        return Work(
            ref=WorkRef(self.key, str(post["id"])),
            title=title.strip(),
            pages=len(images),
            rating=self.post_rating(post),
            blocked=blocked,
            data=images,
        )

    async def download_work(self, work: Work, dest: Path) -> tuple[list[Path], int]:
        return await download_all(self.cache, work.data, dest, self.opts.concurrency)
