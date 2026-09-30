"""Lolicon API：二次元。

r18=0 的结果并不保证全年龄，因此只用于擦边；r18=1 用于 R18。
该 API 没有负向标签参数，未成年内容完全依赖本地黑名单过滤。
"""

from ..models import ANIME, EXPLICIT, SENSITIVE, ImageItem, PicRequest
from .base import Provider

API = "https://api.lolicon.app/setu/v2"


class LoliconProvider(Provider):
    name = "lolicon"
    display = "Lolicon API"

    def build_combos(self):
        return {(ANIME, SENSITIVE): "0", (ANIME, EXPLICIT): "1"}

    async def fetch(self, req: PicRequest, n: int) -> list[ImageItem]:
        params = [
            ("r18", self.combos[(req.style, req.rating)]),
            ("num", str(max(1, min(n, 20)))),
            ("size", "original"),
            ("size", "regular"),
            ("proxy", self.conf.get("proxy_host") or "i.pixiv.re"),
            ("excludeAI", "true" if self.conf.get("exclude_ai", True) else "false"),
        ]
        if self.conf.get("aspect_ratio"):
            params.append(("aspectRatio", self.conf["aspect_ratio"]))
        params += [("tag", t) for t in req.tags]
        data = await self.http.get_json(API, params=params, proxy=self.proxy)
        if data.get("error"):
            raise ValueError(data["error"])
        items = []
        for r in data.get("data", []):
            urls = r.get("urls") or {}
            original = urls.get("original") or ""
            if not original and not urls.get("regular"):
                continue
            author = r.get("author") or ""
            if r.get("uid"):
                author = (
                    f"{author}（uid: {r['uid']}）" if author else f"uid: {r['uid']}"
                )
            items.append(
                ImageItem(
                    image_url=original or urls["regular"],
                    alt_url=urls.get("regular", "") if original else "",
                    rating=EXPLICIT if r.get("r18") else SENSITIVE,
                    style=ANIME,
                    provider=self.display,
                    author=author,
                    title=r.get("title") or "",
                    source_url=f"https://www.pixiv.net/artworks/{r['pid']}"
                    if r.get("pid")
                    else "",
                    tags=list(r.get("tags") or []),
                )
            )
        return items
