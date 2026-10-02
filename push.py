"""定时推送：每条推送任务每天从起始时间起按间隔、或在每天的时间点触发，每个群、每个用户单独抽一次，
通过 QQ（aiocqhttp）主动推送。推送内容是 /抽图 的参数，可以是普通抽图，也可以是完整作品。"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain

from .access import AccessControl
from .drawer import Drawer
from .models import RATING_NAMES, STYLE_NAMES, DrawRequest
from .pdf import PdfStore
from .sender import (
    FORWARD_MAX_NODES,
    ONEBOT,
    SEND_INTERVAL,
    Dispatcher,
    Send,
    SendFailed,
    send_onebot,
)
from .settings import PDF_FORMAT, PushTask
from .works import WorkService, describe

GROUP = "GroupMessage"
FRIEND = "FriendMessage"


def next_fire(after: datetime, interval: int, start: time = time(0, 0)) -> datetime:
    """按间隔推送：after 之后（不含 after）的下一个触发时刻。

    每天从 start 起每 interval 分钟触发一次；间隔不能整除一天时，最后一段到次日的 start 为止，
    次日的 start 重新开始计算。
    """
    anchor = datetime.combine(after.date(), start)
    if anchor > after:
        anchor -= timedelta(days=1)
    step = timedelta(minutes=interval)
    fire = anchor + step * ((after - anchor) // step + 1)
    return min(fire, anchor + timedelta(days=1))


def next_daily(after: datetime, times: list[time]) -> datetime:
    """按时间点推送：after 之后（不含 after）最近的一个时间点。times 已排序。"""
    day = after.date()
    for offset in (0, 1):
        for t in times:
            fire = datetime.combine(day + timedelta(days=offset), t)
            if fire > after:
                return fire
    raise ValueError("没有推送时间")


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


def parse_targets(groups: list[str], users: list[str]) -> list[Target]:
    """群号、QQ 号列表 → 推送目标（去重，保持顺序）。不是纯数字的项记日志后忽略。"""
    targets = []
    for is_group, ids in ((True, groups), (False, users)):
        for chat_id in dict.fromkeys(ids):
            if chat_id.isdigit():
                targets.append(Target(is_group, chat_id))
            else:
                logger.warning(
                    f"[random_pic] 定时推送：不是有效的群号或 QQ 号，已忽略：{chat_id}"
                )
    return targets


def push_title(when: str) -> str:
    """推送说明的开头，写明是本插件在 when（时:分）的定时推送。"""
    return f"【定时推送】抽图插件 {when} 的定时推送"


class PushScheduler:
    """后台循环：等到下一个触发时刻执行 job(触发时刻)，job 耗时太久时跳过错过的时刻。

    next_after(时刻) 返回该时刻之后的下一个触发时刻。
    """

    def __init__(
        self,
        next_after: Callable[[datetime], datetime],
        job: Callable[[datetime], Awaitable[None]],
    ):
        self.next_after = next_after
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
            fire = self.next_after(max(datetime.now(), last))
            await asyncio.sleep(max(0.0, (fire - datetime.now()).total_seconds()))
            last = fire
            try:
                await self.job(fire)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[random_pic] 定时推送出错")


def schedule_of(task: PushTask) -> Callable[[datetime], datetime]:
    if task.times:
        return lambda after: next_daily(after, task.times)
    return lambda after: next_fire(after, task.interval_minutes, task.start)


class Pusher:
    """所有推送任务。不允许在群聊使用时不推送到群；推送不受群白名单、冷却和每日上限限制，
    但仍受分级闸门约束；被拒绝、抽图失败或发送失败时只记日志，不在会话里发消息。"""

    def __init__(
        self,
        tasks: list[PushTask],
        context,
        drawer: Drawer,
        access: AccessControl,
        dispatcher: Dispatcher,
        works: WorkService,
        pdf: PdfStore,
        make_request: Callable[[list[str]], DrawRequest],
    ):
        """make_request(参数) 按 /抽图 的规则把推送内容解析成请求。"""
        self.context = context
        self.drawer = drawer
        self.access = access
        self.dispatcher = dispatcher
        self.works = works
        self.pdf = pdf
        self.make_request = make_request
        # (推送目标, 推送内容, 调度器)
        self.jobs = []
        allow_groups = access.conf.group_enabled
        if not allow_groups and any(task.groups for task in tasks):
            logger.info("[random_pic] 不允许在群聊使用，推送任务里的群不推送")
        for task in tasks:
            groups = task.groups if allow_groups else []
            targets = parse_targets(groups, task.users)
            if not targets:
                continue

            async def job(fire: datetime, targets=targets, content=task.content):
                await self.push_all(targets, content, fire)

            self.jobs.append(
                (targets, task.content, PushScheduler(schedule_of(task), job))
            )

    def start(self):
        for _, _, scheduler in self.jobs:
            scheduler.start()

    def stop(self):
        for _, _, scheduler in self.jobs:
            scheduler.stop()

    async def push_all(
        self, targets: list[Target], content: str, fire: datetime | None = None
    ):
        """把推送内容依次推送到每个目标。fire 是这次的触发时刻。"""
        when = (fire or datetime.now()).strftime("%H:%M")
        platform = self._platform()
        if platform is None:
            logger.warning("[random_pic] 定时推送：没有找到 QQ（aiocqhttp）平台适配器")
            return
        for target in targets:
            try:
                await self._push(platform, target, content, when)
            except Exception:
                logger.exception(f"[random_pic] 定时推送到{target}出错")

    def _platform(self):
        """QQ（aiocqhttp）平台适配器，有多个时用第一个。"""
        return next(
            (
                p
                for p in self.context.platform_manager.platform_insts
                if p.meta().name == ONEBOT
            ),
            None,
        )

    async def _push(self, platform, target: Target, content: str, when: str):
        umo = target.umo(platform.meta().id)
        is_private = not target.is_group
        req = self.make_request(content.split())
        denied = self.access.gate(req.rating, is_private)
        if denied:
            logger.warning(f"[random_pic] 定时推送到{target}被拒绝：{denied}")
            return
        bot = getattr(platform, "bot", None)
        group_id = target.chat_id if target.is_group else ""

        async def send(chain: list) -> str | None:
            if bot is not None:
                delivered, message_id = await send_onebot(
                    bot, chain, group_id, target.chat_id
                )
                if delivered:
                    return message_id
            try:
                await self.context.send_message(umo, MessageChain(chain))
            except Exception as e:
                logger.warning(f"[random_pic] 定时推送到{target}发送失败: {e!r}")
                raise SendFailed(str(e)) from e
            return None

        if req.whole:
            failed, total = await self._push_work(target, umo, req, bot, send, when)
        else:
            result = await self.drawer.draw(
                req, self.access.explicit_allowed(is_private)
            )
            if not result.albums:
                logger.warning(
                    f"[random_pic] 定时推送到{target}抽图失败 {req}: {result.reason()}"
                )
                return
            await self._announce(
                send,
                f"{push_title(when)}：{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}，"
                f"{len(result.albums)} 个图集共 {result.images} 张。",
            )
            failed, total = await self.dispatcher.deliver(
                result.albums, umo, ONEBOT, await self._self_id(bot), send
            )
        if failed:
            logger.warning(
                f"[random_pic] 定时推送到{target}：{failed}/{total} 条消息发送失败"
            )

    async def _push_work(
        self, target: Target, umo: str, req: DrawRequest, bot, send: Send, when: str
    ) -> tuple[int, int]:
        """随机完整作品：抽一个图集，先发作品信息，再推送整个作品（合并转发或 PDF）。

        返回 (失败条数, 总条数)。
        """
        picked = await self.works.random(req, not target.is_group)
        if isinstance(picked, str):
            logger.warning(f"[random_pic] 定时推送到{target}：{picked}")
            return 0, 0
        album, source, work = picked
        if req.whole == PDF_FORMAT:
            parts = self.pdf.parts(work.pages)
            how = "PDF" + (f"，分 {parts} 个文件" if parts > 1 else "")
        else:
            parts = -(-work.pages // FORWARD_MAX_NODES)
            how = "合并转发" + (f"，分 {parts} 条" if parts > 1 else "")
        await self._announce(
            send, f"{push_title(when)}：完整作品\n{describe(source, work, how)}"
        )
        if req.whole == PDF_FORMAT:
            sent = self.dispatcher.record(
                umo, [self.works.whole_album(source, work, [], album)]
            )
            files, missing = await self.pdf.get(
                work, f"push:{umo}", source.download_work
            )
            if missing:
                logger.warning(
                    f"[random_pic] 定时推送《{work.title}》有 {missing} 页下载失败"
                )
            messages = [
                ([Comp.File(name=name, file=str(path.resolve()))], [])
                for path, name in files
            ]
            return await self.dispatcher.send_all(messages, sent, send), len(messages)

        async with self.works.download(source, work) as (paths, missing):
            if not paths:
                logger.warning(
                    f"[random_pic] 定时推送《{work.title}》没有下载到任何图片"
                )
                return 0, 0
            if missing:
                logger.warning(
                    f"[random_pic] 定时推送《{work.title}》有 {missing} 页下载失败"
                )
            whole = self.works.whole_album(source, work, paths, album)
            sent = self.dispatcher.record(umo, [whole])
            composer = self.dispatcher.composer
            messages = composer.forward_album(whole, await self._self_id(bot))
            return await self.dispatcher.send_all(messages, sent, send), len(messages)

    async def _announce(self, send: Send, text: str) -> None:
        """推送前单独发一条说明；发不出去时照常推送（发送失败已记日志）。"""
        try:
            await send([Comp.Plain(text)])
        except SendFailed:
            return
        await asyncio.sleep(SEND_INTERVAL)

    async def _self_id(self, bot) -> str:
        """机器人自己的 QQ 号，用作合并转发节点的发送者；拿不到时用 0。"""
        if bot is None:
            return "0"
        try:
            info = await bot.call_action("get_login_info")
            return str(info["user_id"])
        except Exception as e:
            logger.warning(f"[random_pic] 获取机器人 QQ 号失败: {e!r}")
            return "0"
