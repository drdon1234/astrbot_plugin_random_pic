"""消息发送的模式、限制、类型与异常。"""

from collections.abc import Awaitable, Callable

import astrbot.api.message_components as Comp


def media_count(chain: list) -> int:
    """统计实际提交的媒体数；标题、错误说明和文件不作为图片或视频计数。"""
    total = 0
    for part in chain:
        if isinstance(part, (Comp.Image, Comp.Video)):
            total += 1
        elif isinstance(part, Comp.Nodes):
            total += sum(media_count(node.content) for node in part.nodes)
        elif isinstance(part, Comp.Node):
            total += media_count(part.content)
    return total


FORWARD = "合并转发"


MIXED = "图文混合"


SEPARATE = "逐条发送"


ONEBOT = "aiocqhttp"


# 图文混合时每条消息最多放这么多张图，太多时 QQ 可能发送失败
MIXED_PER_MESSAGE = 10


# 一条合并转发最多这么多个节点（QQ 的上限）
FORWARD_MAX_NODES = 100


# 图片编码（base64）进消息时，一条消息里图片原文件的总字节数上限。
# NapCat 的 WebSocket 单条上限是 50 MB，超过时连接被断开、发送要等到超时才失败；base64 会大三分之一
INLINE_BUDGET = 30 * 1024 * 1024


# 一条消息：(消息段, 其中的图集序号)
Message = tuple[list, list[int]]


# 发送一条消息，返回消息 ID（拿不到时为 None），发送接口报错时抛出 SendFailed
Send = Callable[[list], Awaitable[str | None]]


class SendFailed(Exception):
    """OneBot 发送接口报错，交给 AstrBot 重发也会同样失败。

    rejected：QQ（OneBot 实现）收到请求后拒绝发送；否则是超时、连接中断等，请求可能没送到。
    """

    def __init__(self, message: str, rejected: bool):
        super().__init__(message)
        self.rejected = rejected

    @classmethod
    def of(cls, error: Exception) -> "SendFailed":
        # aiocqhttp 的 ActionFailed 是 OneBot 实现返回了失败；NetworkError 等是请求没拿到回应
        return cls(str(error), type(error).__name__ == "ActionFailed")
