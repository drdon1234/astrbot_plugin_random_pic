"""插件配置：分段与 _conf_schema.json 一一对应，键名、类型、默认值和可选值都只在 schema 里定义。"""

import json
from dataclasses import dataclass, fields
from pathlib import Path

from .models import RATING_WORDS, STYLE_WORDS

SCHEMA = json.loads(
    Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8")
)
# 与 schema 里选项一致的取值
FROM_START = "从第一页起"
FULL_ALBUM = "随机完整图集"
PDF_FORMAT = "PDF"


def _coerce(spec: dict, value):
    """按 schema 转换配置值：类型不对、转换失败或不在可选值里时用默认值。"""
    kind, default = spec["type"], spec["default"]
    try:
        if kind == "bool":
            value = bool(value)
        elif kind == "int":
            value = int(value)
        elif kind == "float":
            value = float(value)
        elif kind == "list":
            if not isinstance(value, list):
                return list(default)
            value = [s for s in (str(v).strip() for v in value) if s]
        else:
            value = "" if value is None else str(value).strip()
    except (TypeError, ValueError):
        return default
    if "options" in spec and value not in spec["options"]:
        return default
    return value


def _clamp(value, low, high=None):
    value = max(low, value)
    return value if high is None else min(value, high)


@dataclass
class Access:
    content_rating: bool
    r18_enabled: bool
    group_sensitive: bool
    group_whitelist: list[str]
    user_blacklist: list[str]
    cooldown_seconds: int
    daily_limit: int

    def __post_init__(self):
        self.cooldown_seconds = _clamp(self.cooldown_seconds, 0)
        self.daily_limit = _clamp(self.daily_limit, 0)


@dataclass
class Command:
    default_style: str
    default_rating: str
    album_count: int
    images_per_album: int
    max_images: int
    aliases: bool
    no_prefix: bool

    def __post_init__(self):
        self.max_images = _clamp(self.max_images, 1)
        self.album_count = _clamp(self.album_count, 1)
        self.images_per_album = _clamp(self.images_per_album, 1)

    @property
    def style(self) -> str:
        return STYLE_WORDS[self.default_style]

    @property
    def rating(self) -> str:
        return RATING_WORDS[self.default_rating.lower()]


@dataclass
class Send:
    mode: str
    header: bool
    caption: bool


@dataclass
class Draw:
    page_pick: str
    explicit_skip: float
    extra_blacklist: list[str]
    block_heavy: bool
    heavy_tags: list[str]

    def __post_init__(self):
        self.explicit_skip = _clamp(self.explicit_skip, 0.0, 0.9)

    @property
    def from_start(self) -> bool:
        return self.page_pick == FROM_START

    @property
    def heavy(self) -> frozenset[str]:
        if not self.block_heavy:
            return frozenset()
        return frozenset(t.lower() for t in self.heavy_tags)


@dataclass
class Sources:
    """三次元各图源的比例，键是图源键。"""

    ehentai: int
    pica: int
    cosplaytele: int
    xiuren: int
    nudecosplay: int
    pixibb: int
    jmcomic: int

    @property
    def weights(self) -> dict[str, int]:
        return {f.name: _clamp(getattr(self, f.name), 0) for f in fields(self)}


@dataclass
class DanbooruConf:
    min_score: int
    exclude_tags: list[str]

    def __post_init__(self):
        self.min_score = _clamp(self.min_score, 0)


@dataclass
class EHentaiConf:
    site: str
    ipb_member_id: str
    ipb_pass_hash: str
    igneous: str
    min_rating: int
    exclude_ai: bool
    min_pages: int
    request_interval: float

    def __post_init__(self):
        self.min_pages = _clamp(self.min_pages, 0)
        self.request_interval = _clamp(self.request_interval, 0.0)

    @property
    def cookies(self) -> dict[str, str]:
        values = {
            "ipb_member_id": self.ipb_member_id,
            "ipb_pass_hash": self.ipb_pass_hash,
            "igneous": self.igneous,
        }
        return {k: v for k, v in values.items() if v}


@dataclass
class PicaConf:
    email: str
    password: str


@dataclass
class JMComicConf:
    domain: str
    min_likes: int
    exclude_tags: list[str]

    def __post_init__(self):
        # 允许粘贴带 https:// 或路径的地址
        domain = self.domain.removeprefix("https://").removeprefix("http://")
        self.domain = (
            domain.split("/", 1)[0] or SCHEMA["jmcomic"]["items"]["domain"]["default"]
        )
        self.min_likes = _clamp(self.min_likes, 0)


@dataclass
class Pdf:
    enabled: bool
    pages_per_file: int
    jpeg_quality: int  # 0 表示不压缩
    keep_galleries: int
    output_dir: str

    def __post_init__(self):
        self.pages_per_file = _clamp(self.pages_per_file, 1)
        self.jpeg_quality = _clamp(self.jpeg_quality, 0, 100)
        self.keep_galleries = _clamp(self.keep_galleries, 1)


@dataclass
class Push:
    enabled: bool
    interval_minutes: int
    groups: list[str]
    users: list[str]
    content: str
    album_format: str

    def __post_init__(self):
        self.interval_minutes = _clamp(self.interval_minutes, 1, 24 * 60)

    @property
    def full_album(self) -> bool:
        return self.content == FULL_ALBUM

    @property
    def as_pdf(self) -> bool:
        return self.album_format == PDF_FORMAT


@dataclass
class Network:
    proxy: str
    timeout: int
    concurrency: int
    max_image_mb: int
    cache_mb: int
    tag_db_url: str

    def __post_init__(self):
        self.timeout = _clamp(self.timeout, 1)
        self.concurrency = _clamp(self.concurrency, 1)
        self.max_image_mb = _clamp(self.max_image_mb, 1)
        self.cache_mb = _clamp(self.cache_mb, 0)
        self.tag_db_url = (
            self.tag_db_url or SCHEMA["network"]["items"]["tag_db_url"]["default"]
        )


@dataclass
class Settings:
    access: Access
    command: Command
    send: Send
    draw: Draw
    sources: Sources
    danbooru: DanbooruConf
    ehentai: EHentaiConf
    pica: PicaConf
    jmcomic: JMComicConf
    pdf: Pdf
    push: Push
    network: Network

    @classmethod
    def load(cls, config) -> "Settings":
        sections = {}
        for f in fields(cls):
            conf = config.get(f.name) or {}
            values = {
                key: _coerce(spec, conf.get(key, spec["default"]))
                for key, spec in SCHEMA[f.name]["items"].items()
            }
            sections[f.name] = f.type(**values)
        return cls(**sections)
