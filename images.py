"""图片处理：判断是否彩图，按配置的质量转成 JPEG。

彩图判断：黑白漫画、线稿、灰度画的每个像素 RGB 三通道几乎相等；彩图则有相当比例的像素
三通道差值明显。实测：黑白页「三通道最大差 > 20 的像素占比」为 0.000，最素淡的
彩色画集页也有 0.17，所以阈值取 0.05。
"""

import io
from pathlib import Path

from PIL import Image, ImageChops

CHROMA_LEVEL = 20
COLOR_RATIO = 0.05
THUMB = (128, 128)
# 原图模式下，PDF 里不是 JPEG 的页面仍要转成 JPEG，用这个质量
ORIGINAL_QUALITY = 95


def colorful_ratio(path: Path) -> float:
    """三通道最大差超过 CHROMA_LEVEL 的像素占比（在缩略图上计算）。"""
    with Image.open(path) as im:
        im.seek(0)  # 动图只看第一帧
        rgb = im.convert("RGB")
    rgb.thumbnail(THUMB)
    r, g, b = rgb.split()
    high = ImageChops.lighter(ImageChops.lighter(r, g), b)
    low = ImageChops.darker(ImageChops.darker(r, g), b)
    histogram = ImageChops.difference(high, low).histogram()
    total = sum(histogram)
    return sum(histogram[CHROMA_LEVEL + 1 :]) / total if total else 0.0


def is_colorful(path: Path) -> bool:
    return colorful_ratio(path) >= COLOR_RATIO


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


def compress(path: Path, quality: int) -> Path:
    """发送前把图片转成指定质量的 JPEG，返回转换后的文件（原文件删除）。

    quality 为 0、动图，以及转换后不会更小的 JPEG 保持原样。
    """
    if quality <= 0:
        return path
    with Image.open(path) as im:
        if getattr(im, "is_animated", False):
            return path
    data, *_ = to_jpeg(path, quality)
    if data is None:
        return path
    out = path.with_suffix(".jpg")
    out.write_bytes(data)
    if out != path:
        path.unlink(missing_ok=True)
    return out
