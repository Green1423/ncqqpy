"""
插件系统 - 完整的插件生命周期管理 + 运行时热重载
支持加载、启用、禁用、卸载，基于文件监控的热重载
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import os
import sys
import time
import traceback
from abc import ABC, abstractmethod

from .skills import SkillContext, SkillRegistry
from pathlib import Path
from typing import Optional, Type

from .api import ApiClient
from .config import ConfigFill
from .deps import (
    DependencyError, RequirementSpec, check_dependencies, dependency_names,
    graph, load_order as dep_load_order, unload_order as dep_unload_order,
    validate_declaration,
)
from .events import EventBus, TimerManager
from .command import CommandManager
from .permission import PermissionManager
from .file_manager import FileManager
from .models import MessageEvent, NapCatEvent
from .errors import (
    ErrorReporter, NapCatError, PluginConfigError, PluginDependencyError,
    PluginError, PluginLifecycleError, PluginLoadError, PluginRuntimeError,
    short_reason,
)


def provided_version(instance) -> str:
    """插件对外声明的版本（依赖版本要求按它比对）。

    取 `provides_version` 声明的版本，没声明就用 `version` —— 插件名与版本号
    本来就是同一个实例上的两个字段，不需要再引一层「提供者注册表」。
    """
    declared = str(getattr(instance, "provides_version", "") or "").strip()
    return declared or str(getattr(instance, "version", "") or "").strip()


class _DisabledProxy:
    """包一层「被显式禁用」的插件实例：`enabled` 恒为 False。

    `check_dependencies` 只看 `enabled` 属性，用它就能把「被运维禁用」
    与「不存在」区分成两条不同的原因（**去启用** / 去安装）—— 两者的
    处理动作完全不同，文案里必须分开。
    """

    def __init__(self, plugin):
        self._plugin = plugin

    def __getattr__(self, item):
        return getattr(self._plugin, item)

    @property
    def enabled(self) -> bool:
        return False


class _DepsProxy:
    """`self.deps` —— 按名字取**依赖插件实例**的只读代理。

    ```python
    store = self.deps.get("store")      # 必需依赖（加载期已保证在）
    if self.deps.has("ai_chat"):        # 可选依赖（不在也不该炸）
        ...
    ```

    取不到时 `get()` 返回 None（不抛）：可选依赖本来就允许缺席，
    而必需依赖在**加载期**就已经校验过，真到运行时还没有属于框架 bug，
    不该让插件作者的调用点到处写 try/except。
    """

    def __init__(self, plugin: "PluginBase"):
        self._plugin = plugin

    def _manager(self):
        own = getattr(self._plugin, "_manager", None)
        if own is not None:
            return own
        return getattr(getattr(self._plugin, "client", None), "plugins", None)

    def get(self, name: str):
        manager = self._manager()
        return manager.get(name) if manager is not None else None

    def has(self, name: str) -> bool:
        """依赖是否**已加载**（不区分是否启用 —— 用 `enabled(name)` 看启用）。"""
        return self.get(name) is not None

    def enabled(self, name: str) -> bool:
        instance = self.get(name)
        return bool(instance is not None and getattr(instance, "enabled", False))

    def require(self, name: str) -> "PluginBase":
        """取必需依赖，取不到直接抛（**运行期**兜底，正常情况下走不到）。"""
        instance = self.get(name)
        if instance is None:
            raise PluginDependencyError(
                f"依赖插件 {name} 没有加载（它是否被禁用/卸载了？）",
                plugin=self._plugin.name, missing=[name])
        return instance

    def names(self) -> list[str]:
        """本插件声明的全部依赖名（必需 + 可选）。"""
        required, optional = dependency_names(self._plugin)
        return required + optional

    def __contains__(self, name: str) -> bool:
        return self.has(name)

    def __bool__(self) -> bool:
        return True


#: 插件包在 `__init__.py` 里用 `PLUGIN = 我的插件类` 显式声明入口类。
#:
#: 为什么需要它：框架原来是在模块属性里**按字母序扫第一个** `PluginBase` 子类。
#: 拆分目录后 `__init__.py` 往往顺手 `from .base import GenericPlugin`，于是
#: 字母序靠前的**基类**会被实例化 —— 症状是「插件加载了，但行为不是我的子类」。
PLUGIN_ENTRY_ATTR = "PLUGIN"


def resolve_plugin_class(module, *, name: str = "", logger=None):
    """找出插件模块里的入口类。

    优先显式声明 `PLUGIN = 类`；没写就退回旧行为（按属性名字母序取第一个），
    但会告警 —— 多个候选时静默取第一个正是「加载了但行为不对」的常见来源。
    """
    explicit = getattr(module, PLUGIN_ENTRY_ATTR, None)
    if explicit is not None:
        if (inspect.isclass(explicit) and issubclass(explicit, PluginBase)
                and explicit is not PluginBase):
            return explicit
        if logger is not None:
            logger.warning(
                "插件 %s 的 __init__.py 里 PLUGIN=%r 不是 PluginBase 子类，已忽略",
                name or "?", explicit)
    candidates: list[type] = []
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (inspect.isclass(attr) and issubclass(attr, PluginBase)
                and attr is not PluginBase and attr not in candidates):
            candidates.append(attr)
    if len(candidates) > 1 and logger is not None:
        logger.warning(
            "插件 %s 导出了 %d 个 PluginBase 子类（%s）；按字母序取 %s。"
            "建议在 __init__.py 里写 `PLUGIN = %s` 显式声明入口",
            name or "?", len(candidates),
            ", ".join(c.__name__ for c in candidates),
            candidates[0].__name__, candidates[0].__name__)
    return candidates[0] if candidates else None


class PluginBase(ABC):
    """插件基类 - 定义完整生命周期。

    生命周期:
        __init__ → on_load → on_enable → (运行中) → on_disable → on_unload

    用法:
        class MyPlugin(PluginBase):
            name = "my_plugin"
            version = "1.0.0"
            description = "示例插件"

            async def on_load(self):
                self.logger.info("插件加载")

            async def on_enable(self):
                @self.on_message()
                async def handle(event: MessageEvent):
                    if event.plain_text.startswith("/hello"):
                        await self.api.send_group_msg(
                            group_id=str(event.group_id),
                            message="Hello!",
                        )
    """

    name: str = ""
    version: str = "0.1.0"
    description: str = ""
    author: str = ""

    #: 必需依赖（插件名列表）：**没装/没启用就不加载本插件**，原因私聊超管。
    #: 支持版本要求：`requires = ["store", "ai_chat>=1.2"]`。
    requires: list[str] = []
    #: 可选依赖：有就用、没有照常加载（`self.deps.get("名字")` 拿到 None）。
    #: 写在这里的价值是「装/卸时能提示」，以及在都装的情况下保证加载顺序。
    optional_requires: list[str] = []
    #: 对外声明的版本（默认同 `version`）—— 依赖方写的 `名字>=1.2` 按它比对。
    provides_version: str = ""

    def __init__(self, api: ApiClient, event_bus: EventBus, timer: TimerManager,
                 cmd_manager: CommandManager, perm_manager: PermissionManager,
                 file_manager: FileManager, logger, groups=None, cfg=None,
                 client=None, error_reporter=None, plugin_dir=None,
                 skill_registry=None):
        self.api = api
        #: 插件自己的目录（包插件是 `plugins/<名字>/`，单文件插件是 `plugins/`）。
        #: `self.database()` 默认把 sqlite 文件放在它下面的 `data/` 里。
        self._plugin_dir = Path(plugin_dir) if plugin_dir else None
        #: 本插件打开的 sqlite 库（`self.database()` 缓存；卸载时统一 close）
        self._databases: dict[str, object] = {}
        #: 本插件注册在 client 上的连接状态回调（`self.on_ws_*`）——
        #: 裸调 `client.on_ws_*` 不受生命周期管理，这里统一记归属，disable/unload 时注销。
        self._client_hooks: list[tuple[str, object]] = []
        self.event_bus = event_bus
        self.timer = timer
        self.cmds = cmd_manager
        #: 技能表（**别的插件**把能力交给 AI 用的通道，见 `napcat.skills`）。
        #: 框架注入共享实例；单独 new 插件（测试/脚本）时给一个独立实例，
        #: 语义仍然是「注册给能看见它的人」。
        self.skills = skill_registry if skill_registry is not None else SkillRegistry()
        self.perm = perm_manager
        self.files = file_manager
        self.logger = logger
        self._groups = groups  # GroupBinding from client
        self._cfg_root = cfg   # ConfigManager
        # 主客户端：只读引用，给需要连接状态/拉起重启的插件用（如 ncqq_watchdog）
        self.client = client
        #: 错误反馈业主：**不要自己拼错误文案发给用户** —— 用
        #: `await self.errors.report(exc, event=event)`（群里会 @ 触发的人、带错误
        #: 编号、同类错误自动节流），或者抛 `napcat` 里的错误类型让框架处理。
        self.errors = error_reporter or ErrorReporter()
        self._config = None     # lazy PluginConfig
        self._enabled = False
        self._loaded = False
        self._handlers: list[tuple] = []
        #: 本实例注册过的定时任务名（卸载/加载失败时按它回收 —— 定时任务原来
        #: 只靠插件自觉取消，`on_load` 里注册完就抛异常的插件会留下永久任务）。
        self._timers: list[str] = []
        # 上一次「未启用」状态下的处理器数量基线：on_disable 只撤销这之后新增的，
        # 使得重复调用 on_disable 不会误删重新注册的处理器（见 on_disable）。
        self._handlers_base = 0
        #: 构造期配置初始化的结果 / 失败原因（框架加载后据此告警超管）。
        self.config_seed: ConfigFill = ConfigFill([], [])
        self.config_seed_error: Optional[BaseException] = None
        #: 依赖访问代理（`self.deps.get("store")`）；见 `_DepsProxy`。
        self.deps = _DepsProxy(self)
        #: 加载自己的 PluginManager（框架实例化时注入；独立构造时为 None）。
        self._manager = None
        # 配置初始化放在**构造期**：这样「所有插件首次运行就把全部配置项写进
        # config.json」对任何构造路径都成立 —— 框架加载、测试直接 new、脚本里
        # 手动构造都一样，不依赖调用方记得补这一步。
        self._seed_config()

    # ---- 生命周期 ----

    async def on_load(self):
        """插件被加载时调用（注册事件/命令/定时器）"""
        pass

    async def on_enable(self):
        """插件被启用时调用"""
        pass

    # ---- 依赖 ----

    @classmethod
    def dependency_specs(cls) -> tuple[list[RequirementSpec], list[RequirementSpec]]:
        """本插件声明的依赖 (必需, 可选)。

        ⚠️ 这是**类方法**：框架在实例化之前就要读它 —— 依赖不满足时压根不该
        构造插件（否则 `config_template()` 已经写盘、`on_load` 已经跑了一半）。
        声明写坏（名字非法、依赖自己、必需/可选重复）会抛 `DependencyError`。
        """
        return validate_declaration(cls.name or cls.__name__,
                                    getattr(cls, "requires", None),
                                    getattr(cls, "optional_requires", None))

    @classmethod
    def dependency_names(cls) -> tuple[list[str], list[str]]:
        """(必需依赖名, 可选依赖名) —— 只要名字，排序/建图用。"""
        required, optional = cls.dependency_specs()
        return [s.name for s in required], [s.name for s in optional]

    def dependency_check(self):
        """当前依赖是否满足（`DependencyCheck`：missing/disabled/version_mismatch…）。

        加载期框架用它决定「能不能加载」；插件运行期也可以用它自查
        （例如可选依赖突然被禁用时降级）。依赖是**按名字**去框架查的，
        不要求依赖先加载 —— 因此可用在 `on_load` 里。
        """
        required, optional = self.dependency_specs()
        manager = getattr(self, "_manager", None) or getattr(
            getattr(self, "client", None), "plugins", None)
        return check_dependencies(
            required, optional,
            resolve=(manager.get if manager is not None else (lambda _name: None)),
            version_of=provided_version, owner=self.name)

    def _dep_names(self) -> tuple[list[str], list[str]]:
        """内部用：声明写坏时不抛异常，退化成「没有依赖」并在日志里说清。"""
        try:
            return self.dependency_names()
        except DependencyError as exc:
            self._log("error", f"依赖声明非法：{exc}")
            return [], []

    def _seed_config(self) -> ConfigFill:
        """按 `config_template()` 初始化 `config.json` 里本插件的配置段。

        语义是 `PluginConfig.fill_defaults()` 的三条（只补缺 / 不覆盖 / 幂等），
        外加两条约定：

        - 补之前**先按磁盘重读** `config.json`：`ConfigManager` 只在构造时读一次
          文件，拿启动时的旧快照整段写回会覆盖运维手改的内容；
        - **绝不抛异常**：模板写错、配置读不出来都只记日志并留在
          `self.config_seed_error` 里（框架加载后会私聊超管），插件照常可用。
        """
        config = self.config
        if config is None:
            return self.config_seed          # 没注入 ConfigManager（独立使用）
        try:
            template = self.config_template() or {}
        except Exception as exc:  # noqa: BLE001 - 模板写错不该让插件起不来
            self.config_seed_error = exc
            self._log("warning", "config_template() 报错，跳过配置初始化", exc_info=True)
            return self.config_seed
        if not template:
            return self.config_seed
        try:
            reloader = getattr(self._cfg_root, "reload", None)
            if callable(reloader):
                reloader()
            report = config.fill_defaults(template)
        except Exception as exc:  # noqa: BLE001
            self.config_seed_error = exc
            self._log("warning", "配置初始化失败", exc_info=True)
            return self.config_seed
        self.config_seed = report
        if report.filled:
            shown = ", ".join(report.filled[:8]) + ("…" if len(report.filled) > 8 else "")
            self._log("info", f"已初始化 {len(report.filled)} 个配置项"
                              f"（plugins.{self.name}）：{shown}")
        if report.conflicts:
            self._log("warning", "配置项类型与模板不符（保留你的值，模板未生效）："
                                 + ", ".join(report.conflicts))
        return report

    def _log(self, level: str, message: str, **kwargs) -> None:
        """打日志（没有 logger 时静默 —— 独立构造的实例不该因此炸掉）。"""
        if not self.logger:
            return
        getattr(self.logger, level)(f"[{self.name}] {message}", **kwargs)

    def _off_own_handlers(self) -> int:
        """撤销本实例注册过的全部处理器（启动后再调用是空操作）。

        ⚠️ `EventBus` **不按 `hid` 去重** —— 同一个 id 注册几次就有几条监听器，
        所以「重复注册」不会被自动吸收，必须显式撤销。热重载的
        「新版本加载失败 → 回滚旧实例」路径会在**同一个实例**上再次调用
        `on_enable()`，`on_load()` 也可能被再调一次，因此注册方（on_enable）
        与撤销方（on_disable）都要幂等，只靠一边不够：
        实测每回滚一次就多一份 `ai_chat._on_any_message`（/events 里能看到）。

        连接状态回调（`self.on_ws_*`）也在这里一并注销：它们和事件处理器一样是
        「插件对外的反应面」，停用后不该继续收到 WS 连上/断开。
        """
        self._off_own_client_hooks()
        removed = 0
        while self._handlers:
            event_type, handler = self._handlers.pop()
            if self.event_bus.off(event_type, handler):
                removed += 1
        self._handlers_base = 0
        return removed

    async def on_disable(self):
        """插件被禁用时调用（取消注册本插件注册的处理器）。

        ⚠️ **必须可重复调用**（幂等）：热重载的「新版本加载失败 → 回滚旧实例」
        路径会在同一个实例上再走一次 `on_enable()`，而框架可能在调用它之前
        又调了一次 `on_disable()`。此前是「清空整个 `_handlers`」，
        第二次调用会把**重新注册过**的处理器也一并摘掉，
        于是回滚后插件静默失效。

        这里改成只撤销「上一次启用之后新增的那一段」，多余的重复调用是空操作。
        """
        while len(self._handlers) > self._handlers_base:
            event_type, handler = self._handlers.pop()
            self.event_bus.off(event_type, handler)

    def _off_own_timers(self) -> int:
        """取消本实例注册过的全部定时任务（离场与加载失败时都要做）。"""
        removed = 0
        for name in list(self._timers):
            try:
                self.timer.cancel(name)
                removed += 1
            except Exception:  # noqa: BLE001 - 回收失败不该影响卸载流程
                self.logger.warning("取消定时任务 %s 失败", name, exc_info=True)
        self._timers.clear()
        return removed

    async def on_unload(self):
        """插件被卸载时调用（清理资源）"""
        pass

    # ---- 辅助装饰器 ----

    def on_message(self, group_id: int = None, private: bool = False,
                   hid: str = None, description: str = ""):
        """注册消息事件处理器。

        Args:
            group_id: 只处理该群的消息；`None` 表示所有群。
            private: `True` 时只处理私聊消息。
            hid: 处理器 ID（用于 `/stop event`、`/pause event`）。
            description: 处理器说明（`/events` 列表展示）。

        Returns:
            装饰器；被装饰函数接收 `MessageEvent`。"""
        def decorator(func):
            async def wrapper(event: MessageEvent):
                if not isinstance(event, MessageEvent):
                    return
                if group_id and event.group_id != group_id:
                    return
                if private and event.message_type != "private":
                    return
                await func(event)
            _hid = hid or f"{self.name}.{func.__name__}"
            self.event_bus.on(MessageEvent, hid=_hid, description=description)(wrapper)
            self._handlers.append((MessageEvent, wrapper))
            return wrapper
        return decorator

    def on_event(self, event_type: Type[NapCatEvent],
                 hid: str = None, description: str = ""):
        """注册指定类型的事件处理器"""
        def decorator(func):
            async def wrapper(event):
                await func(event)
            _hid = hid or f"{self.name}.{func.__name__}"
            self.event_bus.on(event_type, hid=_hid, description=description)(wrapper)
            self._handlers.append((event_type, wrapper))
            return wrapper
        return decorator

    def register_command(self, name: str, **kwargs):
        """注册命令（`CommandManager.register` 的插件便捷入口）。

        Args:
            name: 命令名，子命令写 `父.子`。
            **kwargs: 透传给 `CommandManager.register`（schema / permission / scope …）。

        Returns:
            装饰器；被装饰函数的参数按名注入（`packet`、Schema 字段、`event`）。"""
        kwargs.setdefault("plugin", self.name)
        return self.cmds.register(name, **kwargs)

    def unregister_command(self, name: str) -> bool:
        """取消注册命令"""
        return self.cmds.unregister(name)

    # ---- 技能（把能力交给 AI 用，见 napcat.skills）----

    def register_skill(self, name: str, *, command: str = "",
                       description: str = "",
                       deliver=None) -> "Skill":
        """把你的**一条命令**挂给 AI 使用（技能 = 命令映射，不另写 handler）。

            async def on_load(self):
                self.register_skill("search", command="byrdocs.search")

        参数 schema 与说明都**从命令自己取**（`CommandSchema` / `description`），
        执行时走 `CommandManager.handle()` —— 权限、作用域、Schema 校验、
        错误反馈与用户手敲完全同一条路径。

        触发时由命令的返回值发消息，AI 不接话、也不接收输出。
        卸载/禁用时框架会把本插件注册的技能一起撤掉。
        """
        # `cmds` 可能不存在（测试里 __new__ 出来的最小插件）——
        # 归属校验是「能校验就更严」，不该因此让注册失败
        return self.skills.register(
            self.name, name, command=command, description=description,
            cmds=getattr(self, "cmds", None), deliver=deliver)

    def unregister_skill(self, name: str) -> bool:
        return self.skills.unregister(self.skills.qualify(self.name, name))

    # ---- 声明式注册（注册与逻辑分离，见 napcat.registry）----

    def register_module(self, *modules) -> "PluginBase":
        """把模块里用 `@command` / `@event` / `@every` 声明的处理器注册进来。

        典型用法（`on_enable` 里只有装配，没有逻辑）：

            from .commands import chat, admin
            from .jobs import cleanup

            class MyPlugin(PluginBase):
                async def on_enable(self):
                    self.register_module(chat, admin, cleanup)

        处理器第一个参数固定是插件实例；其余参数按 `napcat.registry` 的约定注入。
        返回 self，便于链式。
        """
        from .registry import CommandSpec, EventSpec, TimerSpec, collect
        for spec in collect(*modules):
            if isinstance(spec, CommandSpec):
                self._register_command_spec(spec)
            elif isinstance(spec, EventSpec):
                self._register_event_spec(spec)
            elif isinstance(spec, TimerSpec):
                self._register_timer_spec(spec)
        return self

    def _register_command_spec(self, spec) -> None:
        if spec.schema is not None:
            async def shim(params=None, event=None):
                result = spec.handler(self, params, event)
                return await result if inspect.isawaitable(result) else result
        else:
            async def shim(packet=None, event=None):
                result = spec.handler(self, packet, event)
                return await result if inspect.isawaitable(result) else result
        self.register_command(
            spec.name, schema=spec.schema, permission=spec.permission,
            scope=spec.scope, description=spec.description,
            aliases=list(spec.aliases) or None)(shim)

    def _register_event_spec(self, spec) -> None:
        async def shim(event):
            result = spec.handler(self, event)
            return await result if inspect.isawaitable(result) else result
        hid = spec.hid or f"{self.name}.{spec.handler.__name__}"
        self.on_event(spec.event_type, hid=hid,
                      description=spec.description)(shim)

    def _register_timer_spec(self, spec) -> None:
        async def shim():
            result = spec.handler(self)
            return await result if inspect.isawaitable(result) else result
        if spec.kind == "every":
            self.schedule(float(spec.value), name=spec.name)(shim)
        elif spec.kind == "daily":
            self.schedule_at(str(spec.value), name=spec.name)(shim)
        elif spec.kind == "delay":
            name = spec.name
            self._timers.append(name)
            self.timer.delay(float(spec.value), name=name,
                             description=spec.description)(shim)
        else:                                            # pragma: no cover - 防御
            raise ValueError(f"未知的定时任务类型：{spec.kind!r}")

    # ---- 连接状态回调（归插件所有，随 disable/unload 注销）----

    def on_ws_connected(self, callback):
        """注册「连接就绪」回调 `async callback()`（首次连上 + 每次重连）。

        与直接 `self.client.on_ws_connected(...)` 的区别：这里注册的回调**归本插件
        所有** —— `on_disable()` / 卸载时框架自动注销。裸注册在 client 上不在生命周期
        清理范围里，热重载后会留下旧实例的回调（实测：卸载后仍被调用）。
        """
        return self._track_client_hook("connected", callback)

    def on_ws_disconnected(self, callback):
        """注册「连接断开」回调 `async callback()`（归本插件所有）。"""
        return self._track_client_hook("disconnected", callback)

    def on_ws_connect_failed(self, callback):
        """注册「连接建立不起来」回调 `async callback(attempt, exc)`（归本插件所有）。"""
        return self._track_client_hook("connect_failed", callback)

    def _track_client_hook(self, kind: str, callback):
        client = self.client
        if client is None:
            raise RuntimeError(
                "没有 client（手搓实例 / 测试宿主）：无法注册连接状态回调")
        getattr(client, f"on_ws_{kind}")(callback)
        self._client_hooks.append((kind, callback))
        return callback

    def _off_own_client_hooks(self) -> None:
        """注销本插件注册的全部连接状态回调（disable / unload 都会走）。"""
        for kind, callback in list(self._client_hooks):
            off = getattr(self.client, f"off_ws_{kind}", None) if self.client else None
            if off is not None:
                try:
                    off(callback)
                except Exception:  # noqa: BLE001 - 注销失败不该挡住卸载
                    self.logger.warning("注销连接回调失败（%s）", kind, exc_info=True)
        self._client_hooks.clear()

    def off_event(self, event_type: Type[NapCatEvent], handler: Callable) -> bool:
        """取消注册指定事件处理器。同时从 self._handlers 中移除。"""
        for i, (et, h) in enumerate(self._handlers):
            if et is event_type and h is handler:
                self._handlers.pop(i)
                break
        return self.event_bus.off(event_type, handler)

    def off_all_events(self):
        """取消注册本插件全部事件处理器"""
        for event_type, handler in self._handlers:
            self.event_bus.off(event_type, handler)
        self._handlers.clear()

    def schedule(self, seconds: float, name: str = None):
        """注册固定间隔的定时任务。

        间隔从**本轮执行结束**开始计时；执行超过间隔会跳过错过的轮次，不会堆积。

        Args:
            seconds: 间隔秒数，必须大于 0。
            name: 任务名（默认取函数名）；同名任务覆盖旧任务。

        Returns:
            装饰器；被装饰函数不接收参数。"""
        def decorator(func):
            task_name = name or func.__name__
            self._timers.append(task_name)
            return self.timer.every(seconds, name=task_name)(func)
        return decorator

    def schedule_at(self, time_str: str, name: str = None):
        """注册每日定时任务（同样由框架在卸载时回收）。"""
        def decorator(func):
            task_name = name or func.__name__
            self._timers.append(task_name)
            return self.timer.at(time_str, name=task_name)(func)
        return decorator

    def cancel_schedule(self, name: str):
        """取消指定名称的定时任务"""
        self.timer.cancel(name)
        if name in self._timers:
            self._timers.remove(name)

    @property
    def config(self):
        """插件配置代理（命名空间 plugins.<self.name>）"""
        if self._config is None and self._cfg_root is not None:
            from .config import PluginConfig
            self._config = PluginConfig(self._cfg_root, f"plugins.{self.name}")
        return self._config

    @property
    def plugin_dir(self):
        """插件目录（框架加载时注入；手搓实例时为 None）。"""
        return self._plugin_dir

    def database(self, name: str = "", *, path=None, ensure=()):
        """打开（或复用）本插件的 sqlite 数据库。

        数据库文件默认在插件目录下，随插件卸载自动关闭。

        Args:
            name: 逻辑名；同名复用同一个 `Database`。
            path: 显式文件路径（默认 `<plugin_dir>/data/<name>.db`）。
            ensure: 首次打开时要建表的模型类。

        Returns:
            线程本地的 `Database` 实例。"""
        from .store import Database, StoreError
        key = name or "__default__"
        cached = self._databases.get(key)
        if cached is not None:
            return cached
        if path is None:
            if self._plugin_dir is None:
                raise StoreError(
                    f"插件 {self.name} 没有 plugin_dir（手搓实例？）："
                    "请显式传 database(path=...)")
            path = Path(self._plugin_dir) / "data" / (f"{name}.db" if name else f"{self.name}.db")
        db = Database(path)
        if ensure:
            db.register(*ensure)
        self._databases[key] = db
        return db

    def _close_databases(self) -> None:
        """关闭本插件打开的所有库（框架在卸载/重载/加载失败时调用）。"""
        for key, db in list(self._databases.items()):
            try:
                db.close()
            except Exception:  # noqa: BLE001 - 关不掉不该挡住卸载
                self.logger.warning("关闭数据库 %s 失败", key, exc_info=True)
        self._databases.clear()

    def config_template(self) -> dict:
        """本插件的**完整配置模板**（键 → 默认值），框架据此初始化 `config.json`。

        契约（由 `PluginBase.__init__` → `_seed_config()` 执行，所以任何构造路径
        都生效：框架加载、测试直接 new、脚本手动构造）：

        1. **首次运行就把全部配置项写进 `config.json`**，运维打开文件即可看到
           有哪些开关可改，而不是只能翻代码；
        2. **只补缺键，绝不覆盖已有值** —— 插件升级新增配置项时老部署会被自动补上，
           用户改过的值一个都不动；
        3. **嵌套字典逐键补全**（不是「父键存在就整段跳过」）；
        4. **幂等**：没有缺键就不写盘。

        ⚠️ 模板要在**构造期**就能算出来（不要依赖 `on_load` 里才建立的实例状态），
        放**出厂默认值**，不要放运行期算出来的值或敏感凭据；返回的字典会被深拷贝
        后再写入，所以模块级可变默认值（列表/字典）不会与写出去的共享。

        默认空模板 = 这个插件没有可写进文件的配置项。
        """
        return {}

    # ---- 动态管理（stop / pause / resume）----

    def stop_event(self, hid: str) -> bool:
        """移除指定事件监听器。

        ⚠️ 这里曾把 Python 内建函数 `id` 当成参数传下去（`self.event_bus.stop(id)`）——
        没有任何监听器的 id 会等于那个内建对象，所以三个方法**永远是空操作**，
        返回值恒为 False，而文档还照着这份错误签名抄了一遍。
        """
        return self.event_bus.stop(hid)

    def pause_event(self, hid: str) -> bool:
        """暂停指定事件监听器"""
        return self.event_bus.pause(hid)

    def resume_event(self, hid: str) -> bool:
        """恢复指定事件监听器"""
        return self.event_bus.resume(hid)

    def stop_command(self, name: str) -> bool:
        """移除指定命令"""
        h = self.cmds._commands.pop(name, None)
        return h is not None

    def pause_command(self, name: str) -> bool:
        """启用/禁用命令（toggle）。禁用后返回\"命令不存在\"。"""
        h = self.cmds._commands.get(name)
        if h:
            h.disabled = not h.disabled
            return True
        return False

    def stop_timer(self, name: str) -> bool:
        """取消指定定时任务"""
        self.timer.cancel(name)
        return True

    def pause_timer(self, name: str) -> bool:
        """暂停指定定时任务"""
        return self.timer.pause_timer(name) if hasattr(self.timer, "pause_timer") else False

    def resume_timer(self, name: str) -> bool:
        """恢复指定定时任务"""
        return self.timer.resume_timer(name) if hasattr(self.timer, "resume_timer") else False

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ---- 群组绑定 ----

    def bind_group(self, group_id: int) -> bool:
        """绑定群（从插件内部调用 client.groups）"""
        return self._groups.bind(group_id) if self._groups else False

    def unbind_group(self, group_id: int) -> bool:
        return self._groups.unbind(group_id) if self._groups else False

    def is_group_bound(self, group_id: int) -> bool:
        return self._groups.is_bound(group_id) if self._groups else True

    @property
    def bound_groups(self) -> list:
        return self._groups.list_bound() if self._groups else []


class PluginManager:
    """插件管理器 - 加载、卸载、热重载。

    用法:
        mgr = PluginManager(plugin_dir="./plugins", api=api, bus=bus, ...)
        await mgr.load_all()
        await mgr.enable_all()

        # 运行时热重载
        await mgr.reload("my_plugin")
    """

    def __init__(self, plugin_dir: str, api: ApiClient, event_bus: EventBus,
                 timer: TimerManager, cmd_manager: CommandManager,
                 perm_manager: PermissionManager, file_manager: FileManager,
                 logger=None, groups=None, cfg=None, client=None,
                 error_reporter=None):
        self.plugin_dir = Path(plugin_dir)
        self.api = api
        self.event_bus = event_bus
        self.timer = timer
        self.cmd_manager = cmd_manager
        self.perm_manager = perm_manager
        self.file_manager = file_manager
        self.logger = logger or _default_logger()
        self._groups = groups
        self._cfg = cfg
        self._client = client          # 主客户端（注入给插件）
        self._plugins: dict[str, PluginBase] = {}
        #: 技能表：**跨插件**共享 —— 提供方注册，调用方（ai_chat）读取。
        #: 放这里而不是各插件自己一份：提供方不该依赖消费者。
        self.skill_registry = SkillRegistry(logger=self.logger)
        self._watcher_task: Optional[asyncio.Task] = None
        self._file_mtimes: dict[str, float] = {}
        #: 被**显式禁用**过的插件名（运维/命令禁用）。与「还没启用」区分开：
        #: 加载期依赖检查只看这个集合 —— 否则 `load_all()` 里
        #: 「A 已加载但尚未启用」会让依赖 A 的 B 永远加载不了（鸡生蛋）。
        self._disabled: set[str] = set()
        #: 因依赖不满足而**没加载**的插件：{名字: 原因}。`/plugins` 与 watcher 用
        #: 它解释「文件明明在、为什么没加载」，而不是让人去猜。
        self._dep_blocked: dict[str, str] = {}
        #: 依赖图缓存（每次插件集合变化时失效）：拓扑序与反向边都由它算。
        self._dep_cache: Optional[tuple[tuple, dict, dict]] = None
        #: `reload()` 级联保护的开关（避免「重载依赖 → 级联重载使用者」时递归）。
        self._reloading: set[str] = set()
        # 生命周期失败（加载/启用/重载）的受众是超管而不是群：群里只该看到
        # 「出错了 + 编号」，内部故障细节走超管私聊与日志。
        self._errors = error_reporter or ErrorReporter()
        self._errors.set_plugin_resolver(self.plugin_of_handler)

    # ---- 依赖图 ----

    def _deps_of(self, name: str) -> tuple[list[str], list[str]]:
        """某插件声明的 (必需依赖名, 可选依赖名)。

        - 已加载的插件：读实例属性（**包含**插件在 `on_load` 里动态改过的声明）；
        - 尚未加载的插件：读**类属性**（先把模块导入一次、拿到 PluginBase 子类）——
          加载顺序必须在加载之前就能算出来，所以不能只依赖实例。
        - 类都拿不到（导入失败/没有子类）时返回空：那种插件的失败由 `load()`
          自己报，不该让排序一起炸。
        """
        plugin = self._plugins.get(name)
        if plugin is not None:
            try:
                return plugin.dependency_names()
            except DependencyError:
                return [], []
        plugin_class = self._peek_plugin_class(name)
        if plugin_class is None:
            return [], []
        try:
            return plugin_class.dependency_names()
        except DependencyError:
            return [], []

    def _peek_plugin_class(self, name: str):
        """不实例化地看一眼插件模块里的 PluginBase 子类（找不到返回 None）。

        加载顺序必须在**加载之前**算出来，而 `requires` / `optional_requires`
        是类属性 —— 所以这里只能先把模块导入一次。副作用已经清干净：

        - 不调用任何生命周期钩子，不注册命令/事件/定时器；
        - 导入期间挂进 `sys.modules` 的名字（含 `__init__.py` 里
          `from .main import X` 拉起来的子模块）**结束后全部摘掉**，
          正式 `load()` 会重新导入，不会拿到这里的半成品。

        ⚠️ 包目录的 `__init__.py` 用相对导入（`from .main import X`）时必须
        先把父模块挂进 `sys.modules`，否则导入直接失败 → 拿不到声明 →
        依赖检查被静默跳过（踩过：`requires` 形同虚设）。
        """
        pkg_init = self.plugin_dir / name / "__init__.py"
        file_path = pkg_init if pkg_init.exists() else self.plugin_dir / f"{name}.py"
        if not file_path.exists():
            return None
        mod_name = f"napcat_plugin_{name}"
        saved = {key: sys.modules[key] for key in list(sys.modules)
                 if key == mod_name or key.startswith(mod_name + ".")}
        try:
            spec = importlib.util.spec_from_file_location(mod_name, str(file_path))
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - 语法错误/缺依赖都在 load() 里正式报
            return None
        finally:
            added = [key for key in list(sys.modules)
                     if key == mod_name or key.startswith(mod_name + ".")]
            for key in added:
                sys.modules.pop(key, None)
            sys.modules.update(saved)      # 本来就在的（热重载中间态）原样放回
        return resolve_plugin_class(module, name=name, logger=self.logger)

    def _invalidate_deps(self, changed: Optional[str] = None) -> None:
        """插件集合变了 → 依赖图作废；`changed` 相关插件的阻塞记录也作废。

        ⚠️ **不能整张 `_dep_blocked` 一起清**：它是「上次为什么被挡下」的判定
        缓存，也是把原因呈现给运维（`/plugins`、`blocked_reason`）的唯一来源。
        整张清掉的后果：随便哪个插件加载成功，其它「因缺依赖/声明写坏」被挡下的
        插件就从视图里消失，运维再也看不到原因（测试逼出来的）。

        这里只清**与本次变化相关**的名字：刚加载/卸载的那个插件，以及依赖它的
        插件（`_edges()` 用的还是旧图，正好覆盖「依赖刚装好」的场景）。
        于是「依赖装上 → 被挡下的插件下次 load 重新判定并放行」成立，
        而无关插件的诊断信息保留。
        """
        names: set[str] = set()
        if changed is not None:
            names.add(changed)
            try:
                requires_edges, optional_edges = self._edges()
                names |= set(requires_edges.get(changed, ()))
                names |= set(optional_edges.get(changed, ()))
            except Exception:  # noqa: BLE001 - 图算不出来就只清自己那条
                pass
        self._dep_cache = None
        for name in names or list(self._dep_blocked):
            self._dep_blocked.pop(name, None)

    def _edges(self) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        """缓存依赖图的 (requires 边, optional 边)；键是依赖、值是使用者。"""
        names = sorted(self._plugin_names())
        key = tuple(names)
        if self._dep_cache is not None and self._dep_cache[0] == key:
            return self._dep_cache[1], self._dep_cache[2]
        requires_edges, optional_edges = graph(names, self._deps_of)
        self._dep_cache = (key, requires_edges, optional_edges)
        return requires_edges, optional_edges

    def load_order(self) -> list[str]:
        """目录里全部插件的加载顺序：**依赖在前**（必需与可选依赖都参与排序）。

        只依赖必需依赖的边做环检测；可选依赖成环不会让谁加载不了。
        声明成环时抛 `DependencyError` —— 由调用方决定是报错还是退化。
        """
        names = sorted(self._plugin_names())
        return dep_load_order(names, self._deps_of)

    def unload_order(self) -> list[str]:
        """卸载顺序：使用者在前、被依赖的在后（`shutdown()` 用）。"""
        names = sorted(self._plugin_names())
        return dep_unload_order(names, self._deps_of)

    def dependents(self, name: str) -> list[str]:
        """哪些**已加载**的插件依赖 `name`（必需 + 可选，去重、排序）。"""
        requires_edges, optional_edges = self._edges()
        users = set(requires_edges.get(name, ())) | set(optional_edges.get(name, ()))
        return sorted(user for user in users if user in self._plugins)

    def dependencies(self, name: str, *, optional: bool = True) -> list[str]:
        """`name` 声明的依赖名（可选 `optional=False` 只看必需依赖）。"""
        required, optionals = self._deps_of(name)
        return sorted(set(required + (optionals if optional else [])))

    def dependency_target(self, name: str):
        """依赖检查的解析器：返回「可用」的依赖实例，不可用返回 None。

        判定只看两件事：**加载了没有**、**有没有被显式禁用**。
        ⚠️ 不看 `_enabled`：「尚未启用」在 `load_all() → enable_all()` 之间是
        每个插件的正常状态，拿它当不满足会让依赖别人先加载的插件永远进不来。
        真正「必须有启用的依赖」的时机是 `enable()`（它先启用依赖再启用自己）。
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            return None
        if name in self._disabled:
            return _DisabledProxy(plugin)
        return plugin

    def blocked_reason(self, name: str) -> str:
        """这个插件是不是因为依赖不满足才没加载（是则返回原因，否则空串）。"""
        return self._dep_blocked.get(name, "")

    def dependency_verdict(self, name: str):
        """**现在**这个插件的必需依赖满足没有（`DependencyCheck`）。

        与 `blocked_reason()` 的区别：那个是「上次加载时为什么被挡下」的缓存，
        这个是实时的（依赖可能刚装上）。诊断类入口（`/plugins`）用这个，
        watcher 用缓存即可（它只关心「要不要现在重试」）。
        """
        from .deps import check_dependencies as _check

        plugin_class = self._plugins[name].__class__ if name in self._plugins \
            else self._peek_plugin_class(name)
        if plugin_class is None:
            return None                      # 模块本身读不了 → 没有判定可言
        try:
            required, optional = plugin_class.dependency_specs()
        except DependencyError:
            return None                      # 声明写坏了 → 由加载期报错路径说明
        return _check(required, optional, resolve=self.dependency_target,
                      version_of=provided_version, owner=name,
                      check_enabled=False)

    async def load_all(self):
        """加载插件目录下的所有插件（支持 .py 文件和包目录）。

        **按依赖顺序加载**：`store` 必须先于用到它的插件进 `_plugins`，
        否则依赖检查永远说「缺少 store」。顺序由 `load_order()` 算
        （拓扑排序，同层按字典序）；依赖声明成环时退化成字典序并报给超管。
        """
        if not self.plugin_dir.exists():
            self.plugin_dir.mkdir(parents=True, exist_ok=True)
            return

        try:
            order = self.load_order()
        except DependencyError as exc:
            self.logger.error(f"插件依赖声明有问题，按字典序加载：{exc}")
            await self._report_lifecycle(exc, "（依赖图）", "依赖解析")
            order = sorted(self._plugin_names())
        for name in order:
            await self.load(name)

    async def enable_all(self):
        """启用全部已加载插件（按依赖顺序：依赖先启用）。"""
        for name in self._ordered(list(self._plugins.keys())):
            await self.enable(name)

    def _ordered(self, names: list[str]) -> list[str]:
        """把一批插件名按依赖顺序排（成环等异常时退回字典序）。"""
        if not names:
            return []
        try:
            order = self.load_order()
        except DependencyError:
            return sorted(names)
        index = {name: i for i, name in enumerate(order)}
        return sorted(names, key=lambda n: (index.get(n, len(order)), n))

    def plugin_of_handler(self, handler) -> str:
        """这个事件处理器属于哪个插件（按函数对象精确匹配，不做名字前缀猜测）。

        `ErrorReporter` 用它给出错反馈标注归属；返回空串表示不是插件注册的
        （框架内置处理器），此时日志里只有出错位置，不影响反馈本身。
        """
        for name, plugin in self._plugins.items():
            for _event_type, registered in plugin._handlers:
                if registered is handler:
                    return name
        return ""

    def _plugin_files(self, name: str) -> list[Path]:
        """该插件需要监控的全部文件。

        单文件插件 → plugins/<name>.py
        包目录插件 → plugins/<name>/**/*.py

        此前 watch() 只查找 plugins/<name>.py，包目录插件该路径不存在便 continue，
        热重载完全失效；同时 reload() 也未清理子模块，即使重载也拿不到新代码。
        """
        pkg = self.plugin_dir / name
        if (pkg / "__init__.py").exists():
            return sorted(p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts)
        single = self.plugin_dir / f"{name}.py"
        return [single] if single.exists() else []

    # ---- 加载 ----

    async def _check_requires_before_load(self, name: str) -> bool:
        """加载前的依赖闸门：True = 可以继续加载，False = 已挡下（跳过构造）。

        挡下的两种情况：依赖不满足（记 `_dep_blocked` + 私聊超管）、
        依赖声明本身写坏了（同上）。两种都**不抛异常** —— 一个插件的声明写错
        不该让 `load_all()` 半路炸掉，其它插件还得照常起来。

        ⚠️ 返回值必须是 `bool`：这个函数早先返回「实例或 None」并且**两种情况
        都返回 None**，等于闸门从未生效（`load()` 把 None 当成「继续」）——
        「依赖不满足的插件照样加载」这种 bug 靠读代码很难发现，测试才逼出来。
        """
        from .deps import check_dependencies as _check

        plugin_class = self._peek_plugin_class(name)
        if plugin_class is None:
            return True                      # 模块本身有问题 → 走正常失败路径
        try:
            required, optional = plugin_class.dependency_specs()
        except DependencyError as exc:
            self.logger.error(f"插件 {name} 的依赖声明非法：{exc}")
            self._dep_blocked[name] = f"依赖声明非法：{exc}"
            await self._report_lifecycle(
                PluginDependencyError(f"依赖声明非法：{exc}", plugin=name,
                                      cause=exc), name, "依赖检查")
            return False
        if not required and not optional:
            return True
        # 必需依赖必须「已加载、且没被显式禁用」。**这里不要求它已启用** ——
        # 启用依赖是 `enable()` 的职责（它会把依赖链一路启用起来），
        # 而 `load_all()` 阶段大家都还没启用：要求已启用就成了鸡生蛋，
        # 表现是「依赖后加载的插件永远进不来」。
        verdict = _check(required, [],
                         resolve=self.dependency_target,
                         version_of=provided_version, owner=name,
                         check_enabled=False)     # 加载期只要求「在且没被禁用」
        if verdict.ok:
            return True
        reason = verdict.reason()
        first_time = self._dep_blocked.get(name) != reason
        self._dep_blocked[name] = reason
        if not first_time:
            # watcher 每 2 秒重试一次，同一个原因只报一次超管（日志也不重复刷）
            return False
        self.logger.warning(f"插件 {name} 暂不加载：{reason}")
        hint = verdict.hint()
        await self._report_lifecycle(
            PluginDependencyError(
                reason + (f"（{hint}）" if hint else ""),
                plugin=name, missing=[s.name for s in
                                      verdict.missing + verdict.disabled]),
            name, "依赖检查")
        return False

    async def load(self, name: str) -> Optional[PluginBase]:
        """加载单个插件（支持 .py 文件或包目录）。

        **依赖是加载的前置条件**：声明的必需依赖没装或没启用时**不构造插件**，
        原因记进 `_dep_blocked` 并私聊超管。不构造是有意的 —— 一旦构造，
        `config_template()` 就已经写盘、`on_load` 也跑了一半，留一个
        「加载了但用不了」的实例比干脆不加载难查得多。
        """
        if name in self._plugins:
            self.logger.warning(f"插件 {name} 已加载")
            return self._plugins[name]

        # 优先尝试包目录: name/__init__.py
        pkg_init = self.plugin_dir / name / "__init__.py"
        if pkg_init.exists():
            file_path = pkg_init
            mod_name = f"napcat_plugin_{name}"
        else:
            file_path = self.plugin_dir / f"{name}.py"
            if not file_path.exists():
                self.logger.error(f"插件不存在: {self.plugin_dir / name}")
                return None
            mod_name = f"napcat_plugin_{name}"

        # 依赖检查放在实例化**之前**（读类属性，见 PluginBase.dependency_specs）
        if not await self._check_requires_before_load(name):
            return None

        plugin_instance = None
        try:
            # 动态导入
            spec = importlib.util.spec_from_file_location(mod_name, str(file_path))
            if spec is None or spec.loader is None:
                raise ImportError(f"无法加载模块: {name}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)

            # 查找 PluginBase 子类（优先 `__init__.py` 里的 `PLUGIN = 类`）
            plugin_instance = None
            plugin_class = resolve_plugin_class(module, name=name, logger=self.logger)
            if plugin_class is not None:
                instance = plugin_class(
                    api=self.api, event_bus=self.event_bus,
                    timer=self.timer, cmd_manager=self.cmd_manager,
                    skill_registry=self.skill_registry,
                    perm_manager=self.perm_manager,
                    file_manager=self.file_manager, logger=self.logger,
                    groups=self._groups, cfg=self._cfg, client=self._client,
                    error_reporter=self._errors,
                    # 单文件插件（plugins/foo.py）没有自己的目录 → 用 plugins/
                    plugin_dir=((self.plugin_dir / name)
                                if (self.plugin_dir / name).is_dir() else self.plugin_dir),
                )
                if not instance.name:
                    instance.name = name
                # 管理器引用：`self.deps` / 依赖自查用它拿「谁依赖谁」，
                # 比绕 `self.client.plugins` 可靠（客户端可能还没就绪、
                # 或是脚本/测试里手搓的宿主对象）。
                instance._manager = self
                plugin_instance = instance

            if plugin_instance is None:
                self.logger.warning(f"插件 {name} 未找到 PluginBase 子类")
                await self._report_lifecycle(
                    PluginLoadError(
                        "模块里没有 PluginBase 子类"
                        "（拆分写法：在 __init__.py 里 `PLUGIN = 你的插件类`）",
                        plugin=name),
                    name, "加载")
                return None

            # 配置初始化在构造期已经完成（见 PluginBase._seed_config）——
            # 任何构造路径都拿得到完整配置，插件在自己的 on_load 里也能直接用。
            await self._report_config_seed(name, plugin_instance)
            await plugin_instance.on_load()
            plugin_instance._loaded = True
            self._plugins[name] = plugin_instance
            self._dep_blocked.pop(name, None)
            self._invalidate_deps(name)
            self._file_mtimes[name] = max(
                (p.stat().st_mtime for p in self._plugin_files(name)), default=0.0)
            self.logger.info(f"插件 {name} v{plugin_instance.version} 加载成功")
            return plugin_instance

        except Exception as exc:
            self.logger.error(f"加载插件 {name} 失败:\n{traceback.format_exc()}")
            # ⚠️ 清掉半成品模块：exec_module 失败前已经把自己塞进 sys.modules，
            # 留着会让下一次加载（以及热重载回滚）导入到坏模块。on_load 里注册过的
            # 命令/事件/定时器也一并撤销，避免同一命令被重复注册、
            # 或者一个永远没人取消的定时任务留在后台。
            self._discard_partial(name, mod_name, plugin_instance)
            await self._report_lifecycle(exc, name, "加载")
            return None

    def _discard_partial(self, name: str, mod_name: str, instance=None) -> None:
        """加载失败后清理痕迹：半成品模块 + 它已注册的命令/事件/定时器。

        ⚠️ 三样都要清。此前只 pop 了模块并按「命令名以插件名开头」猜着注销命令，
        于是 `on_load` 里注册过的事件与定时任务会**永久留在框架里**
        （实测：加载失败的插件仍在收发事件、定时器照跑），而命令名与插件名不同的
        插件（`essential` → `/ban`、`/kick`）一条也回收不了。
        归属现在来自注册时打的标签（`CommandHandler.plugin` / 实例的 `_handlers`、
        `_timers`），不靠命名约定猜。
        """
        for key in [k for k in sys.modules
                    if k == mod_name or k.startswith(mod_name + ".")]:
            sys.modules.pop(key, None)
        try:
            self.skill_registry.unregister_plugin(name)
            self.cmd_manager.unregister_plugin(name)
        except Exception:  # noqa: BLE001 - 回收失败不该掩盖加载失败的原始异常
            self.logger.warning("回收插件 %s 的命令失败", name, exc_info=True)
        if instance is not None:
            instance._off_own_handlers()
            instance._off_own_timers()
            instance._close_databases()
        self._invalidate_deps(name)

    async def _report_config_seed(self, name: str, plugin: "PluginBase") -> None:
        """配置初始化**已经**在构造期做完（见 `PluginBase._seed_config`）。

        管理器这里只补一件事：把「模板写错 / 配置写不进去」这类需要运维知道的
        情况报出去（构造期是同步的，没有投递通道）。
        """
        error = getattr(plugin, "config_seed_error", None)
        if error is None:
            return
        await self._report_lifecycle(
            PluginConfigError(f"配置初始化失败：{short_reason(error)}",
                              plugin=name, cause=error), name, "配置初始化")

    async def _report_lifecycle(self, exc: BaseException, name: str, phase: str) -> None:
        """生命周期失败 → 完整日志 + 私聊超管（`audience="operator"`）。

        群里看不到这类细节：插件没装起来是运维问题，不是提问者的错。
        """
        if isinstance(exc, PluginError):
            error = exc
            error.plugin = error.plugin or name
        else:
            cls = PluginLoadError if phase in ("加载", "重载") else PluginLifecycleError
            error = cls(short_reason(exc), plugin=name, cause=exc)
        await self._errors.notify_operators(error, where=f"插件 {name} {phase}")

    # ---- 启用/禁用 ----

    async def enable(self, name: str) -> bool:
        """启用插件。**先启用它的必需依赖**（递归，环由加载期挡掉）。

        依赖没加载时直接返回 False：那种插件本来就不该被加载（`load()` 的
        依赖闸门会拦住），走到这里说明有人手工 `unload` 了依赖。
        """
        plugin = self._plugins.get(name)
        if not plugin:
            return False
        if plugin._enabled:
            return True
        ok, auto = await self.ensure_requires(name)
        if auto:
            self.logger.info(f"为启用 {name}，先启用了依赖：{'、'.join(auto)}")
        if not ok:
            blocked = [d for d in self.dependencies(name)
                       if d not in self._plugins or d in self._disabled]
            detail = f"（{'、'.join(blocked)}）" if blocked else ""
            self.logger.warning(
                f"插件 {name} 的依赖未就绪{detail}，跳过启用"
                + ("；被显式禁用的依赖需要先自己启用（/plugins 看状态）"
                   if any(d in self._disabled for d in blocked) else ""))
            return False
        self._disabled.discard(name)
        try:
            await plugin.on_enable()
            plugin._enabled = True
            # 记下「本次启用注册到哪」：on_disable 只撤销这一段
            plugin._handlers_base = len(plugin._handlers)
            self.logger.info(f"插件 {name} 已启用")
            return True
        except Exception as exc:
            self.logger.error(f"启用插件 {name} 失败:\n{traceback.format_exc()}")
            # ⚠️ 启用失败必须**回滚这次已经注册的东西**：`on_enable` 是逐条注册的，
            # 抛异常时前面几条已经生效。以前就这么留着（`_enabled` 仍是 False），
            # 而 `disable()` 见到「未启用」直接提前返回、连 `on_disable` 都不会调用，
            # 于是这些处理器永久泄漏：插件明明没启用，事件却照收（再启用一次还会叠加）。
            plugin._off_own_handlers()
            plugin._off_own_timers()
            try:
                # 技能跟命令一样绑在这个实例上：实例要被丢弃了，它注册的技能
                # 也得撤掉 —— 否则模型会调用到一个已经死掉的 handler
                self.skill_registry.unregister_plugin(name)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.cmd_manager.unregister_plugin(name)
            except Exception:  # noqa: BLE001 - 回收失败不掩盖启用失败的原始异常
                self.logger.warning("回收插件 %s 的命令失败", name, exc_info=True)
            plugin._enabled = False
            plugin._handlers_base = 0
            await self._report_lifecycle(exc, name, "启用")
            return False

    async def ensure_requires(self, name: str) -> tuple[bool, list[str]]:
        """确保 `name` 的必需依赖都已**加载并启用**，返回 (是否就绪, 被顺带启用的)。

        递归：依赖自己还有依赖时会一路往上启用（环在加载期就报错了，
        这里再带一个 `seen` 集合兜底，防止声明被运行期篡改后死循环）。
        """
        auto: list[str] = []
        seen: set[str] = set()

        async def ensure(target: str) -> bool:
            if target in seen:
                return True                    # 环兜底：已经处理过就不再往下钻
            seen.add(target)
            plugin = self._plugins.get(target)
            if plugin is None:
                return False                   # 没加载 → 依赖不满足
            try:
                required, _optional = plugin.dependency_names()
            except DependencyError:
                required = []
            for dep in required:
                if not await ensure(dep):
                    return False               # 依赖的依赖不满足 → 整条链不满足
            if plugin._enabled:
                return True
            if await self._enable_direct(target):
                auto.append(target)
                return True
            return False                       # 启用失败（已报超管）

        plugin = self._plugins.get(name)
        if plugin is None:
            return False, []
        try:
            required, _optional = plugin.dependency_names()
        except DependencyError:
            required = []
        ready = True
        for dep in required:
            if not await ensure(dep):
                ready = False
        return ready, auto

    async def _enable_direct(self, name: str) -> bool:
        """只跑 `on_enable`（依赖已由 `ensure_requires` 处理过，避免重复递归）。

        **被显式禁用的插件不会被顺带启用**：`disable()` 是运维/命令的明确决定，
        不该因为另一个插件要启动就被悄悄推翻。这种「依赖被禁用」的情况会如实
        返回 False，由调用方说明原因（`/plugins` 里能看到它是 ✗）。
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            return False
        if plugin._enabled:
            return True
        if name in self._disabled:
            self.logger.warning(f"插件 {name} 已被显式禁用，不自动启用它")
            return False
        self._disabled.discard(name)
        try:
            await plugin.on_enable()
            plugin._enabled = True
            plugin._handlers_base = len(plugin._handlers)
            self.logger.info(f"插件 {name} 已启用")
            return True
        except Exception as exc:
            self.logger.error(f"启用插件 {name} 失败:\n{traceback.format_exc()}")
            plugin._off_own_handlers()
            plugin._off_own_timers()
            try:
                # 技能跟命令一样绑在这个实例上：实例要被丢弃了，它注册的技能
                # 也得撤掉 —— 否则模型会调用到一个已经死掉的 handler
                self.skill_registry.unregister_plugin(name)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.cmd_manager.unregister_plugin(name)
            except Exception:  # noqa: BLE001
                self.logger.warning("回收插件 %s 的命令失败", name, exc_info=True)
            plugin._enabled = False
            plugin._handlers_base = 0
            await self._report_lifecycle(exc, name, "启用")
            return False

    async def dependents_block(self, name: str, *, action: str,
                               what: str = "") -> list[str]:
        """`name` 被别的**已启用**插件依赖时，记一条日志并返回这些使用者的名字。

        返回空列表 = 可以继续。调用方据此决定「拒绝」还是「级联」。
        """
        users = [u for u in self.dependents(name)
                 if self._plugins.get(u) is not None
                 and getattr(self._plugins[u], "_enabled", False)]
        if not users:
            return []
        subject = what or name
        self.logger.warning(
            f"插件 {name} 不能{action}：{ '、'.join(users) } 正在依赖它")
        return users

    async def disable(self, name: str, *, force: bool = False) -> bool:
        """禁用插件。

        **默认不拆别人的台**：还有已启用的插件依赖它时拒绝（`force=True` 才级联
        禁用那些使用者，顺序是「使用者在前」）。理由很实在 —— 把 store 停掉之后
        用它的插件还在收事件、调 `self.deps.get("store")` 拿到 None，
        表现是「某个功能莫名不响应」，比直接拒绝难查得多。
        """
        plugin = self._plugins.get(name)
        if not plugin:
            return False
        if not plugin._enabled:
            return True
        users = await self.dependents_block(name, action="禁用")
        if users and not force:
            return False
        if users:
            for user in await self._ordered_for_unload(users):
                await self.disable(user, force=True)
        self._disabled.add(name)
        try:
            await plugin.on_disable()
            plugin._handlers_base = len(plugin._handlers)
            plugin._enabled = False
            self.logger.info(f"插件 {name} 已禁用")
            return True
        except Exception as exc:
            self.logger.error(f"禁用插件 {name} 失败:\n{traceback.format_exc()}")
            await self._report_lifecycle(exc, name, "禁用")
            return False

    # ---- 卸载 ----

    async def _ordered_for_unload(self, names: list[str]) -> list[str]:
        """把一批插件按「使用者在前、被依赖的在后」排序（卸载顺序）。"""
        if not names:
            return []
        try:
            order = self.unload_order()
        except DependencyError:
            return sorted(names)
        index = {name: i for i, name in enumerate(order)}
        return sorted(names, key=lambda n: (index.get(n, len(order)), n))

    def _purge_modules(self, name: str) -> int:
        """把该插件的整棵模块子树从 `sys.modules` 摘掉，返回摘掉的条数。

        `unload()` 与 `reload()` 共用同一份清理：以前 `unload()` 只 pop
        父模块 `napcat_plugin_<name>`，子模块 `.main` / `.store` 留在里面，
        于是「unload 之后再 load」拿到的是**旧子模块代码**（改文件不生效），
        而热重载路径因为自己另写了一遍清理反而没事 —— 同一件事两个写法，
        迟早有一个漏。
        """
        mod_name = f"napcat_plugin_{name}"
        removed = 0
        for key in [k for k in sys.modules
                    if k == mod_name or k.startswith(mod_name + ".")]:
            sys.modules.pop(key, None)
            removed += 1
        return removed

    async def unload(self, name: str, *, force: bool = False) -> bool:
        """卸载插件。

        **默认不拆别人的台**：还有已启用插件依赖它时拒绝；`force=True` 先按
        「使用者在前」的顺序卸载那些使用者，再卸载它自己。
        `shutdown()` 用 force 走这条路。
        """
        plugin = self._plugins.get(name)
        if not plugin:
            return False
        users = await self.dependents_block(name, action="卸载")
        if users and not force:
            return False
        for user in await self._ordered_for_unload(users):
            await self.unload(user, force=True)
        plugin = self._plugins.pop(name, None)
        if not plugin:
            return False
        self._dep_blocked.pop(name, None)
        self._disabled.discard(name)
        # 卸载后「依赖它的插件」需要重新判定（依赖没了 → 该被挡下）
        self._invalidate_deps(name)
        try:
            if plugin._enabled:
                await plugin.on_disable()
                plugin._enabled = False
            # 离场实例必须与事件总线彻底解绑：它的处理器可能是**任何**历史状态下
            # 注册的（例如热重载回滚过几次），只依赖 on_disable 的增量撤销不够。
            # 不解绑的后果很直观：`/events` 里同一个插件出现两对监听器，
            # 每条群消息被处理两次。
            plugin._off_own_handlers()
            # 定时任务与命令同理：实例都要走了，它的任务不该继续跑、它的命令
            # 也不该继续能被调用（旧命令绑的是这个死实例，调用等于执行旧代码）。
            plugin._off_own_timers()
            try:
                # 技能跟命令一样绑在这个实例上：实例要被丢弃了，它注册的技能
                # 也得撤掉 —— 否则模型会调用到一个已经死掉的 handler
                self.skill_registry.unregister_plugin(name)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.cmd_manager.unregister_plugin(name)
            except Exception:  # noqa: BLE001
                self.logger.warning("回收插件 %s 的命令失败", name, exc_info=True)
            await plugin.on_unload()
            plugin._close_databases()
            # 移除整棵模块子树
            self._purge_modules(name)
            # ⚠️ 这里**不能** pop `_file_mtimes`：那是热重载监控的元数据，
            # 不是插件的运行时状态。reload() 依赖它判断「文件是否又变了」，
            # 删掉之后 watcher 会把它当成「从未加载过的插件」而跳过 ——
            # 表现为「回滚一次以后，再怎么改文件都不会自动重载」。
            self.logger.info(f"插件 {name} 已卸载")
            return True
        except Exception as exc:
            self.logger.error(f"卸载插件 {name} 失败:\n{traceback.format_exc()}")
            await self._report_lifecycle(exc, name, "卸载")
            return False

    # ---- 热重载 ----

    async def reload(self, name: str) -> bool:
        """热重载：禁用 → 卸载 Module → 重新加载 → 启用。

        **失败不会让插件消失**：加载不成功时恢复原来那份实例（重新走 on_load/on_enable，
        把命令/事件/定时器注册回来），等文件下次变更再重试。

        此前 reload 先把插件从 `_plugins` 摘掉再加载，一旦新代码导入失败（语法错误、
        依赖缺失、ImportError…）插件就**永久消失**：watcher 只遍历 `_plugins`，
        再也不会碰它，旧版本也已经被打死 —— 只能重启服务。

        **依赖级联**：重载 `store` 之后，那些 `self.deps.get("store")` 里握着旧实例
        的插件必须一起重载（否则它们看到的是被 `_off_own_handlers` 解绑过的死对象）。
        级联集合在卸载**之前**快照（依赖关系可能随新代码变化），并且只在目标重载
        成功时才级联 —— 目标回滚到旧版本时，使用者本来就是对的。
        """
        self.logger.info(f"热重载插件: {name}")
        previous = self._plugins.get(name)
        was_enabled = bool(previous and previous._enabled)
        # 快照：谁依赖了它（用**旧**代码的声明，因为新代码还没加载）
        cascade = [u for u in self.dependents(name) if u != name]
        cascade_enabled = {u for u in cascade
                           if getattr(self._plugins.get(u), "_enabled", False)}

        if was_enabled:
            await self.disable(name, force=True)
        await self.unload(name, force=True)
        # 清理整棵模块子树：包目录插件的子模块（.main/.portal/...）此前残留
        # 在 sys.modules 中，导致热重载后仍执行旧代码。
        self._purge_modules(name)
        # 清掉字节码缓存：.pyc 的校验只比对「源文件 mtime 的**整秒** + 文件大小」，
        # 同一秒内改写且大小相同（改个常量、换个字符串）时会被判为有效，
        # 于是重新导入拿到的还是**旧字节码** —— 表现为「改了代码但热重载没生效」。
        self._clear_bytecode_cache(name)

        plugin = await self.load(name)
        if plugin is not None:
            ok = True
            if was_enabled:
                # 加载成功 ≠ 能用：`on_enable` 失败时插件是「已加载但没启用」，
                # 这时报「重载成功」会让 `/reload` 与 watcher 都以为没事了
                # （watcher 会刷新 mtime，于是这份代码再也不会被重试）。
                ok = await self.enable(name)
            if cascade:
                await self._reload_dependents(cascade, was_enabled=cascade_enabled)
            return ok

        # 依赖不满足 → **不回滚**：旧实例活着也服务不了（它的依赖不在/被禁用），
        # 留着只会让 `/plugins` 显示一个「加载了但永远起不来」的僵尸。
        # 与「新代码导入失败」区分开：那种回滚是有意义的（旧代码还能干活）。
        if self._dep_blocked.get(name):
            self.logger.warning(
                f"插件 {name} 因依赖未满足不加载：{self._dep_blocked[name]}"
                f"（依赖就绪后会自动加载）")
            return False

        # 新版本加载失败 → 回滚到原来那份实例（代码还留在内存里，仍可服务）
        if previous is not None:
            try:
                await previous.on_load()
                previous._loaded = True
                self._plugins[name] = previous
                if was_enabled:
                    # 回滚时旧实例的处理器已在 disable() 里撤销，这里重新注册；
                    # on_enable 是幂等注册（同名 hid 会新增一条），所以不能重复走到这里。
                    await previous.on_enable()
                    previous._handlers_base = len(previous._handlers)
                    previous._enabled = True
                # 关键：不更新 _file_mtimes —— 文件 mtime 仍大于记录值，
                # watcher 会在下一次文件变更时重试加载新版本。
                self.logger.warning(
                    f"插件 {name} 新版本加载失败，已回滚到修改前的版本"
                    f"（修正后保存文件会自动重试）")
                # 级联前被禁用的使用者：依赖已经回到旧实例，把它们恢复回去
                for user in self._ordered(cascade):
                    if user in cascade_enabled and not getattr(
                            self._plugins.get(user), "_enabled", False):
                        await self.enable(user)
            except Exception as exc:
                self.logger.error(
                    f"插件 {name} 回滚旧版本也失败:\n{traceback.format_exc()}")
                await self._report_lifecycle(exc, name, "回滚")
        return False

    def _clear_bytecode_cache(self, name: str) -> None:
        """删除该插件的 `__pycache__`，避免加载到过期的 .pyc。

        详见 `reload()` 里的说明：.pyc 有效期只由「整秒 mtime + 文件大小」决定，
        热重载场景下这个判据太弱。
        """
        pkg = self.plugin_dir / name
        caches = []
        if pkg.is_dir():
            caches.append(pkg / "__pycache__")
        else:
            caches.append(self.plugin_dir / "__pycache__")
        for cache in caches:
            if not cache.is_dir():
                continue
            for pyc in cache.glob("*.pyc"):
                # 只删本插件的 .pyc：单文件插件的 __pycache__ 与其它插件共用
                stem = pyc.name.split(".", 1)[0]
                if pkg.is_dir() or stem == name:
                    try:
                        pyc.unlink()
                    except OSError:
                        pass

    async def _reload_dependents(self, names: list[str], *,
                                 was_enabled: set[str]) -> None:
        """重载一批「依赖了刚被重载的插件」的使用者（依赖在前，逐个来）。"""
        self.logger.info(f"依赖变更，级联重载：{'、'.join(names)}")
        for user in self._ordered(names):
            if user not in self._plugins:
                continue
            was_on = user in was_enabled
            ok = await self.reload(user)
            if was_on and not ok:
                self.logger.error(
                    f"级联重载 {user} 失败：它依赖的插件刚更新过，"
                    f"请修正后 /reload {user}")
            elif was_on and not getattr(self._plugins.get(user), "_enabled", False):
                await self.enable(user)

    async def reload_all(self):
        for name in self._ordered(list(self._plugins.keys())):
            await self.reload(name)

    # ---- 文件监控热重载 ----

    async def watch(self, interval: float = 2.0):
        """启动文件变更监控，自动热重载修改的插件。

        ⚠️ 必须是 **async**：框架里的调用方一直是 `await self.plugins.watch(...)`
        （napcat/client.py），同步函数返回 None 会直接抛
        `TypeError: 'NoneType' object can't be awaited`。
        """
        if self._watcher_task is not None and not self._watcher_task.done():
            return                      # 已在监控，重复调用不叠加任务
        self._watcher_task = asyncio.create_task(self._watch_loop(interval))

    def _plugin_names(self) -> list[str]:
        """插件目录里所有**可加载**的插件名（不依赖当前是否加载成功）。"""
        names = []
        if not self.plugin_dir.exists():
            return names
        for path in sorted(self.plugin_dir.iterdir()):
            if path.name.startswith("_"):
                continue
            if path.is_dir() and (path / "__init__.py").exists():
                names.append(path.name)
            elif path.suffix == ".py":
                names.append(path.stem)
        return names

    async def _watch_loop(self, interval: float):
        while True:
            await asyncio.sleep(interval)
            # 遍历目录里的**全部**插件，而不是只遍历 self._plugins：
            # 加载失败（或回滚失败）的插件不在里面，但文件修好后应该能被自动拉起。
            # 顺序取依赖拓扑序：**依赖先试** —— 否则依赖后加载的插件这一轮必然
            # 报「缺少依赖」，要等下一轮（2 秒后）才成功。
            try:
                names = self.load_order()
            except DependencyError as exc:
                self.logger.warning(f"依赖声明有问题，watcher 用字典序：{exc}")
                names = self._plugin_names()
            for name in names:
                files = self._plugin_files(name)
                if not files:
                    continue
                try:
                    mtime = max(p.stat().st_mtime for p in files)
                except OSError:
                    continue
                known = self._file_mtimes.get(name)
                if known is None:
                    if name in self._plugins:
                        continue        # 已加载但无 mtime 记录（异常路径），不误触发
                    # 从未成功加载过：试一次，失败就安静等待下次文件变更。
                    # 因依赖不满足被挡住的不再刷日志：`load()` 只在**第一次**挡下时
                    # 报超管（见 `_dep_blocked`），这里保持安静。
                    if self._dep_blocked.get(name):
                        continue
                    self.logger.info(f"检测到尚未加载的插件 {name}，尝试加载...")
                    loaded = await self.load(name)
                    if loaded is not None:
                        await self.enable(name)
                    self._file_mtimes[name] = mtime
                    continue
                if mtime > known:
                    self.logger.info(f"检测到插件 {name} 文件变更，自动热重载...")
                    ok = await self.reload(name)
                    if ok:
                        # 加载成功：以当前 mtime 为准，避免同一份代码反复重载
                        self._file_mtimes[name] = max(
                            self._file_mtimes.get(name, 0.0), mtime)
                    else:
                        # 失败：记下 mtime 等下次变更重试（不刷日志、不重复加载）
                        self._file_mtimes[name] = mtime

    def stop_watch(self):
        if self._watcher_task:
            self._watcher_task.cancel()
            self._watcher_task = None

    # ---- 查询 ----

    @property
    def plugins(self) -> dict[str, PluginBase]:
        return dict(self._plugins)

    def get(self, name: str) -> Optional[PluginBase]:
        return self._plugins.get(name)

    async def shutdown(self):
        """关闭所有插件（**按卸载顺序**：使用者先走，被依赖的后走）。"""
        self.stop_watch()
        try:
            order = self.unload_order()
        except DependencyError:
            order = sorted(self._plugins.keys(), reverse=True)
        loaded = set(self._plugins)
        for name in order:
            if name in loaded:
                await self.unload(name, force=True)


def _default_logger():
    import logging
    logger = logging.getLogger("napcat.plugin")
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
    return logger
