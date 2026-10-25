# 十七、技能（把能力交给 AI 用）

> 上级：[插件开发](README.md) · 相关：[命令](04-commands.md) · [ai_chat 的工具](../plugins/ai-chat/08-tools.md)

**命令给「人」用，技能给「模型」用。** 注册一个技能，装了 `ai_chat` 之后，
模型在对话里就能**主动调用**你插件的功能 —— 不用人先敲命令、再把结果告诉它。

典型场景：群里有人问「有没有线代的真题」，`byrdocs` 注册了搜索技能，模型直接查。

## 17.1 注册

```python
async def on_load(self):
    @self.register_skill(
        "search",
        description=("在 BYR Docs 资料库里搜索。用户问「有没有某某课的资料/真题」时用它。"
                     "返回编号、标题、类型、大小。"),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词"},
                "limit": {"type": "integer", "description": "最多几条，默认 8"},
            },
            "required": ["query"],
        },
    )
    async def _skill_search(args: dict, ctx):
        hits = await self._search(args["query"], limit=int(args.get("limit") or 8))
        return "\n".join(f"{i}. {h.title}" for i, h in enumerate(hits, 1))
```

`description` 与 `parameters` 是**给模型看的**：写得越像「什么时候该用我」，
模型越会用对。`parameters` 就是 OpenAI 的 JSON Schema。

## 17.2 三条硬约束

| 约束 | 为什么 |
|---|---|
| 名字全局唯一（`插件名.技能名`），暴露给模型时点号换成 `_` | OpenAI 函数名只允许 `[a-zA-Z0-9_-]`；点号不合法 |
| 返回**字符串**（dict/list 会被 `json.dumps`） | 工具结果是文本，模型读不了对象 |
| **异常抛出去**，不要自己吞 | 调用方会把它变成「技能执行失败：…」交给模型，模型能换法子重试；吞掉只会让它以为成功 |

结果超过 3000 字会被截断（模型读不动更长的东西），所以**返回前先挑重点** ——
跟命令行里回一大段消息是两回事。

## 17.3 调用上下文

第二个参数是 `SkillContext`：

```python
@dataclass
class SkillContext:
    caller: str          # 谁在调（一般是 "ai_chat"）
    user_id: int         # 真实用户
    group_id: int
    is_group: bool
    session_id: str      # 调用方的会话 id
    is_superuser: bool   # 调用方已经判过的结论，可以直接用
    is_admin: bool
    extra: dict
```

**框架不替你判权限**：只有你知道「谁能查内部资料」。按 `ctx` 自己判，
并用 `return "没有权限"` 或抛异常告诉模型。

> 别以为「技能只会在群里被调」：同一个模型在私聊里也能调。服务范围白名单要
> 自己在技能里再判一次 —— `byrdocs` 就是这么做的（`_skill_allowed`）。

## 17.4 生命周期

* 热重载、禁用、卸载时，框架会把**该插件注册的所有技能**撤掉（与命令同一套
  回收路径）—— 不会留下指向死 handler 的工具；
* `ai_chat` 每轮**重新读**技能表，所以新装的插件注册的技能下一轮就能用；
* ⚠️ **技能通道本身是框架代码**（`napcat/skills.py` + `PluginBase`）：装了新版框架后
  要 **重启一次进程**（`systemctl restart ncqqpy`）才生效 —— 热重载只换插件代码。
  没重启时插件会打一行警告并跳过技能注册，其他功能照常（不会因此加载失败）；
* 同名技能以**后注册**的为准（`register` 覆盖），插件名相同会覆盖自己。

## 17.5 消费方：ai_chat 侧怎么接

`ai_chat` 通过 `register_skill_tools()` 把技能包成工具，过滤口径在
`skills` 配置段：

```json
"skills": {"enabled": true, "allow": [], "deny": []}
```

| 键 | 含义 |
|---|---|
| `enabled` | 全关（一个技能都不接） |
| `allow` | 非空时只接名单里的（全名，如 `byrdocs.search`） |
| `deny` | 排除名单里的（`allow` 为空时生效） |

`ai_chat` **不会**接自己注册的技能（避免自我递归），同名以内置工具为准。

## 17.6 调试

```python
self.skills.list()          # 现在有哪些技能
self.skills.get("byrdocs.search")
self.skills.unregister_plugin("byrdocs")   # 手工撤掉（一般不用，框架会做）
```

在对话里问一句「你现在有哪些工具」，模型会从工具表里报出来 —— 这是最直接的
端到端验证（比看日志快）。
