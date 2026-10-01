"""E-Hentai 随机抽卡插件：指令与参数解析。"""

import asyncio
import re
import shutil
import time
from datetime import date
from pathlib import Path

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .drawer import Drawer, build_pools
from .ehentai import EHentai, EHentaiError
from .filters import TagBlacklist, classify, request_gate
from .models import (
    ANIME,
    EXPLICIT,
    RATING_NAMES,
    REAL,
    SENSITIVE,
    STYLE_NAMES,
    ImageItem,
    PicRequest,
)
from .net import HttpClient, HttpError, ImageCache
from .pdf import write_pdf
from .refs import Unit, gallery_from_text, message_units, pick, resolve_units
from .registry import GalleryRef, SentRegistry
from .tags import DEFAULT_DB_URL, TagDB

PLUGIN_NAME = "astrbot_plugin_random_pic"
# 站点 → (首页, API, cookie 域名)
SITES = {
    "e-hentai": (
        "https://e-hentai.org",
        "https://api.e-hentai.org/api.php",
        "e-hentai.org",
    ),
    "exhentai": (
        "https://exhentai.org",
        "https://exhentai.org/api.php",
        "exhentai.org",
    ),
}
COOKIE_KEYS = ("ipb_member_id", "ipb_pass_hash", "igneous", "sk")
EX_REQUIRED_COOKIES = ("ipb_member_id", "ipb_pass_hash", "igneous")

STYLE_WORDS = {"二次元": ANIME, "三次元": REAL}
RATING_WORDS = {"擦边": SENSITIVE, "r18": EXPLICIT, "色图": EXPLICIT}
DEFAULT_RATING_WORDS = {"擦边": SENSITIVE, "R18": EXPLICIT}
HELP_WORDS = {"help", "帮助"}
FORWARD = "合并转发"
FORWARD_PLATFORMS = {"aiocqhttp"}
OWN_PDF = re.compile(r"\d+(?:-incomplete)?\.pdf")
UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def resolve_site(eh_conf: dict) -> tuple[str, dict[str, str]]:
    """返回 (站点名, 登录 cookie)。ExHentai 缺少必需 cookie 时退回表站。"""
    cookies = {k: str(eh_conf.get(k) or "").strip() for k in COOKIE_KEYS}
    cookies = {k: v for k, v in cookies.items() if v}
    site = eh_conf.get("site", "e-hentai")
    if site not in SITES:
        site = "e-hentai"
    if site == "exhentai":
        missing = [k for k in EX_REQUIRED_COOKIES if k not in cookies]
        if missing:
            logger.warning(
                f"[random_pic] 使用 ExHentai 需要 cookie {', '.join(missing)}，已改用 E-Hentai"
            )
            site = "e-hentai"
    return site, cookies


def parse_args(
    tokens: list[str],
    max_count: int,
    style: str = ANIME,
    rating: str = SENSITIVE,
    count: int = 1,
) -> PicRequest:
    """宽松解析：风格词、分级词、数字（数量）可任意顺序，其余当作搜索关键词。"""
    req = PicRequest(style=style, rating=rating, count=count)
    for token in tokens:
        low = token.lower()
        if token in STYLE_WORDS:
            req.style = STYLE_WORDS[token]
        elif low in RATING_WORDS:
            req.rating = RATING_WORDS[low]
        elif token.isdigit():
            req.count = int(token)
        else:
            req.tags.append(token)
    req.count = max(1, min(req.count, max(1, max_count)))
    return req


def format_caption(item: ImageItem) -> str:
    lines = []
    if item.title:
        lines.append(f"标题：{item.title}")
    if item.author:
        lines.append(f"作者：{item.author}")
    if item.parodies:
        lines.append(f"作品：{'、'.join(item.parodies)}")
    if item.characters:
        lines.append(f"角色：{'、'.join(item.characters)}")
    info = [item.category, f"第 {item.page}/{item.pages} 页"]
    if item.stars:
        info.append(f"★{item.stars:.1f}")
    lines.append(" · ".join(i for i in info if i))
    lines.append(f"画廊：{item.gallery_url}")
    return "\n".join(lines)


def pdf_filename(title: str, gid: int) -> str:
    name = UNSAFE_FILENAME.sub(" ", title).strip()[:80] or str(gid)
    return f"{name}.pdf"


class RandomPicPlugin(Star):
    """E-Hentai 随机抽卡：/抽图 [二次元|三次元] [擦边|r18] [关键词...] [数量]"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.max_count = max(1, int(config.get("max_count", 5)))
        self.access = config.get("access", {})
        self._last_use: dict[str, float] = {}
        self._daily: dict[str, int] = {}
        self._daily_date = date.today()
        self.default_style = STYLE_WORDS.get(config.get("default_style"), REAL)
        self.default_rating = DEFAULT_RATING_WORDS.get(
            config.get("default_rating"), SENSITIVE
        )
        self.default_count = max(1, int(config.get("default_count", 1)))
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.registry = SentRegistry(data_dir / "sent_images.json")
        pdf_conf = config.get("pdf", {})
        self.pdf_conf = pdf_conf
        self.pdf_dir = Path(
            (pdf_conf.get("output_dir") or "").strip() or data_dir / "pdf"
        )
        self.pdf_tmp = data_dir / "pdf_tmp"
        self._pdf_lock = asyncio.Lock()

        eh_conf = config.get("ehentai", {})
        self.site, cookies = resolve_site(eh_conf)
        site_url, api_url, cookie_domain = SITES[self.site]
        # nw=1 跳过画廊的内容警告页；cookie 只发给当前站点的域名
        self.http = HttpClient(
            float(config.get("request_timeout", 20)),
            {cookie_domain: {"nw": "1", **cookies}},
        )
        cache_conf = config.get("cache", {})
        cache = ImageCache(
            self.http,
            StarTools.get_data_dir(PLUGIN_NAME) / "cache",
            max_files=max(int(cache_conf.get("max_files", 100)), self.max_count),
            max_total_mb=float(cache_conf.get("max_total_mb", 200)),
            max_image_mb=float(cache_conf.get("max_image_mb", 10)),
        )
        proxy = (eh_conf.get("proxy") or "").strip() or None
        eh = EHentai(
            self.http,
            site_url,
            api_url,
            proxy,
            float(eh_conf.get("request_interval", 0.5)),
        )
        tag_conf = config.get("tag_db", {})
        self.tagdb = None
        if tag_conf.get("enabled", True):
            self.tagdb = TagDB(
                self.http,
                StarTools.get_data_dir(PLUGIN_NAME) / "ehtag.json.gz",
                (tag_conf.get("url") or "").strip() or DEFAULT_DB_URL,
                proxy if tag_conf.get("use_proxy", True) else None,
                float(tag_conf.get("refresh_days", 7)),
            )
        self._preload: asyncio.Task | None = None
        self.drawer = Drawer(
            eh,
            build_pools(config.get("pools", {})),
            TagBlacklist(config.get("extra_blacklist", [])),
            cache,
            int(config.get("max_retries", 3)),
            exclude_ai=bool(eh_conf.get("exclude_ai", True)),
            min_stars=int(eh_conf.get("min_rating", 4)),
            min_pages=int(eh_conf.get("min_pages", 0)),
            cover_only=eh_conf.get("page_pick", "随机页") == "封面",
            explicit_skip=float(eh_conf.get("explicit_skip_ratio", 0.3)),
            same_gallery=bool(config.get("same_gallery", False)),
            tags=self.tagdb,
        )

    async def initialize(self):
        # 后台预加载标签库，避免第一次抽卡时等待下载
        if self.tagdb:
            self._preload = asyncio.create_task(self.tagdb.get())

    @filter.command("抽图")
    async def draw_pic(self, event: AstrMessageEvent):
        """E-Hentai 随机抽卡。用法：/抽图 [二次元|三次元] [擦边|r18] [关键词...] [数量]，/抽图 帮助 查看说明"""
        async for result in self._handle(event):
            yield result

    @filter.command("随机角色")
    async def random_character(self, event: AstrMessageEvent):
        """每张图先随机抽一个角色再抽图。用法同 /抽图"""
        async for result in self._handle(event, random_character=True):
            yield result

    @filter.command("二次元")
    async def alias_anime(self, event: AstrMessageEvent):
        """随机二次元图，等同于 /抽图 二次元"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, style=ANIME):
                yield result

    @filter.command("三次元")
    async def alias_real(self, event: AstrMessageEvent):
        """随机三次元图，等同于 /抽图 三次元"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, style=REAL):
                yield result

    @filter.command("擦边")
    async def alias_sensitive(self, event: AstrMessageEvent):
        """随机擦边图，等同于 /抽图 擦边"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, rating=SENSITIVE):
                yield result

    @filter.command("色图")
    async def alias_explicit(self, event: AstrMessageEvent):
        """随机 R18 图（仅私聊），等同于 /抽图 r18"""
        if self.config.get("enable_aliases", True):
            async for result in self._handle(event, rating=EXPLICIT):
                yield result

    @filter.command("pdf", alias={"全本"})
    async def gallery_pdf(self, event: AstrMessageEvent):
        """回复抽到的图片，获取整个画廊的 PDF。也可以 /pdf <画廊链接>"""
        async for result in self._handle_pdf(event):
            yield result

    async def _handle(
        self,
        event: AstrMessageEvent,
        style: str | None = None,
        rating: str | None = None,
        random_character: bool = False,
    ):
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        if not self._allowed(user_id, group_id):
            return

        tokens = event.message_str.split()[1:]
        if tokens and tokens[0].lower() in HELP_WORDS:
            yield event.plain_result(self.help_text())
            return
        req = parse_args(
            tokens,
            self.max_count,
            style or self.default_style,
            rating or self.default_rating,
            self.default_count,
        )
        req.random_character = random_character
        is_private = event.is_private_chat()

        denied = request_gate(
            req.rating,
            is_private,
            bool(self.config.get("r18_enabled", False)),
            bool(self.config.get("group_sensitive_enabled", False)),
        )
        if denied:
            yield event.plain_result(denied)
            return

        limited = self._check_limits(user_id, req)
        if limited:
            yield event.plain_result(limited)
            return
        self._last_use[user_id] = time.monotonic()

        result = await self.drawer.draw(req, is_private)
        if not result.images:
            detail = "；".join(result.errors[:6]) or "未知原因"
            logger.warning(f"[random_pic] 获取失败 {req}: {detail}")
            yield event.plain_result(
                f"获取{STYLE_NAMES[req.style]}·{RATING_NAMES[req.rating]}图片失败：{detail}"
            )
            return

        self._daily[user_id] = self._daily.get(user_id, 0) + len(result.images)
        self.registry.record(
            event.unified_msg_origin,
            [
                (
                    path.stat().st_size,
                    GalleryRef(item.gid, item.token, item.title, item.pages),
                )
                for item, path in result.images
            ],
        )
        contents = [self._content(item, path) for item, path in result.images]
        if len(contents) > 1 and self._use_forward(event):
            # 合并转发：每张图一个节点，节点顺序即抽图顺序
            uin = str(event.get_self_id())
            nodes = [Comp.Node(content=c, uin=uin, name="抽图") for c in contents]
            yield event.chain_result([Comp.Nodes(nodes)])
        else:
            for content in contents:
                yield event.chain_result(content)
        if len(result.images) < req.count:
            yield event.plain_result(
                f"仅获取到 {len(result.images)}/{req.count} 张图片。"
            )

    def _content(self, item: ImageItem, path: Path) -> list:
        content = [Comp.Image.fromFileSystem(str(path))]
        if self.config.get("send_caption", True):
            content.append(Comp.Plain(format_caption(item)))
        return content

    def _use_forward(self, event: AstrMessageEvent) -> bool:
        return (
            self.config.get("send_mode", FORWARD) == FORWARD
            and event.get_platform_name() in FORWARD_PLATFORMS
        )

    async def _handle_pdf(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        group_id = str(event.get_group_id() or "")
        if not self._allowed(user_id, group_id):
            return
        tokens = event.message_str.split()[1:]
        if tokens and tokens[0].lower() in HELP_WORDS:
            yield event.plain_result(self.help_text())
            return
        if not self.pdf_conf.get("enabled", True):
            yield event.plain_result("整本 PDF 功能未开启。")
            return

        ref, hint = await self._pdf_target(event, tokens)
        if ref is None:
            yield event.plain_result(hint)
            return
        try:
            gallery = await self.drawer.eh.gallery(ref.gid, ref.token)
        except (
            EHentaiError,
            HttpError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as e:
            logger.warning(f"[random_pic] 查询画廊 {ref.gid} 失败: {e!r}")
            yield event.plain_result(f"查询画廊失败：{e}")
            return
        if gallery is None or gallery.expunged:
            yield event.plain_result("画廊不存在或已被删除。")
            return
        denied = self._pdf_gate(gallery, event.is_private_chat())
        if denied:
            yield event.plain_result(denied)
            return
        max_pages = int(self.pdf_conf.get("max_pages", 200))
        if gallery.filecount > max_pages:
            yield event.plain_result(
                f"画廊共 {gallery.filecount} 页，超过上限 {max_pages} 页，不予打包。"
            )
            return

        out = self.pdf_dir / f"{gallery.gid}.pdf"
        if not out.exists():
            if self._pdf_lock.locked():
                yield event.plain_result("正在打包另一个画廊，请稍后再试。")
                return
            async with self._pdf_lock:
                yield event.plain_result(
                    f"开始下载《{gallery.title}》共 {gallery.filecount} 页，完成后发送 PDF……"
                )
                try:
                    out, missing = await self._build_pdf(gallery)
                except EHentaiError as e:
                    yield event.plain_result(f"打包失败：{e}")
                    return
                except Exception as e:
                    logger.exception(f"[random_pic] 画廊 {gallery.gid} 打包失败")
                    yield event.plain_result(f"打包失败：{e!r}")
                    return
                if missing:
                    yield event.plain_result(
                        f"有 {missing} 页下载失败，PDF 中缺少这些页。"
                    )
        yield event.chain_result(
            [
                Comp.File(
                    name=pdf_filename(gallery.title, gallery.gid),
                    file=str(out.resolve()),
                )
            ]
        )

    async def _pdf_target(
        self, event: AstrMessageEvent, tokens: list[str]
    ) -> tuple[GalleryRef | None, str]:
        """依次看参数里的画廊链接、被回复的消息、本会话上一次抽卡。"""
        for token in tokens:
            ref = gallery_from_text(token)
            if ref:
                return ref, ""
        index = next((int(t) for t in tokens if t.isdigit()), None)
        reply = next(
            (c for c in event.message_obj.message if isinstance(c, Comp.Reply)), None
        )
        if reply is not None:
            refs = resolve_units(await self._reply_units(event, reply), self.registry)
        else:
            refs = self.registry.last(event.unified_msg_origin)
        return pick(refs, index)

    async def _reply_units(self, event: AstrMessageEvent, reply) -> list[Unit]:
        """读取被回复消息的原始消息段（需要文件大小和合并转发内容），失败时退回纯文本。"""
        bot = getattr(event, "bot", None)
        if bot is not None and reply.id:
            try:
                raw = await bot.call_action("get_msg", message_id=int(reply.id))

                async def get_forward(forward_id: str) -> list:
                    data = await bot.call_action("get_forward_msg", id=forward_id)
                    return data.get("messages") or data.get("message") or []

                return await message_units(raw.get("message"), get_forward)
            except Exception as e:
                logger.warning(f"[random_pic] 读取被回复的消息失败: {e!r}")
        return [Unit(reply.message_str or "")]

    def _pdf_gate(self, gallery, is_private: bool) -> str | None:
        """整本打包和抽图走同样的分级闸门；无法判定分级的按 R18 处理。"""
        if not gallery.tags:
            return "画廊缺少标签，无法做未成年过滤，不予打包。"
        if self.drawer.blacklist.hit(gallery.tags):
            return "画廊命中黑名单标签，不予打包。"
        _, rating = classify(gallery.category, gallery.tags)
        return request_gate(
            rating or EXPLICIT,
            is_private,
            bool(self.config.get("r18_enabled", False)),
            bool(self.config.get("group_sensitive_enabled", False)),
        )

    async def _build_pdf(self, gallery) -> tuple[Path, int]:
        """下载整个画廊并写成 PDF，返回 (PDF 路径, 失败页数)。

        有缺页时文件名带 -incomplete，不会被当成完整缓存复用。
        """
        tmp = self.pdf_tmp / str(gallery.gid)
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            concurrency = int(self.pdf_conf.get("concurrency", 3))
            paths, missing = await self.drawer.download_gallery(
                gallery, tmp, concurrency
            )
            if not paths:
                raise EHentaiError("没有下载到任何图片")
            self.pdf_dir.mkdir(parents=True, exist_ok=True)
            suffix = "-incomplete" if missing else ""
            out = self.pdf_dir / f"{gallery.gid}{suffix}.pdf"
            await asyncio.to_thread(write_pdf, paths, out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._prune_pdfs(keep=out)
        return out, missing

    def _prune_pdfs(self, keep: Path):
        """只清理本插件生成的 PDF（文件名为画廊号），输出目录可能是共享目录。"""
        limit = max(1, int(self.pdf_conf.get("keep_files", 10)))
        ours = [
            p
            for p in self.pdf_dir.glob("*.pdf")
            if OWN_PDF.fullmatch(p.name) and p != keep
        ]
        ours.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for path in ours[limit - 1 :]:
            path.unlink(missing_ok=True)

    def help_text(self) -> str:
        on_off = {True: "已开启", False: "未开启"}
        lines = [
            "【抽图用法】",
            "/抽图 [二次元|三次元] [擦边|r18] [关键词...] [数量]",
            f"· 参数顺序随意；不写时默认 {STYLE_NAMES[self.default_style]}·"
            f"{RATING_NAMES[self.default_rating]}、{self.default_count} 张，每次最多 {self.max_count} 张",
            "· 关键词可写中文角色、作品名（如 芙莉莲、原神）或 E-Hentai 标签，前缀 - 表示排除",
            "/随机角色 [同上]：每张图先随机抽一个角色",
        ]
        if self.pdf_conf.get("enabled", True):
            lines.append(
                "/pdf：回复抽到的图片，获取整个画廊的 PDF（合并转发可加序号，如 /pdf 2；"
                "也可以 /pdf <画廊链接>）"
            )
        if self.config.get("enable_aliases", True):
            lines.append("别名：/二次元 /三次元 /擦边 /色图")
        lines += [
            f"R18：仅限私聊（{on_off[bool(self.config.get('r18_enabled', False))]}）；"
            f"群聊擦边：{on_off[bool(self.config.get('group_sensitive_enabled', False))]}",
            "示例：/抽图 原神 2　/抽图 二次元 芙莉莲",
            "/抽图 帮助：显示本说明",
        ]
        return "\n".join(lines)

    def _allowed(self, user_id: str, group_id: str) -> bool:
        blacklist = {str(u) for u in self.access.get("user_blacklist", [])}
        if user_id in blacklist:
            return False
        whitelist = {str(g) for g in self.access.get("group_whitelist", [])}
        if group_id and whitelist and group_id not in whitelist:
            return False
        return True

    def _check_limits(self, user_id: str, req: PicRequest) -> str | None:
        cooldown = float(self.access.get("cooldown_seconds", 0))
        last = self._last_use.get(user_id)
        if cooldown > 0 and last is not None:
            remain = cooldown - (time.monotonic() - last)
            if remain > 0:
                return f"冷却中，请 {remain:.0f} 秒后再试。"

        daily_limit = int(self.access.get("daily_limit", 0))
        if daily_limit > 0:
            if self._daily_date != date.today():
                self._daily_date = date.today()
                self._daily.clear()
            used = self._daily.get(user_id, 0)
            if used >= daily_limit:
                return f"今日次数已用完（{daily_limit} 张）。"
            req.count = min(req.count, daily_limit - used)
        return None

    async def terminate(self):
        if self._preload and not self._preload.done():
            self._preload.cancel()
        await self.http.close()
