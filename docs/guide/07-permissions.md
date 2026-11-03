# 七、权限管理

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[命令系统](06-commands.md) · 下一章：[插件开发](08-plugin-dev.md)

权限系统回答两个问题：**这条命令要什么级别**、**这个人是什么级别**。两者匹配才允许执行，
不匹配时按「命令不存在」处理，不向用户暴露命令存在。

> 本文示例号码均为虚构，替换成你自己的 QQ / 群号即可。

## 7.1 角色层级

```text
superuser (100) > owner (80) > admin (60) > member (20) > everyone (0)
```

| 角色 | 来源 | 说明 |
|------|------|------|
| `superuser` | `config.json` 的 `superusers` | 拥有全部权限，不受任何限制 |
| `owner` / `admin` | 群里的实际身份 | 群主、管理员默认拥有 `admin` 级别的权限 |
| `member` | 默认 | 普通群成员、私聊用户 |
| `everyone` | — | 任何人都满足，等价于「公开」 |

- 群主 / 管理员不需要额外配置，进群后自动按 `admin` 级别判定；
- 用户在某个群被降级 / 提级，用 [`/userperm`](#76-用户级权限userperm) 单独设置；
- 私聊没有群角色，默认是 `member`。

## 7.2 命令权限绑定

注册命令时声明 `permission`，不满足的用户看不到也执行不了：

```python
@client.register_command("ping", description="测试延迟")
async def cmd_ping(packet):
    return "pong!"                      # 不写 permission = 公开


@client.register_command("kick", permission="admin", scope="group",
                         description="踢出群成员")
async def cmd_kick(packet, event):
    ...


@client.register_command("bind", permission="superuser", scope="private",
                         description="绑定群")
async def cmd_bind(packet):
    ...
```

| 声明 | 含义 |
|------|------|
| 不写 / `permission=""` | 公开，任何人都能用 |
| `permission="admin"` | 群管理员及以上 |
| `permission="superuser"` | 仅超级用户 |
| `scope="private"` | 仅私聊可用 |
| `scope="group"` | 仅群聊可用 |
| 不写 `scope` | 私聊、群聊都可用 |

## 7.3 手动权限检查

命令之外（事件处理、定时任务里）需要判断权限时：

```python
# 用户是否满足某权限节点
ok = await client.perm.check(user_id=100000001, permission="admin",
                             group_id=100000002)

# 用户是否达到某角色级别
is_admin = await client.perm.check_role(user_id=100000001, role="admin",
                                        group_id=100000002)

# 超级用户管理
client.perm.add_superuser(100000009)
client.perm.remove_superuser(100000009)
client.perm.set_superusers([100000009, 100000010])

# 手动设置群角色（一般不必要，群身份会自动同步）
client.perm.set_group_role(group_id=100000002, user_id=100000001, role="admin")
```

私聊场景把 `group_id` 省略即可。

## 7.4 自定义权限节点

除了内置角色，还可以注册自定义节点（例如 VIP、白名单）：

```python
@client.perm.register("vip")
async def check_vip(user_id: int, group_id: int = None) -> bool:
    return user_id in vip_list


@client.register_command("vip_only", permission="vip",
                         description="仅 VIP 可用")
async def cmd_vip(packet):
    return "你确实是 VIP"
```

节点名就是 `permission` 的取值；`/perm` 里也能看到并使用已注册的节点。

## 7.5 用命令改权限（`/perm`）

超级用户在私聊里可以改某条命令（或子命令）的权限，不用改代码、不用重启。
`/perm` 是命令组，裸调只显示组帮助；每个动作是独立子命令（`/help perm set` 看用法）。

```text
/perm list                     列出全部权限覆盖
/perm get buptnews.get         查看某条命令的权限
/perm set buptnews.get admin   改成群管理员可用
/perm set ping public          放开为所有人可用
/perm reset buptnews.get       恢复默认权限
/perm reset all                清空全部覆盖
```

规则：

- 目标写顶层命令名或 `父.子`（子命令）；命令组本体不能设，请逐条设置子命令
  （`/perm set aichat.new ...`）；
- 可用取值：`superuser`、`owner`、`admin`、`member`、`public`（= 公开，`everyone` / `公开` /
  `-` 等价），以及已注册的自定义节点；拼错会当场拒绝并列出可用值；
- 改动写入 `config.json`，重启、插件热重载后仍生效：

  ```json
  "command_permissions": {"buptnews.get": "admin", "ping": "superuser"}
  ```

  全部清空时这个键会被删除；手写这个键也可以，无法识别的值会保留并只允许超级用户使用；
- 超级用户不受权限限制，所以不存在「把自己锁在外面」；改坏了照样能用 `/perm` 改回来；
- `/perm` 仅私聊 + 仅超级用户，与 `/stop`、`/pause`、`/reload` 同档。

## 7.6 用户级权限（`/userperm`）

`/perm` 改的是「**命令**要什么权限」，`/userperm` 改的是「**这个人**是什么级别」。
仅私聊 + 仅超级用户可用。

```text
/userperm list                        列出全部用户级覆盖
/userperm list 100000002              只看某个群的覆盖
/userperm get 100000001               查看某人当前生效的级别与来源
/userperm set 100000001 admin         设为全局 admin
/userperm set 100000001 member 100000002   仅在该群内设为 member
/userperm set 100000001 admin --group=100000002   同上（键值写法）
/userperm clear 100000001             清除全局覆盖
/userperm clear 100000001 100000002   清除该群内的覆盖
```

优先级从高到低：

```text
群内覆盖 > 全局覆盖 > 群角色（群主/管理员默认 admin）> 默认成员
```

- 可设置级别：`owner` / `admin` / `member` / `everyone`；
- **不能设 `superuser`**：超级用户只能在 `config.json` 的 `superusers` 里配置，
  对超管设置覆盖会被拒绝；
- 给群号 = 仅该群生效；不给 = 全局。群内覆盖只影响那一个群；
- 群主 / 管理员默认 admin，通常不需要逐个配置；只有要降级某个管理员、或让普通成员
  在某群拥有管理权限时才用；
- 改动写入 `config.json`：

  ```json
  "user_permissions": {
    "users":  {"100000001": "admin"},
    "groups": {"100000002": {"100000001": "member"}}
  }
  ```

  启动时自动恢复；手写的非法 QQ / 级别会被跳过并记录日志，不影响启动；全部清空时
  键会被删除；
- 插件的 `await self.perm.check(...)` / `check_role(...)` 会自动套用这些覆盖，插件无需改动。
