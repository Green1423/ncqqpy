"""
事件系统 - 异步事件分发与处理，支持监听器元数据与暂停/恢复
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Type, TypeVar
from collections import defaultdict

from datetime import datetime, timedelta

from .models import NapCatEvent

_TIMER_LOG = logging.getLogger("napcat.events")

HandlerT = Callable[..., Coroutine[Any, Any, Any]]
T = TypeVar("T", bound=NapCatEvent)


@dataclass
class HandlerMeta:
    """事件处理器元数据"""
    id: str
    func: HandlerT
    event_type: Type[NapCatEvent]
    description: str = ""
    paused: bool = False


class EventBus:
    """异步事件总线，支持监听器 pause / resume / stop / list。

    用法:
        bus = EventBus()

        @bus.on(MessageEvent)
        async def handle_message(event: MessageEvent):
            ...
    """

    def __init__(self, max_concurrency: int = 100):
        self._handlers: dict[Type[NapCatEvent], list[HandlerMeta]] = defaultdict(list)
        self._wildcard_handlers: list[HandlerMeta] = []
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._middlewares: list[Callable[[NapCatEvent, HandlerT], Coroutine]] = []
        # 错误反馈业主（napcat/errors.py）：处理器抛异常时由它记日志并回给触发者。
        # 为 None 时退化成只打 traceback（独立使用 EventBus 的场景），线上由
        # NapCatClient 注入 —— 「有人问了却没回答」多半就是这里原来只 print 堆栈。
        self.error_reporter = None

    def set_error_reporter(self, reporter) -> None:
        self.error_reporter = reporter

    # ---- 注册 ----

    def on(self, event_type: Type[T], hid: str = None,
           description: str = "") -> Callable[[HandlerT], HandlerT]:
        """注册事件处理器。

        Args:
            event_type: 事件类（`MessageEvent` / `GroupBanEvent` …）。
            hid: 处理器 ID，用于 `/stop event` 与 `/pause event`。
            description: 说明（`/events` 列表展示）。

        Returns:
            装饰器；被装饰函数接收该事件实例。"""
        def decorator(func: HandlerT) -> HandlerT:
            _hid = hid or func.__name__
            meta = HandlerMeta(id=_hid, func=func, event_type=event_type,
                               description=description)
            self._handlers[event_type].append(meta)
            return func
        return decorator

    def on_any(self, hid: str = None, description: str = "") -> Callable[[HandlerT], HandlerT]:
        """装饰器：注册通配事件处理器（监听所有事件）。"""
        def decorator(func: HandlerT) -> HandlerT:
            _hid = hid or func.__name__
            meta = HandlerMeta(id=_hid, func=func, event_type=NapCatEvent,
                               description=description)
            self._wildcard_handlers.append(meta)
            return func
        return decorator

    def middleware(self) -> Callable:
        """装饰器：注册事件中间件。"""
        def decorator(func: Callable[[NapCatEvent, HandlerT], Coroutine]):
            self._middlewares.append(func)
            return func
        return decorator

    # ---- 注销 ----

    def off(self, event_type: Type[NapCatEvent], handler: HandlerT) -> bool:
        """按函数引用取消注册。"""
        for meta in list(self._handlers.get(event_type, [])):
            if meta.func is handler:
                self._handlers[event_type].remove(meta)
                return True
        for meta in list(self._wildcard_handlers):
            if meta.func is handler:
                self._wildcard_handlers.remove(meta)
                return True
        return False

    # ---- 暂停 / 恢复 ----

    def pause(self, handler_id: str) -> bool:
        """暂停指定 id 的监听器。"""
        for metas in list(self._handlers.values()) + [self._wildcard_handlers]:
            for meta in metas:
                if meta.id == handler_id:
                    meta.paused = True
                    return True
        return False

    def resume(self, handler_id: str) -> bool:
        """恢复指定 id 的监听器。"""
        for metas in list(self._handlers.values()) + [self._wildcard_handlers]:
            for meta in metas:
                if meta.id == handler_id:
                    meta.paused = False
                    return True
        return False

    def stop(self, handler_id: str) -> bool:
        """停止并移除指定 id 的监听器。"""
        for event_type, metas in list(self._handlers.items()):
            for meta in list(metas):
                if meta.id == handler_id:
                    self._handlers[event_type].remove(meta)
                    return True
        for meta in list(self._wildcard_handlers):
            if meta.id == handler_id:
                self._wildcard_handlers.remove(meta)
                return True
        return False

    # ---- 查询 ----

    def list_all(self) -> list[HandlerMeta]:
        """返回所有已注册监听器的元数据列表。"""
        result = []
        for metas in self._handlers.values():
            result.extend(metas)
        result.extend(self._wildcard_handlers)
        return result

    # ---- 分发 ----

    async def dispatch(self, event: NapCatEvent):
        """分发事件到所有匹配的处理器。"""
        handler_metas = list(self._handlers.get(type(event), [])) + list(self._wildcard_handlers)
        active = [m for m in handler_metas if not m.paused]
        if not active:
            return

        async def _run(meta: HandlerMeta):
            async with self._semaphore:
                try:
                    for mw in self._middlewares:
                        try:
                            await mw(event, meta.func)
                        except Exception as exc:  # noqa: BLE001
                            # 中间件吞异常以前是静默 pass：插件按文档写了中间件却
                            # 不生效，谁都不知道为什么。改走同一条错误反馈。
                            await self._report(exc, event, f"运行中间件 {meta.id} 时")
                    sig = inspect.signature(meta.func)
                    kwargs = {}
                    for name, param in sig.parameters.items():
                        if name == "event":
                            kwargs[name] = event
                        elif name == "bus":
                            kwargs[name] = self
                        elif name == "api":
                            kwargs[name] = getattr(event, "_api", None)
                    if not kwargs:
                        pass
                    await meta.func(**kwargs)
                except Exception as exc:  # noqa: BLE001
                    await self._report(exc, event, f"处理事件 {meta.id} 时", meta.func)

        await asyncio.gather(*(_run(m) for m in active))

    async def _report(self, exc: BaseException, event: NapCatEvent, what: str,
                      func=None) -> None:
        """处理器异常的统一出口：日志 + 回给触发这件事的人。

        以前这里只有 `traceback.print_exc()`：群成员 @ 机器人问了句话，插件在处理
        时抛了异常，日志里有堆栈、**群里一个字都没有** —— 这就是「有问不答」。
        """
        reporter = self.error_reporter
        if reporter is None:
            traceback.print_exc()
            return
        await reporter.report(exc, event=event, plugin=reporter.resolve_plugin(func),
                              what=what)



def next_daily_target(now: datetime, hour: int, minute: int) -> datetime:
    """下一次「每天 hour:minute」的时刻（严格大于 now）。

    ⚠️ 不能写 `target.replace(day=now.day + 1)`：1 月 31 日会变成 `day=32` 直接
    ValueError（2 月 28/29 日同理），也就是说每月最后一两天定时任务会**整天不跑**。
    `timedelta(days=1)` 会正确进位到次月 1 日。
    """
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


class TimerManager:
    """计时事件管理器 — 支持元数据追踪。"""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._metas: dict[str, dict] = {}
        self._running = False
        #: 定时任务异常的统一出口（见 EventBus._report）。定时任务没有「触发者」
        #: 可回，所以 `ErrorReporter` 会把它算成内部故障：日志 + 私聊超管。
        self.error_reporter = None

    def set_error_reporter(self, reporter) -> None:
        self.error_reporter = reporter

    async def _report(self, exc: BaseException, name: str, func=None) -> None:
        reporter = self.error_reporter
        if reporter is None:
            traceback.print_exc()
            return
        await reporter.report(exc, event=None, plugin=reporter.resolve_plugin(func),
                              what=f"跑定时任务 {name} 时")

    def _register(self, name, desc, ttype, value, func, wrapper):
        # 同名只允许一条：schedule 是「按名字索引」的表，重复注册（插件重新排一次、
        # 或先 schedule 再 pause/resume）如果只是覆盖字典，旧 task 会**继续跑** →
        # 同一个任务被执行两遍，而且再也取消不掉（句柄已丢）。
        old_task = self._tasks.get(name)
        if old_task is not None and not old_task.done():
            old_task.cancel()
            _TIMER_LOG.info("定时任务 %s 重新注册，旧任务已取消", name)
        self._tasks[name] = asyncio.create_task(wrapper())
        # ⚠️ 必须把 func 存进元数据：`resume_timer()` 靠它重新套装饰器把任务建回来。
        # 以前没存 → 任何一次「暂停后恢复」都 KeyError: 'func'（`/pause timers <名>`
        # 的 resume 分支、插件里的 pause_timer/resume_timer 全中招）。
        self._metas[name] = {"id": name, "description": desc, "type": ttype,
                             "value": value, "running": True, "func": func}
        return func

    def every(self, seconds: float, name: str = None, description: str = "") -> Callable:
        """注册固定间隔的定时任务。

        Args:
            seconds: 间隔秒数（必须大于 0）。
            name: 任务名，默认取函数名。
            description: 说明（`/timers` 列表展示）。

        Returns:
            装饰器；被装饰函数不接收参数。"""
        def decorator(func):
            task_name = name or func.__name__
            interval = float(seconds)
            if interval <= 0:
                # 0/负数会变成忙等（或者睡 0 秒的紧循环），注册时就报错比运行期烧 CPU 好。
                raise ValueError(f"定时任务 {task_name} 的间隔必须 > 0（收到 {seconds!r}）")

            async def _wrapper():
                loop = asyncio.get_running_loop()
                # ⚠️ 按**绝对时刻**排程：`next_at` 只加 interval，任务耗时不计入周期。
                # 旧写法是「跑完任务再 sleep(seconds)」，于是每轮实际周期 =
                # 任务耗时 + interval —— 实测 40ms 任务 + 100ms 间隔 → 141ms/轮，
                # 误差会一直累积（一天能偏出几十分钟）。
                next_at = loop.time() + 1.0     # 首轮仍延迟 1s（给框架启动留时间）
                while self._running:
                    wait = next_at - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    next_at += interval
                    try:
                        result = func()
                        if inspect.isawaitable(result):
                            await result
                    except Exception as exc:  # noqa: BLE001
                        await self._report(exc, task_name, func)
                    # 落后处理：任务比间隔还久、或进程被挂起过 → **跳过**错过的那些轮次，
                    # 直接对齐到下一个未来时刻。否则会瞬间补跑一堆（连发消息 / 打满 CPU）。
                    late = loop.time() - next_at
                    if late > 0:
                        skipped = int(late // interval) + 1
                        next_at += skipped * interval
                        _TIMER_LOG.info("定时任务 %s 落后 %.0fms，跳过 %d 轮对齐到下一周期",
                                        task_name, late * 1000, skipped)
            return self._register(task_name, description, "interval", f"{seconds}s", func, _wrapper)
        return decorator

    def at(self, time_str: str, name: str = None, description: str = "") -> Callable:
        def decorator(func):
            task_name = name or func.__name__
            hour, minute = map(int, time_str.split(":"))
            async def _wrapper():
                while self._running:
                    now = datetime.now()
                    target = next_daily_target(now, hour, minute)
                    wait = (target - now).total_seconds()
                    await asyncio.sleep(max(1.0, wait))
                    if not self._running:
                        break
                    try:
                        result = func()
                        if inspect.isawaitable(result):
                            await result
                    except Exception as exc:  # noqa: BLE001
                        await self._report(exc, task_name, func)
            return self._register(task_name, description, "daily", time_str, func, _wrapper)
        return decorator

    def delay(self, seconds: float, name: str = None, description: str = "") -> Callable:
        def decorator(func):
            task_name = name or func.__name__
            async def _wrapper():
                await asyncio.sleep(seconds)
                try:
                    result = func()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    await self._report(exc, task_name, func)
            return self._register(task_name, description, "delay", f"{seconds}s", func, _wrapper)
        return decorator

    def start(self):
        self._running = True

    def stop(self):
        self._running = False
        for name in list(self._tasks):
            self.cancel(name)

    def cancel(self, name: str) -> bool:
        """取消定时任务；返回它原本是否存在（让调用方能如实回报）。"""
        meta = self._metas.pop(name, None)
        task = self._tasks.pop(name, None)
        if task:
            task.cancel()
        return meta is not None or task is not None

    def pause_timer(self, name: str) -> bool:
        meta = self._metas.get(name)
        if not meta or not meta["running"]:
            return False
        task = self._tasks.pop(name, None)
        if task:
            task.cancel()
        meta["running"] = False
        return True

    def resume_timer(self, name: str) -> bool:
        meta = self._metas.get(name)
        if not meta or meta["running"]:
            return False
        # 通过重新调用装饰器恢复任务
        meta["running"] = True
        ttype = meta["type"]
        if ttype == "interval":
            self.every(float(meta["value"].rstrip("s")), name=name, description=meta["description"])(meta["func"])
        elif ttype == "daily":
            self.at(meta["value"], name=name, description=meta["description"])(meta["func"])
        elif ttype == "delay":
            self.delay(float(meta["value"].rstrip("s")), name=name, description=meta["description"])(meta["func"])
        return True

    def list_all(self) -> list[dict]:
        return list(self._metas.values())
