"""整本 PDF：把图片按顺序写成 PDF（每张图一页），以及打包结果的复用与清理。

每页按配置的图片质量转成 JPEG 嵌入（见 images.to_jpeg）。同一时间只有一张图在内存里，
几百页的画廊也不会占用太多内存。
"""

import asyncio
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .images import to_jpeg
from .models import Work
from .util import shared

COLOR_SPACES = {"RGB": "DeviceRGB", "L": "DeviceGray"}


def write_pdf(images: list[Path], out: Path, quality: int) -> Path:
    """按 images 的顺序写 PDF，先写临时文件再改名，返回 out。quality 为 0 表示原图。"""
    if not images:
        raise ValueError("没有图片")
    count = len(images)
    # 对象编号：1 目录，2 页树，之后每页依次为 页面、内容流、图片
    page_ids = [3 + 3 * i for i in range(count)]
    offsets: dict[int, int] = {}
    tmp = out.with_suffix(".part")
    with open(tmp, "wb") as f:

        def write_obj(num: int, body: bytes):
            offsets[num] = f.tell()
            f.write(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")

        def stream(header: str, data: bytes) -> bytes:
            return (
                f"<< {header} /Length {len(data)} >>\nstream\n".encode()
                + data
                + b"\nendstream"
            )

        f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        write_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = " ".join(f"{p} 0 R" for p in page_ids)
        write_obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode())
        for page_id, path in zip(page_ids, images):
            data, width, height, mode = to_jpeg(path, quality)
            if data is None:
                data = path.read_bytes()
            content_id, image_id = page_id + 1, page_id + 2
            write_obj(
                image_id,
                stream(
                    f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
                    f"/ColorSpace /{COLOR_SPACES[mode]} /BitsPerComponent 8 /Filter /DCTDecode",
                    data,
                ),
            )
            draw = f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q".encode()
            write_obj(content_id, stream("", draw))
            write_obj(
                page_id,
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
                    f"/Resources << /XObject << /Im0 {image_id} 0 R >> >> "
                    f"/Contents {content_id} 0 R >>"
                ).encode(),
            )
        xref = f.tell()
        size = max(offsets) + 1
        f.write(f"xref\n0 {size}\n0000000000 65535 f \n".encode())
        for num in range(1, size):
            f.write(f"{offsets[num]:010d} 00000 n \n".encode())
        f.write(
            f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        )
    tmp.replace(out)
    return out


# 不同用户最多同时打包这么多个作品（同一用户的请求依次进行）。打包 E-Hentai 画廊时每页都要
# 请求一次，所有打包和抽图共用同一个请求限速，同时打包太多只会互相拖慢、更快耗尽图片额度
PACK_CONCURRENCY = 2
# 这么多秒内生成的 PDF 不清理：可能是另一个打包刚完成、还没发出去的文件
FRESH_SECONDS = 600
# 本插件存储的 PDF：作品键[-incomplete][-第几卷of共几卷].pdf，作品键是 E-Hentai 画廊号或
# 「图源_id」（id 是数字，哔咔是十六进制）。只认这些名字，不会误删共享目录里的其他 PDF
OWN_PDF = re.compile(
    r"(\d+|(?:pica|cosplaytele|xiuren|danbooru|jmcomic)_[0-9a-f]+)"
    r"(?:-incomplete)?(?:-\d+of\d+)?\.pdf"
)
UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


class PdfError(Exception):
    pass


# 下载整个作品：(作品, 目标目录) → (按页码排序、以页码命名的图片, 失败页数)
Download = Callable[[Work, Path], Awaitable[tuple[list[Path], int]]]


def display_name(title: str, key: str, pages: tuple[int, int], parts: int) -> str:
    """发送给用户的文件名；分卷时带上页码范围。"""
    name = UNSAFE_FILENAME.sub(" ", title).strip()[:80] or key
    if parts > 1:
        name += f" ({pages[0]}-{pages[1]})"
    return f"{name}.pdf"


def stored_name(key: str, part: int, parts: int, incomplete: bool = False) -> str:
    """存储用的文件名，只含作品键，便于识别缓存和清理。"""
    name = f"{key}-incomplete" if incomplete else key
    if parts > 1:
        name += f"-{part}of{parts}"
    return f"{name}.pdf"


class PdfStore:
    """整本 PDF 的生成、复用与清理。

    打包请求排队进行：同一作品正在打包时等它完成、共用结果；同一用户的请求依次进行；
    不同用户最多同时打包 PACK_CONCURRENCY 个。
    """

    def __init__(
        self, out_dir: Path, tmp_dir: Path, pages_per_file: int, keep: int, quality: int
    ):
        self.dir = out_dir
        self.tmp = tmp_dir
        self.pages_per_file = pages_per_file
        self.keep = keep
        self.quality = quality
        self._slots = asyncio.Semaphore(PACK_CONCURRENCY)
        # 用户 → (锁, 正在使用的请求数)，没有请求时删除
        self._users: dict[str, tuple[asyncio.Lock, int]] = {}
        # 作品键 → 正在进行的打包
        self._builds: dict[str, asyncio.Future] = {}

    def building(self, work: Work) -> bool:
        return work.ref.key in self._builds

    async def get(
        self, work: Work, user_id: str, download: Download
    ) -> tuple[list[tuple[Path, str]], int]:
        """返回作品的 PDF：(文件列表, 失败页数)。没有完整的缓存时排队打包。"""
        return await shared(
            self._builds,
            work.ref.key,
            lambda: self._queued(work, user_id, download),
        )

    async def _queued(
        self, work: Work, user_id: str, download: Download
    ) -> tuple[list[tuple[Path, str]], int]:
        lock, users = self._users.get(user_id, (asyncio.Lock(), 0))
        self._users[user_id] = (lock, users + 1)
        try:
            async with lock, self._slots:
                # 排队期间可能已经有人打包好了
                files = self.cached(work)
                if files is not None:
                    return files, 0
                return await self.build(work, download)
        finally:
            lock, users = self._users[user_id]
            if users > 1:
                self._users[user_id] = (lock, users - 1)
            else:
                del self._users[user_id]

    def parts(self, pages: int) -> int:
        return -(-pages // self.pages_per_file)

    def cached(self, work: Work) -> list[tuple[Path, str]] | None:
        """完整打包过的作品直接复用；缺任何一个分卷都视为没有缓存。"""
        key = work.ref.key
        parts = self.parts(work.pages)
        files = []
        for part in range(1, parts + 1):
            path = self.dir / stored_name(key, part, parts)
            if not path.exists():
                return None
            start = (part - 1) * self.pages_per_file + 1
            end = min(start + self.pages_per_file - 1, work.pages)
            files.append((path, display_name(work.title, key, (start, end), parts)))
        return files or None

    async def build(
        self, work: Work, download: Download
    ) -> tuple[list[tuple[Path, str]], int]:
        """下载整个作品，每 pages_per_file 页写成一个 PDF。

        返回 ([(PDF 路径, 发送文件名)], 失败页数)。有缺页时存储名带 -incomplete，不会被当成缓存复用。
        """
        key = work.ref.key
        tmp = self.tmp / key
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            paths, missing = await download(work, tmp)
            if not paths:
                raise PdfError("没有下载到任何图片")
            self.dir.mkdir(parents=True, exist_ok=True)
            chunks = [
                paths[i : i + self.pages_per_file]
                for i in range(0, len(paths), self.pages_per_file)
            ]
            files = []
            for part, chunk in enumerate(chunks, 1):
                out = self.dir / stored_name(key, part, len(chunks), bool(missing))
                await asyncio.to_thread(write_pdf, chunk, out, self.quality)
                # 下载的图片以页码命名，分卷文件名标出实际页码范围
                pages = (int(chunk[0].stem), int(chunk[-1].stem))
                files.append((out, display_name(work.title, key, pages, len(chunks))))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._prune(keep=key)
        return files, missing

    def _prune(self, keep: str):
        """按作品清理旧 PDF，只动本插件生成的文件（输出目录可能是共享目录）。"""
        groups: dict[str, list[Path]] = {}
        for path in self.dir.glob("*.pdf"):
            match = OWN_PDF.fullmatch(path.name)
            if match:
                groups.setdefault(match.group(1), []).append(path)
        newest = {
            key: max(p.stat().st_mtime for p in paths) for key, paths in groups.items()
        }
        others = sorted(
            (key for key in groups if key != keep), key=newest.get, reverse=True
        )
        fresh_after = time.time() - FRESH_SECONDS
        for key in others[self.keep - 1 :]:
            if newest[key] > fresh_after or key in self._builds:
                continue
            for path in groups[key]:
                path.unlink(missing_ok=True)
