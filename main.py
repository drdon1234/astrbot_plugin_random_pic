"""AstrBot 插件入口：注册命令并委托运行时和处理器。"""

from contextlib import aclosing
from functools import partial

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.regex import RegexFilter
from astrbot.core.star.star_handler import star_handlers_registry

from .core.commands.catalog import default_words
from .core.commands.common import event_args
from .core.commands.help import help_text
from .core.commands.images import ImageCommands
from .core.commands.parser import ALIASES, VIDEO_COMMAND, WHOLE_COMMANDS
from .core.commands.reserve import REFRESH_COMMAND, ReserveCommands
from .core.commands.videos import VideoCommands
from .core.commands.works import WorkCommands
from .core.config.migration import migrate_config
from .core.runtime import Runtime

# 官方加载器在模块导入完成后才按 schema 清理配置，先迁移以保全旧字段。
migrate_config()


def _bound_runtime(metadata):
    """AstrBot 加载器用 partial(原函数, 当前插件实例) 绑定处理器。"""
    handler = metadata.handler
    owner = handler.args[0] if isinstance(handler, partial) and handler.args else None
    return getattr(owner, "runtime", None)


class CatalogCommandFilter(CommandFilter):
    """随框架当前绑定实例读取触发词，命令枚举与匹配不缓存配置。"""

    def __init__(self, action, metadata):
        self.action = action
        words = default_words(action)
        super().__init__(words[0], set(words[1:]), metadata)

    def _words(self):
        metadata = getattr(self, "handler_md", None)
        runtime = _bound_runtime(metadata) if metadata else None
        return (
            runtime.commands.words(self.action)
            if runtime
            else default_words(self.action)
        )

    @property
    def command_name(self):
        return self._words()[0]

    @command_name.setter
    def command_name(self, value):
        metadata = getattr(self, "handler_md", None)
        runtime = _bound_runtime(metadata) if metadata else None
        if runtime is not None:
            runtime.commands.override(self.action, name=value)

    @property
    def alias(self):
        return set(self._words()[1:])

    @alias.setter
    def alias(self, value):
        metadata = getattr(self, "handler_md", None)
        runtime = _bound_runtime(metadata) if metadata else None
        if runtime is not None:
            runtime.commands.override(self.action, aliases=value)

    def get_complete_command_names(self):
        return list(self._words())

    def equals(self, message_str):
        return message_str.casefold() in {word.casefold() for word in self._words()}

    def filter(self, event, cfg):
        runtime = _bound_runtime(self.handler_md)
        if (
            runtime is None
            or not event.is_at_or_wake_command
            or not getattr(self.handler_md, "enabled", True)
        ):
            return False
        match = runtime.commands.resolve(event.message_str)
        if (
            match is None
            or match[0] != self.action
            or not self.custom_filter_ok(event, cfg)
        ):
            return False
        event.set_extra("parsed_params", {})
        return True


class CatalogNoPrefixFilter(RegexFilter):
    """在唤醒阶段精确判定，不能先用全匹配正则唤醒普通聊天。"""

    def __init__(self, metadata):
        super().__init__(r"(?!)")
        self.handler_md = metadata

    def filter(self, event, cfg):
        runtime = _bound_runtime(self.handler_md)
        if runtime is None or event.is_at_or_wake_command or event.is_private_chat():
            return False
        if not runtime.settings.access.no_prefix:
            return False
        if not runtime.access.allowed(
            str(event.get_sender_id()), str(event.get_group_id() or "")
        ):
            return False
        match = runtime.commands.resolve(event.message_str)
        if match is None or match[0] == REFRESH_COMMAND:
            return False
        target = runtime.commands.handler(match[0])
        if target is not None:
            for item in target.event_filters:
                if isinstance(item, CommandFilter):
                    if not item.custom_filter_ok(event, cfg):
                        return False
                elif not item.filter(event, cfg):
                    return False
        return not (
            match[0] in ALIASES
            and not runtime.settings.access.aliases
            and not runtime.commands.is_help(match[1])
        )


def configured_command(action):
    """沿用框架命令注册，只替换由本插件拥有的匹配器。"""

    def decorate(handler):
        words = default_words(action)
        filter.command(words[0], alias=set(words[1:]))(handler)
        metadata = star_handlers_registry.get_handler_by_full_name(
            f"{handler.__module__}_{handler.__name__}"
        )
        metadata.event_filters = [
            CatalogCommandFilter(action, metadata)
            if isinstance(item, CommandFilter)
            else item
            for item in metadata.event_filters
        ]
        handler._random_pic_action = action
        return handler

    return decorate


def configured_no_prefix(handler):
    filter.regex(r"(?!)")(handler)
    metadata = star_handlers_registry.get_handler_by_full_name(
        f"{handler.__module__}_{handler.__name__}"
    )
    metadata.event_filters = [
        CatalogNoPrefixFilter(metadata) if isinstance(item, RegexFilter) else item
        for item in metadata.event_filters
    ]
    return handler


class RandomPicPlugin(Star):
    """随机抽图：只负责 AstrBot 命令注册和生命周期委托。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.runtime = Runtime(context, config)
        self.works = WorkCommands(self.runtime)
        self.images = ImageCommands(self.runtime, self.works)
        self.videos = VideoCommands(self.runtime)
        self.reserve_commands = ReserveCommands(self.runtime)
        for handler in vars(type(self)).values():
            action = getattr(handler, "_random_pic_action", None)
            if action is not None:
                metadata = star_handlers_registry.get_handler_by_full_name(
                    f"{handler.__module__}_{handler.__name__}"
                )
                self.runtime.commands.bind(action, metadata)

    async def initialize(self):
        await self.runtime.initialize()

    async def terminate(self):
        await self.runtime.terminate()

    @configured_command("抽图")
    async def draw_pic(self, event: AstrMessageEvent):
        """随机抽图，可指定风格、分级、关键词和数量；在参数中加入帮助词查看用法。"""
        async with aclosing(self.images.draw(event, "抽图")) as stream:
            async for result in stream:
                yield result

    @configured_command("随机角色")
    async def random_character(self, event: AstrMessageEvent):
        """每个图集先随机抽一个角色再抽图，参数与抽图相同。"""
        async with aclosing(self.images.draw(event, "随机角色")) as stream:
            async for result in stream:
                yield result

    @configured_command("二次元")
    async def alias_anime(self, event: AstrMessageEvent):
        """随机二次元图，其他参数与抽图相同。"""
        async with aclosing(self.images.draw(event, "二次元")) as stream:
            async for result in stream:
                yield result

    @configured_command("三次元")
    async def alias_real(self, event: AstrMessageEvent):
        """随机三次元图，其他参数与抽图相同。"""
        async with aclosing(self.images.draw(event, "三次元")) as stream:
            async for result in stream:
                yield result

    @configured_command("擦边")
    async def alias_sensitive(self, event: AstrMessageEvent):
        """随机擦边图，其他参数与抽图相同。"""
        async with aclosing(self.images.draw(event, "擦边")) as stream:
            async for result in stream:
                yield result

    @configured_command("色图")
    async def alias_explicit(self, event: AstrMessageEvent):
        """随机 R18 图，受当前会话的 R18 权限限制。"""
        async with aclosing(self.images.draw(event, "色图")) as stream:
            async for result in stream:
                yield result

    @configured_command("全集")
    async def whole_work(self, event: AstrMessageEvent):
        """回复抽到的图片或提供作品链接，获取整个作品（默认用合并转发）。"""
        async with aclosing(self.works.whole(event, "全集")) as stream:
            async for result in stream:
                yield result

    @configured_command("pdf")
    async def whole_pdf(self, event: AstrMessageEvent):
        """回复抽到的图片或提供作品链接，获取整个作品的 PDF。"""
        async with aclosing(self.works.whole(event, "pdf")) as stream:
            async for result in stream:
                yield result

    @configured_command(VIDEO_COMMAND)
    async def draw_video(self, event: AstrMessageEvent):
        """随机抽短视频（实验性），可指定风格、分级、关键词和个数。"""
        async with aclosing(self.videos.draw(event)) as stream:
            async for result in stream:
                yield result

    @configured_command(REFRESH_COMMAND)
    async def refresh_reserve(self, event: AstrMessageEvent):
        """管理员私聊：丢掉预备池里抽好的图和视频，按当前配置重新抽。可加 图片 或 视频 只刷新一种"""
        async with aclosing(self.reserve_commands.refresh(event)) as stream:
            async for result in stream:
                yield result

    @configured_command("帮助")
    async def show_help(self, event: AstrMessageEvent):
        """查看当前有效触发词、参数、开关和操作示例。"""
        if event_args(self.runtime, event) is not None:
            yield event.plain_result(
                help_text(
                    self.runtime,
                    is_admin=self.runtime.access.is_admin(str(event.get_sender_id())),
                )
            )

    @configured_no_prefix
    async def no_prefix(self, event: AstrMessageEvent):
        """群聊里不带 / 也能触发本插件的指令。"""
        # 带了唤醒前缀、@ 了机器人或私聊时，已经由上面的指令处理
        if getattr(event, "is_at_or_wake_command", False) or event.is_private_chat():
            return
        if not self.runtime.settings.access.no_prefix:
            return
        match = self.runtime.commands.resolve(event.message_str)
        if match is None:
            return
        word, tokens = match
        if word == REFRESH_COMMAND:
            return
        if (
            word in ALIASES
            and not self.runtime.settings.access.aliases
            and not self.runtime.commands.is_help(tokens)
        ):
            return
        if word == VIDEO_COMMAND:
            handler = self.videos.draw(event)
        elif word == "帮助":
            handler = self.show_help(event)
        elif word in WHOLE_COMMANDS:
            handler = self.works.whole(event, word)
        else:
            handler = self.images.draw(event, word)
        async with aclosing(handler) as stream:
            async for result in stream:
                yield result
