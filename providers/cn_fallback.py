"""国内三次元兜底接口：三次元 × 全年龄/擦边。

每个条目是一个直接返回图片（或 302 到图片）的 URL，并标注所属分级。
这类接口不返回标签也没有来源，因此永远不接入 R18。
"""

import random

from astrbot.api import logger

from ..models import GENERAL, RATING_NAMES, REAL, SENSITIVE, ImageItem, PicRequest
from .base import Provider

ALLOWED_RATINGS = (GENERAL, SENSITIVE)
RATING_BY_NAME = {RATING_NAMES[r]: r for r in ALLOWED_RATINGS}


class CnFallbackProvider(Provider):
    name = "cn_fallback"
    display = "国内兜底接口"
    returns_tags = False

    def build_combos(self):
        combos: dict[tuple[str, str], list[dict]] = {}
        for entry in self.conf.get("entries", []):
            url = (entry.get("url") or "").strip()
            rating = RATING_BY_NAME.get(entry.get("rating", ""))
            if not url.startswith(("http://", "https://")) or not rating:
                logger.warning(f"[random_pic] 忽略无效的国内兜底条目: {entry}")
                continue
            combos.setdefault((REAL, rating), []).append(
                {"url": url, "name": entry.get("name") or self.display}
            )
        return combos

    def unsupported(self, req: PicRequest) -> str | None:
        reason = super().unsupported(req)
        if reason:
            return reason
        if req.tags:
            return "不支持标签"
        return None

    async def fetch(self, req: PicRequest, n: int) -> list[ImageItem]:
        entries = self.combos[(req.style, req.rating)]
        items = []
        for _ in range(n):
            entry = random.choice(entries)
            items.append(
                ImageItem(
                    image_url=entry["url"],
                    rating=req.rating,
                    style=REAL,
                    provider=entry["name"],
                )
            )
        return items
