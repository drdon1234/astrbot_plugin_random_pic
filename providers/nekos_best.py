"""nekos.best v2：二次元 × 全年龄，免 key。"""

import random

from ..models import ANIME, GENERAL, ImageItem, PicRequest
from .base import Provider

API = "https://nekos.best/api/v2/{category}"
IMAGE_CATEGORIES = ("neko", "waifu", "husbando", "kitsune")


class NekosBestProvider(Provider):
    name = "nekos_best"
    display = "nekos.best"
    returns_tags = False

    def build_combos(self):
        categories = [
            c for c in self.conf.get("categories", []) if c in IMAGE_CATEGORIES
        ]
        return {(ANIME, GENERAL): categories or list(IMAGE_CATEGORIES)}

    def unsupported(self, req: PicRequest) -> str | None:
        reason = super().unsupported(req)
        if reason:
            return reason
        # 仅支持把标签当作分类名使用
        bad = [t for t in req.tags if t.lower() not in IMAGE_CATEGORIES]
        if bad:
            return f"不支持标签 {' '.join(bad)}"
        return None

    async def fetch(self, req: PicRequest, n: int) -> list[ImageItem]:
        categories = [t.lower() for t in req.tags] or self.combos[
            (req.style, req.rating)
        ]
        category = random.choice(categories)
        data = await self.http.get_json(
            API.format(category=category),
            params={"amount": min(n, 20)},
            proxy=self.proxy,
        )
        items = []
        for r in data.get("results", []):
            if not r.get("url"):
                continue
            items.append(
                ImageItem(
                    image_url=r["url"],
                    rating=GENERAL,
                    style=ANIME,
                    provider=self.display,
                    author=r.get("artist_name") or "",
                    source_url=r.get("source_url") or "",
                )
            )
        return items
