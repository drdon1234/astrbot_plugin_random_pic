"""预备池：按默认图集数和每集张数预先抽好几批图，二次元、三次元的擦边和 R18 各一个池。

不带关键词的默认抽图和定时推送直接拿一批发送，发完删掉这批并在后台补一批新的。
每批（桶）存在共享目录（没有时为插件数据目录）里，插件重载后按当前配置同步：多的图集、图片删掉，少的补抽。
没有现成的桶但正在补时，等正在补的这一批。
"""

import asyncio
import json
import shutil
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from astrbot.api import logger

from .drawer import DrawResult
from .models import (
    ANIME,
    EXPLICIT,
    RATING_NAMES,
    REAL,
    SENSITIVE,
    STYLE_NAMES,
    Album,
    DrawRequest,
    WorkRef,
)

# 抽图函数：(请求, 能否出 R18) → 结果
DrawFn = Callable[[DrawRequest, bool], Awaitable[DrawResult]]
MANIFEST = "bucket.json"
# 一批没补满（或维护出错）时隔多久再补
RETRY_DELAY = 120
# 一批最多补几次；补了这么多次还不满就照样拿来用
FILL_ATTEMPTS = 3


@dataclass(frozen=True)
class Spec:
    """池里每批图的规格。"""

    style: str
    rating: str
    albums: int
    per_album: int
    min_pages: int = 0

    def serves(self, req: DrawRequest) -> bool:
        return (
            req.style == self.style
            and req.rating == self.rating
            and not req.keywords
            and not req.random_character
            and not req.whole
            and req.albums <= self.albums
            and req.per_album <= self.per_album
        )


@dataclass
class Bucket:
    dir: Path
    albums: list[Album]
    attempts: int = 0  # 补过几次


def _album_to_json(album: Album) -> dict:
    work = album.work
    return {
        "source": album.source,
        "title": album.title,
        "total": album.total,
        "pictures": [[page, path.name] for page, path in album.pictures],
        "details": album.details,
        "work": [work.source, work.id, work.token] if work else None,
        "duration": album.duration,
    }


def _album_from_json(data: dict, folder: Path) -> Album:
    work = data.get("work")
    return Album(
        source=data["source"],
        title=data["title"],
        total=int(data["total"]),
        pictures=[(int(page), folder / name) for page, name in data["pictures"]],
        details=list(data.get("details") or []),
        work=WorkRef(*work) if work else None,
        duration=data.get("duration"),
    )


def _remove(paths):
    for path in paths:
        path.unlink(missing_ok=True)


class Pool:
    """一个分级的预备池。allowed：允许出现在这个池里的图源键（作品图源不在其中的图集会被删掉）。

    move 为 True 时把抽到的文件移进桶（视频：下载的是临时文件，不必留两份），否则复制（图片缓存另有清理）。
    """

    def __init__(
        self,
        draw: DrawFn,
        root: Path,
        spec: Spec,
        batches: int,
        allowed: set[str],
        move: bool = False,
        label: str = "",
    ):
        self.draw_fn = draw
        self.move = move
        self.root = root
        self.spec = spec
        self.batches = batches
        self.allowed = allowed
        self.name = f"{label}{STYLE_NAMES[spec.style]}·{RATING_NAMES[spec.rating]}"
        self.ready: list[Bucket] = []
        self._pending: list[Bucket] = []  # 重载后要补图集的旧桶
        self._cond = asyncio.Condition()
        self._wake = asyncio.Event()
        self._busy = True  # 正在补桶（或马上要补），取不到现成的桶时可以等
        self._backoff = False  # 没补满或出错后正在隔一会儿再补，这期间取不到就不等
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._run())

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()

    # ---- 取用 ----

    async def take(self, req: DrawRequest) -> Bucket | None:
        """取一批现成的；没有现成的但正在补时等它补完。取不到返回 None。"""
        if not self.spec.serves(req):
            return None
        async with self._cond:
            while not self.ready:
                if not self._busy or self._task is None or self._task.done():
                    return None
                await self._cond.wait()
            bucket = self.ready.pop(0)
        if not self._backoff:
            self._busy = True
        self._wake.set()
        return bucket

    # ---- 维护 ----

    async def _run(self):
        try:
            await asyncio.to_thread(self._load)
        except Exception as e:
            logger.error(f"[random_pic] 预备池（{self.name}）读取旧桶出错: {e!r}")
        logger.info(
            f"[random_pic] 预备池（{self.name}）已有 {len(self.ready)} 批完整、"
            f"{len(self._pending)} 批待补，目标 {self.batches} 批"
        )
        while True:
            bucket = None
            try:
                bucket = await self._next()
                if bucket is None:
                    await self._wake.wait()
                    continue
                self._busy = True
                ok = await self._fill(bucket)
                done = ok and (
                    len(bucket.albums) >= self.spec.albums
                    or bucket.attempts >= FILL_ATTEMPTS
                )
                async with self._cond:
                    if done:
                        self.ready.append(bucket)
                    else:
                        # 没补满的桶过一会儿接着补；这期间没有现成的桶时直接现抽
                        if ok:
                            self._pending.append(bucket)
                        self._busy = False
                    self._cond.notify_all()
                if not done:
                    await self._back_off()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 出错后过一会儿接着维护，而不是让池子停掉
                logger.error(
                    f"[random_pic] 预备池（{self.name}）出错: {e!r}", exc_info=e
                )
                async with self._cond:
                    if bucket is not None and bucket.dir.is_dir():
                        if bucket not in self.ready and bucket not in self._pending:
                            self._pending.append(bucket)
                    self._busy = False
                    self._cond.notify_all()
                await self._back_off()

    async def _back_off(self):
        self._backoff = True
        try:
            await asyncio.sleep(RETRY_DELAY)
        finally:
            self._backoff = False

    async def _next(self) -> Bucket | None:
        """下一个要补的桶：先补待补的，再补新桶；已经够数时返回 None 并放开等待的取用。"""
        async with self._cond:
            if self._pending:
                return self._pending.pop(0)
            if len(self.ready) < self.batches:
                return Bucket(self.root / uuid.uuid4().hex[:12], [])
            self._busy = False
            self._wake.clear()
            self._cond.notify_all()
            return None

    async def _fill(self, bucket: Bucket) -> bool:
        """把桶补到规定的图集数，返回桶里是否有图。"""
        bucket.attempts += 1
        short = self.spec.albums - len(bucket.albums)
        if short > 0:
            req = DrawRequest(
                self.spec.style,
                self.spec.rating,
                albums=short,
                per_album=self.spec.per_album,
            )
            try:
                result = await self.draw_fn(req, self.spec.rating == EXPLICIT)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[random_pic] 预备池（{self.name}）抽图出错: {e!r}")
                result = DrawResult(errors=[repr(e)])
            if result.errors and len(result.albums) < short:
                logger.warning(
                    f"[random_pic] 预备池（{self.name}）补了 {len(result.albums)}/{short} "
                    f"个图集：{result.reason()}"
                )
            await asyncio.to_thread(self._store, bucket, result.albums)
        if not bucket.albums:
            shutil.rmtree(bucket.dir, ignore_errors=True)
            return False
        return True

    def _store(self, bucket: Bucket, albums: list[Album]):
        """把抽到的图复制进桶（缓存会被清理），并写入清单。"""
        bucket.dir.mkdir(parents=True, exist_ok=True)
        for album in albums:
            prefix = uuid.uuid4().hex[:8]
            pictures = []
            for page, path in album.pictures:
                dest = bucket.dir / f"{prefix}_{page}{path.suffix}"
                try:
                    if self.move:
                        shutil.move(path, dest)
                    else:
                        shutil.copyfile(path, dest)
                except OSError as e:
                    logger.warning(f"[random_pic] 预备池复制图片失败 {path}: {e!r}")
                    continue
                pictures.append((page, dest))
            if pictures:
                bucket.albums.append(replace(album, pictures=pictures))
        self._save(bucket)

    @staticmethod
    def _save(bucket: Bucket):
        data = {"albums": [_album_to_json(a) for a in bucket.albums]}
        (bucket.dir / MANIFEST).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def _load(self):
        """读出上次留下的桶并按当前规格同步：删掉多余的桶、图集和图片，缺图集的桶放进待补。"""
        self.root.mkdir(parents=True, exist_ok=True)
        folders = sorted(
            (p for p in self.root.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
        )
        for folder in folders:
            bucket = self._sync(folder)
            if bucket is None or len(self.ready) + len(self._pending) >= self.batches:
                shutil.rmtree(folder, ignore_errors=True)
            elif len(bucket.albums) < self.spec.albums:
                self._pending.append(bucket)
            else:
                self.ready.append(bucket)
        for path in self.root.iterdir():
            if not path.is_dir():
                path.unlink(missing_ok=True)

    def _sync(self, folder: Path) -> Bucket | None:
        try:
            data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
            albums = [_album_from_json(a, folder) for a in data["albums"]]
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.info(f"[random_pic] 预备池丢弃无法读取的桶 {folder.name}: {e!r}")
            return None
        keep: list[Album] = []
        for album in albums:
            album.pictures = [(p, f) for p, f in album.pictures if f.is_file()]
            source_ok = album.work is None or album.work.source in self.allowed
            if (
                not source_ok
                or album.total < self.spec.min_pages
                or len(album.pictures) < self.spec.per_album
                or len(keep) >= self.spec.albums
            ):
                _remove(f for _, f in album.pictures)
                continue
            _remove(f for _, f in album.pictures[self.spec.per_album :])
            album.pictures = album.pictures[: self.spec.per_album]
            keep.append(album)
        used = {f.name for album in keep for _, f in album.pictures} | {MANIFEST}
        _remove(p for p in folder.iterdir() if p.name not in used)
        bucket = Bucket(folder, keep)
        self._save(bucket)
        return bucket


class Reserve:
    """二次元、三次元各有擦边、R18 两个预备池，目录为 root/风格/分级。"""

    def __init__(
        self,
        draw: DrawFn,
        root: Path,
        specs: dict[Spec, int],
        allowed: dict[Spec, set[str]],
        move: bool = False,
        label: str = "",
    ):
        """specs：要维护的池的规格 → 批数（批数为 0 或不在其中的不维护）；allowed：规格 → 可用图源键。

        move：抽到的文件移进桶而不是复制（视频）；label：日志里池名的前缀。
        """
        self.draw_fn = draw
        self.root = root
        # 5.5.x 的池只有三次元，目录是 root/分级
        for rating in (SENSITIVE, EXPLICIT):
            legacy, dest = root / rating, root / REAL / rating
            if legacy.is_dir() and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                legacy.rename(dest)
            shutil.rmtree(legacy, ignore_errors=True)
        self.pools: dict[tuple[str, str], Pool] = {}
        for spec, batches in specs.items():
            if batches > 0:
                self.pools[spec.style, spec.rating] = Pool(
                    draw,
                    root / spec.style / spec.rating,
                    spec,
                    batches,
                    allowed.get(spec, set()),
                    move,
                    label,
                )
        # 不维护的池删掉旧桶
        for style in (REAL, ANIME):
            for rating in (SENSITIVE, EXPLICIT):
                if (style, rating) not in self.pools:
                    shutil.rmtree(root / style / rating, ignore_errors=True)

    def start(self):
        for pool in self.pools.values():
            pool.start()

    def stop(self):
        for pool in self.pools.values():
            pool.stop()

    @asynccontextmanager
    async def draw(self, req: DrawRequest, allow_explicit: bool, draw: DrawFn):
        """抽图：能用预备池时拿一批（离开时删掉这批），否则用 draw 现抽。"""
        pool = self.pools.get((req.style, req.rating))
        bucket = None
        if pool is not None and (req.rating != EXPLICIT or allow_explicit):
            bucket = await pool.take(req)
        if bucket is None:
            yield await draw(req, allow_explicit)
            return
        try:
            albums = [
                replace(album, pictures=album.pictures[: req.per_album])
                for album in bucket.albums[: req.albums]
            ]
            yield DrawResult(albums=albums)
        finally:
            shutil.rmtree(bucket.dir, ignore_errors=True)
