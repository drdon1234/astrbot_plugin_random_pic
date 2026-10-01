"""插件配置：分段与 _conf_schema.json 一一对应，键名、类型和默认值都只在 schema 里定义。"""

import json
from dataclasses import dataclass, fields
from pathlib import Path

SCHEMA = json.loads(
    Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8")
)


def _coerce(kind: str, value, default):
    """按 schema 类型转换配置值，转换失败时用默认值。"""
    try:
        if kind == "bool":
            return bool(value)
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        if kind == "list":
            if not isinstance(value, list):
                return list(default)
            return [s for s in (str(v).strip() for v in value) if s]
        return "" if value is None else str(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Access:
    content_rating: bool
    r18_enabled: bool
    group_sensitive: bool
    group_whitelist: list[str]
    user_blacklist: list[str]
    cooldown_seconds: int
    daily_limit: int


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
        self.max_images = max(1, self.max_images)
        self.album_count = max(1, self.album_count)
        self.images_per_album = max(1, self.images_per_album)


@dataclass
class Send:
    mode: str
    header: bool
    caption: bool


@dataclass
class Draw:
    page_pick: str
    explicit_skip: float
    color_only: bool
    extra_blacklist: list[str]
    block_heavy: bool
    heavy_tags: list[str]

    def __post_init__(self):
        self.explicit_skip = min(max(self.explicit_skip, 0.0), 0.9)

    @property
    def from_start(self) -> bool:
        return self.page_pick == "从第一页起"

    @property
    def heavy(self) -> frozenset[str]:
        if not self.block_heavy:
            return frozenset()
        return frozenset(t.lower() for t in self.heavy_tags)


@dataclass
class Sources:
    ehentai: int
    sixteenk: int
    pica: int

    def __post_init__(self):
        self.ehentai = max(0, self.ehentai)
        self.sixteenk = max(0, self.sixteenk)
        self.pica = max(0, self.pica)


@dataclass
class Network:
    proxy: str
    timeout: int
    concurrency: int
    max_image_mb: int
    cache_mb: int

    def __post_init__(self):
        self.proxy = self.proxy.strip()
        self.timeout = max(1, self.timeout)
        self.concurrency = max(1, self.concurrency)


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

    @property
    def cookies(self) -> dict[str, str]:
        values = {
            "ipb_member_id": self.ipb_member_id.strip(),
            "ipb_pass_hash": self.ipb_pass_hash.strip(),
            "igneous": self.igneous.strip(),
        }
        return {k: v for k, v in values.items() if v}


@dataclass
class Pica:
    email: str
    password: str

    def __post_init__(self):
        self.email = self.email.strip()


@dataclass
class TagDBConf:
    enabled: bool
    url: str

    def __post_init__(self):
        self.url = self.url.strip() or SCHEMA["tag_db"]["items"]["url"]["default"]


@dataclass
class Pdf:
    enabled: bool
    pages_per_file: int
    output_dir: str
    keep_galleries: int

    def __post_init__(self):
        self.pages_per_file = max(1, self.pages_per_file)
        self.output_dir = self.output_dir.strip()
        self.keep_galleries = max(1, self.keep_galleries)


@dataclass
class Settings:
    access: Access
    command: Command
    send: Send
    draw: Draw
    sources: Sources
    network: Network
    ehentai: EHentaiConf
    pools: dict
    pica: Pica
    tag_db: TagDBConf
    pdf: Pdf

    @classmethod
    def load(cls, config) -> "Settings":
        sections = {}
        for f in fields(cls):
            conf = config.get(f.name) or {}
            values = {
                key: _coerce(
                    spec["type"], conf.get(key, spec["default"]), spec["default"]
                )
                for key, spec in SCHEMA[f.name]["items"].items()
            }
            sections[f.name] = values if f.type is dict else f.type(**values)
        return cls(**sections)
