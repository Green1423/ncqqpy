"""工具注册表 —— 插件把能力**作为工具**交给 AI，结果**返回给模型**。

与「技能」的区别（两条通道，别混）：

| | 技能（`register_skill`） | 工具（`register_tool`） |
|---|---|---|
| 本质 | **命令映射**（`byrdocs.search`） | 一段**自己写的 handler** |
| 参数 | 从 `CommandSchema` 取 | 自己给 JSON Schema |
| 执行 | 走 `CommandManager.handle()` | 直接调 handler |
| 结果 | **发给你**（插件自己回消息） | **返回给模型**，由 AI 组织答复 |
| AI | 不接话、拿不到输出 | 拿到输出，据此继续推理 |

适合做成工具的：查询类（搜索结果、日程、状态），模型应该看到数据再解释；
适合做成技能的：动作类（发通知、置顶、写操作），执行完直接告诉用户就行。

```python
class DemoPlugin(PluginBase):
    async def on_load(self):
        @self.register_tool(
            "ping", description="测一下服务是否在线",
            parameters={"type": "object", "properties": {}}, permission="",
            scope="all")
        async def ping(args: dict) -> str:
            return "pong"          # ← 这句话会进模型上下文，由模型决定怎么说
```

几条规矩：

* 名字全局唯一：`插件名.工具名`，暴露给模型是 `插件名_工具名`；
* handler 签名固定：`async def handler(args: dict) -> str`（可以同步）；
  **想让工具知道「谁在调」就多写一个参数**：`async def handler(args, ctx)`，
  消费侧调用时把上下文传进来（`call_tool(tool, args, ctx)`）。上下文是消费侧
  自己的对象（ai_chat 传它的 `ToolContext`），框架只按鸭子类型用，不 import 插件；
  没声明第二个参数的工具行为不变 —— 老插件不用改；
* `prompt_hint`：一句话说明「什么时候该调它」，消费侧会把它写进系统提示
  （工具 schema 的 description 只在模型主动读工具时才看到，时机判断要更显眼）；
* **权限/作用域在消费侧判**：注册时声明 `permission` / `scope`，
  `ai_chat` 在调用前按它检查（与命令同一套口径），不通过就不把工具暴露给模型；
  `permission`/`scope` 管的是「能不能看见」，工具内部要判「这个群的插件服务范围」
  这类业务白名单，就用上面那个 `ctx`；
* 卸载/禁用/热重载时框架按插件名整批撤销。
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: 注册名允许的字符
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
#: 暴露给模型的名字（OpenAI 函数名约束）
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass
class PluginTool:
    """一个可以被模型调用的工具。"""

    #: 全名 `插件名.工具名`
    name: str
    plugin: str
    #: handler：`args -> 文本`（可以是 async）；声明第二个参数就同时拿到调用者上下文
    handler: Callable[..., Any] = None  # type: ignore[assignment]
    #: handler 是否声明了第二个参数（`handler(args, ctx)`）—— 注册时探测一次，
    #: 调用时据此决定传不传 ctx（不靠每次 `inspect`，也不强制所有工具改签名）
    accepts_ctx: bool = False
    description: str = ""
    #: JSON Schema（`{"type": "object", "properties": {...}}`）
    parameters: dict = field(default_factory=lambda: {"type": "object",
                                                      "properties": {}})
    #: 权限要求：""（所有人）/ "admin" / "superuser"
    permission: str = ""
    #: 作用域：all / private / group
    scope: str = "all"
    #: 结果上限（字），消费侧可再截断
    max_chars: int = 4000
    #: 给**系统提示**用的一句话：「什么时候该调用我」。
    #: 工具的 `description` 只有模型主动读 schema 时才看得到；而「用户说搜题就
    #: 应该存题库」这种**时机判断**写在系统提示里才容易被想起来。为空 = 不写。
    prompt_hint: str = ""

    @property
    def tool_name(self) -> str:
        return _TOOL_NAME_RE.sub("_", self.name)

    def describe(self) -> str:
        """给模型看的描述：**先判据，后功能**（模型靠判据选工具）。"""
        hint = str(self.prompt_hint or "").strip()
        desc = str(self.description or "").strip()
        if hint and desc:
            return f"{hint}｜{desc}"
        return hint or desc or f"{self.plugin} 提供的工具"


class ToolRegistry:
    """插件工具的注册表（进程内共享，由 `PluginManager` 注入）。"""

    def __init__(self, logger=None) -> None:
        self.logger = logger
        self._tools: dict[str, PluginTool] = {}

    # ---- 注册 ----
    def register(self, plugin: str, name: str, handler: Callable, *,
                 description: str = "", parameters: Optional[dict] = None,
                 permission: str = "", scope: str = "all",
                 max_chars: int = 4000, prompt_hint: str = "",
                 when: str = "") -> PluginTool:
        raw = str(name or "").strip()
        if not raw or not _NAME_RE.match(raw):
            raise ValueError(f"工具名不合法：{name!r}（字母数字、_ - .）")
        full = raw if raw.startswith(f"{plugin}.") else f"{plugin}.{raw}"
        if "." in raw and not raw.startswith(f"{plugin}."):
            raise ValueError(f"工具 {full} 映射了别的插件的命名空间")
        if not callable(handler):
            raise ValueError("工具必须给 handler（与技能不同：这里自己写实现）")
        if full in self._tools:
            raise ValueError(f"工具 {full} 已存在")
        params = parameters if isinstance(parameters, dict) else {}
        if not params:
            params = {"type": "object", "properties": {}}
        tool = PluginTool(name=full, plugin=plugin, handler=handler,
                          description=str(description or ""),
                          parameters=params, permission=str(permission or ""),
                          scope=str(scope or "all").lower(),
                          max_chars=max(1, int(max_chars or 4000)),
                          # `when=` 是 `prompt_hint=` 的别名（两个名字都有人用，
                          # 语义就是一个：「什么时候该用我」）
                          prompt_hint=str(prompt_hint or when or "").strip(),
                          accepts_ctx=_wants_ctx(handler))
        self._tools[full] = tool
        if self.logger:
            self.logger.info("[tools] %s 注册工具 %s", plugin, full)
        return tool

    def unregister(self, name: str) -> bool:
        return self._tools.pop(str(name or ""), None) is not None

    def unregister_plugin(self, plugin: str) -> int:
        gone = [n for n, t in self._tools.items() if t.plugin == plugin]
        for n in gone:
            self._tools.pop(n, None)
        if gone and self.logger:
            self.logger.info("[tools] %s 撤销了 %d 个工具", plugin, len(gone))
        return len(gone)

    # ---- 查询 ----
    def get(self, name: str) -> Optional[PluginTool]:
        return self._tools.get(str(name or ""))

    def list(self, *, exclude: str = "") -> list[PluginTool]:
        return [t for t in self._tools.values() if not exclude or t.plugin != exclude]

    def by_tool_name(self, tool_name: str) -> Optional[PluginTool]:
        for t in self._tools.values():
            if t.tool_name == tool_name:
                return t
        return None

    def __len__(self) -> int:
        return len(self._tools)


def _wants_ctx(handler: Callable) -> bool:
    """handler 是否接受第二个位置参数（调用者上下文）。

    只数**位置参数**：`async def f(args)` → 不传；`async def f(args, ctx)` → 传。
    用 `*args` / `**kwargs` 兜底的 handler 不算（拿到的 ctx 位置不确定，
    与其猜错，不如不给）。
    """
    try:
        params = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):        # 内建函数/被装饰器包住的偏函数据不出签名
        return False
    positional = [p for p in params
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2


async def call_tool(tool: PluginTool, args: Optional[dict],
                    ctx: Any = None) -> str:
    """调用工具的 handler，统一成字符串（同步/异步都支持）。

    Args:
        tool: 注册表里的工具。
        args: 模型给出的参数（`dict`；非 dict 按空处理）。
        ctx: 可选的调用者上下文。只有 handler 声明了第二个参数才会传；
            上下文是**消费侧自己的对象**（如 ai_chat 的 `ToolContext`），
            框架只保证它是鸭子类型（`user_id` / `group_id` / `is_superuser` …），
            不 import 任何插件。

    Returns:
        结果文本（已按 `tool.max_chars` 截断）。"""
    data = args if isinstance(args, dict) else {}
    # 声明了第二个参数的工具**总是**收到 ctx（没有调用者时是 None）——
    # 「有时传、有时不传」会让 handler 里多一个不可预期的分支，
    # 而 `handler(args, None)` 至少是确定的，工具自己决定怎么处理。
    out = tool.handler(data, ctx) if tool.accepts_ctx else tool.handler(data)
    if inspect.isawaitable(out):
        out = await out
    text = "" if out is None else str(out)
    return text[: tool.max_chars]
