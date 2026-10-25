"""技能注册表 —— 让**别的插件**把自己的能力交给 AI 使用。

命令是给**人**用的（`/byrdocs search …`），技能是给**模型**用的：
一个插件注册技能，`ai_chat` 把它包成工具暴露给模型，模型在对话里调用它。

```python
# 在任意插件里（on_load / on_enable 都行）
@self.register_skill(
    "search",
    description="在 BYR Docs 里搜资料，返回编号 + 标题 + 大小。",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "关键词"}},
        "required": ["query"],
    },
)
async def search(args: dict, ctx: SkillContext) -> str:
    hits = await self._search(args["query"])
    return "\\n".join(f"{h.id} {h.title} {h.size}" for h in hits[:10])
```

几条规矩（都是踩过的坑）：

* **名字全局唯一**：注册名是 `插件名.技能名`，暴露给模型时转成 `插件名_技能名`
  （OpenAI 的函数名只允许 `[a-zA-Z0-9_-]`，点号不合法）；
* **返回字符串**：调用方会截断到上限后塞进工具结果 —— 返回结构体也行，
  会 `json.dumps` 成字符串（模型读文本，读不了对象）；
* **异常不要自己吞**：抛出去，调用方会把它变成「技能执行失败：…」交给模型，
  模型能因此换个问法重试；吞掉只会让它以为成功；
* **别假设调用者是人**：`ctx` 里带调用者身份（谁、在哪个群/私聊、是不是超管），
  需要权限就自己判 —— 这个框架**不替**技能判权限，因为只有你知道
  「谁能搜内部资料」。
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

#: 技能名允许的字符：字母数字、下划线、连字符、点（点用于 `插件.技能` 的分隔）
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
#: 暴露给模型时的名字（OpenAI 函数名约束）
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass
class SkillContext:
    """技能被调用的上下文（谁、在哪、什么会话）。"""

    #: 调用它的插件名（一般是 `ai_chat`）
    caller: str = ""
    #: 真实用户
    user_id: int = 0
    group_id: int = 0
    is_group: bool = False
    #: 会话 id（`ai_chat` 的会话；别的调用方可以自定）
    session_id: str = ""
    #: 权限提示：调用方已经判过的结论，技能可以直接用
    is_superuser: bool = False
    is_admin: bool = False
    #: 附加信息（调用方自定义，技能不认识就忽略）
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"caller": self.caller, "user_id": self.user_id,
                "group_id": self.group_id, "is_group": self.is_group,
                "session_id": self.session_id,
                "is_superuser": self.is_superuser, "is_admin": self.is_admin,
                "extra": dict(self.extra)}


@dataclass
class Skill:
    """一个可被模型调用的技能。"""

    #: 注册名：`插件名.技能名`（唯一）
    name: str
    description: str
    #: JSON Schema（object）。`{type: object, properties: {...}}`
    parameters: dict
    handler: Callable[[dict, SkillContext], Any]
    #: 提供它的插件
    plugin: str = ""

    @property
    def tool_name(self) -> str:
        """给模型看的函数名（点号换成下划线，只留合法字符）。"""
        return _TOOL_NAME_RE.sub("_", self.name)[:64]

    def describe(self) -> str:
        first = (self.description or "").strip().splitlines()[0] if self.description else ""
        return f"`{self.name}` · {self.plugin} · {first[:60]}"


class SkillRegistry:
    """全局技能表（`PluginManager` 持有一个，所有插件共享）。

    为什么放在框架而不是 `ai_chat` 里：提供方是**别的插件**，它们不该为了
    给 AI 用而依赖 `ai_chat`（否则装/卸顺序、可选依赖都会变成坑）。
    """

    def __init__(self, *, logger=None) -> None:
        self._skills: dict[str, Skill] = {}
        self.logger = logger

    # ---- 注册 ----

    def register(self, plugin: str, name: str, *, description: str = "",
                 parameters: Optional[dict] = None,
                 handler: Optional[Callable] = None) -> Skill:
        """注册一个技能（同名覆盖，按插件名可整批撤销）。"""
        full = self.qualify(plugin, name)
        if not _NAME_RE.match(full):
            raise ValueError(
                f"技能名不合法：{full!r}（只允许字母数字、下划线、连字符、点，"
                f"最长 64）")
        if handler is None:
            raise ValueError(f"技能 {full} 没有 handler")
        schema = parameters if isinstance(parameters, dict) else {}
        skill = Skill(name=full, description=str(description or ""),
                      parameters=schema, handler=handler, plugin=plugin)
        self._skills[full] = skill
        if self.logger:
            self.logger.info("[skills] %s 注册技能 %s", plugin, full)
        return skill

    def decorator(self, plugin: str, name: str, **kwargs):
        """`@self.register_skill(...)` 的装饰器形态。"""
        def deco(func):
            self.register(plugin, name, handler=func, **kwargs)
            return func
        return deco

    @staticmethod
    def qualify(plugin: str, name: str) -> str:
        """`("byrdocs", "search")` → `"byrdocs.search"`（已经是全名就原样返回）。"""
        n = str(name or "").strip()
        p = str(plugin or "").strip()
        if p and not n.startswith(p + "."):
            return f"{p}.{n}"
        return n

    # ---- 查询 ----

    def list(self, *, exclude: Optional[str] = None) -> list[Skill]:
        """所有技能（可按提供方过滤：调用方通常要排除自己）。"""
        out = [s for s in self._skills.values() if s.plugin != exclude]
        out.sort(key=lambda s: s.name)
        return out

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(str(name or ""))

    def by_tool_name(self, tool_name: str) -> Optional[Skill]:
        want = str(tool_name or "")
        for skill in self._skills.values():
            if skill.tool_name == want:
                return skill
        return None

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return str(name) in self._skills

    # ---- 撤销 ----

    def unregister(self, name: str) -> bool:
        return self._skills.pop(str(name or ""), None) is not None

    def unregister_plugin(self, plugin: str) -> int:
        """插件卸载/禁用时把它注册的技能一起撤掉（否则模型会调用到死函数）。"""
        gone = [n for n, s in self._skills.items() if s.plugin == plugin]
        for n in gone:
            self._skills.pop(n, None)
        if gone and self.logger:
            self.logger.info("[skills] %s 撤销了 %d 个技能", plugin, len(gone))
        return len(gone)


async def call_skill(skill: Skill, args: dict, ctx: SkillContext) -> str:
    """执行技能并归一化返回值；异常**原样抛出**（由调用方决定怎么告诉模型）。"""
    result = skill.handler(dict(args or {}), ctx)
    if inspect.isawaitable(result):
        result = await result            # type: ignore[assignment]
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list, tuple, int, float, bool)):
        import json
        try:
            return json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(result)
    return str(result)
