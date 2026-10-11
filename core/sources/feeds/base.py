"""订阅抓取接口：读取帖子、识别账号、获取原始媒体地址。"""

import re

from ...config.settings import Feed as FeedConf
from .models import Post


class Feed:
    """一个订阅：抓帖子。子类实现 sample()、post()，可选 warm_up()、close()。"""

    platform_name: str  # 说明里的平台名
    intro: str  # 帮助里的一行介绍
    # sample() 一次就给出全部帖子（抽取时不用再翻），否则每次给一批随机的帖子
    whole = False
    # 每批随机帖子最多用几个（同一页的帖子发布时间相近，换着页抽）
    per_batch: int | None = None

    def __init__(self, conf: FeedConf):
        self.conf = conf
        self.key = conf.key
        self.name = conf.name or f"{self.platform_name} @{conf.account}"
        self.link_re: re.Pattern | None = None

    def is_name(self, word: str) -> bool:
        """关键词是不是在指名这个订阅（账号、显示名称或图源键，不分大小写，账号可以带 @）。"""
        word = word.strip().lower()
        names = {self.conf.account.lower(), f"@{self.conf.account.lower()}", self.key}
        if self.conf.name:
            names.add(self.conf.name.lower())
        return word in names

    def link(self, post: Post) -> str:
        raise NotImplementedError

    def photo_urls(self, url: str) -> list[str]:
        """下载一张图片时依次尝试的地址。"""
        return [url]

    async def sample(self) -> list[Post]:
        """一批随机顺序的帖子。"""
        raise NotImplementedError

    async def post(self, post_id: str) -> Post | None:
        """按 id 查一个帖子，不存在时返回 None。"""
        raise NotImplementedError

    async def warm_up(self):
        pass

    async def close(self):
        pass
