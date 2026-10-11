"""订阅帖子与视频清晰度的数据模型。"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# 视频按清晰度挑选时长边不超过这么多像素的优先（更大的发 QQ 也只是被压缩）
MAX_VIDEO_SIDE = 1920


@dataclass
class Clip:
    """帖子里的一个视频：variants 是可下载的 mp4 [地址, 长边像素, 码率]（未知为 0）。"""

    variants: list[tuple[str, int, int]]
    duration: float | None = None

    def order(self, max_bytes: int | None) -> list[str]:
        """下载时依次尝试的地址：长边不超过 MAX_VIDEO_SIDE、估计大小不超过上限的优先，其中码率高的优先。"""

        def too_big(bitrate: int) -> bool:
            return bool(
                max_bytes
                and bitrate
                and self.duration
                and bitrate * self.duration / 8 > max_bytes
            )

        ranked = sorted(
            self.variants,
            key=lambda v: (v[1] > MAX_VIDEO_SIDE, too_big(v[2]), -v[2]),
        )
        return list(dict.fromkeys(url for url, _, _ in ranked))


@dataclass
class Post:
    """一个帖子（一条推文、一条频道消息或一组相册）。"""

    id: str
    text: str = ""
    photos: list[str] = field(default_factory=list)
    clips: list[Clip] = field(default_factory=list)
    time: float | None = None  # 发布时间（Unix 秒）
    likes: int | None = None
    sensitive: bool = False  # 平台标成敏感内容（X 的 possibly_sensitive）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Post":
        clips = [
            Clip([tuple(v) for v in c.get("variants") or []], c.get("duration"))
            for c in data.get("clips") or []
        ]
        return cls(
            id=str(data["id"]),
            text=str(data.get("text") or ""),
            photos=[str(p) for p in data.get("photos") or []],
            clips=clips,
            time=data.get("time"),
            likes=data.get("likes"),
            sensitive=bool(data.get("sensitive")),
        )

    @property
    def date(self) -> str:
        if not self.time:
            return ""
        return datetime.fromtimestamp(self.time, timezone.utc).strftime("%Y-%m-%d")
