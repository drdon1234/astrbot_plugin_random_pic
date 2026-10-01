"""已发送图集的记录，以及 /pdf 要打包哪个画廊的判定。

一次抽卡发出若干图集，按发送顺序编号（标题行里的【序号】）。插件记下：
- 每条发出的消息里有哪些图集（QQ 上插件自己调用 OneBot 接口发送，拿得到消息 ID）；
- 每个会话最近一次抽卡的全部图集。

/pdf 回复某条消息时按消息 ID 查；查不到（AstrBot 代发、其他平台）时解析被回复消息的文字，
按【序号】标题行分段，每段里找画廊链接（说明文字附带）。
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from .models import GalleryRef

MAX_MESSAGES = 1000
MAX_SESSIONS = 500
# 提示选序号时最多列出的条数
MAX_LISTED = 30
GALLERY_URL_RE = re.compile(
    r"https?://(?:e-hentai|exhentai)\.org/g/(\d+)/([0-9a-f]{10})"
)
# 图集标题行：「【序号】标题」换行「第 x/y 张 · 来源」
HEADER_RE = re.compile(r"(?m)^【(\d+)】(.*)\n第 \d+/\d+ 张 · (.*)$")


@dataclass
class SentAlbum:
    idx: int
    title: str
    source: str
    gallery: GalleryRef | None = None

    def to_json(self) -> dict:
        data = {"idx": self.idx, "title": self.title, "source": self.source}
        if self.gallery:
            data["gid"], data["token"] = self.gallery.gid, self.gallery.token
        return data

    @classmethod
    def from_json(cls, data: dict) -> "SentAlbum":
        gallery = (
            GalleryRef(int(data["gid"]), data["token"]) if data.get("gid") else None
        )
        return cls(int(data["idx"]), data["title"], data["source"], gallery)


def clean_title(title: str) -> str:
    return " ".join(title.split()) or "无标题"


def header_text(idx: int, title: str, page: int, total: int, source: str) -> str:
    """图集第一张图上方的两行标题。"""
    return f"【{idx}】{clean_title(title)}\n第 {page}/{total} 张 · {source}"


def page_text(page: int, total: int) -> str:
    """同一图集后面几张图上方的页码行。"""
    return f"第 {page}/{total} 张"


def gallery_from_text(text: str) -> GalleryRef | None:
    match = GALLERY_URL_RE.search(text or "")
    return GalleryRef(int(match.group(1)), match.group(2)) if match else None


def albums_from_text(text: str) -> list[SentAlbum]:
    """从消息文字里认出图集：有标题行时按【序号】分段，否则每个画廊链接算一个图集。"""
    headers = list(HEADER_RE.finditer(text or ""))
    if not headers:
        links = dict.fromkeys(
            GalleryRef(int(gid), token)
            for gid, token in GALLERY_URL_RE.findall(text or "")
        )
        return [SentAlbum(i, "", "E-Hentai", ref) for i, ref in enumerate(links, 1)]
    albums = []
    for i, match in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        albums.append(
            SentAlbum(
                int(match.group(1)),
                match.group(2).strip(),
                match.group(3).strip(),
                gallery_from_text(text[match.end() : end]),
            )
        )
    return albums


def _listing(albums: list[SentAlbum], intro: str) -> str:
    lines = [intro]
    for album in albums[:MAX_LISTED]:
        name = album.title or "（无标题）"
        if album.gallery is None:
            name += f"（{album.source}，不支持）"
        lines.append(f"{album.idx}. {name}")
    if len(albums) > MAX_LISTED:
        lines.append(f"……共 {len(albums)} 个图集")
    return "\n".join(lines)


def pick(
    albums: list[SentAlbum], index: int | None, strict: bool
) -> tuple[SentAlbum | None, str]:
    """选出要打包的图集；选不出时返回提示文字。

    strict（回复的是某条消息）时，消息里有多个图集就必须带序号；否则（本会话上一次抽卡）
    所有能打包的图集都来自同一个画廊时可以不带序号。选中的图集不是 E-Hentai 画廊时由调用方提示。
    """
    if not albums:
        return None, "没有找到图集：请回复抽图发出的消息，或使用 /pdf <画廊链接>。"
    if index is not None:
        chosen = next((a for a in albums if a.idx == index), None)
        if chosen is None:
            return None, _listing(
                albums, f"没有序号 {index}，共 {len(albums)} 个图集："
            )
        return chosen, ""
    if len(albums) == 1:
        return albums[0], ""
    if not strict:
        galleries = {a.gallery for a in albums if a.gallery}
        if len(galleries) == 1:
            return next(a for a in albums if a.gallery), ""
    example = next((a.idx for a in albums if a.gallery), albums[0].idx)
    where = "引用的消息里" if strict else "上一次抽卡"
    return None, _listing(
        albums,
        f"{where}共 {len(albums)} 个图集，请在指令后加序号，例如 /pdf {example}：",
    )


class History:
    def __init__(self, path: Path):
        self.path = path
        # 消息 ID → 图集；会话 → 最近一次抽卡的图集
        self._messages: dict[str, list[SentAlbum]] = {}
        self._last: dict[str, list[SentAlbum]] = {}
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for table, key in ((self._messages, "messages"), (self._last, "last")):
                for k, albums in data[key].items():
                    table[k] = [SentAlbum.from_json(a) for a in albums]
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"[random_pic] 已发送图集记录损坏，已重置: {e!r}")
            self._messages.clear()
            self._last.clear()

    def _save(self):
        data = {
            key: {k: [a.to_json() for a in albums] for k, albums in table.items()}
            for key, table in (("messages", self._messages), ("last", self._last))
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            logger.warning(f"[random_pic] 保存已发送图集记录失败: {e!r}")

    @staticmethod
    def _put(table: dict, key: str, albums: list[SentAlbum], limit: int):
        # 重新插入，保持「最近」在末尾
        table.pop(key, None)
        table[key] = list(albums)
        while len(table) > limit:
            table.pop(next(iter(table)))

    def record_draw(self, session: str, albums: list[SentAlbum]):
        """登记本会话最近一次抽卡的全部图集。"""
        self._put(self._last, session, albums, MAX_SESSIONS)
        self._save()

    def record_message(self, message_id: str, albums: list[SentAlbum]):
        """登记一条发出的消息里有哪些图集。"""
        self._put(self._messages, str(message_id), albums, MAX_MESSAGES)
        self._save()

    def by_message(self, message_id: str) -> list[SentAlbum] | None:
        albums = self._messages.get(str(message_id))
        return list(albums) if albums is not None else None

    def last(self, session: str) -> list[SentAlbum]:
        return list(self._last.get(session, []))
