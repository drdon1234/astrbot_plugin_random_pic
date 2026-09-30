"""图源适配器基类。"""

from ..models import ImageItem, PicRequest
from ..net import HttpClient


class Provider:
    """每个图源一个适配器。

    combos 声明支持的 (style, rating) 组合及对应的请求参数；
    returns_tags 为 False 的图源不返回标签，不得用于 R18。
    """

    name = ""
    display = ""
    returns_tags = True

    def __init__(self, http: HttpClient, proxy: str | None, conf: dict):
        self.http = http
        self.proxy = proxy
        self.conf = conf
        self.combos: dict[tuple[str, str], object] = self.build_combos()

    def build_combos(self) -> dict[tuple[str, str], object]:
        raise NotImplementedError

    def unsupported(self, req: PicRequest) -> str | None:
        """当前请求无法由本图源满足时返回原因（如不支持标签、缺少 API key）。"""
        if req.twitter:
            return "不支持推特子模式"
        return None

    async def fetch(self, req: PicRequest, n: int) -> list[ImageItem]:
        raise NotImplementedError
