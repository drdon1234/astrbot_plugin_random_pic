"""Iwara（二次元 MMD、Koikatsu、Blender 等 3D 视频），只有 R18。

接口（api.iwara.tv，2026-10 实测，匿名可用）：
- GET /videos?rating=ecchi&sort=likes&limit=50&page=N[&tags=a,b]：按点赞从高到低分页（page 从 0 起），
  多个标签是「都要有」；匿名时返回的 count 不是总数，所以第一次用到某个搜索条件时先倍增再二分，
  试出整页点赞都够的页数（不带关键词约 20 次请求，启动时在后台先试；带标签的多半两三次）。
  sort=likes 第 100 页约 2800 赞、第 400 页约 1200 赞、第 1000 页约 640 赞；
- GET /autocomplete/tags?query=<词> 补全标签（英文，如 hu_tao、raiden_shogun、genshin_impact）；
- GET /video/<id> 返回 fileUrl（带 expires），再带 X-Version = sha1("<文件 id>_<expires>_<密钥>")
  请求 fileUrl 得到各清晰度（Source、540、360）的地址；Source 动辄一两百 MB，优先取 540。

分级：站内只分 general、ecchi 两档，ecchi 里穿着衣服的舞蹈和性内容混在一起，所以整站按 R18 处理，
只取 ecchi，并且要有 R18 的迹象：标签或标题里有性内容的词，或者是 Koikatsu、Blender、HMV 一类
（几乎都是性内容）；标题写明「No R-18」「健全」的不要。目检 24 段时没有这条规则有 5 段是穿着衣服的
MMD 舞蹈；按规则验算 400 个 ecchi 视频放行 307 个，筛掉的多是没写任何 R18 迹象的舞蹈。片长多在 2~7 分钟。

未成年：标签没有统一的年龄标注。黑名单查标签和标题，再把每个标签经 Danbooru 补全成标签，是角色的
按 Danbooru 统计的 loli / shota 比例过滤儿童设定的角色（可莉、纳西妲等），中文关键词也经 Danbooru 翻译。
"""

import asyncio
import hashlib
import json
import random
import re
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger

from ..models import ANIME, EXPLICIT, Album, DrawContext, WorkRef
from ..net import NETWORK_ERRORS, HttpClient, HttpError
from ..sources.base import Source, split_terms
from ..sources.danbooru import CHARACTER_CATEGORY, QUALIFIER_RE, DanbooruSource, tag_word
from ..util import TTLCache
from .base import Recent, VideoFiles, count_pages, is_mmd, young_word

API = "https://api.iwara.tv"
SITE = "https://www.iwara.tv"
# 网页版脚本里写死的密钥，用来生成 X-Version
FILE_SECRET = "_5nFp9kmbNnHdAFhaqMvt"
PAGE_SIZE = 50
# 最多在这么多页里抽：按点赞排到第 1000 页还有 600 多赞
MAX_PAGE = 1000
PAGES_TTL = 6 * 3600
TAG_TTL = 86400
QUALITIES = ("540", "360", "Source")
AI_TAGS = frozenset({"ai", "ai_generated", "aigc", "stable_diffusion"})
# 标签里有这些片段时算有 R18 迹象（Koikatsu、Blender、HS2 是 H 向的 3D 工具，HMV 是 H 向剪辑）
R18_TAG_HINTS = (
    "sex", "r18", "r-18", "nude", "naked", "hentai", "creampie", "blowjob", "anal",
    "cum", "strip", "pussy", "nipple", "uncensored", "squirt", "ahegao", "gangbang",
    "fellatio", "penetration", "dildo", "hmv", "koikatsu", "blender", "honey_select",
    "hs2", "ntr", "netorare", "futanari", "bbc", "paizuri", "masturbat", "orgasm",
    "breeding", "hypno", "exhibition", "doggy", "from_behind", "tentacle", "cowgirl",
    "missionary", "handjob", "hand_job", "titjob", "oppai", "bukkake", "facial",
)
R18_TITLE_RE = re.compile(
    r"r-?18|sex|hmv|ntr|エロ|えっち|エッチ|全裸|裸|做爱|性爱|セックス|中出|骑乘|後入|后入|侵犯|"
    r"口交|淫|自慰|オナ|ちんぽ|おっぱい|プッシー|触手|突かれ|ぱんぱん|逆バニー|コイカツ|恋活|"
    r"anal|hand ?job|cowgirl|doggy|blowjob|creampie|fuck",
    re.I,
)
NOT_R18_RE = re.compile(r"no\s*r-?18|non[- ]?r-?18|非\s*r-?18|健全|全年齢|全年龄|sfw", re.I)
# 每个视频最多换这么多次（被过滤、下载失败时）
TRIES = 4
# 每次抽取最多翻这么多页候选
MAX_FETCHES = 8


def x_version(file_id: str, expires: str) -> str:
    return hashlib.sha1(f"{file_id}_{expires}_{FILE_SECRET}".encode()).hexdigest()


def tag_ids(video: dict) -> list[str]:
    return [
        str(t.get("id"))
        for t in video.get("tags") or []
        if isinstance(t, dict) and t.get("id")
    ]


class IwaraSource(Source):
    key = "iwara"
    name = "Iwara"
    style = ANIME
    intro = "MMD、3D 舞蹈与动画，可搜角色、作品名"
    ratings = frozenset({EXPLICIT})

    def __init__(
        self,
        http: HttpClient,
        cache,
        content,
        opts,
        files: VideoFiles,
        danbooru: DanbooruSource,
        *,
        min_likes: int,
        allow_mmd: bool = True,
    ):
        super().__init__(cache, content, opts)
        self.http = http
        self.files = files
        self.danbooru = danbooru
        self.min_likes = min_likes
        self.allow_mmd = allow_mmd
        self.recent = Recent()
        self.exclude = AI_TAGS if content.block_ai else frozenset()
        # 搜索条件（标签）→ 可以抽的页数
        self._pages = TTLCache(PAGES_TTL, 1024)
        # 关键词 → Iwara 标签（找不到时为 None）
        self._tags = TTLCache(TAG_TTL, 4096)
        # Iwara 标签 → 是否儿童设定的角色
        self._child = TTLCache(TAG_TTL, 8192)

    def accepts(self, ctx: DrawContext) -> bool:
        return super().accepts(ctx) and not ctx.req.random_character

    async def _get(self, path: str, params: dict | None = None, headers=None):
        text = await self.http.get_text(API + path, params=params, headers=headers)
        try:
            return json.loads(text)
        except ValueError as e:
            raise HttpError(f"{self.name} 返回的不是 JSON") from e

    # ---- 关键词 ----

    async def _autocomplete(self, word: str) -> str | None:
        data = await self._get("/autocomplete/tags", {"query": word})
        ids = [
            str(r["id"])
            for r in (data.get("results") or [] if isinstance(data, dict) else [])
            if isinstance(r, dict) and r.get("id")
        ]
        return word if word in ids else (ids[0] if ids else None)

    async def resolve(self, term: str, ctx: DrawContext) -> str | None:
        """关键词 → Iwara 标签：先按原词补全，补不到时经 Danbooru 译成英文标签（去掉消歧义括号）再补全。"""
        word = tag_word(term)
        if word is None:
            return None

        async def lookup() -> str | None:
            if word.isascii():
                found = await self._autocomplete(word)
                if found:
                    return found
            hit = await self.danbooru.resolve(term, ctx.index)
            if hit is None:
                return None
            plain = QUALIFIER_RE.sub("", hit[0]) or hit[0]
            return await self._autocomplete(plain)

        return await self._tags.load(word, lookup)

    # ---- 过滤 ----

    async def is_child(self, tag: str) -> bool:
        """标签经 Danbooru 补全后是角色，且该角色多为儿童设定。"""

        async def load() -> bool:
            hit = await self.danbooru.resolve(tag, None)
            if hit is None or hit[1] != CHARACTER_CATEGORY:
                return False
            return await self.danbooru.is_child(hit[0])

        return await self._child.load(tag, load)

    def _reject(self, video: dict, local: set[str]) -> str | None:
        if video.get("rating") != "ecchi":
            return f"分级是 {video.get('rating')}"
        if video.get("private") or video.get("unlisted") or video.get("embedUrl"):
            return "不是公开的站内视频"
        file = video.get("file")
        if not isinstance(file, dict) or file.get("type", "video") != "video":
            return "没有视频文件"
        if int(video.get("numLikes") or 0) < self.min_likes:
            return "点赞太少"
        tags = tag_ids(video)
        reason = self.content.plain_tags_reason(tags)
        if reason:
            return reason
        hit = next((t for t in tags if t in self.exclude or t in local), None)
        if hit:
            return f"带排除的标签 {hit}"
        title = str(video.get("title") or "")
        term = self.content.blacklist.hit([title]) or young_word([title, *tags])
        if term:
            return f"标题或标签命中 {term}"
        if NOT_R18_RE.search(title):
            return "标题写明不是 R18"
        if not self.allow_mmd and is_mmd(tags, title):
            return "是 MMD 视频"
        if not R18_TITLE_RE.search(title) and not any(
            hint in tag for tag in tags for hint in R18_TAG_HINTS
        ):
            return "看不出是 R18（多为穿着衣服的舞蹈）"
        return self.files.reason((file or {}).get("duration"))

    async def _child_tag(self, video: dict) -> str | None:
        tags = tag_ids(video)
        flags = await asyncio.gather(*map(self.is_child, tags))
        return next((t for t, child in zip(tags, flags) if child), None)

    # ---- 抽取 ----

    async def _list(self, tags: list[str], page: int) -> list[dict]:
        params = {
            "rating": "ecchi",
            "sort": "likes",
            "limit": str(PAGE_SIZE),
            "page": str(page),
        }
        if tags:
            params["tags"] = ",".join(tags)
        data = await self._get("/videos", params)
        results = data.get("results") if isinstance(data, dict) else None
        return [v for v in results or [] if isinstance(v, dict)]

    async def _full(self, tags: list[str], page: int) -> bool:
        """这一页是满的，且最后一个（点赞最少的）也达到最低点赞。"""
        videos = await self._list(tags, page)
        return (
            len(videos) >= PAGE_SIZE
            and int(videos[-1].get("numLikes") or 0) >= self.min_likes
        )

    async def pages(self, tags: list[str]) -> int:
        """可以抽的页数：整页点赞都够的页，再加上后面一页（其中点赞不够的抽到时过滤）。"""

        async def load() -> int:
            return await count_pages(lambda p: self._full(tags, p), 0, MAX_PAGE)

        return await self._pages.load(",".join(tags), load)

    async def _random_page(self, tags: list[str]) -> list[dict]:
        return await self._list(tags, random.randrange(await self.pages(tags)))

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        positive, negative = split_terms(ctx.req.keywords)
        try:
            resolved = await asyncio.gather(*(self.resolve(w, ctx) for w in positive))
            missing = [w for w, tag in zip(positive, resolved) if tag is None]
            if missing:
                return [], [f"{self.name} 找不到「{missing[0]}」对应的标签"]
            tags = list(dict.fromkeys(resolved))
            for tag in tags:
                if await self.is_child(tag):
                    return [], [f"「{tag}」多为儿童设定，不提供"]
            excluded = await asyncio.gather(*(self.resolve(w, ctx) for w in negative))
        except NETWORK_ERRORS as e:
            logger.warning(f"[random_pic] {self.name} 请求失败: {e!r}")
            return [], [f"{self.name} 请求失败"]
        local = {t for t in excluded if t} | {tag_word(w) or w for w in negative}

        seen: set[str] = set()
        queue: list[dict] = []
        fetches = 0
        lock = asyncio.Lock()

        async def pick() -> dict | None:
            nonlocal fetches
            async with lock:
                while True:
                    while queue:
                        video = queue.pop()
                        vid = video.get("id")
                        key = f"{self.key}:{vid}"
                        if vid in seen or key in self.recent:
                            continue
                        reason = self._reject(video, local)
                        if reason is None:
                            seen.add(vid)
                            self.recent.add(key)
                            return video
                        logger.debug(f"[random_pic] 跳过 Iwara {vid}: {reason}")
                    if fetches >= MAX_FETCHES:
                        return None
                    fetches += 1
                    videos = await self._random_page(tags)
                    random.shuffle(videos)
                    queue.extend(videos)

        async def attempt() -> Album | None:
            video = await pick()
            if video is None:
                return None
            child = await self._child_tag(video)
            if child:
                logger.info(
                    f"[random_pic] 丢弃 Iwara {video.get('id')}: {child} 多为儿童设定"
                )
                return None
            return await self._album(video)

        albums, errors = await self.collect(n, TRIES, attempt)
        if not albums and fetches >= MAX_FETCHES:
            what = f"「{' '.join(positive)}」" if tags else ""
            errors.append(f"{self.name} 搜不到{what}符合条件的视频")
        return albums, errors

    async def file_url(self, video_id: str) -> str | None:
        """视频文件的下载地址（优先 540p），取不到时返回 None。"""
        detail = await self._get(f"/video/{video_id}")
        url = detail.get("fileUrl") if isinstance(detail, dict) else None
        file_id = (detail.get("file") or {}).get("id") if url else None
        if not url or not file_id:
            return None
        expires = parse_qs(urlparse(url).query).get("expires", [""])[0]
        text = await self.http.get_text(
            url, headers={"X-Version": x_version(file_id, expires)}
        )
        files = json.loads(text)
        if not isinstance(files, list):
            return None
        by_name = {
            str(f.get("name")): ((f.get("src") or {}).get("view") or "")
            for f in files
            if isinstance(f, dict)
        }
        for quality in QUALITIES:
            src = by_name.get(quality)
            if src:
                return "https:" + src if src.startswith("//") else src
        return None

    async def _album(self, video: dict) -> Album | None:
        url = await self.file_url(str(video["id"]))
        if url is None:
            logger.warning(f"[random_pic] Iwara {video.get('id')} 没有视频地址")
            return None
        path = await self.files.download(url)
        if path is None:
            return None
        file = video.get("file") or {}
        user = (video.get("user") or {}).get("name")
        details = []
        if user:
            details.append(f"作者：{user}")
        tags = tag_ids(video)
        if tags:
            details.append(f"标签：{', '.join(tags[:8])}")
        details.append(f"点赞：{video.get('numLikes')}")
        details.append(f"链接：{SITE}/video/{video['id']}")
        return Album(
            source=self.name,
            title=" ".join(str(video.get("title") or "").split()) or "无标题",
            total=1,
            pictures=[(1, path)],
            details=details,
            work=WorkRef(self.key, str(video["id"])),
            duration=file.get("duration"),
        )
