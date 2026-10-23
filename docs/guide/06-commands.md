# 六、命令系统

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[API 调用](05-api.md) · 下一章：[权限管理](07-permissions.md)

命令是用户与机器人交互的主要入口：私聊直接发，群聊 @机器人 后发。默认前缀为
`/` `!` `。` `#`，可在 `config.json` 的 `command_prefixes` 里修改。

> 本文示例中的 QQ、群号、命令名都是虚构的（`100000001`、`demo` 等），换成你自己的即可。

## 6.1 基本用法

```python
from napcat.models import MessageEvent
from napcat.message import MessageChain


@client.register_command("ping", description="测试延迟")
async def cmd_ping(packet):
    return "pong!"


@client.register_command("echo", description="复读你的话")
async def cmd_echo(packet):
    return MessageChain().text(packet.arg(0, "（空）"))
```

命令函数的返回值决定回复方式：

| 返回值 | 行为 |
|--------|------|
| `str` / `MessageChain` | 自动回复到命令来源（群聊回群、私聊回私聊、小窗回小窗） |
| `list[str]` / `list[MessageChain]` | 按顺序分成多条消息发送 |
| `None` | 不自动回复（由插件自己发） |

命令可以声明权限、作用域、参数校验：

```python
from napcat.command import CommandSchema, QQField, StrField


class KickSchema(CommandSchema):
    user = QQField(required=True, description="要踢出的成员")
    reason = StrField(description="原因")


@client.register_command("kick", schema=KickSchema, permission="admin",
                         scope="group", description="踢出群成员")
async def cmd_kick(user, reason, event):
    await client.api.set_group_kick(str(event.group_id), str(user))
    return f"已踢出 {user}" + (f"（{reason}）" if reason else "")
```

有 `schema` 时，命令函数的参数**按字段名注入**；另外可以按名字接收
`packet`（原始命令包）、`event`（消息事件）、`api`（API 客户端）。

## 6.1.1 `/help` 三级入口（帮助文档的构成）

帮助分三级，每一级只比上一级多展开一层信息：

```text
/help                  → 你能用的命令清单
/help <命令组>         → 这个组里有哪些子命令（权限、作用域、一句话介绍）
/help <命令组> <子命令> → 某个子命令的完整用法（参数、默认值）
```

以 `demo` 命令组为例（`/help demo.alpha` 与 `/help demo alpha` 等价）：

```text
/help
  📖 可用命令（4）
  [📦 命令组]
    /demo — 演示命令组
  [📖 命令]
    /ping — 测试延迟
    /kick — 踢出群成员

/help demo
  📖 /demo
    演示命令组
    📦 命令组 · 本体无需权限 · 作用域：所有场景
    子命令
      /demo alpha [权限：公开 · 作用域：所有场景] — 按名字办事
      /demo beta [权限：admin · 作用域：仅私聊] — 管理动作

/help demo alpha
  📖 /demo alpha <name> [times=1]
    按名字办事
    权限：公开 · 作用域：所有场景
    位置传参：/demo alpha <name> [times=1]
    键值传参：/demo alpha --name=<值> [--times=1]
    参数
      <name>  StrField 必填 — 对象名字
      [times] IntField 可选 [默认: 1] — 重复次数
```

规则：

- 只列**调用者当前能用**的命令（见 [6.2.3](#623-help-只列你能用的命令)）；
- 命令组本体不需要权限，所以组本身总会显示；具体子命令按各自的权限过滤；
- 每条命令都写明 `权限：… · 作用域：…`，包括公开命令；
- 子命令不在第一级出现，避免把命令组淹没；
- 参数与默认值只出现在第三级，用法行由参数定义生成，不需要手写。

## 6.1.2 帮助重定向：裸调命令组 / 用法不正确

以下输入不执行动作，直接回对应的帮助页：

| 输入 | 行为 |
|------|------|
| `/demo`（裸调命令组） | 回 `/help demo`，即命令组帮助页 |
| `/demo nosuch`（子命令名写错） | 回 `/help demo`，页里列着可用的子命令名 |
| `/demo alpha`（参数缺失或类型不对） | 回 `⚠️ 用法不正确：<原因>` + `/help demo alpha` |
| `/nosuch`（完全未知的命令） | 回 `未知命令: /nosuch` |

群聊里，命令组没有 @机器人 时不会响应（与普通命令一致）。

## 6.2 命令解析规则

```text
/demo hello                → command=demo, args=["hello"]
/demo alpha 晴天 3         → command=demo, subcommand=alpha, args=["晴天", "3"]
/demo --limit=10 关键词    → command=demo, args=["关键词"], kwargs={"limit": "10"}
/echo "hello world"        → command=echo, args=["hello world"]
/签到                      → command=签到
/点歌 列表 晴天            → command=点歌, subcommand=列表, args=["晴天"]
```

**名字规则**：命令名、子命令名、别名都以**字母开头**（支持中文），后面可含字母、
数字、下划线、连字符。`/签到`、`点歌.列表`、别名 `/签` 都合法；`//`、`/签到！`、
`/2签到` 不是命令，按普通消息静默忽略。

**键值参数**：`--名称=值` 优先于同位置的位置参数，因此可以跳过中间参数只改后面某个：

```text
/group_config greeting 欢迎新同学 true --recall_delay=2m
→ content="欢迎新同学", enabled=true, recall_delay=120
```

**布尔简写**：布尔参数可以用单字母旗标，等价于 `--字段=true`：

```text
/demo -y            等于 --yes=true
/demo -y -f         两个布尔同时为真
/demo --force=false 显式值优先
```

字母默认取布尔字段名首字母；同一字母对应多个布尔字段时不会猜测，会提示改用
`--字段名=值`。插件也可以用 `BoolField(short="v")` 指定、`short=""` 关闭。

**占位符记号**：帮助文本里只有两种记号，末尾会附图例：

| 记号 | 含义 | 例子 |
|------|------|------|
| `<名称>` | 必填 | `/buptnews get <id>` |
| `[名称]` | 可选（带默认值时写 `[名称=默认]`） | `/buptnews sync [pages=1]` |
| `[名称 ...]` | 可变参数，接收剩余全部参数 | `/tags [标签 ...]` |

```text
ℹ️ <名称> 必填，[名称] 可选；尖括号/方括号不用输入
```

## 6.2.1 群聊里必须 @机器人

群聊里只有 **@了机器人** 的消息才会被解析为命令：`@机器人 /help` ✅，
直接发 `/help` ❌（`/help` 自己也一样）。私聊没有这个限制。

`/help` 在群聊里会多打一行提示：

```text
📖 可用命令（4）
⚠️ 群里需先 @机器人再发命令（直接复制命令不触发）
```

## 6.2.2 群聊临时会话（小窗）

群成员在群里点机器人头像开的临时会话，在协议里是「私聊消息 + 来源群号」：

```json
{"message_type": "private", "sub_type": "group", "group_id": 100000002, "user_id": 100000001}
```

对用户和插件来说：

| 场景 | 行为 |
|------|------|
| 触发命令 | 按私聊处理，不需要 @ |
| 回复路由 | 自动回复到小窗（不会错发成好友私聊） |
| 权限/作用域 | 按私聊判定：`scope="private"` 的命令可用，`scope="group"` 的不可用；群角色不参与 |
| 插件自己发消息 | 用 `event.is_temp_session` 判断；转发时把 `event.group_id` 传给 `send_private_msg` |

```python
msg = MessageChain().text("收到")
if event.message_type == "group":
    await self.api.send_group_msg(str(event.group_id), msg)
elif event.is_temp_session:
    await self.api.send_private_msg(str(event.user_id), msg,
                                     group_id=str(event.group_id))
else:
    await self.api.send_private_msg(str(event.user_id), msg)
```

## 6.2.3 `/help` 只列你能用的命令

`/help` 与真正执行命令使用同一套权限判定，所以列表里的命令一定能执行：

| 规则 | 说明 |
|------|------|
| 作用域过滤 | 私聊命令不出现在群聊列表，反之亦然 |
| 权限过滤 | 权限不够的命令不显示；列表不按权限分段 |
| 命令组 | 组本体始终显示（不需要权限），组内子命令按各自权限过滤 |
| 详情 | 无权限的命令按「未知命令」处理；命令组页只列可用的子命令 |
| 计数 | `📖 可用命令（N）` 只统计命令组与叶子命令，不含子命令 |

同一群里的三种身份看到的范围不同（具体取决于部署了哪些命令）：

```text
普通成员  → 公开命令（如 /groupinfo）
群管理员  → 再加上 admin 命令（如 /ban /kick）
超级用户  → 再加上 superuser 专属命令（如 /bind /perm）
```

## 6.2.4 引号与转义（字符串参数）

参数按 shell 习惯分词：带空格的文本用 `"…"` 或 `'…'` 包起来。支持转义
`\n` `\t` `\r` `\\` `\"` `\'`。

| 输入 | 解析结果 | 说明 |
|------|----------|------|
| `/echo "hello world"` | `["hello world"]` | 双引号 |
| `/echo 'hello world'` | `["hello world"]` | 单引号 |
| `/echo 'don\'t'` | `["don't"]` | 单引号内转义单引号 |
| `/echo don\'t` | `["don't"]` | 不加引号也能转义 |
| `/echo "say \"hi\""` | `["say "hi""]` | 双引号内转义双引号 |
| `/echo 'a\nb'` | `["a\nb"]`（含真实换行） | 单引号内也解析转义 |
| `/echo ‘a b’ “c d”` | `["a b", "c d"]` | 中文引号等价于英文引号 |
| `/echo We're BUPT` | `["We're", "BUPT"]` | 引号不配对时按空白切分，不整条报错 |

想把含空格的整段文本作为**一个**参数（例如欢迎语），必须加引号：
`/group_config greeting 'We\'re BUPT!'`。

## 6.3 CommandPacket 字段

没有 Schema 的命令函数会收到 `CommandPacket`，字段如下：

| 字段 | 类型 | 含义 |
|------|------|------|
| `raw` | `str` | 原始文本（不含前缀） |
| `prefix` | `str` | 命中的前缀 |
| `command` | `str` | 命令名（小写） |
| `subcommand` | `str \| None` | 子命令名 |
| `args` | `list[str]` | 位置参数 |
| `kwargs` | `dict[str, str]` | `--键=值` 参数 |
| `event` | `MessageEvent` | 原始消息事件 |
| `illegal` | `bool` | 是否为非法输入 |
| `illegal_reason` | `str` | 非法原因（可展示给用户） |

快捷方法：

```python
packet[0]              # 第一个位置参数
packet.arg(1, "默认")   # 第二个位置参数，不存在时返回默认值
packet.opt("limit")    # --limit=... 的值
```

## 6.4 命令返回值处理

命令函数返回 `str` 或 `MessageChain` 时，框架自动把返回值发回命令来源：

```python
@client.register_command("天气")
async def cmd_weather(packet, api):
    city = packet.arg(0, "北京")
    weather = await fetch_weather(city)
    return MessageChain().text(f"{city}天气：{weather}")


@client.register_command("silent")
async def cmd_silent(packet):
    return None      # 自己决定要不要回复
```

**太长就分段**：返回 `list[MessageChain]`（或 `list[str]`）会按顺序逐条发送。
`MessageChain.split()` 会保持原有顺序与排版：

```python
@client.register_command("长文")
async def cmd_long(packet):
    chain = MessageChain().text(packet.arg(0, ""))
    chunks = chain.split(max_chars=1200, max_images=4)
    return chunks[0] if len(chunks) == 1 else chunks
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `max_chars` | `1200` | 每条消息最多多少字符；优先在换行处断开 |
| `max_images` | `4` | 每条消息最多多少媒体段；单个媒体段不会被拆开 |

分段只改变「一条消息变多条」，内容与顺序不变；发送途中断线则停止后续分段。

## 6.5 子命令

用点号命名即可注册命令组：

```python
@client.register_command("music.play", description="播放音乐")
async def music_play(packet):
    return f"正在播放：{packet.arg(0)}"


@client.register_command("music.stop", description="停止播放")
async def music_stop(packet):
    return "已停止播放"
```

```text
/music play 晴天
/music stop
```

规则：

- 命令组（有子命令的命令）本体不需要权限，父命令上写的 `permission`/`scope`
  只是子命令的默认值；
- 子命令不写 `permission` 时继承父命令，写 `permission=""` 表示公开；
- 权限按**实际执行的那条子命令**校验，所以需要权限的逻辑必须放在子命令里。

## 6.6 非法输入与权限拒绝回调

默认行为：前缀开头但不是命令的输入（如 `//`）静默忽略；权限不足按「未知命令」
处理。需要自定义时注册回调：

```python
@client.cmds.on_illegal
async def on_illegal(packet, event):
    await client.api.send_group_msg(
        group_id=str(event.group_id),
        message=f"命令格式错误：{packet.illegal_reason}")


@client.cmds.on_denied
async def on_denied(packet, event):
    await client.api.send_group_msg(
        group_id=str(event.group_id),
        message="你没有权限执行这个命令")
```

## 6.7 手动解析命令

需要在自己的逻辑里复用命令解析时：

```python
from napcat.command import CommandParser

parser = CommandParser(prefixes=["/", "!"])
packet = parser.parse("/ban 100000001 600")

if packet and not packet.illegal:
    print(packet.command, packet.args)   # ban ['100000001', '600']
```

## 6.8 命令别名（`/alias`）

超级用户可以在私聊里给命令起别名，让用户用更短的写法。别名与目标命令共享同一套
权限、参数与作用域，仅私聊 + 仅超级用户可管理。

```text
/alias list                     列出全部别名
/alias list demo                看某条命令的别名
/alias add d demo               给 /demo 加别名 /d（之后 /d 与 /demo 等价）
/alias del d                    删除别名
/alias set demo d1 d2           整体替换（也可写成 d1,d2）
/alias set demo                 清空 /demo 的全部别名
/alias move d other             把 /d 改指向 /other
```

规则：

- 别名以字母开头（支持中文，如 `/签`、`/歌-单`），只能含字母、数字、下划线、连字符；
- 不能与现有命令名或其他别名冲突，也不能等于目标命令自己的名字；
- 目标可以是顶层命令、命令组或子命令（`父.子`）：
  - 给命令组起别名后，`/别名 子命令` 照常路由，`/别名` 显示该组帮助页；
  - 给子命令起别名是**组内作用域**：`/alias add 列 点歌.列表` 之后用 `/点歌 列`，
    不会多出顶层 `/列`；不同组可以各有一个 `列`。跨组同名时用 `/alias del 点歌.列`
    这种点号写法消歧；
- 别名保存在 `config.json` 的 `command_aliases`：

  ```json
  "command_aliases": {"demo": ["d1", "d2"], "点歌.列表": ["列"]}
  ```

  某条命令出现在这里后，配置里的列表就是它的全部别名（空列表表示清空，包括插件
  声明的别名）；删掉这个键则恢复插件声明值；
- 插件重载或重新注册后别名保留；命令被卸载时别名条目一起清理，命令重新出现后恢复。
