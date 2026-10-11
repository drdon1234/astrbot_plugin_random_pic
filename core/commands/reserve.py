"""管理员预备池命令。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .common import event_args

if TYPE_CHECKING:
    from ..runtime import Runtime


REFRESH_COMMAND = "刷新预备池"
# 刷新后最多等多久报告本轮结果，之后仅由预备池继续维护。
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
        tokens = event_args(self.runtime, event)
        if tokens is None:
            return
        commands = self.runtime.commands
        if commands.is_help(tokens):
            yield event.plain_result(
                "【刷新预备池】仅限管理员私聊使用。\n"
                "会丢弃所选预备池中已准备和正在准备的批次，按当前配置重新补充。\n"
                f"· {commands.command(REFRESH_COMMAND)}：刷新图片和视频\n"
                f"· {commands.command(REFRESH_COMMAND, '图片')}：只刷新图片（也可写「图」）\n"
                f"· {commands.command(REFRESH_COMMAND, '视频')}：只刷新视频\n"
                f"· {commands.command(REFRESH_COMMAND, '图片 视频')}：同时刷新两类\n"
                "会报告本轮结果；未补满或等待超时后在后台继续，不会另发完成通知。"
            )
            return
        unknown = [token for token in tokens if token not in {"图片", "图", "视频"}]
        if unknown:
            yield event.plain_result(
                f"不支持的刷新参数：{'、'.join(unknown)}。"
                f"请发送 {commands.command('帮助')} 查看用法。"
            )
            return
        selected = []
        if not tokens or {"图片", "图"} & set(tokens):
            selected.append(("图片", self.runtime.reserve))
        if not tokens or "视频" in tokens:
            selected.append(("视频", self.runtime.video_reserve))
        reserves = [reserve for _, reserve in selected if reserve.pools]
        if not reserves:
            scope = "和".join(name for name, _ in selected)
            yield event.plain_result(
                f"当前未配置可维护的{scope}预备池。"
                "请检查预选池配置中的批数，并确认对应图源或视频源已启用。"
            )
            return
        if self._refreshing:
            yield event.plain_result(
                "预备池正在刷新，本次未重复执行；请等待当前轮次的结果。"
            )
            return
        self._refreshing = True
        try:
            yield event.plain_result("预备池刷新已受理，正在清理所选批次并重新准备……")
            dropped = sum([await r.refresh() for r in reserves])
            pools = [p for r in reserves for p in r.pools.values()]
            skipped = "、".join(name for name, reserve in selected if not reserve.pools)
            note = f"{skipped}预备池未配置，已跳过。" if skipped else ""
            yield event.plain_result(
                f"已丢弃 {dropped} 批，正在按当前配置重新抽 {len(pools)} 个预备池"
                f"（共 {sum(p.batches for p in pools)} 批）。{note}"
            )
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*(r.settled() for r in reserves)), REFRESH_WAIT
                )
            except asyncio.TimeoutError:
                yield event.plain_result(
                    f"等待 {REFRESH_WAIT // 60} 分钟后仍未完成本轮补充，"
                    "预备池会在后台继续；本次不会再发送完成通知。"
                )
                return
            rows = [row for result in results for row in result]
            incomplete = any(ready < batches for _, ready, batches in rows)
            lines = [
                "本轮补充已结束，预备池尚未补满：" if incomplete else "预备池刷新完成："
            ]
            for name, ready, batches in rows:
                note = "" if ready >= batches else "（未补满）"
                lines.append(f"· {name}：{ready}/{batches} 批{note}")
            if incomplete:
                lines.append("未补满的池会在后台继续补充，本次不会再发送完成通知。")
            yield event.plain_result("\n".join(lines))
        except Exception:
            logger.exception("[random_pic] 刷新预备池失败")
            yield event.plain_result(
                "预备池刷新失败，部分池可能已开始补充。请检查日志和目录配置，"
                f"排除问题后再发送 {commands.command(REFRESH_COMMAND, ' '.join(tokens))}。"
            )
        finally:
            self._refreshing = False
