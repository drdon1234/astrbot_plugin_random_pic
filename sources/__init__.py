"""图源：E-Hentai（二次元、三次元），16K 与哔咔（仅三次元）。

每个图源提供：
- name：来源的显示名，出现在标题行和错误说明里；
- accepts(ctx)：这次请求能否使用该图源；
- draw(ctx, n)：抽 n 个图集，返回 (图集, 错误说明)。
"""

from dataclasses import dataclass

from ..models import DrawRequest
from ..tags import TagIndex


@dataclass
class DrawContext:
    req: DrawRequest
    is_private: bool
    allow_unrated: bool  # 能否使用没有分级的图源（见 AccessControl.unrated_allowed）
    terms: list[str]  # 关键词翻译成的 E-Hentai 搜索词
    index: TagIndex | None
