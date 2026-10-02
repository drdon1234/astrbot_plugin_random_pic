"""图源：Danbooru（二次元），E-Hentai、哔咔、各 WordPress 写真站与禁漫天堂（三次元）。

SourceSet 按配置装配所有图源，是抽图、/pdf、链接识别和帮助里「有哪些图源」的唯一来源。
"""

from pathlib import Path

from astrbot.api import logger

from ..filters import ContentFilter
from ..models import RATING_NAMES, STYLE_NAMES, DrawOptions, WorkRef
from ..net import HttpClient, ImageCache
from ..settings import Settings
from .base import Source
from .danbooru import DanbooruSource
from .ehentai import EHentaiSource
from .ehentai_api import EHentai
from .pica import Picacomic
from .wordpress import SITES, WordPressSource

# 禁漫天堂依赖 jmcomic，没装上时其他图源照常可用
JM_KEY = "jmcomic"
JM_NAME = "禁漫天堂"


class SourceSet:
    def __init__(
        self,
        settings: Settings,
        http: HttpClient,
        cache: ImageCache,
        content: ContentFilter,
        opts: DrawOptions,
        data_dir: Path,
        eh_site: str,
    ):
        """eh_site 是实际使用的 E-Hentai 站点（见 ehentai_api.resolve_site）。"""
        conf = settings.sources
        self.danbooru = DanbooruSource(
            http, cache, content, opts, min_score=conf.danbooru.min_score
        )
        self.ehentai = EHentaiSource(
            EHentai(http, eh_site),
            cache,
            content,
            opts,
            min_stars=conf.ehentai.min_stars,
        )
        pica = Picacomic(
            http,
            cache,
            content,
            opts,
            conf.pica.email,
            conf.pica.password,
            token_path=data_dir / "pica_token.json",
        )
        wordpress = [
            WordPressSource(site, http, cache, content, opts) for site in SITES
        ]
        self.all: list[Source] = [self.danbooru, self.ehentai, pica, *wordpress]
        # 图源键 → 不能用的原因（装不上的图源）
        self.missing: dict[str, str] = {}
        try:
            from .jm import JMComicSource
        except ImportError as e:
            if conf.jmcomic.enabled:
                logger.warning(
                    f"[random_pic] 禁漫天堂图源不可用，缺少依赖 jmcomic：{e!r}"
                )
            self.missing[JM_KEY] = f"{JM_NAME}不可用（缺少依赖 jmcomic）"
        else:
            self.all.append(
                JMComicSource(
                    cache,
                    content,
                    opts,
                    domain=conf.jmcomic.domain,
                    proxy=conf.proxy,
                    min_likes=conf.jmcomic.min_likes,
                )
            )
        self._by_key = {source.key: source for source in self.all}
        # 配置里停用的图源键：不参与抽图，/全集 也不再获取它们的作品
        self.disabled = {key for key in self.keys if not conf.site(key).enabled}
        for source in self.all:
            source.scope = conf.site(source.key).ratings
            if source.unavailable and source.key not in self.disabled:
                logger.info(
                    f"[random_pic] {source.name} 不参与抽图：{source.unavailable}"
                )
        # 参与抽图的图源和比例：启用、能用、适用分级和站点能判定的分级有交集
        self.drawing: list[tuple[Source, int]] = [
            (source, conf.site(source.key).weight)
            for source in self.all
            if source.key not in self.disabled
            and not source.unavailable
            and source.usable_ratings
        ]
        # 其他三次元图源没抽够时由 E-Hentai 补（它停用时不补）
        self.fallback: Source | None = next(
            (s for s, _ in self.drawing if s is self.ehentai), None
        )

    @property
    def keys(self) -> list[str]:
        """所有图源键，包括没装上的。"""
        return [*self._by_key, *self.missing]

    def find(self, key: str) -> tuple[Source | None, str]:
        """按图源键找能用的图源，返回 (图源, 不能用的原因)。"""
        source = self._by_key.get(key)
        if key in self.disabled:
            return None, f"{source.name if source else JM_NAME}已在图源管理里停用"
        if source is None:
            return None, self.missing.get(key, f"未知的图源 {key}")
        if source.unavailable:
            return None, source.unavailable
        return source, ""

    def links(self, text: str) -> list[WorkRef]:
        """文字里的作品链接，按出现顺序去重。"""
        found = [hit for source in self.all for hit in source.links(text or "")]
        return list(dict.fromkeys(ref for _, ref in sorted(found, key=lambda f: f[0])))

    def help_lines(self) -> list[str]:
        lines = ["图源（同一类图按比例混合）："]
        for source, _ in self.drawing:
            ratings = "/".join(
                RATING_NAMES[r] for r in RATING_NAMES if r in source.usable_ratings
            )
            lines.append(
                f"· {source.name}（{STYLE_NAMES[source.style]}·{ratings}）：{source.intro}"
            )
        return lines

    async def close(self):
        for source in self.all:
            await source.close()
