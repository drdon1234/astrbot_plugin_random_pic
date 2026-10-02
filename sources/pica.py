"""哔咔漫画（PicACG）三次元图源：只用 Cosplay 分类，默认插件自动注册专用账号登录，也可以手动配置账号。

接口是 App 用的 picaapi.picacomic.com，每个请求带 HMAC-SHA256 签名
（路径 + 时间 + nonce + 方法 + api-key，转小写，密钥写死在 App 里），登录后带 token。
- auth/register：注册账号（用户名只能是字母数字，密码至少 8 位，还要昵称、生日、性别、三组密保）；
- auth/sign-in：用户名 + 密码换 token，实测有效期 7 天，过期后接口返回 401；
- comics?page=N&c=Cosplay&s=dd：分类列表，每页 20 本，带标签；
- comics/advanced-search?page=N（POST 关键词和分类）：关键词搜索，结果格式同分类列表；
- comics/{id}：本子详情（搜索结果缺标签时补标签）；
- comics/{id}/order/{第几话}/pages?page=N：一话的图片，每页若干张，带总张数。
  第几话从 1 开始，整本打包时依次取第 1 到 epsCount 话。

随机抽取 = 随机翻一页分类列表（带关键词时翻搜索结果），再随机挑一本、随机取几张。
关键词只支持普通词，E-Hentai 的标签语法（带 : $ "）交给 E-Hentai。哔咔没有分级，
上传者给无露点的写真打「無H內容」标签（实测 Cosplay 分类约 83% 带这个标签），
带它的算擦边，不带的算 R18。
"""

import asyncio
import hashlib
import hmac
import json
import random
import secrets
import time
import uuid
from pathlib import Path

from yarl import URL

from astrbot.api import logger

from ..filters import ContentFilter
from ..models import (
    EXPLICIT,
    REAL,
    SENSITIVE,
    Album,
    DrawContext,
    DrawOptions,
    Work,
    WorkRef,
)
from ..net import HttpClient, ImageCache
from ..util import TTLCache
from .base import Source, SourceError, has_tag_syntax, keyword_and_excludes

API_BASE = "https://picaapi.picacomic.com/"
API_KEY = "C69BAF41DA5ABD1FFEDC6D2FEA56B"
SECRET = r"~d}$Q7$eIni=V)9\RK/P.RM4;9[7|@/CA}b~OW!3?EV`:<>M7pddUBL5n|0/*Cn"
APP_HEADERS = {
    "api-key": API_KEY,
    "accept": "application/vnd.picacomic.com.v1+json",
    "app-channel": "2",
    "app-version": "2.2.1.2.3.3",
    "app-uuid": "defaultUuid",
    "app-platform": "android",
    "app-build-version": "44",
    "User-Agent": "okhttp/3.8.1",
    "image-quality": "original",
}
CATEGORY = "Cosplay"
NON_H_TAG = "無H內容"
# 说明文字里不列出的标签，以及最多列出的标签数
PLAIN_TAGS = frozenset({"COSPLAY", NON_H_TAG})
MAX_CAPTION_TAGS = 4
# 总页数缓存：分类列表一小时，搜索结果 10 分钟
LISTING_TTL = 3600
SEARCH_TTL = 600
PAGES_CACHE_SIZE = 256
# 每个图集最多看这么多页列表（一页里没有符合分级、黑名单的本子或下载失败时换一页）
LISTINGS_PER_ALBUM = 4
# 搜索结果不带标签时，每页最多查这么多本的详情
DETAIL_TRIES = 3
# 默认插件自己注册一个专用账号（不占用用户的账号，也不用把密码写进配置），
# 用户名、密码和 token 存到磁盘反复使用；也可以在配置里手动填账号。
ACCOUNT_PREFIX = "rp"
AUTO_STATE = "pica_account.json"
MANUAL_STATE = "pica_token.json"
# 登录失败（网络错误、手动账号密码错误等）后这么久内不再尝试，避免每次抽卡都去登录
LOGIN_BACKOFF = 600
# 注册失败、登录被限流（错误码 1023 too many requests）、专用账号失效后的冷却：
# 注册 / 账号失效首次 1 小时，限流首次 6 小时，之后每次翻倍，最多 24 小时。
# 实测登录限流按账号计，只在密码正确时出现，限流期间反复登录会让它一直解除不了；
# 反复注册也会被限流。所以冷却时间和账号一起存到磁盘，重载插件也不清零。
RATE_LIMIT_ERROR = "1023"
BAD_ACCOUNT_ERROR = "1004"  # invalid email or password
REGISTER_BACKOFF = 3600
RATE_LIMIT_BACKOFF = 6 * 3600
BACKOFF_MAX = 24 * 3600


class PicaError(SourceError):
    pass


def sign(path: str, ts: str, nonce: str, method: str) -> str:
    raw = (path + ts + nonce + method + API_KEY).lower()
    return hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


def comic_rating(tags: list[str]) -> str:
    return SENSITIVE if NON_H_TAG in tags else EXPLICIT


def media_url(media: dict) -> str:
    return f"{str(media['fileServer']).rstrip('/')}/static/{media['path']}"


class Picacomic(Source):
    key = "pica"
    name = "哔咔"
    style = REAL
    intro = "Cosplay 分类，可搜普通关键词"

    def __init__(
        self,
        http: HttpClient,
        cache: ImageCache,
        content: ContentFilter,
        opts: DrawOptions,
        data_dir: Path | None = None,
        account: tuple[str, str] | None = None,
    ):
        """account 是手动配置的 (账号, 密码)，None 时用插件自己注册维护的专用账号。"""
        super().__init__(cache, content, opts)
        self.http = http
        self._manual = account is not None
        # 当前账号：{"email", "password", "registered"}，专用账号还没生成时为 None
        self._account: dict | None = None
        if account is not None:
            email, password = account
            self._account = {"email": email, "password": password, "registered": True}
        self._token: str | None = None
        # 注册、登录失败后到这个时间（time.time()）之前不再尝试；连续失败的次数决定冷却多久
        self._blocked_until = 0.0
        self._failures = 0
        self._login_lock = asyncio.Lock()
        # 关键词（不带时为 ""，即整个分类）→ 列表总页数
        self._pages = TTLCache(LISTING_TTL, PAGES_CACHE_SIZE)
        # 账号、token 和冷却存到磁盘，插件重载后不必重新注册、登录（两个接口限流都很严）。
        # 专用账号和手动账号分开存，来回切换时专用账号不会丢
        self._state_path = (
            data_dir / (MANUAL_STATE if self._manual else AUTO_STATE)
            if data_dir is not None
            else None
        )
        self._load_state()

    @property
    def unavailable(self) -> str | None:
        account = self._account
        if self._manual and not (account["email"] and account["password"]):
            return "没有填写哔咔账号"
        return None

    def accepts(self, ctx: DrawContext) -> bool:
        """只有 Cosplay 分类，不支持随机角色和 E-Hentai 标签语法；登录冷却期间不参与抽取。"""
        req = ctx.req
        return (
            super().accepts(ctx)
            and not req.random_character
            and not has_tag_syntax(req.keywords)
            and not self._login_blocked()
        )

    def _login_blocked(self) -> bool:
        return self._token is None and time.time() < self._blocked_until

    def _credentials_key(self) -> str:
        """手动账号的状态跟账号和密码绑定（不存密码）：改了账号或密码就不用旧 token 和冷却。"""
        raw = f"{self._account['email']}\0{self._account['password']}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _load_state(self):
        if self._state_path is None:
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if self._manual:
            if data.get("credentials") != self._credentials_key():
                return
            self._token = data.get("token") or None
        elif (
            isinstance(account := data.get("account"), dict)
            and isinstance(account.get("email"), str)
            and isinstance(account.get("password"), str)
        ):
            self._account = {
                "email": account["email"],
                "password": account["password"],
                "registered": bool(account.get("registered")),
            }
            self._token = data.get("token") or None
        try:
            self._blocked_until = float(data.get("blocked_until") or 0)
            self._failures = int(data.get("failures") or 0)
        except (TypeError, ValueError):
            pass

    def _save_state(self):
        if self._state_path is None:
            return
        who = (
            {"credentials": self._credentials_key()}
            if self._manual
            else {"account": self._account}
        )
        state = {
            **who,
            "token": self._token,
            "blocked_until": self._blocked_until,
            "failures": self._failures,
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state), encoding="utf-8")
        except OSError as e:
            logger.warning(f"[random_pic] 保存哔咔账号失败: {e!r}")

    def _blocked_text(self) -> str:
        return time.strftime("%m-%d %H:%M", time.localtime(self._blocked_until))

    def _block(self, base: float, grow: bool = True):
        """记下一次失败，算出下次可以尝试的时间。grow 为 False 时固定冷却 base 秒。"""
        if grow:
            self._failures += 1
            delay = min(BACKOFF_MAX, base * 2 ** (self._failures - 1))
        else:
            delay = base
        self._blocked_until = time.time() + delay
        self._save_state()

    def _fail(self, what: str, reason: object) -> PicaError:
        return PicaError(f"哔咔{what}失败：{reason}，{self._blocked_text()} 后再试")

    async def _register(self):
        """注册专用账号。账号在发请求前就存到磁盘：响应丢失时下次按「已存在」处理，不会再注册一个。"""
        if self._account is None:
            name = ACCOUNT_PREFIX + secrets.token_hex(5)
            self._account = {
                "email": name,
                "password": secrets.token_hex(8),
                "registered": False,
            }
            self._save_state()
        account = self._account
        body = {
            "email": account["email"],
            "password": account["password"],
            "name": account["email"],
            "birthday": "2000-01-01",
            "gender": "m",
            "question1": "1",
            "question2": "2",
            "question3": "3",
            "answer1": "1",
            "answer2": "2",
            "answer3": "3",
        }
        try:
            status, data = await self._send("POST", "auth/register", body)
        except Exception as e:
            self._block(LOGIN_BACKOFF, grow=False)
            raise self._fail("注册", repr(e)) from e
        message = str(data.get("message") or "")
        # 「email is already exist」：上次注册其实成功了，只是没收到响应
        if status != 200 and "already exist" not in message:
            self._block(REGISTER_BACKOFF)
            raise self._fail("注册", message or status)
        account["registered"] = True
        self._save_state()
        logger.info(f"[random_pic] 已注册哔咔专用账号 {account['email']}")

    async def _login(self, stale: str | None):
        async with self._login_lock:
            if self._token != stale:
                return  # 等锁期间别的请求已经重新登录过
            if time.time() < self._blocked_until:
                raise PicaError(f"哔咔登录失败，{self._blocked_text()} 后再试")
            self._token = None
            if self._account is None or not self._account["registered"]:
                await self._register()
            account = self._account
            try:
                status, data = await self._send(
                    "POST",
                    "auth/sign-in",
                    {"email": account["email"], "password": account["password"]},
                )
            except Exception as e:
                self._block(LOGIN_BACKOFF, grow=False)
                raise self._fail("登录", repr(e)) from e
            token = (data.get("data") or {}).get("token")
            if status != 200 or not token:
                error = str(data.get("error"))
                if error == RATE_LIMIT_ERROR:
                    self._block(RATE_LIMIT_BACKOFF)
                elif error == BAD_ACCOUNT_ERROR and not self._manual:
                    # 专用账号不存在或密码不对（「已存在」的其实是别人的账号）：冷却后重新注册
                    self._account = None
                    self._block(REGISTER_BACKOFF)
                else:
                    self._block(LOGIN_BACKOFF, grow=False)
                raise self._fail("登录", data.get("message") or status)
            self._token = token
            self._blocked_until = 0.0
            self._failures = 0
            self._save_state()
            logger.info("[random_pic] 哔咔登录成功")

    async def _send(
        self, method: str, path: str, body: dict | None
    ) -> tuple[int, dict]:
        ts, nonce = str(int(time.time())), uuid.uuid4().hex
        headers = {
            **APP_HEADERS,
            "time": ts,
            "nonce": nonce,
            "signature": sign(path, ts, nonce, method),
        }
        if self._token:
            headers["authorization"] = self._token
        # 签名按原样的路径计算，不能让 aiohttp 重新编码
        url = URL(API_BASE + path, encoded=True)
        async with self.http.request(method, url, json=body, headers=headers) as resp:
            text = await resp.text(errors="replace")
            try:
                data = json.loads(text)
            except ValueError:
                data = {"message": text[:200]}
            return resp.status, data if isinstance(data, dict) else {}

    async def _request(self, path: str, body: dict | None = None) -> dict:
        """带 token 的请求（有 body 时为 POST），返回响应里的 data。token 过期时重新登录一次。"""
        if self._token is None:
            await self._login(None)
        method = "GET" if body is None else "POST"
        for attempt in range(2):
            token = self._token
            status, data = await self._send(method, path, body)
            if status == 401 and attempt == 0:
                await self._login(token)
                continue
            if status != 200:
                raise PicaError(
                    f"哔咔请求失败：HTTP {status} {data.get('message', '')}"
                )
            return data.get("data") or {}
        raise AssertionError("unreachable")

    async def _listing(self, page: int) -> dict:
        data = await self._request(f"comics?page={page}&c={CATEGORY}&s=dd")
        return data.get("comics") or {}

    async def _search(self, keyword: str, page: int) -> dict:
        data = await self._request(
            f"comics/advanced-search?page={page}",
            {"keyword": keyword, "categories": [CATEGORY], "sort": "dd"},
        )
        return data.get("comics") or {}

    async def _random_listing(self, keyword: str) -> list[dict]:
        """随机翻一页分类列表（带关键词时翻搜索结果）。总页数不知道时先取第一页。"""

        async def fetch(page: int) -> dict:
            if keyword:
                return await self._search(keyword, page)
            return await self._listing(page)

        first = None
        pages = self._pages.get(keyword)
        if pages is None:
            first = await fetch(1)
            pages = int(first.get("pages") or 0)
            self._pages.put(keyword, pages, SEARCH_TTL if keyword else LISTING_TTL)
        if pages <= 0:
            raise PicaError(
                f"哔咔 Cosplay 分类里搜不到「{keyword}」"
                if keyword
                else "哔咔 Cosplay 分类为空"
            )
        page = random.randint(1, pages)
        listing = first if first is not None and page == 1 else await fetch(page)
        return listing.get("docs") or []

    def _reject(self, comic: dict, ctx: DrawContext, exclude: list[str]) -> str | None:
        categories = comic.get("categories")
        if categories and CATEGORY not in categories:
            return "不在 Cosplay 分类"
        tags = [str(t) for t in comic.get("tags") or []]
        words = [str(comic.get("title") or ""), str(comic.get("author") or "")]
        reason = (
            self.rating_reason(comic_rating(tags), ctx)
            or self.content.tags_reason(tags)
            or self.content.text_reason(words)
        )
        if reason:
            return reason
        text = "\n".join([*tags, *words]).lower()
        if any(word in text for word in exclude):
            return "命中排除的关键词"
        if not comic.get("pagesCount"):
            return "没有图片"
        return None

    async def _detail(self, comic: dict) -> dict:
        data = await self._request(f"comics/{comic['_id']}")
        return {**comic, **(data.get("comic") or {})}

    async def _pick(
        self, ctx: DrawContext, keyword: str, exclude: list[str], seen: set[str]
    ) -> dict | None:
        """随机翻一页，挑一本符合分级、黑名单的本子。"""
        docs = [
            c
            for c in await self._random_listing(keyword)
            if c.get("_id") and c["_id"] not in seen
        ]
        random.shuffle(docs)
        details = 0
        for comic in docs:
            if "tags" not in comic:
                if details >= DETAIL_TRIES:
                    break
                details += 1
                comic = await self._detail(comic)
            if comic["_id"] in seen or self._reject(comic, ctx, exclude):
                continue
            seen.add(comic["_id"])
            return comic
        return None

    async def draw(self, ctx: DrawContext, n: int) -> tuple[list[Album], list[str]]:
        """抽 n 本，每本取一个图集，并发抽取。关键词里 - 开头的词表示排除。"""
        keyword, exclude = keyword_and_excludes(ctx.req.keywords)
        seen: set[str] = set()

        async def attempt() -> Album | None:
            comic = await self._pick(ctx, keyword, exclude, seen)
            return await self._album(comic, ctx.req.per_album) if comic else None

        return await self.collect(n, LISTINGS_PER_ALBUM, attempt)

    async def _episode_page(self, cid: str, order: int, page: int) -> dict:
        data = await self._request(f"comics/{cid}/order/{order}/pages?page={page}")
        return data.get("pages") or {}

    async def _album(self, comic: dict, n: int) -> Album | None:
        """从本子随机的一话里取 n 张，按页码排序。"""
        cid = comic["_id"]
        tags = [str(t) for t in comic.get("tags") or []]
        order = random.randint(1, max(1, int(comic.get("epsCount") or 1)))
        first = await self._episode_page(cid, order, 1)
        total, limit = int(first.get("total") or 0), int(first.get("limit") or 0)
        if total <= 0 or limit <= 0:
            return None
        # 图片列表页用到时才取，同一页只取一次
        pages: dict[int, asyncio.Future] = {
            1: asyncio.get_running_loop().create_future()
        }
        pages[1].set_result(first)

        async def docs_of(page: int) -> list:
            if page not in pages:
                pages[page] = asyncio.ensure_future(
                    self._episode_page(cid, order, page)
                )
            return (await pages[page]).get("docs") or []

        async def download(index: int) -> tuple[int, Path] | None:
            docs = await docs_of(index // limit + 1)
            if index % limit >= len(docs):
                return None
            path = await self.cache.download(media_url(docs[index % limit]["media"]))
            return (index + 1, path) if path else None

        pictures = await self.pick_pages(total, n, comic_rating(tags), download)
        if not pictures:
            return None
        author = str(comic.get("author") or "").strip()
        shown = [t for t in tags if t not in PLAIN_TAGS][:MAX_CAPTION_TAGS]
        # 哔咔没有公开的网页地址，说明文字不附链接
        details = [f"作者：{author}"] if author else []
        if shown:
            details.append(f"标签：{'、'.join(shown)}")
        return Album(
            source=self.name,
            title=str(comic.get("title") or "").strip(),
            total=total,
            pictures=pictures,
            details=details,
            work=WorkRef(self.key, cid),
        )

    async def work(self, ref: WorkRef) -> Work | None:
        """查询本子详情，不存在时返回 None。分级和过滤与抽图相同。"""
        comic = (await self._request(f"comics/{ref.id}")).get("comic")
        if not isinstance(comic, dict) or not comic.get("_id"):
            return None
        tags = [str(t) for t in comic.get("tags") or []]
        title = str(comic.get("title") or "").strip()
        words = [title, str(comic.get("author") or "")]
        return Work(
            ref=WorkRef(self.key, str(comic["_id"])),
            title=title,
            pages=int(comic.get("pagesCount") or 0),
            rating=comic_rating(tags),
            blocked=self.content.tags_reason(tags) or self.content.text_reason(words),
            data=max(1, int(comic.get("epsCount") or 1)),
        )

    async def _episode_urls(self, cid: str, order: int) -> list[str]:
        """一话的全部图片地址：先取第一页得到总数，其余页并发取。"""
        first = await self._episode_page(cid, order, 1)
        total, limit = int(first.get("total") or 0), int(first.get("limit") or 0)
        if total <= 0 or limit <= 0:
            return []
        rest = await asyncio.gather(
            *(
                self._episode_page(cid, order, page)
                for page in range(2, -(-total // limit) + 1)
            )
        )
        docs = [d for data in [first, *rest] for d in data.get("docs") or []]
        return [media_url(d["media"]) for d in docs if d.get("media")]

    async def page_items(self, work: Work) -> list[str]:
        """依次取每一话的图片地址，所有话连起来编页码。"""
        urls = []
        for order in range(1, work.data + 1):
            urls += await self._episode_urls(work.ref.id, order)
        return urls
