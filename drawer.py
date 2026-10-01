"""随机抽卡：风格 × 分级 → 画廊池，随机跳转、复核过滤后取随机一页的图片。"""

import asyncio
import random
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from astrbot.api import logger

from .ehentai import (
    BlockedError,
    EHentai,
    EHentaiError,
    Gallery,
    NoHitsError,
    build_search,
)
from .filters import TagBlacklist, check_gallery
from .models import EXPLICIT, RATINGS, STYLES, ImageItem, PicRequest
from .net import HttpError, ImageCache
from .tags import TagDB, TagIndex, search_term

DEFAULT_POOLS = {
    # 画集与无 H 图集，避免抽到整页文字的漫画
    "anime_sensitive": {
        "categories": ["Non-H"],
        "search": '~other:artbook$ ~other:"non-h imageset$" -other:"nudity only$"',
    },
    # 单张 CG 比同人志、漫画的随机一页更适合抽卡
    "anime_explicit": {
        "categories": ["Artist CG", "Game CG", "Image Set"],
        "search": '-other:"non-nude$"',
    },
    # Asian Porn 的无露点画廊以杂志写真为主
    "real_sensitive": {
        "categories": ["Cosplay", "Asian Porn"],
        "search": 'other:"non-nude$" -other:"nudity only$"',
    },
    # 只取带裸露或性内容标签的画廊，与 filters.EXPLICIT_TAGS 一致
    "real_explicit": {
        "categories": ["Cosplay"],
        "search": (
            '~other:"nudity only$" ~other:uncensored$ ~other:"mosaic censorship$" '
            '~other:"full censorship$" ~other:hardcore$ ~other:"no penetration$" '
            '~other:"object insertion only$"'
        ),
    },
}

# 每次随机跳转后最多尝试的画廊数，未通过复核或下载失败时换同一页的下一个
CANDIDATES_PER_JUMP = 3
AUTHOR_NAMESPACES = ("artist", "cosplayer", "group")
# 随机角色模式下，每张图最多换这么多个角色（没有画廊的角色每个只花一次请求）
CHARACTER_TRIES = 10
# 说明文字中最多列出的作品、角色数
MAX_NAMES = 3


@dataclass
class Pool:
    categories: list[str]
    search: str


@dataclass
class FetchResult:
    images: list[tuple[ImageItem, Path]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def pool_key(style: str, rating: str) -> str:
    return f"{style}_{rating}"


def build_pools(conf: dict) -> dict[tuple[str, str], Pool]:
    pools = {}
    for style in STYLES:
        for rating in RATINGS:
            key = pool_key(style, rating)
            default = DEFAULT_POOLS[key]
            categories = conf.get(f"{key}_categories") or default["categories"]
            search = conf.get(f"{key}_search")
            pools[(style, rating)] = Pool(
                [str(c).strip() for c in categories],
                default["search"] if search is None else str(search),
            )
    return pools


def gallery_author(gallery: Gallery) -> str:
    for namespace in AUTHOR_NAMESPACES:
        names = [
            t.split(":", 1)[1] for t in gallery.tags if t.startswith(namespace + ":")
        ]
        if names:
            return ", ".join(names)
    return gallery.uploader


def gallery_names(
    gallery: Gallery, namespace: str, index: TagIndex | None
) -> list[str]:
    tags = [t for t in gallery.tags if t.startswith(namespace + ":")][:MAX_NAMES]
    return [index.zh(t) if index else t.split(":", 1)[1] for t in tags]


class Drawer:
    def __init__(
        self,
        eh: EHentai,
        pools: dict[tuple[str, str], Pool],
        blacklist: TagBlacklist,
        cache: ImageCache,
        max_rounds: int,
        *,
        exclude_ai: bool,
        min_stars: int,
        min_pages: int,
        cover_only: bool,
        explicit_skip: float = 0.0,
        same_gallery: bool = False,
        tags: TagDB | None = None,
    ):
        self.eh = eh
        self.pools = pools
        self.blacklist = blacklist
        self.cache = cache
        self.max_rounds = max(1, max_rounds)
        self.exclude_ai = exclude_ai
        self.min_stars = min_stars
        self.min_pages = min_pages
        self.cover_only = cover_only
        self.explicit_skip = min(max(explicit_skip, 0.0), 0.9)
        self.same_gallery = same_gallery
        self.tags = tags

    def _params(self, pool: Pool, terms: list[str]) -> dict:
        return build_search(
            pool.categories,
            pool.search,
            terms,
            exclude_ai=self.exclude_ai,
            min_stars=self.min_stars,
            min_pages=self.min_pages,
        )

    async def draw(self, req: PicRequest, is_private: bool) -> FetchResult:
        result = FetchResult()
        pool = self.pools[(req.style, req.rating)]
        index = await self.tags.get() if self.tags else None
        terms = [index.translate(t) for t in req.tags] if index else list(req.tags)
        term = self.blacklist.hit([t for t in terms if not t.startswith("-")])
        if term:
            result.errors.append(f"关键词命中黑名单 {term}")
            return result
        if req.random_character and (index is None or not index.characters):
            result.errors.append("标签库不可用，无法随机角色")
            return result
        try:
            self._params(pool, terms)
        except ValueError as e:
            result.errors.append(f"画廊池配置错误：{e}")
            return result

        seen: set[int] = set()
        discarded = 0
        for _ in range(self.max_rounds):
            need = req.count - len(result.images)
            # 同一画廊模式只取一个画廊；画廊页数不足时就少发几张
            if need <= 0 or (self.same_gallery and result.images):
                break
            try:
                discarded += await self._round(
                    req, is_private, pool, terms, index, need, seen, result
                )
            except EHentaiError as e:
                result.errors.append(str(e))
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, HttpError) as e:
                # 网络抖动（连接被重置、超时）多半是偶发的，下一轮重试
                logger.warning(f"[random_pic] E-Hentai 请求失败: {e!r}")
                if "E-Hentai 请求失败" not in result.errors:
                    result.errors.append("E-Hentai 请求失败")
        if discarded:
            result.errors.append(f"{discarded} 个画廊被过滤或下载失败")
        return result

    async def _round(
        self,
        req: PicRequest,
        is_private: bool,
        pool: Pool,
        terms: list[str],
        index: TagIndex | None,
        need: int,
        seen: set[int],
        result: FetchResult,
    ) -> int:
        """跳转若干次，每次跳转选中一个画廊。元数据一轮只查一次 API。

        普通模式跳转 need 次、每个画廊取一张；同一画廊模式只跳一次、取 need 张。
        """
        jumps, per_gallery = (1, need) if self.same_gallery else (need, 1)
        groups = []
        for _ in range(jumps):
            listing = await self._listing(req, pool, terms, index)
            candidates = [g for g in listing if g[0] not in seen]
            random.shuffle(candidates)
            groups.append(candidates[:CANDIDATES_PER_JUMP])
        metas = await self.eh.gdata([g for group in groups for g in group])

        discarded = 0
        for group in groups:
            for gid, _ in group:
                if gid in seen:
                    continue
                seen.add(gid)
                gallery = metas.get(gid)
                reason = self._reject(gallery, req, is_private)
                if reason:
                    logger.info(f"[random_pic] 丢弃画廊 {gid}: {reason}")
                    discarded += 1
                    continue
                items = await self._fetch_images(gallery, req, index, per_gallery)
                if not items:
                    discarded += 1
                    continue
                result.images.extend(items)
                break
        return discarded

    async def _listing(
        self, req: PicRequest, pool: Pool, terms: list[str], index: TagIndex | None
    ) -> list[tuple[int, str]]:
        if not req.random_character:
            return await self.eh.random_listing(self._params(pool, terms))
        for _ in range(CHARACTER_TRIES):
            character = search_term("character", index.random_character())
            try:
                return await self.eh.random_listing(
                    self._params(pool, [*terms, character])
                )
            except NoHitsError:
                continue
        raise EHentaiError(f"连续 {CHARACTER_TRIES} 个随机角色都没有符合条件的画廊")

    def _reject(
        self, gallery: Gallery | None, req: PicRequest, is_private: bool
    ) -> str | None:
        if gallery is None:
            return "元数据缺失"
        if gallery.expunged:
            return "画廊已被删除"
        if gallery.filecount <= 0:
            return "画廊没有图片"
        return check_gallery(
            gallery.category,
            gallery.tags,
            req.style,
            req.rating,
            is_private,
            self.blacklist,
        )

    def _page_indices(self, gallery: Gallery, req: PicRequest, n: int) -> list[int]:
        """选 n 个不重复的页（从 0 开始），按页码升序。"""
        if self.cover_only:
            return list(range(min(n, gallery.filecount)))
        # R18 画廊通常从穿着完整开始，跳过前一段
        start = (
            int(gallery.filecount * self.explicit_skip) if req.rating == EXPLICIT else 0
        )
        population = range(start, gallery.filecount)
        return sorted(random.sample(population, min(n, len(population))))

    async def _fetch_images(
        self, gallery: Gallery, req: PicRequest, index: TagIndex | None, n: int
    ) -> list[tuple[ImageItem, Path]]:
        """按页码顺序逐张取图，保证同一画廊的多张图按顺序发送。"""
        items = []
        for page_index in self._page_indices(gallery, req, n):
            item = await self._fetch_image(gallery, req, index, page_index)
            if item:
                items.append(item)
        return items

    async def _fetch_image(
        self,
        gallery: Gallery,
        req: PicRequest,
        index: TagIndex | None,
        page_index: int,
    ) -> tuple[ImageItem, Path] | None:
        try:
            page, page_url = await self.eh.page_url(gallery, page_index)
            image, path = await self._download_page(page_url)
        except BlockedError:
            raise
        except (
            EHentaiError,
            HttpError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as e:
            logger.warning(f"[random_pic] 画廊 {gallery.gid} 取图失败: {e}")
            return None
        if path is None:
            return None
        item = ImageItem(
            image_url=image,
            style=req.style,
            rating=req.rating,
            gid=gallery.gid,
            token=gallery.token,
            title=gallery.title,
            author=gallery_author(gallery),
            category=gallery.category,
            gallery_url=self.eh.gallery_url(gallery.gid, gallery.token),
            page_url=page_url,
            page=page,
            pages=gallery.filecount,
            stars=gallery.stars,
            tags=gallery.tags,
            parodies=gallery_names(gallery, "parody", index),
            characters=gallery_names(gallery, "character", index),
        )
        return item, path

    async def _download_page(
        self, page_url: str, dest: Path | None = None
    ) -> tuple[str, Path | None]:
        """解析单页并下载图片，返回 (图片 URL, 本地路径)。"""
        image, reload = await self.eh.image_url(page_url)
        path = await self.cache.download(image, self.eh.proxy, dest)
        if path is None and reload:
            # 当前图片服务器不可用，换一台服务器再试一次
            image, _ = await self.eh.image_url(page_url, reload)
            path = await self.cache.download(image, self.eh.proxy, dest)
        return image, path

    async def download_gallery(
        self, gallery: Gallery, dest: Path, concurrency: int
    ) -> tuple[list[Path], int]:
        """下载整个画廊到 dest，返回 (按页码排序的图片路径, 失败页数)。"""
        dest.mkdir(parents=True, exist_ok=True)
        pages = await self.eh.all_page_urls(gallery)
        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def fetch(number: int, url: str) -> Path | None:
            async with semaphore:
                try:
                    _, path = await self._download_page(url, dest / f"{number:05d}")
                    return path
                except BlockedError:
                    raise
                except (
                    EHentaiError,
                    HttpError,
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                ) as e:
                    logger.warning(
                        f"[random_pic] 画廊 {gallery.gid} 第 {number} 页失败: {e}"
                    )
                    return None

        results = await asyncio.gather(
            *(fetch(n, u) for n, u in pages), return_exceptions=True
        )
        for r in results:
            if isinstance(r, BaseException):
                raise r
        paths = [p for p in results if p]
        return paths, gallery.filecount - len(paths)
