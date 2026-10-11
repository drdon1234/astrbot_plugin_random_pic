"""视频文件下载、大小与时长限制，以及临时文件清理。"""

import asyncio
import uuid
from pathlib import Path

import aiohttp
from astrbot.api import logger

from ..utils import duration_text
from .http import CONNECT_TIMEOUT, HttpClient, HttpError

MB = 1024 * 1024
# 视频比图片大得多：总时长不设短上限，但每次读取最多等这么多秒
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(
    total=900, sock_connect=CONNECT_TIMEOUT, sock_read=60
)
CHUNK = 256 * 1024
VIDEO_SUFFIX = ".mp4"
# 短于这么多秒的不要：Danbooru 有不少 1~3 秒的循环小动图，算不上视频
MIN_SECONDS = 5


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

    def new_path(self) -> Path:
        """下载目录里一个新的视频文件名。"""
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root / f"{uuid.uuid4().hex}{VIDEO_SUFFIX}"

    async def download(self, url: str, headers: dict | None = None) -> Path | None:
        """下载一个 mp4，返回本地文件；超过大小上限、不是 mp4 或下载失败时返回 None。"""
        path = self.new_path()
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
