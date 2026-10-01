"""内容过滤：未成年内容黑名单、重口标签与 E-Hentai 画廊的风格 / 分级判定。

分级判定只依赖画廊的分类和标签，不开放配置；未成年内容过滤始终生效。
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
    # 低龄指向的服饰 / 设定：无 H 画廊不会打 lolicon，但常带这些标签
    "randoseru",
    "kindergarten uniform",
    "age regression",
    "萝莉",
    "蘿莉",  # 哔咔的标签是繁体
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

HEAVY_NAMESPACES = ("female", "male", "mixed")
# E-Hentai 搜索词形式的标签，例如 female:"body modification$"
SEARCH_TAG_RE = re.compile(r'^(female|male|mixed):"?([^"$]+)\$?"?$')


class TagBlacklist:
    """忽略大小写的黑名单。

    ASCII 词按单词匹配（冒号、下划线、空格、连字符等视为分隔），避免 lolita_fashion
    这类误伤；非 ASCII 词（中日文）按子串匹配，覆盖「合法ロリ」「萝莉塔」等变体。
    """

    def __init__(self, extra: list[str] = ()):
        terms = {t.strip().lower() for t in (*BASE_BLACKLIST, *extra)}
        terms.discard("")
        self._word_terms = [
            (t, re.compile(rf"(?<![0-9a-z]){re.escape(t)}(?![0-9a-z])"))
            for t in sorted(terms)
            if t.isascii()
        ]
        self._sub_terms = sorted(t for t in terms if not t.isascii())

    def hit(self, texts: list[str]) -> str | None:
        """返回命中的黑名单词，未命中返回 None。"""
        for text in texts:
            low = str(text).strip().lower()
            if not low:
                continue
            for term, pattern in self._word_terms:
                if pattern.search(low):
                    return term
            for term in self._sub_terms:
                if term in low:
                    return term
        return None


class ContentFilter:
    """黑名单与重口标签，对所有图源生效。heavy 为空表示不屏蔽重口。"""

    def __init__(
        self, extra_blacklist: list[str] = (), heavy: frozenset[str] = frozenset()
    ):
        self.blacklist = TagBlacklist(extra_blacklist)
        self.heavy = heavy

    def heavy_hit(self, tags: list[str]) -> str | None:
        """返回命中的重口标签名，未命中返回 None。"""
        for tag in tags:
            namespace, _, name = str(tag).strip().lower().partition(":")
            if namespace in HEAVY_NAMESPACES and name in self.heavy:
                return name
        return None

    def keyword_reason(self, raw: list[str], terms: list[str]) -> str | None:
        """关键词闸门：原词和翻译后的标签都检查，排除词（- 开头）不检查。

        重口只认标签写法（中文关键词会先被翻译成这种写法）；不带命名空间的英文词是标题搜索，
        例如 blood 可能是在找《Blood+》，交给画廊复核过滤即可。
        """
        positive = [t for t in [*raw, *terms] if not t.startswith("-")]
        term = self.blacklist.hit(positive)
        if term:
            return f"关键词命中黑名单 {term}"
        for word in positive:
            match = SEARCH_TAG_RE.match(word.strip().lower())
            if match and match.group(2) in self.heavy:
                return f"关键词是已屏蔽的重口标签 {match.group(2)}"
        return None

    def tags_reason(self, tags: list[str]) -> str | None:
        """按标签过滤：没有标签无法做未成年过滤，一律拒绝。"""
        if not tags:
            return "缺少标签，无法做未成年过滤"
        term = self.blacklist.hit(tags)
        if term:
            return f"命中黑名单 {term}"
        heavy = self.heavy_hit(tags)
        if heavy:
            return f"带重口标签 {heavy}"
        return None

    def plain_tags_reason(self, tags: list[str]) -> str | None:
        """不带命名空间的标签（Danbooru）：下划线视为空格，重口按标签名匹配。"""
        if not tags:
            return "缺少标签，无法做未成年过滤"
        names = [t.strip().lower().replace("_", " ") for t in tags]
        term = self.blacklist.hit(names)
        if term:
            return f"命中黑名单 {term}"
        heavy = next((n for n in names if n in self.heavy), None)
        if heavy:
            return f"带重口标签 {heavy}"
        return None

    def text_reason(self, texts: list[str]) -> str | None:
        """没有标签的图源只能检查标题、简介等文字。"""
        term = self.blacklist.hit(texts)
        return f"命中黑名单 {term}" if term else None


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


def rating_reason(
    actual: str | None, requested: str, is_private: bool, rating_enabled: bool
) -> str | None:
    """结果阶段的分级复核，关闭内容分级时不复核。

    R18 双重闸门的第二道：请求阶段已经拒绝了群聊 R18，这里再按作品的实际分级确认是私聊。
    """
    if not rating_enabled:
        return None
    if actual is None:
        return "无法判定分级"
    if actual != requested:
        return f"分级不符（请求 {requested}，实际 {actual}）"
    if actual == EXPLICIT and not is_private:
        return "R18 结果出现在非私聊会话"
    return None
