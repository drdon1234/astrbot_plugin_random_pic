"""判断图片是否为彩色。

黑白漫画、线稿、灰度画的每个像素 RGB 三通道几乎相等；彩图则有相当比例的像素
三通道差值明显。实测：黑白页「三通道最大差 > 20 的像素占比」为 0.000，最素淡的
彩色画集页也有 0.17，所以阈值取 0.05。
"""

from pathlib import Path

from PIL import Image, ImageChops

CHROMA_LEVEL = 20
COLOR_RATIO = 0.05
THUMB = (128, 128)


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
