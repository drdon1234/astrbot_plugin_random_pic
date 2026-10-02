"""插件配置：分段与 _conf_schema.json 一一对应，键名、类型、默认值和可选值都只在 schema 里定义。

只有用户需要选择的项才放进配置；各图源的门槛、超时、缓存大小等调优参数是各模块里的常量。
"""

import json
import re
from dataclasses import dataclass, fields
from datetime import time
from pathlib import Path

from astrbot.api import logger

from .models import RATING_WORDS, STYLE_WORDS

SCHEMA = json.loads(
    Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8")
)
# 与 schema 里选项一致的取值
FROM_START = "从第一页起"
PDF_FORMAT = "PDF"
FORWARD_FORMAT = "合并转发"
TIME_RE = re.compile(r"^(\d{1,2})[:：](\d{2})$")
PUSH_TEMPLATES = SCHEMA["push"]["items"]["tasks"]["templates"]


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
        key: _coerce(spec, conf.get(key, spec["default"]))
        for key, spec in items.items()
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
    aliases: bool
    no_prefix: bool

    def __post_init__(self):
        self.album_count = _clamp(self.album_count, 1)
        self.images_per_album = _clamp(self.images_per_album, 1)
        self.max_images = _clamp(self.max_images, 1)

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

    def __post_init__(self):
        self.cooldown_seconds = _clamp(self.cooldown_seconds, 0)
        self.daily_limit = _clamp(self.daily_limit, 0)


@dataclass
class Filter:
    block_heavy: bool
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
    pdf_dir: str


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
class Sites:
    proxy: str
    ehentai_site: str
    ipb_member_id: str
    ipb_pass_hash: str
    igneous: str
    pica_email: str
    pica_password: str
    jmcomic_domain: str

    def __post_init__(self):
        # 允许粘贴带 https:// 或路径的地址
        domain = self.jmcomic_domain.removeprefix("https://").removeprefix("http://")
        self.jmcomic_domain = (
            domain.split("/", 1)[0]
            or SCHEMA["sites"]["items"]["jmcomic_domain"]["default"]
        )

    @property
    def cookies(self) -> dict[str, str]:
        values = {
            "ipb_member_id": self.ipb_member_id,
            "ipb_pass_hash": self.ipb_pass_hash,
            "igneous": self.igneous,
        }
        return {k: v for k, v in values.items() if v}


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
    sources: Sources
    sites: Sites
    push: Push

    @classmethod
    def load(cls, config) -> "Settings":
        sections = {
            f.name: f.type(**_values(SCHEMA[f.name]["items"], config.get(f.name) or {}))
            for f in fields(cls)
        }
        return cls(**sections)
