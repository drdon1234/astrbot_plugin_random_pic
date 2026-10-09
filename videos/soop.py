"""SOOP（原 AfreecaTV，韩国直播站）的 Catch 短视频：女主播的舞蹈片段，只有擦边。

Catch 是站内的竖屏短视频（多为粉丝从直播里剪的 10~60 秒片段）。2026-10 截帧目检 Talk/Cam 分区里
带「여캠」（女主播）标签、标题或标签写着舞蹈 / 性感的片段：近一周 12/12、近一个月 12/12 都是
紧身短裙、黑丝、深 V 一类的擦边舞蹈，近一年高播放的 9/12（没过的 3 段是男主播团 C9 的反应片段，
按标签排除）。站方规定非 19 禁内容不能露点，19 禁的（grade 19，约四分之一）要成人登录才能看，不取。

接口（匿名，2026-10 实测）：
- GET sch.sooplive.co.kr/api.php?m=categoryContentsList&szType=catch&szCateNo=<分区>&szTerm=<范围>
  &szOrder=view_cnt&nListCnt=120&nPageNo=N：按播放量从高到低分页，每个时间范围最多 84 页（约 1 万个），
  近一个月的第 84 页约 800 次播放，近一年的约 1 万次；3month、all 返回的是两三年前的旧数据，不用；
- POST api.m.sooplive.co.kr/station/video/a/view（nTitleNo、nApiLevel=10）：files[0].file 是 HLS
  主播放列表（540p 和原画，fMP4 分片）；接口里的 mp4_path 直链会 502。
"""

import asyncio
import json
import random
import re

from astrbot.api import logger

from ..models import REAL, SENSITIVE, Album, DrawContext, WorkRef
from ..net import NETWORK_ERRORS, HttpClient, HttpError
from ..sources.base import Source, split_terms
from ..util import TTLCache
from .base import Recent, RecentAuthors, VideoFiles, count_pages, young_word
from . import hls
from .hls import HlsDownloader

LIST_API = "https://sch.sooplive.co.kr/api.php"
VIEW_API = "https://api.m.sooplive.co.kr/station/video/a/view"
PLAYER = "https://vod.sooplive.co.kr/player/{}/catch"
HEADERS = {"Referer": "https://www.sooplive.co.kr/"}
CATEGORY = "00130000"  # Talk/Cam
# 从这两个时间范围里抽（近一年的只有播放量最高的约 1 万个）
TERMS = ("1month", "1year")
PAGE_SIZE = 120
MAX_PAGES = 84
PAGES_TTL = 6 * 3600
FEMALE_TAG = "여캠"
# 标题或标签里的舞蹈、性感一类的词（标题只写主播名加爱心的多是舞蹈片段）
DANCE_RE = re.compile(
    r"댄스|춤|섹시|엑셀|직캠|골반|웨이브|트월킹|dance|sexy|twerk|❤|♥|💘|💖", re.I
)
# 男主播团的标签：他们的反应片段也带「여캠」「댄스」
CREW_TAGS = frozenset({"c9", "씨나인", "철구", "배틀그라운드"})
TITLE_PREFIX_RE = re.compile(r"^\s*\[(?:캐치|catch|클립)\]\s*", re.I)
TRIES = 4
AUTHOR_WINDOW = 10
MAX_FETCHES = 6


def seconds(text) -> float | None:
    """「0:29」「1:02:03」→ 秒数。"""
    try:
        parts = [int(p) for p in str(text).split(":")]
    except ValueError:
        return None
    total = 0
    for part in parts:
        total = total * 60 + part
    return float(total) if parts else None


def hashtags(item: dict) -> list[str]:
    return [str(t) for t in item.get("hash_tags") or [] if t]


class SoopSource(Source):
    key = "soop"
    name = "SOOP"
    style = REAL
    intro = "韩国女主播的舞蹈短片"
    ratings = frozenset({SENSITIVE})

    def __init__(
        self,
        http: HttpClient,
        cache,
        content,
        opts,
        files: VideoFiles,
        *,
        min_views: int,
    ):
        super().__init__(cache, content, opts)
        self.http = http
        self.files = files
        self.hls = HlsDownloader(http, files.max_bytes)
        self.min_views = min_views
        self.recent = Recent()
        self.authors = RecentAuthors(AUTHOR_WINDOW)
        self._pages = TTLCache(PAGES_TTL, len(TERMS))

    @property
    def unavailable(self) -> str | None:
        return None if hls.ffmpeg_path() else "没有安装 ffmpeg"

    def accepts(self, ctx: DrawContext) -> bool:
        """不能按关键词搜索：带要搜的关键词或随机角色时不参与（只带排除词可以）。"""
        positive, _ = split_terms(ctx.req.keywords)
        return super().accepts(ctx) and not positive and not ctx.req.random_character

    async def _list(self, term: str, page: int) -> list[dict]:
        params = {
            "m": "categoryContentsList",
            "szType": "catch",
            "nPageNo": str(page),
            "nListCnt": str(PAGE_SIZE),
            "szPlatform": "pc",
            "szTerm": term,
            "szFileType": "ALL",
            "szCateNo": CATEGORY,
            "szOrder": "view_cnt",
            "strmLangType": "",
        }
        text = await self.http.get_text(LIST_API, params=params, headers=HEADERS)
        try:
            data = json.loads(text)
        except ValueError as e:
            raise HttpError(f"{self.name} 返回的不是 JSON") from e
        items = ((data or {}).get("data") or {}).get("list") if isinstance(data, dict) else None
        return [i for i in items or [] if isinstance(i, dict)]

    async def _full(self, term: str, page: int) -> bool:
        items = await self._list(term, page)
        return (
            len(items) >= PAGE_SIZE
            and int(items[-1].get("view_cnt") or 0) >= self.min_views
        )

    async def pages(self, term: str) -> int:
        async def load() -> int:
            return await count_pages(lambda p: self._full(term, p), 1, MAX_PAGES)

        return await self._pages.load(term, load)

    async def _random_page(self) -> list[dict]:
        counts = [await self.pages(term) for term in TERMS]
        index = random.randrange(sum(counts))
        for term, count in zip(TERMS, counts):
            if index < count:
                return await self._list(term, index + 1)
            index -= count
        return []

    def _reject(self, item: dict, local: set[str]) -> str | None:
        if int(item.get("grade") or 0) != 0:
            return "19 禁"
        if int(item.get("view_cnt") or 0) < self.min_views:
            return "播放太少"
        tags = hashtags(item)
        low = {t.lower() for t in tags}
        if FEMALE_TAG not in low:
            return "不是女主播"
        crew = next((t for t in low if t in CREW_TAGS), None)
        if crew:
            return f"是男主播团的片段（{crew}）"
        title = str(item.get("title") or "")
        if not DANCE_RE.search(" ".join([title, *tags])):
            return "看不出是舞蹈片段"
        term = self.content.blacklist.hit([title, *tags]) or young_word([title, *tags])
        if term:
            return f"标题或标签命中 {term}"
        text = " ".join([title, *tags]).lower()
        hit = next((w for w in local if w in text), None)
        if hit:
            return f"命中排除的关键词 {hit}"
        return self.files.reason(seconds(item.get("duration")))

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        _, negative = split_terms(ctx.req.keywords)
        local = {w.lower() for w in negative}
        try:
            await asyncio.gather(*(self.pages(term) for term in TERMS))
        except NETWORK_ERRORS as e:
            logger.warning(f"[random_pic] {self.name} 请求失败: {e!r}")
            return [], [f"{self.name} 请求失败"]
        seen: set[str] = set()
        queue: list[dict] = []
        fetches = 0
        lock = asyncio.Lock()

        async def pick() -> dict | None:
            nonlocal fetches
            async with lock:
                while True:
                    while queue:
                        item = queue.pop()
                        vid = str(item.get("title_no") or "")
                        key = f"{self.key}:{vid}"
                        author = str(item.get("original_user_id") or item.get("user_id") or "")
                        if not vid or vid in seen or key in self.recent or author in self.authors:
                            continue
                        reason = self._reject(item, local)
                        if reason is None:
                            seen.add(vid)
                            self.recent.add(key)
                            self.authors.add(author)
                            return item
                        logger.debug(f"[random_pic] 跳过 {self.name} {vid}: {reason}")
                    if fetches >= MAX_FETCHES:
                        return None
                    fetches += 1
                    items = await self._random_page()
                    random.shuffle(items)
                    queue.extend(items)

        async def attempt() -> Album | None:
            item = await pick()
            return await self._album(item) if item else None

        albums, errors = await self.collect(n, TRIES, attempt)
        if not albums and fetches >= MAX_FETCHES:
            errors.append(f"{self.name} 连续 {fetches} 页都没有符合条件的视频")
        return albums, errors

    async def stream(self, title_no: str) -> str | None:
        """视频的 HLS 地址；19 禁、不存在时返回 None。"""
        async with self.http.request(
            "POST", VIEW_API, data={"nTitleNo": title_no, "nApiLevel": "10"}, headers=HEADERS
        ) as resp:
            if resp.status != 200:
                raise HttpError(f"HTTP {resp.status}", resp.status)
            data = json.loads(await resp.text(errors="replace"))
        data = data.get("data") if isinstance(data, dict) else None
        if not isinstance(data, dict) or int(data.get("grade") or 0) != 0:
            return None
        files = [f for f in data.get("files") or [] if isinstance(f, dict) and f.get("file")]
        return str(files[0]["file"]) if files else None

    async def _album(self, item: dict) -> Album | None:
        vid = str(item["title_no"])
        url = await self.stream(vid)
        if url is None:
            logger.info(f"[random_pic] {self.name} {vid} 取不到视频地址")
            return None
        media, variant = await self.hls.playlist(url, HEADERS)
        path = await self.hls.download(
            media, variant, media.segments, self.files.new_path(), HEADERS
        )
        if path is None:
            return None
        title = TITLE_PREFIX_RE.sub("", str(item.get("title") or "")).strip()
        user = str(item.get("original_user_nick") or item.get("user_nick") or "")
        tags = hashtags(item)
        details = []
        if user:
            details.append(f"主播：{user}")
        if tags:
            details.append(f"标签：{', '.join(tags[:8])}")
        details.append(f"播放：{item.get('view_cnt')}")
        details.append(f"链接：{PLAYER.format(vid)}")
        return Album(
            source=self.name,
            title=title or user or vid,
            total=1,
            pictures=[(1, path)],
            details=details,
            work=WorkRef(self.key, vid),
            duration=media.duration or seconds(item.get("duration")),
        )
