"""整本 PDF：把图片按顺序写成 PDF（每张图一页），以及打包结果的复用与清理。

JPEG（RGB / 灰度）原样嵌入不重新编码；其他格式（webp、png、gif 等）逐张用 Pillow
转为 JPEG。同一时间只有一张图在内存里，几百页的画廊也不会占用太多内存。
"""

import asyncio
import io
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from PIL import Image

from .sources.ehentai_api import Gallery
from .util import shared

JPEG_QUALITY = 90


def jpeg_data(path: Path) -> tuple[bytes, int, int, str]:
    """返回 (JPEG 数据, 宽, 高, PDF 色彩空间)。"""
    with Image.open(path) as im:
        if im.format == "JPEG" and im.mode in ("RGB", "L"):
            space = "DeviceRGB" if im.mode == "RGB" else "DeviceGray"
            return path.read_bytes(), im.width, im.height, space
        im.seek(0)  # 动图只取第一帧
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, (255, 255, 255))
            rgb.paste(rgba, mask=rgba.split()[-1])
        else:
            rgb = im.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=JPEG_QUALITY)
        return buf.getvalue(), rgb.width, rgb.height, "DeviceRGB"


def write_pdf(images: list[Path], out: Path) -> Path:
    """按 images 的顺序写 PDF，先写临时文件再改名，返回 out。"""
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
            data, width, height, space = jpeg_data(path)
            content_id, image_id = page_id + 1, page_id + 2
            write_obj(
                image_id,
                stream(
                    f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
                    f"/ColorSpace /{space} /BitsPerComponent 8 /Filter /DCTDecode",
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


# 不同用户最多同时打包这么多个画廊（同一用户的请求依次进行）。打包时每页都要请求一次
# E-Hentai，所有打包和抽图共用同一个请求限速，同时打包太多只会互相拖慢、更快耗尽图片额度
PACK_CONCURRENCY = 2
# 这么多秒内生成的 PDF 不清理：可能是另一个打包刚完成、还没发出去的文件
FRESH_SECONDS = 600
# 本插件存储的 PDF：画廊号[-incomplete][-第几卷of共几卷].pdf
OWN_PDF = re.compile(r"(\d+)(?:-incomplete)?(?:-\d+of\d+)?\.pdf")
UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


class PdfError(Exception):
    pass


# 下载整个画廊：(画廊, 目标目录) → (按页码排序、以页码命名的图片, 失败页数)
Download = Callable[[Gallery, Path], Awaitable[tuple[list[Path], int]]]


def display_name(title: str, gid: int, pages: tuple[int, int], parts: int) -> str:
    """发送给用户的文件名；分卷时带上页码范围。"""
    name = UNSAFE_FILENAME.sub(" ", title).strip()[:80] or str(gid)
    if parts > 1:
        name += f" ({pages[0]}-{pages[1]})"
    return f"{name}.pdf"


def stored_name(gid: int, part: int, parts: int, incomplete: bool = False) -> str:
    """存储用的文件名，只含画廊号，便于识别缓存和清理。"""
    name = f"{gid}-incomplete" if incomplete else str(gid)
    if parts > 1:
        name += f"-{part}of{parts}"
    return f"{name}.pdf"


class PdfStore:
    """整本 PDF 的生成、复用与清理。

    打包请求排队进行：同一画廊正在打包时等它完成、共用结果；同一用户的请求依次进行；
    不同用户最多同时打包 PACK_CONCURRENCY 个。
    """

    def __init__(self, out_dir: Path, tmp_dir: Path, pages_per_file: int, keep: int):
        self.dir = out_dir
        self.tmp = tmp_dir
        self.pages_per_file = pages_per_file
        self.keep = keep
        self._slots = asyncio.Semaphore(PACK_CONCURRENCY)
        # 用户 → (锁, 正在使用的请求数)，没有请求时删除
        self._users: dict[str, tuple[asyncio.Lock, int]] = {}
        # 画廊号 → 正在进行的打包
        self._builds: dict[int, asyncio.Future] = {}

    def building(self, gid: int) -> bool:
        return gid in self._builds

    async def get(
        self, gallery: Gallery, user_id: str, download: Download
    ) -> tuple[list[tuple[Path, str]], int]:
        """返回画廊的 PDF：(文件列表, 失败页数)。没有完整的缓存时排队打包。"""
        return await shared(
            self._builds,
            gallery.gid,
            lambda: self._queued(gallery, user_id, download),
        )

    async def _queued(
        self, gallery: Gallery, user_id: str, download: Download
    ) -> tuple[list[tuple[Path, str]], int]:
        lock, users = self._users.get(user_id, (asyncio.Lock(), 0))
        self._users[user_id] = (lock, users + 1)
        try:
            async with lock, self._slots:
                # 排队期间可能已经有人打包好了
                files = self.cached(gallery)
                if files is not None:
                    return files, 0
                return await self.build(gallery, download)
        finally:
            lock, users = self._users[user_id]
            if users > 1:
                self._users[user_id] = (lock, users - 1)
            else:
                del self._users[user_id]

    def parts(self, filecount: int) -> int:
        return -(-filecount // self.pages_per_file)

    def cached(self, gallery: Gallery) -> list[tuple[Path, str]] | None:
        """完整打包过的画廊直接复用；缺任何一个分卷都视为没有缓存。"""
        parts = self.parts(gallery.filecount)
        files = []
        for part in range(1, parts + 1):
            path = self.dir / stored_name(gallery.gid, part, parts)
            if not path.exists():
                return None
            start = (part - 1) * self.pages_per_file + 1
            end = min(start + self.pages_per_file - 1, gallery.filecount)
            files.append(
                (path, display_name(gallery.title, gallery.gid, (start, end), parts))
            )
        return files or None

    async def build(
        self, gallery: Gallery, download: Download
    ) -> tuple[list[tuple[Path, str]], int]:
        """下载整个画廊，每 pages_per_file 页写成一个 PDF。

        返回 ([(PDF 路径, 发送文件名)], 失败页数)。有缺页时存储名带 -incomplete，不会被当成缓存复用。
        """
        tmp = self.tmp / str(gallery.gid)
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            paths, missing = await download(gallery, tmp)
            if not paths:
                raise PdfError("没有下载到任何图片")
            self.dir.mkdir(parents=True, exist_ok=True)
            chunks = [
                paths[i : i + self.pages_per_file]
                for i in range(0, len(paths), self.pages_per_file)
            ]
            files = []
            for part, chunk in enumerate(chunks, 1):
                out = self.dir / stored_name(
                    gallery.gid, part, len(chunks), bool(missing)
                )
                await asyncio.to_thread(write_pdf, chunk, out)
                # 下载的图片以页码命名，分卷文件名标出实际页码范围
                pages = (int(chunk[0].stem), int(chunk[-1].stem))
                files.append(
                    (out, display_name(gallery.title, gallery.gid, pages, len(chunks)))
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._prune(keep=gallery.gid)
        return files, missing

    def _prune(self, keep: int):
        """按画廊清理旧 PDF，只动本插件生成的文件（输出目录可能是共享目录）。"""
        groups: dict[int, list[Path]] = {}
        for path in self.dir.glob("*.pdf"):
            match = OWN_PDF.fullmatch(path.name)
            if match:
                groups.setdefault(int(match.group(1)), []).append(path)
        newest = {
            gid: max(p.stat().st_mtime for p in paths) for gid, paths in groups.items()
        }
        others = sorted(
            (gid for gid in groups if gid != keep), key=newest.get, reverse=True
        )
        fresh_after = time.time() - FRESH_SECONDS
        for gid in others[self.keep - 1 :]:
            if newest[gid] > fresh_after or gid in self._builds:
                continue
            for path in groups[gid]:
                path.unlink(missing_ok=True)
