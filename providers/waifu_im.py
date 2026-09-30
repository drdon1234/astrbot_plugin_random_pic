"""waifu.im：二次元。IsNsfw=False 为全年龄，IsNsfw=True 为 R18。"""

from ..models import ANIME, EXPLICIT, GENERAL, ImageItem, PicRequest
from .base import Provider

API = "https://api.waifu.im/images"


class WaifuImProvider(Provider):
    name = "waifu_im"
    display = "waifu.im"

    def build_combos(self):
        return {(ANIME, GENERAL): "False", (ANIME, EXPLICIT): "True"}

    async def fetch(self, req: PicRequest, n: int) -> list[ImageItem]:
        params = [
            ("IsNsfw", self.combos[(req.style, req.rating)]),
            ("PageSize", str(n)),
            ("OrderBy", "Random"),
        ]
        params += [("IncludedTags", t.lower()) for t in req.tags]
        headers = {}
        if self.conf.get("api_key"):
            headers["X-Api-Key"] = self.conf["api_key"]
        data = await self.http.get_json(
            API, params=params, headers=headers, proxy=self.proxy
        )
        items = []
        for r in data.get("items", []):
            if not r.get("url"):
                continue
            artists = [a.get("name", "") for a in r.get("artists") or [] if a]
            items.append(
                ImageItem(
                    image_url=r["url"],
                    rating=EXPLICIT if r.get("isNsfw") else GENERAL,
                    style=ANIME,
                    provider=self.display,
                    author=", ".join(a for a in artists if a),
                    source_url=r.get("source") or "",
                    tags=[
                        t.get("slug") or t.get("name", "") for t in r.get("tags") or []
                    ],
                )
            )
        return items
