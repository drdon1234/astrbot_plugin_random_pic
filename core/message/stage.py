"""准备发送所需的共享文件，并管理暂存文件清理。"""

import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger

# 发送时在共享目录里给图片、视频建的链接的文件名前缀
STAGE_PREFIX = "rp_"


# 发送超时、连接中断时 QQ 机器人可能还在读这些文件（大视频上传要很久），这么多秒后再删
STAGE_KEEP_SECONDS = 600


class Stage:
    """共享目录：QQ 机器人（如 NapCat）也能以同一路径读取的目录，插件把图片、视频直接存在里面。

    发送时给每个文件建一个硬链接（不复制数据），消息里只放链接的 file:// 路径，不再把整个文件
    编码进消息；发完删掉链接。用链接而不是原文件，是为了发送超时时原文件被缓存、预备池删掉后
    QQ 机器人仍能读到。文件不在同一文件系统（建不了硬链接）时复制。
    """

    def __init__(self, root: Path):
        self.root = root

    def prepare(self):
        """建好目录，仅回收超过上传保护期的暂存文件。"""
        self.root.mkdir(parents=True, exist_ok=True)
        for lease in self.root.glob(f"{STAGE_PREFIX}*.lease"):
            try:
                expires = float(lease.read_text(encoding="ascii"))
            except (OSError, ValueError):
                continue
            if expires <= time.time():
                self.remove([lease.with_suffix('')])
        # 缺失或损坏的标记无法证明媒体已过期，先赋予一次完整保护期再回收。
        for path in self.root.glob(f"{STAGE_PREFIX}*"):
            if not path.is_file() or path.suffix == '.lease':
                continue
            try:
                float(Path(f"{path}.lease").read_text(encoding="ascii"))
            except (OSError, ValueError):
                self.protect([path])

    @staticmethod
    def supports(chain: list) -> bool:
        """消息里只有文字、本地图片和视频以及（只含这些的）合并转发时才能改用文件路径发送。"""
        for comp in chain:
            if isinstance(comp, Comp.Nodes):
                if not all(Stage.supports(node.content) for node in comp.nodes):
                    return False
            elif isinstance(comp, (Comp.Image, Comp.Video)):
                if not getattr(comp, "path", ""):
                    return False
            elif not isinstance(comp, Comp.Plain):
                return False
        return True

    def segments(self, content: list, copies: list[Path]) -> list[dict]:
        """消息段 → OneBot 消息段：图片、视频链接到共享目录，建出的文件记进 copies。"""
        out = []
        for comp in content:
            if isinstance(comp, (Comp.Image, Comp.Video)):
                src = Path(comp.path)
                dest = self.root / f"{STAGE_PREFIX}{uuid.uuid4().hex}{src.suffix}"
                # 在创建前登记，复制中途失败或取消后也能清理半成品。
                copies.append(dest)
                self.protect([dest])
                try:
                    os.link(src, dest)
                except OSError:
                    shutil.copyfile(src, dest)
                kind = "video" if isinstance(comp, Comp.Video) else "image"
                out.append({"type": kind, "data": {"file": dest.resolve().as_uri()}})
            elif comp.text.strip():
                out.append({"type": "text", "data": {"text": comp.text}})
        return out

    def forward(self, nodes, copies: list[Path]) -> list[dict]:
        return [
            {
                "type": "node",
                "data": {
                    "user_id": str(node.uin),
                    "nickname": node.name,
                    "content": self.segments(node.content, copies),
                },
            }
            for node in nodes.nodes
        ]

    @staticmethod
    def protect(copies: list[Path], delay: float = STAGE_KEEP_SECONDS):
        # 独立小文件记录期限，不改变硬链接对应的原始资产时间戳；重载仍可读取。
        expires = str(time.time() + delay)
        for path in copies:
            Path(f"{path}.lease").write_text(expires, encoding="ascii")

    @staticmethod
    def remove(copies: list[Path]):
        for path in copies:
            path.unlink(missing_ok=True)
            Path(f"{path}.lease").unlink(missing_ok=True)

    @staticmethod
    def remove_later(copies: list[Path], delay: float = STAGE_KEEP_SECONDS):
        if copies:
            try:
                Stage.protect(copies, delay)
            except OSError as e:
                # 延长期限失败时去掉旧期限，重载不得按过期标记立即删除媒体。
                for path in copies:
                    try:
                        Path(f"{path}.lease").unlink(missing_ok=True)
                    except OSError:
                        pass
                logger.warning(f"[random_pic] 更新发送文件保护期限失败，按延时回收: {e!r}")
            asyncio.get_running_loop().call_later(delay, Stage.remove, list(copies))
