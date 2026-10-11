"""按当前配置生成命令帮助。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import RATING_NAMES, STYLE_NAMES

if TYPE_CHECKING:
    from ..runtime import Runtime


def help_text(runtime: Runtime, *, is_admin: bool = False) -> str:
    s = runtime.settings
    draw, access = s.draw, s.access
    commands = runtime.commands
    command = commands.command

    def entries(action: str) -> str:
        return " ".join("/" + word for word in commands.words(action))

    on = {True: "允许", False: "不允许"}
    lines = ["【抽图用法】"]
    if commands.enabled("抽图"):
        lines.append(
            f"{entries('抽图')} [二次元|三次元] [擦边|r18] [关键词...] [图集数 或 图集数x每集张数]"
        )
    lines += [
        f"· 参数顺序随意；不写时默认 {STYLE_NAMES[draw.style]}·"
        f"{RATING_NAMES[draw.rating]}、{draw.album_count} 个图集、每集 "
        f"{draw.images_per_album} 张，每次最多 {draw.max_images} 张",
        "· 网站图集来自同一作品；订阅可跨帖抽齐。数量 3x4 表示请求 3 集、每集 4 张，超限时先减每集张数",
        "· 关键词可写中文角色、作品名（如 芙莉莲、原神）或标签，前缀 - 表示排除",
    ]
    if commands.enabled("随机角色"):
        lines.append(f"{entries('随机角色')} [同上]：每个图集先随机抽一个角色")
    if s.whole.enabled:
        for action, description in [
            ("全集", f"获取整个作品（{s.whole.default_format}）"),
            ("pdf", "打包成 PDF"),
        ]:
            if commands.enabled(action):
                lines.append(f"{entries(action)}：回复抽到的图片，{description}")
        work_action = next(
            (action for action in ("全集", "pdf") if commands.enabled(action)), None
        )
        if work_action:
            lines.append(
                f"· 回复的消息里有多个图集时加序号（【】里的数字），如 {command(work_action, '2')}；也可以 {command(work_action, '<作品链接>')}"
            )
        if commands.enabled("抽图"):
            lines.append(
                f"· {command('抽图', '全集 [参数]')}：直接随机抽一个完整作品，参数写 pdf 则打包成 PDF"
            )
    if access.aliases:
        shortcuts = [
            f"{entries(action)}（{action}）"
            for action in ("二次元", "三次元", "擦边", "色图")
            if commands.enabled(action)
        ]
        if shortcuts:
            lines.append("快捷入口：" + "；".join(shortcuts))
    lines.append(f"R18：群聊{on[access.group_r18]}，私聊{on[access.private_r18]}")
    if access.cooldown_seconds or access.daily_limit:
        limits = []
        if access.cooldown_seconds:
            limits.append(f"冷却 {access.cooldown_seconds} 秒")
        if access.daily_limit:
            limits.append(f"每日 {access.daily_limit} 张")
        lines.append(
            "使用限制：" + "，".join(limits)
            + "；图片、视频和完整作品共用，视频计 1 张，整本按成功发送页数计。"
        )
    lines += runtime.sources.help_lines()
    if commands.enabled("抽图") and s.sources.feeds.enabled and runtime.feeds.feeds:
        example = (
            runtime.feeds.feeds[0].conf.name or runtime.feeds.feeds[0].conf.account
        )
        lines.append(
            f"· 关键词写订阅的账号或名称时只抽这个订阅，例如 {command('抽图', example)}"
        )
    if commands.enabled("抽视频") and s.video.enabled and runtime.videos.drawing:
        lines += [
            f"{entries('抽视频')} [二次元|三次元] [擦边|r18] [关键词...] [个数]：随机短视频（实验性），"
            f"默认 {s.video.default_count} 个、最多 {s.video.max_count} 个",
            "视频源：",
            *runtime.videos.help_lines(),
        ]
    if commands.enabled("抽图"):
        examples = [command("抽图", "原神 2"), command("抽图", "二次元 芙莉莲")]
        if s.whole.enabled:
            examples.append(command("抽图", "全集 原神"))
        lines.append("示例：" + "　".join(examples))
    if commands.enabled("帮助"):
        lines.append(f"独立帮助：{entries('帮助')}")
    lines += [
        f"在上述任意指令参数中加入 {' / '.join(commands.help_words)} 可查看帮助，不执行操作。",
        "群聊无前缀触发："
        + (
            "关闭（群聊功能未开启）。"
            if not access.group_enabled
            else "开启，完整触发词后用空格分隔参数。"
            if access.no_prefix
            else "关闭，请使用机器人唤醒前缀或 @ 机器人。"
        ),
        "上面 / 为唤醒前缀示例；实际使用 AstrBot 配置的唤醒前缀。",
    ]
    if is_admin and commands.enabled("刷新预备池"):
        lines.append(
            f"管理员私聊：{entries('刷新预备池')} [图片|视频]，不写范围则刷新全部预备池。"
        )
    return "\n".join(lines)
