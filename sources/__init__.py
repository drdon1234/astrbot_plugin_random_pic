"""图源：E-Hentai（二次元、三次元），哔咔、CosplayTele 与 XiuRen（仅三次元）。

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
    terms: list[str]  # 关键词翻译成的 E-Hentai 搜索词
    index: TagIndex | None
