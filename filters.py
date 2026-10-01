"""分级闸门、画廊分级判定与未成年内容标签过滤。

这里的判定只依赖 E-Hentai 画廊的分类和标签，属于硬性规则，不开放配置。
"""

import re

from .models import ANIME, EXPLICIT, REAL, SENSITIVE

# 内置基线黑名单，不可删除，用户只能追加
BASE_BLACKLIST = (
    "loli",
    "lolicon",
    "shota",
    "shotacon",
    "toddlercon",
    "child",
    "female_child",
    "male_child",
    "toddler",
    "萝莉",
    "正太",
    "幼女",
    "幼児",
    "ロリ",
    "ショタ",
)

# 每次搜索都排除的标签（ExHentai 上能搜到这类画廊），本地黑名单是第二道保险
SEARCH_EXCLUDES = (
    "-female:lolicon$",
    "-male:shotacon$",
    '-female:"low lolicon$"',
    '-male:"low shotacon$"',
    '-female:"oppai loli$"',
    "-female:toddlercon$",
    "-male:toddlercon$",
)

# 三次元分类，其余分类一律视为二次元
REAL_CATEGORIES = frozenset({"Cosplay", "Asian Porn"})
# 「无性内容」的分类与标签：命中即为擦边
SENSITIVE_CATEGORIES = frozenset({"Non-H"})
SENSITIVE_TAGS = frozenset({"other:non-nude"})
# 裸露或性内容的证据（打码类标签只用于露出性器官的画廊）：
# 优先级高于上面两项，与 non-nude 矛盾时按 R18 处理
EXPLICIT_TAGS = frozenset(
    {
        "other:nudity only",
        "other:uncensored",
        "other:mosaic censorship",
        "other:full censorship",
        "other:hardcore",
        "other:no penetration",
        "other:object insertion only",
    }
)


class TagBlacklist:
    """忽略大小写的标签黑名单。

    ASCII 词按单词匹配（冒号、下划线、空格、连字符等视为分隔），避免 lolita_fashion
    这类误伤；非 ASCII 词（中日文）按子串匹配，覆盖「合法ロリ」「萝莉塔」等变体。
    """

    def __init__(self, extra: list[str] | None = None):
        terms = {t.strip().lower() for t in (*BASE_BLACKLIST, *(extra or []))}
        terms.discard("")
        self.terms = frozenset(terms)
        self._word_terms = [
            (t, re.compile(rf"(?<![0-9a-z]){re.escape(t)}(?![0-9a-z])"))
            for t in sorted(self.terms)
            if t.isascii()
        ]
        self._sub_terms = sorted(t for t in self.terms if not t.isascii())

    def hit(self, tags: list[str]) -> str | None:
        """返回命中的黑名单词，未命中返回 None。"""
        for tag in tags:
            low = str(tag).strip().lower()
            if not low:
                continue
            for term, pattern in self._word_terms:
                if pattern.search(low):
                    return term
            for term in self._sub_terms:
                if term in low:
                    return term
        return None


def classify(category: str, tags: list[str]) -> tuple[str, str | None]:
    """按画廊分类和标签判定 (风格, 分级)，无法判定分级时为 None。

    二次元 H 分类本身就是 R18；三次元必须有标签证据，两种标签都没有的画廊
    （实测约 2%）里既有性内容也有穿着完整的写真，无法判定。
    """
    style = REAL if category in REAL_CATEGORIES else ANIME
    tagset = {t.lower() for t in tags}
    if tagset & EXPLICIT_TAGS:
        return style, EXPLICIT
    if category in SENSITIVE_CATEGORIES or tagset & SENSITIVE_TAGS:
        return style, SENSITIVE
    return style, (None if style == REAL else EXPLICIT)


def request_gate(
    rating: str,
    is_private: bool,
    r18_enabled: bool,
    group_sensitive_enabled: bool,
) -> str | None:
    """请求阶段闸门。返回拒绝原因，允许时返回 None。"""
    if rating == EXPLICIT:
        if not is_private:
            return "R18 内容仅限私聊。"
        if not r18_enabled:
            return "R18 功能未开启。"
    if rating == SENSITIVE and not is_private and not group_sensitive_enabled:
        return "本群未开启擦边内容。"
    return None


def check_gallery(
    category: str,
    tags: list[str],
    style: str,
    rating: str,
    is_private: bool,
    blacklist: TagBlacklist,
) -> str | None:
    """结果阶段复核。返回丢弃原因，通过时返回 None。"""
    real_style, real_rating = classify(category, tags)
    if real_style != style:
        return f"风格不符（{category}）"
    if real_rating is None:
        return "缺少可判定分级的标签"
    if real_rating != rating:
        return f"分级不符（请求 {rating}，实际 {real_rating}）"
    # R18 双重闸门的第二道：以画廊实际分级为准复核私聊
    if real_rating == EXPLICIT and not is_private:
        return "R18 结果出现在非私聊会话"
    if not tags:
        return "画廊缺少标签，无法做未成年过滤"
    term = blacklist.hit(tags)
    if term:
        return f"命中黑名单标签 {term}"
    return None
