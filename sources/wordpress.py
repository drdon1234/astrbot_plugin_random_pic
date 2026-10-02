"""WordPress 写真站三次元图源：CosplayTele、NudeCosplay（Cosplay，擦边和 R18）、
XiuRen（棚拍写真，只有擦边）与 PixiBB（Cosplay 和写真，分级混杂，只用于 R18）。

这些站都开放了 WordPress 的 REST 接口 /wp-json/wp/v2/posts：
- per_page=1&page=N：按发布时间排序的第 N 个帖子，响应头 X-WP-Total 是符合条件的帖子数，
  所以总数缓存后随机抽一个帖子只要一次请求；N 超出总数时返回 400；
- categories / categories_exclude / tags_exclude：按分类、标签筛选（分类和标签用数字 id）；
- search：在标题和正文里搜索，几个词要同时出现，带引号的词组整体匹配；
- _embed=wp:term：附带分类和标签的名称，用于黑名单过滤和说明文字；
- posts/{id}、posts?slug=：按 id 或链接里的 slug 取帖子，/pdf 整本打包用。
帖子正文里的 <img> 就是整套图（长边 1400~1600px 的 webp、jpg 或原图），图片没有防盗链。

分级来自站点的分类，2026-10 抽样目检：
- CosplayTele「Cosplay Ero」为擦边（24 张随机页 23 张达到擦边），「Cosplay Nude」为 R18
  （随机单页约 2/3 露点，其余是同一套里还穿着的页，和 E-Hentai 一样按整套定分级）；
  两个分类几乎不重叠，同时在两个分类时按 R18 处理。
- NudeCosplay 的分类和 CosplayTele 一样分「Ero Cosplay」「Nude」（21 张随机页 20 张擦边、
  1 张露点；Nude 约一半单页露点），帖子和 CosplayTele 约 1/4 重复。
- XiuRen 是秀人、尤蜜等工作室的写真（24 张随机页 21 张达到擦边），没有分级分类，全部按擦边。
- PixiBB 的「Cosplay」「Sexy Girls」分类都达到擦边，但两类都混着露点的套图（Cosplay 24 张
  随机页 6 张露点，Sexy Girls 多为内衣、也有露点），没有可靠的分级依据，全部按 R18 处理。
  排除街拍（偷拍）、OtherXXX（福利姬订阅内容）和 AI 生成 / AI 增强的帖子。
各站都排除站点自己标出的 AI 生成分类。
"""

import asyncio
import html
import json
import random
import re
from dataclasses import dataclass, field
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
from ..net import NETWORK_ERRORS, HttpClient, HttpError, ImageCache
from ..util import TTLCache
from .base import Source, SourceError, plain_word, split_terms

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


@dataclass(frozen=True)
class Site:
    key: str  # 图源键
    name: str  # 显示名
    host: str  # 接口所在的域名
    intro: str  # 帮助里的一行介绍
    ratings: dict[int, str] = field(default_factory=dict)  # 分类 id → 分级
    default_rating: str | None = None  # 没有分级分类的站点：所有帖子的分级
    categories: tuple[int, ...] = ()  # 没有分级分类的站点：只抽这些分类，空为不限
    exclude_categories: tuple[int, ...] = ()
    exclude_tags: tuple[int, ...] = ()
    # 站点标出的 AI 作品，「屏蔽 AI 作品」开启时排除
    ai_categories: tuple[int, ...] = ()
    ai_tags: tuple[int, ...] = ()
    # 关键词先经标签库翻译成英文（站点的标题和标签是英文时）
    translate: bool = False
    # 原词和翻译后的英文都搜，用帖子多的那个（标题中英文混杂时）
    bilingual: bool = False
    # 正文图片地址里必须有的片段，用来跳过站外图片
    image_marker: str = "/wp-content/uploads/"
    # 显示标题时去掉的部分（张数、站点名一类的后缀）
    title_noise: re.Pattern | None = None
    # 帖子链接的域名（任意子域名都认），空为 host
    domain: str = ""

    @property
    def api(self) -> str:
        return f"https://{self.host}/wp-json/wp/v2/posts"

    @property
    def link_re(self) -> re.Pattern:
        """帖子链接是一级路径的 slug（非 ASCII 字符是 %xx），分类、标签页是两级路径。"""
        domain = re.escape(self.domain or self.host)
        return re.compile(
            rf"https?://(?:[\w-]+\.)?{domain}/([\w%-]+)/?(?![\w%/-])", re.ASCII
        )


COSPLAYTELE = Site(
    key="cosplaytele",
    name="CosplayTele",
    host="cosplaytele.com",
    intro="Cosplay 写真，可搜角色、作品名",
    ratings={194: SENSITIVE, 193: EXPLICIT},  # Cosplay Ero / Cosplay Nude
    ai_categories=(589,),  # AI Art
    translate=True,
)
XIUREN = Site(
    key="xiuren",
    name="XiuRen",
    host="xiuren.biz",
    intro="工作室棚拍写真，只有擦边",
    default_rating=SENSITIVE,
    ai_categories=(1558,),  # AI Generated
    ai_tags=(1559, 1560),  # AI、AI Generated
)
NUDECOSPLAY = Site(
    key="nudecosplay",
    name="NudeCosplay",
    host="nudecosplay.biz",
    intro="Cosplay 写真，可搜角色、作品名",
    ratings={1790: SENSITIVE, 1794: EXPLICIT},  # Ero Cosplay / Nude
    ai_categories=(3373,),  # Waifu AI
    translate=True,
    title_noise=re.compile(r"\s*/nudecosplay\.biz/\s*$", re.I),
)
PIXIBB = Site(
    key="pixibb",
    name="PixiBB",
    host="sexy.pixibb.com",
    intro="Cosplay 与写真，只用于 R18，可搜角色、作品、模特名",
    default_rating=EXPLICIT,
    categories=(10, 112),  # Cosplay、Sexy Girls
    exclude_categories=(74, 209),  # Anime、Toon Girls
    exclude_tags=(3472, 3821),  # 精选街拍作品（偷拍）、OtherXXX（福利姬）
    ai_categories=(24, 119),  # AI Lookbook、Almost Real
    ai_tags=(3535, 3881),  # AIGirl、AI Enhanced
    bilingual=True,
    image_marker=".pixibb.com/",
    domain="pixibb.com",  # sexy.、cosplay.、hub. 等子域名是同一个站
    # 例如「(54 photos + 1 video) Sexy Cosplay」
    title_noise=re.compile(
        r"\s*\(\d+ photos?(?: \+ \d+ videos?)?\)|\s*\bSexy (?:Cosplay|Girls?)\s*$", re.I
    ),
)
SITES = (COSPLAYTELE, XIUREN, NUDECOSPLAY, PIXIBB)


class WordPressError(SourceError):
    pass


def search_terms(terms: list[str]) -> tuple[list[str], list[str]] | None:
    """返回 (搜索词, 排除词)，都转成小写。

    E-Hentai 标签写法取出标签名（character:"hu tao$" → hu tao）；其他带 : $ " 的写法搜不了，返回 None。
    """
    out: tuple[list[str], list[str]] = ([], [])
    for words, target in zip(split_terms(terms), out):
        for term in words:
            if not term.strip():
                continue
            word = plain_word(term)
            if word is None:
                return None
            target.append(word.lower())
    return out


def search_query(words: list[str]) -> str:
    """多个词的标签名整体搜索，加引号。"""
    return " ".join(f'"{w}"' if " " in w else w for w in words)


def post_images(content: str, marker: str = "/wp-content/uploads/") -> list[str]:
    """正文里的图片地址（按出现顺序去重），跳过缩略图、非图片和地址里没有 marker 的站外图片。"""
    urls: list[str] = []
    for tag in IMG_RE.findall(content):
        attrs = {k.lower(): v for k, v in ATTR_RE.findall(tag)}
        src = html.unescape(attrs.get("src", "")).strip()
        path = src.split("?", 1)[0].lower()
        if not src.startswith("http") or marker not in path:
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


class WordPressSource(Source):
    style = REAL

    def __init__(
        self,
        site: Site,
        http: HttpClient,
        cache: ImageCache,
        content: ContentFilter,
        opts: DrawOptions,
    ):
        super().__init__(cache, content, opts)
        self.site = site
        self.key = site.key
        self.name = site.name
        self.intro = site.intro
        self.link_re = site.link_re
        self.http = http
        # 查询参数 → 帖子总数
        self._totals = TTLCache(TOTAL_TTL, TOTAL_CACHE_SIZE)

    def _searches(self, ctx: DrawContext) -> list[tuple[list[str], list[str]]]:
        """可用的 (搜索词, 排除词)：原词、翻译后的词，或两者都试（去重）；关键词搜不了时为空。"""
        if self.site.bilingual:
            candidates = [ctx.req.keywords, ctx.terms]
        else:
            candidates = [ctx.terms if self.site.translate else ctx.req.keywords]
        searches = []
        for terms in candidates:
            search = search_terms(terms)
            if search is not None and search not in searches:
                searches.append(search)
        return searches

    def _raw_title(self, post: dict) -> str:
        return html.unescape(str((post.get("title") or {}).get("rendered") or ""))

    def _title(self, post: dict) -> str:
        """显示用的标题：去掉站点的标题噪声。"""
        title = self._raw_title(post)
        if self.site.title_noise:
            title = self.site.title_noise.sub("", title)
        return title.strip()

    def _images(self, post: dict) -> list[str]:
        content = str((post.get("content") or {}).get("rendered") or "")
        return post_images(content, self.site.image_marker)

    def query(self, rating: str) -> dict[str, str] | None:
        """筛选帖子的参数，站点没有这个分级的帖子时返回 None。关闭内容分级时不按分级筛选。"""
        site, enabled = self.site, self.opts.rating_enabled
        params: dict[str, str] = {}
        exclude_categories, exclude_tags = self._excludes()
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
        elif site.categories:
            params["categories"] = ",".join(map(str, site.categories))
        if exclude_categories:
            params["categories_exclude"] = ",".join(map(str, exclude_categories))
        if exclude_tags:
            params["tags_exclude"] = ",".join(map(str, exclude_tags))
        return params

    def _excludes(self) -> tuple[list[int], list[int]]:
        """要排除的 (分类, 标签)：站点的排除项，开启「屏蔽 AI 作品」时加上 AI 分类和标签。"""
        site = self.site
        categories, tags = list(site.exclude_categories), list(site.exclude_tags)
        if self.content.block_ai:
            categories += site.ai_categories
            tags += site.ai_tags
        return categories, tags

    def accepts(self, ctx: DrawContext) -> bool:
        """三次元，不是随机角色，站点有这个分级的帖子，关键词能搜索。"""
        req = ctx.req
        return (
            req.style == REAL
            and not req.random_character
            and self.query(req.rating) is not None
            and bool(self._searches(ctx))
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
        async def count() -> int:
            _, headers = await self._get({**params, "per_page": "1", "_fields": "id"})
            try:
                return int(headers.get("x-wp-total") or 0)
            except ValueError:
                return 0

        ttl = SEARCH_TTL if "search" in params else TOTAL_TTL
        return await self._totals.load(json.dumps(params, sort_keys=True), count, ttl)

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
            self._totals.pop(json.dumps(params, sort_keys=True))
            return None
        if isinstance(posts, list) and posts and isinstance(posts[0], dict):
            return posts[0]
        return None

    def excluded(self, post: dict) -> bool:
        """帖子在排除的分类或标签里（街拍、AI 作品等）。"""
        categories, tags = self._excludes()
        return bool(
            set(post.get("categories") or []) & set(categories)
            or set(post.get("tags") or []) & set(tags)
        )

    def _reject(
        self, post: dict, ctx: DrawContext, exclude: list[str], images: list[str]
    ) -> str | None:
        if not images:
            return "没有图片"
        categories, tags = term_names(post)
        words = [self._raw_title(post), *categories, *tags]
        reason = self.rating_reason(
            self.post_rating(post), ctx
        ) or self.content.text_reason(words)
        if reason:
            return reason
        text = "\n".join(words).lower()
        if any(word in text for word in exclude):
            return "命中排除的关键词"
        return None

    async def _search_params(
        self, params: dict[str, str], searches: list[tuple[list[str], list[str]]]
    ) -> tuple[dict[str, str], list[str]]:
        """加上搜索词的参数和排除词。有几种搜索词时用帖子最多的，排除词取全部。"""
        options = []
        for positive, _ in searches:
            option = dict(params)
            if positive:
                option["search"] = search_query(positive)
            options.append(option)
        if len(options) > 1:
            totals = await asyncio.gather(*(self._total(o) for o in options))
            options = [options[totals.index(max(totals))]]
        exclude = list(dict.fromkeys(w for _, words in searches for w in words))
        return options[0], exclude

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        """抽 n 个帖子，每个帖子取至多 per_album 张，帖子之间并发抽取。"""
        params = self.query(ctx.req.rating)
        searches = self._searches(ctx)
        if params is None or not searches:
            return [], [f"{self.name} 不支持这次请求"]
        try:
            params, exclude = await self._search_params(params, searches)
        except NETWORK_ERRORS as e:
            logger.warning(f"[random_pic] {self.name} 请求失败: {e!r}")
            return [], [f"{self.name} 请求失败"]
        seen: set[int] = set()
        semaphore = asyncio.Semaphore(self.opts.concurrency)

        async def attempt() -> Album | None:
            post = await self.random_post(params)
            if post is None or post.get("id") in seen:
                return None
            seen.add(post.get("id"))
            images = self._images(post)
            reason = self._reject(post, ctx, exclude, images)
            if reason:
                logger.info(
                    f"[random_pic] 丢弃 {self.name} 帖子 {post.get('id')}: {reason}"
                )
                return None
            return await self._album(post, images, ctx.req.per_album, semaphore)

        return await self.collect(n, POSTS_PER_ALBUM, attempt)

    async def _album(
        self, post: dict, images: list[str], n: int, semaphore: asyncio.Semaphore
    ) -> Album | None:
        async def download(index: int) -> tuple[int, Path] | None:
            async with semaphore:
                path = await self.cache.download(images[index])
            return (index + 1, path) if path else None

        pictures = await self.pick_pages(
            len(images), n, self.post_rating(post), download
        )
        if not pictures:
            return None
        _, tags = term_names(post)
        details = []
        if tags:
            details.append(f"标签：{'、'.join(tags[:MAX_CAPTION_TAGS])}")
        if post.get("link"):
            details.append(f"帖子：{post['link']}")
        return Album(
            source=self.name,
            title=self._title(post),
            total=len(images),
            pictures=pictures,
            details=details,
            work=WorkRef(self.key, str(post.get("id"))),
        )

    # ---- 整本打包 ----

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
        images = self._images(post)
        categories, tags = term_names(post)
        blocked = (
            ("属于站点排除的分类或标签" if self.excluded(post) else None)
            or self.content.text_reason([self._raw_title(post), *categories, *tags])
            or (None if images else "没有图片")
        )
        return Work(
            ref=WorkRef(self.key, str(post["id"])),
            title=self._title(post),
            pages=len(images),
            rating=self.post_rating(post),
            blocked=blocked,
            data=images,
        )
