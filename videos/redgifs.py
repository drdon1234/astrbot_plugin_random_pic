"""RedGifs：短视频站，按 niche（主题频道）随机抽高赞短片，只有 R18。

接口（2026-10 实测，匿名）：
- GET /v2/auth/temporary 取临时 token（约 24 小时有效），之后的请求带 Authorization: Bearer；
- GET /v2/niches/<niche>/gifs?order=top&count=80&page=N 按点赞从高到低分页，返回 pages（总页数）；
  /v2/gifs/search 的 search_text 实际被忽略、tags 搜索结果极少，所以不支持关键词；
- 每个条目有 duration、tags、verified（创作者已实名认证）、urls.hd / urls.sd（mp4，sd 是手机清晰度）。

niche 的选择和目检（各抽 8 段，45% 处截帧）：nsfw-cosplay（约 3.4 万）7/8 是 R18，多为欧美
OnlyFans coser；hanime（约 5.8 万）7/8 是 R18 动画片段。korean-nsfw 有 AI 换脸、明星和偷拍外流，不用。

年龄：用户上传站，35% 的 cosplay 片段带 Teen 标签（站方指 18~19 岁）。真人频道只要实名认证创作者的
（约七成），认证过年龄，Teen 不再排除；动画频道没有认证，排除 Teen、Young 一类标签。
18 Years Old、Barely Legal 写明成年，Schoolgirl、School Uniform 是装扮，都不排除。
"""

import asyncio
import json
import random

from astrbot.api import logger

from ..models import EXPLICIT, Album, DrawContext
from ..net import NETWORK_ERRORS, HttpClient, HttpError
from ..sources.base import Source, split_terms
from ..util import TTLCache
from .base import VideoFiles, young_word

API = "https://api.redgifs.com/v2"
PAGE_SIZE = 80
# 只在点赞最高的这么多页里抽（80 × 100 = 8000 个）
MAX_PAGES = 100
PAGES_TTL = 3600
# token 有效期约 24 小时，提前换
TOKEN_TTL = 20 * 3600
VIDEO_TYPE = 1
# 低龄指向的标签（RedGifs 的标签首字母大写，比较时转小写）
# 没有实名认证的频道（动画）排除的标签（RedGifs 的标签首字母大写，比较时转小写）
YOUNG_TAGS = frozenset({"teen", "teens", "petite teen", "young"})
AI_TAGS = frozenset({"ai", "ai generated", "ai porn", "ai hentai", "ai art"})
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
        niche: str,
        intro: str,
        verified_only: bool,
    ):
        super().__init__(cache, content, opts)
        self.http = http
        self.files = files
        self.key = key
        self.name = name
        self.style = style
        self.niche = niche
        self.intro = intro
        self.verified_only = verified_only
        self.exclude = (frozenset() if verified_only else YOUNG_TAGS) | (
            AI_TAGS if content.block_ai else frozenset()
        )
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
            try:
                text = await self.http.get_text(API + path, params=params, headers=headers)
            except HttpError as e:
                if e.status == 401 and not refresh:
                    continue
                raise
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        raise HttpError(f"{self.name} token 无效")

    async def _page(self, page: int) -> tuple[list[dict], int]:
        data = await self._get(
            f"/niches/{self.niche}/gifs",
            {"order": "top", "count": str(PAGE_SIZE), "page": str(page)},
        )
        gifs = [g for g in data.get("gifs") or [] if isinstance(g, dict)]
        return gifs, int(data.get("pages") or 0)

    async def pages(self) -> int:
        """可以抽的页数（不超过 MAX_PAGES）。"""

        async def load() -> int:
            _, pages = await self._page(1)
            return pages

        return min(await self._pages.load(self.niche, load), MAX_PAGES)

    def _reject(self, gif: dict, local: set[str]) -> str | None:
        if gif.get("type") != VIDEO_TYPE:
            return "不是视频"
        urls = gif.get("urls") or {}
        if not (urls.get("hd") or urls.get("sd")):
            return "没有视频地址"
        if self.verified_only and not gif.get("verified"):
            return "创作者未认证"
        tags = [str(t) for t in gif.get("tags") or []]
        reason = self.content.plain_tags_reason(tags)
        if reason:
            return reason
        low = {t.lower() for t in tags}
        hit = next((t for t in low if t in self.exclude or t in local), None)
        if hit:
            return f"带排除的标签 {hit}"
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
                        if gid in seen:
                            continue
                        reason = self._reject(gif, local)
                        if reason is None:
                            seen.add(gid)
                            return gif
                        logger.debug(f"[random_pic] 跳过 {self.name} {gid}: {reason}")
                    if fetches >= MAX_FETCHES:
                        return None
                    fetches += 1
                    gifs, _ = await self._page(random.randint(1, pages))
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
            duration=gif.get("duration"),
        )
