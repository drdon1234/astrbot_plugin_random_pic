"""并发、抽样与缓存工具。"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

# 一个图集里有页下载失败时换别的页补上，多试的页数和要的张数一样多，但至少这么多页
MIN_SPARE_PAGES = 2


async def run_thread(func: Callable[..., Any], *args, **kwargs) -> Any:
    """在线程执行磁盘工作；取消时等真实线程退出，再向上传播取消。

    线程不能随等待者一起取消。等待期间再次收到取消也不能提前返回，否则调用者可能
    清理仍在读写的目录，或让旧线程在刷新完成后重新写入旧状态。
    """
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # 取消优先传播，但仍读取线程异常，避免无人接收的异常告警
        raise


def page_order(total: int, *, from_start: bool, skip: float = 0.0) -> list[int]:
    """所有候选页（从 0 开始）的尝试顺序。

    from_start 时从第一页起依次取；否则跳过开头 skip 比例的页后随机打乱。
    """
    if from_start:
        return list(range(total))
    population = list(range(int(total * skip), total))
    random.shuffle(population)
    return population


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


async def fetch_pages(
    total: int,
    n: int,
    concurrency: int,
    fetch: Callable[[int], Awaitable[Any]],
    *,
    from_start: bool,
    skip: float = 0.0,
) -> list:
    """按 page_order 的顺序并发调用 fetch(页)，凑够 n 个非 None 的结果。

    某页失败（返回 None）时换下一页补上，最多多试 max(n, MIN_SPARE_PAGES) 页，
    避免站点不可用时把整个图集都试一遍。结果按完成先后排列。
    """
    order = iter(page_order(total, from_start=from_start, skip=skip))

    async def attempt():
        index = next(order, None)
        return None if index is None else await fetch(index)

    results: list = []
    attempts = min(total, n + max(n, MIN_SPARE_PAGES))
    if n > 0 and attempts > 0:
        await fill(results, n, attempts, concurrency, attempt)
    return results


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


def duration_text(seconds: float | None) -> str:
    """秒数 → 「1:05」，未知时为空。"""
    if not seconds:
        return ""
    total = round(float(seconds))
    return f"{total // 60}:{total % 60:02d}"


class TTLCache:
    """带过期时间、数量上限的缓存，超出上限时丢掉最早放入的。值可以是 None。"""

    def __init__(self, ttl: float, size: int = 256):
        self.ttl = ttl
        self.size = size
        self._data: dict = {}
        self._inflight: dict = {}
        self._closed = False
        self._closing: asyncio.Future | None = None

    def __contains__(self, key) -> bool:
        hit = self._data.get(key)
        return hit is not None and hit[0] >= time.monotonic()

    def get(self, key, default=None):
        return self._data[key][1] if key in self else default

    def put(self, key, value, ttl: float | None = None):
        if self._closed:
            return
        self._data.pop(key, None)
        self._data[key] = (time.monotonic() + (self.ttl if ttl is None else ttl), value)
        while len(self._data) > self.size:
            self._data.pop(next(iter(self._data)))

    def pop(self, key):
        self._data.pop(key, None)

    async def load(
        self,
        key,
        factory: Callable[[], Awaitable[Any]],
        ttl: float | None = None,
    ) -> Any:
        """先查缓存；没有时调用 factory()，同一个 key 并发只调用一次。出错时不缓存。"""
        if self._closed:
            raise RuntimeError("缓存已关闭")
        if key in self:
            return self.get(key)

        async def run():
            value = await factory()
            self.put(key, value, ttl)
            return value

        return await shared(self._inflight, key, run)

    async def close(self):
        """停止共享加载并等待清理完成；取消关闭等待者不打断清理。"""
        if not self._closed:
            self._closed = True
            self._data.clear()
            futures = list(self._inflight.values())
            for future in futures:
                if not future.done():
                    future.cancel()
            self._closing = asyncio.gather(*futures, return_exceptions=True)
        await asyncio.shield(self._closing)
        self._inflight.clear()


async def close_caches(owner) -> None:
    """关闭对象直接持有的 TTLCache 字段，全部尝试后传播首个异常。"""
    caches = [value for value in vars(owner).values() if isinstance(value, TTLCache)]
    results = await asyncio.gather(
        *(cache.close() for cache in caches), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
