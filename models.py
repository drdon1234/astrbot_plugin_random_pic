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
class WorkRef:
    """一个作品（E-Hentai 画廊、帖子、本子），/pdf 用。"""

    source: str  # 图源键：ehentai、pica、cosplaytele、xiuren、danbooru
    id: str  # 作品 id；WordPress 站点从链接认出时可能是帖子的 slug
    token: str = ""  # E-Hentai 画廊的 token

    @property
    def key(self) -> str:
        """PDF 的存储名：E-Hentai 画廊沿用画廊号（已打包的文件继续复用），其他为「图源_id」。"""
        return self.id if self.source == "ehentai" else f"{self.source}_{self.id}"


@dataclass
class Work:
    """要整本打包的作品，由图源查询得到。"""

    ref: WorkRef  # 规范的引用（WordPress 的 slug 已换成帖子 id）
    title: str
    pages: int
    rating: str | None  # 无法判定时为 None，按 R18 处理
    blocked: str | None = None  # 命中黑名单、重口等不予打包的原因
    data: object = None  # 图源下载时要用的数据


@dataclass
class Album:
    """一个图集：同一画廊 / 帖子 / 本子里抽到的几张图。"""

    source: str  # 来源的显示名
    title: str
    total: int  # 作品总页数
    pictures: list[tuple[int, Path]]  # (页码，从 1 开始, 本地文件)，按页码排序
    details: list[str] = field(default_factory=list)  # 说明文字，每项一行
    work: WorkRef | None = None


@dataclass
class DrawOptions:
    """各图源共用的取图选项。"""

    rating_enabled: bool = True
    from_start: bool = False
    explicit_skip: float = 0.0
    concurrency: int = 1
