"""风格 × 分级路由：(style, rating) → 有序图源列表，失败时回退到下一个图源。"""

from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

from .filters import TagBlacklist, check_item
from .models import EXPLICIT, RATINGS, STYLES, ImageItem, PicRequest
from .net import ImageCache
from .providers import Provider

DEFAULT_ROUTES = {
    "anime_general": ["nekos_best", "waifu_im", "danbooru", "wallhaven"],
    "anime_sensitive": ["danbooru", "lolicon", "wallhaven", "yandere"],
    "anime_explicit": ["lolicon", "danbooru", "yandere", "waifu_im", "wallhaven"],
    "real_general": ["wallhaven", "cn_fallback"],
    "real_sensitive": ["wallhaven", "cn_fallback"],
    "real_explicit": ["wallhaven"],
}


@dataclass
class FetchResult:
    images: list[tuple[ImageItem, Path]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def route_key(style: str, rating: str) -> str:
    return f"{style}_{rating}"


class Router:
    def __init__(
        self,
        providers: dict[str, Provider],
        routes_conf: dict,
        blacklist: TagBlacklist,
        cache: ImageCache,
        max_rounds: int,
    ):
        self.providers = providers
        self.blacklist = blacklist
        self.cache = cache
        self.max_rounds = max(1, max_rounds)
        self.routes = self.build_routes(routes_conf)

    def build_routes(self, routes_conf: dict) -> dict[tuple[str, str], list[Provider]]:
        routes = {}
        for style in STYLES:
            for rating in RATINGS:
                key = route_key(style, rating)
                names = routes_conf.get(key, DEFAULT_ROUTES[key])
                chain = []
                for name in names:
                    provider = self.providers.get(str(name).strip())
                    if provider is None:
                        logger.warning(
                            f"[random_pic] 路由 {key} 中的图源 {name} 不存在"
                        )
                    elif (style, rating) not in provider.combos:
                        logger.warning(
                            f"[random_pic] 图源 {name} 不支持组合 {key}，已跳过"
                        )
                    elif rating == EXPLICIT and not provider.returns_tags:
                        logger.warning(
                            f"[random_pic] 图源 {name} 不返回标签，不得用于 R18"
                        )
                    else:
                        chain.append(provider)
                routes[(style, rating)] = chain
        return routes

    async def fetch(self, req: PicRequest, is_private: bool) -> FetchResult:
        result = FetchResult()
        seen: set[str] = set()
        chain = self.routes[(req.style, req.rating)]
        if not chain:
            result.errors.append("该组合没有可用图源")
        for provider in chain:
            if len(result.images) >= req.count:
                break
            reason = provider.unsupported(req)
            if reason:
                result.errors.append(f"{provider.display}: {reason}")
                continue
            await self._drain(provider, req, is_private, seen, result)
        return result

    async def _drain(
        self,
        provider: Provider,
        req: PicRequest,
        is_private: bool,
        seen: set[str],
        result: FetchResult,
    ):
        """从单个图源取图，被丢弃的结果会重抽，最多 max_rounds 轮。"""
        discarded = 0
        for _ in range(self.max_rounds):
            need = req.count - len(result.images)
            if need <= 0:
                return
            try:
                items = await provider.fetch(req, need)
            except Exception as e:
                logger.warning(f"[random_pic] 图源 {provider.name} 请求失败: {e!r}")
                result.errors.append(f"{provider.display}: 请求失败")
                return
            if not items:
                result.errors.append(f"{provider.display}: 无结果")
                return
            for item in items:
                if len(result.images) >= req.count:
                    return
                reason = check_item(item, req.rating, is_private, self.blacklist)
                if reason:
                    logger.info(
                        f"[random_pic] 丢弃 {item.post_url or item.image_url}: {reason}"
                    )
                    discarded += 1
                    continue
                if item.post_url:
                    if item.post_url in seen:
                        continue
                    seen.add(item.post_url)
                path = await self.cache.download(
                    [item.image_url, item.alt_url], provider.proxy
                )
                if path is None:
                    discarded += 1
                    continue
                result.images.append((item, path))
        if discarded:
            result.errors.append(f"{provider.display}: {discarded} 张被过滤或下载失败")
