"""插件「服务范围」白名单 —— 四个插件共用的一套口径（语义取自 byrdocs）。

四条规矩，ai_chat / bupt_news / byrdocs / oj_bank 一致：

1. **只看本插件自己的名单**，不回落框架的 `bound_groups`。后者是「机器人监听
   哪些群」（为空 = 监听所有群），与「允许哪些群用本插件」（为空 = 一个都不许）
   语义正好相反 —— 拿监听列表兜底等于「进了多少群就开多少群」；
2. **名单为空 = 谁都不用**。没有 `empty_means: allow` 这种开关：要开哪一个，
   就用对应的 `bind add` 显式加进来；
3. **群聊与群小窗按来源群判，直接私聊按用户判**，两条通道互不代替（群开通
   不等于这个人的私聊开通，反之亦然）；
4. **超级用户豁免**。改白名单的命令本身就只在私聊由超管执行，不豁免就会出现
   「名单配错 → 谁（包括超管）都改不了」的死锁。

`parse_ids` 是名单的唯一解析口径：只收正整数 —— `bool` 是 `int` 的子类，
配置文件里 `users: true` 这种笔误不该悄悄变成「放行 QQ 1」；0/负数不是有效
号码，同样丢弃；非列表值按空处理（`users: 123` 这种少写方括号的不该抛异常）。

各插件仍然各自决定：从哪个配置键读名单（`binding` / `targets`）、回绝时说什么
话、以及 ai_chat 那套「受邀参与别人的会话」的例外。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["parse_ids", "Scope"]


def parse_ids(raw: Any) -> set[int]:
    """配置里的「号列表」→ 正整数集合（`bool` / 0 / 负数 / 非数字一律丢弃）。

    Args:
        raw: 配置值（列表 / 元组 / 集合；其它类型按空处理）。

    Returns:
        QQ 号或群号的集合。
    """
    if not isinstance(raw, (list, tuple, set)):
        return set()
    out: set[int] = set()
    for entry in raw:
        if isinstance(entry, bool):
            continue
        try:
            num = int(entry)
        except (TypeError, ValueError):
            continue
        if num > 0:
            out.add(num)
    return out


def _one(raw: Any) -> Optional[int]:
    """单个号（事件字段）→ 正整数；缺失或非数字返回 None。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        num = int(raw)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


@dataclass(frozen=True)
class Scope:
    """一个插件的服务范围（群白名单 + 用户白名单）。

    用 `Scope.of(binding)` 从 `{"groups": [...], "users": [...]}` 建；
    名单来源不是这个结构的（`bupt_news` 的 `targets`）直接构造。
    """

    groups: frozenset[int] = field(default_factory=frozenset)
    users: frozenset[int] = field(default_factory=frozenset)

    @classmethod
    def of(cls, binding: Any) -> "Scope":
        """从 `{"groups": ..., "users": ...}` 建；不是 dict 时按空名单处理。"""
        block = binding if isinstance(binding, dict) else {}
        return cls(groups=frozenset(parse_ids(block.get("groups"))),
                   users=frozenset(parse_ids(block.get("users"))))

    # ---- 判定 ----

    def group_ok(self, group_id: Any) -> bool:
        """群聊 / 群小窗是否放行（`group_id` 是来源群）。"""
        gid = _one(group_id)
        return gid is not None and gid in self.groups

    def user_ok(self, user_id: Any) -> bool:
        """直接私聊是否放行。"""
        uid = _one(user_id)
        return uid is not None and uid in self.users

    def event_ok(self, event) -> bool:
        """按事件类型判定（群聊 / 群小窗按来源群，私聊按用户）。

        **不含超级用户豁免** —— 那要 `PermissionManager`，由调用方在更外层判
        （豁免是「别把自己锁在外面」的兜底，不是名单的一部分）。
        """
        if event is None:
            return True                       # 无上下文（帮助渲染 / 直接调用）
        if getattr(event, "message_type", "") != "private":
            return self.group_ok(getattr(event, "group_id", None))
        if getattr(event, "is_temp_session", False):
            return self.group_ok(getattr(event, "group_id", None))
        return self.user_ok(getattr(event, "user_id", None))
