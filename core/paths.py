"""插件安装位置；运行数据仍由 AstrBot 与用户配置决定。"""

from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_random_pic"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PLUGIN_ROOT / "_conf_schema.json"
