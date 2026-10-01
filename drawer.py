"""随机抽卡：风格 × 分级 → 画廊池，随机跳转、复核过滤后取随机一页的图片。"""

import asyncio
import functools
import random
from dataclasses import dataclass, field, replace
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
from .filters import TagBlacklist, check_gallery, heavy_hit, heavy_search_term
from .imagecheck import is_colorful
from .models import ANIME, EXPLICIT, RATINGS, REAL, STYLES, ImageItem, PicRequest
from .net import HttpError, ImageCache
from .picacomic import SOURCE as PICA, Picacomic, supports_terms
from .sixteenk import SOURCE as SIXTEENK, SixteenK
from .tags import TagDB, TagIndex, search_term
from .workers import fill

EH = "ehentai"
SOURCES = (EH, SIXTEENK, PICA)
SOURCE_NAMES = {EH: "E-Hentai", SIXTEENK: "16K", PICA: "哔咔"}

# 二次元擦边：无 H 画廊里带这些「擦边」属性之一的（实测纯画集有 78% 不带任何人物标签，
# 多是设定集、线稿和教程）
ANIME_SENSITIVE_SEARCH = (
    '~female:swimsuit$ ~female:bikini$ ~female:"micro bikini$" ~female:lingerie$ '
    '~female:"bunny girl$" ~female:leotard$ ~female:"big breasts$" ~female:"huge breasts$" '
    '~female:"big ass$" ~female:pantyhose$ ~female:stockings$ ~female:"garter belt$" '
    '~female:"exposed clothing$" ~female:"school swimsuit$" ~female:bodysuit$ '
    '~female:"thigh high boots$" ~female:maid$ '
    '-other:"nudity only$" -other:"sketch lines$" -other:"how to$"'
)

DEFAULT_POOLS = {
    # 再要求是画集或图集，排除整页文字的漫画
    "anime_sensitive": {
        "categories": ["Non-H"],
        "search": ANIME_SENSITIVE_SEARCH,
        "require": ["other:artbook", "other:non-h imageset"],
    },
    # 单张 CG 比同人志、漫画的随机一页更适合抽卡；要求有人物标签，排除未打标签的杂图
    "anime_explicit": {
        "categories": ["Artist CG", "Game CG", "Image Set"],
        "search": '-other:"non-nude$" -other:"sketch lines$"',
        "require": ["female:*", "male:*", "mixed:*"],
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

# 每次随机跳转后最多尝试的画廊数，未通过复核或下载失败时换同一页的下一个；
# 画廊池有本地标签要求时多取一些（一页最多 25 个，一次元数据请求就能查完）
CANDIDATES_PER_JUMP = 3
CANDIDATES_WITH_REQUIRE = 10
# 二次元黑白页被丢弃时，同一画廊里最多多试这么多页
COLOR_RETRIES = 4
AUTHOR_NAMESPACES = ("artist", "cosplayer", "group")
# 随机角色模式下，每张图最多换这么多个角色（没有画廊的角色每个只花一次请求）
CHARACTER_TRIES = 10
# 说明文字中最多列出的作品、角色数
MAX_NAMES = 3


@dataclass
class Pool:
    categories: list[str]
    search: str
    # 画廊至少要带其中一个标签；「female:*」表示该命名空间下任意标签
    require: list[str] = field(default_factory=list)

    def missing_required(self, tags: list[str]) -> bool:
        if not self.require:
            return False
        tagset = {t.lower() for t in tags}
        namespaces = {t.split(":", 1)[0] for t in tagset}
        for want in self.require:
            want = want.strip().lower()
            if want.endswith(":*") and want[:-2] in namespaces:
                return False
            if want in tagset:
                return False
        return True


# 一个图集：同一画廊 / 帖子 / 本子里抽到的几张图，按页码排序
Album = list[tuple[ImageItem, Path]]


@dataclass
class FetchResult:
    albums: list[Album] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def images(self) -> list[tuple[ImageItem, Path]]:
        return [image for album in self.albums for image in album]


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
            require = conf.get(f"{key}_require")
            pools[(style, rating)] = Pool(
                [str(c).strip() for c in categories],
                default["search"] if search is None else str(search),
                list(default.get("require", []) if require is None else require),
            )
    return pools


def colorful(path: Path) -> bool:
    """图片损坏无法判断时按黑白处理（丢弃换页）。"""
    try:
        return is_colorful(path)
    except Exception as e:
        logger.warning(f"[random_pic] 无法判断是否彩图 {path}: {e!r}")
        return False


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
        color_only: bool = True,
        heavy: frozenset[str] = frozenset(),
        tags: TagDB | None = None,
        rating_enabled: bool = True,
        sixteenk: SixteenK | None = None,
        sixteenk_ratio: int = 0,
        pica: Picacomic | None = None,
        pica_ratio: int = 0,
        concurrency: int = 4,
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
        self.color_only = color_only
        self.heavy = heavy
        self.tags = tags
        self.rating_enabled = rating_enabled
        self.sixteenk = sixteenk
        self.sixteenk_ratio = min(max(sixteenk_ratio, 0), 100)
        self.pica = pica
        self.pica_ratio = min(max(pica_ratio, 0), 100)
        self.concurrency = max(1, concurrency)

    def _params(self, pool: Pool, terms: list[str]) -> dict:
        return build_search(
            pool.categories,
            pool.search,
            terms,
            exclude_ai=self.exclude_ai,
            min_stars=self.min_stars,
            min_pages=self.min_pages,
        )

    def _source_counts(self, req: PicRequest, allow_unrated: bool) -> dict[str, int]:
        """这次抽卡里每个图源（EH / SIXTEENK / PICA）分到的图集数。

        每个图集按配置的比例选图源。某个图源这次不能用时（不支持关键词、随机角色、未分级），
        它的比例按其余可用图源的比例分给它们。两个比例之和超过 100 时 E-Hentai 的比例为 0；
        可用图源的比例全为 0 时全部由 E-Hentai 抽。
        """
        sk = self.sixteenk_ratio if self.sixteenk is not None else 0
        pk = self.pica_ratio if self.pica is not None else 0
        weights = {EH: max(0, 100 - sk - pk)}
        if req.style == REAL and not req.random_character:
            # 16K 不能搜索也没有分级；哔咔只能搜普通关键词
            if sk and not req.tags and allow_unrated:
                weights[SIXTEENK] = sk
            if pk and supports_terms(req.tags):
                weights[PICA] = pk
        counts = dict.fromkeys(SOURCES, 0)
        if sum(weights.values()) <= 0 or len(weights) == 1:
            counts[EH] = req.count
            return counts
        for name in random.choices(list(weights), list(weights.values()), k=req.count):
            counts[name] += 1
        return counts

    def _check_terms(self, raw: list[str], terms: list[str]) -> str | None:
        """关键词闸门，对所有图源生效：原词和翻译后的标签都检查。"""
        positive = [t for t in [*raw, *terms] if not t.startswith("-")]
        term = self.blacklist.hit(positive)
        if term:
            return f"关键词命中黑名单 {term}"
        heavy = next(
            (h for t in positive if (h := heavy_search_term(t, self.heavy))), None
        )
        if heavy:
            return f"关键词是已屏蔽的重口标签 {heavy}"
        return None

    async def draw(
        self, req: PicRequest, is_private: bool, allow_unrated: bool = False
    ) -> FetchResult:
        """allow_unrated：本次请求能否使用没有分级的图源（见 filters.unrated_allowed）。

        req.count 个图集、每个图集 req.per_album 张（画廊页数不够时少几张）。各图源同时抽取；
        16K、哔咔没抽够的图集在它们结束时马上由 E-Hentai 补上。图集之间打乱顺序，图集内按页码排序。
        """
        result = FetchResult()
        index = await self.tags.get() if self.tags else None
        terms = [index.translate(t) for t in req.tags] if index else list(req.tags)
        reason = self._check_terms(req.tags, terms)
        if reason:
            result.errors.append(reason)
            return result

        counts = self._source_counts(req, allow_unrated)
        ehentai = functools.partial(
            self._draw_ehentai, is_private=is_private, terms=terms, index=index
        )

        async def run(name: str, job) -> tuple[list, list[str]]:
            """抽一个图源；16K、哔咔没抽够时马上由 E-Hentai 补，不等其他图源。"""
            try:
                albums, errors = await job
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"[random_pic] {SOURCE_NAMES[name]} 抽取出错: {e!r}", exc_info=e
                )
                albums, errors = [], [f"{SOURCE_NAMES[name]} 出错：{e!r}"]
            short = counts[name] - len(albums)
            if name != EH and short > 0:
                more, more_errors = await ehentai(replace(req, count=short))
                albums, errors = albums + more, errors + more_errors
            return albums, errors

        jobs = []
        if counts[SIXTEENK]:
            jobs.append(
                run(
                    SIXTEENK,
                    self.sixteenk.draw(
                        counts[SIXTEENK], req.per_album, req.rating, self.concurrency
                    ),
                )
            )
        if counts[PICA]:
            jobs.append(
                run(
                    PICA,
                    self.pica.draw(
                        counts[PICA],
                        req.per_album,
                        req.rating,
                        req.tags,
                        self.concurrency,
                    ),
                )
            )
        if counts[EH]:
            jobs.append(run(EH, ehentai(replace(req, count=counts[EH]))))
        for albums, errors in await asyncio.gather(*jobs):
            result.albums.extend(albums)
            result.errors.extend(errors)
        random.shuffle(result.albums)
        return result

    async def _draw_ehentai(
        self,
        req: PicRequest,
        is_private: bool,
        terms: list[str],
        index: TagIndex | None,
    ) -> tuple[list[Album], list[str]]:
        result = FetchResult()
        pool = self.pools[(req.style, req.rating)]
        if req.random_character and (index is None or not index.characters):
            result.errors.append("标签库不可用，无法随机角色")
            return result.albums, result.errors
        try:
            self._params(pool, terms)
        except ValueError as e:
            result.errors.append(f"画廊池配置错误：{e}")
            return result.albums, result.errors

        seen: set[int] = set()
        discarded = 0
        for _ in range(self.max_rounds):
            need = req.count - len(result.albums)
            if need <= 0:
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
        return result.albums, result.errors

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
        """并发跳转 need 次，每次跳转选中一个画廊、取 req.per_album 张。元数据一轮只查一次 API。

        E-Hentai 的页面请求仍受 request_interval 限速，并发省下的是等待网络和下载图片的时间。
        """
        semaphore = asyncio.Semaphore(self.concurrency)

        async def jump():
            async with semaphore:
                return await self._listing(req, pool, terms, index)

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
        metas = await self.eh.gdata([g for group in groups for g in group])

        async def take(group) -> tuple[Album, int]:
            """依次试分组里的画廊，取到图就停。返回 (图集, 丢弃的画廊数)。"""
            discarded = 0
            for gid, _ in group:
                gallery = metas.get(gid)
                reason = self._reject(gallery, req, is_private, pool)
                if reason:
                    logger.info(f"[random_pic] 丢弃画廊 {gid}: {reason}")
                    discarded += 1
                    continue
                async with semaphore:
                    items = await self._fetch_images(
                        gallery, req, index, req.per_album
                    )
                if items:
                    return items, discarded
                discarded += 1
            return [], discarded

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
            if outcome[0]:
                result.albums.append(outcome[0])
            discarded += outcome[1]
        if blocked is not None:
            # 已经取到的图片保留，IP 被封、额度用尽时停止抽卡
            raise blocked
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
        self, gallery: Gallery | None, req: PicRequest, is_private: bool, pool: Pool
    ) -> str | None:
        if gallery is None:
            return "元数据缺失"
        if gallery.expunged:
            return "画廊已被删除"
        if gallery.filecount <= 0:
            return "画廊没有图片"
        reason = check_gallery(
            gallery.category,
            gallery.tags,
            req.style,
            req.rating,
            is_private,
            self.blacklist,
            self.rating_enabled,
        )
        if reason is None and (heavy := heavy_hit(gallery.tags, self.heavy)):
            reason = f"重口标签 {heavy}"
        if reason is None and pool.missing_required(gallery.tags):
            reason = "缺少画廊池要求的标签"
        return reason

    def _page_candidates(self, gallery: Gallery, req: PicRequest, n: int) -> list[int]:
        """按尝试顺序返回至多 n 个不重复的页（从 0 开始）。"""
        if self.cover_only:
            return list(range(min(n, gallery.filecount)))
        # R18 画廊通常从穿着完整开始，跳过前一段
        start = (
            int(gallery.filecount * self.explicit_skip) if req.rating == EXPLICIT else 0
        )
        population = range(start, gallery.filecount)
        return random.sample(population, min(n, len(population)))

    async def _fetch_images(
        self, gallery: Gallery, req: PicRequest, index: TagIndex | None, n: int
    ) -> list[tuple[ImageItem, Path]]:
        """取 n 张图（多张时并发），按页码排序后返回，保证同一画廊的多张图按顺序发送。

        二次元只要彩图时，黑白页（线稿、黑白漫画）丢弃后换一页，最多多试 COLOR_RETRIES 页。
        """
        check_color = self.color_only and req.style == ANIME
        tries = n + (COLOR_RETRIES if check_color else 0)
        candidates = iter(self._page_candidates(gallery, req, tries))

        async def attempt() -> tuple[ImageItem, Path] | None:
            page_index = next(candidates, None)
            if page_index is None:
                return None
            item = await self._fetch_image(gallery, req, index, page_index)
            if item is None:
                return None
            if check_color and not await asyncio.to_thread(colorful, item[1]):
                logger.info(f"[random_pic] 丢弃黑白页 {item[0].page_url}")
                item[1].unlink(missing_ok=True)
                return None
            return item

        items: list[tuple[ImageItem, Path]] = []
        await fill(items, n, tries, self.concurrency, attempt)
        return sorted(items, key=lambda it: it[0].page)

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
