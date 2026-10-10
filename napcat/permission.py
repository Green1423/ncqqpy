"""
权限管理系统 - 基于用户与群组的角色+自定义权限节点
区分私聊与群聊上下文：私聊仅看 user_id；群聊看 user_id + group_id
"""
from __future__ import annotations

from typing import Optional, Callable, Awaitable
from enum import Enum


class DefaultPermission(str, Enum):
    SUPERUSER = "superuser"
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    EVERYONE = "everyone"


class PermissionManager:
    """权限管理器 — 上下文感知的权限校验。

    核心规则:
    - **私聊** (is_group=False): 仅 superuser / everyone / 自定义节点生效。
      owner/admin/member 在私聊中不适用，自动降级。
    - **群聊** (is_group=True):  以 user_id + group_id 查群角色缓存。
      owner > admin > member > everyone。

    用法:
        perm = PermissionManager(superusers=[123456])

        # 群聊中检查
        await perm.check(user_id=111, permission="admin",
                         group_id=123456, is_group=True)

        # 私聊中检查（group_id 传入 None 或不传）
        await perm.check(user_id=111, permission="superuser", is_group=False)

        # 自定义权限节点
        @perm.register("vip")
        async def check_vip(user_id, group_id, is_group):
            return user_id in some_list
    """

    ROLE_HIERARCHY = {
        "superuser": 100,
        "owner": 80,
        "admin": 60,
        "member": 20,
        "everyone": 0,
    }

    def __init__(self, superusers: list[int] = None):
        self._superusers: set[int] = set(superusers or [])
        self._custom_checkers: dict[str, Callable] = {}
        # {group_id: {user_id: role}}
        self._group_roles: dict[int, dict[int, str]] = {}

    # ---- 超级用户管理 ----

    def set_superusers(self, users: list[int]):
        self._superusers = set(users)

    def add_superuser(self, user_id: int):
        self._superusers.add(user_id)

    def remove_superuser(self, user_id: int):
        self._superusers.discard(user_id)

    def is_superuser(self, user_id: int) -> bool:
        return user_id in self._superusers

    @property
    def superusers(self) -> set[int]:
        """超管名单（副本）—— 错误反馈等只读消费者用它，别去摸 `_superusers`。"""
        return set(self._superusers)

    # ---- 群角色缓存 ----

    def set_group_role(self, group_id: int, user_id: int, role: str):
        if group_id not in self._group_roles:
            self._group_roles[group_id] = {}
        self._group_roles[group_id][user_id] = role

    def get_group_role(self, group_id: int, user_id: int) -> str:
        return self._group_roles.get(group_id, {}).get(user_id, "member")

    def bulk_set_group_roles(self, group_id: int, roles: dict[int, str]):
        """批量设置群角色 {user_id: role}"""
        if group_id not in self._group_roles:
            self._group_roles[group_id] = {}
        self._group_roles[group_id].update(roles)

    # ---- 自定义权限节点 ----

    def register(self, node: str) -> Callable:
        """注册自定义权限检查器。签名: async def(user_id, group_id, is_group) -> bool"""
        def decorator(func: Callable):
            self._custom_checkers[node] = func
            return func
        return decorator

    # ---- 已注册权限节点（供 /perm 之类的交互入口做取值校验） ----

    def known_nodes(self) -> list[str]:
        """全部可用节点：内置角色 + 已注册的自定义节点（顺序稳定，便于提示）。"""
        return list(self.ROLE_HIERARCHY) + sorted(self._custom_checkers)

    def custom_nodes(self) -> list[str]:
        """仅已注册的自定义节点（`/perm` 列取值时与内置角色分开排）。"""
        return sorted(self._custom_checkers)

    def has_node(self, node: str) -> bool:
        """节点当前是否可用 —— 注意 `""`（公开）不走这里，它由 check() 直接放行。"""
        return node in self.ROLE_HIERARCHY or node in self._custom_checkers

    # ---- 权限检查 ----

    async def check(self, user_id: int, permission: str,
                    group_id: Optional[int] = None,
                    is_group: bool = False) -> bool:
        """检查用户权限。

        Args:
            user_id: 用户 QQ 号
            permission: 权限节点名
            group_id: 群号（群聊时传入）
            is_group: 是否为群聊上下文。私聊时 owner/admin 等群角色不适用。
        """
        # 1. 超级用户无条件通过
        if user_id in self._superusers:
            return True

        # 2. everyone 始终通过
        if permission in ("everyone", DefaultPermission.EVERYONE.value):
            return True

        # 3. 内置角色权限
        if permission in self.ROLE_HIERARCHY:
            return self._check_role(user_id, permission, group_id, is_group)

        # 4. 自定义权限节点
        if permission in self._custom_checkers:
            return await self._custom_checkers[permission](user_id, group_id, is_group)

        return False

    def _check_role(self, user_id: int, permission: str,
                    group_id: Optional[int], is_group: bool) -> bool:
        required = self.ROLE_HIERARCHY[permission]
        user_level = self._resolve_user_level(user_id, group_id, is_group)
        return user_level >= required

    def _resolve_user_level(self, user_id: int, group_id: Optional[int],
                            is_group: bool) -> int:
        """计算用户在当前上下文中的等级分数"""
        if user_id in self._superusers:
            return 100

        if is_group and group_id and group_id in self._group_roles:
            # 群聊：查群角色
            role = self._group_roles[group_id].get(user_id, "member")
            return self.ROLE_HIERARCHY.get(role, 0)

        # 私聊 / 无群上下文：无角色区分，所有人平级
        return self.ROLE_HIERARCHY["member"]

    async def check_role(self, user_id: int, role: str,
                         group_id: Optional[int] = None,
                         is_group: bool = False) -> bool:
        """检查用户是否至少拥有指定角色等级"""
        if user_id in self._superusers:
            return True
        required = self.ROLE_HIERARCHY.get(role, 0)
        user_level = self._resolve_user_level(user_id, group_id, is_group)
        return user_level >= required

    # ---- 上下文解析工具 ----

    def parse_context(self, event: "MessageEvent") -> dict:
        """从消息事件提取权限检查所需的上下文参数"""
        from .models import MessageEvent
        return {
            "user_id": event.user_id,
            "group_id": event.group_id,
            "is_group": event.message_type == "group" and event.group_id is not None,
        }
