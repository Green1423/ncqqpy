"""框架错误类型与统一错误反馈。

这个模块是**错误分类与反馈策略**的唯一业主：

- 什么算「可以给用户看」的失败（`NapCatError.user_message` / `show_reason`）；
- 谁该收到这条失败（触发者 / 超管：`audience`）；
- 文案长什么样（分类 + 短原因 + 错误编号）；
- 同一类失败多久内不重复刷（节流）。

投递本身**不是**本模块的责任：发到群还是私聊、要不要 @ 人，由注入的
`send` / `send_private` 回调完成（线上是 `NapCatClient._send_error_reply`
与 `_send_error_private`）。插件不该自己拼错误文案 —— 自己拼就没有编号、
没有节流、群里也不会 @ 触发的人，同一类失败会以 N 种写法出现在 N 个地方。

用法（插件侧）::

    from napcat.errors import PluginTimeoutError, PluginConfigError

    class MyPlugin(PluginBase):
        async def on_enable(self):
            @self.register_command("q", permission="admin")
            async def cmd_q(packet):
                if not self.config.get("api_key"):
                    # 用户看得懂、且知道该做什么 → 直接把这句话回给他
                    raise PluginConfigError("没配 api_key，找超管在 config.json 里补")
                try:
                    return await self._ask_upstream()
                except TimeoutError as e:
                    # 分类明确的上游失败：文案自带「稍后再试」，编号进日志
                    raise PluginTimeoutError(cause=e, plugin=self.name) from e

框架层（命令执行、事件分发、定时任务、插件生命周期）也会把未预期的异常
包成这套类型后由 `ErrorReporter` 统一反馈，插件不写 try/except 也不会再出现
「有问不答」。
"""
from __future__ import annotations

import hashlib
import logging
import time
import traceback
from typing import Any, Awaitable, Callable, Optional

# ==================== 错误类型 ====================


class NapCatError(Exception):
    """框架错误基类 —— 子类用三个类属性决定框架怎么处理。

    | 属性 | 作用 |
    |------|------|
    | `kind` | 文案里的分类名（如「上游超时」）|
    | `audience` | `"trigger"` 回给触发者；`"operator"` 只给超管（群里不暴露内部故障）|
    | `retryable` | 文案末尾补一句「可以稍后再试」|

    实例属性 `user_message` 非空时，框架**只**回这一句（不再附异常原文）；
    为空时按 `show_reason` 决定要不要附短原因。
    """

    kind = "内部错误"
    audience = "trigger"
    retryable = False
    #: 可展示给用户的一句话；None = 由 `ErrorReporter` 按类型推导
    user_message: Optional[str] = None

    def __init__(self, message: str = "", *, user_message: Optional[str] = None,
                 cause: Optional[BaseException] = None):
        super().__init__(message or user_message or self.kind)
        if user_message is not None:
            self.user_message = user_message
        #: 原始异常（分类包装时保留，便于日志里的根因排查）
        self.cause = cause


class PluginError(NapCatError):
    """插件相关错误基类。"""

    kind = "插件错误"

    def __init__(self, message: str = "", *, plugin: str = "",
                 user_message: Optional[str] = None,
                 cause: Optional[BaseException] = None):
        super().__init__(message, user_message=user_message, cause=cause)
        self.plugin = plugin


class PluginRuntimeError(PluginError):
    """插件运行期未预期失败（命令/事件处理器里抛出的普通异常）。

    框架把**任何**未被插件自行分类的异常都归到这里，因此它的存在本身就是
    「这里有个 bug」的信号：日志里有完整堆栈，用户侧拿到编号可以报给超管。
    """

    kind = "运行错误"


class PluginConfigError(PluginError):
    """插件配置缺失或非法。用户看得懂，文案应当说清「该改哪个键」。"""

    kind = "配置错误"


class PluginExternalError(PluginError):
    """插件依赖的外部服务失败（网络、上游 API、第三方进程）。"""

    kind = "上游错误"
    retryable = True


class PluginTimeoutError(PluginExternalError):
    """上游超时 —— 重试常常就好了。"""

    kind = "上游超时"


class PluginRateLimitError(PluginExternalError):
    """被上游限流 / 额度用尽。"""

    kind = "被限流"


class PluginAccessError(PluginError):
    """权限或服务范围不足。

    ⚠️ 默认**不建议**用它回绝用户：本框架的口径是「没有权限 = 命令不存在」
    （不暴露命令的存在）。它存在的意义是给「用户已经知道这个功能、只是当前
    用不了」的场景（例如服务范围白名单）一个统一的说法。
    """

    kind = "无权访问"


class PluginLoadError(PluginError):
    """插件装载失败：导入/发现/实例化阶段（含热重载回滚失败）。"""

    kind = "插件装载失败"
    audience = "operator"


class PluginLifecycleError(PluginError):
    """插件生命周期钩子失败：`on_load` / `on_enable` / `on_disable` / `on_unload`。"""

    kind = "插件生命周期失败"
    audience = "operator"


class PluginDependencyError(PluginError):
    """插件依赖不满足或依赖声明非法（必需依赖缺失/未启用/版本不符、声明成环…）。

    受众是**超管**：缺的是另一个插件，问触发者没有意义，该装/该启用的也是运维。
    插件侧也能用它做运行期兜底（见 `_DepsProxy.require`）。
    """

    kind = "插件依赖未满足"
    audience = "operator"

    def __init__(self, message: str = "", *, plugin: str = "",
                 missing: Optional[list[str]] = None,
                 cause: Optional[BaseException] = None):
        super().__init__(message, plugin=plugin, cause=cause)
        #: 缺哪些依赖（留给调用方做后续动作，比如「提示一键安装」）
        self.missing = list(missing or [])


class CommandExecutionError(PluginError):
    """命令执行失败，且**原因可以展示给用户**。

    历史上的写法是 `raise CommandExecutionError("会话列表读取失败")`，构造参数
    就是回给用户的那句话 —— 这个语义保留（见 `user_message`）。
    """

    kind = "命令失败"

    def __init__(self, message: str = "", *, plugin: str = "",
                 cause: Optional[BaseException] = None):
        super().__init__(message, plugin=plugin, user_message=message or None,
                         cause=cause)


# ==================== 短原因与编号 ====================


def short_reason(exc: BaseException, limit: int = 120) -> str:
    """异常 → 一行短原因（给用户看的；换行折叠、超长截断）。"""
    text = str(exc).strip() or type(exc).__name__
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f"{type(exc).__name__}: {text}"


def error_code(exc: BaseException, plugin: str = "") -> str:
    """稳定错误编号（4 位十六进制）。

    同一个插件 + 同一分类 + 同一异常类型 → 同一个编号，所以：
    - 用户报「E-8f31」时，`grep E-8f31 日志` 就能找到全部同类失败；
    - 节流按这个编号分组，同一类失败不会把群刷屏。
    """
    raw = f"{plugin}|{getattr(exc, 'kind', '')}|{type(exc).__name__}"
    return "E-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:4]


def _default_logger() -> logging.Logger:
    logger = logging.getLogger("napcat.errors")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


# ==================== 反馈策略 ====================


class ErrorReporter:
    """把异常翻译成「日志 + 回谁一句什么」，并做节流。

    - **日志**：永远完整（含编号、插件名、出错位置、堆栈），不受节流影响。
    - **触发者**：`audience="trigger"` 的错误回给触发的人；群聊里由投递方 @ 他。
      同一编号在 `throttle_seconds` 内重复出现时，只回一句带编号的短文案 ——
      **不会静默**（静默正是「有问不答」的来源），但也不会把群刷屏。
    - **超管**：`audience="operator"` 的错误只私聊超管；此时触发者只得到
      「已记录 + 编号」，群里不出现堆栈或内部细节。
    """

    def __init__(self, *, logger: Optional[logging.Logger] = None,
                 perm: Any = None, send: Optional[Callable[..., Awaitable]] = None,
                 send_private: Optional[Callable[..., Awaitable]] = None,
                 plugin_resolver: Optional[Callable[[Any], str]] = None,
                 enabled: bool = True, throttle_seconds: float = 60.0,
                 show_reason: bool = True, notify_superusers: bool = True):
        self.logger = logger or _default_logger()
        self.perm = perm
        self._send = send
        self._send_private = send_private
        #: 「这个处理器/命令属于哪个插件」的解析器（线上是
        #: `PluginManager.plugin_of_handler`）。归属只用于日志与定位，
        #: 缺失时留空，不影响反馈本身。
        self._plugin_resolver = plugin_resolver
        self.enabled = bool(enabled)
        self.throttle_seconds = max(0.0, float(throttle_seconds or 0.0))
        self.show_reason = bool(show_reason)
        self.notify_superusers = bool(notify_superusers)
        self._last: dict[str, float] = {}      # 节流：编号 → 上次完整提示时间

    # ---- 编号 / 文案 ----

    @staticmethod
    def code(exc: BaseException, plugin: str = "") -> str:
        return error_code(exc, plugin)

    def render(self, exc: BaseException, *, plugin: str = "",
               what: str = "处理这条消息") -> str:
        """完整文案：`⚠️ <位置>出错（E-xxxx）：<短原因>`。"""
        code = self.code(exc, plugin)
        reason = self._reason(exc)
        text = f"⚠️ {what}出错（{code}）"
        if reason:
            text += f"：{reason}"
        if getattr(exc, "retryable", False):
            text += "（可以稍后再试）"
        return text

    def render_throttled(self, exc: BaseException, *, plugin: str = "") -> str:
        """节流后的短文案：仍然带编号，方便用户报给超管。"""
        code = self.code(exc, plugin)
        return f"⚠️ 又出错了（{code}），稍后再试"

    def _reason(self, exc: BaseException) -> str:
        shown = getattr(exc, "user_message", None)
        if shown:
            return str(shown)
        if self.show_reason:
            cause = getattr(exc, "cause", None) or exc
            return short_reason(cause)
        return ""

    # ---- 投递 ----

    def _throttled(self, code: str) -> bool:
        """同一编号是否还在静默窗口内（返回 True = 这次要压成短文案）。"""
        if self.throttle_seconds <= 0:
            return False
        now = time.time()
        last = self._last.get(code, 0.0)
        if now - last < self.throttle_seconds:
            return True
        self._last[code] = now
        return False

    def log(self, exc: BaseException, *, plugin: str = "", what: str = "",
            note: str = "", level: int = logging.ERROR) -> str:
        """完整记录一条错误（含编号），返回编号。日志永远不被节流。

        `note` 只进日志（如会话 id、群号），不会出现在给用户的文案里。
        """
        code = self.code(exc, plugin)
        head = f"[{code}]" + (f" [{plugin}]" if plugin else "") + (f" {what}" if what else "")
        if note:
            head += f" ({note})"
        self.logger.log(level, "插件/框架错误 %s: %s\n%s", head, short_reason(exc),
                        "".join(traceback.format_exception(
                            type(exc), exc, exc.__traceback__)))
        return code

    async def report(self, exc: BaseException, *, event: Any = None,
                     plugin: str = "", what: str = "处理这条消息") -> Optional[str]:
        """运行期失败的统一出口：记录 + 回给触发者（或转超管）。

        返回回给用户的文案（没回则 None）。
        """
        code = self.log(exc, plugin=plugin, what=what)
        if not self.enabled:
            return None

        if event is None or self._send is None:
            # 没有「触发者」可回（定时任务、内部后台任务、独立使用的子系统）：
            # 转超管 —— 绝不静默。否则定时任务连炸几天都没人知道。
            await self.notify_operators(exc, where=what, plugin=plugin)
            return None

        if getattr(exc, "audience", "trigger") == "operator":
            # 内部故障：群里不出现细节，触发者只知道「出错了 + 编号」
            await self.notify_operators(exc, where=what, plugin=plugin)
            text = f"⚠️ 我这边出错了，已记录（{code}）"
            await self._deliver(event, text)
            return text

        text = (self.render_throttled(exc, plugin=plugin)
                if self._throttled(code)
                else self.render(exc, plugin=plugin, what=what))
        await self._deliver(event, text)
        return text

    async def notify_operators(self, exc: BaseException, *, where: str = "",
                               plugin: str = "", force: bool = False) -> bool:
        """私聊全部超管（同编号节流）。返回是否真的发了。"""
        if not self.enabled or not self.notify_superusers or self._send_private is None:
            return False
        if not force and self._throttled(self.code(exc, plugin) + "@ops"):
            return False
        users = self.superusers()
        if not users:
            return False
        text = self.render(exc, plugin=plugin, what=where or "后台任务")
        for user_id in users:
            try:
                await self._send_private(int(user_id), text)
            except Exception:  # noqa: BLE001 - 通知超管失败不该递归上报
                self.logger.error("错误反馈发送超管 %s 失败", user_id, exc_info=True)
        return True

    def superusers(self) -> list[int]:
        """超管名单的唯一来源是 `PermissionManager`（含运行时增删）。"""
        if self.perm is None:
            return []
        getter = getattr(self.perm, "superusers", None)
        return sorted(int(u) for u in (getter or []))

    def resolve_plugin(self, target: Any) -> str:
        """解析某个处理器/命令属于哪个插件（失败返回空串，绝不上抛）。"""
        if target is None or self._plugin_resolver is None:
            return ""
        try:
            return self._plugin_resolver(target) or ""
        except Exception:  # noqa: BLE001 - 归属只是日志信息，不该影响反馈
            return ""

    def set_plugin_resolver(self, resolver: Optional[Callable[[Any], str]]) -> None:
        """注入归属解析器（线上是 `PluginManager.plugin_of_handler`）。"""
        self._plugin_resolver = resolver

    async def _deliver(self, event: Any, text: str) -> None:
        try:
            await self._send(event, text)
        except Exception:  # noqa: BLE001 - 反馈发不出去时只记日志，绝不递归上报
            self.logger.error("错误反馈发送失败: %s", text, exc_info=True)

    # ---- 包装 ----

    def wrap(self, exc: BaseException, *, plugin: str = "") -> NapCatError:
        """把任意异常包成错误类型（已经是本体系的类型则原样返回）。"""
        if isinstance(exc, NapCatError):
            if plugin and isinstance(exc, PluginError) and not exc.plugin:
                exc.plugin = plugin
            return exc
        return PluginRuntimeError(short_reason(exc), plugin=plugin, cause=exc)
