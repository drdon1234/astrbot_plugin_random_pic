"""在 AstrBot 按新 schema 清理配置之前，原位迁移本插件的配置布局。

只迁移换了位置的业务字段，不做类型转换。第一次实际迁移时保留原文件的字节副本，
随后原子替换；失败则中止插件导入，避免 AstrBot 继续加载 schema 后删除旧设置。
"""

import copy
import json
import os
import tempfile
from pathlib import Path

from ..paths import PLUGIN_ROOT, SCHEMA_PATH

VIDEO_SITES = (
    "danbooru",
    "iwara",
    "redgifs_hentai",
    "redgifs_cosplay",
    "soop",
    "cosplaytele",
)
IMAGE_POOLS = (
    "real_sensitive",
    "real_explicit",
    "anime_sensitive",
    "anime_explicit",
)
MISSING = object()


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8-sig"))


def _defaults(items: dict) -> dict:
    return {
        key: _defaults(spec.get("items", {}))
        if spec.get("type") == "object"
        else copy.deepcopy(spec.get("default"))
        for key, spec in items.items()
    }


def _section(config: dict, key: str) -> dict:
    value = config.get(key, MISSING)
    if value is MISSING:
        value = config[key] = {}
    if not isinstance(value, dict):
        raise ValueError("配置分组不是对象，无法安全迁移")
    return value


def _merge(target: dict, original: dict, defaults: dict):
    """新路径的自定义值优先；缺失值或面板预填的默认值沿用原设置。"""
    for key, value in original.items():
        current = target.get(key, MISSING)
        default = defaults.get(key, MISSING)
        if isinstance(current, dict) and isinstance(value, dict):
            _merge(current, value, default if isinstance(default, dict) else {})
        elif current is MISSING or current is None or current == default:
            target[key] = copy.deepcopy(value)


def migrate_layout(config: dict) -> dict:
    """返回迁移后的独立副本；不写文件，也不改变传入的原始配置。"""
    if not isinstance(config, dict):
        raise ValueError("配置根节点不是对象，无法安全迁移")
    # AstrBotConfig 是带文件锁等框架状态的 dict 子类，只复制配置值。
    migrated = copy.deepcopy(dict(config))
    defaults = _defaults(_load_schema())

    def move(path: tuple[str, ...], values: dict):
        target, default = migrated, defaults
        for key in path:
            target = _section(target, key)
            default = default.get(key, {})
            if not isinstance(default, dict):
                default = {}
        _merge(target, values, default)

    sources = migrated.get("sources")
    if isinstance(sources, dict):
        sources.pop("show_sites", None)

    old_feeds = migrated.pop("feeds", MISSING)
    if old_feeds is not MISSING:
        if not isinstance(old_feeds, dict):
            raise ValueError("旧订阅配置不是对象，无法安全迁移")
        mode = old_feeds.pop("mode", "网站")
        old_feeds.setdefault("enabled", mode == "订阅")
        move(("sources", "feeds"), old_feeds)

    draw = migrated.get("draw")
    if isinstance(draw, dict):
        images = {
            pool: draw.pop(f"reserve_{pool}")
            for pool in IMAGE_POOLS
            if f"reserve_{pool}" in draw
        }
        if images:
            move(("reserve", "images"), images)

    video = migrated.get("video")
    if isinstance(video, dict):
        video.pop("show_sources", None)
        sites = {key: video.pop(key) for key in VIDEO_SITES if key in video}
        if sites:
            move(("sources", "video"), sites)
        videos = {}
        for style in ("real", "anime"):
            key = f"reserve_{style}"
            if key in video:
                value = video.pop(key)
                for rating in ("sensitive", "explicit"):
                    videos[f"{style}_{rating}"] = value
        if videos:
            move(("reserve", "videos"), videos)

    return migrated


def _config_path() -> Path | None:
    # 官方插件加载器用插件目录名定位配置；核心配置 profile 不另存插件配置。
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_config_path
    except ModuleNotFoundError as exc:
        # 独立脚本或本地测试未加载 AstrBot 运行时，没有插件配置可迁移。
        if exc.name not in {
            "astrbot",
            "astrbot.core",
            "astrbot.core.utils",
            "astrbot.core.utils.astrbot_path",
        }:
            raise
        return None
    plugin_dir_name = PLUGIN_ROOT.name
    return Path(get_astrbot_config_path()) / f"{plugin_dir_name}_config.json"


def _write_temp(directory: Path, prefix: str, suffix: str, content: bytes) -> Path:
    descriptor, filename = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=suffix)
    path = Path(filename)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def migrate_config(config_path: Path | str | None = None) -> bool:
    """迁移本插件文件；无文件或已迁移时返回 False，失败时保留原文件并抛错。"""
    path = Path(config_path) if config_path is not None else _config_path()
    if path is None or not path.exists():
        return False
    replacement = None
    try:
        original_bytes = path.read_bytes()
        original = json.loads(original_bytes.decode("utf-8-sig"))
        migrated = migrate_layout(original)
        if migrated == original:
            return False
        replacement_bytes = (
            json.dumps(migrated, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        replacement = _write_temp(
            path.parent, f".{path.name}.", ".tmp", replacement_bytes
        )
        _write_temp(
            path.parent,
            f"{path.stem}.before-layout-migration.",
            ".json",
            original_bytes,
        )
        if path.read_bytes() != original_bytes:
            raise OSError("配置在迁移期间被修改")
        os.replace(replacement, path)
        return True
    except Exception:
        # 不附原异常、原 JSON 或配置值，避免在插件加载日志里输出账号凭据。
        raise RuntimeError(
            "配置布局迁移失败，原配置已保留；请检查配置文件及目录读写权限后重载插件。"
        ) from None
    finally:
        if replacement is not None:
            replacement.unlink(missing_ok=True)
