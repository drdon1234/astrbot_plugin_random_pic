"""CosplayTele 的视频（三次元 R18）：「Video Cosplay」分类里同时在「Cosplay Nude」的帖子。

2026-10 截帧目检：Nude 帖子的视频 8/8 是 R18（亚洲 coser，1080p~4K）；Ero 帖子的视频约一半露点、
有的带道具，分不出擦边，所以只取 Nude（约 730 帖）。分类用 categories[terms]=850,193 与
categories[operator]=AND 同时筛选，和图片一样能搜角色、作品名。

视频托管在 cossora.stream：帖子正文的 iframe 是播放页，播放页里的 videoURL 用 AES-256-CBC 加密
（密钥是同一页里 decryptLink(videoURL, '<32 个字符>') 的第二个参数，Base64 解码后前 16 字节是 IV），
解出带 token 的 HLS 地址（播放页要带 CosplayTele 的 Referer，播放列表和分片要带 cossora 的）。
视频多为 2~40 分钟、15 秒一个分片，只截 CLIP_SECONDS 秒，起点在全片 10%~90% 之间随机。
"""

import base64
import random
import re
from dataclasses import replace

from astrbot.api import logger

from ..models import EXPLICIT, Album, WorkRef
from ..net import HttpError
from ..sources.wordpress import COSPLAYTELE, WordPressSource, term_names
from ..util import duration_text
from .base import VideoFiles
from . import hls
from .hls import HlsDownloader, clip, decrypt

VIDEO_CATEGORY = 850  # Video Cosplay
NUDE_CATEGORY = 193  # Cosplay Nude
SITE = replace(
    COSPLAYTELE,
    key="cosplaytele_video",
    name="CosplayTele 视频",
    intro="亚洲 coser 的视频片段，可搜角色、作品名",
    ratings={NUDE_CATEGORY: EXPLICIT},
)
EMBED_RE = re.compile(r"""<iframe\b[^>]*\bsrc=["'](https://cossora\.stream/embed/[^"']+)""", re.I)
VIDEO_URL_RE = re.compile(r"""\bvideoURL\s*=\s*['"]([A-Za-z0-9+/=]+)['"]""")
KEY_RE = re.compile(r"""decryptLink\(\s*videoURL\s*,\s*['"]([^'"]+)['"]""")
PAGE_HEADERS = {"Referer": "https://cosplaytele.com/"}
STREAM_HEADERS = {"Referer": "https://cossora.stream/"}
# 截取的片段长度（秒），全片不比它长多少时整段都要
CLIP_SECONDS = 30
WHOLE_FACTOR = 1.5


def stream_url(page: str) -> str | None:
    """播放页里解密出的 HLS 地址，页面格式不对时返回 None。"""
    data, key = VIDEO_URL_RE.search(page), KEY_RE.search(page)
    if not data or not key:
        return None
    raw = base64.b64decode(data.group(1))
    if len(raw) <= 16 or len(key.group(1).encode()) not in (16, 24, 32):
        return None
    url = decrypt(raw[16:], key.group(1).encode(), raw[:16]).decode(errors="replace")
    return url if url.startswith("http") else None


class CosplayTeleVideoSource(WordPressSource):
    def __init__(self, http, cache, content, opts, files: VideoFiles):
        super().__init__(SITE, http, cache, content, opts)
        self.link_re = None
        self.files = files
        self.hls = HlsDownloader(http, files.max_bytes)

    @property
    def unavailable(self) -> str | None:
        return None if hls.ffmpeg_path() else "没有安装 ffmpeg"

    def query(self, rating: str) -> dict[str, str] | None:
        if rating != EXPLICIT:
            return None
        params = {
            "categories[terms]": f"{VIDEO_CATEGORY},{NUDE_CATEGORY}",
            "categories[operator]": "AND",
        }
        categories, tags = self._excludes()
        if categories:
            params["categories_exclude"] = ",".join(map(str, categories))
        if tags:
            params["tags_exclude"] = ",".join(map(str, tags))
        return params

    def _images(self, post: dict) -> list[str]:
        """帖子里的视频播放页（沿用图片源的流程，「图片」就是播放页）。"""
        content = str((post.get("content") or {}).get("rendered") or "")
        return list(dict.fromkeys(EMBED_RE.findall(content)))

    async def _album(self, post: dict, embeds: list[str], n, semaphore) -> Album | None:
        embed = random.choice(embeds)
        async with semaphore:
            page = await self.http.get_text(embed, headers=PAGE_HEADERS)
            url = stream_url(page)
            if url is None:
                logger.warning(f"[random_pic] {self.name} 播放页里找不到视频地址: {embed}")
                return None
            try:
                media, variant = await self.hls.playlist(url, STREAM_HEADERS)
            except HttpError as e:
                logger.warning(f"[random_pic] {self.name} 播放列表出错 {embed}: {e}")
                return None
            total = media.duration
            if total <= CLIP_SECONDS * WHOLE_FACTOR:
                start, segments = 0.0, media.segments
            else:
                start = random.uniform(total * 0.1, max(total * 0.1, total * 0.9 - CLIP_SECONDS))
                segments = clip(media, start, CLIP_SECONDS)
                start = sum(s.duration for s in media.segments[: media.segments.index(segments[0])])
            path = await self.hls.download(
                media, variant, segments, self.files.new_path(), STREAM_HEADERS
            )
        if path is None:
            return None
        _, tags = term_names(post)
        details = []
        if tags:
            details.append(f"标签：{'、'.join(tags[:4])}")
        if segments is not media.segments:
            details.append(f"片段：{duration_text(start)} 起（全片 {duration_text(total)}）")
        if post.get("link"):
            details.append(f"帖子：{post['link']}")
        return Album(
            source=self.name,
            title=self._title(post),
            total=1,
            pictures=[(1, path)],
            details=details,
            work=WorkRef(self.key, str(post.get("id"))),
            duration=sum(s.duration for s in segments),
        )

