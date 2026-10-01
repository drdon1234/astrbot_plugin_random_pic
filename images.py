"""图片处理：整本 PDF 的页面转成 JPEG。"""

import io
from pathlib import Path

from PIL import Image

# 原图模式下，PDF 里不是 JPEG 的页面仍要转成 JPEG，用这个质量
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

    quality 为 0 表示原图：JPEG 原样使用，其他格式按 ORIGINAL_QUALITY 转换。
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
