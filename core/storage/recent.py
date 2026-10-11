"""图片、视频和订阅共用的近期去重记录。"""

import json
from collections import deque
from pathlib import Path

from astrbot.api import logger


class Recent:
    """最近抽到过的视频（「图源键:id」），跨抽取、跨预备池的桶去重。

    每次抽取各自只记得自己抽过什么，热门的视频就会在桶内（补桶时再抽）、桶间反复出现；
    这里记住最近 limit 个，存到 path（有时），重启后保留。
    """

    def __init__(self, path: Path | None = None, limit: int = 3000):
        self.path = path
        self.limit = limit
        self._keys: dict[str, None] = {}
        if path is not None:
            try:
                keys = json.loads(path.read_text(encoding="utf-8"))
                self._keys = dict.fromkeys(str(k) for k in keys[-limit:])
            except (OSError, ValueError, TypeError):
                pass

    def __contains__(self, key: str) -> bool:
        return key in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    def add(self, key: str):
        self._keys.pop(key, None)
        self._keys[key] = None
        while len(self._keys) > self.limit:
            self._keys.pop(next(iter(self._keys)))
        if self.path is not None:
            try:
                self.path.write_text(json.dumps(list(self._keys)), encoding="utf-8")
            except OSError as e:
                logger.warning(f"[random_pic] 保存最近抽过的视频失败: {e!r}")


class RecentAuthors:
    """最近抽到过的作者（只在内存里）：高赞片集中在少数作者，换着来。"""

    def __init__(self, window: int):
        self._names: deque[str] = deque(maxlen=window)

    def __contains__(self, name: str) -> bool:
        return bool(name) and name in self._names

    def add(self, name: str):
        if name:
            self._names.append(name)
