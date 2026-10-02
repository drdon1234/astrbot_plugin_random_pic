"""定时推送：从每天 0 点起按固定间隔触发（默认每个整点），每个群、每个用户单独抽一次，
通过 QQ（aiocqhttp）主动推送。"""

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain

from .access import AccessControl
from .drawer import Drawer
from .models import EXPLICIT, RATING_NAMES, STYLE_NAMES, Album, DrawRequest, Work
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
from .settings import Push
from .sources import SourceSet
from .sources.base import Source

GROUP = "GroupMessage"
FRIEND = "FriendMessage"
# 随机完整图集：抽到的作品不能推送（被过滤、分级不符）时最多抽这么多次
WORK_ATTEMPTS = 3


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
    return f"【定时推送】这是抽图插件 {when} 的定时推送"


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


class Pusher:
    """推送任务。推送不受群白名单、冷却和每日上限限制，但仍受分级闸门约束；
    被拒绝、抽图失败或发送失败时只记日志，不在会话里发消息。"""

    def __init__(
        self,
        conf: Push,
        targets: list[Target],
        context,
        drawer: Drawer,
        access: AccessControl,
        dispatcher: Dispatcher,
        sources: SourceSet,
        pdf: PdfStore,
        make_request: Callable[[], DrawRequest],
        tmp_dir: Path,
    ):
        """make_request 生成和不带参数的 /抽图 相同的请求。"""
        self.conf = conf
        self.targets = targets
        self.context = context
        self.drawer = drawer
        self.access = access
        self.dispatcher = dispatcher
        self.sources = sources
        self.pdf = pdf
        self.make_request = make_request
        self.tmp = tmp_dir
        self.scheduler = PushScheduler(conf.interval_minutes, self.push_all)

    def start(self):
        self.scheduler.start()

    def stop(self):
        self.scheduler.stop()

    async def push_all(self, fire: datetime | None = None):
        """每个群、每个用户单独抽一次，依次推送。fire 是这次的触发时刻。"""
        when = (fire or datetime.now()).strftime("%H:%M")
        platform = self._platform()
        if platform is None:
            logger.warning("[random_pic] 定时推送：没有找到 QQ（aiocqhttp）平台适配器")
            return
        for target in self.targets:
            try:
                await self._push(platform, target, when)
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

    async def _push(self, platform, target: Target, when: str):
        umo = target.umo(platform.meta().id)
        is_private = not target.is_group
        req = self.make_request()
        if self.conf.full_album:
            req.albums = req.per_album = 1
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

        if self.conf.full_album:
            failed, total = await self._push_work(target, umo, req, bot, send, when)
        else:
            result = await self.drawer.draw(req, is_private)
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
        """随机完整图集：抽一个图集，先发作品信息，再推送作品的全部图片（合并转发或 PDF）。

        返回 (失败条数, 总条数)。
        """
        picked = await self._pick_work(target, req)
        if picked is None:
            return 0, 0
        album, source, work = picked
        sent = self.dispatcher.record(umo, [replace(album, work=work.ref)])
        await self._announce(send, self._work_notice(when, album, work))
        if self.conf.as_pdf:
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

        tmp = self.tmp / work.ref.key
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            paths, missing = await source.download_work(work, tmp)
            if not paths:
                logger.warning(
                    f"[random_pic] 定时推送《{work.title}》没有下载到任何图片"
                )
                return 0, 0
            if missing:
                logger.warning(
                    f"[random_pic] 定时推送《{work.title}》有 {missing} 页下载失败"
                )
            # 下载的图片以页码命名
            whole = replace(
                album,
                total=work.pages,
                pictures=[(int(path.stem), path) for path in paths],
            )
            composer = self.dispatcher.composer
            messages = composer.forward_album(whole, await self._self_id(bot))
            return await self.dispatcher.send_all(messages, sent, send), len(messages)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _announce(self, send: Send, text: str) -> None:
        """推送前单独发一条说明；发不出去时照常推送（发送失败已记日志）。"""
        try:
            await send([Comp.Plain(text)])
        except SendFailed:
            return
        await asyncio.sleep(SEND_INTERVAL)

    def _work_notice(self, when: str, album: Album, work: Work) -> str:
        """随机完整图集的推送说明：作品的全部信息和发送方式。"""
        if self.conf.as_pdf:
            parts = self.pdf.parts(work.pages)
            how = "PDF" + (f"，分 {parts} 个文件" if parts > 1 else "")
        else:
            parts = -(-work.pages // FORWARD_MAX_NODES)
            how = "合并转发" + (f"，分 {parts} 条" if parts > 1 else "")
        lines = [
            f"{push_title(when)}：随机完整图集",
            f"标题：{work.title or album.title or '无标题'}",
            f"来源：{album.source} · {RATING_NAMES[work.rating or EXPLICIT]}",
            f"页数：{work.pages} 页",
            f"发送：{how}",
            *album.details,
        ]
        return "\n".join(lines)

    async def _pick_work(
        self, target: Target, req: DrawRequest
    ) -> tuple[Album, Source, Work] | None:
        """抽一个图集并查询它所在的作品，返回 (图集, 图源, 作品)。

        作品查不到或不能推送（被过滤、分级不符）时重抽，最多 WORK_ATTEMPTS 次；都不行时返回 None。
        """
        is_private = not target.is_group
        for _ in range(WORK_ATTEMPTS):
            result = await self.drawer.draw(req, is_private)
            if not result.albums:
                logger.warning(
                    f"[random_pic] 定时推送到{target}抽图失败 {req}: {result.reason()}"
                )
                return None
            album = result.albums[0]
            source, why = (
                self.sources.find(album.work.source)
                if album.work
                else (None, "没有作品信息")
            )
            if source is None:
                logger.warning(f"[random_pic] 定时推送：《{album.title}》{why}，重抽")
                continue
            work = await source.work(album.work)
            denied = (
                "作品不存在或已被删除"
                if work is None
                else self.access.work_gate(work, is_private)
            )
            if denied:
                logger.warning(
                    f"[random_pic] 定时推送：《{album.title}》{denied}，重抽"
                )
                continue
            return album, source, work
        logger.warning(
            f"[random_pic] 定时推送到{target}：连续 {WORK_ATTEMPTS} 次没有抽到可推送的完整图集"
        )
        return None

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
