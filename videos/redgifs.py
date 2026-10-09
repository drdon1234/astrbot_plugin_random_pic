"""RedGifs：短视频站，按标签在全站搜高赞短片，只有 R18（站点没有分级，创作者自打的 SFW、Non-nude
标签不可靠：目检 SFW 只有 2/8 算擦边、其余是指甲、下巴一类杂片，Non-nude 里混着性爱片段）。

接口（2026-10 实测，匿名）：
- GET /v2/auth/temporary 取临时 token（约 24 小时有效），之后的请求带 Authorization: Bearer；
- GET /v2/gifs/search?tags=<标签>&order=top&count=80&page=N 按点赞从高到低分页，最多能翻到第 125 页
  （1 万个）；search_text 实际被忽略，多个标签搜到的极少，所以不支持关键词；请求太密会 429，限速；
- 每个条目有 duration、likes、tags、verified（创作者已实名认证）、urls.hd / urls.sd（mp4）。
- 几乎都是单个视频：gallery（合集）在 640 个条目里只有 1 个，而且是图片。

为什么不用 niche（主题频道）：/v2/niches/<niche>/gifs 同样按点赞排，但衰减很快（nsfw-cosplay 第 5 页
点赞中位数 197、第 20 页 41），按 8000 个随机时 24 段里大多只有几十赞，混着足控片和广告。
全站按标签搜 Cosplay 第 20 页中位数 318、第 40 页 225，Hentai 第 20 页 267，所以按标签搜、
只要点赞不低于「最低点赞数」的，能抽的页数第一次用时试出来。

年龄：用户上传站，35% 的 cosplay 片段带 Teen 标签（站方指 18~19 岁）。真人频道只要实名认证创作者的
（约七成），认证过年龄，Teen 不再排除；动画频道没有认证，排除 Teen、Young 一类标签。
18 Years Old、Barely Legal 写明成年，Schoolgirl、School Uniform 是装扮，都不排除。
"""

import asyncio
import json
import random

from astrbot.api import logger

from ..models import EXPLICIT, Album, DrawContext, WorkRef
from ..net import NETWORK_ERRORS, HttpClient, HttpError, RateLimiter
from ..sources.base import Source, split_terms
from ..util import TTLCache
from .base import MMD_TAGS, VideoFiles, count_pages, young_word

API = "https://api.redgifs.com/v2"
PAGE_SIZE = 80
# 搜索最多能翻到的页数（80 × 125 = 1 万个）
MAX_PAGES = 125
PAGES_TTL = 6 * 3600
REQUEST_INTERVAL = 0.5
# token 有效期约 24 小时，提前换
TOKEN_TTL = 20 * 3600
VIDEO_TYPE = 1
# 低龄指向的标签（RedGifs 的标签首字母大写，比较时转小写）
# 没有实名认证的频道（动画）排除的标签（RedGifs 的标签首字母大写，比较时转小写）
YOUNG_TAGS = frozenset({"teen", "teens", "petite teen", "young"})
AI_TAGS = frozenset({"ai", "ai generated", "ai porn", "ai hentai", "ai art"})
# RedGifs 上跨性别内容的标签（「屏蔽跨性别作品」开启时排除，通用词表里没有这些写法）
TRANS_TAGS = frozenset(
    {
        "trans", "trans woman", "transgender", "tgirl", "ts", "shemale", "femboy",
        "girlcock", "babecock", "girldick", "trap", "sissy", "futa", "futanari",
    }
)
# 跑题的恋物类标签（目检时混进来的足控片）
FETISH_TAGS = frozenset(
    {"feet", "feet fetish", "foot fetish", "foot worship", "soles", "toes", "footjob"}
)
# 每个视频最多换这么多次（被过滤、下载失败时）
TRIES = 4
# 每次抽取最多翻这么多页候选
MAX_FETCHES = 6


class RedGifsSource(Source):
    ratings = frozenset({EXPLICIT})

    def __init__(
        self,
        http: HttpClient,
        cache,
        content,
        opts,
        files: VideoFiles,
        *,
        key: str,
        name: str,
        style: str,
        tag: str,
        intro: str,
        verified_only: bool,
        min_likes: int,
        require: frozenset[str] = frozenset(),
        reject: frozenset[str] = frozenset(),
        allow_mmd: bool = True,
    ):
        """tag：搜索的标签；require / reject：条目至少要带其中一个、不能带的标签（小写），区分动画和真人。"""
        super().__init__(cache, content, opts)
        self.http = http
        self.files = files
        self.key = key
        self.name = name
        self.style = style
        self.tag = tag
        self.intro = intro
        self.verified_only = verified_only
        self.min_likes = min_likes
        self.require = require
        self.exclude = (
            (frozenset() if verified_only else YOUNG_TAGS)
            | (AI_TAGS if content.block_ai else frozenset())
            | (TRANS_TAGS if content.block_trans else frozenset())
            | (frozenset() if allow_mmd else MMD_TAGS)
            | FETISH_TAGS
            | reject
        )
        self.limiter = RateLimiter(REQUEST_INTERVAL)
        self._token = TTLCache(TOKEN_TTL, 1)
        self._pages = TTLCache(PAGES_TTL, 4)

    def accepts(self, ctx: DrawContext) -> bool:
        """不能按关键词搜索：带要搜的关键词或随机角色时不参与（只带排除词可以）。"""
        positive, _ = split_terms(ctx.req.keywords)
        return (
            super().accepts(ctx) and not positive and not ctx.req.random_character
        )

    async def token(self, refresh: bool = False) -> str:
        if refresh:
            self._token.pop("token")

        async def load() -> str:
            data = json.loads(await self.http.get_text(f"{API}/auth/temporary"))
            token = data.get("token") if isinstance(data, dict) else None
            if not token:
                raise HttpError(f"{self.name} 没有返回 token")
            return str(token)

        return await self._token.load("token", load)

    async def _get(self, path: str, params: dict) -> dict:
        """带 token 请求，token 失效（401）时换一个重试一次。"""
        for refresh in (False, True):
            headers = {"Authorization": f"Bearer {await self.token(refresh)}"}
            await self.limiter.wait()
            try:
                text = await self.http.get_text(API + path, params=params, headers=headers)
            except HttpError as e:
                if e.status == 401 and not refresh:
                    continue
                raise
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        raise HttpError(f"{self.name} token 无效")

    async def _page(self, page: int) -> list[dict]:
        data = await self._get(
            "/gifs/search",
            {
                "tags": self.tag,
                "order": "top",
                "count": str(PAGE_SIZE),
                "page": str(page),
            },
        )
        return [g for g in data.get("gifs") or [] if isinstance(g, dict)]

    async def _full(self, page: int) -> bool:
        """这一页是满的，且点赞最少的也达到最低点赞。"""
        gifs = await self._page(page)
        return len(gifs) >= PAGE_SIZE and all(
            int(g.get("likes") or 0) >= self.min_likes for g in gifs
        )

    async def pages(self) -> int:
        """可以抽的页数（页码从 1 起）。"""

        async def load() -> int:
            return await count_pages(self._full, 1, MAX_PAGES)

        return await self._pages.load(self.tag, load)

    def _reject(self, gif: dict, local: set[str]) -> str | None:
        if gif.get("type") != VIDEO_TYPE:
            return "不是视频"
        urls = gif.get("urls") or {}
        if not (urls.get("hd") or urls.get("sd")):
            return "没有视频地址"
        if self.verified_only and not gif.get("verified"):
            return "创作者未认证"
        if int(gif.get("likes") or 0) < self.min_likes:
            return "点赞太少"
        tags = [str(t) for t in gif.get("tags") or []]
        reason = self.content.plain_tags_reason(tags)
        if reason:
            return reason
        low = {t.lower() for t in tags}
        hit = next((t for t in low if t in self.exclude or t in local), None)
        if hit:
            return f"带排除的标签 {hit}"
        if self.require and not low & self.require:
            return "风格不符"
        description = str(gif.get("description") or "")
        term = self.content.blacklist.hit([description]) or young_word(
            [description, *tags]
        )
        if term:
            return f"描述或标签命中 {term}"
        return self.files.reason(gif.get("duration"))

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        _, negative = split_terms(ctx.req.keywords)
        local = {t.lower().replace("_", " ") for t in negative}
        try:
            pages = await self.pages()
        except NETWORK_ERRORS as e:
            logger.warning(f"[random_pic] {self.name} 请求失败: {e!r}")
            return [], [f"{self.name} 请求失败"]
        if pages <= 0:
            return [], [f"{self.name} 没有视频"]
        seen: set[str] = set()
        # 高赞的片子集中在少数作者（目检 24 段里 9 段是同一个人），一次抽取里每个作者只出一个
        authors: set[str] = set()
        queue: list[dict] = []
        fetches = 0
        lock = asyncio.Lock()

        async def pick() -> dict | None:
            """各视频共用一批批随机页里的候选；翻满 MAX_FETCHES 页还不够时返回 None。"""
            nonlocal fetches
            async with lock:
                while True:
                    while queue:
                        gif = queue.pop()
                        gid = gif.get("id")
                        author = str(gif.get("userName") or "")
                        if gid in seen or (author and author in authors):
                            continue
                        reason = self._reject(gif, local)
                        if reason is None:
                            seen.add(gid)
                            authors.add(author)
                            return gif
                        logger.debug(f"[random_pic] 跳过 {self.name} {gid}: {reason}")
                    if fetches >= MAX_FETCHES:
                        return None
                    fetches += 1
                    gifs = await self._page(random.randint(1, pages))
                    random.shuffle(gifs)
                    queue.extend(gifs)

        async def attempt() -> Album | None:
            gif = await pick()
            return await self._album(gif) if gif else None

        albums, errors = await self.collect(n, TRIES, attempt)
        if not albums and fetches >= MAX_FETCHES:
            errors.append(f"{self.name} 连续 {fetches} 页都没有符合条件的视频")
        return albums, errors

    async def _album(self, gif: dict) -> Album | None:
        """先下高清版，超过大小上限或失败时改下手机清晰度的。"""
        urls = gif.get("urls") or {}
        path = None
        for url in dict.fromkeys(u for u in (urls.get("hd"), urls.get("sd")) if u):
            path = await self.files.download(str(url))
            if path:
                break
        if path is None:
            return None
        tags = [str(t) for t in gif.get("tags") or []]
        user = str(gif.get("userName") or "")
        details = []
        if user:
            details.append(f"作者：{user}")
        if tags:
            details.append(f"标签：{', '.join(tags[:8])}")
        details.append(f"点赞：{gif.get('likes')}")
        details.append(f"链接：https://www.redgifs.com/watch/{gif.get('id')}")
        return Album(
            source=self.name,
            # 描述多是推广文字和链接，标题用作者名
            title=user or str(gif.get("id")),
            total=1,
            pictures=[(1, path)],
            details=details,
            work=WorkRef(self.key, str(gif.get("id"))),
            duration=gif.get("duration"),
        )
