"""视频源（实验性）：Danbooru 视频、Iwara、RedGifs 动画（二次元）和 RedGifs Cosplay（三次元），只有 R18。

VideoSet 和图片的 SourceSet 一样提供 drawing（参与抽取的源和比例），交给同一个 Drawer 按比例抽取。
"""

from pathlib import Path

from astrbot.api import logger

from ..filters import ContentFilter
from ..models import ANIME, RATING_NAMES, REAL, STYLE_NAMES, DrawOptions
from ..net import HttpClient, ImageCache
from ..settings import Video
from ..sources.base import Source
from ..sources.danbooru import DanbooruSource
from .base import Recent, VideoFiles
from .danbooru import DanbooruVideoSource
from .iwara import IwaraSource
from .redgifs import RedGifsSource

# 动画片段的标签：RedGifs 动画只要带其中之一的，Cosplay（真人）不要带的
ANIMATION_TAGS = frozenset(
    {"animation", "anime", "3d", "cartoon", "animated", "sfm", "blender", "rule34"}
)


class VideoSet:
    def __init__(
        self,
        conf: Video,
        http: HttpClient,
        cache: ImageCache,
        content: ContentFilter,
        opts: DrawOptions,
        root: Path,
        danbooru: DanbooruSource,
        recent_path: Path | None = None,
    ):
        """danbooru 是图片的 Danbooru 源：Iwara 借它翻译关键词、判断儿童角色。

        recent_path：保存最近抽过的视频的文件，各视频源共用（None 时只在内存里）。
        """
        self.files = VideoFiles(http, root, conf.max_mb, conf.max_seconds)
        self.recent = Recent(recent_path)
        args = (http, cache, content, opts, self.files)
        sources: list[tuple[Source, object]] = [
            (
                DanbooruVideoSource(
                    *args, min_score=conf.danbooru.min_score, allow_mmd=conf.mmd
                ),
                conf.danbooru,
            ),
            (
                IwaraSource(
                    *args,
                    danbooru,
                    min_likes=conf.iwara.min_likes,
                    allow_mmd=conf.mmd,
                ),
                conf.iwara,
            ),
            (
                RedGifsSource(
                    *args,
                    key="redgifs_hentai",
                    name="RedGifs 动画",
                    style=ANIME,
                    tag="Hentai",
                    intro="高赞动画片段",
                    verified_only=False,
                    min_likes=conf.redgifs_hentai.min_likes,
                    require=ANIMATION_TAGS,
                    allow_mmd=conf.mmd,
                ),
                conf.redgifs_hentai,
            ),
            (
                RedGifsSource(
                    *args,
                    key="redgifs_cosplay",
                    name="RedGifs Cosplay",
                    style=REAL,
                    tag="Cosplay",
                    intro="实名认证 coser 的高赞短片",
                    verified_only=True,
                    min_likes=conf.redgifs_cosplay.min_likes,
                    reject=ANIMATION_TAGS,
                    allow_mmd=conf.mmd,
                ),
                conf.redgifs_cosplay,
            ),
        ]
        self.all = [source for source, _ in sources]
        for source in self.all:
            source.recent = self.recent
        self.drawing: list[tuple[Source, int]] = [
            (source, site.weight) for source, site in sources if site.enabled
        ]
        # Drawer 的补位只用于三次元图片
        self.fallback = None

    def prepare(self):
        self.files.prepare()

    async def warm_up(self):
        """后台先试出 Iwara、RedGifs 不带关键词时能抽的页数，第一次抽视频时不用等。"""
        for source, _ in self.drawing:
            try:
                if isinstance(source, IwaraSource):
                    await source.pages([])
                elif isinstance(source, RedGifsSource):
                    await source.pages()
            except Exception as e:
                logger.warning(f"[random_pic] {source.name} 页数预热失败: {e!r}")

    def styles(self, rating: str) -> set[str]:
        """这个分级有视频源的风格。"""
        return {s.style for s, _ in self.drawing if rating in s.usable_ratings}

    def help_lines(self) -> list[str]:
        lines = []
        for source, _ in self.drawing:
            ratings = "/".join(
                RATING_NAMES[r] for r in RATING_NAMES if r in source.usable_ratings
            )
            lines.append(
                f"· {source.name}（{STYLE_NAMES[source.style]}·{ratings}）：{source.intro}"
            )
        return lines
