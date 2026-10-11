"""插件配置：分段与 _conf_schema.json 一一对应，键名、类型、默认值和可选值都只在 schema 里定义。

各站点的开关、适用分级、权重和质量门槛（评分、点赞数）集中在源配置区，
超时、缓存大小等调优参数是各模块里的常量。
schema 里 show_ 开头的开关只用来在配置面板上收起、展开一组设置（其他项的 condition 引用它），不进入设置。
"""

import json
import re
from dataclasses import dataclass, fields
from datetime import time

from astrbot.api import logger

from ..models import EXPLICIT, RATING_WORDS, SENSITIVE, STYLE_WORDS
from ..paths import SCHEMA_PATH
from .migration import migrate_layout

SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
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
            value = value if isinstance(value, bool) else default
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

    def __post_init__(self):
        self.album_count = _clamp(self.album_count, 1)
        self.images_per_album = _clamp(self.images_per_album, 1)
        self.max_images = _clamp(self.max_images, 1)
        self.min_pages = _clamp(self.min_pages, 0)
        self.explicit_skip = min(max(float(self.explicit_skip), 0.0), 0.9)

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
class Commands:
    draw: list[str]
    random_character: list[str]
    anime: list[str]
    real: list[str]
    sensitive: list[str]
    explicit: list[str]
    whole: list[str]
    pdf: list[str]
    video: list[str]
    refresh: list[str]
    help: list[str]
    help_words: list[str]


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
    """一个图源站点的设置。只有一种分级的站点不显示「适用分级」，沿用默认范围。"""

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
    """/抽视频：默认个数、大小和时长上限。"""

    enabled: bool
    default_count: int
    max_count: int
    max_mb: int
    max_seconds: int
    mmd: bool

    def __post_init__(self):
        self.max_count = _clamp(self.max_count, 1)
        self.default_count = _clamp(self.default_count, 1, self.max_count)
        self.max_mb = _clamp(self.max_mb, 0)
        self.max_seconds = _clamp(self.max_seconds, 0)


FEED_TEMPLATES = SCHEMA["sources"]["items"]["feeds"]["items"]["subscriptions"][
    "templates"
]
X = "x"
TELEGRAM = "telegram"
# 账号的写法：X 用户名 1~15 位，Telegram 频道名 4~32 位、字母开头
ACCOUNT_RE = {
    X: re.compile(r"^[A-Za-z0-9_]{1,15}$"),
    TELEGRAM: re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$"),
}
# 粘贴的主页、频道链接 → 账号
ACCOUNT_URL_RE = {
    X: re.compile(
        r"^(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/([^/?#]+)", re.I
    ),
    TELEGRAM: re.compile(r"^(?:https?://)?(?:www\.)?t\.me/(?:s/)?([^/?#]+)", re.I),
}
MEDIA_IMAGES = {"图片和视频": True, "仅图片": True, "仅视频": False}
MEDIA_VIDEOS = {"图片和视频": True, "仅图片": False, "仅视频": True}


def account_name(platform: str, text: str) -> str | None:
    """配置里写的账号（可以带 @ 或是链接）→ 账号名，写法不对时返回 None。"""
    text = text.strip()
    match = ACCOUNT_URL_RE[platform].match(text)
    name = (match.group(1) if match else text).removeprefix("@")
    return name if ACCOUNT_RE[platform].match(name) else None


@dataclass
class Feed:
    """一个订阅：X 博主或 Telegram 频道，帖子按设定的风格、分级抽取。"""

    platform: str  # X / TELEGRAM
    account: str
    name: str
    style: str
    rating: str
    images: bool  # 图片参与 /抽图
    videos: bool  # 视频参与 /抽视频
    weight: int
    min_likes: int = 0
    fill_album: bool = True
    prefer_same_post: bool = False

    @classmethod
    def parse(cls, entry) -> "Feed | None":
        """配置里的一条订阅；停用、模板不认识或账号写错时返回 None。"""
        if not isinstance(entry, dict):
            return None
        platform = entry.get("__template_key")
        template = FEED_TEMPLATES.get(platform)
        if template is None:
            return None
        values = _values(template["items"], entry)
        if not values["enabled"]:
            return None
        account = account_name(platform, values["account"])
        if account is None:
            if values["account"]:
                logger.warning(
                    f"[random_pic] 订阅的账号写法不对，已忽略：{values['account']}"
                )
            return None
        return cls(
            platform=platform,
            account=account,
            name=values["name"],
            style=STYLE_WORDS[values["style"]],
            # Telegram 频道没有分级可依，一律按 R18
            rating=RATING_WORDS[values.get("rating", "R18").lower()],
            images=MEDIA_IMAGES[values["media"]],
            videos=MEDIA_VIDEOS[values["media"]],
            weight=_clamp(values["weight"], 1),
            min_likes=_clamp(values.get("min_likes", 0), 0),
            fill_album=values["fill_album"],
            prefer_same_post=values["prefer_same_post"],
        )

    @property
    def key(self) -> str:
        """图源键（也用在 PDF 的存储名里）。"""
        prefix = "tg" if self.platform == TELEGRAM else self.platform
        return f"{prefix}_{self.account.lower()}"


@dataclass
class Feeds:
    enabled: bool
    subscriptions: list[Feed]

    def __post_init__(self):
        feeds: dict[str, Feed] = {}
        for feed in map(Feed.parse, self.subscriptions):
            # 同一个账号订阅了两次时只用第一条
            if feed and feed.key not in feeds:
                feeds[feed.key] = feed
        self.subscriptions = list(feeds.values())


@dataclass
class VideoSources:
    danbooru: DanbooruSite
    iwara: IwaraSite
    redgifs_hentai: LikesSite
    redgifs_cosplay: LikesSite
    soop: ViewsSite
    cosplaytele: Site

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, dict):
                setattr(self, f.name, f.type(**value))


@dataclass
class Sources:
    """图片站点、视频站点、订阅与补位设置。"""

    fallback: str  # 补位图源的键，"off" 为不补位
    danbooru: DanbooruSite
    ehentai: EHentaiSite
    pica: PicaSite
    cosplaytele: Site
    xiuren: Site
    nudecosplay: Site
    pixibb: Site
    jmcomic: JMSite
    video: VideoSources
    feeds: Feeds

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, dict):
                setattr(self, f.name, f.type(**value))

    def site(self, key: str) -> Site:
        return getattr(self, key)


@dataclass
class ReserveCounts:
    real_sensitive: int
    real_explicit: int
    anime_sensitive: int
    anime_explicit: int

    def __post_init__(self):
        for f in fields(self):
            setattr(self, f.name, _clamp(getattr(self, f.name), 0))


@dataclass
class Reserves:
    images: ReserveCounts
    videos: ReserveCounts

    def __post_init__(self):
        self.images = ReserveCounts(**self.images)
        self.videos = ReserveCounts(**self.videos)


@dataclass
class Env:
    """部署环境：网络代理，QQ 机器人和 AstrBot 共用的共享目录、PDF 目录（留空为不用 / 插件数据目录）。"""

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
    sources: Sources
    draw: Draw
    video: Video
    send: Send
    access: Access
    commands: Commands
    filter: Filter
    reserve: Reserves
    whole: Whole
    push: Push
    env: Env

    @classmethod
    def load(cls, config) -> "Settings":
        config = migrate_layout(config)
        sections = {
            f.name: f.type(**_values(SCHEMA[f.name]["items"], config.get(f.name) or {}))
            for f in fields(cls)
        }
        return cls(**sections)
