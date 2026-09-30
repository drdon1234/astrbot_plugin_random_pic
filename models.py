"""统一数据结构：请求与图片条目。"""

from dataclasses import dataclass, field

ANIME = "anime"
REAL = "real"
STYLES = (ANIME, REAL)

GENERAL = "general"
SENSITIVE = "sensitive"
EXPLICIT = "explicit"
RATINGS = (GENERAL, SENSITIVE, EXPLICIT)

# 分级严格程度，数值越大越严格
RATING_LEVEL = {GENERAL: 0, SENSITIVE: 1, EXPLICIT: 2}

STYLE_NAMES = {ANIME: "二次元", REAL: "三次元"}
RATING_NAMES = {GENERAL: "全年龄", SENSITIVE: "擦边", EXPLICIT: "R18"}


@dataclass
class PicRequest:
    style: str = ANIME
    rating: str = GENERAL
    tags: list[str] = field(default_factory=list)
    count: int = 1
    twitter: bool = False  # Danbooru 推特子模式


@dataclass
class ImageItem:
    image_url: str
    rating: str
    style: str
    provider: str
    author: str = ""
    title: str = ""
    source_url: str = ""  # 原始出处（Pixiv / 推特等）
    post_url: str = ""  # 图站作品页
    tags: list[str] = field(default_factory=list)
    alt_url: str = ""  # 原图过大时降级使用的较小尺寸
