"""权限：群聊总开关、管理员、群白名单、用户黑名单、R18 闸门、冷却与每日额度。

优先级从高到低：不允许群聊时所有群聊消息都不处理；管理员不受其余限制；黑名单、白名单、
R18、冷却和每日额度只约束普通用户。
"""

import time
from datetime import date

from .models import EXPLICIT, DrawRequest, Work
from .settings import Access


class AccessControl:
    def __init__(self, conf: Access):
        self.conf = conf
        self._admins = set(conf.admins)
        self._whitelist = set(conf.group_whitelist)
        self._blacklist = set(conf.user_blacklist)
        self._last_use: dict[str, float] = {}
        self._daily: dict[str, int] = {}
        self._daily_date = date.today()

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

    def take(self, user_id: str, req: DrawRequest) -> str | None:
        """检查冷却和每日额度，通过时开始冷却。额度不够时缩小请求：先减图集数，再减每集张数。"""
        if self.is_admin(user_id):
            return None
        now = time.monotonic()
        last = self._last_use.get(user_id)
        if last is not None:
            remain = self.conf.cooldown_seconds - (now - last)
            if remain > 0:
                return f"冷却中，请 {remain:.0f} 秒后再试。"
        limit = self.conf.daily_limit
        if limit > 0:
            if self._daily_date != date.today():
                self._daily_date = date.today()
                self._daily.clear()
            remain = limit - self._daily.get(user_id, 0)
            if remain <= 0:
                return f"今日次数已用完（{limit} 张）。"
            req.per_album = min(req.per_album, remain)
            req.albums = max(1, min(req.albums, remain // req.per_album))
        self._last_use[user_id] = now
        return None

    def used(self, user_id: str, images: int):
        if not self.is_admin(user_id):
            self._daily[user_id] = self._daily.get(user_id, 0) + images
