"""AstrBot 插件入口：注册命令并委托运行时和处理器。"""

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.commands.images import ImageCommands
from .core.commands.parser import DRAW_COMMANDS, VIDEO_COMMAND, WHOLE_COMMANDS
from .core.commands.reserve import REFRESH_COMMAND, ReserveCommands
from .core.commands.videos import VideoCommands
from .core.commands.works import WorkCommands
from .core.config.migration import migrate_config
from .core.runtime import Runtime

# 官方加载器在模块导入完成后才按 schema 清理配置，先迁移以保全旧字段。
migrate_config()

# 群聊不带唤醒前缀时，完整指令词及其参数也可触发。
NO_PREFIX_RE = (
    rf"(?i)^\s*({'|'.join([*DRAW_COMMANDS, *WHOLE_COMMANDS, VIDEO_COMMAND])})(?:\s|$)"
)


class RandomPicPlugin(Star):
    """随机抽图：只负责 AstrBot 命令注册和生命周期委托。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.runtime = Runtime(context, config)
        self.works = WorkCommands(self.runtime)
        self.images = ImageCommands(self.runtime, self.works)
        self.videos = VideoCommands(self.runtime)
        self.reserve_commands = ReserveCommands(self.runtime)

    async def initialize(self):
        await self.runtime.initialize()

    async def terminate(self):
        await self.runtime.terminate()

    @filter.command("抽图")
    async def draw_pic(self, event: AstrMessageEvent):
        """随机抽图。用法：/抽图 [二次元|三次元] [擦边|r18] [关键词...] [数量]，/抽图 帮助 查看说明"""
        async for result in self.images.draw(event, "抽图"):
            yield result

    @filter.command("随机角色")
    async def random_character(self, event: AstrMessageEvent):
        """每个图集先随机抽一个角色再抽图。用法同 /抽图"""
        async for result in self.images.draw(event, "随机角色"):
            yield result

    @filter.command("二次元")
    async def alias_anime(self, event: AstrMessageEvent):
        """随机二次元图，等同于 /抽图 二次元"""
        async for result in self.images.draw(event, "二次元"):
            yield result

    @filter.command("三次元")
    async def alias_real(self, event: AstrMessageEvent):
        """随机三次元图，等同于 /抽图 三次元"""
        async for result in self.images.draw(event, "三次元"):
            yield result

    @filter.command("擦边")
    async def alias_sensitive(self, event: AstrMessageEvent):
        """随机擦边图，等同于 /抽图 擦边"""
        async for result in self.images.draw(event, "擦边"):
            yield result

    @filter.command("色图")
    async def alias_explicit(self, event: AstrMessageEvent):
        """随机 R18 图（仅私聊），等同于 /抽图 r18"""
        async for result in self.images.draw(event, "色图"):
            yield result

    @filter.command("全集")
    async def whole_work(self, event: AstrMessageEvent):
        """回复抽到的图片，获取整个作品（默认用合并转发）。也可以 /全集 <作品链接>"""
        async for result in self.works.whole(event, "全集"):
            yield result

    @filter.command("pdf")
    async def whole_pdf(self, event: AstrMessageEvent):
        """回复抽到的图片，获取整个作品的 PDF。也可以 /pdf <作品链接>"""
        async for result in self.works.whole(event, "pdf"):
            yield result

    @filter.command(VIDEO_COMMAND)
    async def draw_video(self, event: AstrMessageEvent):
        """随机抽短视频（实验性）。用法：/抽视频 [二次元|三次元] [擦边|r18] [关键词...] [个数]"""
        async for result in self.videos.draw(event):
            yield result

    @filter.command(REFRESH_COMMAND, alias={"刷新预选池"})
    async def refresh_reserve(self, event: AstrMessageEvent):
        """管理员私聊：丢掉预备池里抽好的图和视频，按当前配置重新抽。可加 图片 或 视频 只刷新一种"""
        async for result in self.reserve_commands.refresh(event):
            yield result

    @filter.regex(NO_PREFIX_RE)
    async def no_prefix(self, event: AstrMessageEvent):
        """群聊里不带 / 也能触发本插件的指令。"""
        # 带了唤醒前缀、@ 了机器人或私聊时，已经由上面的指令处理
        if getattr(event, "is_at_or_wake_command", False):
            return
        if not self.runtime.settings.access.no_prefix:
            return
        word = event.message_str.split()[0].lower()
        if word == VIDEO_COMMAND:
            handler = self.videos.draw(event)
        elif word in WHOLE_COMMANDS:
            handler = self.works.whole(event, word)
        else:
            handler = self.images.draw(event, word)
        async for result in handler:
            yield result
