"""完整作品：查询并检查能否发送、随机抽一个完整作品、下载整个作品。

/全集、/pdf、/抽图 全集 和定时推送共用；PDF 的打包与复用见 pdf.PdfStore。
"""

import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from astrbot.api import logger

from .access import AccessControl
from .drawer import Drawer
from .models import EXPLICIT, RATING_NAMES, Album, DrawRequest, Work, WorkRef
from .net import NETWORK_ERRORS
from .sources import SourceSet
from .sources.base import Source, SourceError

# 随机完整作品：抽到的作品不能发送（被过滤、分级不符）时最多抽这么多次
WORK_ATTEMPTS = 3
# 最多同时下载这么多个完整作品（合并转发用；PDF 由 PdfStore 排队）
DOWNLOAD_CONCURRENCY = 2


def describe(source: Source, work: Work, how: str) -> str:
    """发送完整作品前的说明：标题、来源、分级、页数和发送方式。"""
    rating = RATING_NAMES[work.rating or EXPLICIT]
    return (
        f"《{work.title or '无标题'}》\n"
        f"{source.name} · {rating} · {work.pages} 页\n{how}"
    )


class WorkService:
    def __init__(
        self,
        sources: SourceSet,
        access: AccessControl,
        drawer: Drawer,
        tmp_dir: Path,
    ):
        self.sources = sources
        self.access = access
        self.drawer = drawer
        self.tmp = tmp_dir
        self._downloads = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async def lookup(
        self, ref: WorkRef, is_private: bool, user_id: str | None = None
    ) -> tuple[Source | None, Work | None, str]:
        """查询作品并检查能否在这个会话发送，返回 (图源, 作品, 不能发送的原因)。"""
        source, why = self.sources.find(ref.source)
        if source is None:
            return None, None, f"{why}，无法获取完整作品。"
        try:
            work = await source.work(ref)
        except Exception as e:  # 登录失败、网络错误、站点改版等都要告诉用户
            expected = isinstance(e, (SourceError, *NETWORK_ERRORS))
            logger.warning(
                f"[random_pic] 查询作品 {ref} 失败: {e!r}", exc_info=not expected
            )
            return None, None, f"查询作品失败：{str(e) or repr(e)}"
        if work is None:
            return None, None, "作品不存在或已被删除。"
        denied = self.access.work_gate(work, is_private, user_id)
        if denied:
            return None, None, denied
        return source, work, ""

    async def random(
        self, req: DrawRequest, is_private: bool, user_id: str | None = None
    ) -> tuple[Album, Source, Work] | str:
        """按请求抽一个图集并查询它所在的作品，返回 (图集, 图源, 作品)，抽不到时返回原因。

        作品查不到或不能发送时重抽，最多 WORK_ATTEMPTS 次。
        """
        why = ""
        allow_explicit = self.access.explicit_allowed(is_private, user_id)
        for _ in range(WORK_ATTEMPTS):
            result = await self.drawer.draw(req, allow_explicit)
            if not result.albums:
                return result.reason()
            album = result.albums[0]
            if album.work is None:
                why = f"《{album.title}》没有作品信息"
                continue
            source, work, why = await self.lookup(album.work, is_private, user_id)
            if source is not None:
                return album, source, work
            logger.info(f"[random_pic] 随机完整作品《{album.title}》不能发送：{why}")
        return f"连续 {WORK_ATTEMPTS} 次抽到的作品都不能发送（{why.rstrip('。')}）"

    @asynccontextmanager
    async def download(
        self, source: Source, work: Work
    ) -> AsyncIterator[tuple[list[Path], int]]:
        """下载整个作品到临时目录，得到 (以页码命名的图片, 失败页数)；用完后删除。"""
        tmp = self.tmp / f"{work.ref.key}-{uuid.uuid4().hex[:8]}"
        try:
            async with self._downloads:
                yield await source.download_work(work, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def whole_album(
        source: Source, work: Work, paths: list[Path], album: Album | None = None
    ) -> Album:
        """整个作品作为一个图集；抽到的图集有说明文字时沿用。"""
        return Album(
            source=album.source if album else source.name,
            title=work.title or (album.title if album else ""),
            total=work.pages,
            pictures=[(int(path.stem), path) for path in paths],
            details=list(album.details) if album else [],
            work=work.ref,
        )
