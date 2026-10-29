"""技能注册表 —— 把插件**已有的命令**交给 AI 使用。

命令是给人敲的（`/byrdocs search 线代`），技能是给模型调的：同一个命令，
模型也能触发。所以技能**不另写 handler** —— 它只是一个「命令映射」：

```python
async def on_load(self):
    # 把 /byrdocs search 这个命令挂给 AI；参数 schema 与说明都从命令里取
    self.register_skill("search", command="byrdocs.search")
```

执行时走 **`CommandManager.handle()`**（合成一条真实消息事件），所以
**权限、作用域、Schema 校验、错误反馈**与用户手敲完全同一条路径。
结果由命令的返回值发出，AI 不说话、也不接收它的输出（委派语义）。

几条规矩：

* **名字全局唯一**：注册名是 `插件名.技能名`，暴露给模型时转成 `插件名_技能名`
  （OpenAI 的函数名只允许 `[a-zA-Z0-9_-]`）；
* **只映射自己插件的命令**（防止把别人的命令包装成自己的技能绕过权限）；
* **命令不存在就不注册**（插件还没注册命令时别硬挂，`SkillRegistry.resolve`
  会在消费侧再查一次，查不到就跳过，模型不会看到死工具）。

本模块同时提供技能表与工具表共用的 `NamedRegistry` 骨架（命名空间归一化 +
按插件整批回收）—— 两条通道的区别在**内容物**（命令映射 vs handler），
簿记逻辑没必要各写一份。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: 注册名允许的字符：字母数字、下划线、连字符、点（点用于 `插件.技能` 的分隔）
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
#: 暴露给模型时的名字（OpenAI 函数名约束）
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def qualify_name(plugin: str, name: str) -> str:
    """`("byrdocs", "search")` → `"byrdocs.search"`（已经是全名就原样返回）。

    命名空间归一化只有这一份实现：技能表、工具表和 `PluginBase` 的便捷入口
    都调它（以前三处各写一遍带前缀判断的代码）。
    """
    n = str(name or "").strip()
    p = str(plugin or "").strip()
    if p and not n.startswith(p + "."):
        return f"{p}.{n}"
    return n


def tool_name_for(name: str) -> str:
    """给模型看的函数名（点号换成下划线，只留 OpenAI 允许的字符）。"""
    return _TOOL_NAME_RE.sub("_", str(name or ""))[:64]


class NamedRegistry:
    """按「插件名.名字」存取的注册表骨架（技能表 / 工具表共用）。

    子类只管两件事：`register()` 的参数校验与数据类语义。查询、按插件整批
    回收、命名空间归一化都在这里 —— 这些曾经在两个注册表里各写了一份，
    连 `list()` 的默认参数与排序都悄悄不一致。

    子类通过类属性描述差异：

    - `LOG_TAG`：日志前缀（`skills` / `tools`）；
    - `LABEL`：日志里的分类名（`技能` / `工具`）；
    - `SORTED`：`list()` 是否按名字排序（技能排、工具保持注册顺序）。
    """

    LOG_TAG = "registry"
    LABEL = "条目"
    SORTED = True

    def __init__(self, *, logger=None) -> None:
        self._entries: dict[str, Any] = {}
        self.logger = logger

    # ---- 查询 ----

    def get(self, name: str):
        return self._entries.get(str(name or ""))

    def list(self, *, exclude: str = "") -> list:
        """所有条目（可按提供方过滤：调用方通常要排除自己）。

        Args:
            exclude: 提供方插件名；空串表示不过滤。

        Returns:
            条目列表；`SORTED` 为真时按名字排序。
        """
        out = [e for e in self._entries.values() if not exclude or e.plugin != exclude]
        if self.SORTED:
            out.sort(key=lambda e: e.name)
        return out

    def by_tool_name(self, tool_name: str):
        want = str(tool_name or "")
        for entry in self._entries.values():
            if entry.tool_name == want:
                return entry
        return None

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: str) -> bool:
        return str(name or "") in self._entries

    @staticmethod
    def qualify(plugin: str, name: str) -> str:
        """把 `插件名 + 短名` 拼成全名（见 `qualify_name`）。"""
        return qualify_name(plugin, name)

    # ---- 撤销 ----

    def unregister(self, name: str) -> bool:
        return self._entries.pop(str(name or ""), None) is not None

    def unregister_plugin(self, plugin: str) -> int:
        """插件卸载/禁用时把它注册的条目整批撤掉（否则模型会调用到死函数）。"""
        gone = [n for n, e in self._entries.items() if e.plugin == plugin]
        for n in gone:
            self._entries.pop(n, None)
        if gone and self.logger:
            self.logger.info("[%s] %s 撤销了 %d 个%s", self.LOG_TAG, plugin,
                             len(gone), self.LABEL)
        return len(gone)


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
    #: **怎么回消息**：`await ctx.send("文字")` 直接把结果发给用户。
    #:
    #: 技能是「用户说人话 → AI 转成指令 → 插件执行并自己回复」这条链，
    #: 所以插件要么用它回话，要么走自己的命令通道；AI 不会替它转述，
    #: 也不会收到它的输出（`SkillContext` 里没有返回值回传通道）。
    send: Optional[Callable[[str], Any]] = None

    def to_dict(self) -> dict:
        return {"caller": self.caller, "user_id": self.user_id,
                "group_id": self.group_id, "is_group": self.is_group,
                "session_id": self.session_id,
                "is_superuser": self.is_superuser, "is_admin": self.is_admin,
                "extra": dict(self.extra)}


@dataclass
class Skill:
    """一个可被模型触发的**命令映射**。"""

    #: 注册名：`插件名.技能名`（唯一）
    name: str
    #: 被映射的命令（`父.子`，与 `register_command` 里的名字一致）
    command: str
    #: 提供它的插件
    plugin: str = ""
    #: 说明（不给就用命令自己的 description）
    description: str = ""
    #: 给**系统提示**用的一句话：「什么时候该用它」。
    #:
    #: 与 `description` 的分工：description 说明「它能干什么」，这句说明
    #: 「**何时**该用、何时不要用」（模型的时机判断靠它）。消费方会把它写进
    #: 系统提示的「工具/技能使用时机」一节，比藏在 schema 里显眼得多。
    prompt_hint: str = ""
    #: **怎么把命令的产物发给用户** —— 由提供方插件决定（`async (payload, ctx)`）。
    #:
    #: 为什么不由调用方（ai_chat）发：命令的产物可能是 `MessageChain`、`list[chain]`、
    #: 段列表，还各有分段/图片/文件/@ 的规矩。只有插件自己知道该怎么发；转发层
    #: 猜一次就错一次（实测：chain 列表被当段列表，整条通知一个字都没发出去）。
    #: 不给就用调用方的默认发送（简单插件可以不管）。
    deliver: Optional[Callable[[Any, SkillContext], Any]] = None

    @property
    def tool_name(self) -> str:
        """给模型看的函数名（点号换成下划线，只留合法字符）。"""
        return tool_name_for(self.name)

    def command_line(self) -> str:
        """/byrdocs search（命令名里的点换成空格，即用户手敲的样子）。"""
        return "/" + str(self.command or "").replace(".", " ")

    def describe(self) -> str:
        return f"`{self.name}` → {self.command_line()} · {self.plugin}"


class SkillRegistry(NamedRegistry):
    """全局技能表（`PluginManager` 持有一个，所有插件共享）。

    为什么放在框架而不是 `ai_chat` 里：提供方是**别的插件**，它们不该为了
    给 AI 用而依赖 `ai_chat`（否则装/卸顺序、可选依赖都会变成坑）。
    """

    LOG_TAG = "skills"
    LABEL = "技能"
    SORTED = True

    def register(self, plugin: str, name: str, *, command: str = "",
                 description: str = "", prompt_hint: str = "", when: str = "",
                 cmds=None, deliver: Optional[Callable] = None) -> Skill:
        """把一个**已有命令**注册成技能（同名覆盖，按插件名可整批撤销）。

        Args:
            plugin: 提供方插件名（框架自动填）。
            name: 技能名（暴露给模型时是 `插件名_技能名`）。
            command: 被映射的命令全名，如 `"byrdocs.search"`。
            description: 说明；不给就用命令自己的 description。

        Raises:
            ValueError: 名字非法、没给 command、或映射了**别人**的命令。
        """
        full = qualify_name(plugin, name)
        if not NAME_RE.match(full):
            raise ValueError(
                f"技能名不合法：{full!r}（只允许字母数字、下划线、连字符、点，"
                f"最长 64）")
        cmd = str(command or "").strip()
        if not cmd:
            raise ValueError(
                f"技能 {full} 没有 command：技能只是「命令映射」，"
                f"写法 self.register_skill({name!r}, command=\"插件名.命令\")")
        # 归属校验靠**命令自己记的 plugin 字段**，不靠命名空间猜：
        # `bupt_news` 插件的命令叫 `buptnews.*`（名字和插件名并不相同），
        # 按前缀比对会把正常映射误杀。
        # 命不存在的命令在这里**不报错**（插件可能稍后才注册命令），
        # 消费侧每次 `find_command()` 解析，解析不到就不暴露。
        if cmds is not None and hasattr(cmds, "find_command"):
            found = cmds.find_command(cmd)
            handler = found[1] if found else None
            owner = getattr(handler, "plugin", "") if handler is not None else ""
            if owner and plugin and owner != plugin:
                raise ValueError(
                    f"技能 {full} 映射了别的插件的命令 {cmd}"
                    f"（它属于 {owner}，应由那个插件自己注册技能）")
        skill = Skill(name=full, command=cmd, plugin=plugin,
                      description=str(description or ""),
                      # `when=` 是别名：两个名字都会有人写，语义相同
                      prompt_hint=str(prompt_hint or when or "").strip(),
                      deliver=deliver)
        self._entries[full] = skill
        if self.logger:
            self.logger.info("[skills] %s 注册技能 %s → %s",
                             plugin, full, skill.command_line())
        return skill
