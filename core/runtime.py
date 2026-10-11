"""服务装配与生命周期；所有命令共用一个运行时。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import Context, StarTools

from .commands.catalog import CommandCatalog
from .commands.parser import RequestParser
from .config.settings import Settings
from .download.http import HttpClient
from .download.images import ImageCache
from .message.composer import Composer
from .message.dispatcher import Dispatcher
from .message.stage import Stage
from .message.types import INLINE_BUDGET
from .models import (
    ANIME,
    CONCURRENCY,
    EXPLICIT,
    REAL,
    SENSITIVE,
    STYLE_NAMES,
    DrawOptions,
)
from .paths import PLUGIN_NAME
from .policy.access import AccessControl
from .policy.content import HEAVY_TAGS, ContentFilter
from .services.drawing import Drawer
from .services.push import Pusher
from .services.reserve import Reserve, Spec
from .services.tags import TagDB
from .services.works import WorkService
from .sources.feeds.registry import FeedSet
from .sources.images.ehentai_api import resolve_site, site_cookies
from .sources.images.registry import SourceSet
from .sources.videos.registry import VideoSet
from .storage.history import History
from .storage.pdf import PdfStore


class Runtime:
    """集中持有插件服务，统一构造、启动和关闭。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        self.settings = s = Settings.load(config)
        self.commands = CommandCatalog(s.commands)
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        stage = self._stage(s.env.share_dir)
        # 要发送的图片、视频直接存在共享目录（有的话），发送时不用再复制
        media_dir = stage.root if stage else data_dir
        self.access = AccessControl(s.access)
        self.content = ContentFilter(
            s.filter.extra_blacklist,
            HEAVY_TAGS if s.filter.block_heavy else frozenset(),
            s.filter.block_ai,
            s.filter.block_trans,
        )

        eh = s.sources.ehentai
        site = resolve_site(eh.site, eh.cookies)
        self.http = HttpClient(s.env.proxy, site_cookies(site, eh.cookies))
        cache = ImageCache(self.http, media_dir / "cache")
        opts = DrawOptions(
            from_start=s.draw.from_start,
            explicit_skip=s.draw.explicit_skip,
            min_pages=s.draw.min_pages,
            concurrency=CONCURRENCY,
        )
        self.sources = SourceSet(
            s, self.http, cache, self.content, opts, data_dir, site
        )
        self.tagdb = TagDB(self.http, data_dir / "ehtag.json.gz")
        self.drawer = Drawer(self.sources, self.content, self.tagdb)
        self.videos = VideoSet(
            s.video,
            s.sources.video,
            self.http,
            cache,
            self.content,
            opts,
            media_dir / "video_tmp",
            self.sources.danbooru,
            data_dir / "video_recent.json",
        )
        self.feeds = FeedSet(
            s.sources.feeds,
            self.http,
            cache,
            self.content,
            opts,
            data_dir,
            self.videos.files,
            self.videos.recent,
        )
        self.sources.add(self.feeds.images, draw=s.sources.feeds.enabled)
        if s.sources.feeds.enabled and s.video.enabled:
            self.videos.add(self.feeds.videos)
        self.video_drawer = Drawer(self.videos, self.content, self.tagdb)
        self.requests = RequestParser(s, self.feeds)
        self.video_reserve = self._video_reserve(media_dir / "reserve_video")
        self.reserve = self._reserve(media_dir / "reserve")
        self.history = History(data_dir / "sent_albums.json")
        self.dispatcher = Dispatcher(
            self.history,
            Composer(
                s.send.header,
                s.send.caption,
                None if stage else INLINE_BUDGET,
                self.commands if s.whole.enabled else None,
            ),
            s.send.mode,
            stage,
        )
        self.works = WorkService(
            self.sources,
            self.access,
            self.drawer,
            media_dir / "work_tmp",
            s.whole.max_pages,
        )
        self.pdf = PdfStore(
            Path(s.env.pdf_dir) if s.env.pdf_dir else data_dir / "pdf",
            data_dir / "pdf_tmp",
            self.sources.keys,
        )
        self.pusher = Pusher(
            s.push.tasks,
            context,
            self.drawer,
            self.access,
            self.dispatcher,
            self.works,
            self.pdf,
            lambda tokens: self.requests.image("抽图", tokens),
            self.reserve,
        )
        self._preload: asyncio.Task | None = None
        self._warm_up: asyncio.Task | None = None
        self._feeds_warm_up: asyncio.Task | None = None
        self._started = False
        self._closed = False
        self._closing: asyncio.Task | None = None

    async def initialize(self):
        if self._started or self._closed or self._closing is not None:
            return
        self._started = True
        # 后台预加载标签库，避免第一次抽卡时等待下载
        self._preload = asyncio.create_task(self.tagdb.get())
        if self.settings.video.enabled:
            self.videos.prepare()
            self._warm_up = asyncio.create_task(self.videos.warm_up())
        if self.settings.sources.feeds.enabled and self.feeds.feeds:
            self._feeds_warm_up = asyncio.create_task(self.feeds.warm_up())
        self.reserve.start()
        self.video_reserve.start()
        self.pusher.start()

    async def terminate(self):
        if self._closed:
            return
        if self._closing is None or self._closing.done():
            self._closing = asyncio.create_task(self._terminate())
        # 所有关闭调用共用同一次清理，等待者取消时后台仍完成资源回收。
        await asyncio.shield(self._closing)

    async def _terminate(self):
        tasks = [
            task
            for task in (self._preload, self._warm_up, self._feeds_warm_up)
            if task is not None
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        # 同时停止各后台所有者，等待取消后的子任务和文件清理全部结束。
        results = await asyncio.gather(
            self.pusher.stop(),
            self.reserve.stop(),
            self.video_reserve.stop(),
            *tasks,
            return_exceptions=True,
        )
        errors = [result for result in results[:3] if isinstance(result, Exception)]
        for result in results[3:]:
            if isinstance(result, Exception):
                logger.warning(f"[random_pic] 预热任务已失败，继续关闭: {result!r}")
        # 先关闭下载任务的所有者，再关网络；一处失败也继续回收其余服务。
        for service in (
            self.feeds, self.pdf, self.videos, self.sources, self.history, self.http
        ):
            try:
                await service.close()
            except Exception as e:
                errors.append(e)
        if errors:
            raise errors[0]
        self._closed = True

    @staticmethod
    def _stage(share_dir: str) -> Stage | None:
        """配置了共享目录时图片、视频存在那里并以文件路径发送；目录建不起来时照旧编码进消息。"""
        if not share_dir:
            return None
        stage = Stage(Path(share_dir))
        try:
            stage.prepare()
        except OSError as e:
            logger.warning(f"[random_pic] 共享目录不可用，改为编码进消息发送: {e!r}")
            return None
        return stage

    def _reserve(self, root: Path) -> Reserve:
        """按默认图集数和每集张数维护二次元、三次元的擦边、R18 预备池（R18 群聊私聊都不允许时不维护）。"""
        s = self.settings
        explicit = s.access.group_r18 or s.access.private_r18
        batches = {
            (REAL, SENSITIVE): s.reserve.images.real_sensitive,
            (REAL, EXPLICIT): s.reserve.images.real_explicit if explicit else 0,
            (ANIME, SENSITIVE): s.reserve.images.anime_sensitive,
            (ANIME, EXPLICIT): s.reserve.images.anime_explicit if explicit else 0,
        }
        specs, allowed = {}, {}
        for (style, rating), n in batches.items():
            default = self.requests.image(STYLE_NAMES[style], [])
            min_pages = s.draw.min_pages if style == REAL else 0
            spec = Spec(style, rating, default.albums, default.per_album, min_pages)
            allowed[spec] = {
                source.key
                for source, _ in self.sources.drawing
                if source.style == style and rating in source.usable_ratings
            }
            # 没有启用源的风格·分级不维护
            specs[spec] = n if allowed[spec] else 0
        feed_keys = {source.key for source, _ in self.feeds.images}
        # 订阅帖子不套用网站作品页数门槛；实际图片不足时仍可保留。
        return Reserve(
            lambda req, allow: self.drawer.draw(req, allow),
            root,
            specs,
            allowed,
            min_pages_exempt=feed_keys,
            allow_short=feed_keys | {self.sources.danbooru.key},
            policy_key=self._reserve_policy(video=False),
        )

    def _video_reserve(self, root: Path) -> Reserve:
        """按默认个数维护二次元、三次元的擦边、R18 视频预备池（视频关闭时不维护，
        R18 群聊私聊都不允许时不维护 R18 的，没有视频源的风格和分级也不维护）。"""
        s = self.settings
        explicit = s.access.group_r18 or s.access.private_r18
        batches = {
            (REAL, SENSITIVE): s.reserve.videos.real_sensitive,
            (REAL, EXPLICIT): s.reserve.videos.real_explicit,
            (ANIME, SENSITIVE): s.reserve.videos.anime_sensitive,
            (ANIME, EXPLICIT): s.reserve.videos.anime_explicit,
        }
        count = self.requests.video([]).albums
        specs, allowed = {}, {}
        for (style, rating), n in batches.items():
            keys = {
                source.key
                for source, _ in self.videos.drawing
                if source.style == style and rating in source.usable_ratings
            }
            on = s.video.enabled and keys and (rating != EXPLICIT or explicit)
            spec = Spec(style, rating, count, 1)
            specs[spec] = n if on else 0
            allowed[spec] = keys
        return Reserve(
            self.video_drawer.draw, root, specs, allowed, move=True, label="视频·",
            policy_key=self._reserve_policy(video=True),
        )

    def _reserve_policy(self, *, video: bool) -> str:
        """摘要只包含内容筛选配置；凭证、发送样式和面板展开不使桶失效。"""
        s = self.settings
        sources = asdict(s.sources)
        feeds = sources.pop('feeds')
        video_sources = sources.pop('video')
        if video:
            sites = video_sources
        else:
            sites = sources
        for value in sites.values():
            if isinstance(value, dict):
                for key in (
                    'weight', 'ipb_member_id', 'ipb_pass_hash', 'igneous',
                    'manual_account', 'email', 'password',
                ):
                    value.pop(key, None)
        policy = {
            'filter': asdict(s.filter),
            'sites': sites,
            'feeds': feeds,
            'draw': {
                'page_pick': s.draw.page_pick,
                'explicit_skip': s.draw.explicit_skip,
                'min_pages': s.draw.min_pages,
            },
        }
        if video:
            policy['video'] = {
                'max_mb': s.video.max_mb,
                'max_seconds': s.video.max_seconds,
                'mmd': s.video.mmd,
            }
        data = json.dumps(policy, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(data.encode('utf-8')).hexdigest()
