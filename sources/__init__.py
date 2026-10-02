"""图源：Danbooru（二次元），E-Hentai、哔咔、各 WordPress 写真站与禁漫天堂（三次元）。

SourceSet 按配置装配所有图源，是抽图、/pdf、链接识别和帮助里「有哪些图源」的唯一来源。
"""

from pathlib import Path

from astrbot.api import logger

from ..filters import ContentFilter
from ..models import REAL, DrawOptions, WorkRef
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
        sites = settings.sites
        self.danbooru = DanbooruSource(http, cache, content, opts)
        self.ehentai = EHentaiSource(EHentai(http, eh_site), cache, content, opts)
        pica = Picacomic(
            http,
            cache,
            content,
            opts,
            sites.pica_email,
            sites.pica_password,
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
            logger.warning(f"[random_pic] 禁漫天堂图源不可用，缺少依赖 jmcomic：{e!r}")
            self.missing[JM_KEY] = "禁漫天堂不可用（缺少依赖 jmcomic）"
        else:
            self.all.append(
                JMComicSource(
                    cache,
                    content,
                    opts,
                    domain=sites.jmcomic_domain,
                    proxy=sites.proxy,
                )
            )
        self._by_key = {source.key: source for source in self.all}
        for source in self.all:
            if source.unavailable:
                logger.info(
                    f"[random_pic] {source.name} 不参与抽图：{source.unavailable}"
                )
        # 三次元按权重混合：E-Hentai 总在其中（权重为 0 时只用来补其他图源没抽够的图集），
        # 其他图源权重大于 0 且能用时参与
        weights = settings.sources.weights
        self.real: list[tuple[Source, int]] = [
            (self.ehentai, weights[self.ehentai.key])
        ]
        self.real += [
            (source, weights[source.key])
            for source in self.all
            if source.style == REAL
            and source is not self.ehentai
            and weights[source.key] > 0
            and not source.unavailable
        ]

    @property
    def keys(self) -> list[str]:
        """所有图源键，包括没装上的。"""
        return [*self._by_key, *self.missing]

    def find(self, key: str) -> tuple[Source | None, str]:
        """按图源键找能用的图源，返回 (图源, 不能用的原因)。"""
        source = self._by_key.get(key)
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
        danbooru = self.danbooru
        lines = [
            f"二次元：{danbooru.name}（{danbooru.intro}）",
            "三次元图源（按比例混合）：",
        ]
        lines += [
            f"· {source.name}：{source.intro}" for source, weight in self.real if weight
        ]
        return lines

    async def close(self):
        for source in self.all:
            await source.close()
