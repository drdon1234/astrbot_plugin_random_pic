"""从被回复的消息中找出画廊：/pdf 用。

被回复的消息拆成若干「单元」：普通消息是一个单元，合并转发的每个节点各是一个单元。
每个单元先看文字里的画廊链接（带说明文字时），再按图片字节数查已发送登记表（纯图片时）。
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .registry import GalleryRef, SentRegistry

GALLERY_URL_RE = re.compile(
    r"https?://(?:e-hentai|exhentai)\.org/g/(\d+)/([0-9a-f]{10})"
)
# NapCat 配置为字符串（CQ 码）消息格式时
CQ_FILE_SIZE_RE = re.compile(r"\[CQ:image,[^\]]*?file_size=(\d+)")
MAX_FORWARD_DEPTH = 3

GetForward = Callable[[str], Awaitable[list]]


@dataclass
class Unit:
    text: str = ""
    sizes: list[int] = field(default_factory=list)


def gallery_from_text(text: str) -> GalleryRef | None:
    match = GALLERY_URL_RE.search(text or "")
    return GalleryRef(int(match.group(1)), match.group(2)) if match else None


async def message_units(
    message, get_forward: GetForward | None, depth: int = 0
) -> list[Unit]:
    """把 OneBot 消息（消息段数组或 CQ 码字符串）拆成单元。"""
    if isinstance(message, str):
        sizes = [int(s) for s in CQ_FILE_SIZE_RE.findall(message)]
        return [Unit(message, sizes)]
    own = Unit()
    nested: list[Unit] = []
    for seg in message or []:
        kind, data = seg.get("type"), seg.get("data") or {}
        if kind == "text":
            own.text += str(data.get("text", ""))
        elif kind == "image" and str(data.get("file_size", "")).isdigit():
            own.sizes.append(int(data["file_size"]))
        elif kind == "forward" and depth < MAX_FORWARD_DEPTH:
            nodes = data.get("content")
            if not nodes and get_forward and data.get("id"):
                nodes = await get_forward(str(data["id"]))
            for node in nodes or []:
                body = node.get("message") or node.get("content") or []
                nested.extend(await message_units(body, get_forward, depth + 1))
    return ([own] if own.text or own.sizes else []) + nested


def resolve_units(units: list[Unit], registry: SentRegistry) -> list[GalleryRef | None]:
    refs = []
    for unit in units:
        ref = gallery_from_text(unit.text)
        if ref is None:
            ref = next(
                (r for r in map(registry.by_size, unit.sizes) if r is not None), None
            )
        refs.append(ref)
    return refs


def pick(
    refs: list[GalleryRef | None], index: int | None
) -> tuple[GalleryRef | None, str]:
    """从候选里选出一个画廊；选不出时返回提示文字。"""
    if index is not None:
        if 1 <= index <= len(refs) and refs[index - 1]:
            return refs[index - 1], ""
        return None, f"序号 {index} 没有对应的画廊（共 {len(refs)} 项）。"
    found = [r for r in refs if r]
    unique = {r.gid: r for r in found}
    if not unique:
        return None, "没有识别到画廊：请回复抽图发出的图片，或使用 /pdf <画廊链接>。"
    if len(unique) == 1:
        # 优先返回带标题的那条（来自登记表）
        return max(found, key=lambda r: bool(r.title)), ""
    lines = ["包含多个画廊，请加序号，例如 /pdf 2："]
    for i, ref in enumerate(refs, 1):
        if ref:
            lines.append(f"{i}. {ref.title or ref.gid}")
    return None, "\n".join(lines)
