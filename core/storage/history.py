"""已发送图集的记录，以及 /pdf 要打包哪个画廊的判定。

一次抽卡发出若干图集，按发送顺序编号（标题行里的【序号】）。插件记下：
- 每条发出的消息里有哪些图集（QQ 上插件自己调用 OneBot 接口发送，拿得到消息 ID）；
- 每个会话最近一次抽卡的全部图集。

/pdf 回复某条消息时按消息 ID 查；查不到（AstrBot 代发、其他平台）时解析被回复消息的文字，
按【序号】标题行分段，每段里找作品链接（说明文字附带；哔咔没有公开链接，只能按消息 ID 查）。
认链接交给各图源（SourceSet.links）。
"""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger

from ..models import WorkRef
from ..utils import run_thread

MAX_MESSAGES = 1000
MAX_SESSIONS = 500
# 固定窗口覆盖默认 0.5 秒的逐条发送节奏，不随新记录延长等待。
SAVE_DELAY = 2
SAVE_RETRY_DELAY = 5
# 提示选序号时最多列出的条数
MAX_LISTED = 30
# 图集标题行：「【序号】标题」换行「第 x/y 张 · 来源」
HEADER_RE = re.compile(r"(?m)^【(\d+)】(.*)\n第 \d+/\d+ 张 · (.*)$")
# 文字 → 其中的作品链接（按出现顺序去重）
Links = Callable[[str], list[WorkRef]]


@dataclass
class SentAlbum:
    idx: int
    title: str
    source: str
    work: WorkRef | None = None

    def to_json(self) -> dict:
        data = {"idx": self.idx, "title": self.title, "source": self.source}
        if self.work:
            data["work"] = [self.work.source, self.work.id, self.work.token]
        return data

    @classmethod
    def from_json(cls, data: dict) -> "SentAlbum":
        work = WorkRef(*map(str, data["work"])) if data.get("work") else None
        return cls(int(data["idx"]), data["title"], data["source"], work)


def clean_title(title: str) -> str:
    return " ".join(title.split()) or "无标题"


def header_text(idx: int, title: str, page: int, total: int, source: str) -> str:
    """图集第一张图上方的两行标题。"""
    return f"【{idx}】{clean_title(title)}\n第 {page}/{total} 张 · {source}"


def page_text(page: int, total: int) -> str:
    """同一图集后面几张图上方的页码行。"""
    return f"第 {page}/{total} 张"


def albums_from_text(text: str, links: Links) -> list[SentAlbum]:
    """从消息文字里认出图集：有标题行时按【序号】分段，否则每个作品链接算一个图集。"""
    headers = list(HEADER_RE.finditer(text or ""))
    if not headers:
        return [SentAlbum(i, "", "", ref) for i, ref in enumerate(links(text), 1)]
    albums = []
    for i, match in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        works = links(text[match.end() : end])
        albums.append(
            SentAlbum(
                int(match.group(1)),
                match.group(2).strip(),
                match.group(3).strip(),
                # 跨帖图集包含多个作品链接，不能把首帖当作整个图集。
                works[0] if len(works) == 1 else None,
            )
        )
    return albums


def _listing(albums: list[SentAlbum], intro: str) -> str:
    lines = [intro]
    for album in albums[:MAX_LISTED]:
        name = album.title or "（无标题）"
        if album.work is None:
            name += f"（{album.source}，无单个作品引用）"
        lines.append(f"{album.idx}. {name}")
    if len(albums) > MAX_LISTED:
        lines.append(f"……共 {len(albums)} 个图集")
    return "\n".join(lines)


def pick(
    albums: list[SentAlbum], index: int | None, strict: bool, command: str
) -> tuple[SentAlbum | None, str]:
    """选出要打包的图集；选不出时返回提示文字。

    strict（回复的是某条消息）时，消息里有多个图集就必须带序号；否则（本会话上一次抽卡）
    所有能打包的图集都来自同一个作品时可以不带序号。选中的图集找不到作品时由调用方提示。
    """
    if not albums:
        if strict:
            return None, (
                "没有找到所引用消息的作品记录，引用文字中也没有可识别的作品链接。"
                f"请复制图集说明中的链接，发送 {command} <作品链接>；"
                "没有链接时请重新抽图，再引用新图片消息。"
            )
        return None, (
            "没有找到本会话最近抽图的记录。请引用抽图发出的图片消息，"
            f"或发送 {command} <作品链接>；也可以先重新抽图。"
        )
    example = next((a.idx for a in albums if a.work), albums[0].idx)
    follow = (
        "请继续引用原图片消息" if strict else "请引用要选的原图片消息"
    ) + f"，发送 {command} {example}（不能只发数字）："
    if index is not None:
        chosen = next((a for a in albums if a.idx == index), None)
        if chosen is None:
            return None, _listing(
                albums, f"没有序号 {index}，共 {len(albums)} 个图集。{follow}"
            )
        return chosen, ""
    if len(albums) == 1:
        return albums[0], ""
    if not strict:
        works = {a.work for a in albums if a.work}
        if len(works) == 1:
            return next(a for a in albums if a.work), ""
    where = "引用的消息里" if strict else "本会话最近一次抽图"
    return None, _listing(
        albums,
        f"{where}共 {len(albums)} 个图集。{follow}",
    )


class History:
    def __init__(self, path: Path):
        self.path = path
        # 消息 ID → 图集；会话 → 最近一次抽卡的图集
        self._messages: dict[str, list[SentAlbum]] = {}
        self._last: dict[str, list[SentAlbum]] = {}
        self._revision = 0
        self._saved_revision = 0
        self._save_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._closed = False
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

    def _snapshot(self):
        return {
            key: {k: [a.to_json() for a in albums] for k, albums in table.items()}
            for key, table in (("messages", self._messages), ("last", self._last))
        }

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def _schedule_save(self):
        self._revision += 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._save(self._snapshot())
                self._saved_revision = self._revision
            except OSError as e:
                logger.warning(f"[random_pic] 保存已发送图集记录失败: {e!r}")
            return
        if self._save_task is None or self._save_task.done():
            self._save_task = loop.create_task(self._save_later())

    async def _flush(self):
        while self._saved_revision != self._revision:
            revision, data = self._revision, self._snapshot()
            writing = asyncio.create_task(run_thread(self._save, data))
            try:
                await asyncio.shield(writing)
            except asyncio.CancelledError:
                while not writing.done():
                    try:
                        await asyncio.shield(writing)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not writing.cancelled() and writing.exception() is None:
                    self._saved_revision = revision
                raise
            self._saved_revision = revision

    async def _save_later(self):
        delay = SAVE_DELAY
        while self._saved_revision != self._revision:
            await asyncio.sleep(delay)
            try:
                await self._flush()
            except OSError as e:
                logger.warning(f"[random_pic] 保存已发送图集记录失败，稍后重试: {e!r}")
                delay = SAVE_RETRY_DELAY

    async def close(self):
        """停止计划保存并等待磁盘线程，最后刷新尚未落盘的记录。"""
        if self._closed:
            return
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self._save_task is not None:
            self._save_task.cancel()
            await asyncio.gather(self._save_task, return_exceptions=True)
        await self._flush()
        self._closed = True

    def _check_open(self):
        if self._closed or (self._close_task is not None and not self._close_task.done()):
            raise RuntimeError("已发送图集记录正在关闭或已关闭")

    @staticmethod
    def _put(table: dict, key: str, albums: list[SentAlbum], limit: int):
        # 重新插入，保持「最近」在末尾
        table.pop(key, None)
        table[key] = list(albums)
        while len(table) > limit:
            table.pop(next(iter(table)))

    def record_draw(self, session: str, albums: list[SentAlbum]):
        """登记本会话最近一次抽卡的全部图集。"""
        self._check_open()
        self._put(self._last, session, albums, MAX_SESSIONS)
        self._schedule_save()

    def record_message(self, message_id: str, albums: list[SentAlbum]):
        """登记一条发出的消息里有哪些图集。"""
        self._check_open()
        self._put(self._messages, str(message_id), albums, MAX_MESSAGES)
        self._schedule_save()

    def by_message(self, message_id: str) -> list[SentAlbum] | None:
        albums = self._messages.get(str(message_id))
        return list(albums) if albums is not None else None

    def last(self, session: str) -> list[SentAlbum]:
        return list(self._last.get(session, []))
