"""
权限管理系统 - 基于用户与群组的角色+自定义权限节点
区分私聊与群聊上下文：私聊仅看 user_id；群聊看 user_id + group_id
"""
from __future__ import annotations

from typing import Optional, Callable, Awaitable
from enum import Enum


#: `/userperm` 能设置的级别 —— **不含 superuser**：超管只能在 `config.json` 的
#: `superusers` 里配。一条指令就能把自己提到最高级，等于权限系统没有边界。
ASSIGNABLE_USER_LEVELS = ("owner", "admin", "member", "everyone")


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
        # {group_id: {user_id: role}} —— 群角色缓存（owner/admin/member）
        self._group_roles: dict[int, dict[int, str]] = {}
        # 显式覆盖（`/userperm` / config.json 的 user_permissions）：
        #   _user_levels        {user_id: level}                全局
        #   _group_user_levels  {group_id: {user_id: level}}    仅该群
        # 优先级：群内覆盖 > 全局覆盖 > 群角色（群主/管理员）> 默认成员
        self._user_levels: dict[int, str] = {}
        self._group_user_levels: dict[int, dict[int, str]] = {}

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

    # ---- 用户级权限覆盖（/userperm）----

    @staticmethod
    def _level(value) -> str:
        raw = getattr(value, "value", value)
        return str(raw).strip().lower()

    def set_user_level(self, user_id: int, level: str, *,
                       group_id: Optional[int] = None) -> None:
        """设置用户级覆盖：`group_id` 为空 = 全局；给了 = 仅在该群生效。"""
        level = self._level(level)
        if level not in ASSIGNABLE_USER_LEVELS:
            raise ValueError(
                f"不可设置的权限级别：{level!r}"
                f"（可用：{' / '.join(ASSIGNABLE_USER_LEVELS)}）")
        uid = int(user_id)
        if group_id:
            self._group_user_levels.setdefault(int(group_id), {})[uid] = level
        else:
            self._user_levels[uid] = level

    def clear_user_level(self, user_id: int, *,
                         group_id: Optional[int] = None) -> bool:
        """清除覆盖，返回原来是否存在（全局与群内是两套，互不影响）。"""
        uid = int(user_id)
        if group_id:
            table = self._group_user_levels.get(int(group_id), {})
            existed = table.pop(uid, None) is not None
            if not table:
                self._group_user_levels.pop(int(group_id), None)
            return existed
        return self._user_levels.pop(uid, None) is not None

    def user_level_override(self, user_id: int, *,
                            group_id: Optional[int] = None) -> tuple[str, str]:
        """命中的覆盖：`(级别, 来源)`；没有覆盖返回 `("", "")`。群内优先于全局。"""
        uid = int(user_id)
        if group_id:
            level = self._group_user_levels.get(int(group_id), {}).get(uid)
            if level:
                return level, "群内覆盖"
        level = self._user_levels.get(uid)
        if level:
            return level, "全局覆盖"
        return "", ""

    def user_level_source(self, user_id: int, *, group_id: Optional[int] = None,
                          is_group: bool = False) -> str:
        """这个人在该上下文里的级别来自哪里（诊断/`/userperm get` 用）。"""
        level, source = self.user_level_override(
            user_id, group_id=group_id if is_group else None)
        if level:
            return source
        if is_group and group_id:
            role = self.get_group_role(int(group_id), int(user_id))
            if role in self.ROLE_HIERARCHY:
                return f"群角色（{role}）"
        return "默认成员"

    def effective_user_level(self, user_id: int, *, group_id: Optional[int] = None,
                             is_group: bool = False) -> int:
        """当前上下文里的等级分数（0~100）。"""
        return self._resolve_user_level(int(user_id), group_id, is_group)

    def export_user_levels(self) -> dict:
        """导出成配置结构（空的部分不出现，避免 config.json 里堆空对象）。"""
        data: dict = {}
        if self._user_levels:
            data["users"] = {str(uid): lv
                             for uid, lv in sorted(self._user_levels.items())}
        if self._group_user_levels:
            data["groups"] = {
                str(gid): {str(uid): lv for uid, lv in sorted(table.items())}
                for gid, table in sorted(self._group_user_levels.items())}
        return data

    def import_user_levels(self, data) -> list[str]:
        """从配置恢复覆盖。容错：非法 id / 非法级别跳过并返回说明（不抛异常）。

        配置是给人手改的，写错一个键不该让整个权限系统起不来 —— 但也不能静默，
        调用方把返回的说明打进日志。
        """
        notes: list[str] = []
        if not data:
            return notes
        if not isinstance(data, dict):
            return [f"user_permissions 不是对象（{type(data).__name__}），已忽略"]

        def _uid(raw) -> int:
            try:
                uid = int(str(raw).strip())
            except (TypeError, ValueError):
                raise ValueError(f"非法 QQ：{raw!r}") from None
            if uid <= 0:
                raise ValueError(f"非法 QQ：{raw!r}")
            return uid

        users = data.get("users") or {}
        if isinstance(users, dict):
            for raw_uid, raw_level in users.items():
                try:
                    level = self._level(raw_level)
                    if level not in ASSIGNABLE_USER_LEVELS:
                        raise ValueError(f"非法级别：{raw_level!r}")
                    self.set_user_level(_uid(raw_uid), level)
                except ValueError as exc:
                    notes.append(f"user_permissions.users.{raw_uid}：{exc}")
        groups = data.get("groups") or {}
        if isinstance(groups, dict):
            for raw_gid, table in groups.items():
                if not isinstance(table, dict):
                    notes.append(f"user_permissions.groups.{raw_gid} 不是对象，已忽略")
                    continue
                try:
                    gid = _uid(raw_gid)
                except ValueError as exc:
                    notes.append(f"user_permissions.groups：{exc}")
                    continue
                for raw_uid, raw_level in table.items():
                    try:
                        level = self._level(raw_level)
                        if level not in ASSIGNABLE_USER_LEVELS:
                            raise ValueError(f"非法级别：{raw_level!r}")
                        self.set_user_level(_uid(raw_uid), level, group_id=gid)
                    except ValueError as exc:
                        notes.append(
                            f"user_permissions.groups.{raw_gid}.{raw_uid}：{exc}")
        return notes

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
        """计算用户在当前上下文中的等级分数。

        优先级（高 → 低）：
        1. 超管（config.json `superusers`，不可被用户级覆盖降级）；
        2. **群内覆盖**（`/userperm set <QQ> <级别> <群号>`）；
        3. **全局覆盖**（`/userperm set <QQ> <级别>`）；
        4. **群角色**（群主 owner / 管理员 admin —— 由每条消息的 `sender.role`
           与群管理变更事件共同维护，所以「群主/管理员默认是管理员权限」）；
        5. 默认成员（私聊 / 无群上下文同样落在这里）。
        """
        if user_id in self._superusers:
            return 100

        level, _source = self.user_level_override(
            user_id, group_id=group_id if is_group else None)
        if level:
            return self.ROLE_HIERARCHY.get(level, self.ROLE_HIERARCHY["member"])

        if is_group and group_id and group_id in self._group_roles:
            role = self._group_roles[group_id].get(user_id, "member")
            return self.ROLE_HIERARCHY.get(role, 0)

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
