# 四、命令注册

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[消息事件处理](03-events.md) · 下一章：[全部事件类型](05-event-types.md)


## 4.1 基础命令

```python
@self.register_command("ping", description="Ping 测试")
async def cmd_ping(packet):
    return "pong!"
```

## 4.2 带 Schema 验证的命令

```python
from napcat.command import CommandSchema, QQField, DurationField, StrField
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

## 4.3 `register_command` 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `schema` | `CommandSchema` | `None` | 参数验证 Schema |
| `permission` | `str` | `""` | 所需权限节点，不满足返回"命令不存在" |
| `scope` | `str` | `"all"` | `"private"` / `"group"` / `"all"` |
| `description` | `str` | `""` | 命令描述（在 `/help` 中显示） |
| `usage` | `str` | `""` | 用法示例（自动从 Schema 生成；子命令同样生效） |
| `aliases` | `list[str]` | `None` | 别名列表（子命令同样生效，作用域限定在父命令组内；超管可用 `/alias` 增删改，配置会接管这里声明的集合） |

> **命令名规则**：`Unicode 字母`开头，后续可含字母、数字、`_`、`-`；支持中文
> （`register_command("签到", ...)`、子命令 `"点歌.列表"`、别名 `"签"`）。
> 注册期就会校验，非法名字直接 `ValueError` —— 不会出现「注册成功但解析不到」。

> **别名**：`register_command("demo", aliases=["d"])` 之后 `/d` 与 `/demo` 等价
> （共用同一套权限与参数定义）。运行期可用 `/alias add|del|set|move`
> 管理（仅私聊 + 超管），落盘在 `config.json` 的 `command_aliases`；
> 配置存在时是该命令别名的**全集**（空列表 = 清空，会压掉代码里的声明值）。
> 别名支持**顶层命令、命令组与子命令**：顶层/组别名可继续接子命令
> （`/别名 子命令`）；子命令别名是**组内作用域**（`aliases=["列"]` 或
> `/alias add 列 点歌.列表` 之后用 `/点歌 列`，不会生成顶层 `/列`）。
> 命令被 `/stop command` 或插件卸载时，别名条目一起清掉，重注册后恢复。

## 4.3.1 占位符记号（全框架统一）

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

**用法不足 / 写错时不要自己拼用法提示。** 规范是"命令组 + 子命令，且子命令有参数时
重定向到 `/help <命令组> <子命令>`"：

| 情况 | 谁负责 | 插件要做什么 |
|------|--------|--------------|
| 必填参数缺失/类型不对（Schema 校验失败） | 框架自动回「⚠️ 用法不正确 + 该子命令帮助页」 | 什么都不用做 |
| **可变参数**（`VariadicField` 不参与必填校验）内容不合法 | 同上，但框架不知道 | `return ("_schema_error", "原因")` |
| 裸调命令组（`/cmd`） | 框架重定向到 `/help cmd` | 命令组本体**不要写函数体**（写了也不会运行） |

参数不合法时会先回一句原因，再附上该命令的帮助页：
「⚠️ 用法不正确：<原因>」外加**完整的帮助页**（用法行、参数表、默认值）。
手写 `💡 用法：/x add group <群号>` 这类提示会与 Schema 漂移，且拿不到参数表 —— 不要写。

```python
from napcat.command import PLACEHOLDER_LEGEND   # 需要手写时从这里取
from napcat.command import build_usage
usage = build_usage("ban", BanSchema)   # → "ban <user_id> [duration=600] [reason]"
```

**子命令带参数时也要声明 Schema**（框架的系统命令组就是这么做的）：各子命令单独
`register_command("demo.sub", schema=SubSchema)`，帮助里的用法/参数表由 Schema 生成；
参数缺失或取值非法时框架自动回 `⚠️ 用法不正确 + /help demo.sub`。不要在每个
命令函数里手写「用法：…」兜底 —— 那会和 Schema 漂移。

## 4.3.2 命令函数的签名（无 schema 时只有一个 `packet`）

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

> ⚠️ **签名写错不会静默。** 例如无 schema 的命令写成
> `async def cmd_ping(params, event)`：`params` 拿不到值 → 实参为空 → 调用时
> `TypeError`。框架把它包成 `PluginRuntimeError`：日志里有命令名 + 完整 traceback，
> 用户侧收到一句带错误编号的提示（见 (16-errors.md#十六错误处理与用户反馈)）。排查时按编号 grep 日志即可。

## 4.4 子命令（Subcommands）

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
```text
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
> **需要权限的逻辑一律写进子命令** —— 权限按实际执行的子命令校验。

`/help` 的三级入口、可见性过滤、每个页面各显示什么，见
(../guide/06-commands.md#611-help-三级入口帮助文档的构成) 与
(../guide/06-commands.md#623-help-只列你能用的命令)。插件不需要自己渲染帮助 ——
注册信息（`description` / `scope` / `permission` / Schema）就是帮助的全部来源；
调试视图 `/commands`（superuser）逐条列出子命令自己的范围与说明。

帮助文本的排版约定（框架侧）：首行 `📖 <命令>`；**权限与作用域总是写全**，格式
`权限：xxx · 作用域：仅私聊/仅群聊/所有场景`（由 `describe_requirements()` 生成，
命令组页把同一句话挂在子命令名后的方括号里）；
参数块用 `位置传参：`/`键值传参：`（空默认值不再显示 `[默认: ]`）；`/help` 列表首行
`📖 可用命令（N）`（N = 命令组数 + 无子命令命令数），分段标题固定两个：`[📦 命令组]` 在前、
`[📖 命令]` 在后（**按权限分段已取消**），每行 `  /名字 — 介绍`，
结尾 `💡 /help <命令名> 查看子命令或用法`。

## 4.5 命令处理函数参数绑定

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

## 4.6 命令返回值

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

## 4.7 回复要回到「消息来的地方」

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

## 4.8 给整组命令加一道自己的闸门（服务范围 / 白名单）

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

两个**静默**陷阱：

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

## 4.9 字符串参数的引号与转义

`StrField` 拿到的是**分词并反转义后**的字符串，写命令时直接提示用户加引号即可：
含空格的文本用 `"…"` 或 `'…'` 包起来；文本里有单引号就写成 `'don\'t'`（单引号内也支持 `\'`；此外 `\n` `\t` `\\` `\"` 都会转义）。引号没配对时框架按空白宽松切分，
不会让命令报错。

## 4.10 Schema 字段类型

| Field | Python 类型 | 特性 |
|-------|-------------|------|
| `StrField` | `str` | `min_length`, `max_length`, `regex`, `choices` |
| `IntField` | `int` | `min`, `max`, `choices` |
| `FloatField` | `float` | `min`, `max` |
| `BoolField` | `bool` | 支持 true/false/1/0/yes/no；**单字母简写**：默认用字段名首字母（`-y`），冲突时不猜并提示；`short="f"` 指定、`short=""` 关闭。简写等价 `--字段=true`，帮助里显示 `[-y|--字段=<值>]` 与「简写：-y」 |
| `QQField` | `int` | 自动校验 QQ 号范围，支持 `[@123456]` 格式 |
| `DurationField` | `int`(秒) | 支持 `600` / `10m` / `1h30m` / `2d` |
| `ChoiceField` | `str` | 枚举选项，大小写不敏感 |
| `VariadicField` | `list[str]` | 接收剩余所有位置参数 |

---
