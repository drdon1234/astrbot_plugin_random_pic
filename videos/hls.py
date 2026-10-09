"""HLS（m3u8）视频下载：选清晰度、取一段分片（可用 AES-128 解密）、用 ffmpeg 封装成 mp4。

SOOP 和 CosplayTele 的视频只有 HLS：
- SOOP 是 fMP4 分片（EXT-X-MAP 的 init 段 + .m4s），整段几十秒，全下；
- CosplayTele（cossora.stream）是 15 秒一个的 TS 分片，新视频用 AES-128 加密并把分片伪装成 .png
  （ffmpeg 按扩展名拒收，所以分片都由这里下载、解密后拼起来，ffmpeg 只做本地封装）。
  视频长 2~40 分钟、源画质 1080p~4K，只截一段，太大或分辨率太高时转码成 720p 级别。

播放列表里没带查询参数的相对地址沿用播放列表地址的查询参数（cossora 的密钥地址要带 token）。
"""

import asyncio
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from astrbot.api import logger

from ..net import NETWORK_ERRORS, HttpClient, HttpError
from .base import DOWNLOAD_TIMEOUT, MB

# 选清晰度时长边不超过这么多像素（再高的发到 QQ 也看不出区别，文件却大得多）
MAX_SIDE = 1920
# 转码后的长边
TRANSCODE_SIDE = 1280
# 同时下载的分片数
SEGMENT_CONCURRENCY = 3
FFMPEG_TIMEOUT = 300
# 下载的分片总大小超过上限这么多倍时不再转码，直接放弃（转码一般能压到 1/3 以下）
RAW_FACTOR = 4

ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _attrs(text: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in ATTR_RE.findall(text)}


def resolve(base: str, uri: str) -> str:
    """相对地址转绝对地址；相对地址没有查询参数时沿用 base 的。"""
    url = urljoin(base, uri)
    query = urlsplit(base).query
    if query and "://" not in uri and "?" not in uri:
        url = f"{url}?{query}"
    return url


@dataclass
class Variant:
    url: str
    bandwidth: int = 0
    width: int = 0
    height: int = 0

    @property
    def long_side(self) -> int:
        return max(self.width, self.height)


@dataclass
class Segment:
    url: str
    duration: float
    sequence: int


@dataclass
class Media:
    segments: list[Segment] = field(default_factory=list)
    init: str | None = None
    key: str | None = None  # AES-128 密钥地址
    iv: bytes | None = None  # None 时用分片序号

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)


def parse_master(text: str, base: str) -> list[Variant]:
    variants = []
    lines = [line.strip() for line in text.splitlines()]
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        uri = next((u for u in lines[i + 1 :] if u and not u.startswith("#")), None)
        if uri is None:
            continue
        attrs = _attrs(line.split(":", 1)[1])
        width, _, height = attrs.get("RESOLUTION", "").partition("x")
        variants.append(
            Variant(
                resolve(base, uri),
                int(attrs.get("BANDWIDTH") or 0),
                int(width) if width.isdigit() else 0,
                int(height) if height.isdigit() else 0,
            )
        )
    return variants


def pick_variant(variants: list[Variant]) -> Variant:
    """长边不超过 MAX_SIDE 的里面最清楚的；都超过时取最小的（不知道分辨率的按码率）。"""
    fits = [v for v in variants if v.long_side <= MAX_SIDE]
    if fits:
        return max(fits, key=lambda v: (v.long_side, v.bandwidth))
    return min(variants, key=lambda v: (v.long_side, v.bandwidth))


def parse_media(text: str, base: str) -> Media:
    media = Media()
    sequence = 0
    duration = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1] or 0)
        elif line.startswith("#EXT-X-MAP:"):
            uri = _attrs(line.split(":", 1)[1]).get("URI")
            media.init = resolve(base, uri) if uri else None
        elif line.startswith("#EXT-X-KEY:"):
            attrs = _attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE")
            if method == "NONE":
                media.key = None
            elif method == "AES-128" and attrs.get("URI"):
                media.key = resolve(base, attrs["URI"])
                iv = attrs.get("IV", "")
                media.iv = bytes.fromhex(iv[2:].rjust(32, "0")) if iv[:2].lower() == "0x" else None
            else:
                raise HttpError(f"不支持的加密方式 {method}")
        elif line.startswith("#EXTINF:"):
            value = line.split(":", 1)[1].split(",", 1)[0]
            try:
                duration = float(value)
            except ValueError:
                duration = 0.0
        elif line and not line.startswith("#"):
            media.segments.append(Segment(resolve(base, line), duration, sequence))
            sequence += 1
            duration = 0.0
    return media


def clip(media: Media, start: float, seconds: float | None) -> list[Segment]:
    """从 start 秒所在的分片起，凑够 seconds 秒的分片（None 为到结尾）。"""
    picked, at, total = [], 0.0, 0.0
    for segment in media.segments:
        end = at + segment.duration
        if end > start:
            picked.append(segment)
            total += segment.duration
            if seconds is not None and total >= seconds:
                break
        at = end
    return picked or media.segments[-1:]


def decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    pad = plain[-1] if plain else 0
    return plain[:-pad] if 0 < pad <= 16 else plain


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


async def run_ffmpeg(args: list[str], timeout: float = FFMPEG_TIMEOUT) -> bool:
    exe = ffmpeg_path()
    if exe is None:
        return False
    proc = await asyncio.create_subprocess_exec(
        exe,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        logger.warning(
            f"[random_pic] ffmpeg 失败: {err.decode(errors='replace')[-300:].strip()}"
        )
        return False
    return True


def scale_filter(side: int) -> str:
    """长边缩到不超过 side，短边按比例（取偶数）。"""
    return (
        f"scale='if(gte(iw,ih),min({side},iw),-2)':'if(gte(iw,ih),-2,min({side},ih))'"
    )


class HlsDownloader:
    def __init__(self, http: HttpClient, max_bytes: int | None):
        self.http = http
        self.max_bytes = max_bytes

    async def _bytes(self, url: str, headers: dict | None) -> bytes:
        async with self.http.request(
            "GET", url, headers=headers, timeout=DOWNLOAD_TIMEOUT
        ) as resp:
            if resp.status != 200:
                raise HttpError(f"HTTP {resp.status}", resp.status)
            return await resp.read()

    async def playlist(
        self, url: str, headers: dict | None = None
    ) -> tuple[Media, Variant]:
        """播放列表（主列表时选一个清晰度），返回 (分片列表, 选中的清晰度)。"""
        text = await self.http.get_text(url, headers=headers)
        variant = Variant(url)
        if "#EXT-X-STREAM-INF" in text:
            variants = parse_master(text, url)
            if not variants:
                raise HttpError("主播放列表里没有清晰度")
            variant = pick_variant(variants)
            text = await self.http.get_text(variant.url, headers=headers)
        media = parse_media(text, variant.url)
        if not media.segments:
            raise HttpError("播放列表里没有分片")
        return media, variant

    async def download(
        self,
        media: Media,
        variant: Variant,
        segments: list[Segment],
        dest: Path,
        headers: dict | None = None,
    ) -> Path | None:
        """下载分片拼成 dest（mp4）；太大或分辨率太高时转码。失败返回 None。"""
        raw = dest.with_name(dest.name + ".part")
        ok = False
        try:
            ok = await self._build(media, variant, segments, raw, dest, headers)
        except NETWORK_ERRORS + (OSError,) as e:
            logger.warning(f"[random_pic] 视频片段下载失败 {variant.url}: {e!r}")
        finally:
            # 失败、被取消时都删掉下了一半的文件
            raw.unlink(missing_ok=True)
            if not ok:
                dest.unlink(missing_ok=True)
        return dest if ok else None

    async def _build(
        self,
        media: Media,
        variant: Variant,
        segments: list[Segment],
        raw: Path,
        dest: Path,
        headers: dict | None,
    ) -> bool:
        if not await self._fetch(media, segments, raw, headers):
            return False
        if not await run_ffmpeg(
            ["-i", str(raw), "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(dest)]
        ):
            return False
        if self._too_big(dest) or variant.long_side > MAX_SIDE:
            if not await self._transcode(dest):
                return False
        if self._too_big(dest):
            logger.info(f"[random_pic] 视频片段转码后仍超过大小上限: {variant.url}")
            return False
        return True

    def _too_big(self, path: Path) -> bool:
        return (
            self.max_bytes is not None
            and path.exists()
            and path.stat().st_size > self.max_bytes
        )

    async def _fetch(
        self, media: Media, segments: list[Segment], raw: Path, headers: dict | None
    ) -> bool:
        key = await self._bytes(media.key, headers) if media.key else None
        if key is not None and len(key) != 16:
            raise HttpError(f"密钥长度不对（{len(key)} 字节）")
        semaphore = asyncio.Semaphore(SEGMENT_CONCURRENCY)

        async def one(segment: Segment) -> bytes:
            async with semaphore:
                data = await self._bytes(segment.url, headers)
            if key is not None:
                iv = media.iv or segment.sequence.to_bytes(16, "big")
                data = decrypt(data, key, iv)
            return data

        parts = await asyncio.gather(*map(one, segments))
        size = sum(len(p) for p in parts)
        if self.max_bytes is not None and size > self.max_bytes * RAW_FACTOR:
            logger.info(f"[random_pic] 视频片段 {size / MB:.0f} MB 太大，已跳过")
            return False
        with raw.open("wb") as f:
            if media.init:
                f.write(await self._bytes(media.init, headers))
            for part in parts:
                f.write(part)
        return True

    async def _transcode(self, path: Path) -> bool:
        out = path.with_name(path.name + ".small")
        try:
            ok = await run_ffmpeg(
                [
                    "-i", str(path),
                    "-vf", scale_filter(TRANSCODE_SIDE),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart",
                    "-f", "mp4",
                    str(out),
                ]
            )
            if ok:
                out.replace(path)
            return ok
        finally:
            out.unlink(missing_ok=True)

