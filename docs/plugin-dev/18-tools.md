# 十八、工具（给 AI 用，**结果回给模型**）

> 上级：[插件开发](README.md) · 相关：[技能](17-skills.md) · [ai_chat 的工具](../plugins/ai-chat/08-tools.md)

插件有两条路把自己的能力交给 AI。**先选对通道**，两者的语义完全不同：

| | 技能 `register_skill` | 工具 `register_tool` |
|---|---|---|
| 本质 | **命令映射**（`byrdocs.search`） | 一段**自己写的 handler** |
| 参数 | 从 `CommandSchema` 取 | 自己给 JSON Schema |
| 执行 | 走 `CommandManager.handle()` | 直接调 handler |
| 结果 | **发给你**（命令自己回消息） | **返回给模型**，由 AI 组织答复 |
| AI | 不接话、拿不到输出 | 拿到输出，据此继续推理 |
| 适合 | 动作类：发通知、置顶、写操作 | 查询类：搜索结果、日程、状态 |

## 18.1 注册

```python
from napcat import PluginBase

class DemoPlugin(PluginBase):
    name = "demo"
    version = "1.0.0"

    async def on_load(self):
        @self.register_tool(
            "lookup",
            description="按关键词查资料；返回条数与标题，**由你决定怎么转述**",
            parameters={
                "type": "object",
                "properties": {
                    "kw": {"type": "string", "description": "关键词"},
                },
                "required": ["kw"],
            },
            permission="",          # "" / admin / superuser
            scope="all",            # all / private / group
            # 写进系统提示的一句话：什么时候该调它（见 18.6）
            prompt_hint="用户要「存题库」时先调它，再回答",
        )
        async def lookup(args: dict) -> str:
            kw = str(args.get("kw") or "")
            ...
            return f"命中 3 条：…"    # ← 这句话进模型上下文
```

- handler 签名固定 `args: dict -> str`，**同步/异步都行**；
- 返回值会被截断到 `max_chars`（默认 4000）；
- 名字全局唯一：`demo.lookup`，暴露给模型是 `demo_lookup`；
- `prompt_hint`（可选）：一句话说明「什么时候该用我」。它会被消费方写进**系统提示**，
  比只放在工具 schema 的 `description` 更容易被模型想起来（见 18.6）。

## 18.2 权限与作用域

在**消费侧**判（`ai_chat` 每轮调用前检查）：不通过的工具**不会出现在模型工具表里**，
模型根本看不到，也就无从调用。

| `permission` | 谁能用 |
|---|---|
| `""` | 所有人 |
| `"admin"` | 群管理员 / 超管 |
| `"superuser"` | 仅超管 |

`scope`：`all` / `private`（仅私聊）/ `group`（仅群聊）。

## 18.3 生命周期

卸载、禁用、热重载时框架按插件名**整批撤销**（与技能同一套位置）。所以：

- 不要自己保存 handler 指针给别的插件用 —— 拿 `tool_name` 让消费侧去查；
- 重载后模型工具表**自动**更新（消费侧每轮重读，不是快照）。

## 18.4 调试

```python
self.plugin_tools.list()                     # 我注册了哪些
self.plugin_tools.get("demo.lookup")         # 单个
self.unregister_tool("lookup")               # 手工撤一个
```

在 ai_chat 里验证：让模型「查一下 XX」，看回复是不是**模型转述**的（工具结果走
`role=tool`，不会作为一条单独消息发给用户）。

> ⚠️ 工具是**框架代码**（`napcat/tools.py`）：换版本要**重启进程**；
> 插件侧只改自己的 handler 则热重载即可。

## 18.5 拿到调用者（可选）

工具如果需要知道「谁在调、在哪个群调」——比如插件自己的服务范围白名单，
`permission` / `scope` 只能表达「超管 / 群聊」这种粗粒度 —— 给 handler 多写一个
参数就行：

```python
@self.register_tool("lookup", description="查资料")
async def lookup(args: dict, ctx=None) -> str:
    if ctx is not None and getattr(ctx, "in_group", False):
        if ctx.group_id not in my_allowlist():
            return "这个群没有开通本插件"
    ...
```

约定：

- `ctx` 由**消费方**注入（`ai_chat` 传它的 `ToolContext`），框架只按鸭子类型使用
  （`user_id` / `group_id` / `in_group` / `is_superuser`），**不 import 任何插件**；
- 声明了第二个参数的工具**总是**收到它（没有调用者时是 `None`）——「有时传、有时
  不传」会让 handler 多出不可预期的分支；
- 没声明第二个参数的工具行为不变，老插件不用改；
- 单参数 handler 上写 `ctx` 是拿不到的：框架按**参数个数**探测，`*args` / `**kwargs`
  不算（拿到的位置不确定，与其猜错不如不给）。

## 18.6 让模型记得用（`prompt_hint`）

工具的 `description` 只在模型**主动读工具清单**时才看到；而「用户说搜题就该存题库」
这类**时机判断**，写在系统提示里才容易被想起来。所以工具可以带一句话：

```python
@self.register_tool(
    "save_problem",
    description="把题目保存进题库",
    parameters={...},
    prompt_hint="用户让你搜题/找题，或说「存进题库」时：先调用它存题，再回答。",
)
```

消费方（`ai_chat`）会把这些提示拼进系统提示的「可用能力」一节：

```text
## 可用能力
你可以调用以下工具获取实时信息：`web_search`、`oj_bank_save_problem`、…
插件工具的使用时机：
- 用户让你搜题、找题，或说「存进题库」时：先调 oj_bank_save_problem 存题，再回答。
```

写法建议：

- 一条工具一句话，说**触发条件 + 动作**（「用户说 X 时调 Y」），不要重复 schema 里的参数说明；
- 只写消费方**看不到 schema 就不知道**的信息（时机、优先级、失败怎么办）；
- 别写「总是调用」——会和别的工具的时机提示打架，模型会乱调。
