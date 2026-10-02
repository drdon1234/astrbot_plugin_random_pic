"""权限：群白名单、用户黑名单、分级闸门、冷却与每日额度。"""

import time
from datetime import date

from .models import EXPLICIT, SENSITIVE, DrawRequest, Work
from .settings import Access, Rating


class AccessControl:
    def __init__(self, rating: Rating, conf: Access):
        self.rating = rating
        self.conf = conf
        self._whitelist = set(conf.group_whitelist)
        self._blacklist = set(conf.user_blacklist)
        self._last_use: dict[str, float] = {}
        self._daily: dict[str, int] = {}
        self._daily_date = date.today()

    def allowed(self, user_id: str, group_id: str) -> bool:
        """不允许群聊时所有群聊消息都不处理；黑名单用户和白名单以外的群不回复。"""
        if group_id and not self.conf.group_enabled:
            return False
        if user_id in self._blacklist:
            return False
        return not (group_id and self._whitelist and group_id not in self._whitelist)

    def gate(self, rating: str, is_private: bool) -> str | None:
        """请求阶段的分级闸门，返回拒绝原因。关闭内容分级后只保留 R18 总开关。"""
        if rating == EXPLICIT and not self.rating.r18_enabled:
            return "R18 功能未开启。"
        if not self.rating.content_rating or is_private:
            return None
        if rating == EXPLICIT:
            return "R18 内容仅限私聊。"
        if rating == SENSITIVE and not self.rating.group_sensitive:
            return "本群未开启擦边内容。"
        return None

    def work_gate(self, work: Work, is_private: bool) -> str | None:
        """完整作品（/全集、/pdf、推送）的闸门：过滤与抽图相同，无法判定分级的按 R18 处理。"""
        if work.blocked:
            return f"作品{work.blocked}，不予发送。"
        return self.gate(work.rating or EXPLICIT, is_private)

    def take(self, user_id: str, req: DrawRequest) -> str | None:
        """检查冷却和每日额度，通过时开始冷却。额度不够时缩小请求：先减图集数，再减每集张数。"""
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
        self._daily[user_id] = self._daily.get(user_id, 0) + images
