"""定时推送：从每天 0 点起按固定间隔触发（默认每个整点），把抽图结果主动发到配置的会话。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from astrbot.api import logger

GROUP = "GroupMessage"
FRIEND = "FriendMessage"


def next_fire(after: datetime, interval: int) -> datetime:
    """after 之后（不含 after）的下一个触发时刻。

    从当天 0 点起每 interval 分钟触发一次；间隔不能整除一天时，最后一段到次日 0 点为止，
    次日 0 点重新开始计算。
    """
    midnight = after.replace(hour=0, minute=0, second=0, microsecond=0)
    step = timedelta(minutes=interval)
    fire = midnight + step * ((after - midnight) // step + 1)
    return min(fire, midnight + timedelta(days=1))


@dataclass(frozen=True)
class Target:
    """一个推送目标会话。"""

    umo: str
    platform_id: str
    is_group: bool
    # 群号或 QQ 号
    chat_id: str


def parse_target(umo: str) -> Target | None:
    """解析会话 ID（/sid 显示的「平台:消息类型:会话」），不是群聊或私聊时返回 None。"""
    parts = umo.strip().split(":", 2)
    if len(parts) != 3 or parts[1] not in (GROUP, FRIEND) or not parts[2]:
        return None
    platform_id, kind, session = parts
    is_group = kind == GROUP
    # 开启会话隔离时群聊会话是「用户_群号」
    chat_id = session.split("_")[-1] if is_group else session
    return Target(umo.strip(), platform_id, is_group, chat_id)


class PushScheduler:
    """后台循环：等到下一个触发时刻执行 job，job 耗时超过间隔时跳过错过的时刻。"""

    def __init__(self, interval: int, job: Callable[[], Awaitable[None]]):
        self.interval = interval
        self.job = job
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop())

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self):
        last = datetime.now()
        while True:
            # 睡眠可能提前几毫秒醒来，从上一次的触发时刻往后算，避免同一时刻触发两次
            fire = next_fire(max(datetime.now(), last), self.interval)
            await asyncio.sleep(max(0.0, (fire - datetime.now()).total_seconds()))
            last = fire
            try:
                await self.job()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[random_pic] 定时推送出错")
