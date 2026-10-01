"""已发送图片的登记表：回复图片要整本 PDF 时，用它找回图片所属的画廊。

NapCat 回传的图片消息段里，file 是上传时的临时文件名，不能用来识别图片；
file_size 则是原始字节数，和插件发出的图片文件大小一致。所以按字节数登记。
合并转发里的图片可能不带 file_size，所以同时按标题行（第几张/共几张-标题）登记。
"""

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from astrbot.api import logger

MAX_ENTRIES = 2000
MAX_SESSIONS = 500


@dataclass
class GalleryRef:
    gid: int
    token: str
    title: str = ""
    pages: int = 0


class SentRegistry:
    def __init__(self, path: Path):
        self.path = path
        self._by_size: dict[int, GalleryRef] = {}
        self._by_label: dict[str, GalleryRef] = {}
        self._last: dict[str, list[GalleryRef]] = {}
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._by_size = {
                int(k): GalleryRef(**v) for k, v in data["by_size"].items()
            }
            self._last = {
                k: [GalleryRef(**r) for r in refs] for k, refs in data["last"].items()
            }
            self._by_label = {
                k: GalleryRef(**v) for k, v in data.get("by_label", {}).items()
            }
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"[random_pic] 已发送图片登记表损坏，已重置: {e!r}")

    def _save(self):
        data = {
            "saved": int(time.time()),
            "by_size": {str(k): asdict(v) for k, v in self._by_size.items()},
            "by_label": {k: asdict(v) for k, v in self._by_label.items()},
            "last": {k: [asdict(r) for r in refs] for k, refs in self._last.items()},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            logger.warning(f"[random_pic] 保存已发送图片登记表失败: {e!r}")

    def record(self, session: str, sent: list[tuple[int, str, GalleryRef]]):
        """登记一次抽卡发出的图片：[(文件字节数, 标题行, 画廊)]，按发送顺序。"""
        for size, label, ref in sent:
            # 重新插入，保持「最近」在末尾
            for table, key in ((self._by_size, size), (self._by_label, label)):
                table.pop(key, None)
                table[key] = ref
        for table in (self._by_size, self._by_label):
            while len(table) > MAX_ENTRIES:
                table.pop(next(iter(table)))
        self._last.pop(session, None)
        self._last[session] = [ref for _, _, ref in sent]
        while len(self._last) > MAX_SESSIONS:
            self._last.pop(next(iter(self._last)))
        self._save()

    def by_size(self, size: int) -> GalleryRef | None:
        return self._by_size.get(size)

    def by_label(self, label: str) -> GalleryRef | None:
        return self._by_label.get(label)

    def last(self, session: str) -> list[GalleryRef]:
        return list(self._last.get(session, []))
