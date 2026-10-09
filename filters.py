"""内容过滤：未成年内容黑名单、重口和跨性别标签与结果阶段的分级复核，对所有图源生效。

未成年内容过滤始终生效；各图源怎么判定作品的分级见各自的模块。
"""

import re

from .models import EXPLICIT

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

# 「屏蔽重口」开启时屏蔽的标签（E-Hentai 标签名，不带命名空间；Danbooru、禁漫按标签名匹配）
HEAVY_TAGS = frozenset(
    {
        "guro",
        "low guro",
        "snuff",
        "amputee",
        "body modification",
        "vore",
        "unbirth",
        "absorption",
        "scat",
        "scat insertion",
        "vomit",
        "torture",
        "blood",
        "necrophilia",
        "cannibalism",
        "eye penetration",
        "brain fuck",
        "skinsuit",
        "ryona",
        "abortion",
        "bestiality",
        "prolapse",
        "dismantling",
        "piss drinking",
        "farting",
        "hanging",
        "electric shocks",
        "cbt",
        "nose hook",
        "insect",
        "worm",
        "parasite",
        "cervix penetration",
        "infantilism",
        "diaper",
    }
)
HEAVY_NAMESPACES = ("female", "male", "mixed")

# 「屏蔽跨性别作品」开启时加进黑名单的词，和黑名单一样匹配标签和标题。
# 英文是 E-Hentai（EhTagTranslation 里扶她、扶他、人妖、伪娘一类的标签）和 Danbooru 的标签名；
# crossdressing 只认 male 命名空间（男性女装），female:crossdressing 是女性男装。
# 性转（gender change、gender morph）不算在内：Cosplay 里的性转多是女性扮演男角色的女体版。
TRANS_TAGS = (
    "futanari",
    "futanarization",
    "otokofutanari",
    "futa",
    "shemale",
    "dickgirl",
    "dickgirls",
    "cuntboy",
    "pussyboy",
    "pussyboys",
    "tomgirl",
    "josou seme",
    "feminization",
    "male:crossdressing",
    "otoko no ko",
    "newhalf",
    "ladyboy",
    "transgender",
    "transsexual",
    "扶她",
    "扶他",
    "人妖",
    "伪娘",
    "偽娘",
    "男娘",
    "药娘",
    "藥娘",
    "跨性别",
    "跨性別",
    "女装大佬",
    "女裝大佬",
    "ふたなり",
    "フタナリ",
    "男の娘",
    "ニューハーフ",
    "シーメール",
)
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
    """黑名单、重口标签与 AI 作品，对所有图源生效。

    heavy 为空表示不屏蔽重口；block_trans 时跨性别标签（TRANS_TAGS）也进黑名单；
    block_ai 时各图源排除站点标出的 AI 作品（分类、标签）。
    """

    def __init__(
        self,
        extra_blacklist: list[str] = (),
        heavy: frozenset[str] = frozenset(),
        block_ai: bool = True,
        block_trans: bool = False,
    ):
        self.blacklist = TagBlacklist(
            [*extra_blacklist, *(TRANS_TAGS if block_trans else ())]
        )
        self.heavy = heavy
        self.block_ai = block_ai
        self.block_trans = block_trans

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


def rating_reason(
    actual: str | None, requested: str, allow_explicit: bool
) -> str | None:
    """结果阶段的分级复核：作品的实际分级必须和请求一致。

    R18 双重闸门的第二道：请求阶段已经拒绝了不允许 R18 的会话，这里再按作品的实际分级确认。
    """
    if actual is None:
        return "无法判定分级"
    if actual != requested:
        return f"分级不符（请求 {requested}，实际 {actual}）"
    if actual == EXPLICIT and not allow_explicit:
        return "这个会话不允许 R18"
    return None
