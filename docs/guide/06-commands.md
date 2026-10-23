# 六、命令系统

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[API 调用](05-api.md) · 下一章：[权限管理](07-permissions.md)


## 6.1 基本用法

消息以 `/` `!` `。` `#` 开头时自动解析为命令：

```python
# 注册命令（装饰器方式，推荐）
@client.register_command("ping", description="Ping 测试")
async def cmd_ping(packet):
    return "pong!"

# 注册需要权限的命令
@client.register_command("kick", permission="admin", description="踢出成员")
async def cmd_kick(packet, event):
    target = packet[0]
    await client.api.set_group_kick(str(event.group_id), target)
    return f"已踢出 {target}"
```

## 6.1.1 `/help` 三级入口（帮助文档的构成）

命令的帮助文档由**三层**拼成，每层只多展开一档信息。**整体描述写在主命令里**
（`register_command(..., description=...)`），子命令清单由**各个子命令的描述**拼出来 ——
框架渲染，插件不手写（手写会与注册信息漂移）：

| 输入 | 展示什么 | 数据来源 |
|------|----------|----------|
| `/help` | 命令清单：`[📦 命令组]` + `[📖 命令]` 两段，每行 `/{名字} — {介绍}` | 各命令的 `description` |
| `/help <主命令>` | 是命令组 → 本体**无需权限 + 作用域**，子命令清单逐条：名称 + **权限/作用域** + 描述（不带参数/用法）；<br>没有子命令 → 该命令的介绍 + **权限/作用域** + 用法/参数表 | 命令组：本体 `description`/`scope` + 各子命令生效的 `permission`/`scope`/`description`；无子命令：自身注册信息 |
| `/help <主命令> <子命令>` | 该子命令的**介绍 + 权限/作用域 + 用法/参数表** | 子命令自身生效的声明；与 `/help <主命令>.<子命令>` **等价** |

> 后两（三）种入口**必须写明权限与作用域**（`权限：superuser · 作用域：仅私聊`；公开且
> 不限场景也要写成 `权限：公开 · 作用域：所有场景`）—— 只写权限不写作用域，「这条命令在
> 群里能不能用」在帮助里就没有答案了。两处用同一句话（`describe_requirements()`），
> 详情行与子命令清单的方括号标注不会各说各话。

> 分层原则：**每级只多展开一档信息**，且**同一层里的命令排成同一个样子**
> （`/{名字} — {介绍}`）。第一级只给介绍 → **子命令不进第一级**、**叶子命令的用法也不进
> 第一级**、**也不按权限分成 `[👑 superuser]` 之类的段**（用不了的命令根本不会列出来，
> 再标权限档只会把同一类命令拆成几段）；第二级的子命令清单只给「名称 + 权限/作用域 + 描述」，
> **参数与用法不进第二级**（那是第三级）。

以 `demo` 命令组（`demo` 本体 + `demo.alpha` / `demo.beta`）和两个叶子命令为例：

```
/help
  [📦 命令组]
    /demo — 演示命令组：整体描述写在主命令里          ← 第一级：两段同版式，
  [📖 命令]                                            每行只有名称 + 描述
    /leafy — 叶子命令：带参数                          （没有子命令行、没有用法，
    /plain — 叶子命令：无子命令                          也不按权限分段）

/help demo
  📖 /demo
    演示命令组：整体描述写在主命令里
    📦 命令组 · 本体无需权限 · 作用域：所有场景 · 各子命令的权限/作用域见下行标注
    子命令
      /demo alpha [权限：公开 · 作用域：所有场景] — 子命令甲：按名字办事  ← 第二级（命令组）：
      /demo beta [权限：公开 · 作用域：所有场景] — 子命令乙：无参数        名称 + 权限/作用域 + 介绍
                                                                        （参数/用法不在这里）

/help demo alpha          （= /help demo.alpha）
  📖 /demo alpha <name> [times=1]
    子命令甲：按名字办事                              ← 第三级：介绍
    权限：公开 · 作用域：所有场景                      ← 权限 + 作用域（公开也写全）
    位置传参：/demo alpha <name> [times=1]            ← 用法与参数表
    键值传参：/demo alpha --name=<值> [--times=1]
    参数
      <name> StrField 必填 — 对象名字
      [times] StrField 可选 [默认: 1] — 重复次数

/help leafy
  📖 /leafy <name> [times=1]
    叶子命令：带参数                                  ← 第二级（无子命令）：介绍 + 权限/作用域 + 用法
    权限：admin · 作用域：所有场景
    位置传参：/leafy <name> [times=1]
    键值传参：/leafy --name=<值> [--times=1]
```

（三级输出结尾都按需带占位符图例，原文见 (#625-占位符记号-必填-可选)；示例里的整体缩进仅为排版，
框架输出的缩进是每层两空格。）

> 第二段不是已注册的子命令时**回落到主命令**（`/help perm set` 这类「用法里带取值」
> 的写法照旧显示 `/perm` 详情），不会判成未知命令。每一级都按权限/范围过滤，口径与
> 执行完全一致 —— 见 (#623-help-只列你能用的命令)。裸调命令组与用法错误也会落到这些帮助页，见 (#612-帮助重定向裸调命令组--用法不正确)。

## 6.1.2 帮助重定向：裸调命令组 / 用法不正确

有两类输入**不执行任何动作**，而是回**对应的帮助页**（框架在 `CommandManager.handle()`
里统一处理，插件不用自己写）：

| 输入 | 行为 |
|------|------|
| `/命令组`（裸调，如 `/aichat`）| 重定向到 `/help 命令组`：本体描述 + 你有权使用的子命令。**注册在父命令上的函数不会运行**（想自定义这一页就写父命令的 `description`）；范围不符或插件过滤器否决时静默不回复 |
| `/命令组 不存在的子命令` | 同上一行 —— 子命令名写错时，这一页正好列着可用的名字（组本身用不了时仍按「未知命令」处理，不泄露命名空间）|
| 参数写错（Schema 校验失败）| `⚠️ 用法不正确：<原因>` + **该（子）命令**的帮助页：用法行、参数表、默认值、`权限：…` |

> 口径是「说不清就指路」：只回一句「参数不对」用户仍然不知道该怎么写，而帮助页与真实校验
> **同源**（用法行、参数表都由 Schema 生成），改了参数帮助自动跟着变。
> 完全未知的顶层命令（`/nosuch`）仍是「未知命令: /nosuch」。

## 6.2 命令解析规则

```
输入: /ban 987654 600
解析: prefix="/" command="ban" args=["987654", "600"]

输入: /music play qq 12345
解析: prefix="/" command="music" subcommand="play" args=["qq", "12345"]

输入: /search --limit=10 关键词
解析: prefix="/" command="search" args=["关键词"] kwargs={"limit": "10"}

输入: /echo "hello world"
解析: prefix="/" command="echo" args=["hello world"]

输入: /签到
解析: prefix="/" command="签到"           ← 命令名支持中文等非 ASCII 字母

输入: /点歌 列表 晴天
解析: prefix="/" command="点歌" subcommand="列表" args=["晴天"]
```

**名字规则**（命令名、子命令名、别名共用一条）：**Unicode 字母开头**，后续可含
字母、数字、下划线、连字符。所以 `/签到`、`点歌.列表`、别名 `/签` 都合法；
`//`、`/签到！`、`/2签到`、`/🎵` 不是命令名（前缀后不是名字的输入按普通消息静默丢弃，
不会回「未知命令」）。注册期就校验：非法名字会直接 `ValueError`，不会出现
「注册成功但永远收不到消息」。

> 解析器把「第二个 token 如果是合法名字」先当作**子命令候选**（中文也算名字）；
> 命令本身没有子命令时，执行阶段会把它**回落成第一个参数**（`/回声 你好` →
> `args=["你好"]`）。所以命令组用中文子命令（`点歌.列表`）也能路由，
> 而普通命令的中文参数行为不变。

> **`--键=值` 优先于位置位**：Schema 校验时，显式写了 `--名称=值` 的字段就用键值（照样过
> 字段校验、非法值回「⚠️ 用法不正确」+ 该命令的帮助页，见 (#612-帮助重定向裸调命令组--用法不正确)），其余字段才按位置逐个对应。这样调用者能**只改后面
> 某个可选参数**而跳过中间的：
>
> ```
> 输入: /group_config greeting 欢迎新同学 true --recall_delay=2m
> 解析: content="欢迎新同学" enabled=true recall_delay=120
> ```
>
> 反之，位置参数里放了非该字段类型的值（例如把 `2m` 塞进 `enabled` 这个布尔位）会被拒绝，
> 不会静默错位。
> **布尔字段的简写**：布尔参数可以用 `-y` 这种单字母旗标，等价于 `--字段=true`：
>
> ```
> 输入: /demo -y            跳过确认（--yes=true）
> 输入: /demo -y -f         两个布尔同时置真
> 输入: /demo --force=false 显式值优先于简写
> ```
>
> 字母默认取布尔字段名的首字母；同一字母被多个布尔字段使用时**不猜**，会回
> 「短选项 -y 有歧义：yes / yolo（请用 --字段名=值）」。插件可用
> `BoolField(short="v")` 显式指定，`short=""` 关闭。帮助的「键值传参」会同时显示
> `[-y|--yes=<值>]`，参数表标注「简写：-y」。

## 6.2.1 群聊里必须 @机器人

群聊消息只有 **@了机器人** 才会进入命令分发（框架入口 `command_dispatcher` 的规则，
见 (../../napcat/client.py)）：`@机器人 /help` ✅、直接发 `/help` ❌（连 `/help` 自己也不例外）。
私聊没有这个限制。

因此 **`/help` 在群聊里会额外打一行提示**：

```
📖 可用命令（9）
⚠️ 群里需先 @机器人再发命令（直接复制命令不触发）
```

`/help <命令名>` 详情同样会带上这句（仅当该命令在群里本来就能用时；`scope="private"`
的命令只显示「仅私聊」，不会误导）。

## 6.2.2 群聊临时会话（小窗）

群成员在群里点你头像开的**临时会话（小窗）**，在 OneBot 里长这样：

```json
{"message_type": "private", "sub_type": "group", "group_id": 123456, "user_id": 10001}
```

框架的处理：

| 环节 | 行为 |
|------|------|
| 触发条件 | 按**私聊**处理：不需要 @，消息直接进命令分发 |
| 回复路由 | 自动带 `group_id` 调 `send_private_msg`（`MessageEvent.is_temp_session`）——**不带的话 NapCat 会当好友私聊发送，非好友直接失败**（小窗收不到回复） |
| 权限/作用域 | 仍按**私聊**判定（`is_private` 语义）：`scope="private"` 的命令可用，`scope="group"` 的被拒（返回「未知命令」），群角色不参与判定 |
| 插件自己发消息 | 用 `event.is_temp_session` 判断，转发时把 `event.group_id` 传给 `send_private_msg(group_id=…)`；参数校验失败的回执（见 (#612-帮助重定向裸调命令组--用法不正确)）同样按来源回：群里回群、小窗回小窗，不会把错误泄露到群里 |

```python
# 插件里想把消息发回「消息来的地方」（群 / 私聊 / 小窗）
if event.message_type == "group":
    await self.api.send_group_msg(str(event.group_id), msg)
else:
    await self.api.send_private_msg(
        str(event.user_id), msg,
        group_id=str(event.group_id) if event.is_temp_session else None)
```

## 6.2.3 `/help` 只列「你能用的」命令

`/help` 按**调用者当下能实际执行**的命令来列，口径与 `CommandManager.handle()` 完全一致。
列表的构成见 (#611-help-三级入口帮助文档的构成)：只列**命令组**与**没有子命令的命令**（子命令在 `/help <命令组>` 里）：

| 规则 | 说明 |
|------|------|
| 范围过滤 | `scope` 与当前上下文不符的命令不列（私聊命令不出现在群里，反之亦然）|
| 权限过滤 | `check_permission` 不通过的命令不列；**第一级列表不按权限分段**（没有 `[👑 superuser]` 之类的标题）—— 列出来的就是你能用的 |
| 权限/作用域 | 单命令详情、命令组页、子命令详情**都写明** `权限：xxx · 作用域：…`：命令组页逐条挂在子命令名后（`/buptnews add [权限：superuser · 作用域：仅私聊] — 添加推送目标`），详情页单独一行；公开且不限场景写成 `权限：公开 · 作用域：所有场景` |
| 命令组 | 本体不设权限（父命令上的 `permission` 只是子命令默认值），所以命令组本身始终可见；子命令**不在第一级列表里**，由 `/help <命令组>` 按各自 scope/permission 过滤后逐条列出（**只给名称 + 描述**；权限/范围看各子命令的详情）|
| 详情 | `/help <命令名>` 对无权限的命令按「未知命令」处理（与执行时的隐藏口径一致）；`/help <命令组>` 只列你可用子命令 |
| 计数 | 首行 `📖 可用命令（N）` 的 N = 可见的命令组数 + 无子命令命令数（不含子命令）|

效果（同一个群里的三种身份；条数与内容随该部署的命令数变化）：

```
普通成员  → [📦 命令组] + [📖 命令]（公开命令，如 /groupinfo）
群管理员  → 上面 + [📖 命令] 里的 admin 命令（如 /ban /kick /unban）
超级用户  → 上面 + [📖 命令] 里的 superuser 专属命令（/bind /perm …）
            （私聊里还能看到仅私聊的命令；分段标题始终只有「命令组」与「命令」两个）
```

## 6.2.4 引号与转义（字符串参数）

参数按 shlex 规则分词，`"…"` 与 `'…'` 都能把空格包进同一个参数；**框架在分词后统一处理
转义序列**：`\n` `\t` `\r` `\\` `\"` `\'`。

| 输入 | 解析结果 | 说明 |
|------|----------|------|
| `/echo "hello world"` | `["hello world"]` | 双引号 |
| `/echo 'hello world'` | `["hello world"]` | 单引号 |
| `/echo 'don\'t'` | `["don't"]` | **单引号内转义单引号**（历史上会判成引号没配对）|
| `/echo don\'t` | `["don't"]` | 不加引号也能转义 |
| `/echo "say \"hi\""` | `["say "hi""]` | 双引号内转义双引号 |
| `/echo 'a\'b\\c'` | `["a'b\\c"]` | 转义单引号与反斜杠混用 |
| `/echo 'a\nb'` | `["a\nb"]`（含真实换行）| 单引号内 `\n` 仍按框架约定转义 |
| `/echo ‘a b’ “c d”` | `["a b", "c d"]` | 中文引号自动等价于 ASCII 引号 |
| `/echo We're BUPT` | `["We're", "BUPT"]` | 引号没配对时**宽松回退**：按空白切，不会整条报错 |

> 想把含空格的整段文本作为**一个**参数（例如欢迎语），请加引号：
> `/group_config greeting 'We\'re BUPT!'`。不加引号时多词会按空白拆成多个参数，
> 只有第一个会进入单值字段。

## 6.2.5 占位符记号：`<>` 必填、`[]` 可选

所有帮助文本（`/help` 首屏、`/help <命令名>` 详情、`/help <命令组>` 的子命令清单、参数表）**只使用两种记号**，
并且都会在末尾附一行图例，明确告诉用户这是占位符、括号本身不用输入：

| 记号 | 含义 | 例子 |
|------|------|------|
| `<名称>` | **必填** | `/buptnews get <id>` |
| `[名称]` | **可选**（带默认值时写成 `[名称=默认]`） | `/buptnews sync [pages=1] [preview=False]` |
| `[名称 ...]` | 可变参数，吃剩余所有位置参数 | `/xxx tags [标签 ...]` |

图例原文：

```
ℹ️ <名称> 必填，[名称] 可选；尖括号/方括号不用输入
```

实现口径（`napcat/command.py`）：

- 用法行由 Schema 生成（`build_usage()` / `format_field_usage()`），子命令同样生效 ——
  显式传 `usage=` 也可以，两者都以同一套记号输出；
- 参数表（`describe_fields()`）里的字段名同样标注 `<>` / `[]`，与用法行口径一致；
- 报错文案也用同一记号（如 `请指定群号: /bind <群号>`）；
- 图例只在**这段帮助真的渲染了占位符**时才追加（`/help ping` 这类无参命令不会多这一行），
  所以插件不要再自己追加一次，否则会连出两行（命令组页的渲染完全由框架负责，见 (#612-帮助重定向裸调命令组--用法不正确)）。

## 6.3 CommandPacket 字段

```python
packet.raw           # str     原始文本（不含前缀）
packet.prefix        # str     命中的前缀（如 "/"）
packet.command       # str     命令名（已转小写）
packet.subcommand    # str?    子命令
packet.args          # [str]   位置参数列表
packet.kwargs        # {str:str} 关键字参数（--key=value）
packet.event         # MessageEvent  原始消息事件

# 快捷访问
packet[0]            # 等同于 packet.arg(0)
packet.arg(1, "默认") # 获取位置参数，不存在时返回默认
packet.opt("key")    # 获取 --key=value

# 非法输入标记
packet.illegal       # bool    是否非法
packet.illegal_reason # str    非法原因
```

## 6.4 命令返回值处理

命令函数可以返回 `str` / `MessageChain`，客户端会自动将返回值作为回复发送：

```python
@client.register_command("天气")
async def cmd_weather(packet, api):
    city = packet.arg(0, "北京")
    weather = await fetch_weather(city)
    return MessageChain().text(f"{city}天气: {weather}")

@client.register_command("status")
async def cmd_status(packet, event):
    # 返回 None 表示不自动回复
    return None
```

> **注意**：命令返回值会通过 `send_group_msg` 自动发送到命令来源群。私聊命令会通过 `send_private_msg` 发送。

**太长就分段**：返回 `list[MessageChain]`（或 `list[str]`）时按顺序逐条发送。
直接用 `MessageChain.split()` 切，它会保持排版与顺序：

```python
@client.register_command("长文")
async def cmd_long(packet):
    chain = MessageChain().text(packet.arg(0, ""))    # 很长的内容
    chunks = chain.split(max_chars=1200, max_images=4)  # 0 = 该维度不限
    return chunks[0] if len(chunks) == 1 else chunks
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `max_chars` | `1200` | 每条消息最多多少文字字符；文字优先在 `\n` 处断开，找不到换行才硬切 |
| `max_images` | `4` | 每条消息最多多少媒体段（图片/语音/视频/文件）；单个媒体段不会被拆开 |

分段只影响「一条消息变多条」，**顺序与内容与原来完全一致**；发送过程中断线则停止后续分段。

## 6.5 子命令

```python
# 点号分隔自动注册为父子命令
@client.register_command("music.play", description="播放音乐")
async def music_play(packet):
    song = packet.arg(0)
    return f"正在播放: {song}"

@client.register_command("music.stop", description="停止播放")
async def music_stop(packet):
    return "已停止播放"

# 用法:
# /music play 晴天
# /music stop
```

> **命令组（有子命令的命令）本体不设权限**：父命令上写的 `permission`/`scope` 只作为
> 子命令的默认值（子命令可单独覆盖），不会用来拦截 `/music` 本身 —— 父命令函数只负责
> 分发/打印用法，任何人都能触发（`scope` 仍然生效）。需要权限的逻辑必须放进子命令，
> 框架会在路由到子命令**之前**按该子命令自己的声明校验。
> 子命令不传 `permission` 表示继承父命令；传 `permission=""` 表示**显式无权限限制**。

## 6.6 非法输入与权限拒绝回调

```python
@client.cmds.on_illegal
async def on_illegal(packet, event):
    await client.api.send_group_msg(
        group_id=str(event.group_id),
        message=f"命令格式错误: {packet.illegal_reason}",
    )

@client.cmds.on_denied
async def on_denied(packet, event):
    await client.api.send_group_msg(
        group_id=str(event.group_id),
        message="你没有权限执行此命令",
    )
```

## 6.7 手动解析命令

```python
from napcat.command import CommandParser
parser = CommandParser(prefixes=["/", "!"])
packet = parser.parse("/ban 987654 600")

if packet and not packet.illegal:
    print(packet.command, packet.args)  # ban ['987654', '600']
```

---

## 6.8 命令别名（`/alias`）

别名不是「另一条命令」：它在命令表里指向**同一个 handler**，所以权限、schema、
作用域、归属标签与目标命令完全一致，插件卸载时也一起回收。仅私聊 + 仅超级用户
可用（与 `/perm`、`/userperm` 同档）。

> 从 1.x 起这些系统命令都是**真正的命令组**：裸调（如 `/alias`）显示的是命令组
> 帮助页（列出可用子命令与各自权限），列表动作请显式写 `list`。

```
/alias list                     列出全部别名
/alias list demo                看某条命令的别名
/alias add d demo               给 /demo 加别名 /d（之后 /d 与 /demo 等价）
/alias del d                    删除别名
/alias set demo d1 d2           整体替换 /demo 的别名（也可以写成 d1,d2）
/alias set demo                 清空 /demo 的全部别名
/alias move d other             把 /d 改指向 /other
```

规则：

- 别名要**字母开头**（支持中文，如 `/签`、`/歌-单`），只能含字母/数字/下划线/连字符，
  长度不超过命令名上限；
- 不能与现有**命令名**或**别的别名**冲突；不能等于目标命令自己的名字；
- 目标可以是**顶层命令、命令组或子命令**（`父.子`）：
  - 顶层命令/命令组：别名与目标共用同一个 handler，`/别名 子命令` 照常路由，
    `/别名` 显示命令组帮助页；权限/作用域与目标一致（超管专属组的别名对普通用户
    同样不可见）；
  - 子命令：别名是**组内作用域** —— `/alias add 列 点歌.列表` 之后是 `/点歌 列`，
    不会凭空多出顶层 `/列`（不同组可以各有一个 `列`）。组内别名不能和该组的
    子命令名、其他子命令的别名撞；跨组同名时用 `/alias del 点歌.列` 这种点号写法
    消歧；`/alias move` 支持顶层别名与组内别名互转；
- 改动落盘到 (../../config.json)：

  ```json
  "command_aliases": {
    "demo": ["d1", "d2"],
    "点歌.列表": ["列"]
  }
  ```

  配置里一旦出现某个命令，它就是该命令别名的**全集**：空列表表示清空，连插件
  `register_command(..., aliases=[...])` 里声明的别名也会被压掉；删掉这个键则恢复
  代码里的声明值；
- 插件热重载 / 重新注册后别名自动恢复（重放点在命令登记处）；命令被
  `/stop command` 或插件卸载移除时，它的别名条目一起清掉；命令暂时不在（插件没装）
  时配置会保留并在 `/alias` 列表里标「未注册」，重装后自动生效；
- 帮助里会标注别名的归属：命令组页的子命令行追加 `（别名：列）`，
  `/help 点歌 列` 也能解析到子命令帮助页。
