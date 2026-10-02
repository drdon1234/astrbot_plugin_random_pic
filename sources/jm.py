"""禁漫天堂（JMComic）三次元图源：只用「其他類 › 角色扮演」（Cosplay）分类，网页版。

网页版的域名经常更换（18comic.vip、18comic.org 等），当前能用的域名填在配置里，通常要走代理。
- /albums/another/sub/cosplay?o=mr&page=N：分类列表，按上架时间排序，每页 80 本，带点赞数和
  部分标签；页码超出时返回最后一页（翻页栏里的总页数会偏大，以实际返回的页码为准）；
- /search/photos/another/sub/cosplay?search_query=关键词&o=mr&page=N：在这个分类里搜索，
  格式同分类列表，搜不到时页面里有「找不到和 … 相符的內容」，后面是推荐的本子；
- /album/{id}：本子详情（完整的标签、作品、登场人物、章节列表），不存在时跳转到 /error/…；
- /photo/{id}：一个章节的图片列表和切割参数 scramble_id。
网页有 Cloudflare 校验：只认浏览器的 TLS 指纹，aiohttp 一律 403，所以网页用 curl_cffi
模拟浏览器请求。哪个指纹能通过和 curl_cffi 的版本有关，而且时好时坏，被拦时依次换指纹重试。
图片服务器（cdn-msp*.18comic.vip）没有校验，用插件共用的 aiohttp 下载。
详情页和章节页的解析用 jmcomic 库（与 astrbot_plugin_jm_parser 相同的依赖）。

图片被横向切成若干条打乱顺序，条数由本子 id 和文件名算出（jmcomic 的 JmImageTool.get_num），
下载后还原成 JPEG。

分级：JM 没有分级，上传者偶尔给无露点的写真打「无H」「无漏」之类的标签，带这类标签的算擦边，
其余算 R18。2026-10 抽样目检（点赞数 500 以上 48 本，每本随机 3 张）：全部达到擦边以上，
约七成有露点，48 本都没有这类标签，搜索也搜不到这些标签，所以 JM 实际上只用于三次元 R18。
分类末尾约 20%（本子 id 5 万以下）是早年的日本素人摄影，点赞多在 100 以下、画质差，
按点赞数下限过滤。
"""

import asyncio
import html
import random
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException
from jmcomic import JmcomicText, JmImageTool
from jmcomic.jm_exception import JmcomicException
from PIL import Image

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
from ..net import TIMEOUT, ImageCache
from ..util import TTLCache, shared
from .base import Source, SourceError, has_tag_syntax, keyword_and_excludes

CATEGORY_PATH = "/albums/another/sub/cosplay"
SEARCH_PATH = "/search/photos/another/sub/cosplay"
# 依次尝试的浏览器指纹；记住上一次通过的，下次先用它
FINGERPRINTS = ("chrome", "safari", "safari18_0", "safari17_0", "chrome131", "edge101")
# 无露点的标签（小写），带其一的本子算擦边
SENSITIVE_TAGS = frozenset(
    {"无h", "無h", "非h", "无漏", "無漏", "不漏", "无露点", "無露點", "non-nude"}
)
# 说明文字里不列出的标签，以及最多列出的标签数
PLAIN_TAGS = frozenset({"cosplay", "cospaly", "csopaly", "copslay", "cosplaqy"})
MAX_CAPTION_TAGS = 4
# 总页数缓存：分类列表一小时，搜索结果 10 分钟
LISTING_TTL = 3600
SEARCH_TTL = 600
PAGES_CACHE_SIZE = 256
# 每个图集最多翻这么多页列表
LISTINGS_PER_ALBUM = 4
# 每页列表最多查这么多本的详情（标签不符时换下一本）
DETAIL_TRIES = 3
# 还原后的图片存成 JPEG 的质量
JPEG_QUALITY = 95
# 点赞数下限：分类末尾约两成是早年的日本素人摄影，点赞多在 100 以下、画质差，
# 500 能滤掉其中绝大多数，其他本子只损失约一成
MIN_LIKES = 500
# 排除的标签（小写，整个标签匹配）：3D 渲染、动图、重口
EXCLUDE_TAGS = frozenset({"3d", "動圖", "重口"})
AI_TAGS = frozenset({"ai", "ai繪圖", "ai生成"})

NOT_FOUND = "找不到和"
ITEM_SPLIT_RE = re.compile(r'<div class="col-[^"]*\blist-col\b[^"]*">')
LIKES_RE = re.compile(r'id="albim_likes_(\d+)"[^>]*>\s*([^<]*?)\s*<')
TITLE_RE = re.compile(r'<span class="video-title[^"]*">\s*(.*?)\s*</span>', re.S)
TAG_RE = re.compile(r'class="tag">\s*([^<]*?)\s*</a>')
PAGINATION_RE = re.compile(r'<ul class="pagination[^"]*">(.*?)</ul>', re.S)
PAGE_RE = re.compile(r"[?&]page=(\d+)")
ACTIVE_RE = re.compile(r'<li class="active">\s*<span>(\d+)</span>')
COUNT_RE = re.compile(r"^([\d.]+)\s*([KkMm]?)$")


class JMError(SourceError):
    pass


@dataclass
class Item:
    """列表里的一本。"""

    id: str
    title: str
    likes: int
    tags: list[str]


@dataclass
class Listing:
    items: list[Item]
    pages: int  # 翻页栏里最大的页码
    current: int  # 实际返回的页码（页码超出时是最后一页）


def parse_count(text: str) -> int:
    """「7K」「1.2M」「856」这样的点赞数。"""
    match = COUNT_RE.match(text.strip())
    if not match:
        return 0
    scale = {"": 1, "k": 1000, "m": 1000_000}[match.group(2).lower()]
    return int(float(match.group(1)) * scale)


def parse_listing(text: str, page: int) -> Listing:
    if NOT_FOUND in text:
        return Listing([], 0, page)
    items = []
    for block in ITEM_SPLIT_RE.split(text)[1:]:
        likes = LIKES_RE.search(block)
        if not likes:
            continue
        title = TITLE_RE.search(block)
        items.append(
            Item(
                id=likes.group(1),
                title=html.unescape(title.group(1)) if title else "",
                likes=parse_count(likes.group(2)),
                tags=[html.unescape(t) for t in TAG_RE.findall(block) if t],
            )
        )
    pagination = PAGINATION_RE.search(text)
    numbers = (
        [int(n) for n in PAGE_RE.findall(pagination.group(1))] if pagination else []
    )
    active = ACTIVE_RE.search(pagination.group(1)) if pagination else None
    current = int(active.group(1)) if active else page
    return Listing(items, max([current, *numbers]), current)


def content_rating(tags: list[str]) -> str:
    return SENSITIVE if SENSITIVE_TAGS & {t.lower() for t in tags} else EXPLICIT


def unscramble(src: Image.Image, segments: int) -> Image.Image:
    """把切成 segments 条、上下颠倒排列的图片还原（同 jmcomic 的 JmImageTool.decode_and_save）。"""
    width, height = src.size
    out = Image.new("RGB", (width, height))
    step, over = height // segments, height % segments
    for i in range(segments):
        move = step + (over if i == 0 else 0)
        y_src = height - step * (i + 1) - over
        y_dst = step * i + (0 if i == 0 else over)
        out.paste(src.crop((0, y_src, width, y_src + move)), (0, y_dst))
    return out


def restore(path: Path, segments: int) -> Path:
    """还原下载的图片，存成 JPEG 并删掉原文件。segments 为 0 或是动图时原样返回。"""
    if segments <= 0 or path.suffix.lower() == ".gif":
        return path
    with Image.open(path) as image:
        src = image.convert("RGB")
    target = path.with_name(path.stem + ".jpg")
    unscramble(src, segments).save(target, "JPEG", quality=JPEG_QUALITY)
    if target != path:
        path.unlink(missing_ok=True)
    return target


@dataclass
class Page:
    """一个章节里的一张图。"""

    url: str
    segments: int


def photo_pages(photo) -> list[Page]:
    pages = []
    for index in range(len(photo.page_arr)):
        image = photo.create_image_detail(index)
        pages.append(Page(image.img_url, JmImageTool.get_num_by_detail(image)))
    return pages


class JMComicSource(Source):
    key = "jmcomic"
    name = "禁漫天堂"
    style = REAL
    intro = "Cosplay 分类，只用于 R18，可搜普通关键词"
    # 禁漫的域名经常更换，认域名里带 18comic、jm 的
    link_re = re.compile(
        r"https?://[\w.-]*(?:18comic|jm)[\w.-]*/album/(\d+)(?![\w%-])", re.ASCII
    )

    def __init__(
        self,
        cache: ImageCache,
        content: ContentFilter,
        opts: DrawOptions,
        *,
        domain: str,
        proxy: str | None,
        timeout: float = TIMEOUT,
        min_likes: int = MIN_LIKES,
        exclude_tags: frozenset[str] = EXCLUDE_TAGS,
    ):
        super().__init__(cache, content, opts)
        self.domain = domain
        self.proxy = proxy or None
        self.timeout = timeout
        self.min_likes = min_likes
        self.exclude_tags = frozenset(t.lower() for t in exclude_tags) | (
            AI_TAGS if content.block_ai else frozenset()
        )
        self._sessions: dict[str, AsyncSession] = {}
        self._fingerprint = 0
        # 关键词（不带时为 ""，即整个分类）→ (总页数, 没有一本达到点赞数下限的页码)；
        # 后者是分类末尾的老图，随机翻页时跳过，总页数变了就重新记
        self._pages = TTLCache(LISTING_TTL, PAGES_CACHE_SIZE)
        self._inflight: dict = {}

    @property
    def base(self) -> str:
        return f"https://{self.domain}"

    def album_url(self, album_id: str) -> str:
        return f"{self.base}/album/{album_id}"

    def accepts(self, ctx: DrawContext) -> bool:
        """三次元 R18（关闭内容分级时不限），不支持随机角色和 E-Hentai 标签语法。"""
        req = ctx.req
        return (
            req.style == REAL
            and not req.random_character
            and req.rating == EXPLICIT
            and not has_tag_syntax(req.keywords)
        )

    # ---- 网页请求 ----

    async def _fetch(self, url: str, fingerprint: str) -> tuple[int, str, str]:
        """用指定的浏览器指纹请求，返回 (状态码, 最终地址, 文本)。"""
        session = self._sessions.get(fingerprint)
        if session is None:
            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
            session = AsyncSession(
                impersonate=fingerprint, proxies=proxies, timeout=self.timeout
            )
            self._sessions[fingerprint] = session
        resp = await session.get(url)
        return resp.status_code, str(resp.url), resp.text

    async def _get(self, path: str) -> tuple[str, str]:
        """请求网页，返回 (最终地址, 文本)。被 Cloudflare 拦截或连接出错时换指纹重试。"""
        url = self.base + path
        last = ""
        count = len(FINGERPRINTS)
        for i in range(count):
            index = (self._fingerprint + i) % count
            try:
                status, final, text = await self._fetch(url, FINGERPRINTS[index])
            except RequestException as e:
                last = f"{e!r}"[:200]
                continue
            if status == 200:
                self._fingerprint = index
                return final, text
            last = f"HTTP {status}"
            if status not in (403, 429, 503):
                break
        raise JMError(f"禁漫请求失败（{last}），检查域名 {self.domain} 和代理")

    async def close(self):
        for session in self._sessions.values():
            try:
                await session.close()
            except Exception as e:
                logger.debug(f"[random_pic] 关闭禁漫会话出错: {e!r}")
        self._sessions.clear()

    # ---- 列表 ----

    def _listing_path(self, keyword: str, page: int) -> str:
        if keyword:
            query = urlencode({"search_query": keyword, "o": "mr", "page": page})
            return f"{SEARCH_PATH}?{query}"
        return f"{CATEGORY_PATH}?{urlencode({'o': 'mr', 'page': page})}"

    async def _listing(self, keyword: str, page: int) -> Listing:
        _, text = await self._get(self._listing_path(keyword, page))
        return parse_listing(text, page)

    def _remember(self, keyword: str, pages: int) -> set[int]:
        """记下总页数，返回这些页里已知没有达标本子的页码。"""
        old = self._pages.get(keyword)
        barren = old[1] if old and old[0] == pages else set()
        self._pages.put(keyword, (pages, barren), SEARCH_TTL if keyword else None)
        return barren

    async def _random_listing(self, keyword: str) -> tuple[int, list[Item]]:
        """随机翻一页，返回 (页码, 本子)。总页数不知道时先取第一页；页码超出时以实际返回的
        页码修正总页数。"""
        cached = self._pages.get(keyword)
        first = None
        if cached:
            pages, barren = cached
        else:
            first = await shared(
                self._inflight, keyword, lambda: self._listing(keyword, 1)
            )
            pages = first.pages if first.items else 0
            barren = self._remember(keyword, pages)
        if pages <= 0:
            raise JMError(
                f"禁漫 Cosplay 分类里搜不到「{keyword}」"
                if keyword
                else "禁漫 Cosplay 分类为空"
            )
        choices = [p for p in range(1, pages + 1) if p not in barren]
        if not choices:
            raise JMError(f"禁漫没有点赞数达到 {self.min_likes} 的本子")
        page = random.choice(choices)
        listing = first if first is not None and page == 1 else None
        if listing is None:
            listing = await self._listing(keyword, page)
            if listing.current < page:
                self._remember(keyword, listing.current)
        return listing.current, listing.items

    # ---- 过滤 ----

    def _item_reason(self, item: Item, exclude: list[str]) -> str | None:
        """列表阶段的过滤，省掉查详情。"""
        if item.likes < self.min_likes:
            return f"点赞数 {item.likes} 低于 {self.min_likes}"
        tags = [t.lower() for t in item.tags]
        hit = next((t for t in tags if t in self.exclude_tags), None)
        if hit:
            return f"带排除的标签 {hit}"
        term = self.content.blacklist.hit([*item.tags, item.title])
        if term:
            return f"命中黑名单 {term}"
        text = "\n".join([item.title, *tags]).lower()
        if any(word in text for word in exclude):
            return "命中排除的关键词"
        return None

    def _blocked(self, album) -> str | None:
        """本子详情的黑名单、重口和排除标签检查，抽图和 /pdf 共用。"""
        hit = next((t for t in album.tags if t.lower() in self.exclude_tags), None)
        if hit:
            return f"带排除的标签 {hit}"
        return self.content.plain_tags_reason(
            [*album.tags, *album.works, *album.actors]
        ) or self.content.text_reason([album.name, *album.authors])

    def _reject(self, album, ctx: DrawContext, exclude: list[str]) -> str | None:
        reason = self.rating_reason(content_rating(album.tags), ctx) or self._blocked(
            album
        )
        if reason:
            return reason
        words = [album.name, *album.tags, *album.works, *album.actors, *album.authors]
        if any(word in "\n".join(words).lower() for word in exclude):
            return "命中排除的关键词"
        return None

    # ---- 详情 ----

    async def _album(self, album_id: str):
        """本子详情，不存在时返回 None。"""
        final, text = await self._get(f"/album/{album_id}")
        if "/error/" in final:
            return None
        return JmcomicText.analyse_jm_album_html(text)

    async def _photo_pages(self, photo_id: str) -> list[Page]:
        final, text = await self._get(f"/photo/{photo_id}")
        if "/error/" in final:
            return []
        return photo_pages(JmcomicText.analyse_jm_photo_html(text))

    async def _download(self, page: Page, dest: Path | None = None) -> Path | None:
        path = await self.cache.download(page.url, dest)
        if path is None:
            return None
        try:
            return await asyncio.to_thread(restore, path, page.segments)
        except OSError as e:
            logger.warning(f"[random_pic] 还原禁漫图片失败 {page.url}: {e!r}")
            path.unlink(missing_ok=True)
            return None

    # ---- 抽图 ----

    async def _pick(
        self, ctx: DrawContext, keyword: str, exclude: list[str], seen: set[str]
    ):
        """随机翻一页，挑一本点赞数、标签、分级都符合的本子。"""
        page, items = await self._random_listing(keyword)
        if not any(item.likes >= self.min_likes for item in items):
            cached = self._pages.get(keyword)
            if cached:
                cached[1].add(page)
        items = [
            item
            for item in items
            if item.id not in seen and not self._item_reason(item, exclude)
        ]
        random.shuffle(items)
        for item in items[:DETAIL_TRIES]:
            seen.add(item.id)
            album = await self._album(item.id)
            if album is None:
                continue
            reason = self._reject(album, ctx, exclude)
            if reason:
                logger.info(f"[random_pic] 丢弃禁漫本子 {item.id}: {reason}")
                continue
            return album
        return None

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        """抽 n 本，每本随机取一个章节作为图集，并发抽取。关键词里 - 开头的词表示排除。"""
        keyword, exclude = keyword_and_excludes(ctx.req.keywords)
        seen: set[str] = set()

        async def attempt() -> Album | None:
            try:
                album = await self._pick(ctx, keyword, exclude, seen)
                return (
                    await self._make_album(album, ctx.req.per_album) if album else None
                )
            except JmcomicException as e:
                logger.warning(f"[random_pic] 解析禁漫网页失败: {e!r}")
                return None

        return await self.collect(n, LISTINGS_PER_ALBUM, attempt)

    async def _make_album(self, album, n: int) -> Album | None:
        """从随机的一个章节里取 n 张，按页码排序。"""
        photo_id = random.choice(album.episode_list)[0]
        pages = await self._photo_pages(photo_id)
        if not pages:
            return None

        async def download(index: int) -> tuple[int, Path] | None:
            path = await self._download(pages[index])
            return (index + 1, path) if path else None

        pictures = await self.pick_pages(
            len(pages), n, content_rating(album.tags), download
        )
        if not pictures:
            return None
        details = []
        if album.authors:
            details.append(f"作者：{'、'.join(album.authors[:MAX_CAPTION_TAGS])}")
        if album.actors:
            details.append(f"角色：{'、'.join(album.actors[:MAX_CAPTION_TAGS])}")
        shown = [t for t in album.tags if t.lower() not in PLAIN_TAGS]
        if shown:
            details.append(f"标签：{'、'.join(shown[:MAX_CAPTION_TAGS])}")
        details.append(f"本子：{self.album_url(album.album_id)}")
        return Album(
            source=self.name,
            title=album.name,
            total=len(pages),
            pictures=pictures,
            details=details,
            work=WorkRef(self.key, album.album_id),
        )

    # ---- /pdf ----

    async def work(self, ref: WorkRef) -> Work | None:
        album = await self._album(ref.id)
        if album is None:
            return None
        return Work(
            ref=WorkRef(self.key, album.album_id),
            title=album.name,
            pages=album.page_count,
            rating=content_rating(album.tags),
            blocked=self._blocked(album),
            data=[episode[0] for episode in album.episode_list],
        )

    async def page_items(self, work: Work) -> list[Page]:
        """依次取每个章节的图片列表，所有章节连起来编页码。"""
        pages: list[Page] = []
        for photo_id in work.data:
            pages += await self._photo_pages(photo_id)
        return pages

    async def download_page(self, page: Page, dest: Path) -> Path | None:
        return await self._download(page, dest)
