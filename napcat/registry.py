"""声明式注册：把「注册」和「逻辑」拆开。

插件写大了容易变成「`on_enable` 里定义一堆闭包」：注册、参数、权限、业务逻辑
挤在同一段代码里，找一条命令的逻辑要翻半个文件。本模块把注册变成**声明**：

    # plugins/demo/commands/chat.py —— 只有逻辑
    from napcat.registry import command

    @command("demo.say", schema=SaySchema, permission="", scope="all")
    async def say(plugin, params, event):
        ...

    # plugins/demo/jobs/cleanup.py —— 定时逻辑
    from napcat.registry import every

    @every(3600, name="demo.cleanup")
    async def cleanup(plugin):
        ...

    # plugins/demo/plugin.py —— 只有装配，没有逻辑
    class DemoPlugin(PluginBase):
        async def on_enable(self):
            self.register_module(chat_commands, cleanup_jobs)

处理器**第一个参数固定是插件实例**（`plugin`），其余参数按类型约定：

| 声明 | 处理器签名 | 框架注入 |
|------|-----------|---------|
| `@command(..., schema=...)` | `async def h(plugin, params, event)` | 校验后的 `params`、事件 |
| `@command(...)`（无 schema） | `async def h(plugin, packet, event)` | 原始 `CommandPacket`、事件 |
| `@event(EventType)` | `async def h(plugin, event)` | 事件对象 |
| `@every` / `@daily` / `@delay` | `async def h(plugin)` | 无 |

处理器可以是同步函数（返回值也能用），但事件与定时任务在框架里是 await 的，
写成 `async` 最省心。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional


# ==================== 声明 ====================

@dataclass(frozen=True)
class CommandSpec:
    """一条命令的注册信息（不含逻辑）。"""

    name: str
    handler: Callable
    schema: Optional[type] = None
    permission: Optional[str] = None
    scope: str = "all"
    description: str = ""
    aliases: tuple[str, ...] = ()
    hidden: bool = False


@dataclass(frozen=True)
class EventSpec:
    """一个事件处理器的注册信息。"""

    event_type: type
    handler: Callable
    hid: str = ""
    description: str = ""


@dataclass(frozen=True)
class TimerSpec:
    """一个定时任务的注册信息。

    `kind`：`every`（每 N 秒）/ `daily`（每天 HH:MM）/ `delay`（N 秒后一次）。
    `value`：秒数（float）或 `"08:00"`。
    """

    kind: str
    value: Any
    handler: Callable
    name: str = ""
    description: str = ""


#: 装饰器挂在函数上的属性名（`collect()` 靠它识别）
_COMMAND_ATTR = "__napcat_command__"
_EVENT_ATTR = "__napcat_event__"
_TIMER_ATTR = "__napcat_timer__"


def _attach(func: Callable, attr: str, spec: Any) -> Callable:
    specs = list(getattr(func, attr, ()))
    specs.append(spec)
    setattr(func, attr, specs)
    return func


def command(name: str, *, schema: Optional[type] = None,
            permission: Optional[str] = None, scope: str = "all",
            description: str = "", aliases: Iterable[str] = (),
            hidden: bool = False) -> Callable[[Callable], Callable]:
    """声明一条命令。`name` 用点号分层（`demo.say` → `/demo say`）。"""
    def decorator(func: Callable) -> Callable:
        return _attach(func, _COMMAND_ATTR, CommandSpec(
            name=name, handler=func, schema=schema, permission=permission,
            scope=scope, description=description or _docline(func),
            aliases=tuple(aliases), hidden=hidden))
    return decorator


def event(event_type: type, *, hid: str = "",
          description: str = "") -> Callable[[Callable], Callable]:
    """声明一个事件处理器。`hid` 不写时注册时默认用 `插件名.函数名`。"""
    def decorator(func: Callable) -> Callable:
        return _attach(func, _EVENT_ATTR, EventSpec(
            event_type=event_type, handler=func, hid=hid,
            description=description or _docline(func)))
    return decorator


def every(seconds: float, *, name: str = "",
          description: str = "") -> Callable[[Callable], Callable]:
    """声明周期定时任务（每 `seconds` 秒）。"""
    def decorator(func: Callable) -> Callable:
        return _attach(func, _TIMER_ATTR, TimerSpec(
            kind="every", value=float(seconds), handler=func,
            name=name or func.__name__, description=description))
    return decorator


def daily(time_str: str, *, name: str = "",
          description: str = "") -> Callable[[Callable], Callable]:
    """声明每日定时任务（`"08:00"`，本机时区，月末/闰年由框架处理）。"""
    def decorator(func: Callable) -> Callable:
        return _attach(func, _TIMER_ATTR, TimerSpec(
            kind="daily", value=str(time_str), handler=func,
            name=name or func.__name__, description=description))
    return decorator


def delay(seconds: float, *, name: str = "",
          description: str = "") -> Callable[[Callable], Callable]:
    """声明一次性延时任务（`seconds` 秒后跑一次）。"""
    def decorator(func: Callable) -> Callable:
        return _attach(func, _TIMER_ATTR, TimerSpec(
            kind="delay", value=float(seconds), handler=func,
            name=name or func.__name__, description=description))
    return decorator


def _docline(func: Callable) -> str:
    """没写 description 时取函数 docstring 第一行（命令帮助里会用到）。"""
    doc = inspect.getdoc(func) or ""
    return doc.splitlines()[0].strip() if doc else ""


# ==================== 收集 ====================

def collect(*modules) -> list[Any]:
    """把若干模块里的声明按**定义顺序**收集成 `[CommandSpec | EventSpec | TimerSpec]`。

    只认本模块的装饰器（属性挂在函数上），模块里的其它对象会被忽略 —— 所以
    `commands/__init__.py` 里 `from . import chat` 这种再导出不会重复注册（同一个
    函数对象只收集一次）。
    """
    specs: list[Any] = []
    seen: set[int] = set()
    for module in modules:
        if module is None:
            continue
        attrs = vars(module) if not isinstance(module, dict) else module
        for value in attrs.values():
            for attr in (_COMMAND_ATTR, _EVENT_ATTR, _TIMER_ATTR):
                for spec in getattr(value, attr, ()) or ():
                    key = id(spec)
                    if key in seen:
                        continue
                    seen.add(key)
                    specs.append(spec)
    return specs


__all__ = [
    "CommandSpec", "EventSpec", "TimerSpec",
    "command", "event", "every", "daily", "delay", "collect",
]
