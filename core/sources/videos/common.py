"""视频来源共用的内容标记与分页抽样。"""

import re
from collections.abc import Awaitable, Callable

from ...policy.content import TagBlacklist

# 视频的标题、描述和标签里明确指向小学、初中生的词（在内置黑名单之外；视频站没有统一的年龄标签）。
# 雌小鬼、メスガキ多是梗，JK、女子高生、schoolgirl 多是校服装扮，都不算，和图片源一致。
# 韩文的是 SOOP 用的：小学生、初中生、女初中生、小学生 / 初中生的俗称、未成年
YOUNG_WORDS = TagBlacklist(
    ["小学生", "中学生", "女子中学生", "初中生", "jc"]
    + ["초등학생", "중학생", "여중생", "초딩", "중딩", "미성년"]
)


# MMD（MikuMikuDance）视频的标签和标题写法：Iwara 约三成带 mikumikudance 标签或标题写 MMD，
# Danbooru 的 mikumikudance_(medium) 和 RedGifs 的 MMD 都很少。没标的认不出来
MMD_TAGS = frozenset({"mikumikudance", "mikumikudance_(medium)", "mmd"})
MMD_TITLE_RE = re.compile(r"mmd|mikumikudance|ミクミクダンス", re.I)


def is_mmd(tags: list[str], title: str = "") -> bool:
    return any(t.lower() in MMD_TAGS for t in tags) or bool(MMD_TITLE_RE.search(title))


def young_word(texts: list[str]) -> str | None:
    """返回命中的低龄指向词（内置黑名单和上面的词），没有时返回 None。"""
    return YOUNG_WORDS.hit(texts)


async def count_pages(
    full: Callable[[int], Awaitable[bool]], first: int, limit: int
) -> int:
    """按热度从高到低分页的列表里能抽的页数：整页都达标的页，再加后面一页（不达标的抽到时过滤）。

    full(页) 判断这一页是否整页达标，页码从 first 起，最多 limit 页。先倍增再二分，
    约 2·log2(limit) 次请求。
    """
    if not await full(first):
        return 1
    low, high = 0, 1  # 相对 first 的页：low 整页达标，high 待查
    while await full(first + high):
        if high >= limit - 1:
            return limit
        low, high = high, min(high * 2, limit - 1)
    while high - low > 1:
        mid = (low + high) // 2
        if await full(first + mid):
            low = mid
        else:
            high = mid
    return high + 1
