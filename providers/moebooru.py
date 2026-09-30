"""Yande.re / Konachan（Moebooru 系）：二次元，作为 Danbooru 的回退。

Moebooru 的 rating 只有 s(safe) / q(questionable) / e(explicit)，
分别对应全年龄 / 擦边 / R18。
"""

from ..models import ANIME, EXPLICIT, GENERAL, SENSITIVE, ImageItem, PicRequest
from .base import Provider

RATING_MAP = {"s": GENERAL, "q": SENSITIVE, "e": EXPLICIT}
QUERY_EXCLUDES = ("-loli", "-shota")


class MoebooruProvider(Provider):
    base_url = ""

    def build_combos(self):
        return {
            (ANIME, GENERAL): "rating:s",
            (ANIME, SENSITIVE): "rating:q",
            (ANIME, EXPLICIT): "rating:e",
        }

    async def fetch(self, req: PicRequest, n: int) -> list[ImageItem]:
        tags = ["order:random", self.combos[(req.style, req.rating)], *req.tags]
        tags += QUERY_EXCLUDES
        data = await self.http.get_json(
            f"{self.base_url}/post.json",
            params={"tags": " ".join(tags), "limit": str(n)},
            proxy=self.proxy,
        )
        items = []
        for post in data:
            rating = RATING_MAP.get(post.get("rating", ""))
            if not post.get("file_url") or not rating:
                continue
            sample = post.get("sample_url") or ""
            items.append(
                ImageItem(
                    image_url=post["file_url"],
                    alt_url=sample if sample != post["file_url"] else "",
                    rating=rating,
                    style=ANIME,
                    provider=self.display,
                    source_url=post.get("source") or "",
                    post_url=f"{self.base_url}/post/show/{post.get('id')}",
                    tags=(post.get("tags") or "").split(),
                )
            )
        return items


class YandereProvider(MoebooruProvider):
    name = "yandere"
    display = "yande.re"
    base_url = "https://yande.re"


class KonachanProvider(MoebooruProvider):
    name = "konachan"
    display = "Konachan"
    base_url = "https://konachan.com"
