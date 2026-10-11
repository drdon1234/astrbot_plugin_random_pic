"""权限：群聊总开关、管理员、群白名单、用户黑名单、R18 闸门、冷却与每日额度。

优先级从高到低：不允许群聊时所有群聊消息都不处理；管理员不受其余限制；黑名单、白名单、
R18、冷却和每日额度只约束普通用户。
"""

import time
from dataclasses import dataclass
from datetime import date

from ..config.settings import Access
from ..models import EXPLICIT, DrawRequest, Work


@dataclass
class Quota:
    """一次请求的额度预留；仅确认发送后扣费，退出释放未使用部分。"""

    access: 'AccessControl'
    user_id: str
    remaining: int = 0
    closed: bool = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()

    def reserve(self, pages: int) -> str | None:
        if self.closed:
            raise RuntimeError('请求额度已经释放')
        pages = max(0, pages)
        access = self.access
        available = access._available(self.user_id) + self.remaining
        if access._limited(self.user_id) and pages > available:
            return f'完整作品有 {pages} 页，今日剩余 {max(0, available)} 张额度，不足以发送完整作品。'
        self._hold(pages)
        return None

    def _hold(self, pages: int):
        table = self.access._reserved
        value = table.get(self.user_id, 0) + pages - self.remaining
        if value:
            table[self.user_id] = value
        else:
            table.pop(self.user_id, None)
        self.remaining = pages

    def charge(self, count: int):
        if self.closed or count < 0:
            raise RuntimeError('无效的额度结算')
        if self.access.is_admin(self.user_id):
            return
        if self.access._limited(self.user_id) and count > self.remaining:
            raise RuntimeError('发送内容超过已预留额度')
        self.access._new_day()
        self._hold(max(0, self.remaining - count))
        self.access._daily[self.user_id] = self.access._daily.get(self.user_id, 0) + count

    def release(self):
        if not self.closed:
            self._hold(0)
            self.closed = True


class AccessControl:
    def __init__(self, conf: Access):
        self.conf = conf
        self._admins = set(conf.admins)
        self._whitelist = set(conf.group_whitelist)
        self._blacklist = set(conf.user_blacklist)
        self._last_use: dict[str, float] = {}
        self._daily: dict[str, int] = {}
        self._daily_date = date.today()
        self._reserved: dict[str, int] = {}

    def is_admin(self, user_id: str | None) -> bool:
        return user_id is not None and user_id in self._admins

    def allowed(self, user_id: str, group_id: str) -> bool:
        """这条消息是否处理（不处理时不回复）。"""
        if group_id and not self.conf.group_enabled:
            return False
        if self.is_admin(user_id):
            return True
        if user_id in self._blacklist:
            return False
        return not (group_id and self._whitelist and group_id not in self._whitelist)

    def explicit_allowed(self, is_private: bool, user_id: str | None = None) -> bool:
        """这个会话能否出 R18；user_id 为空（定时推送）时只看会话。"""
        if self.is_admin(user_id):
            return True
        return self.conf.private_r18 if is_private else self.conf.group_r18

    def gate(
        self, rating: str, is_private: bool, user_id: str | None = None
    ) -> str | None:
        """请求阶段的 R18 闸门，返回拒绝原因。"""
        if rating == EXPLICIT and not self.explicit_allowed(is_private, user_id):
            return "私聊不允许 R18。" if is_private else "本群不允许 R18。"
        return None

    def work_gate(
        self, work: Work, is_private: bool, user_id: str | None = None
    ) -> str | None:
        """完整作品（/全集、/pdf、推送）的闸门：过滤与抽图相同，无法判定分级的按 R18 处理。"""
        if work.blocked:
            return f"作品{work.blocked}，不予发送。"
        return self.gate(work.rating or EXPLICIT, is_private, user_id)

    def _new_day(self):
        if self._daily_date != date.today():
            self._daily_date = date.today()
            self._daily.clear()

    def _limited(self, user_id: str) -> bool:
        return self.conf.daily_limit > 0 and not self.is_admin(user_id)

    def _available(self, user_id: str) -> int:
        self._new_day()
        return self.conf.daily_limit - self._daily.get(user_id, 0) - self._reserved.get(user_id, 0)

    def begin(self, user_id: str, req: DrawRequest | None = None) -> tuple[Quota | None, str | None]:
        """检查冷却并预留请求额度；完整作品先启动请求，查询后预留实际页数。"""
        if self.is_admin(user_id):
            return Quota(self, user_id), None
        now = time.monotonic()
        last = self._last_use.get(user_id)
        if last is not None:
            remain = self.conf.cooldown_seconds - (now - last)
            if remain > 0:
                return None, f"冷却中，请 {remain:.0f} 秒后再试。"
        limit = self.conf.daily_limit
        if limit > 0:
            remain = self._available(user_id)
            if remain <= 0:
                return None, f"今日额度已用完或正在使用（{limit} 张）。"
            if req is not None:
                before = req.albums, req.per_album
                req.per_album = min(req.per_album, remain)
                req.albums = max(1, min(req.albums, remain // req.per_album))
                if before != (req.albums, req.per_album):
                    req.notices.append(
                        f"今日剩余 {remain} 张额度，本次调整为 {req.albums} 个图集、"
                        f"每集 {req.per_album} 张（视频每个计 1 张）。"
                    )
        self._last_use[user_id] = now
        quota = Quota(self, user_id)
        if req is not None:
            quota._hold(req.albums * req.per_album)
        return quota, None
