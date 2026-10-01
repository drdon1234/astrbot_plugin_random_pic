"""E-Hentai 标签中文库：中文关键词翻译、标签中文名与随机角色。

数据来自 EhTagTranslation 数据库（https://github.com/EhTagTranslation/Database，
CC BY-NC-SA 3.0），运行时下载到插件数据目录，不随插件分发。
"""

import asyncio
import gzip
import json
import random
import re
import time
from pathlib import Path

from astrbot.api import logger

from .net import HttpClient

# 中文名冲突时按这个顺序取第一个
INDEX_NAMESPACES = (
    "character",
    "parody",
    "cosplayer",
    "female",
    "male",
    "mixed",
    "other",
    "artist",
    "group",
)
# 本地缓存超过这么久重新下载
REFRESH_SECONDS = 7 * 86400
# 下载失败后至少隔这么久再试
RETRY_SECONDS = 600
TRAILING_NOTE = re.compile(r"\s*[（(][^（()）]*[）)]$")
# 部分中文名带表情符号，例如「粪便💩」「呕吐🤮」，用户输入时不会带
EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),
    (0x2600, 0x27BF),
    (0xFE0F, 0xFE0F),
    (0x200D, 0x200D),
)
EMOJI = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in EMOJI_RANGES) + "]")


def search_term(namespace: str, raw: str) -> str:
    return f'{namespace}:"{raw}$"'


def name_variants(name: str) -> list[str]:
    """「榛名(鲑) | 春奈」→ ["榛名(鲑)", "春奈", "榛名"]，第一个是主名。"""
    names = [n.strip() for n in name.split("|") if n.strip()]
    stripped = [TRAILING_NOTE.sub("", n) for n in names]
    stripped += [EMOJI.sub("", n).strip() for n in names + stripped]
    out = []
    for n in names + stripped:
        if n and n not in out:
            out.append(n)
    return out


class TagIndex:
    def __init__(self, data: dict[str, dict[str, dict]]):
        """data 为 {命名空间: {原始标签: {"name": 中文名, ...}}}。"""
        self.zh_names: dict[str, str] = {}
        self.lookup: dict[str, tuple[str, str]] = {}
        for namespace in INDEX_NAMESPACES:
            # 先按命名空间顺序，同一命名空间内主名优先于别名
            aliases = []
            for raw, entry in data.get(namespace, {}).items():
                variants = name_variants(str(entry.get("name") or ""))
                if not variants:
                    continue
                self.zh_names[f"{namespace}:{raw}"] = variants[0]
                self.lookup.setdefault(variants[0].lower(), (namespace, raw))
                aliases.extend((v.lower(), (namespace, raw)) for v in variants[1:])
            for key, value in aliases:
                self.lookup.setdefault(key, value)
        self.characters = sorted(data.get("character", {}))

    @classmethod
    def from_db(cls, db: dict) -> "TagIndex":
        return cls({ns["namespace"]: ns["data"] for ns in db["data"]})

    def translate(self, token: str) -> str:
        """中文名转为 E-Hentai 搜索词，支持「-」前缀排除；查不到时原样返回。

        纯 ASCII 的词（英文、罗马音、已经写好的 E-Hentai 语法）不翻译。
        """
        if token.isascii() or ":" in token or '"' in token or token.startswith("~"):
            return token
        negate = token.startswith("-")
        hit = self.lookup.get(token[1:].lower() if negate else token.lower())
        if hit is None:
            return token
        return ("-" if negate else "") + search_term(*hit)

    def zh(self, tag: str) -> str:
        """「namespace:raw」→ 中文名，查不到时返回原始标签名。"""
        return self.zh_names.get(tag, tag.split(":", 1)[-1])

    def random_character(self) -> str | None:
        return random.choice(self.characters) if self.characters else None


class TagDB:
    """按需加载标签库：本地缓存过期时重新下载，下载失败时沿用旧缓存。"""

    def __init__(self, http: HttpClient, path: Path, url: str):
        self.http = http
        self.path = path
        self.url = url
        self.index: TagIndex | None = None
        self._loaded_mtime = 0.0
        self._next_download = 0.0
        self._lock = asyncio.Lock()

    def _stale(self) -> bool:
        return (
            not self.path.exists()
            or time.time() - self.path.stat().st_mtime > REFRESH_SECONDS
        )

    async def get(self) -> TagIndex | None:
        async with self._lock:
            if self._stale() and time.monotonic() >= self._next_download:
                await self._download()
            if self.path.exists() and self.path.stat().st_mtime != self._loaded_mtime:
                try:
                    self.index = await asyncio.to_thread(self._load)
                    self._loaded_mtime = self.path.stat().st_mtime
                except Exception as e:
                    logger.warning(f"[random_pic] 标签库解析失败: {e!r}")
            return self.index

    async def _download(self):
        try:
            data = await self.http.get_bytes(self.url)
            json.loads(gzip.decompress(data))  # 校验完整性
        except Exception as e:
            self._next_download = time.monotonic() + RETRY_SECONDS
            logger.warning(f"[random_pic] 标签库下载失败: {e!r}")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(self.path)
        logger.info(f"[random_pic] 标签库已更新（{len(data) // 1024} KB）")

    def _load(self) -> TagIndex:
        with gzip.open(self.path) as f:
            return TagIndex.from_db(json.load(f))
