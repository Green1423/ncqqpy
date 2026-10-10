# ncqqpy 插件开发文档

> 版本 1.3.0

---

## 一、插件概述

插件是 ncqqpy 的核心扩展机制。每个插件是一个独立的 Python 包目录（`plugins/name/__init__.py`）或单文件（`plugins/name.py`），继承 `PluginBase` 即可被框架自动发现和加载。

### 1.1 最小插件

```python
# plugins/hello/__init__.py
from napcat import PluginBase

class HelloPlugin(PluginBase):
    name = "hello"
    version = "1.0.0"
    description = "我的第一个插件"
    author = "Me"

    async def on_enable(self):
        self.logger.info("Hello 插件已启用")
```

### 1.2 插件生命周期

```
PluginManager.load()
  └→ __init__(api, event_bus, timer, ...)   # 注入依赖
  └→ on_load()                               # 初始化、不注册事件

PluginManager.enable()
  └→ on_enable()                             # ★ 注册事件/命令/定时器

...运行中...                                   # 处理事件、响应命令

PluginManager.disable()
  └→ on_disable()                            # 自动取消 self._handlers

PluginManager.unload()
  └→ on_unload()                             # 释放资源、清理
```

- **`on_load`**: 仅在加载时调用一次。可用于读取配置、初始化数据库连接等。**不应在此注册事件处理器**（插件可能被禁用而不卸载）。
- **`on_enable`**: 插件激活时调用。**在此注册命令、事件处理器、定时任务**。
- **`on_disable`**: 插件停用时调用。通过 `self.on_message` / `self.on_event` 注册的处理器**自动取消**。
- **`on_unload`**: 插件卸载时调用。释放文件句柄、关闭连接等。

---

## 二、插件可用资源

`PluginBase` 实例在 `__init__` 时注入以下资源：

| 属性 | 类型 | 说明 |
|------|------|------|
| `self.api` | `ApiClient` | API 调用客户端（100+ 方法） |
| `self.event_bus` | `EventBus` | 事件总线 |
| `self.timer` | `TimerManager` | 定时任务管理器 |
| `self.cmds` | `CommandManager` | 命令管理器 |
| `self.perm` | `PermissionManager` | 权限管理器 |
| `self.files` | `FileManager` | 文件管理器 |
| `self.logger` | `Logger` | Python 标准 Logger |
| `self.errors` | `ErrorReporter` | **错误反馈业主**：`render()` / `report()` / `log()`，见 §十五 |
| `self._groups` | `GroupBinding` | 群组绑定管理器 |
| `self.client` | `NapCatClient` | 主客户端（连接状态、`launcher` 等；见 2.2） |

### 2.2 读取连接状态 / 拉起结果（`self.client`）

机器人掉线时往往**不能靠 QQ 自己报告**，需要插件观察 WebSocket 状态。
`NapCatClient` 暴露三个注册接口（回调都是 `async`，可注册多个）：

```python
class MyPlugin(PluginBase):
    async def on_enable(self):
        @self.client.on_ws_connect_failed
        async def on_failed(attempt: int, exc: BaseException):
            """连接建立不起来（服务器没上线）。每轮连续失败只触发第 1 次。"""
            self.logger.warning(f"连不上（第 {attempt} 次）: {exc!r}")

        @self.client.on_ws_connected
        async def on_connected():
            """连接就绪（首次连上 + 每次重连）。此时 self.client.self_id 已刷新。"""

        @self.client.on_ws_disconnected
        async def on_disconnected():
            """曾经连上过之后断开。启动时连不上走 on_ws_connect_failed。"""
```

| 属性 / 方法 | 说明 |
|------|------|
| `self.client.ws_connected` | 当前 WebSocket 是否可用 |
| `self.client.launcher` | `ServerLauncher`，`status()` 返回拉起次数/模式/最近结果 |
| `self.client.launcher.launch(reason)` | 手动拉起服务端（配置见 `config.json` 顶层的 `restart_*`） |

> 框架自身的拉起逻辑：连不上时按 `restart_command` 拉起服务端，**每轮连续失败只拉一次**，
> 并在后台 task 里执行（不阻塞重连循环）。该逻辑现在是框架内置的
> （`napcat/watchdog.py`，配置见 `config.json` 的 `watchdog*`）——
> 历史上的 `plugins/ncqq_watchdog` 插件已退役，只剩 `data/` 目录。

### 2.1 快捷方法

| 方法 | 说明 |
|------|------|
| `self.on_message(group_id=..., private=...)` | 注册消息事件处理器 |
| `self.on_event(EventType)` | 注册指定类型事件处理器 |
| `self.off_event(EventType, handler)` | 取消注册指定事件处理器 |
| `self.off_all_events()` | 取消注册本插件全部事件 |
| `self.register_command(name, **kwargs)` | 注册命令 |
| `self.unregister_command(name)` | 取消注册命令 |
| `self.schedule(seconds, name=...)` | 注册周期定时任务 |
| `self.schedule_at("HH:MM", name=...)` | 注册每日定时任务 |
| `self.cancel_schedule(name)` | 取消指定名称的定时任务 |
| `self.bind_group(gid)` | 绑定群 |
| `self.unbind_group(gid)` | 解绑群 |
| `self.is_group_bound(gid)` | 检查群是否已绑定 |
| `self.bound_groups` | 已绑定群列表（property） |
| `self.stop_event(hid)` | 移除指定事件监听器（按注册时的 `hid`）|
| `self.pause_event(hid)` | 暂停指定事件监听器 |
| `self.resume_event(hid)` | 恢复指定事件监听器 |
| `self.stop_command(name)` | 删除指定命令 |
| `self.pause_command(name)` | 启用/禁用命令（toggle，禁用后返回"命令不存在"） |
| `self.stop_timer(name)` | 取消指定定时任务 |
| `self.pause_timer(name)` | 暂停指定定时任务 |
| `self.resume_timer(name)` | 恢复指定定时任务 |

> **框架自动回收的范围**（不用自己写清理代码）：
> `unload()` 时会撤销本插件注册的**全部**事件处理器、**定时任务**与**命令**
> （按注册时打的归属标签，不看命名约定）。
> `on_disable()` 只撤销事件处理器 —— 停用但没卸载时，定时任务与命令仍归插件自己管
> （文档 §3.0 的写法仍然有效，重复回收是空操作）。
> 加载失败或 `on_enable` 抛异常时，这一次已经注册的东西会被框架回滚干净。

---

## 三、消息事件处理

### 3.0 动态卸载（事件/命令/定时器）

```python
from napcat import normalize_text          # 归一化入口（框架统一提供）

class MyPlugin(PluginBase):
    async def on_enable(self):
        @self.on_message()
        async def temp_handler(event: MessageEvent):
            # ⚠️ plain_text 带首尾空白（用户可能发 "  /stop\n"），
            # 自行判断前必须 normalize_text()；命令系统内部已自动归一化
            if normalize_text(event.plain_text) == "/stop":
                self.off_event(MessageEvent, temp_handler)
                await self.api.send_group_msg(
                    group_id=str(event.group_id),
                    message=MessageChain().text("已停止监听"),
                )

        @self.register_command("disable_me", permission="admin")
        async def cmd_disable(packet):
            self.unregister_command("disable_me")
            self.cancel_schedule("hourly")
            return "命令和定时任务已卸载"

        @self.schedule(3600, name="hourly")
        async def hourly(): ...
```

### 3.1 基础消息处理

```python
from napcat import PluginBase, MessageEvent, MessageChain

class MyPlugin(PluginBase):
    name = "my"
    version = "1.0.0"

    async def on_enable(self):
        @self.on_message()
        async def on_all(event: MessageEvent):
            self.logger.info(f"[{event.group_id}] {event.sender.nickname}: {event.plain_text}")

        @self.on_message(group_id=123456)
        async def on_specific_group(event: MessageEvent):
            await self.api.send_group_msg(
                group_id=str(event.group_id),
                message=MessageChain().text("本群消息已记录"),
            )

        @self.on_message(private=True)
        async def on_private(event: MessageEvent):
            await self.api.send_private_msg(
                user_id=str(event.user_id),
                message=MessageChain().text("收到私聊"),
            )
```

### 3.2 指定事件类型

```python
from napcat import (
    GroupIncreaseEvent, GroupDecreaseEvent, GroupBanEvent,
    FriendRequestEvent, PokeEvent,
)

class MyPlugin(PluginBase):
    async def on_enable(self):
        @self.on_event(GroupIncreaseEvent)
        async def welcome(event: GroupIncreaseEvent):
            await self.api.send_group_msg(
                group_id=str(event.group_id),
                message=MessageChain().at(event.user_id).text(" 欢迎!"),
            )

        @self.on_event(FriendRequestEvent)
        async def auto_accept(event: FriendRequestEvent):
            await self.api.set_friend_add_request(flag=event.flag, approve=True)
```

---

## 四、命令注册

### 4.1 基础命令

```python
@self.register_command("ping", description="Ping 测试")
async def cmd_ping(packet):
    return "pong!"
```

### 4.2 带 Schema 验证的命令

```python
from napcat import CommandSchema, QQField, DurationField, StrField

class BanSchema(CommandSchema):
    user_id = QQField(required=True, description="目标QQ")
    duration = DurationField(default=600, min=0, max=2592000)
    reason = StrField(default="", max_length=200)

@self.register_command("ban", schema=BanSchema, permission="admin",
                       scope="group", description="禁言")
async def cmd_ban(params, event):
    await self.api.set_group_ban(
        group_id=str(event.group_id),
        user_id=str(params.user_id),
        duration=params.duration,
    )
    return f"已禁言 {params.user_id} {params.duration}秒"
```

### 4.3 `register_command` 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `schema` | `CommandSchema` | `None` | 参数验证 Schema |
| `permission` | `str` | `""` | 所需权限节点，不满足返回"命令不存在" |
| `scope` | `str` | `"all"` | `"private"` / `"group"` / `"all"` |
| `description` | `str` | `""` | 命令描述（在 `/help` 中显示） |
| `usage` | `str` | `""` | 用法示例（自动从 Schema 生成；子命令同样生效） |
| `aliases` | `list[str]` | `None` | 别名列表 |

### 4.3.1 占位符记号（全框架统一）

用法行里**只允许**两种记号，`/help` 会显式告诉用户它们是占位符：

| 记号 | 含义 | 例子 |
|------|------|------|
| `<名称>` | **必填** | `/buptnews get <编号>` |
| `[名称]` | **可选**（含默认值写法 `[名称=默认]`） | `/buptnews sync [页数] [preview]` |
| `名称 ...`（在 `[]` 内） | 可变参数，吃剩余所有位置参数 | `/xxx tags [标签 ...]` |

约定与实现：

- `build_usage()` / `format_field_usage()` 由 Schema 生成用法，**不要手写**用法行 ——
  手写会与 Schema 漂移，记号也会和 `/help` 不一致；
- `describe_fields()` 生成参数表，字段名同样按 `<>` / `[]` 标注；
- 用到占位符的帮助输出（命令/子命令详情、参数表、`/help` 列表结尾的用法提示）都要带上
  `PLACEHOLDER_LEGEND` 图例，否则用户会照着 `<编号>` 原样发送；
  （框架侧 `render_help()` 只在真的渲染了占位符时才追加，命令组不要自行再加一次。）
- 报错文案里的参数提示也要用同一记号（如 `请指定群号: /bind <群号>`）。

```python
from napcat import build_usage, PLACEHOLDER_LEGEND   # 需要手写时从这里取

usage = build_usage("ban", BanSchema)   # → "ban <user_id> [duration=600] [reason]"
```

### 4.3.2 命令函数的签名（无 schema 时只有一个 `packet`）

框架按**参数名**注入，认不出的名字会被丢掉：

| 参数名 | 注入内容 |
|--------|----------|
| `packet` | `CommandPacket`（`raw` / `prefix` / `command` / `subcommand` / `args` / `kwargs` / `event`）|
| `event` | 原始 `MessageEvent` |
| `api` | `ApiClient`（等价于 `self.api`）|
| `params` | Schema 校验后的 `ValidatedParams`（**仅带 `schema=` 时**）|
| 其它名字 | 从 `params` 里按同名字段取值 |

```python
# ✅ 无 schema
@self.register_command("ping", description="探活")
async def cmd_ping(packet):
    return "pong!"

# ✅ 带 schema（params 与 event 都要）
@self.register_command("ban", schema=BanSchema, permission="admin")
async def cmd_ban(params, event):
    return f"已禁言 {params.user_id}"
```

> ⚠️ **签名写错不会静默了，但也别指望它好看。** 例如无 schema 的命令写成
> `async def cmd_ping(params, event)`：`params` 拿不到值 → 实参为空 → 调用时
> `TypeError`。框架把它包成 `PluginRuntimeError`：日志里有命令名 + 完整 traceback，
> 用户侧收到一句带错误编号的提示（见 §十五）。排查时按编号 grep 日志即可。

### 4.4 子命令（Subcommands）

子命令用于将一组相关操作组织在同一个根命令下，支持独立的 Schema 和权限。

```python
@self.register_command("group", permission="admin", scope="group",
                       description="群管理")
async def cmd_group(packet):
    return "用法: /group ban|kick|mute <参数>"

@self.register_command("group.ban", schema=BanSchema,
                       permission="admin", scope="group")
async def cmd_group_ban(params, event):
    await self.api.set_group_ban(
        group_id=str(event.group_id),
        user_id=str(params.user_id), duration=params.duration)
    return f"已禁言 {params.user_id}"

@self.register_command("group.kick", schema=KickSchema,
                       permission="owner", scope="group")
async def cmd_group_kick(params, event):
    await self.api.set_group_kick(
        group_id=str(event.group_id),
        user_id=str(params.user_id))
    return f"已踢出 {params.user_id}"
```

用法：
```
/group              → 重定向到 /help group（框架渲染的帮助页，不执行本体）
/group ban 123 600  → 执行 group.ban
/group kick 456     → 执行 group.kick（仅 owner 及以上）
```

子命令继承父命令的 `scope` 和 `permission` 作为默认值，可单独覆盖（如 `kick` 需要 `owner`）。

> 覆盖的写法有区别：子命令**不传** `permission`/`scope`（`None`）= 继承父命令；
> 传 `permission=""` = **显式声明无权限限制**（不继承）。例如
> `@self.register_command("buptnews.list", permission="", scope="all", ...)`
> 让该子命令对所有人开放，不受父命令 `admin` 默认值影响。

> **有子命令的命令 = 命令组，本体不设权限、也不会被裸调执行。** 父命令上的
> `permission`/`scope` 只作为子命令的默认值；`/group` 这类**没有子命令的裸调**由框架
> 重定向到 `/help group`（帮助页里有本体描述 + 调用者有权使用的子命令），注册在父命令上的
> 函数**不会运行** —— 想自定义这一页的内容，请写进父命令的 `description`，不要写函数体。
> （`scope` 仍按父命令自己的声明生效：范围不符时连帮助页也不给。）
> **需要权限的逻辑一律写进子命令** —— 框架在路由到子命令之前按该子命令自己的声明校验。

`/help` 的权限提示以**每条命令自身**的声明为准，与实际拦截口径一致：

- `/help` 列表**只列命令组与没有子命令的命令**，分 `[📦 命令组]` 与 `[📖 命令]` 两段，
  每行只给「命令名 — 介绍」（**版式与第二级的子命令清单一致，不按权限分段**），且只列
  调用者有权访问的那些：按各自的 `scope` 与 `permission` 过滤（`scope="private"` 的命令
  不出现在群聊帮助里；`check_permission` 不通过的也不列）。**用不了的命令直接不出现**，
  所以不需要 `[👑 superuser]` 之类的权限档标题；**子命令、以及任何命令的用法/参数也都不在
  这一级**（子命令属于 `/help <命令组>`，叶子命令的用法属于 `/help <命令名>`），
  `/help <无权限命令>` 按「未知命令」处理；
- `/help <命令组>` 逐条列出**你有权使用**的子命令：`名字 [权限：xxx · 作用域：…] — 介绍`，
  如 `/buptnews add [权限：superuser · 作用域：仅私聊] — 添加推送目标`；
  **不带参数与用法**（这些在下一级），权限或作用域不符的子命令直接不列；
- `/help <无子命令的命令>` 给出该命令的介绍、**权限/作用域**与用法/参数表；
- `/help <父命令> <子命令>` 与 `/help <父命令>.<子命令>` **等价**，显示该子命令自身的
  权限与作用域（含 `scope`）、描述、用法与参数表（**参数只在这里出现**）；第二段不是已注册的
  子命令时回落到 `/help <父命令>`（`/help perm set` 这类「用法里带取值」的写法照旧给
  `/perm` 详情）；
- `/commands`（superuser）是调试视图：命令组本体不报权限，子命令逐条列出自己的范围与说明。

帮助文本的排版约定（框架侧）：首行 `📖 <命令>`；**权限与作用域总是写全**，格式
`权限：xxx · 作用域：仅私聊/仅群聊/所有场景`（由 `describe_requirements()` 生成，
命令组页把同一句话挂在子命令名后的方括号里）；
参数块用 `位置传参：`/`键值传参：`（空默认值不再显示 `[默认: ]`）；`/help` 列表首行
`📖 可用命令（N）`（N = 命令组数 + 无子命令命令数），分段标题固定两个：`[📦 命令组]` 在前、
`[📖 命令]` 在后（**按权限分段已取消**），每行 `  /名字 — 介绍`，
结尾 `💡 /help <命令名> 查看子命令或用法`。

### 4.5 命令处理函数参数绑定

函数参数按名称自动注入：

| 参数名 | 注入值 |
|--------|--------|
| `packet` | `CommandPacket` — 命令解析结果 |
| `event` | `MessageEvent` — 原始消息事件 |
| `api` | `ApiClient` — API 客户端 |
| `params` | `ValidatedParams` — Schema 验证后的参数 |

```python
@self.register_command("test")
async def cmd_test(packet, event, api):
    # packet.args → ["arg1", "arg2"]
    # event.user_id → 发送者 QQ
    # api.send_group_msg(...)
    ...
```

### 4.5 命令返回值

- 返回 `str` / `MessageChain` → 自动回复到来源群/私聊
- 返回 `list[MessageChain]`（或 `list[str]`）→ **按顺序逐条发送**，用于长内容分段
- 返回 `None` → 不自动回复（手动调用 `self.api.send_xxx()`）

长消息建议用框架的分段工具，别自己拼字符串：

```python
@self.register_command("long")
async def cmd_long(packet):
    chain = MessageChain().text(very_long_text)
    chunks = chain.split(max_chars=1200, max_images=4)   # 0 = 不限
    return chunks[0] if len(chunks) == 1 else chunks
```

`MessageChain.split()` 的保证：文字优先在换行处断开（不破坏排版）、图片/文件段整段搬走、
分段后顺序与内容与原来一致；客户端发送时断线则停止后续分段（不会刷一堆失败）。

### 4.5.0 回复要回到「消息来的地方」

群里点开的**临时会话（小窗）**是 `message_type="private"` + `sub_type="group"` + `group_id`
（`event.is_temp_session` 为真）。自己调 API 发消息时别只按 `message_type` 分叉，否则小窗消息会
被当好友私聊发送（非好友失败）：

```python
if event.message_type == "group":
    await self.api.send_group_msg(str(event.group_id), msg)
else:
    await self.api.send_private_msg(str(event.user_id), msg,
                                    group_id=str(event.group_id) if event.is_temp_session else None)
```

命令返回值由框架自动回复，已经处理好了这一点；只有插件自己 `send_xxx` 时才需要注意。

### 4.5.05 给整组命令加一道自己的闸门（服务范围 / 白名单）

框架的 `permission` 回答的是「这个人有没有资格执行这条命令」，管不了「这个地址/人能不能用
**这个插件**」（例如「只服务绑定群的小窗 + 指定用户」）。要加这类闸门，别逐条命令手写判断 ——
在**登记处**统一包一层，后加的子命令就不会漏：

```python
def _register_commands(self):
    original_register = self.register_command

    def _guarded(func):
        wanted = tuple(inspect.signature(func).parameters)     # 原函数的参数名

        @functools.wraps(func)
        async def wrapper(packet=None, params=None, **kwargs):
            event = getattr(packet, "event", None)             # 事件只能从 packet 取
            if not self.check_access(event):
                return None                                    # 静默：与「无权限=未知命令」同口径
            forward = {k: v for k, v in kwargs.items() if k in wanted}
            if "packet" in wanted:
                forward["packet"] = packet
            if "params" in wanted and params is not None:
                forward["params"] = params
            if "event" in wanted and event is not None:
                forward["event"] = event
            return await func(**forward)

        # ⚠️ 关键：显式覆盖签名（原因见下），否则框架看不到 packet / params
        wrapper.__signature__ = inspect.Signature([
            inspect.Parameter("packet", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None),
            inspect.Parameter("params", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=None)])
        return wrapper

    def _register_with_guard(name, **kw):
        decorator = original_register(name, **kw)

        def _wrap(func):
            return decorator(_guarded(func))
        return _wrap

    self.register_command = _register_with_guard
```

两个**静默**陷阱（`bupt_news` 的 `binding` 实测全踩过）：

1. **框架按参数名注入**（`CommandHandler.execute()` 遍历
   `inspect.signature(self.func).parameters`）。`functools.wraps` 会把 `__signature__`
   指回原函数，于是包装器自己多写的参数**看起来毫无作用**：`cmd_list(params)` 这类命令
   连 `event` 都拿不到，守卫从 `**kwargs` 里取事件永远是 `None` → 对这类命令**全部放行**。
   解决：显式 `wrapper.__signature__`，且**只**声明 `packet` / `params`（把 `event` 写进去，
   框架会把它塞给不收 event 的命令 → `TypeError` → 被吞成「命令没反应」）。
2. **被包装器形参接住的参数不会出现在 `**kwargs` 里**，只按 kwargs 转发会把它们全丢掉
   （`cmd_status(packet)` 直接报 missing argument）。所以 `packet` / `params` / `event`
   都要按 `wanted` 手工放回。

另：`/help` 要与执行同口径，用 `cmds.add_visibility_filter(...)` 装同一条判定
（签名 `(handler, event) -> bool`，可同步可异步；见 `/help` 的可见性过滤），
否则范围外的用户会看到一份用不了的功能清单。

### 4.5.1 字符串参数的引号与转义

`StrField` 拿到的是**分词并反转义后**的字符串，写命令时直接提示用户加引号即可：
含空格的文本用 `"…"` 或 `'…'` 包起来；文本里有单引号就写成 `'don\'t'`（框架在单引号内
也支持 `\'`；此外 `\n` `\t` `\\` `\"` 都会转义）。引号没配对时框架按空白宽松切分，
不会让命令报错。

### 4.6 Schema 字段类型

| Field | Python 类型 | 特性 |
|-------|-------------|------|
| `StrField` | `str` | `min_length`, `max_length`, `regex`, `choices` |
| `IntField` | `int` | `min`, `max`, `choices` |
| `FloatField` | `float` | `min`, `max` |
| `BoolField` | `bool` | 支持 true/false/1/0/yes/no |
| `QQField` | `int` | 自动校验 QQ 号范围，支持 `[@123456]` 格式 |
| `DurationField` | `int`(秒) | 支持 `600` / `10m` / `1h30m` / `2d` |
| `ChoiceField` | `str` | 枚举选项，大小写不敏感 |
| `VariadicField` | `list[str]` | 接收剩余所有位置参数 |

---

## 五、全部事件类型

| 事件类 | post_type | notice_type | 触发场景 |
|--------|-----------|-------------|----------|
| `HeartbeatEvent` | meta_event | — | 定时心跳 |
| `LifeCycleEvent` | meta_event | — | 连接/启用/禁用 |
| `MessageEvent` | message | — | 接收私聊/群聊消息 |
| `GroupIncreaseEvent` | notice | group_increase | 群成员增加 |
| `GroupDecreaseEvent` | notice | group_decrease | 群成员减少 |
| `GroupBanEvent` | notice | group_ban | 群禁言/解禁 |
| `GroupAdminEvent` | notice | group_admin | 管理员设置/取消 |
| `GroupRecallEvent` | notice | group_recall | 群消息撤回 |
| `GroupUploadEvent` | notice | group_upload | 群文件上传 |
| `GroupCardEvent` | notice | group_card | 群名片修改 |
| `GroupNameEvent` | notice | group_name | 群名修改 |
| `GroupTitleEvent` | notice | title | 群头衔变更 |
| `PokeEvent` | notice | poke | 戳一戳 |
| `InputStatusEvent` | notice | input_status | 对方正在输入 |
| `ProfileLikeEvent` | notice | profile_like | 资料卡点赞 |
| `GroupGrayTipEvent` | notice | gray_tip | 群灰条消息 |
| `FriendAddEvent` | notice | friend_add | 好友添加 |
| `FriendRecallEvent` | notice | friend_recall | 好友消息撤回 |
| `EssenceEvent` | notice | essence | 精华消息变更 |
| `MsgEmojiLikeEvent` | notice | group_msg_emoji_like | 消息表情点赞 |
| `BotOfflineEvent` | notice | bot_offline | Bot 掉线 |
| `OnlineFileSendEvent` | notice | online_file_send | 在线文件发送 |
| `OnlineFileReceiveEvent` | notice | online_file_receive | 在线文件接收 |
| `FriendRequestEvent` | request | — | 好友请求 |
| `GroupRequestEvent` | request | — | 群请求/邀请 |

### MessageEvent 常用属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `message_id` | int | 消息 ID |
| `user_id` | int | 发送者 QQ |
| `group_id` | int? | 群号（群消息） |
| `message_type` | str | `"private"` / `"group"` |
| `sender.nickname` | str | 发送者昵称 |
| `sender.card` | str? | 群名片 |
| `sender.role` | str? | `"owner"` / `"admin"` / `"member"` |
| `plain_text` | str | 纯文本（自动提取；**首尾空白保留原样**，自行比较前先 `normalize_text()`）|
| `raw_message` | str | CQ 码原始字符串 |
| `message` | list/str | 原始消息段数组 |
| `get_segments(type)` | list[dict] | 提取指定类型消息段 |

---

## 六、全部 API 方法

### 6.1 消息

| 方法 | 参数 | 返回 |
|------|------|------|
| `send_msg(*, message_type, user_id, group_id, message, auto_escape)` | | `SendMsgResult` |
| `send_private_msg(user_id, message, auto_escape)` | | `SendMsgResult` |
| `send_group_msg(group_id, message, auto_escape)` | | `SendMsgResult` |
| `delete_msg(message_id)` | | `None` |
| `get_msg(message_id)` | | `Any` |
| `get_forward_msg(*, message_id, id)` | | `Any` |
| `send_forward_msg(*, message_type, group_id, user_id, messages)` | | `Any` |
| `mark_msg_as_read(*, user_id, group_id)` | | `None` |
| `set_msg_emoji_like(message_id, emoji_id, is_add)` | | `None` |
| `fetch_ptt_text(message_id)` | | `str` |
| `get_friend_msg_history(user_id)` | | `Any` |
| `get_group_msg_history(group_id)` | | `Any` |

### 6.2 好友/用户

| 方法 | 参数 | 返回 |
|------|------|------|
| `get_friend_list(no_cache)` | | `list[User]` |
| `get_stranger_info(user_id)` | | `Any` |
| `get_friends_with_category()` | | `Any` |
| `set_friend_add_request(flag, approve, remark)` | | `None` |
| `set_friend_remark(user_id, remark)` | | `None` |
| `delete_friend(user_id)` | | `None` |
| `send_like(user_id, times)` | | `None` |
| `get_recent_contact()` | | `Any` |
| `get_profile_like()` | | `Any` |

### 6.3 群组

| 方法 | 参数 | 返回 |
|------|------|------|
| `get_group_list(no_cache)` | | `list[Group]` |
| `get_group_info(group_id)` | | `Group` |
| `get_group_member_list(group_id, no_cache)` | | `list[GroupMember]` |
| `get_group_member_info(group_id, user_id, no_cache)` | | `GroupMember` |
| `get_group_honor_info(group_id, type)` | | `Any` |
| `set_group_ban(group_id, user_id, duration)` | | `None` |
| `set_group_whole_ban(group_id, enable)` | | `None` |
| `set_group_admin(group_id, user_id, enable)` | | `None` |
| `set_group_kick(group_id, user_id, reject_add_request)` | | `None` |
| `set_group_leave(group_id, is_dismiss)` | | `None` |
| `set_group_name(group_id, group_name)` | | `None` |
| `set_group_card(group_id, user_id, card)` | | `None` |
| `set_group_special_title(group_id, user_id, special_title)` | | `None` |
| `set_group_add_request(flag, sub_type, approve, reason)` | | `None` |
| `get_group_at_all_remain(group_id)` | | `Any` |
| `set_group_sign(group_id)` | | `None` |
| `send_group_sign(group_id)` | | `None` |

### 6.4 文件

| 方法 | 参数 | 返回 |
|------|------|------|
| `get_image(file)` | | `FileInfo` |
| `get_record(file)` | | `FileInfo` |
| `get_file(file)` | | `FileInfo` |
| `upload_group_file(group_id, file, name, folder, upload_file)` | | `Any` |
| `download_file(*, url, base64, name, headers)` | | `FileInfo` |
| `get_group_root_files(group_id)` | | `Any` |
| `get_group_files_by_folder(group_id, folder_id)` | | `Any` |

### 6.5 系统/扩展

| 方法 | 参数 | 返回 |
|------|------|------|
| `get_login_info()` | | `LoginInfo` |
| `get_status()` | | `StatusInfo` |
| `get_version_info()` | | `VersionInfo` |
| `can_send_image()` | | `bool` |
| `can_send_record()` | | `bool` |
| `get_cookies(domain)` | | `Any` |
| `set_online_status(status, ext_status, battery_status)` | | `None` |
| `set_qq_profile(nickname, personal_note, sex)` | | `None` |
| `set_qq_avatar(file)` | | `None` |
| `ocr_image(image)` | | `Any` |
| `translate_en2zh(words)` | | `Any` |
| `send_poke(user_id, group_id, target_id)` | | `None` |
| `send_qzone_msg(content, images, ugc_right, target_uins)` | | `Any` |
| `delete_qzone_msg(tid)` | | `None` |

### 6.6 错误处理

```python
from napcat import ApiError

try:
    await self.api.set_group_ban("123", "456", 600)
except ApiError as e:
    self.logger.error(f"API 错误 [{e.retcode}]: {e.message}")
```

> **上报节流**：所有 API 调用发送前统一等待 `api_delay`（默认 0.5–1.0s）并串行化，
> 插件不需要自己 sleep 限速；反过来说，**别在一个事件里同步连发上百个 API** ——
> 它们会排队，每个仍要等 0.5–1s。
> **断线期间**调用 API **不会立即失败**：框架把调用排队留住，服务器一上线就按序补发
> （`offline_hold_timeout` 控制最长等待，默认 0 = 一直等；`offline_queue_max` 控制排队上限）。
> 只有**已经发出但没等到响应**的调用会立即抛 `ConnectionError`（不自动重发，避免重复消息），
> 这类失败按业务自行重试即可。

---

## 七、权限管理

### 7.1 角色层级

```
superuser(100) > owner(80) > admin(60) > member(20) > everyone(0)
```

### 7.2 命令权限绑定

```python
@self.register_command("kick", permission="admin")
async def cmd_kick(params, event): ...

@self.register_command("reload", permission="superuser")
async def cmd_reload(packet): ...
```

### 7.3 手动权限检查

```python
can = await self.perm.check(user_id=123, permission="admin", group_id=456)

is_admin = await self.perm.check_role(user_id=123, role="admin", group_id=456)
```

### 7.4 自定义权限节点

```python
@self.perm.register("vip")
async def check_vip(user_id, group_id, is_group):
    return user_id in my_vip_database
```

### 7.5 运行时权限覆盖（`/perm` + `command_permissions`）

超级用户可在**私聊**用 `/perm` 改写任意已注册命令（含子命令）的**生效权限**，改动写进
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
- **只作用于框架的命令级权限**，不影响插件内部自做的判定（如 `ai_chat` 的工具级权限、
  `bupt_news` 的服务范围白名单）。
- 命令名只支持两级（`父.子`）：三级名现在会显式 `ValueError`
  （旧实现把它静默截断成两级，注册"成功"但名字不是你要的）。

---

## 八、消息构造

```python
from napcat import MessageChain, MusicPlatform, ContactType

# 基础
MessageChain().text("你好").at("123456").face("123")

# 多媒体
MessageChain().image("https://example.com/photo.jpg")
MessageChain().record("https://example.com/voice.amr")

# 回复
MessageChain().reply(seq=123456).text("收到")

# 合并转发
MessageChain().node(user_id=123, nickname="小明",
    content=MessageChain().text("转发的消息"))

# 音乐
MessageChain().music_id(MusicPlatform.QQ, "12345")
MessageChain().music_custom(url="...", image="...", title="歌名")

# 交互
MessageChain().dice().rps("石头").poke()

# 输出
chain.build()    # → [{"type":"text","data":{...}}, ...]
chain.to_cq()    # → "你好[CQ:at,qq=123456]"
```

---

## 九、定时任务

```python
@self.schedule(60, name="save")          # 每60秒
async def periodic():
    self.logger.info("定时任务执行")

@self.schedule_at("08:00", name="morning")  # 每天8点
async def daily():
    await self.api.send_private_msg(
        user_id="123456",
        message=MessageChain().text("早安"),
    )

@self.schedule(3600)  # 无 name 时用函数名
async def hourly_check(): ...
```

---

## 十、文件管理

```python
# 获取图片
info = await self.files.get_image("file_id")
print(info.file, info.url, info.size)

# 下载
path = await self.files.download("https://example.com/doc.pdf", save_path="./dl/")

# 上传群文件
await self.files.upload_group_file(
    group_id="123456", file_path="./test.pdf", name="文档.pdf")
```

---

## 十一、完整插件示例

```python
# plugins/welcome/__init__.py
from napcat import (
    PluginBase, MessageEvent, MessageChain,
    GroupIncreaseEvent, CommandSchema, StrField,
)

class WelcomeSchema(CommandSchema):
    text = StrField(default="欢迎加入！", max_length=200)

class WelcomePlugin(PluginBase):
    name = "welcome"
    version = "1.0.0"
    description = "入群欢迎 + 自定义欢迎语"
    author = "ncqqpy"

    async def on_load(self):
        self._welcome_text = "欢迎加入！"

    async def on_enable(self):
        # 入群欢迎
        @self.on_event(GroupIncreaseEvent)
        async def say_hello(event: GroupIncreaseEvent):
            await self.api.send_group_msg(
                group_id=str(event.group_id),
                message=MessageChain()
                    .at(event.user_id)
                    .text(f" {self._welcome_text}"),
            )

        # 修改欢迎语
        @self.register_command("welcome", schema=WelcomeSchema,
                               permission="admin", scope="group")
        async def set_welcome(params):
            self._welcome_text = params.text
            return f"欢迎语已改为: {self._welcome_text}"

        # 定时清理
        @self.schedule(86400, name="daily_cleanup")
        async def cleanup():
            self.logger.info("每日清理完成")
```

---

## 十二、热重载

开发时保存文件即可自动生效（2 秒检测间隔）。也可手动触发：

```
/reload              # 重载全部插件（含新添加的）
/reload welcome      # 热重载指定插件
```

关闭自动监控：`client.plugins.stop_watch()`

---

## 十三、插件包结构

```
plugins/
├── welcome/              # 包目录格式（推荐）
│   ├── __init__.py       # 包含 PluginBase 子类
│   └── utils.py          # 自定义辅助模块
├── admin.py              # 单文件格式
└── _disabled_tag.py      # 以下划线开头 → 跳过加载
```

---

## 十四、配置管理

### 14.1 config.json

首次启动自动生成 `config.json`，所有静态配置集中在此：

```json
{
  "ws_url": "ws://127.0.0.1:3001",
  "plugin_dir": "./plugins",
  "superusers": [123456789],
  "command_prefixes": ["/", "!"],
  "bound_groups": [100200300],
  "all_groups": false,
  "plugins": {}
}
```

构造参数覆盖配置文件值：

```python
client = NapCatClient()                           # 全走 config.json
client = NapCatClient(superusers=[111])           # 部分覆盖
```

### 14.2 bind / unbind 自动持久化

绑定/解绑群时自动同步 `config.json`，重启后保持。

### 14.3 插件配置

`self.config` 返回插件专属命名空间代理，读写自动隔离到 `plugins.<插件名>.*`：

```python
class MyPlugin(PluginBase):
    name = "my_plugin"

    async def on_load(self):
        # 配置项已经在加载前按 config_template() 补齐（见 14.3.1），
        # 这里直接读就行，不用再 setdefault
        self.threshold = self.config.get("threshold")

    async def on_disable(self):
        self.config.set("last_run", time.time())
```

#### 14.3.1 首次运行即初始化全部配置项（`config_template()`）

**契约**：每个插件都要实现 `config_template()`，返回自己的**完整**默认配置；
框架在**插件构造时**（`PluginBase.__init__`，即 `on_load()` 之前）把它补进
`config.json` 的 `plugins.<插件名>` 段。运维打开配置文件就能看到这个插件有哪些
开关可改，而不是只能翻代码。

> 放在构造期而不是加载期，是为了让保证对**任何**构造路径都成立：框架加载、
> 测试里直接 `MyPlugin(...)`、脚本里手动 new —— 都拿得到完整配置，不依赖调用方
> 记得补这一步（`tests/test_watchdog.py` 里就有直接 new 插件的用例）。

```python
class MyPlugin(PluginBase):
    name = "my_plugin"

    def config_template(self) -> dict:
        """出厂默认值（框架负责写进 config.json）。"""
        return {
            "enabled": True,
            "threshold": 100,
            "tags": ["a", "b"],
            "llm": {"model": "", "timeout": 60},     # 嵌套段逐键补
        }
```

四条语义（由 `PluginConfig.fill_defaults()` 实现）：

| 语义 | 说明 |
|------|------|
| **只补缺键** | 已有值（含运维手改的）**一个都不覆盖**；插件升级新增配置项时老部署自动补齐 |
| **递归补全** | 父键已存在也照样补里面缺的子键 —— `setdefault("llm", {...})` 在用户已配 `llm` 时整段不生效，这个坑踩过不止一次 |
| **幂等** | 没有缺键就完全不写盘；有缺键也只写一次（不是逐键重写整个文件） |
| **类型冲突只报告** | 模板要 dict、用户写成了别的类型 → 保留用户值，启动日志里 warning |

其它约定：

- 模板放**出厂默认值**，不要放运行期算出来的值；返回的字典会被深拷贝后再写入；
- **凭据留空占位**（`api_key: ""` 之类），真实密钥建议只放环境变量或由运维填；
- 模板要在**构造期**就能算出来（别依赖 `on_load` 里才建立的实例状态）；
- 补配置前框架会先按磁盘重读 `config.json`，所以手工编辑过的内容不会被启动时的
  旧快照覆盖；
- 模板写错（`config_template()` 抛异常）**不会**让插件起不来：插件照常可用，
  错误私聊超管；
- 不想写任何配置项就返回 `{}`（默认实现），框架不会创建空的 `plugins.<插件名>` 段。

> 运行期想主动补一次（例如运维删了键之后）用
> `self.config.fill_defaults(self.config_template())`，它返回
> `ConfigFill(filled, conflicts)`；`/aichat config` 就是这么做的。

### 14.4 ConfigManager API

| 方法 | 说明 |
|------|------|
| `cfg.get(key, default)` | 读（支持 `a.b.c` 点号路径） |
| `cfg.set(key, value)` | 写（自动保存） |
| `cfg.delete(key)` | 删 |
| `cfg.has(key)` | 是否存在 |
| `cfg.setdefault(key, default)` | 不存在时写入默认值 |
| `cfg.keys(prefix)` | 列出前缀下所有 key |
| `cfg.plugin(name)` | 返回 `PluginConfig` 代理 |

---

## 十五、错误处理与用户反馈

**一句话**：插件**抛什么异常**决定用户看到什么、谁收到；**怎么发**由框架统一负责，
插件不要自己拼「出错了」的文案。

### 15.1 三种写法，按你掌握的信息量选

```python
from napcat import PluginConfigError, PluginTimeoutError, PluginRateLimitError

class MyPlugin(PluginBase):
    async def on_enable(self):
        @self.register_command("ask", schema=AskSchema)
        async def cmd_ask(params, event):
            if not self.config.get("api_key"):
                # ① 用户看得懂、也知道该找谁 → 直接给文案（原文回给用户）
                raise PluginConfigError("没配 api_key，找超管在 config.json 里补")

            try:
                return await self._call_upstream(params.q)
            except TimeoutError as e:
                # ② 分类明确 → 抛对应类型：文案自带「可以稍后再试」，
                #    日志与用户文案里都带同一个错误编号
                raise PluginTimeoutError(cause=e, plugin=self.name) from e
            except UpstreamQuota as e:
                raise PluginRateLimitError(cause=e, plugin=self.name) from e
            # ③ 其它异常：别 catch —— 框架包成 PluginRuntimeError 记录完整堆栈，
            #    用户收到「运行错误（E-xxxx）+ 短原因」，不会有「有问不答」
```

`self.errors`（`ErrorReporter`）是给**需要自己投递**的插件用的
（例如 ai_chat 要按会话分段/出图）：`render()` 取文案、`log()` 记日志（带编号）。

### 15.2 错误类型速查（`from napcat import ...`）

| 类型 | 分类文案 | 可重试 | 受众 | 用在哪 |
|------|---------|--------|------|--------|
| `PluginConfigError` | 配置错误 | — | 触发者 | 配置缺失/非法，用户能自己改或找超管 |
| `PluginTimeoutError` | 上游超时 | ✅ | 触发者 | 上游/网络超时 |
| `PluginRateLimitError` | 被限流 | ✅ | 触发者 | 被限流、额度用尽 |
| `PluginExternalError` | 上游错误 | ✅ | 触发者 | 其它外部依赖失败（父类）|
| `PluginAccessError` | 无权访问 | — | 触发者 | 服务范围/白名单拒绝 |
| `PluginRuntimeError` | 运行错误 | — | 触发者 | 插件 bug（框架兜底用，也可主动抛）|
| `CommandExecutionError` | 命令失败 | — | 触发者 | 等价于「构造参数就是要回给用户的那句话」（历史写法，仍支持）|
| `PluginLoadError` / `PluginLifecycleError` | 装载/生命周期失败 | — | **超管** | 框架在加载、`on_*` 钩子失败时用 |

### 15.3 框架怎么反馈

| 出错位置 | 谁收到 | 说明 |
|---------|--------|------|
| 命令处理函数抛异常 | 触发命令的人 | 群聊里 **@ 他**，私聊直接回；带「分类 + 短原因 + 编号」|
| 事件处理器抛异常 | 触发事件的人 | 同上（定时器/通知类事件没有触发者时转超管）|
| 定时任务抛异常 | 超管私聊 | 没有「触发者」可回 |
| 插件加载 / 启用 / 卸载 / 重载失败 | 超管私聊 | 群里不暴露内部故障细节 |
| 回复发送失败（断线等）| 超管私聊 | 触发者此刻联系不上，只能让运维知道 |

文案形如：

```
⚠️ 执行 /aichat ask 时出错（E-3f21）：PluginTimeoutError: 上游 120s 没响应（可以稍后再试）
```

**编号**由「插件 + 分类 + 异常类型」稳定推导（`error_code()`）。用户报编号时，
`grep E-3f21 日志` 就能找到全部同类失败及其堆栈。

**节流**：同一编号在 `error_report.throttle_seconds`（默认 60s）内重复出现时，
只回一句 `⚠️ 又出错了（E-3f21），稍后再试` —— **仍然有回应**（静默才是 bug），
但不会把群刷屏；日志从不节流。

### 15.4 配置（`config.json` 顶层）

```json
"error_report": {
  "enabled": true,          // false = 只写日志，不回任何错误文案
  "throttle_seconds": 60,   // 同一编号的最小重复间隔
  "show_reason": true,      // false = 只给编号不附短原因
  "notify_superusers": true // false = 生命周期故障不私聊超管
}
```

### 15.5 插件作者要守的两条

1. **不要自己 `send_xxx` 拼错误文案** —— 你的插件不会 @ 人、没有编号、不管节流；
   同类失败在群里会以 N 种写法出现。要么抛类型，要么用 `self.errors`。
2. **不要用 `except Exception: pass` 把失败吞掉** —— 用户侧表现就是「有问不答」。
