"""整本 PDF：把图片按顺序写成 PDF（每张图一页），以及打包结果的复用与清理。

每页按配置的质量转成 JPEG 嵌入（PDF 只能直接放 JPEG）。同一时间只有一张图在内存里，
几百页的画廊也不会占用太多内存。
"""

import asyncio
import io
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..models import Work
from ..utils import run_thread, shared

COLOR_SPACES = {"RGB": "DeviceRGB", "L": "DeviceGray"}
# 不压缩时，不是 JPEG 的页面仍要转成 JPEG，用这个质量
ORIGINAL_QUALITY = 95


def _flatten(im: Image.Image) -> Image.Image:
    """取第一帧，透明部分铺白底，转成 JPEG 能存的 RGB 或灰度。"""
    im.seek(0)
    if im.mode == "L":
        return im.copy()
    if im.mode in ("RGBA", "LA", "P", "PA"):
        rgba = im.convert("RGBA")
        rgb = Image.new("RGB", rgba.size, (255, 255, 255))
        rgb.paste(rgba, mask=rgba.split()[-1])
        return rgb
    return im.convert("RGB")


def to_jpeg(path: Path, quality: int) -> tuple[bytes | None, int, int, str]:
    """把图片编码成 JPEG，返回 (数据, 宽, 高, 模式 RGB / L)。

    quality 为 0 表示不压缩：JPEG 原样使用，其他格式按 ORIGINAL_QUALITY 转换。
    原图已经是 JPEG、重新编码后不会更小时也原样使用。原样使用时数据为 None。
    """
    with Image.open(path) as im:
        jpeg = im.format == "JPEG" and im.mode in ("RGB", "L")
        if jpeg and quality <= 0:
            return None, im.width, im.height, im.mode
        flat = _flatten(im)
    buf = io.BytesIO()
    flat.save(buf, "JPEG", quality=quality if quality > 0 else ORIGINAL_QUALITY)
    if jpeg and buf.tell() >= path.stat().st_size:
        return None, flat.width, flat.height, flat.mode
    return buf.getvalue(), flat.width, flat.height, flat.mode


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
# 单个 PDF 最多页数（超过时分卷）、页面的 JPEG 质量、最多保留多少个作品的 PDF
PAGES_PER_FILE = 200
JPEG_QUALITY = 85
KEEP_WORKS = 10
# 这么多秒内生成的 PDF 不清理：可能是另一个打包刚完成、还没发出去的文件
FRESH_SECONDS = 600
UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


class PdfError(Exception):
    pass


@dataclass(frozen=True)
class PdfFile:
    path: Path
    name: str
    pages: int


def page_count(path: Path) -> int:
    """读取本插件固定格式的页树，避免把来源的估计页数当成 PDF 实际页数。"""
    with path.open('rb') as stream:
        header = stream.read(65536)
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 16))
        end = stream.read()
    tree = re.search(
        rb'2 0 obj\n<< /Type /Pages /Kids \[(.*?)\] /Count (\d+) >>\nendobj',
        header,
    )
    if not header.startswith(b'%PDF-1.4\n') or tree is None or not end.rstrip().endswith(b'%%EOF'):
        raise ValueError('PDF 缓存不完整或格式无效')
    count = int(tree[2])
    if count <= 0 or len(re.findall(rb'\d+ 0 R', tree[1])) != count:
        raise ValueError('PDF 缓存页树无效')
    return count


# 下载整个作品：(作品, 目标目录) → (按页码排序、以页码命名的图片, 失败页数)
Download = Callable[[Work, Path], Awaitable[tuple[list[Path], int]]]


def display_name(title: str, key: str, pages: tuple[int, int], parts: int) -> str:
    """发送给用户的文件名；分卷时带上页码范围。"""
    name = UNSAFE_FILENAME.sub(" ", title).strip()[:80] or key
    if parts > 1:
        name += f" ({pages[0]}-{pages[1]})"
    return f"{name}.pdf"


def own_pdf_pattern(sources: Iterable[str]) -> re.Pattern:
    """本插件存储的 PDF：作品键[-incomplete][-第几卷of共几卷].pdf，作品键是「图源_id」
    （id 是数字，哔咔是十六进制）。只认这些名字，不会误删共享目录里的其他 PDF。"""
    keys = "|".join(map(re.escape, sources))
    return re.compile(rf"((?:{keys})_[0-9a-f]+)(?:-incomplete)?(?:-\d+of\d+)?\.pdf")


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
        self,
        out_dir: Path,
        tmp_dir: Path,
        sources: Iterable[str],
        pages_per_file: int = PAGES_PER_FILE,
        keep: int = KEEP_WORKS,
        quality: int = JPEG_QUALITY,
    ):
        """sources 是所有图源键，用来认出本插件生成的文件；quality 为 0 表示不压缩。"""
        self.dir = out_dir
        self.tmp = tmp_dir
        self.pages_per_file = pages_per_file
        self.keep = keep
        self.quality = quality
        self.own = own_pdf_pattern(sources)
        self._slots = asyncio.Semaphore(PACK_CONCURRENCY)
        # 用户 → (锁, 正在使用的请求数)，没有请求时删除
        self._users: dict[str, tuple[asyncio.Lock, int]] = {}
        # 作品键 → 正在进行的打包
        self._builds: dict[str, asyncio.Future] = {}
        self._closed = False
        self._closing: asyncio.Task | None = None

    def building(self, work: Work) -> bool:
        return work.ref.key in self._builds

    async def get(
        self, work: Work, user_id: str, download: Download
    ) -> tuple[list[PdfFile], int]:
        """返回作品的 PDF：(文件列表, 失败页数)。没有完整的缓存时排队打包。"""
        if self._closed:
            raise PdfError("PDF 服务已关闭")
        return await shared(
            self._builds,
            work.ref.key,
            lambda: self._queued(work, user_id, download),
        )

    async def close(self):
        """拒绝新请求，取消并等待所有共享构建及其实际磁盘工作结束。"""
        self._closed = True
        if self._closing is None:
            async def stop():
                tasks = list(self._builds.values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            self._closing = asyncio.create_task(stop())
        try:
            await asyncio.shield(self._closing)
        except asyncio.CancelledError:
            while not self._closing.done():
                try:
                    await asyncio.shield(self._closing)
                except asyncio.CancelledError:
                    continue
            raise

    async def _queued(
        self, work: Work, user_id: str, download: Download
    ) -> tuple[list[PdfFile], int]:
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

    def cached(self, work: Work) -> list[PdfFile] | None:
        """仅复用分卷齐全且实际页数与当前作品一致的缓存。"""
        key = work.ref.key
        parts = self.parts(work.pages)
        files = []
        for part in range(1, parts + 1):
            path = self.dir / stored_name(key, part, parts)
            try:
                count = page_count(path)
            except (OSError, ValueError):
                return None
            start = (part - 1) * self.pages_per_file + 1
            end = min(start + self.pages_per_file - 1, work.pages)
            if count != end - start + 1:
                return None
            files.append(
                PdfFile(path, display_name(work.title, key, (start, end), parts), count)
            )
        return files or None

    async def build(
        self, work: Work, download: Download
    ) -> tuple[list[PdfFile], int]:
        """下载整个作品，每 pages_per_file 页写成一个 PDF。

        返回 (带实际页数的文件列表, 失败页数)。有缺页时存储名带 -incomplete，不会被当成缓存复用。
        """
        key = work.ref.key
        tmp = self.tmp / key
        packed = self.dir / f".{key}-{uuid.uuid4().hex}"
        published: list[Path] = []
        try:
            await run_thread(shutil.rmtree, tmp, ignore_errors=True)
            paths, missing = await download(work, tmp)
            if not paths:
                raise PdfError("没有下载到任何图片")
            self.dir.mkdir(parents=True, exist_ok=True)
            packed.mkdir()
            chunks = [
                paths[i : i + self.pages_per_file]
                for i in range(0, len(paths), self.pages_per_file)
            ]
            files = []
            for part, chunk in enumerate(chunks, 1):
                out = self.dir / stored_name(key, part, len(chunks), bool(missing))
                await run_thread(write_pdf, chunk, packed / out.name, self.quality)
                # 下载的图片以页码命名，分卷文件名标出实际页码范围
                pages = (int(chunk[0].stem), int(chunk[-1].stem))
                files.append(
                    PdfFile(out, display_name(work.title, key, pages, len(chunks)), len(chunk))
                )
            # 全部分卷成功后再发布；临时 PDF 与输出同目录，支持跨文件系统的图片目录。
            # 先移除同作品旧卷；发布途中进程退出也只会留下不完整的新卷，不混入旧卷。
            for path in self.dir.glob('*.pdf'):
                match = self.own.fullmatch(path.name)
                if match and match.group(1) == key:
                    path.unlink(missing_ok=True)
            for file in files:
                (packed / file.path.name).replace(file.path)
                published.append(file.path)
        except BaseException:
            for path in published:
                path.unlink(missing_ok=True)
            raise
        finally:
            def cleanup():
                for directory in (tmp, packed):
                    shutil.rmtree(directory, ignore_errors=True)

            await run_thread(cleanup)
        self._prune(keep=key)
        return files, missing

    def _prune(self, keep: str):
        """按作品清理旧 PDF，只动本插件生成的文件（输出目录可能是共享目录）。"""
        groups: dict[str, list[Path]] = {}
        for path in self.dir.glob("*.pdf"):
            match = self.own.fullmatch(path.name)
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
