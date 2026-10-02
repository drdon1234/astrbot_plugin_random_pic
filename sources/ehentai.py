"""E-Hentai 图源（只用于三次元）：分级 → 画廊池，随机跳转、复核过滤后取随机几页。

真人内容基本只在 Cosplay、Asian Porn 两个分类里（Western、Image Set、Non-H 几乎都是绘画，
Misc 七成是 3D 渲染），站上没有通用的「真人照片」标签。分级只看画廊级的标签（2026-10 抽样
500 个 Cosplay 画廊）：约 20% 带裸露 / 性内容标签，为 R18；约 78% 带 other:non-nude，为擦边；
约 2% 两者都没有，里面既有性内容也有穿着完整的写真，无法判定、直接丢弃。打码类标签只用于露出
性器官的画廊，和 non-nude 同时出现时（约 0.5%）按 R18 处理。

两个画廊池就按这个分级搜索：擦边池加入 Asian Porn（无露点的多是杂志写真）；R18 池不加，
因为其中有业余和流出内容，真人年龄也无法靠标签过滤。
"""

import asyncio
import random
import re
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from ..filters import ContentFilter
from ..models import (
    EXPLICIT,
    REAL,
    SENSITIVE,
    Album,
    DrawContext,
    DrawOptions,
    Work,
    WorkRef,
)
from ..net import NETWORK_ERRORS, ImageCache
from ..tags import TagIndex, search_term
from .base import Source
from .ehentai_api import (
    BlockedError,
    EHentai,
    EHentaiError,
    Gallery,
    NoHitsError,
    build_search,
)

REAL_CATEGORIES = ("Cosplay", "Asian Porn")
NON_NUDE = "other:non-nude"
# 裸露或性内容的证据，优先级高于 non-nude
EXPLICIT_TAGS = (
    "other:nudity only",
    "other:uncensored",
    "other:mosaic censorship",
    "other:full censorship",
    "other:hardcore",
    "other:no penetration",
    "other:object insertion only",
)


@dataclass(frozen=True)
class Pool:
    categories: tuple[str, ...]
    search: str


POOLS = {
    SENSITIVE: Pool(
        REAL_CATEGORIES, f'{search_term("other", "non-nude")} -other:"nudity only$"'
    ),
    # 带任一裸露 / 性内容标签（~ 表示「或」）
    EXPLICIT: Pool(
        ("Cosplay",),
        " ".join("~" + search_term(*t.split(":", 1)) for t in EXPLICIT_TAGS),
    ),
}

# 抽取轮数：画廊被复核丢弃、取图失败时重新随机跳转
MAX_ROUNDS = 3
# 每次随机跳转后最多尝试的画廊数，未通过复核或下载失败时换同一页的下一个
CANDIDATES_PER_JUMP = 3
AUTHOR_NAMESPACES = ("artist", "cosplayer", "group")
# 随机角色模式下，每个图集最多换这么多个角色（没有画廊的角色每个只花一次请求）
CHARACTER_TRIES = 10
# 说明文字中最多列出的作品、角色数
MAX_NAMES = 3

Picture = tuple[int, Path]


def gallery_rating(tags: list[str]) -> str | None:
    """按画廊标签判定分级，无法判定时为 None。"""
    tagset = {t.lower() for t in tags}
    if tagset & set(EXPLICIT_TAGS):
        return EXPLICIT
    if NON_NUDE in tagset:
        return SENSITIVE
    return None


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


class EHentaiSource(Source):
    key = "ehentai"  # ExHentai 也用它
    style = REAL
    intro = "Cosplay 与写真画廊，支持 E-Hentai 标签语法和 /随机角色"
    link_re = re.compile(r"https?://(?:e-hentai|exhentai)\.org/g/(\d+)/([0-9a-f]{10})")

    def __init__(
        self,
        api: EHentai,
        cache: ImageCache,
        content: ContentFilter,
        opts: DrawOptions,
        *,
        exclude_ai: bool,
        min_stars: int,
        min_pages: int,
    ):
        super().__init__(cache, content, opts)
        self.api = api
        self.name = api.name
        self.exclude_ai = exclude_ai
        self.min_stars = min_stars
        self.min_pages = min_pages

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
        pool = POOLS[ctx.req.rating]
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
            except NETWORK_ERRORS as e:
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
        for listing in listings:
            if isinstance(listing, BaseException):
                continue
            candidates = [g for g in listing if g[0] not in seen]
            random.shuffle(candidates)
            group = candidates[:CANDIDATES_PER_JUMP]
            # 几次跳转可能落在同一页，同一个画廊只给一个分组
            seen.update(gid for gid, _ in group)
            groups.append(group)
        metas = await self.api.gdata([g for group in groups for g in group])

        async def take(group) -> tuple[Album | None, int]:
            """依次试分组里的画廊，取到图就停。返回 (图集, 丢弃的画廊数)。"""
            discarded = 0
            for gid, _ in group:
                gallery = metas.get(gid)
                reason = self._reject(gallery, ctx)
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

    def _reject(self, gallery: Gallery | None, ctx: DrawContext) -> str | None:
        if gallery is None:
            return "元数据缺失"
        if gallery.expunged:
            return "画廊已被删除"
        if gallery.filecount <= 0:
            return "画廊没有图片"
        if gallery.category not in REAL_CATEGORIES:
            return f"不是三次元分类（{gallery.category}）"
        return self.rating_reason(
            gallery_rating(gallery.tags), ctx
        ) or self.content.tags_reason(gallery.tags)

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
        """从画廊取 ctx.req.per_album 张（多张时并发），失败的页换别的页补上，按页码排序。"""

        async def picture(index: int) -> Picture | None:
            try:
                page, page_url = await self.api.page_url(gallery, index)
                path = await self._download_page(page_url)
            except BlockedError:
                raise
            except NETWORK_ERRORS + (EHentaiError,) as e:
                logger.warning(f"[random_pic] 画廊 {gallery.gid} 取图失败: {e}")
                return None
            return (page, path) if path else None

        return await self.pick_pages(
            gallery.filecount, ctx.req.per_album, gallery_rating(gallery.tags), picture
        )

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

    # ---- 整本打包 ----

    async def work(self, ref: WorkRef) -> Work | None:
        """查询画廊，不存在或已被删除时返回 None。分级和过滤与抽图相同，没有标签的不予打包。"""
        gallery = await self.api.gallery(int(ref.id), ref.token)
        if gallery is None or gallery.expunged:
            return None
        return Work(
            ref=WorkRef(self.key, str(gallery.gid), gallery.token),
            title=gallery.title,
            pages=gallery.filecount,
            rating=gallery_rating(gallery.tags),
            blocked=self.content.tags_reason(gallery.tags),
            data=gallery,
        )

    async def page_items(self, work: Work) -> list[str]:
        """画廊全部单页的地址。"""
        return [url for _, url in await self.api.all_page_urls(work.data)]

    async def download_page(self, page_url: str, dest: Path) -> Path | None:
        """IP 被封、额度用尽时整本打包停止，其他错误只算这一页失败。"""
        try:
            return await self._download_page(page_url, dest)
        except BlockedError:
            raise
        except NETWORK_ERRORS + (EHentaiError,) as e:
            logger.warning(f"[random_pic] 下载画廊单页 {page_url} 失败: {e}")
            return None
