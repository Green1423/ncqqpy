# 七、权限管理

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[命令系统](06-commands.md) · 下一章：[插件开发](08-plugin-dev.md)


## 7.1 角色层级

```
superuser (100) > owner (80) > admin (60) > member (20) > everyone (0)
```

- **superuser** 拥有所有权限，通过 `NapCatClient(superusers=[...])` 配置
- **owner/admin/member** 从**每条消息的 `sender.role`** 缓存（`GroupAdminEvent` 变更时也会更新）——
  所以群主 / 管理员**默认至少是 admin**，启动后不需要先发生一次管理员变更
- `"everyone"` 权限始终判定为通过

## 7.2 命令权限绑定

```python
@client.register_command("reload", permission="superuser")
async def cmd_reload(packet):
    name = packet[0]
    await client.plugins.reload(name)

@client.register_command("kick", permission="admin")
async def cmd_kick(packet, event):
    ...

@client.register_command("ping", permission="everyone")   # 等同于不设 permission
async def cmd_ping(packet):
    return "pong!"
```

## 7.3 手动权限检查

```python
# 检查权限
can = await client.perm.check(user_id=987654, permission="admin", group_id=123456)

# 检查角色级别
is_admin = await client.perm.check_role(user_id=987654, role="admin", group_id=123456)

# 管理超级用户
client.perm.add_superuser(111111)
client.perm.remove_superuser(111111)
client.perm.set_superusers([123, 456])

# 手动设置群角色
client.perm.set_group_role(group_id=123456, user_id=987654, role="admin")
```

## 7.4 自定义权限节点

```python
# 注册自定义权限检查器
@client.perm.register("vip")
async def check_vip(user_id: int, group_id: int = None) -> bool:
    # 异步查询数据库、API 等
    return user_id in some_vip_list

# 使用自定义节点
@client.register_command("vip_only", permission="vip")
async def cmd_vip(packet):
    return "你确实是 VIP"
```

## 7.5 用命令改权限（`/perm`）

超级用户私聊里可以直接改某条命令（或子命令）的权限，不用改代码、不用重启。
每个动作是独立子命令（`/help perm set` 看用法），配置里也可以单独覆盖某个子命令：

> `/perm`、`/userperm`、`/alias`、`/stop`、`/pause` 都是**命令组**：
> 子命令各有独立帮助（`/help perm set`）与权限声明，裸调父命令只显示命令组帮助页。
> **带参数的子命令一律用 Schema 声明**（不是手写用法行）：帮助里的「位置传参 /
> 键值传参 / 参数表」由 Schema 生成，参数缺失或取值非法时框架自动回
> `⚠️ 用法不正确 + /help <命令组> <子命令>`（见 (06-commands.md#612-帮助重定向裸调命令组--用法不正确)）。

```
/perm list                     列出全部覆盖
/perm get buptnews.get         查看某条命令的权限
/perm set buptnews.get admin   把「查通知详情」改成需群管理员
/perm set ping public          放开为所有人可用
/perm reset buptnews.get       恢复代码里声明的权限
/perm reset all                清空全部覆盖
```

规则：

- 目标写顶层命令名或 `父.子`（子命令）；**命令组本体**（如 `aichat`）不能设 ——
  本体不校验权限，请逐条设置子命令（`/perm set aichat.new ...`）。
- 可用取值：`superuser`、`owner`、`admin`、`member`、`public`（= 公开；`everyone`/`公开`/`-`
  等价）、以及插件已注册的自定义节点（如 `vip`）。拼错会当场拒绝并列出可用值。
- 改动落盘到 (../../config.json)：

  ```json
  "command_permissions": { "buptnews.get": "admin", "ping": "superuser" }
  ```

  重启与插件热重载后仍生效；全部清空时这个键会被删掉。手写这个键也可以（不认识的值会记
  warning 并保留，但那时该命令只有超级用户能用）。
- 超级用户在任何情况下都不受权限限制（`check()` 对超级用户无条件通过），所以不存在
  「把自己锁在外面」：改坏了照样能用 `/perm` 改回来。
- `/perm` 本身是**仅私聊 + 仅超级用户**，与 `/stop`、`/pause`、`/reload` 同档。

## 7.6 用户级权限（`/userperm`）

`/perm` 改的是「**命令**要求什么权限」，`/userperm` 改的是「**这个人**算几级」。
仅私聊 + 仅超级用户可用（与 `/perm` 同档）。

```
/userperm list                        列出全部用户级覆盖
/userperm list 123456                 只看某个群的覆盖
/userperm get 987654                  查看某人当前生效的级别与来源
/userperm set 987654 admin            设为**全局** admin（所有场景）
/userperm set 987654 member 123456    仅在该群内设为 member
/userperm set 987654 admin --group=123456   同上（键值写法）
/userperm clear 987654                清除全局覆盖
/userperm clear 987654 123456         清除该群内的覆盖
```

优先级（高 → 低）：

```
群内覆盖 > 全局覆盖 > 群角色（群主/管理员默认 admin）> 默认成员
```

- **可设置级别**：`owner` / `admin` / `member` / `everyone`。**不能设 `superuser`** ——
  超管只能在 `config.json` 的 `superusers` 里配（一条命令就能提到最高级 = 权限系统没有边界）；
  给超管设覆盖也会被拒绝并提示「超管不受用户级覆盖影响」；
- **给群号 = 仅该群**；不给 = 全局。群内覆盖只影响那一个群，别的群仍按全局/群角色算；
- 群主 / 管理员默认 admin，所以通常**不需要**给每个管理员单独配；只有「想在群里降级某个
  管理员」或「想让普通成员在某群能管理」时才用；
- 改动落盘到 (../../config.json)：

  ```json
  "user_permissions": {
    "users":  { "987654": "admin" },
    "groups": { "123456": { "987654": "member" } }
  }
  ```

  启动时恢复；手写这个键也可以 —— 非法 QQ / 非法级别只记 warning 并跳过那一条，
  不会让机器人起不来；全部清空时键会被删掉；
- 插件的 `await self.perm.check(...)` / `check_role(...)` 会自动套用这些覆盖，插件不用改。

---
