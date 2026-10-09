"""插件配置：分段与 _conf_schema.json 一一对应，键名、类型、默认值和可选值都只在 schema 里定义。

只有用户需要选择的项才放进配置：各站点的开关、适用分级、比例和质量门槛（评分、点赞数）在图源管理里，
超时、缓存大小等调优参数是各模块里的常量。
schema 里 show_ 开头的开关只用来在配置面板上收起、展开一组设置（其他项的 condition 引用它），不进入设置。
"""

import json
import re
from dataclasses import dataclass, fields
from datetime import time
from pathlib import Path

from astrbot.api import logger

from .models import EXPLICIT, RATING_WORDS, SENSITIVE, STYLE_WORDS

SCHEMA = json.loads(
    Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8")
)
# 与 schema 里选项一致的取值
FROM_START = "从第一页起"
PDF_FORMAT = "PDF"
FORWARD_FORMAT = "合并转发"
TIME_RE = re.compile(r"^(\d{1,2})[:：](\d{2})$")
PUSH_TEMPLATES = SCHEMA["push"]["items"]["tasks"]["templates"]
SITE_SCHEMA = SCHEMA["sources"]["items"]
# 只控制配置面板显示的开关的键前缀
PANEL_PREFIX = "show_"
# 图源的「适用分级」→ 参与抽取的分级
SCOPES = {
    "通用": frozenset({SENSITIVE, EXPLICIT}),
    "仅擦边": frozenset({SENSITIVE}),
    "仅R18": frozenset({EXPLICIT}),
}


def _coerce(spec: dict, value):
    """按 schema 转换配置值：类型不对、转换失败或不在可选值里时用默认值；object 逐项转换。"""
    kind = spec["type"]
    if kind == "object":
        return _values(spec["items"], value if isinstance(value, dict) else {})
    default = spec["default"]
    try:
        if kind == "bool":
            value = bool(value)
        elif kind == "int":
            value = int(value)
        elif kind == "float":
            value = float(value)
        elif kind in ("list", "template_list"):
            if not isinstance(value, list):
                return list(default)
            if kind == "list":
                value = [s for s in (str(v).strip() for v in value) if s]
        else:
            value = "" if value is None else str(value).strip()
    except (TypeError, ValueError):
        return default
    if "options" in spec and value not in spec["options"]:
        return default
    return value


def _values(items: dict, conf: dict) -> dict:
    return {
        key: _coerce(spec, conf.get(key, spec.get("default")))
        for key, spec in items.items()
        if not key.startswith(PANEL_PREFIX)
    }


def _clamp(value, low, high=None):
    value = max(low, value)
    return value if high is None else min(value, high)


def parse_time(text: str) -> time | None:
    """「8:00」「20:30」→ 时刻，写错时返回 None。"""
    match = TIME_RE.match(text.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return time(hour, minute) if hour < 24 and minute < 60 else None


@dataclass
class Draw:
    default_style: str
    default_rating: str
    album_count: int
    images_per_album: int
    max_images: int
    page_pick: str
    explicit_skip: float
    min_pages: int
    reserve_real_sensitive: int
    reserve_real_explicit: int
    reserve_anime_sensitive: int
    reserve_anime_explicit: int

    def __post_init__(self):
        self.album_count = _clamp(self.album_count, 1)
        self.images_per_album = _clamp(self.images_per_album, 1)
        self.max_images = _clamp(self.max_images, 1)
        self.min_pages = _clamp(self.min_pages, 0)
        self.explicit_skip = min(max(float(self.explicit_skip), 0.0), 0.9)
        self.reserve_real_sensitive = _clamp(self.reserve_real_sensitive, 0)
        self.reserve_real_explicit = _clamp(self.reserve_real_explicit, 0)
        self.reserve_anime_sensitive = _clamp(self.reserve_anime_sensitive, 0)
        self.reserve_anime_explicit = _clamp(self.reserve_anime_explicit, 0)

    @property
    def style(self) -> str:
        return STYLE_WORDS[self.default_style]

    @property
    def rating(self) -> str:
        return RATING_WORDS[self.default_rating.lower()]

    @property
    def from_start(self) -> bool:
        return self.page_pick == FROM_START


@dataclass
class Access:
    group_enabled: bool
    group_r18: bool
    private_r18: bool
    admins: list[str]
    group_whitelist: list[str]
    user_blacklist: list[str]
    cooldown_seconds: int
    daily_limit: int
    no_prefix: bool
    aliases: bool

    def __post_init__(self):
        self.cooldown_seconds = _clamp(self.cooldown_seconds, 0)
        self.daily_limit = _clamp(self.daily_limit, 0)


@dataclass
class Filter:
    block_heavy: bool
    block_trans: bool
    block_ai: bool
    extra_blacklist: list[str]


@dataclass
class Send:
    mode: str
    header: bool
    caption: bool


@dataclass
class Whole:
    enabled: bool
    default_format: str
    max_pages: int

    def __post_init__(self):
        self.max_pages = _clamp(self.max_pages, 1)


@dataclass(kw_only=True)
class Site:
    """一个图源站点的设置。只有一种分级的站点没有「适用分级」，唯一的二次元图源没有「比例」，用这里的默认值。"""

    enabled: bool
    scope: str = "通用"
    weight: int = 1

    def __post_init__(self):
        self.weight = _clamp(self.weight, 1)

    @property
    def ratings(self) -> frozenset[str]:
        """配置允许参与抽取的分级（还要和站点自身能判定的分级取交集）。"""
        return SCOPES[self.scope]


@dataclass(kw_only=True)
class DanbooruSite(Site):
    min_score: int

    def __post_init__(self):
        super().__post_init__()
        self.min_score = _clamp(self.min_score, 0)


@dataclass(kw_only=True)
class EHentaiSite(Site):
    min_stars: int
    site: str
    ipb_member_id: str
    ipb_pass_hash: str
    igneous: str

    def __post_init__(self):
        super().__post_init__()
        self.min_stars = _clamp(self.min_stars, 1, 5)

    @property
    def cookies(self) -> dict[str, str]:
        values = {
            "ipb_member_id": self.ipb_member_id,
            "ipb_pass_hash": self.ipb_pass_hash,
            "igneous": self.igneous,
        }
        return {k: v for k, v in values.items() if v}


@dataclass(kw_only=True)
class PicaSite(Site):
    manual_account: bool
    email: str
    password: str

    @property
    def account(self) -> tuple[str, str] | None:
        """手动配置的账号；None 表示用插件自己注册维护的账号。"""
        return (self.email, self.password) if self.manual_account else None


@dataclass(kw_only=True)
class JMSite(Site):
    min_likes: int
    domain: str

    def __post_init__(self):
        super().__post_init__()
        self.min_likes = _clamp(self.min_likes, 0)
        # 允许粘贴带 https:// 或路径的地址
        domain = self.domain.removeprefix("https://").removeprefix("http://")
        self.domain = (
            domain.split("/", 1)[0]
            or SITE_SCHEMA["jmcomic"]["items"]["domain"]["default"]
        )


@dataclass
class Sources:
    """补位图源和各图源站点的设置，站点的键是图源键。"""

    fallback: str  # 补位图源的键，"off" 为不补位
    danbooru: DanbooruSite
    ehentai: EHentaiSite
    pica: PicaSite
    cosplaytele: Site
    xiuren: Site
    nudecosplay: Site
    pixibb: Site
    jmcomic: JMSite

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, dict):
                setattr(self, f.name, f.type(**value))

    def site(self, key: str) -> Site:
        return getattr(self, key)


@dataclass(kw_only=True)
class LikesSite(Site):
    """带最低点赞数的视频站（Iwara、RedGifs）。"""

    min_likes: int

    def __post_init__(self):
        super().__post_init__()
        self.min_likes = _clamp(self.min_likes, 0)


@dataclass(kw_only=True)
class IwaraSite(LikesSite):
    """Iwara：R18（ecchi）和擦边（general）各有最低点赞数。"""

    min_likes_sensitive: int

    def __post_init__(self):
        super().__post_init__()
        self.min_likes_sensitive = _clamp(self.min_likes_sensitive, 0)


@dataclass(kw_only=True)
class ViewsSite(Site):
    """带最低播放量的视频站（SOOP）。"""

    min_views: int

    def __post_init__(self):
        super().__post_init__()
        self.min_views = _clamp(self.min_views, 0)


@dataclass
class Video:
    """/抽视频（实验性）：个数、大小和时长上限与各视频源。"""

    enabled: bool
    default_count: int
    max_count: int
    max_mb: int
    max_seconds: int
    mmd: bool
    reserve_anime: int
    reserve_real: int
    danbooru: DanbooruSite
    iwara: IwaraSite
    redgifs_hentai: LikesSite
    redgifs_cosplay: LikesSite
    soop: ViewsSite
    cosplaytele: Site

    def __post_init__(self):
        self.max_count = _clamp(self.max_count, 1)
        self.default_count = _clamp(self.default_count, 1, self.max_count)
        self.max_mb = _clamp(self.max_mb, 0)
        self.max_seconds = _clamp(self.max_seconds, 0)
        self.reserve_anime = _clamp(self.reserve_anime, 0)
        self.reserve_real = _clamp(self.reserve_real, 0)
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, dict):
                setattr(self, f.name, f.type(**value))


@dataclass
class Env:
    """部署环境：网络代理，QQ 机器人和 AstrBot 共用的中转目录、PDF 目录（留空为不用 / 插件数据目录）。"""

    proxy: str
    share_dir: str
    pdf_dir: str


@dataclass
class PushTask:
    """一条推送任务：每天从 start 起按间隔（interval_minutes）推送，或按每天的时间点（times）推送。"""

    groups: list[str]
    users: list[str]
    content: str  # /抽图 的参数
    interval_minutes: int | None = None
    start: time | None = None
    times: list[time] | None = None

    @classmethod
    def parse(cls, entry) -> "PushTask | None":
        """配置里的一条任务；停用、模板不认识或没有推送时间时返回 None。"""
        if not isinstance(entry, dict):
            return None
        template = PUSH_TEMPLATES.get(entry.get("__template_key"))
        if template is None:
            return None
        values = _values(template["items"], entry)
        if not values.pop("enabled"):
            return None
        if "interval_minutes" in values:
            values["interval_minutes"] = _clamp(values["interval_minutes"], 1, 24 * 60)
            start = parse_time(values["start"])
            if start is None:
                logger.warning(
                    f"[random_pic] 推送起始时间格式不对，按 00:00 计算：{values['start']}"
                )
            values["start"] = start or time(0, 0)
        else:
            parsed = {t: parse_time(t) for t in values["times"]}
            for text in (t for t, value in parsed.items() if value is None):
                logger.warning(f"[random_pic] 推送时间格式不对，已忽略：{text}")
            times = set(parsed.values()) - {None}
            if not times:
                return None
            values["times"] = sorted(times)
        return cls(**values)


@dataclass
class Push:
    tasks: list[PushTask]

    def __post_init__(self):
        self.tasks = [t for t in map(PushTask.parse, self.tasks) if t]


@dataclass
class Settings:
    draw: Draw
    access: Access
    filter: Filter
    send: Send
    whole: Whole
    push: Push
    video: Video
    sources: Sources
    env: Env

    @classmethod
    def load(cls, config) -> "Settings":
        sections = {
            f.name: f.type(**_values(SCHEMA[f.name]["items"], config.get(f.name) or {}))
            for f in fields(cls)
        }
        return cls(**sections)
