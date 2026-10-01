"""并发与抽样工具。"""

import asyncio
import random
from collections.abc import Awaitable, Callable, Hashable
from typing import Any


def pick_pages(total: int, n: int, *, from_start: bool, skip: float = 0.0) -> list[int]:
    """按尝试顺序返回至多 n 个不重复的页（从 0 开始）。

    from_start 时从第一页起依次取；否则跳过开头 skip 比例的页后随机取。
    """
    if from_start:
        return list(range(min(n, total)))
    population = range(int(total * skip), total)
    return random.sample(population, min(n, len(population)))


async def fill(
    results: list,
    n: int,
    attempts: int,
    concurrency: int,
    attempt: Callable[[], Awaitable[Any]],
):
    """并发调用 attempt()，把非 None 的结果追加到 results，直到凑够 n 个或总共尝试 attempts 次。

    正在进行的尝试也计入「已有」，所以不会多抽；某次尝试落空时，由它所在的协程再补一次。
    attempt 抛出的异常会取消其余协程并向上传递，已经追加到 results 的结果保留。
    """
    left, pending = attempts, 0

    async def worker():
        nonlocal left, pending
        while left > 0 and len(results) + pending < n:
            left -= 1
            pending += 1
            try:
                item = await attempt()
            finally:
                pending -= 1
            if item is not None:
                results.append(item)

    count = max(1, min(concurrency, n - len(results), attempts))
    tasks = [asyncio.ensure_future(worker()) for _ in range(count)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def shared(
    inflight: dict, key: Hashable, factory: Callable[[], Awaitable[Any]]
) -> Any:
    """同一个 key 同时只执行一次 factory()，并发的调用者等待同一个结果。"""
    future = inflight.get(key)
    if future is None:
        future = asyncio.ensure_future(factory())
        inflight[key] = future

        def done(f: asyncio.Future):
            if inflight.get(key) is f:
                del inflight[key]
            if not f.cancelled():
                f.exception()  # 调用者都被取消时，避免「异常从未被读取」的警告

        future.add_done_callback(done)
    # 某个调用者被取消时不取消共享的请求
    return await asyncio.shield(future)
