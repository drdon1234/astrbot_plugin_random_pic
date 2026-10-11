"""按当前配置生成命令帮助。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import RATING_NAMES, STYLE_NAMES
from .parser import VIDEO_COMMAND

if TYPE_CHECKING:
    from ..runtime import Runtime


def help_text(runtime: Runtime) -> str:
    s = runtime.settings
    draw, access = s.draw, s.access
    on = {True: "允许", False: "不允许"}
    lines = [
        "【抽图用法】",
        "/抽图 [二次元|三次元] [擦边|r18] [关键词...] [图集数 或 图集数x每集张数]",
        f"· 参数顺序随意；不写时默认 {STYLE_NAMES[draw.style]}·"
        f"{RATING_NAMES[draw.rating]}、{draw.album_count} 个图集、每集 "
        f"{draw.images_per_album} 张，每次最多 {draw.max_images} 张",
        "· 网站图集来自同一作品；订阅可跨帖抽齐。例如 /抽图 3x4 抽 3 集、每集 4 张",
        "· 关键词可写中文角色、作品名（如 芙莉莲、原神）或标签，前缀 - 表示排除",
        "/随机角色 [同上]：每个图集先随机抽一个角色",
    ]
    if s.whole.enabled:
        lines += [
            f"/全集：回复抽到的图片，获取整个作品（{s.whole.default_format}）；"
            "/pdf：同上，打包成 PDF",
            "· 回复的消息里有多个图集时加序号（【】里的数字），如 /全集 2；也可以 /全集 <作品链接>",
            "· /抽图 全集 [参数]：直接随机抽一个完整作品，写 pdf 则打包成 PDF",
        ]
    if access.aliases:
        lines.append("别名：/二次元 /三次元 /擦边 /色图")
    lines.append(f"R18：群聊{on[access.group_r18]}，私聊{on[access.private_r18]}")
    lines += runtime.sources.help_lines()
    if s.sources.feeds.enabled and runtime.feeds.feeds:
        example = (
            runtime.feeds.feeds[0].conf.name or runtime.feeds.feeds[0].conf.account
        )
        lines.append(f"· 关键词写订阅的账号或名称时只抽这个订阅，例如 /抽图 {example}")
    if s.video.enabled and runtime.videos.drawing:
        lines += [
            f"/{VIDEO_COMMAND} [二次元|三次元] [擦边|r18] [关键词...] [个数]：随机短视频（实验性），"
            f"默认 {s.video.default_count} 个、最多 {s.video.max_count} 个",
            "视频源：",
            *runtime.videos.help_lines(),
        ]
    lines += [
        "示例：/抽图 原神 2　/抽图 二次元 芙莉莲　/抽图 全集 原神",
        "/抽图 帮助：显示本说明",
    ]
    return "\n".join(lines)
