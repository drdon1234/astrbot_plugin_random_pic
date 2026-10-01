"""统一数据结构：抽卡请求、图集与各图源共用的取图选项。"""

from dataclasses import dataclass, field
from pathlib import Path

ANIME = "anime"
REAL = "real"
STYLES = (ANIME, REAL)

SENSITIVE = "sensitive"
EXPLICIT = "explicit"
RATINGS = (SENSITIVE, EXPLICIT)

STYLE_NAMES = {ANIME: "二次元", REAL: "三次元"}
RATING_NAMES = {SENSITIVE: "擦边", EXPLICIT: "R18"}


@dataclass
class DrawRequest:
    style: str
    rating: str
    keywords: list[str] = field(default_factory=list)  # 用户原样输入的关键词
    albums: int = 1  # 图集数
    per_album: int = 1  # 每个图集几张
    random_character: bool = False  # 每个图集先随机抽一个角色


@dataclass(frozen=True)
class GalleryRef:
    """E-Hentai 画廊，/pdf 用。"""

    gid: int
    token: str


@dataclass
class Album:
    """一个图集：同一画廊 / 帖子 / 本子里抽到的几张图。"""

    source: str  # 来源的显示名
    title: str
    total: int  # 作品总页数
    pictures: list[tuple[int, Path]]  # (页码，从 1 开始, 本地文件)，按页码排序
    details: list[str] = field(default_factory=list)  # 说明文字，每项一行
    gallery: GalleryRef | None = None


@dataclass
class DrawOptions:
    """各图源共用的取图选项。"""

    rating_enabled: bool = True
    from_start: bool = False
    explicit_skip: float = 0.0
    concurrency: int = 1
    color_only: bool = False
