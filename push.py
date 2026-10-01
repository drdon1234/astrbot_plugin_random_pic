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
    """一个推送目标：QQ 群或 QQ 用户。"""

    is_group: bool
    # 群号或 QQ 号
    chat_id: str

    def umo(self, platform_id: str) -> str:
        """该目标在平台 platform_id 上的会话 ID。"""
        return f"{platform_id}:{GROUP if self.is_group else FRIEND}:{self.chat_id}"

    def __str__(self) -> str:
        return f"{'群' if self.is_group else 'QQ'} {self.chat_id}"


def parse_targets(
    groups: list[str], users: list[str]
) -> tuple[list[Target], list[str]]:
    """群号、QQ 号列表 → 推送目标（去重，保持顺序），以及不是纯数字的无效项。"""
    targets, invalid = [], []
    for is_group, ids in ((True, groups), (False, users)):
        for chat_id in dict.fromkeys(ids):
            if chat_id.isdigit():
                targets.append(Target(is_group, chat_id))
            else:
                invalid.append(chat_id)
    return targets, invalid


class PushScheduler:
    """后台循环：等到下一个触发时刻执行 job(触发时刻)，job 耗时超过间隔时跳过错过的时刻。"""

    def __init__(self, interval: int, job: Callable[[datetime], Awaitable[None]]):
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
                await self.job(fire)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[random_pic] 定时推送出错")
