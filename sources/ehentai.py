"""E-Hentai 图源（只用于三次元）：分级 → 画廊池，随机跳转、复核过滤后取随机几页。"""

import asyncio
import random
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from astrbot.api import logger

from ..filters import ContentFilter, classify, rating_reason
from ..models import EXPLICIT, RATINGS, REAL, Album, DrawOptions, Work, WorkRef
from ..net import HttpError, ImageCache
from ..tags import TagIndex, search_term
from ..util import fill, pick_pages
from . import DrawContext
from .ehentai_api import (
    BlockedError,
    EHentai,
    EHentaiError,
    Gallery,
    NoHitsError,
    build_search,
)

# 抽取轮数：画廊被复核丢弃、取图失败时重新随机跳转
MAX_ROUNDS = 3
# 每次随机跳转后最多尝试的画廊数，未通过复核或下载失败时换同一页的下一个；
# 画廊池有本地标签要求时多取一些（一页最多 25 个，一次元数据请求就能查完）
CANDIDATES_PER_JUMP = 3
CANDIDATES_WITH_REQUIRE = 10
AUTHOR_NAMESPACES = ("artist", "cosplayer", "group")
# 随机角色模式下，每个图集最多换这么多个角色（没有画廊的角色每个只花一次请求）
CHARACTER_TRIES = 10
# 说明文字中最多列出的作品、角色数
MAX_NAMES = 3
NETWORK_ERRORS = (EHentaiError, HttpError, aiohttp.ClientError, asyncio.TimeoutError)

Picture = tuple[int, Path]


@dataclass
class Pool:
    categories: list[str]
    search: str
    # 画廊至少要带其中一个标签；「female:*」表示该命名空间下任意标签
    require: list[str]

    def missing_required(self, tags: list[str]) -> bool:
        if not self.require:
            return False
        tagset = {t.lower() for t in tags}
        namespaces = {t.split(":", 1)[0] for t in tagset}
        for want in self.require:
            if want.endswith(":*") and want[:-2] in namespaces:
                return False
            if want in tagset:
                return False
        return True


def build_pools(conf: dict) -> dict[str, Pool]:
    """conf 为配置的 pools 段，键为 real_<分级>_categories / _search / _require。"""
    pools = {}
    for rating in RATINGS:
        key = f"{REAL}_{rating}"
        pools[rating] = Pool(
            list(conf[f"{key}_categories"]),
            conf[f"{key}_search"].strip(),
            [t.lower() for t in conf[f"{key}_require"]],
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


class EHentaiSource:
    key = "ehentai"  # 图源键，ExHentai 也用它

    def __init__(
        self,
        api: EHentai,
        pools: dict[str, Pool],
        cache: ImageCache,
        content: ContentFilter,
        opts: DrawOptions,
        *,
        exclude_ai: bool,
        min_stars: int,
        min_pages: int,
    ):
        self.api = api
        self.name = api.name
        self.pools = pools
        self.cache = cache
        self.content = content
        self.opts = opts
        self.exclude_ai = exclude_ai
        self.min_stars = min_stars
        self.min_pages = min_pages

    def accepts(self, ctx: DrawContext) -> bool:
        return ctx.req.style == REAL

    def _params(self, pool: Pool, terms: list[str]) -> dict:
        return build_search(
            pool.categories,
            pool.search,
            terms,
            exclude_ai=self.exclude_ai,
            min_stars=self.min_stars,
            min_pages=self.min_pages,
        )

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        albums: list[Album] = []
        errors: list[str] = []
        if not self.accepts(ctx):
            return albums, [f"{self.name} 只用于三次元"]
        pool = self.pools[ctx.req.rating]
        try:
            self._params(pool, ctx.terms)
        except ValueError as e:
            return albums, [f"画廊池配置错误：{e}"]

        seen: set[int] = set()
        discarded = 0
        for _ in range(MAX_ROUNDS):
            need = n - len(albums)
            if need <= 0:
                break
            try:
                discarded += await self._round(ctx, pool, need, seen, albums)
            except EHentaiError as e:
                errors.append(str(e))
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, HttpError) as e:
                # 网络抖动（连接被重置、超时）多半是偶发的，下一轮重试
                logger.warning(f"[random_pic] {self.name} 请求失败: {e!r}")
                if f"{self.name} 请求失败" not in errors:
                    errors.append(f"{self.name} 请求失败")
        if discarded:
            errors.append(f"{discarded} 个画廊被过滤或下载失败")
        return albums, errors

    async def _round(
        self,
        ctx: DrawContext,
        pool: Pool,
        need: int,
        seen: set[int],
        albums: list[Album],
    ) -> int:
        """并发跳转 need 次，每次跳转选中一个画廊、取一个图集。元数据一轮只查一次 API。

        页面请求仍受 request_interval 限速，并发省下的是等待网络和下载图片的时间。
        返回丢弃的画廊数。
        """
        semaphore = asyncio.Semaphore(self.opts.concurrency)

        async def jump():
            async with semaphore:
                return await self._listing(ctx, pool)

        listings = await asyncio.gather(
            *(jump() for _ in range(need)), return_exceptions=True
        )
        failures = [r for r in listings if isinstance(r, BaseException)]
        for failure in failures:
            if isinstance(failure, (BlockedError, asyncio.CancelledError)):
                raise failure
        if len(failures) == len(listings):
            raise failures[0]
        groups = []
        per_jump = CANDIDATES_WITH_REQUIRE if pool.require else CANDIDATES_PER_JUMP
        for listing in listings:
            if isinstance(listing, BaseException):
                continue
            candidates = [g for g in listing if g[0] not in seen]
            random.shuffle(candidates)
            group = candidates[:per_jump]
            # 几次跳转可能落在同一页，同一个画廊只给一个分组
            seen.update(gid for gid, _ in group)
            groups.append(group)
        metas = await self.api.gdata([g for group in groups for g in group])

        async def take(group) -> tuple[Album | None, int]:
            """依次试分组里的画廊，取到图就停。返回 (图集, 丢弃的画廊数)。"""
            discarded = 0
            for gid, _ in group:
                gallery = metas.get(gid)
                reason = self._reject(gallery, ctx, pool)
                if reason:
                    logger.info(f"[random_pic] 丢弃画廊 {gid}: {reason}")
                    discarded += 1
                    continue
                async with semaphore:
                    pictures = await self._pictures(gallery, ctx)
                if pictures:
                    return self._album(gallery, pictures, ctx.index), discarded
                discarded += 1
            return None, discarded

        taken = await asyncio.gather(
            *(take(group) for group in groups), return_exceptions=True
        )
        discarded = 0
        blocked = None
        for outcome in taken:
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                blocked = blocked or outcome
                continue
            album, dropped = outcome
            if album:
                albums.append(album)
            discarded += dropped
        if blocked is not None:
            # 已经取到的图片保留，IP 被封、额度用尽时停止抽卡
            raise blocked
        return discarded

    async def _listing(self, ctx: DrawContext, pool: Pool) -> list[tuple[int, str]]:
        if not ctx.req.random_character:
            return await self.api.random_listing(self._params(pool, ctx.terms))
        for _ in range(CHARACTER_TRIES):
            character = search_term("character", ctx.index.random_character())
            try:
                return await self.api.random_listing(
                    self._params(pool, [*ctx.terms, character])
                )
            except NoHitsError:
                continue
        raise EHentaiError(f"连续 {CHARACTER_TRIES} 个随机角色都没有符合条件的画廊")

    def _reject(
        self, gallery: Gallery | None, ctx: DrawContext, pool: Pool
    ) -> str | None:
        if gallery is None:
            return "元数据缺失"
        if gallery.expunged:
            return "画廊已被删除"
        if gallery.filecount <= 0:
            return "画廊没有图片"
        style, rating = classify(gallery.category, gallery.tags)
        if style != ctx.req.style:
            return f"风格不符（{gallery.category}）"
        reason = rating_reason(
            rating, ctx.req.rating, ctx.is_private, self.opts.rating_enabled
        ) or self.content.tags_reason(gallery.tags)
        if reason is None and pool.missing_required(gallery.tags):
            reason = "缺少画廊池要求的标签"
        return reason

    def _album(
        self, gallery: Gallery, pictures: list[Picture], index: TagIndex | None
    ) -> Album:
        details = []
        author = gallery_author(gallery)
        if author:
            details.append(f"作者：{author}")
        for label, namespace in (("作品", "parody"), ("角色", "character")):
            names = gallery_names(gallery, namespace, index)
            if names:
                details.append(f"{label}：{'、'.join(names)}")
        info = [gallery.category, f"★{gallery.stars:.1f}" if gallery.stars else ""]
        if any(info):
            details.append(" · ".join(i for i in info if i))
        details.append(f"画廊：{self.api.gallery_url(gallery.gid, gallery.token)}")
        return Album(
            source=self.name,
            title=gallery.title,
            total=gallery.filecount,
            pictures=pictures,
            details=details,
            work=WorkRef(self.key, str(gallery.gid), gallery.token),
        )

    async def _pictures(self, gallery: Gallery, ctx: DrawContext) -> list[Picture]:
        """从画廊取 ctx.req.per_album 张（多张时并发），按页码排序。"""
        n = ctx.req.per_album
        explicit = classify(gallery.category, gallery.tags)[1] == EXPLICIT
        candidates = iter(
            pick_pages(
                gallery.filecount,
                n,
                from_start=self.opts.from_start,
                skip=self.opts.explicit_skip if explicit else 0.0,
            )
        )

        async def attempt() -> Picture | None:
            index = next(candidates, None)
            return None if index is None else await self._picture(gallery, index)

        pictures: list[Picture] = []
        await fill(pictures, n, n, self.opts.concurrency, attempt)
        return sorted(pictures)

    async def _picture(self, gallery: Gallery, index: int) -> Picture | None:
        """取画廊第 index 张（从 0 开始），返回 (实际页码, 本地文件)。"""
        try:
            page, page_url = await self.api.page_url(gallery, index)
            path = await self._download_page(page_url)
        except BlockedError:
            raise
        except NETWORK_ERRORS as e:
            logger.warning(f"[random_pic] 画廊 {gallery.gid} 取图失败: {e}")
            return None
        return (page, path) if path else None

    async def _download_page(
        self, page_url: str, dest: Path | None = None
    ) -> Path | None:
        """解析单页并下载图片。图片服务器不可用时换一台服务器再试一次。"""
        image, reload = await self.api.image_url(page_url)
        path = await self.cache.download(image, dest)
        if path is None and reload:
            image, _ = await self.api.image_url(page_url, reload)
            path = await self.cache.download(image, dest)
        return path

    async def work(self, ref: WorkRef) -> Work | None:
        """查询画廊，不存在或已被删除时返回 None。分级和过滤与抽图相同，没有标签的不予打包。"""
        gallery = await self.api.gallery(int(ref.id), ref.token)
        if gallery is None or gallery.expunged:
            return None
        return Work(
            ref=WorkRef(self.key, str(gallery.gid), gallery.token),
            title=gallery.title,
            pages=gallery.filecount,
            rating=classify(gallery.category, gallery.tags)[1],
            blocked=self.content.tags_reason(gallery.tags),
            data=gallery,
        )

    async def download_work(self, work: Work, dest: Path) -> tuple[list[Path], int]:
        return await self.download_gallery(work.data, dest)

    async def download_gallery(
        self, gallery: Gallery, dest: Path
    ) -> tuple[list[Path], int]:
        """下载整个画廊到 dest（文件以页码命名），返回 (按页码排序的图片路径, 失败页数)。"""
        dest.mkdir(parents=True, exist_ok=True)
        pages = await self.api.all_page_urls(gallery)
        semaphore = asyncio.Semaphore(self.opts.concurrency)

        async def fetch(number: int, url: str) -> Path | None:
            async with semaphore:
                try:
                    return await self._download_page(url, dest / f"{number:05d}")
                except BlockedError:
                    raise
                except NETWORK_ERRORS as e:
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
