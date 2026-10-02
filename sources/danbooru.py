"""Danbooru 二次元图源：按分级和最低评分筛选，随机游标抽帖子，一个帖子就是一个图集。

匿名接口（2026-10 实测）：
- 必须用非浏览器的 User-Agent：带浏览器 UA 的请求（接口和 cdn.donmai.us 的图片）都被 Cloudflare 拦成 403；
- 一次搜索最多 2 个标签，rating: 和 score: 不计数；带排除词（-tag）的大范围搜索容易数据库超时；
- random:N 在全部符合条件的帖子里均匀随机取 N 个，约 1 秒，但它也算 1 个标签；
  order:random 要 3 秒以上且时常超时，不用；
- 关键词占满 2 个标签时只能用游标随机：page=a0 取最旧的帖子、默认排序取最新的帖子，在两者之间
  随机取 id，page=b<id> 取这个 id 之前的 20 个帖子。高分帖子越新越密（2007 年 20 个帖子跨 15 万个 id，
  2025 年只跨两三千个），游标会偏向老帖子，所以只在不得不用时用；
- /autocomplete.json 把中文、日文名（来自 wiki 的别名）和英文前缀补全成标签，按帖子数排序；
- /related_tag.json 统计一次搜索里各标签出现的比例，也能列出和某个标签相关的角色；
- 短时间内请求太多会 429，所以接口请求按 REQUEST_INTERVAL 限速（图片不限）；
- parent:<id> 返回这个帖子和它的子帖子（差分），/pdf 整本打包时把这一组打成一个 PDF。

分级：s（sensitive）为擦边，q、e 为 R18。抽样目检（score > 150）：s 约 20/24 明显擦边、其余较轻，
q 多为露点，e 为性内容；50~100 分的 s 只有约 16/24 达到擦边，杂图也多，所以默认最低 150 分。

未成年：Danbooru 的 loli / shota 标签基本只打在性内容上，设定是儿童的角色在擦边图里不会被标出，
R18 图里也有漏标（实测伊莉雅的露点图没有 loli）。所以按角色统计：该角色的 q、e 帖子里带 loli / shota
的比例达到 CHILD_RATIO 时，这个角色的所有帖子都不要（实测可莉 0.88、纳西妲 0.85、伊莉雅 0.56、
阿比盖尔 0.31、空崎日奈 0.28；芙莉莲 0.03、约尔 0.02）。
"""

import asyncio
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from ..filters import ContentFilter
from ..models import (
    ANIME,
    EXPLICIT,
    SENSITIVE,
    Album,
    DrawContext,
    DrawOptions,
    Work,
    WorkRef,
)
from ..net import NETWORK_ERRORS, HttpClient, HttpError, ImageCache, RateLimiter
from ..tags import TagIndex
from ..util import TTLCache
from .base import Source, SourceError, plain_word, split_terms

API = "https://danbooru.donmai.us"
HEADERS = {"User-Agent": "astrbot_plugin_random_pic"}
REQUEST_INTERVAL = 0.15
TAG_LIMIT = 2
PAGE_SIZE = 20
# random:N 一次最多取这么多个帖子
MAX_BATCH = 100
# 一次抽卡最多取这么多批候选帖子（都被过滤时停止）
MAX_BATCHES = 5
RATING_QUERY = {SENSITIVE: "rating:s", EXPLICIT: "rating:q,e"}
POST_RATINGS = {"s": SENSITIVE, "q": EXPLICIT, "e": EXPLICIT}
CHARACTER_CATEGORY = 4
FIELDS = ",".join(
    (
        "id",
        "rating",
        "score",
        "source",
        "pixiv_id",
        "tag_string",
        "tag_string_artist",
        "tag_string_character",
        "tag_string_copyright",
        "file_ext",
        "file_size",
        "file_url",
        "large_file_url",
    )
)
IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "webp"})
# 最低用户投票分：150 分时擦边池约 7.8 万张、R18 池约 27 万张；50~100 分的明显变差
MIN_SCORE = 150
# 排除的标签：低龄化的版本（Danbooru 不会给它打 loli）、动图，以及漫画、多格、黑白、草图、
# 3D 和男性向等不适合抽图的帖子
EXCLUDE_TAGS = frozenset(
    {
        "aged_down",
        "animated",
        "comic",
        "4koma",
        "multiple_views",
        "monochrome",
        "greyscale",
        "sketch",
        "lineart",
        "3d",
        "photorealistic",
        "yaoi",
        "male_focus",
        "furry",
    }
)
AI_TAGS = frozenset({"ai-generated", "ai-assisted"})
CHILD_TAGS = frozenset({"loli", "shota"})
CHILD_RATIO = 0.2
# 统计角色时取出现比例最高的这么多个标签；比例达到 CHILD_RATIO 的标签一定在里面
RELATED_LIMIT = 150
# 帖子范围（最旧、最新的 id）缓存：不带关键词时一小时，带关键词时 10 分钟
BOUNDS_TTL = 3600
SEARCH_TTL = 600
CHARACTERS_TTL = 86400
# 随机角色：不带关键词时从帖子数最多的这么多个角色里抽，带关键词时从相关的角色里抽
TOP_CHARACTERS = 2000
RELATED_CHARACTERS = 100
CHARACTER_TRIES = 10
# 每个图集最多随机这么多次（整页都被过滤、角色不合格或下载失败时重来）
POSTS_PER_ALBUM = 4
CACHE_SIZE = 4096
MAX_NAMES = 3
# 标签末尾的消歧义括号，例如 hu_tao_(genshin_impact)、toki_(bunny)_(blue_archive)
QUALIFIER_RE = re.compile(r"(?:_\([^()]*\))+$")


@dataclass(frozen=True)
class Pool:
    query: str
    bounds: tuple[int, int]  # 最旧、最新帖子的 id
    uniform: bool  # 还有一个标签的额度，可以用 random:N


class DanbooruError(SourceError):
    pass


def tag_word(term: str) -> str | None:
    """关键词 → 补全用的词：E-Hentai 标签写法取出标签名，空格换成下划线。"""
    word = plain_word(term)
    return "_".join(word.lower().split()) if word else None


def display_names(tags: str, namespace: str, index: TagIndex | None) -> list[str]:
    """Danbooru 标签 → 显示名：去掉消歧义括号，标签库有中文名时用中文，否则用英文。"""
    names = []
    for tag in tags.split()[:MAX_NAMES]:
        if tag == "original":
            names.append("原创")
            continue
        plain = QUALIFIER_RE.sub("", tag).replace("_", " ") or tag
        zh = index.zh_names.get(f"{namespace}:{plain}") if index else None
        names.append(zh or plain)
    return list(dict.fromkeys(names))


class DanbooruSource(Source):
    key = "danbooru"
    name = "Danbooru"
    style = ANIME
    intro = "高分插画，可搜中文角色、作品名，支持 /随机角色"
    link_re = re.compile(r"https?://danbooru\.donmai\.us/posts/(\d+)")

    def __init__(
        self,
        http: HttpClient,
        cache: ImageCache,
        content: ContentFilter,
        opts: DrawOptions,
        *,
        min_score: int = MIN_SCORE,
        exclude_tags: frozenset[str] = EXCLUDE_TAGS,
    ):
        super().__init__(cache, content, opts)
        self.http = http
        self.min_score = min_score
        self.exclude = frozenset(exclude_tags) | (
            AI_TAGS if content.block_ai else frozenset()
        )
        self.limiter = RateLimiter(REQUEST_INTERVAL)
        # 搜索条件 → 最旧、最新帖子的 id，没有帖子时为 None
        self._bounds = TTLCache(BOUNDS_TTL, CACHE_SIZE)
        # 补全用的词 → (标签, 分类)，找不到时为 None
        self._tags = TTLCache(BOUNDS_TTL, CACHE_SIZE)
        # 角色 → q、e 帖子里 loli / shota 的比例
        self._ratios = TTLCache(CHARACTERS_TTL, CACHE_SIZE)
        # 关键词标签（不带时为 ""）→ 随机角色候选
        self._characters = TTLCache(CHARACTERS_TTL, CACHE_SIZE)

    async def _get(self, path: str, params: dict):
        await self.limiter.wait()
        text = await self.http.get_text(API + path, params=params, headers=HEADERS)
        try:
            return json.loads(text)
        except ValueError as e:
            raise HttpError(f"{self.name} 返回的不是 JSON") from e

    async def _posts(self, query: str, **params) -> list[dict]:
        data = await self._get("/posts.json", {"tags": query, **params})
        if not isinstance(data, list):
            return []
        return [p for p in data if isinstance(p, dict)]

    async def _related(self, query: str, category: int, limit: int) -> dict[str, float]:
        """query 的帖子里各标签出现的比例（只看 category 分类，取最高的 limit 个）。"""
        data = await self._get(
            "/related_tag.json",
            {
                "search[query]": query,
                "search[category]": str(category),
                "limit": str(limit),
            },
        )
        related = data.get("related_tags") if isinstance(data, dict) else None
        out = {}
        for item in related or []:
            name = ((item or {}).get("tag") or {}).get("name")
            if name:
                out[str(name)] = float(item.get("frequency") or 0)
        return out

    # ---- 关键词 ----

    async def resolve(
        self, term: str, index: TagIndex | None
    ) -> tuple[str, int | None] | None:
        """关键词 → (Danbooru 标签, 分类)，找不到时返回 None。

        先按原词补全（Danbooru 的 wiki 收录了很多中文、日文名）；补全不到的中文名再经
        E-Hentai 标签库翻译成英文名补全。补全结果里有和输入完全相同的标签时取它，否则取帖子最多的。
        """
        word = tag_word(term)
        if word is None:
            return None

        async def lookup():
            words = [word]
            if index and not word.isascii():
                translated = tag_word(index.translate(term.strip()))
                if translated and translated != word:
                    words.append(translated)
            for w in words:
                found = await self._autocomplete(w)
                if found:
                    return found
            return None

        return await self._tags.load(word, lookup)

    async def _autocomplete(self, word: str) -> tuple[str, int | None] | None:
        data = await self._get(
            "/autocomplete.json",
            {"search[type]": "tag_query", "search[query]": word, "limit": "10"},
        )
        # 只要标签（有分类），不要补全出来的元标签、用户名等
        found = [
            (str(d["value"]), d.get("category"))
            for d in (data if isinstance(data, list) else [])
            if isinstance(d, dict) and d.get("value") and "category" in d
        ]
        if not found:
            return None
        return next((f for f in found if f[0] == word), found[0])

    async def _plan(self, ctx: DrawContext) -> tuple[list[str], set[str]]:
        """返回 (搜索标签, 本地排除的标签)。

        匿名搜索最多 2 个标签：随机角色占 1 个，其余给关键词。排除词全部在本地过滤，
        放进搜索条件容易让数据库超时。
        """
        positive_words, negative_words = split_terms(ctx.req.keywords)
        resolved = await asyncio.gather(
            *(self.resolve(w, ctx.index) for w in [*positive_words, *negative_words])
        )
        positive: list[tuple[str, int | None]] = []
        for word, hit in zip(positive_words, resolved):
            if hit is None:
                raise DanbooruError(f"{self.name} 找不到「{word}」对应的标签")
            if hit not in positive:
                positive.append(hit)
        tags = [tag for tag, _ in positive]
        if tags:
            reason = self.content.plain_tags_reason(tags)
            if reason:
                raise DanbooruError(f"关键词{reason}")
        budget = TAG_LIMIT - (1 if ctx.req.random_character else 0)
        if len(tags) > budget:
            raise DanbooruError(f"{self.name} 一次最多搜 {budget} 个关键词")
        for tag, category in positive:
            if category == CHARACTER_CATEGORY and await self.is_child(tag):
                raise DanbooruError(f"「{tag}」多为儿童设定，不提供")
        negative = {hit[0] for hit in resolved[len(positive_words) :] if hit}
        return tags, negative - set(tags)

    # ---- 抽帖子 ----

    def _query(self, rating: str, tags: list[str], score: int) -> str:
        parts = [RATING_QUERY[rating], *tags]
        if score > 0:
            parts.append(f"score:>={score}")
        return " ".join(parts)

    def _scores(self, narrowed: bool) -> list[int]:
        """带关键词或随机角色时，按最低分搜不到再降到三分之一搜一次。"""
        if narrowed and self.min_score >= 3:
            return [self.min_score, self.min_score // 3]
        return [self.min_score]

    async def bounds(self, query: str, ttl: float) -> tuple[int, int] | None:
        """符合条件的最旧、最新帖子 id，没有帖子时为 None。"""

        async def load():
            oldest = await self._posts(query, page="a0", limit="1", only="id")
            if not oldest:
                return None
            newest = await self._posts(query, limit="1", only="id")
            ids = [int(p["id"]) for p in [*oldest, *newest] if "id" in p]
            return (min(ids), max(ids)) if ids else None

        return await self._bounds.load(query, load, ttl)

    async def _pool(self, ctx: DrawContext, tags: list[str]) -> Pool | None:
        narrowed = bool(tags)
        ttl = SEARCH_TTL if narrowed else BOUNDS_TTL
        for score in self._scores(narrowed):
            query = self._query(ctx.req.rating, tags, score)
            found = await self.bounds(query, ttl)
            if found:
                return Pool(query, found, len(tags) < TAG_LIMIT)
        return None

    async def _batch(self, pool: Pool, want: int) -> list[dict]:
        """一批随机的候选帖子：能用 random:N 时均匀随机取，否则跳到随机游标取一页。"""
        if pool.uniform:
            k = min(MAX_BATCH, max(PAGE_SIZE, want))
            posts = await self._posts(
                f"{pool.query} random:{k}", limit=str(k), only=FIELDS
            )
        else:
            oldest, newest = pool.bounds
            posts = await self._posts(
                pool.query,
                page=f"b{random.randint(oldest + 1, newest + 1)}",
                limit=str(PAGE_SIZE),
                only=FIELDS,
            )
        random.shuffle(posts)
        return posts

    async def characters(self, tags: list[str]) -> list[str]:
        """随机角色的候选：带关键词时是和它相关的角色，否则是帖子数最多的角色。缓存一天。"""
        key = " ".join(tags)

        async def load() -> list[str]:
            if tags:
                related = await self._related(
                    key, CHARACTER_CATEGORY, RELATED_CHARACTERS
                )
                return [name for name in related if name not in tags]
            pages = await asyncio.gather(
                *(
                    self._get(
                        "/tags.json",
                        {
                            "search[category]": str(CHARACTER_CATEGORY),
                            "search[order]": "count",
                            "limit": "1000",
                            "page": str(page),
                            "only": "name",
                        },
                    )
                    for page in range(1, TOP_CHARACTERS // 1000 + 1)
                )
            )
            return [
                str(t["name"])
                for page in pages
                if isinstance(page, list)
                for t in page
                if isinstance(t, dict) and t.get("name")
            ]

        return await self._characters.load(key, load)

    async def _character_pool(self, ctx: DrawContext, tags: list[str]) -> Pool:
        names = await self.characters(tags)
        if not names:
            raise DanbooruError(f"{self.name} 找不到可以随机的角色")
        for _ in range(CHARACTER_TRIES):
            character = random.choice(names)
            if await self.is_child(character):
                continue
            pool = await self._pool(ctx, [character, *tags])
            if pool:
                return pool
        raise DanbooruError(f"连续 {CHARACTER_TRIES} 个随机角色都没有符合条件的帖子")

    async def is_child(self, character: str) -> bool:
        """该角色的 q、e 帖子里带 loli / shota 的比例是否达到 CHILD_RATIO。"""

        async def load() -> float:
            related = await self._related(f"{character} rating:q,e", 0, RELATED_LIMIT)
            return sum(related.get(tag, 0.0) for tag in CHILD_TAGS)

        ratio = await self._ratios.load(character, load)
        return ratio >= CHILD_RATIO

    def _reject(self, post: dict, ctx: DrawContext, local: set[str]) -> str | None:
        if not post.get("file_url"):
            return "没有原图"
        if str(post.get("file_ext") or "").lower() not in IMAGE_EXTS:
            return f"不是静态图片（{post.get('file_ext')}）"
        tags = str(post.get("tag_string") or "").split()
        reason = self.rating_reason(
            POST_RATINGS.get(str(post.get("rating"))), ctx
        ) or self.content.plain_tags_reason(tags)
        if reason:
            return reason
        hit = next((t for t in tags if t in self.exclude or t in local), None)
        return f"带排除的标签 {hit}" if hit else None

    async def _child_character(self, post: dict) -> str | None:
        """返回帖子里设定多为儿童的角色，没有时返回 None。"""
        characters = str(post.get("tag_string_character") or "").split()
        flags = await asyncio.gather(*map(self.is_child, characters))
        return next((c for c, child in zip(characters, flags) if child), None)

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        """抽 n 个帖子，每个帖子一个图集（一张图），帖子之间并发抽取。"""
        try:
            tags, local = await self._plan(ctx)
            pool = None
            if not ctx.req.random_character:
                pool = await self._pool(ctx, tags)
                if pool is None:
                    raise DanbooruError(
                        f"{self.name} 搜不到「{' '.join(tags)}」"
                        if tags
                        else f"{self.name} 没有符合条件的帖子"
                    )
        except SourceError as e:
            return [], [str(e)]
        except NETWORK_ERRORS as e:
            logger.warning(f"[random_pic] {self.name} 请求失败: {e!r}")
            return [], [f"{self.name} 请求失败"]

        seen: set[int] = set()
        queue: list[dict] = []
        batches = 0
        lock = asyncio.Lock()

        def take(posts: list[dict]) -> dict | None:
            """从 posts 末尾依次取出，返回第一个通过过滤的帖子。"""
            while posts:
                post = posts.pop()
                if post.get("id") in seen:
                    continue
                reason = self._reject(post, ctx, local)
                if reason is None:
                    seen.add(post.get("id"))
                    return post
                logger.debug(
                    f"[random_pic] 跳过 Danbooru 帖子 {post.get('id')}: {reason}"
                )
            return None

        async def pick() -> dict | None:
            """随机角色时每个图集换一个角色取一批；否则各图集共用一批批取来的候选。"""
            nonlocal batches
            if pool is None:
                own = await self._character_pool(ctx, tags)
                return take(await self._batch(own, 1))
            async with lock:
                post = take(queue)
                while post is None and batches < MAX_BATCHES:
                    batches += 1
                    queue.extend(await self._batch(pool, 2 * n))
                    post = take(queue)
                return post

        async def attempt() -> Album | None:
            post = await pick()
            if post is None:
                return None  # 候选帖子都被过滤了
            child = await self._child_character(post)
            if child:
                logger.info(
                    f"[random_pic] 丢弃 Danbooru 帖子 {post.get('id')}: {child} 多为儿童设定"
                )
                return None
            return await self._album(post, ctx)

        return await self.collect(n, POSTS_PER_ALBUM, attempt)

    async def _album(self, post: dict, ctx: DrawContext) -> Album | None:
        """下载原图，超过大小上限或下载失败时改用 850px 宽的缩小图。"""
        urls = [post["file_url"]]
        sample = post.get("large_file_url")
        if sample and sample != post["file_url"]:
            if int(post.get("file_size") or 0) > self.cache.max_image:
                urls = [sample]
            else:
                urls.append(sample)
        path = None
        for url in urls:
            path = await self.cache.download(url, headers=HEADERS)
            if path:
                break
        if path is None:
            return None
        index = ctx.index
        characters = display_names(
            str(post.get("tag_string_character") or ""), "character", index
        )
        works = display_names(
            str(post.get("tag_string_copyright") or ""), "parody", index
        )
        details = []
        artists = str(post.get("tag_string_artist") or "").split()[:MAX_NAMES]
        if artists:
            details.append(f"画师：{', '.join(a.replace('_', ' ') for a in artists)}")
        if characters and works:
            details.append(f"作品：{'、'.join(works)}")
        details.append(f"评分：{post.get('score')}")
        source = str(post.get("source") or "")
        if post.get("pixiv_id"):
            details.append(f"出处：https://www.pixiv.net/artworks/{post['pixiv_id']}")
        elif source.startswith("http"):
            details.append(f"出处：{source}")
        details.append(f"帖子：{API}/posts/{post.get('id')}")
        return Album(
            source=self.name,
            title="、".join(characters or works) or "无标题",
            total=1,
            pictures=[(1, path)],
            details=details,
            work=WorkRef(self.key, str(post.get("id"))),
        )

    # ---- 整本打包 ----

    def _url(self, post: dict) -> str:
        """打包用的图片地址：原图超过大小上限时用 850px 宽的缩小图。"""
        sample = post.get("large_file_url")
        if sample and int(post.get("file_size") or 0) > self.cache.max_image:
            return str(sample)
        return str(post["file_url"])

    async def _family_reason(self, post: dict) -> str | None:
        """差分里的一张图能否打包：和抽图同样的过滤，不看分级（分级由整组决定）。"""
        if not post.get("file_url"):
            return "没有原图"
        if str(post.get("file_ext") or "").lower() not in IMAGE_EXTS:
            return f"不是静态图片（{post.get('file_ext')}）"
        tags = str(post.get("tag_string") or "").split()
        reason = self.content.plain_tags_reason(tags)
        if reason:
            return reason
        hit = next((t for t in tags if t in self.exclude), None)
        if hit:
            return f"带排除的标签 {hit}"
        child = await self._child_character(post)
        return f"{child} 多为儿童设定" if child else None

    async def work(self, ref: WorkRef) -> Work | None:
        """帖子和它的差分（父帖子、子帖子）按 id 排序打成一本，不存在时返回 None。

        每张图单独过滤，过滤掉的不打包；剩下的有任何一张是 q、e 时整本按 R18。
        """
        fields = FIELDS + ",parent_id,has_children"
        try:
            post = await self._get(f"/posts/{ref.id}.json", {"only": fields})
        except HttpError as e:
            if e.status == 404:
                return None
            raise
        if not isinstance(post, dict) or not post.get("id"):
            return None
        family = [post]
        if post.get("parent_id") or post.get("has_children"):
            root = post.get("parent_id") or post["id"]
            family = await self._posts(f"parent:{root}", limit="200", only=fields)
            if not any(p.get("id") == post["id"] for p in family):
                family.append(post)
        family.sort(key=lambda p: int(p.get("id") or 0))
        reasons = await asyncio.gather(*map(self._family_reason, family))
        kept = [p for p, reason in zip(family, reasons) if reason is None]
        if len(kept) < len(family):
            logger.info(
                f"[random_pic] Danbooru 帖子 {ref.id} 的差分有 "
                f"{len(family) - len(kept)} 张被过滤"
            )
        ratings = {POST_RATINGS.get(str(p.get("rating"))) for p in kept}
        characters = display_names(
            str(post.get("tag_string_character") or ""), "character", None
        )
        works = display_names(
            str(post.get("tag_string_copyright") or ""), "parody", None
        )
        own = next(
            (r for p, r in zip(family, reasons) if p.get("id") == post["id"]), None
        )
        return Work(
            ref=WorkRef(self.key, str(post["id"])),
            title="、".join(characters or works) or f"Danbooru {post['id']}",
            pages=len(kept),
            rating=EXPLICIT if EXPLICIT in ratings else SENSITIVE,
            blocked=None if kept else (own or "差分都被过滤了"),
            data=[self._url(p) for p in kept],
        )

    async def download_page(self, url: str, dest: Path) -> Path | None:
        return await self.cache.download(url, dest, headers=HEADERS)
