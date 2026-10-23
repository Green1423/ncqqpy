# 七、权限管理

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[全部 API 方法](06-api-methods.md) · 下一章：[消息构造](08-messages.md)


## 7.1 角色层级

```
superuser(100) > owner(80) > admin(60) > member(20) > everyone(0)
```

**群主 / 管理员默认至少 admin**：客户端在每条群消息上按 `sender.role` 刷新群角色缓存
（`GroupAdminEvent` 变更时也更新），所以插件里 `check(..., "admin", group_id=gid,
is_group=True)` 对群管理员直接通过，不需要先等一次管理员变更事件。

## 7.2 命令权限绑定

```python
@self.register_command("kick", permission="admin")
async def cmd_kick(params, event): ...

@self.register_command("reload", permission="superuser")
async def cmd_reload(packet): ...
```

## 7.3 手动权限检查

```python
can = await self.perm.check(user_id=123, permission="admin", group_id=456)

is_admin = await self.perm.check_role(user_id=123, role="admin", group_id=456)
```

## 7.4 自定义权限节点

```python
@self.perm.register("vip")
async def check_vip(user_id, group_id, is_group):
    return user_id in my_vip_database
```

## 7.4.1 用户级权限覆盖（`/userperm`）

超管可在私聊用 `/userperm`（**命令组**，动作是子命令）改「某个人算几级」
（全局或仅某个群），改动写进 `config.json` 的 `user_permissions`：

```
/userperm set 987654 admin              全局 admin
/userperm set 987654 member 123456      仅群 123456 内为 member
/userperm clear 987654 123456
```

优先级：**群内覆盖 > 全局覆盖 > 群角色 > 默认成员**。可设置 `owner` / `admin` /
`member` / `everyone`（**不含 superuser**）。插件的 `self.perm.check()/check_role()`
自动套用，不用改代码。程序化写入：

```python
self.perm.set_user_level(uid, "admin")                 # 全局
self.perm.set_user_level(uid, "admin", group_id=123)   # 仅该群
self.perm.clear_user_level(uid, group_id=123)
self.perm.user_level_source(uid, group_id=123, is_group=True)   # 来源诊断
```

详见 (../guide/07-permissions.md#76-用户级权限userperm)。

## 7.5 运行时权限覆盖（`/perm` + `command_permissions`）

`/perm` 是**命令组**（`list` / `get` / `set` / `reset` 是子命令，各有独立帮助；
裸调 `/perm` 显示命令组帮助页）。超级用户可在**私聊**用 `/perm set` 改写任意已注册
命令（含子命令）的**生效权限**，改动写进
`config.json` 的 `command_permissions`，重启与插件热重载后仍生效：

```
/perm                      列出全部权限覆盖
/perm get <命令>           查看生效值 + 声明值
/perm set <命令> <权限>    设置覆盖
/perm reset <命令|all>     恢复代码声明值 / 清空全部
```

框架契约（插件作者需要知道的）：

- **覆盖值优先于 `register(permission=...)` 的声明值**。`CommandHandler.permission` 是生效值，
  新增的 `declared_permission` 是代码里的原值（`/perm reset` 恢复它；`/help <命令>` 详情在两者
  不同时标注 `（已覆盖，声明值：X）`）。
- **重放点只有命令登记处**（`CommandManager._install_handler` / `register()`）：插件热重载后
  重新注册命令时会自动重新套用覆盖，`/reload` 不会把权限改回声明值。
- **命令组本体不可覆盖**（本体不校验权限，权限属于各子命令）：`/perm set <父命令>` 会被拒绝
  并列出子命令，请逐条设置 `父.子`。
- **取值白名单** = `superuser`/`owner`/`admin`/`member` + `public`（公开；`everyone`、`公开`、
  `所有人`、`-` 等价，统一归一化为空串）+ **已注册**的自定义节点。交互设置时未注册的节点
  当场拒绝；`config.json` 里手写的未知节点只记 warning 并保留 —— 该命令对非超级用户不可用。
- **只作用于框架的命令级权限**，不影响插件内部自做的判定（如 (../../plugins/ai_chat) 的工具级权限、
  (../../plugins/bupt_news) 的服务范围白名单）。
- 命令名只支持两级（`父.子`）：三级名现在会显式 `ValueError`
  （旧实现把它静默截断成两级，注册"成功"但名字不是你要的）。

---
