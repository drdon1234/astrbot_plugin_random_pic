"""命令触发词的唯一运行时目录：匹配、帮助和后续操作提示共用。"""

from __future__ import annotations

from ..config.settings import Commands, SCHEMA


COMMAND_FIELDS = {
    "抽图": "draw",
    "随机角色": "random_character",
    "二次元": "anime",
    "三次元": "real",
    "擦边": "sensitive",
    "色图": "explicit",
    "全集": "whole",
    "pdf": "pdf",
    "抽视频": "video",
    "刷新预备池": "refresh",
    "帮助": "help",
}


def default_words(action: str) -> tuple[str, ...]:
    """装饰器导入时尚无实例，默认注册名仍从配置定义读取。"""
    return tuple(SCHEMA["commands"]["items"][COMMAND_FIELDS[action]]["default"])


class CommandCatalog:
    def __init__(self, settings: Commands):
        self._words: dict[str, tuple[str, ...]] = {}
        self._actions: dict[str, str | None] = {}
        self._handlers: dict[str, object] = {}
        for action, field in COMMAND_FIELDS.items():
            words = self._validate(getattr(settings, field), action)
            for word in words:
                key = word.casefold()
                if key in self._actions:
                    raise ValueError(
                        f"命令「{action}」与「{self._actions[key]}」的触发词「{word}」重复。"
                    )
                self._actions[key] = action
            self._words[action] = words
        self.help_words = self._validate(settings.help_words, "参数帮助")
        self._help = {word.casefold() for word in self.help_words}
        self._max_words = 1

    @staticmethod
    def _validate(values: list[str], label: str) -> tuple[str, ...]:
        if not values:
            raise ValueError(f"命令「{label}」至少需要一个触发词。")
        result: dict[str, str] = {}
        for word in values:
            if (
                not word
                or any(c.isspace() for c in word)
                or "/" in word
                or "\\" in word
            ):
                raise ValueError(
                    f"命令「{label}」的触发词「{word}」不能留空、包含空白或斜杠；无需填写唤醒前缀。"
                )
            result.setdefault(word.casefold(), word)
        return tuple(result.values())

    def words(self, action: str) -> tuple[str, ...]:
        return self._words[action]

    def bind(self, action: str, metadata) -> None:
        self._handlers[action] = metadata

    def handler(self, action: str):
        return self._handlers.get(action)

    def enabled(self, action: str) -> bool:
        return getattr(self.handler(action), "enabled", True)

    def override(
        self, action: str, *, name: str | None = None, aliases: set[str] | None = None
    ) -> None:
        """AstrBot 命令管理的有效覆盖优先，只更新当前实例，不改持久配置。"""
        old = self.words(action)
        values = [
            name if name is not None else old[0],
            *(sorted(aliases) if aliases is not None else old[1:]),
        ]
        # 框架命令管理允许带空格的完整命令，按其完整词序列匹配。
        words = tuple(
            dict.fromkeys(" ".join(value.split()) for value in values if value.strip())
        )
        if not words:
            raise ValueError(f"命令「{action}」至少需要一个触发词。")
        self._words[action] = words
        # 框架逐项恢复已保存的改名；合法的名称对调也会暂时重名。
        # 每次从完整目录重建，歧义词保留占位但不执行，恢复唯一后自动生效。
        actions: dict[str, str | None] = {}
        for owner, configured in self._words.items():
            for word in configured:
                key = word.casefold()
                actions[key] = (
                    None if key in actions and actions[key] != owner else owner
                )
        self._actions = actions
        self._max_words = max(len(word.split()) for word in actions)

    def command(self, action: str, args: str = "") -> str:
        text = "/" + self.words(action)[0]
        return f"{text} {args}" if args else text

    def resolve(self, text: str) -> tuple[str, list[str]] | None:
        tokens = text.split()
        for count in range(min(len(tokens), self._max_words), 0, -1):
            key = " ".join(tokens[:count]).casefold()
            if key in self._actions:
                action = self._actions[key]
                return (
                    (action, tokens[count:])
                    if action is not None and self.enabled(action)
                    else None
                )
        return None

    def is_help(self, tokens: list[str]) -> bool:
        return any(token.casefold() in self._help for token in tokens)
