"""Wallhaven：二次元 + 三次元。

categories 三位掩码 general/anime/people：二次元 010，三次元 001；
purity 三位掩码 sfw/sketchy/nsfw：100 全年龄，010 擦边，001 R18（需要 API key）。
搜索结果不含标签，需逐张请求详情接口取标签，用于黑名单过滤。
"""

from ..models import (
    ANIME,
    EXPLICIT,
    GENERAL,
    REAL,
    SENSITIVE,
    ImageItem,
    PicRequest,
)
from .base import Provider

SEARCH_API = "https://wallhaven.cc/api/v1/search"
DETAIL_API = "https://wallhaven.cc/api/v1/w/{id}"
CATEGORIES = {ANIME: "010", REAL: "001"}
PURITIES = {GENERAL: "100", SENSITIVE: "010", EXPLICIT: "001"}
PURITY_MAP = {"sfw": GENERAL, "sketchy": SENSITIVE, "nsfw": EXPLICIT}


class WallhavenProvider(Provider):
    name = "wallhaven"
    display = "Wallhaven"

    def build_combos(self):
        return {
            (style, rating): {"categories": cat, "purity": pur}
            for style, cat in CATEGORIES.items()
            for rating, pur in PURITIES.items()
        }

    def unsupported(self, req: PicRequest) -> str | None:
        reason = super().unsupported(req)
        if reason:
            return reason
        if req.rating == EXPLICIT and not self.conf.get("api_key"):
            return "R18 需要配置 Wallhaven API key"
        return None

    def _params(self, extra: dict) -> dict:
        if self.conf.get("api_key"):
            extra["apikey"] = self.conf["api_key"]
        return extra

    async def fetch(self, req: PicRequest, n: int) -> list[ImageItem]:
        params = dict(self.combos[(req.style, req.rating)])
        params["sorting"] = "random"
        if req.tags:
            params["q"] = " ".join(f"+{t}" for t in req.tags)
        data = await self.http.get_json(
            SEARCH_API, params=self._params(params), proxy=self.proxy
        )
        items = []
        for brief in (data.get("data") or [])[:n]:
            detail = await self.http.get_json(
                DETAIL_API.format(id=brief["id"]),
                params=self._params({}),
                proxy=self.proxy,
            )
            item = self.parse(detail.get("data") or {}, req.style)
            if item:
                items.append(item)
        return items

    def parse(self, w: dict, style: str) -> ImageItem | None:
        rating = PURITY_MAP.get(w.get("purity", ""))
        if not w.get("path") or not rating:
            return None
        return ImageItem(
            image_url=w["path"],
            alt_url=(w.get("thumbs") or {}).get("original", ""),
            rating=rating,
            style=style,
            provider=self.display,
            source_url=w.get("source") or "",
            post_url=w.get("url") or "",
            tags=[t.get("name", "") for t in w.get("tags") or []],
        )
