"""分级闸门、分级复核与未成年内容标签过滤。

这里的规则只依赖 rating 和 tags，与 style 无关。
"""

import re

from .models import EXPLICIT, RATING_LEVEL, SENSITIVE, ImageItem

# 内置基线黑名单，不可删除，用户只能追加
BASE_BLACKLIST = (
    "loli",
    "shota",
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


class TagBlacklist:
    """忽略大小写的标签黑名单。

    ASCII 词按单词匹配（下划线、空格、连字符等视为分隔），避免 lolita_fashion
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


def check_item(
    item: ImageItem,
    requested_rating: str,
    is_private: bool,
    blacklist: TagBlacklist,
) -> str | None:
    """结果阶段复核。返回丢弃原因，通过时返回 None。"""
    if item.rating not in RATING_LEVEL:
        return f"未知分级 {item.rating!r}"
    if RATING_LEVEL[item.rating] > RATING_LEVEL[requested_rating]:
        return f"分级超出请求（请求 {requested_rating}，实际 {item.rating}）"
    if item.rating == EXPLICIT:
        # R18 双重闸门的第二道：以图源返回的分级为准复核私聊
        if not is_private:
            return "R18 结果出现在非私聊会话"
        if not item.tags:
            return "R18 结果缺少标签，无法做未成年过滤"
    term = blacklist.hit(item.tags)
    if term:
        return f"命中黑名单标签 {term}"
    return None
