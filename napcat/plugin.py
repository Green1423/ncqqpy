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
from pathlib import Path
from typing import Optional, Type

from .api import ApiClient
from .config import ConfigFill
from .events import EventBus, TimerManager
from .command import CommandManager
from .permission import PermissionManager
from .file_manager import FileManager
from .models import MessageEvent, NapCatEvent
from .errors import (
    ErrorReporter, NapCatError, PluginConfigError, PluginError,
    PluginLifecycleError, PluginLoadError, PluginRuntimeError, short_reason,
)


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

    def __init__(self, api: ApiClient, event_bus: EventBus, timer: TimerManager,
                 cmd_manager: CommandManager, perm_manager: PermissionManager,
                 file_manager: FileManager, logger, groups=None, cfg=None,
                 client=None, error_reporter=None):
        self.api = api
        self.event_bus = event_bus
        self.timer = timer
        self.cmds = cmd_manager
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
        """
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
        """快捷注册消息事件处理器"""
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
        """注册命令（自动打上本插件的归属标签，回收/定位都靠它）。"""
        kwargs.setdefault("plugin", self.name)
        return self.cmds.register(name, **kwargs)

    def unregister_command(self, name: str) -> bool:
        """取消注册命令"""
        return self.cmds.unregister(name)

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
        """注册周期定时任务（名字记在实例上，卸载时框架负责回收）。"""
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
        self._watcher_task: Optional[asyncio.Task] = None
        self._file_mtimes: dict[str, float] = {}
        # 生命周期失败（加载/启用/重载）的受众是超管而不是群：群里只该看到
        # 「出错了 + 编号」，内部故障细节走超管私聊与日志。
        self._errors = error_reporter or ErrorReporter()
        self._errors.set_plugin_resolver(self.plugin_of_handler)

    async def load_all(self):
        """加载插件目录下的所有插件（支持 .py 文件和包目录）"""
        if not self.plugin_dir.exists():
            self.plugin_dir.mkdir(parents=True, exist_ok=True)
            return

        for path in sorted(self.plugin_dir.iterdir()):
            if path.name.startswith("_"):
                continue
            # 包目录: name/__init__.py
            if path.is_dir() and (path / "__init__.py").exists():
                await self.load(path.name)
            # 单文件: name.py
            elif path.suffix == ".py":
                await self.load(path.stem)

    async def enable_all(self):
        for name in list(self._plugins.keys()):
            await self.enable(name)

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

    async def load(self, name: str) -> Optional[PluginBase]:
        """加载单个插件（支持 .py 文件或包目录）"""
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

        plugin_instance = None
        try:
            # 动态导入
            spec = importlib.util.spec_from_file_location(mod_name, str(file_path))
            if spec is None or spec.loader is None:
                raise ImportError(f"无法加载模块: {name}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)

            # 查找 PluginBase 子类
            plugin_instance = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (inspect.isclass(attr) and issubclass(attr, PluginBase)
                        and attr is not PluginBase):
                    instance = attr(
                        api=self.api, event_bus=self.event_bus,
                        timer=self.timer, cmd_manager=self.cmd_manager,
                        perm_manager=self.perm_manager,
                        file_manager=self.file_manager, logger=self.logger,
                        groups=self._groups, cfg=self._cfg, client=self._client,
                        error_reporter=self._errors,
                    )
                    if not instance.name:
                        instance.name = name
                    plugin_instance = instance
                    break

            if plugin_instance is None:
                self.logger.warning(f"插件 {name} 未找到 PluginBase 子类")
                await self._report_lifecycle(
                    PluginLoadError(f"模块里没有 PluginBase 子类", plugin=name),
                    name, "加载")
                return None

            # 配置初始化在构造期已经完成（见 PluginBase._seed_config）——
            # 任何构造路径都拿得到完整配置，插件在自己的 on_load 里也能直接用。
            await self._report_config_seed(name, plugin_instance)
            await plugin_instance.on_load()
            plugin_instance._loaded = True
            self._plugins[name] = plugin_instance
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
            self.cmd_manager.unregister_plugin(name)
        except Exception:  # noqa: BLE001 - 回收失败不该掩盖加载失败的原始异常
            self.logger.warning("回收插件 %s 的命令失败", name, exc_info=True)
        if instance is not None:
            instance._off_own_handlers()
            instance._off_own_timers()

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
        plugin = self._plugins.get(name)
        if not plugin:
            return False
        if plugin._enabled:
            return True
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
                self.cmd_manager.unregister_plugin(name)
            except Exception:  # noqa: BLE001 - 回收失败不掩盖启用失败的原始异常
                self.logger.warning("回收插件 %s 的命令失败", name, exc_info=True)
            plugin._enabled = False
            plugin._handlers_base = 0
            await self._report_lifecycle(exc, name, "启用")
            return False

    async def disable(self, name: str) -> bool:
        plugin = self._plugins.get(name)
        if not plugin:
            return False
        if not plugin._enabled:
            return True
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

    async def unload(self, name: str) -> bool:
        plugin = self._plugins.pop(name, None)
        if not plugin:
            return False
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
                self.cmd_manager.unregister_plugin(name)
            except Exception:  # noqa: BLE001
                self.logger.warning("回收插件 %s 的命令失败", name, exc_info=True)
            await plugin.on_unload()
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
        """
        self.logger.info(f"热重载插件: {name}")
        previous = self._plugins.get(name)
        was_enabled = bool(previous and previous._enabled)

        if was_enabled:
            await self.disable(name)
        await self.unload(name)
        # 清理整棵模块子树：包目录插件的子模块（.main/.portal/...）此前残留
        # 在 sys.modules 中，导致热重载后仍执行旧代码。
        self._purge_modules(name)
        # 清掉字节码缓存：.pyc 的校验只比对「源文件 mtime 的**整秒** + 文件大小」，
        # 同一秒内改写且大小相同（改个常量、换个字符串）时会被判为有效，
        # 于是重新导入拿到的还是**旧字节码** —— 表现为「改了代码但热重载没生效」。
        self._clear_bytecode_cache(name)

        plugin = await self.load(name)
        if plugin is not None:
            if was_enabled:
                # 加载成功 ≠ 能用：`on_enable` 失败时插件是「已加载但没启用」，
                # 这时报「重载成功」会让 `/reload` 与 watcher 都以为没事了
                # （watcher 会刷新 mtime，于是这份代码再也不会被重试）。
                return await self.enable(name)
            return True

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

    async def reload_all(self):
        for name in list(self._plugins.keys()):
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
            for name in self._plugin_names():
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
                    # 从未成功加载过：试一次，失败就安静等待下次文件变更
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
        """关闭所有插件"""
        self.stop_watch()
        for name in list(self._plugins.keys()):
            await self.unload(name)


def _default_logger():
    import logging
    logger = logging.getLogger("napcat.plugin")
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
    return logger
