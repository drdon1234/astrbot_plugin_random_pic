"""管理员预备池命令。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:
    from ..runtime import Runtime


REFRESH_COMMAND = "刷新预备池"
# 刷新后最多等多久报告结果，之后在后台继续补充。
REFRESH_WAIT = 1800


class ReserveCommands:
    """只向私聊管理员提供预备池刷新。"""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self._refreshing = False

    async def refresh(self, event: AstrMessageEvent):
        """只有管理员在私聊里能用，其他人不回复。等这一轮补完后报告每个池补了几批。"""
        if not event.is_private_chat() or not self.runtime.access.is_admin(
            str(event.get_sender_id())
        ):
            return
        tokens = event.message_str.split()[1:]
        reserves = []
        if "视频" not in tokens:
            reserves.append(self.runtime.reserve)
        if not {"图片", "图"} & set(tokens):
            reserves.append(self.runtime.video_reserve)
        reserves = [r for r in reserves if r.pools]
        if not reserves:
            yield event.plain_result("没有在维护的预备池。")
            return
        if self._refreshing:
            yield event.plain_result("预备池正在刷新，请等这次刷新完成。")
            return
        self._refreshing = True
        try:
            dropped = sum([await r.refresh() for r in reserves])
            pools = [p for r in reserves for p in r.pools.values()]
            yield event.plain_result(
                f"已丢弃 {dropped} 批，正在按当前配置重新抽 {len(pools)} 个预备池"
                f"（共 {sum(p.batches for p in pools)} 批），抽完后告诉你……"
            )
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*(r.settled() for r in reserves)), REFRESH_WAIT
                )
            except asyncio.TimeoutError:
                yield event.plain_result(
                    f"{REFRESH_WAIT // 60} 分钟还没抽完，之后在后台接着抽。"
                )
                return
            lines = ["预备池刷新完成："]
            for name, ready, batches in (row for rows in results for row in rows):
                note = "" if ready >= batches else "（没抽满，稍后自动接着抽）"
                lines.append(f"· {name}：{ready}/{batches} 批{note}")
            yield event.plain_result("\n".join(lines))
        finally:
            self._refreshing = False
