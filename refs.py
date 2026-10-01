"""从被回复的消息中找出画廊：/pdf 用。

一次抽卡发出若干图集（同一画廊 / 帖子 / 本子的几张图），每个图集一个序号。QQ 上插件记下每条
发出消息的 ID 和其中的图集（见 registry），回复时先按消息 ID 查；查不到时才解析消息内容：

图集第一张图上方是「【序号】标题」和「第 x/y 张 · 来源」两行，同一图集后面的图上方只有
「第 x/y 张」。被回复的消息拆成若干「单元」，每个图集一个：合并转发的每个节点、每个【序号】行
各开始一个新单元。每个单元先看文字里的画廊链接，再按图片字节数、标题行查已发送登记表。
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from html import unescape

from .registry import GalleryRef, SentRegistry

GALLERY_URL_RE = re.compile(
    r"https?://(?:e-hentai|exhentai)\.org/g/(\d+)/([0-9a-f]{10})"
)
# 图集标题：「【序号】标题」换行「第 x/y 张 · 来源」
HEADER_RE = re.compile(r"(?m)^【(\d+)】(.*)\n第 (\d+)/(\d+) 张(?: · .*)?$")
# 同一图集后面几张图上方的页码行
PAGE_RE = re.compile(r"(?m)^第 \d+/\d+ 张(?: · .*)?$")
# 2.7.0 的标题行：序号-第几张/共几张-标题
LEGACY_HEADER_RE = re.compile(r"(?m)^(\d+)-(\d+/\d+-.*)$")
LABEL_RE = re.compile(r"(\d+)/(\d+)-(.*)")
# NapCat 配置为字符串（CQ 码）消息格式时
CQ_RE = re.compile(r"\[CQ:(\w+)((?:,[^\]]*)?)\]")
# NapCat 发出的合并转发在 get_msg 里是 multimsg 卡片（json 消息段）
MULTIMSG_APP = "com.tencent.multimsg"
MAX_FORWARD_DEPTH = 3
# 提示选序号时最多列出的条数
MAX_LISTED = 30

GetForward = Callable[[str], Awaitable[list]]


def clean_title(title: str) -> str:
    return " ".join(title.split()) or "无标题"


def image_label(page: int, pages: int, title: str) -> str:
    """登记表里按标题行查画廊的键：第几张/共几张-标题。"""
    return f"{page}/{pages}-{clean_title(title)}"


def header_text(idx: int, page: int, pages: int, title: str, source: str) -> str:
    """图集第一张图上方的两行标题。"""
    return f"【{idx}】{clean_title(title)}\n第 {page}/{pages} 张 · {source}"


def page_text(page: int, pages: int) -> str:
    """同一图集后面几张图上方的页码行。"""
    return f"第 {page}/{pages} 张"


def label_display(label: str) -> str:
    match = LABEL_RE.fullmatch(label)
    if not match:
        return label
    return f"{match.group(3)}（第 {match.group(1)}/{match.group(2)} 张）"


def find_marks(text: str) -> list[tuple[int, int | None, str]]:
    """文字里的标题行，返回 [(起始位置, 序号, 登记键)]，按位置排序。

    序号为 None 的是同一图集后面几张图的页码行。
    """
    found = []
    covered = []
    for m in HEADER_RE.finditer(text):
        label = image_label(int(m.group(3)), int(m.group(4)), m.group(2))
        found.append((m.start(), int(m.group(1)), label))
        covered.append((m.start(), m.end()))
    for m in PAGE_RE.finditer(text):
        if not any(a <= m.start() < b for a, b in covered):
            found.append((m.start(), None, ""))
    for m in LEGACY_HEADER_RE.finditer(text):
        found.append((m.start(), int(m.group(1)), m.group(2).strip()))
    return sorted(found, key=lambda f: f[0])


def multimsg_resid(raw) -> str | None:
    """json 消息段里合并转发卡片的 resid。"""
    try:
        card = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(card, dict) or card.get("app") != MULTIMSG_APP:
            return None
        return str(card["meta"]["detail"]["resid"]) or None
    except (ValueError, KeyError, TypeError):
        return None


@dataclass
class Unit:
    text: str = ""
    sizes: list[int] = field(default_factory=list)
    images: int = 0
    idx: int | None = None  # 标题行里的序号
    label: str = ""


@dataclass
class Candidate:
    idx: int
    label: str
    ref: GalleryRef | None


def gallery_from_text(text: str) -> GalleryRef | None:
    match = GALLERY_URL_RE.search(text or "")
    return GalleryRef(int(match.group(1)), match.group(2)) if match else None


def cq_segments(message: str) -> list[dict]:
    """把 CQ 码字符串转换成消息段数组。"""
    segments, pos = [], 0
    for match in CQ_RE.finditer(message):
        if match.start() > pos:
            segments.append(
                {"type": "text", "data": {"text": unescape(message[pos : match.start()])}}
            )
        data = {}
        for pair in match.group(2).split(",")[1:]:
            key, _, value = pair.partition("=")
            data[key] = unescape(value)
        segments.append({"type": match.group(1), "data": data})
        pos = match.end()
    if pos < len(message):
        segments.append({"type": "text", "data": {"text": unescape(message[pos:])}})
    return segments


class _Splitter:
    """按标题行和图片把一条消息切成单元。"""

    def __init__(self):
        self.units: list[Unit] = []
        self.current: Unit | None = None
        self.more = False  # 刚看到页码行：下一张图属于当前图集

    def _new(self) -> Unit:
        self.current = Unit()
        self.units.append(self.current)
        self.more = False
        return self.current

    def _append(self, text: str):
        if text:
            (self.current or self._new()).text += text

    def text(self, text: str):
        pos = 0
        for start, idx, label in find_marks(text):
            self._append(text[pos:start])
            if idx is None:
                self.more = True
            else:
                unit = self._new()
                unit.idx, unit.label = idx, label
            pos = start
        self._append(text[pos:])

    def image(self, size: int | None):
        # 没有标题行的消息：每张图开始一个新单元，图后面的文字是它的说明
        unit = self.current
        if unit is None or (unit.images and not self.more):
            unit = self._new()
        self.more = False
        unit.images += 1
        if size:
            unit.sizes.append(size)

    def nested(self, units: list[Unit]):
        self.units.extend(units)
        self.current = None
        self.more = False


def _merge(units: list[Unit]) -> Unit:
    merged = Unit()
    for unit in units:
        merged.text += unit.text
        merged.sizes += unit.sizes
        merged.images += unit.images
    return merged


async def _forward_units(nodes, get_forward, depth: int) -> list[Unit]:
    """合并转发的每个节点是一个图集；节点里没有标题行时整个节点算一个单元。"""
    units = []
    for node in nodes or []:
        body = node.get("message") or node.get("content") or []
        inner = await message_units(body, get_forward, depth + 1)
        if inner and all(u.idx is None for u in inner):
            inner = [_merge(inner)]
        units.extend(inner)
    return units


async def message_units(
    message, get_forward: GetForward | None, depth: int = 0
) -> list[Unit]:
    """把 OneBot 消息（消息段数组或 CQ 码字符串）拆成单元，每个图集一个。"""
    if isinstance(message, str):
        message = cq_segments(message)
    splitter = _Splitter()
    for seg in message or []:
        kind, data = seg.get("type"), seg.get("data") or {}
        if kind == "text":
            splitter.text(str(data.get("text", "")))
        elif kind == "image":
            size = str(data.get("file_size", ""))
            splitter.image(int(size) if size.isdigit() else None)
        elif kind in ("forward", "json") and depth < MAX_FORWARD_DEPTH:
            nodes, forward_id = None, None
            if kind == "forward":
                nodes, forward_id = data.get("content"), data.get("id")
            else:
                forward_id = multimsg_resid(data.get("data"))
            if not nodes and get_forward and forward_id:
                nodes = await get_forward(str(forward_id))
            splitter.nested(await _forward_units(nodes, get_forward, depth))
    return [
        u
        for u in splitter.units
        if u.images or u.idx is not None or gallery_from_text(u.text)
    ]


def resolve_units(units: list[Unit], registry: SentRegistry) -> list[Candidate]:
    candidates: dict[int, Candidate] = {}
    for position, unit in enumerate(units, 1):
        ref = gallery_from_text(unit.text)
        if ref is None:
            ref = next(
                (r for r in map(registry.by_size, unit.sizes) if r is not None), None
            )
        if ref is None and unit.label:
            ref = registry.by_label(unit.label)
        if ref is not None and not ref.title and unit.label:
            match = LABEL_RE.fullmatch(unit.label)
            ref = replace(ref, title=match.group(3) if match else "")
        idx = unit.idx if unit.idx is not None else position
        # 同一序号（图文混合分成几条发送时）只保留一个，优先识别到画廊的
        if idx not in candidates or (candidates[idx].ref is None and ref is not None):
            candidates[idx] = Candidate(idx, unit.label, ref)
    return list(candidates.values())


def message_candidates(albums: list[tuple[int, GalleryRef]]) -> list[Candidate]:
    return [Candidate(idx, ref.title, ref) for idx, ref in albums]


def last_candidates(refs: list[GalleryRef]) -> list[Candidate]:
    return [Candidate(i, ref.title, ref) for i, ref in enumerate(refs, 1)]


def _listing(candidates: list[Candidate], intro: str) -> str:
    lines = [intro]
    for c in candidates[:MAX_LISTED]:
        name = label_display(c.label) or (c.ref.title if c.ref else "") or "（无标题）"
        if c.ref is None:
            name += "（未识别）"
        elif not c.ref.gid:
            name += "（16K / 哔咔，不支持）"
        lines.append(f"{c.idx}. {name}")
    if len(candidates) > MAX_LISTED:
        lines.append(f"……共 {len(candidates)} 个图集")
    return "\n".join(lines)


def pick(
    candidates: list[Candidate], index: int | None, strict: bool
) -> tuple[GalleryRef | None, str]:
    """从候选图集里选出一个画廊；选不出时返回提示文字。

    strict（回复的是某条消息）时，消息里有多个图集就必须带序号，不带就列出来让用户选；
    否则（本会话上一次抽卡）所有图集都来自同一个画廊时可以不带序号。
    16K、哔咔的图集登记为 gid 0，由调用方提示不支持。
    """
    if not candidates:
        return None, "没有识别到画廊：请回复抽图发出的消息，或使用 /pdf <画廊链接>。"
    if index is not None:
        chosen = next((c for c in candidates if c.idx == index), None)
        if chosen is None:
            return None, _listing(
                candidates, f"没有序号 {index}，共 {len(candidates)} 个图集："
            )
        if chosen.ref is None:
            return None, f"第 {index} 个图集没有识别到画廊，请改用 /pdf <画廊链接>。"
        return chosen.ref, ""
    if len(candidates) == 1:
        ref = candidates[0].ref
        if ref is None:
            return None, "没有识别到这个图集的画廊，请改用 /pdf <画廊链接>。"
        return ref, ""
    example = next((c.idx for c in candidates if c.ref and c.ref.gid), candidates[0].idx)
    intro = f"共 {len(candidates)} 个图集，请在指令后加序号，例如 /pdf {example}："
    if strict:
        return None, _listing(candidates, f"引用的消息里{intro}")
    found = [c.ref for c in candidates if c.ref and c.ref.gid]
    unique = {r.gid for r in found}
    if len(unique) == 1:
        # 优先返回带标题的那条（来自登记表）
        return max(found, key=lambda r: bool(r.title)), ""
    if not unique:
        other = next((c.ref for c in candidates if c.ref), None)
        if other:
            return other, ""
    return None, _listing(candidates, f"上一次抽卡{intro}")
