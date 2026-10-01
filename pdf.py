"""把图片按顺序写成 PDF，每张图一页。

JPEG（RGB / 灰度）原样嵌入不重新编码；其他格式（webp、png、gif 等）逐张用 Pillow
转为 JPEG。同一时间只有一张图在内存里，几百页的画廊也不会占用太多内存。
"""

import io
from pathlib import Path

from PIL import Image

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
