"""图源基类与各图源共用的关键词处理。

每个图源提供：
- key / name：配置和 PDF 存储名里的图源键、标题行和提示里的显示名；
- style、intro：风格，帮助里的一行介绍；
- ratings：站点能判定出的分级；scope 是配置的适用分级，两者都包含请求的分级时才参与抽取；
- link_re：作品链接（第 1 组是作品 id，有第 2 组时是 E-Hentai 画廊的 token），没有公开链接时为 None；
- unavailable：缺少账号等不能使用的原因；
- accepts(ctx)：这次请求能否使用该图源（子类在分级之外再加自己的限制）；
- draw(ctx, n)：抽 n 个图集，返回 (图集, 错误说明)；
- work(ref) / download_work(work, dest)：/pdf 查询和下载整个作品。
"""

import re
from pathlib import Path

from astrbot.api import logger

from ..filters import ContentFilter, rating_reason
from ..models import (
    EXPLICIT,
    SENSITIVE,
    Album,
    DrawContext,
    DrawOptions,
    Work,
    WorkRef,
)
from ..net import NETWORK_ERRORS, ImageCache, download_all
from ..util import fetch_pages, fill

ALL_RATINGS = frozenset({SENSITIVE, EXPLICIT})
# E-Hentai 搜索语法里的字符：带这些字符的关键词是标签写法
TAG_SYNTAX = frozenset(':$"')
# E-Hentai 搜索词形式的标签，例如 character:"hu tao$"、female:swimsuit$
TAG_TERM_RE = re.compile(r'^(?:[a-z]+:)?"?([^":$]+?)\$?"?$', re.I)


class SourceError(Exception):
    """图源自身的错误（登录失败、搜不到、站点改版等），说明直接给用户看，并停止这次抽取。"""


def split_terms(terms: list[str]) -> tuple[list[str], list[str]]:
    """返回 (要搜的词, 要排除的词)，「-」开头的是排除词（去掉「-」）。"""
    positive = [t for t in terms if t and not t.startswith("-")]
    negative = [t[1:] for t in terms if t.startswith("-") and len(t) > 1]
    return positive, negative


def keyword_and_excludes(terms: list[str]) -> tuple[str, list[str]]:
    """不支持排除语法的搜索：返回 (搜索用的关键词, 在本地过滤的排除词，转成小写)。"""
    positive, negative = split_terms(terms)
    return " ".join(positive), [t.lower() for t in negative]


def has_tag_syntax(terms: list[str]) -> bool:
    return any(TAG_SYNTAX & set(t) for t in terms)


def plain_word(term: str) -> str | None:
    """E-Hentai 标签写法取出标签名（character:"hu tao$" → hu tao），其他词合并空白后原样返回。

    带 : $ " 但不是单个标签的写法取不出来，返回 None。
    """
    word = term.strip()
    if TAG_SYNTAX & set(word):
        match = TAG_TERM_RE.match(word)
        if not match:
            return None
        word = match.group(1)
    return " ".join(word.split()) or None


def note(errors: list[str], message: str):
    """同样的错误说明只记一次。"""
    if message not in errors:
        errors.append(message)


class Source:
    key: str
    name: str
    style: str
    intro: str
    link_re: re.Pattern | None = None
    ratings: frozenset[str] = ALL_RATINGS

    def __init__(self, cache: ImageCache, content: ContentFilter, opts: DrawOptions):
        self.cache = cache
        self.content = content
        self.opts = opts
        self.scope: frozenset[str] = ALL_RATINGS

    @property
    def usable_ratings(self) -> frozenset[str]:
        return self.ratings & self.scope

    @property
    def unavailable(self) -> str | None:
        return None

    def accepts(self, ctx: DrawContext) -> bool:
        return ctx.req.style == self.style and ctx.req.rating in self.usable_ratings

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        raise NotImplementedError

    async def work(self, ref: WorkRef) -> Work | None:
        """查询作品，不存在时返回 None。"""
        raise NotImplementedError

    async def close(self):
        pass

    def links(self, text: str) -> list[tuple[int, WorkRef]]:
        """文字里本图源的作品链接：[(出现位置, 作品)]。"""
        if self.link_re is None:
            return []
        has_token = self.link_re.groups > 1
        return [
            (m.start(), WorkRef(self.key, m.group(1), m.group(2) if has_token else ""))
            for m in self.link_re.finditer(text)
        ]

    # ---- 抽图 ----

    def rating_reason(self, actual: str | None, ctx: DrawContext) -> str | None:
        return rating_reason(actual, ctx.req.rating, ctx.allow_explicit)

    async def pick_pages(self, total: int, n: int, rating: str | None, fetch) -> list:
        """从作品的 total 页里取 n 张，失败的页换别的页补上，按页码排序。

        fetch(页, 从 0 开始) 返回 (页码, 本地文件) 或 None。R18 作品随机取页时跳过开头一部分。
        """
        skip = self.opts.explicit_skip if rating == EXPLICIT else 0.0
        pictures = await fetch_pages(
            total,
            n,
            self.opts.concurrency,
            fetch,
            from_start=self.opts.from_start,
            skip=skip,
        )
        return sorted(pictures)

    async def collect(
        self, n: int, tries: int, attempt
    ) -> tuple[list[Album], list[str]]:
        """并发调用 attempt() 凑 n 个图集，每个图集最多试 tries 次。

        attempt 返回图集，被过滤或下载失败时返回 None；网络错误换一次再试，
        SourceError 停止抽取并作为说明返回。
        """
        albums: list[Album] = []
        errors: list[str] = []
        failed = False

        async def run() -> Album | None:
            nonlocal failed
            try:
                album = await attempt()
            except NETWORK_ERRORS as e:
                logger.warning(f"[random_pic] {self.name} 请求失败: {e!r}")
                note(errors, f"{self.name} 请求失败")
                return None
            if album is None:
                failed = True
            return album

        try:
            await fill(albums, n, tries * n, self.opts.concurrency, run)
        except SourceError as e:
            logger.warning(f"[random_pic] {e}")
            errors.append(str(e))
        if failed:
            errors.append(f"{self.name} 有作品被过滤或下载失败")
        return albums, errors

    # ---- 整本打包 ----

    async def page_items(self, work: Work) -> list:
        """整个作品每页要下载的东西，默认是 work.data 里的图片地址。"""
        return list(work.data)

    async def download_page(self, item, dest: Path) -> Path | None:
        return await self.cache.download(item, dest)

    async def download_work(self, work: Work, dest: Path) -> tuple[list[Path], int]:
        """下载整个作品到 dest（文件以页码命名），返回 (按页码排序的图片路径, 失败页数)。"""
        items = await self.page_items(work)
        if not items:
            raise SourceError(f"{self.name} 没有返回图片列表")
        paths, missing = await download_all(
            items, dest, self.opts.concurrency, self.download_page
        )
        return paths, missing + max(0, work.pages - len(items))
