"""统一数据结构：请求与图片条目。"""

from dataclasses import dataclass, field

ANIME = "anime"
REAL = "real"
STYLES = (ANIME, REAL)

SENSITIVE = "sensitive"
EXPLICIT = "explicit"
RATINGS = (SENSITIVE, EXPLICIT)

STYLE_NAMES = {ANIME: "二次元", REAL: "三次元"}
RATING_NAMES = {SENSITIVE: "擦边", EXPLICIT: "R18"}


@dataclass
class PicRequest:
    style: str = ANIME
    rating: str = SENSITIVE
    tags: list[str] = field(
        default_factory=list
    )  # 追加到 E-Hentai 搜索词，中文名经标签库翻译
    count: int = 1
    random_character: bool = False  # 每张图先随机抽一个角色，再在该角色的画廊中抽


@dataclass
class ImageItem:
    image_url: str
    rating: str
    style: str
    gid: int = 0
    token: str = ""
    title: str = ""
    author: str = ""
    category: str = ""
    gallery_url: str = ""
    page_url: str = ""
    page: int = 0  # 从 1 开始
    pages: int = 0
    stars: float = 0.0
    tags: list[str] = field(default_factory=list)
    parodies: list[str] = field(default_factory=list)  # 中文名（标签库可用时）
    characters: list[str] = field(default_factory=list)
    source: str = "ehentai"  # 图源：ehentai / 16k
