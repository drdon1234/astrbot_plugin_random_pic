"""视频图源共用的部分：下载视频到临时目录（发完删除）、大小和时长上限、说明文字。

视频源复用图片的抽取框架：一个视频就是一个只有一页的图集（Album），本地文件是 mp4。
"""

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp

from astrbot.api import logger

from ..filters import TagBlacklist
from ..net import CONNECT_TIMEOUT, HttpClient, HttpError
from ..util import duration_text

MB = 1024 * 1024
# 视频比图片大得多：总时长不设短上限，但每次读取最多等这么多秒
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=900, sock_connect=CONNECT_TIMEOUT, sock_read=60
)
CHUNK = 256 * 1024
VIDEO_SUFFIX = ".mp4"
# 短于这么多秒的不要：Danbooru 有不少 1~3 秒的循环小动图，算不上视频
MIN_SECONDS = 5


# 视频的标题、描述和标签里明确指向小学、初中生的词（在内置黑名单之外；视频站没有统一的年龄标签）。
# 雌小鬼、メスガキ多是梗，JK、女子高生、schoolgirl 多是校服装扮，都不算，和图片源一致
YOUNG_WORDS = TagBlacklist(["小学生", "中学生", "女子中学生", "初中生", "jc"])


# MMD（MikuMikuDance）视频的标签和标题写法：Iwara 约三成带 mikumikudance 标签或标题写 MMD，
# Danbooru 的 mikumikudance_(medium) 和 RedGifs 的 MMD 都很少。没标的认不出来
MMD_TAGS = frozenset({"mikumikudance", "mikumikudance_(medium)", "mmd"})
MMD_TITLE_RE = re.compile(r"mmd|mikumikudance|ミクミクダンス", re.I)


def is_mmd(tags: list[str], title: str = "") -> bool:
    return any(t.lower() in MMD_TAGS for t in tags) or bool(MMD_TITLE_RE.search(title))


def young_word(texts: list[str]) -> str | None:
    """返回命中的低龄指向词（内置黑名单和上面的词），没有时返回 None。"""
    return YOUNG_WORDS.hit(texts)


async def count_pages(
    full: Callable[[int], Awaitable[bool]], first: int, limit: int
) -> int:
    """按热度从高到低分页的列表里能抽的页数：整页都达标的页，再加后面一页（不达标的抽到时过滤）。

    full(页) 判断这一页是否整页达标，页码从 first 起，最多 limit 页。先倍增再二分，
    约 2·log2(limit) 次请求。
    """
    if not await full(first):
        return 1
    low, high = 0, 1  # 相对 first 的页：low 整页达标，high 待查
    while await full(first + high):
        if high >= limit - 1:
            return limit
        low, high = high, min(high * 2, limit - 1)
    while high - low > 1:
        mid = (low + high) // 2
        if await full(first + mid):
            low = mid
        else:
            high = mid
    return high + 1


class TooLarge(Exception):
    pass


def is_mp4(head: bytes) -> bool:
    """mp4 文件第 4~8 字节是 ftyp。"""
    return head[4:8] == b"ftyp"


class VideoFiles:
    """视频的下载目录与大小、时长上限。下载的视频发完由调用方删除，启动时清掉上次残留的。"""

    def __init__(self, http: HttpClient, root: Path, max_mb: int, max_seconds: int):
        """max_mb、max_seconds 为 0 时不限。"""
        self.http = http
        self.root = root
        self.max_bytes = max_mb * MB if max_mb > 0 else None
        self.max_seconds = max_seconds if max_seconds > 0 else None

    def prepare(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.glob(f"*{VIDEO_SUFFIX}*"):
            path.unlink(missing_ok=True)

    def too_big(self, size: int | None) -> bool:
        return bool(size) and self.max_bytes is not None and size > self.max_bytes

    def reason(self, duration: float | None, size: int | None = None) -> str | None:
        """按站点给出的时长、大小判断要不要这个视频（未知的不判断，下载时再看大小）。"""
        if duration and duration < MIN_SECONDS:
            return f"时长 {duration:.1f} 秒太短"
        if duration and self.max_seconds is not None and duration > self.max_seconds:
            return f"时长 {duration_text(duration)} 超过上限"
        if self.too_big(size):
            return f"大小 {size / MB:.0f} MB 超过上限"
        return None

    async def download(self, url: str, headers: dict | None = None) -> Path | None:
        """下载一个 mp4，返回本地文件；超过大小上限、不是 mp4 或下载失败时返回 None。"""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{uuid.uuid4().hex}{VIDEO_SUFFIX}"
        ok = False
        try:
            await self._fetch(url, headers, path)
            with path.open("rb") as f:
                ok = is_mp4(f.read(16))
            if not ok:
                logger.warning(f"[random_pic] 下载的不是 mp4 视频: {url}")
        except TooLarge:
            logger.info(f"[random_pic] 视频超过大小上限，已跳过: {url}")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, HttpError) as e:
            logger.warning(f"[random_pic] 视频下载失败 {url}: {e!r}")
        finally:
            # 失败、被取消（同一次抽取的其他视频出错）时都删掉下了一半的文件
            if not ok:
                path.unlink(missing_ok=True)
        return path if ok else None

    async def _fetch(self, url: str, headers: dict | None, path: Path):
        async with self.http.request(
            "GET", url, headers=headers, timeout=DOWNLOAD_TIMEOUT
        ) as resp:
            if resp.status != 200:
                raise HttpError(f"HTTP {resp.status}", resp.status)
            if self.too_big(resp.content_length):
                raise TooLarge
            size = 0
            with path.open("wb") as f:
                async for chunk in resp.content.iter_chunked(CHUNK):
                    size += len(chunk)
                    if self.too_big(size):
                        raise TooLarge
                    f.write(chunk)

    @staticmethod
    def remove(paths: list[Path]):
        for path in paths:
            path.unlink(missing_ok=True)
