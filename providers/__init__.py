from ..net import HttpClient
from .base import Provider
from .cn_fallback import CnFallbackProvider
from .danbooru import DanbooruProvider
from .lolicon import LoliconProvider
from .moebooru import KonachanProvider, YandereProvider
from .nekos_best import NekosBestProvider
from .waifu_im import WaifuImProvider
from .wallhaven import WallhavenProvider

PROVIDER_CLASSES: tuple[type[Provider], ...] = (
    NekosBestProvider,
    WaifuImProvider,
    DanbooruProvider,
    LoliconProvider,
    WallhavenProvider,
    YandereProvider,
    KonachanProvider,
    CnFallbackProvider,
)


def build_providers(config: dict, http: HttpClient) -> dict[str, Provider]:
    """按配置实例化全部图源。各图源的配置位于同名配置段，代理按图源单独开关。"""
    proxy_conf = config.get("proxy", {})
    proxy_url = (proxy_conf.get("url") or "").strip() or None
    providers = {}
    for cls in PROVIDER_CLASSES:
        proxy = proxy_url if proxy_conf.get(cls.name) else None
        providers[cls.name] = cls(http, proxy, config.get(cls.name, {}))
    return providers
