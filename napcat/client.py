"""
WebSocket 客户端核心 - 连接 NapCatQQ，收发消息，驱动所有子系统
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import time
import traceback
from typing import Optional, Any, Awaitable, Callable

import websockets
from websockets.asyncio.client import ClientConnection

from .models import (
    NapCatEvent, ApiResponse, LifeCycleEvent, MessageEvent,
    GroupIncreaseEvent, GroupDecreaseEvent, GroupAdminEvent,
    parse_event,
)
from .api import ApiClient, SentLog
from .events import EventBus, TimerManager
from .command import CommandManager
from .permission import PermissionManager
from .file_manager import FileManager
from .launcher import (
    ServerLauncher, DEFAULT_RESTART_COMMAND, DEFAULT_RESTART_MODE,
)
from .plugin import PluginManager, PluginBase
from .errors import ErrorReporter, PluginError, PluginRuntimeError, short_reason
from .message import MessageChain


# 所有出站 API 上报的默认节流区间（秒）：每次调用前等待 0.5–1.0s
DEFAULT_API_DELAY = (0.5, 1.0)

# 「连上就断」判定阈值（秒）：连接活不过这个时长，算一次失败并参与指数退避。
# 取 3s 是因为真实的服务器重启后重连能稳定存活，而抖动连接通常在 1s 内就被关掉。
DEFAULT_SHORT_CONNECTION_THRESHOLD = 3.0


def parse_api_delay(value) -> tuple[float, float]:
    """把构造参数/配置解析成 (min, max) 秒区间。

    - `None`            → 默认 `(0.5, 1.0)`
    - 数字 `n`          → `(n, n)`
    - `[a]`             → `(a, a)`
    - `[a, b]`          → `(min(a,b), max(a,b))`（随机取值）

    允许 `0`（关闭节流，测试用）。
    """
    if value is None:
        return DEFAULT_API_DELAY
    if isinstance(value, bool):
        return DEFAULT_API_DELAY
    if isinstance(value, (int, float)):
        v = max(0.0, float(value))
        return (v, v)
    if isinstance(value, (list, tuple)) and value:
        try:
            vals = [max(0.0, float(v)) for v in value[:2]]
        except (TypeError, ValueError):
            return DEFAULT_API_DELAY
        if len(vals) == 1:
            return (vals[0], vals[0])
        return (min(vals), max(vals))
    return DEFAULT_API_DELAY


async def _stop_task(task: Optional[asyncio.Task]):
    """取消并等待任务结束；吞掉取消与异常（shutdown 路径专用，可重复调用）。"""
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


class WsHandler:
    """WebSocket 连接处理器。

    - **自动重连**：首次连接失败、连接被对端关闭、收发过程抛任何异常，都会回到
      重连循环（指数退避、上限 `max_reconnect_interval`），直到 `_running=False`。
    - **出站节流**：所有 API 上报发送前等待 `api_delay`（默认 0.5–1.0s）并串行化，
      避免瞬间打出一串请求触发风控。
    - **连接状态回调**（供上层做「服务器掉线 → 拉起 → 通知」这类编排）：

      | 回调 | 触发时机 | 次数语义 |
      |------|----------|----------|
      | `on_connect_failed(attempt, exc)` | 连接**建立不起来**（服务器没上线） | 每轮「连续失败」只回调第 1 次 |
      | `on_connected()` | 每次连接建立并进入收消息循环 | 首次连上 + 每次重连 |
      | `on_disconnected()` | **曾经连上**过之后连接断开 | 每次断开一次（未连上不算） |

      三个回调都在**独立 task** 里执行，异常只记日志，绝不会拖慢或打断重连循环。
    """

    def __init__(self, url: str, token: str = "", reconnect_interval: float = 5.0,
                 heartbeat_interval: float = 30.0, api_delay=DEFAULT_API_DELAY,
                 max_reconnect_interval: float = 60.0,
                 offline_hold_timeout: float = 0.0, offline_queue_max: int = 1000,
                 short_connection_threshold: float = DEFAULT_SHORT_CONNECTION_THRESHOLD,
                 logger: logging.Logger = None):
        self.url = url
        self.token = token
        self.reconnect_interval = max(0.1, float(reconnect_interval))
        self.max_reconnect_interval = max(self.reconnect_interval,
                                          float(max_reconnect_interval))
        # 「连上就断」判定阈值：连接活不过这个时长算一次失败，参与指数退避
        self.short_connection_threshold = max(0.0, float(short_connection_threshold))
        self.heartbeat_interval = heartbeat_interval
        self.api_delay = parse_api_delay(api_delay)
        self.logger = logger or _get_logger("napcat.ws")
        self._ws: Optional[ClientConnection] = None
        self._running = False
        self._pending: dict[str, asyncio.Future] = {}
        #: 流式响应：echo → 数据包队列（一条请求会收到多个同 echo 的 WS 消息）
        self._streams: dict[str, asyncio.Queue] = {}
        self._msg_queue = asyncio.Queue(maxsize=10000)
        self._processor_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        # 掉线期间把 API 调用留住：0 = 一直等到重连；队列上限防止无限堆积
        self.offline_hold_timeout = max(0.0, float(offline_hold_timeout or 0))
        self.offline_queue_max = max(0, int(offline_queue_max or 0))
        self._queued = 0                # 正在排队等重连的调用数
        self.connect_count = 0          # 成功建立连接的次数（含重连）
        self.stable_connect_count = 0   # 活得比阈值久的连接数（真的可用）
        self.flap_count = 0             # 连续「连上就断」的次数
        self.last_connection_seconds = 0.0   # 上一次连接活了多久
        self.on_connected: Optional[Callable[[], Awaitable[None]]] = None
        # 连接建立不起来时的回调：(连续失败次数, 异常)；每轮只回调第 1 次
        self.on_connect_failed: Optional[Callable[[int, BaseException], Awaitable[None]]] = None
        # 断开回调（仅「曾经连上过」才触发）
        self.on_disconnected: Optional[Callable[[], Awaitable[None]]] = None
        self._was_ready = False         # 本次运行中是否成功就绪过
        self._stable_evt = asyncio.Event()   # 「连接已稳定」信号
        self.last_error: Optional[BaseException] = None
        self._connected_at: float = 0.0     # 最近一次连接建立的时刻（算存活时长）
        # 「等用户扫码」期间：重连用固定间隔，不做指数退避。
        # 二维码已经发到用户手上，退避到 60s 只会让他扫完还等一分钟才恢复。
        self.awaiting_qr_scan = False
        self.qr_scan_retry_interval = 5.0

    async def connect(self):
        url = self.url
        if self.token and "access_token" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={self.token}"
        headers = {
            "User-Agent": "ncqqpy/1.0",
            "x-client-role": "Universal",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._ws = await websockets.connect(
            url, additional_headers=headers,
            max_size=50 * 1024 * 1024,
            ping_interval=None,
        )
        self.connect_count += 1
        self._connected_at = time.monotonic()
        self.logger.info(f"WebSocket 已连接: {self.url}")

    async def disconnect(self):
        await self._teardown()

    @property
    def connected(self) -> bool:
        """当前是否持有可用连接（掉线/重连期间为 False）。"""
        return self._ws is not None

    def _retry_delay(self, failures: int) -> float:
        """第 failures 次失败后的等待时长：从 reconnect_interval 起指数退避并封顶。"""
        n = max(1, failures)
        return min(self.reconnect_interval * (2 ** (n - 1)),
                   self.max_reconnect_interval)

    async def run_forever(self):
        """连接 → 收消息 → 断开后重连。任何异常都不会终止循环（除非停止）。

        两个**互相独立**的计数（很容易混淆，但必须分开）：

        | 计数 | 含义 | 用途 |
        |------|------|------|
        | `failures` | 连续「连不上」的次数；**「连上就断」也计入** | 指数退避 |
        | `notified_failure` | 本轮「连不上」是否已经通知过（成功连接后清零） | 拉起/告警只触发一次 |

        为什么要分开：抖动时 `failures` 会被「连上就断」抬高（这是退避需要的），
        如果拿 `failures == 1` 当「首次连不上」的判据，抖动之后服务器**真的**
        下线时，第一次连接失败会因为 `failures` 已经是 2 而**不再触发拉起** ——
        拉起逻辑就永久失效了。
        """
        failures = 0
        notified_failure = False
        while self._running:
            try:
                await self.connect()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 首次连接失败也走这里：不再让机器人启动即退出
                failures += 1
                self.last_error = e
                delay = (max(1.0, float(self.qr_scan_retry_interval or 5.0))
                         if self.awaiting_qr_scan else self._retry_delay(failures))
                self.logger.error(
                    f"WebSocket 连接失败（连续 {failures} 次）: "
                    f"{type(e).__name__}: {e}；{delay:.1f}s 后重试")
                # 每轮「连不上」只通知一次：调用方据此拉起服务器即可，
                # 后续失败说明上一次拉起没成功，重复触发没有意义。
                if not notified_failure:
                    notified_failure = True
                    self._spawn_callback("连接失败", self.on_connect_failed,
                                         failures, e)
                await asyncio.sleep(delay)
                continue

            # 连接一建立就挂一个「存活满阈值」的定时器：活过阈值即计入「稳定连接」。
            # 不能等连接结束时再判断 —— 稳定连接本来就可能一直不断开，那样
            # stable_connect_count 永远不涨，外部也就无法观察「服务真的好了」。
            connection_epoch = self.connect_count
            stable_timer = None
            if self.short_connection_threshold > 0 and self._running:
                loop = asyncio.get_running_loop()
                stable_timer = loop.call_later(
                    self.short_connection_threshold,
                    self._on_connection_stable, connection_epoch)

            reconnected = self.connect_count > 1
            if reconnected:
                if self._queued:
                    self.logger.info(
                        f"WebSocket 重连成功（第 {self.connect_count} 次连接），"
                        f"开始补发 {self._queued} 个排队中的 API 调用")
                else:
                    self.logger.info(f"WebSocket 重连成功（第 {self.connect_count} 次连接）")
            self.last_error = None
            notified_failure = False        # 连上了 → 下一轮「连不上」要重新通知

            connected_at = asyncio.get_running_loop().time()
            receive_task = asyncio.create_task(self._receive_loop())
            try:
                # 收消息循环先就绪，再回调（回调里通常会发 API 请求）
                await self._notify_connected()
                await receive_task
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 接收循环里没被就地处理的异常，一律当作断线处理
                self.logger.error(f"接收循环异常: {type(e).__name__}: {e}")
            finally:
                if stable_timer is not None:
                    stable_timer.cancel()
                await _stop_task(receive_task)
                was_ready = self._was_ready
                self._was_ready = False
                alive = asyncio.get_running_loop().time() - connected_at
                self.last_connection_seconds = alive
                await self._teardown()
                # 只有「连上过」的断开才算掉线；启动时连不上走 on_connect_failed
                if was_ready and self._running:
                    self._spawn_callback("连接断开", self.on_disconnected)

            if not self._running:
                break

            # 连上就断 = 抖动：算作失败，让退避真正生效。
            # 「稳定连接」的计数与 flap 清零由 `_on_connection_stable` 定时器负责，
            # 因为它可能在连接结束**之前**就已经满足了。
            flapping = alive < self.short_connection_threshold
            if flapping:
                self.flap_count += 1
                failures += 1
            else:
                failures = 0

            if self.awaiting_qr_scan:
                # 用户正在扫码：固定间隔重试，扫完立刻就能连上
                delay = max(1.0, float(self.qr_scan_retry_interval or 5.0))
            else:
                delay = self._retry_delay(failures if flapping else 1)
            if self.awaiting_qr_scan:
                self.logger.info(f"等待扫码登录中，{delay:.1f}s 后重试连接")
            elif flapping:
                self.logger.warning(
                    f"WebSocket 连上就断（本次仅存活 {alive:.1f}s < "
                    f"{self.short_connection_threshold:.1f}s，连续 {self.flap_count} 次），"
                    f"{delay:.1f}s 后重连")
            else:
                self.logger.warning(f"WebSocket 连接已断开，{delay:.1f}s 后重连")
            await asyncio.sleep(delay)
        self.logger.info("WebSocket 重连循环已退出")

    def _on_connection_stable(self, epoch: int):
        """连接存活满 `short_connection_threshold`：记为一次「稳定连接」。"""
        if not self._running or self.connect_count != epoch:
            return
        self.stable_connect_count += 1
        if self.flap_count:
            self.logger.info(
                f"WebSocket 连接已稳定（连续 {self.flap_count} 次「连上就断」结束）")
            self.flap_count = 0
        self._stable_evt.set()

    async def wait_stable(self, timeout: float = None) -> bool:
        """等到当前连接存活超过 `short_connection_threshold`（供编排/测试使用）。"""
        self._stable_evt.clear()
        if self.stable_connect_count:
            return True
        try:
            await asyncio.wait_for(self._stable_evt.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _spawn_callback(self, label: str, callback, *args):
        """在独立 task 里跑状态回调，异常只记日志（不干扰重连循环）。"""
        if callback is None:
            return None
        task = asyncio.create_task(self._run_callback(label, callback, *args))

        def _report(t: asyncio.Task):
            with contextlib.suppress(asyncio.CancelledError):
                if t.exception() is not None:
                    self.logger.error(
                        f"{label}回调异常:\n"
                        f"{''.join(traceback.format_exception(t.exception()))}")

        task.add_done_callback(_report)
        return task

    async def _run_callback(self, label: str, callback, *args):
        try:
            await callback(*args)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.error(f"{label}回调异常:\n{traceback.format_exc()}")

    async def _notify_connected(self):
        if self.on_connected is None:
            self._was_ready = True
            return
        try:
            await self.on_connected()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.error(f"连接就绪回调异常:\n{traceback.format_exc()}")
        finally:
            # 回调失败也算「连上过」：连接本身是可用的，断开就该报掉线
            self._was_ready = True

    async def _teardown(self):
        """清掉当前连接并让所有挂起请求立即失败（避免白等 30s 超时）。"""
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        self._fail_pending(ConnectionError("WebSocket 连接已断开"))

    def _fail_pending(self, exc: Exception):
        for echo, future in list(self._pending.items()):
            self._pending.pop(echo, None)
            if not future.done():
                future.set_exception(exc)
        # 流式请求没有 future：放一个 None 让消费者立刻结束并报连接断开
        for queue in list(self._streams.values()):
            with contextlib.suppress(Exception):
                queue.put_nowait(None)

    async def _wait_ready(self) -> None:
        """等到连接可用。

        **掉线期间不失败**：调用在这里排队，等服务器重新上线后按序补发
        （`offline_hold_timeout` 可设最长等待秒数，0 = 一直等）。
        """
        if self._ws is not None:
            return
        if not self._running:
            raise ConnectionError("WebSocket 未运行")
        if self.offline_queue_max and self._queued >= self.offline_queue_max:
            raise ConnectionError(
                f"WebSocket 掉线，排队中的 API 调用已达上限（{self.offline_queue_max}）")

        first = self._queued == 0
        self._queued += 1
        if first:
            self.logger.warning("WebSocket 掉线，API 调用开始排队，等待重连后补发")
        loop = asyncio.get_running_loop()
        deadline = (loop.time() + self.offline_hold_timeout
                    if self.offline_hold_timeout > 0 else None)
        try:
            while self._ws is None:
                if not self._running:
                    raise ConnectionError("WebSocket 已关闭，排队中的调用被取消")
                if deadline is not None and loop.time() >= deadline:
                    raise ConnectionError(
                        f"等待 WebSocket 重连超时（{self.offline_hold_timeout:.0f}s）")
                await asyncio.sleep(0.1)
        finally:
            self._queued -= 1

    async def send(self, data: dict, *, throttle: bool = True):
        # 掉线时在这里排队（不占用发送锁），重连后统一补发
        while True:
            await self._wait_ready()
            # 所有 API 上报统一节流：串行 + 每次 0.5–1.0s 延迟
            async with self._send_lock:
                ws = self._ws
                if ws is None:
                    continue                   # 等待节流期间又断了 → 回队列
                delay = self._next_delay() if throttle else 0.0
                if delay > 0:
                    await asyncio.sleep(delay)
                    ws = self._ws
                    if ws is None:
                        continue
                await ws.send(json.dumps(data, ensure_ascii=False))
                return

    def _next_delay(self) -> float:
        lo, hi = self.api_delay
        return random.uniform(lo, hi) if hi > 0 else 0.0

    async def request(self, payload: dict, timeout: float = 30.0, *,
                      throttle: bool = True) -> ApiResponse:
        echo = payload.get("echo", "")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[echo] = future
        try:
            await self.send(payload, throttle=throttle)
            result = await asyncio.wait_for(future, timeout=timeout)
            return ApiResponse(**result) if isinstance(result, dict) else result
        finally:
            self._pending.pop(echo, None)

    async def stream_request(self, payload: dict,
                             timeout: float = 60.0):
        """发起**流式** action，逐条产出服务端推送的数据包。

        NapCat 的流式文件接口（`download_file_stream` 等）会先用一条
        `data.type == "stream"` 的消息发文件信息/分片，最后一条
        `data.type == "response"` 表示结束；这些消息共用同一个 `echo`。
        普通 `request()` 只等第一条，会把后续分片丢掉，所以单独走这里。

        `timeout` 是**单条数据包**的等待上限（下载大文件时每片间隔很小，
        不会因为总时长超时）。
        """
        echo = payload.get("echo", "")
        queue: asyncio.Queue = asyncio.Queue()
        self._streams[echo] = queue
        try:
            await self.send(payload)
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError as exc:
                    raise TimeoutError(
                        f"流式响应超时（{timeout:.0f}s 内没有新数据）") from exc
                if data is None:
                    raise ConnectionError("WebSocket 连接已断开（流式传输中断）")
                yield data
                body = data.get("data") or {}
                if body.get("type") in ("response", "error"):
                    return
        finally:
            self._streams.pop(echo, None)

    async def _receive_loop(self):
        while self._running:
            ws = self._ws
            if ws is None:
                return
            try:
                raw = await ws.recv()
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed as e:
                # 把 close code/reason 与「连接活了多久」一起打出来：1005/1006 这类
                # 无状态码的关闭通常意味着对端主动断开，需要和正常关闭区分开。
                alive = time.monotonic() - self._connected_at if self._connected_at else 0.0
                self.logger.warning(
                    f"WebSocket 连接已关闭: code={e.code} reason={e.reason!r} "
                    f"存活 {alive:.1f}s rcvd={e.rcvd} sent={e.sent}")
                return
            except Exception as e:
                # 任何传输层异常都返回给 run_forever → 触发自动重连
                self.logger.error(f"接收消息异常: {type(e).__name__}: {e}")
                return
            try:
                data = json.loads(raw)
            except Exception:
                # 脏数据只丢弃这一条，不影响连接
                self.logger.error(f"消息不是合法 JSON（已忽略）: {str(raw)[:120]!r}")
                continue
            await self._msg_queue.put(data)

    async def _process_loop(self, on_event=None):
        """处理消息：API 响应 → pending futures；事件 → on_event 回调"""
        while self._running:
            try:
                data = await self._msg_queue.get()
                echo = data.get("echo")

                # 分支 0：流式响应（同 echo 的多条消息，交给 stream_request）
                if echo and echo in self._streams and "post_type" not in data:
                    self._streams[echo].put_nowait(data)
                    continue

                # 分支 1：API 响应（有匹配 echo 且无 post_type）
                if echo and echo in self._pending and "post_type" not in data:
                    self._pending[echo].set_result(data)
                    continue

                # 分支 2：事件推送（异步执行，不阻塞消息循环）
                if "post_type" in data:
                    if on_event:
                        try:
                            event = parse_event(data)
                            asyncio.create_task(on_event(event))
                        except Exception:
                            self.logger.error(f"事件解析异常:\n{traceback.format_exc()}")
                    continue

                # 分支 3：无法识别的消息（可能是已超时的 API 响应）
                if echo:
                    self.logger.debug(f"收到已过期的 API 响应: echo={echo}")
                else:
                    self.logger.debug(f"收到未识别的消息: {list(data.keys())[:5]}")

            except Exception:
                self.logger.error(f"消息处理异常:\n{traceback.format_exc()}")


# ==================== 群组绑定管理 ====================

class GroupBinding:
    """群组绑定管理器 — 控制机器人监听的群。

    规则:
    - `bound_groups` 为空 → 监听所有群（兼容旧行为）
    - `bound_groups` 非空 → 仅监听已绑定的群
    - **私聊始终不受影响**，永远监听
    """

    def __init__(self, bound: set[int] = None, all_groups: bool = True):
        self._bound: set[int] = set(bound or [])
        self._all_groups = all_groups

    # ---- 公共 API ----

    def bind(self, group_id: int) -> bool:
        """绑定一个群，返回是否为新绑定"""
        if group_id in self._bound:
            return False
        self._bound.add(group_id)
        self._all_groups = False
        return True

    def bind_many(self, group_ids: list[int]) -> int:
        """批量绑定，返回新增数量"""
        before = len(self._bound)
        self._bound.update(group_ids)
        if self._bound:
            self._all_groups = False
        return len(self._bound) - before

    def bind_all(self):
        """切换为全群模式（清空绑定列表，监听所有群）"""
        self._bound.clear()
        self._all_groups = True

    def unbind(self, group_id: int) -> bool:
        """解绑一个群，返回是否成功移除"""
        removed = group_id in self._bound
        self._bound.discard(group_id)
        return removed

    def is_bound(self, group_id: int) -> bool:
        """检查群是否已绑定（或全群模式）"""
        return self._all_groups or group_id in self._bound

    def list_bound(self) -> list[int]:
        return sorted(self._bound)

    def clear(self):
        self._bound.clear()
        self._all_groups = False

    def reset_to_all(self):
        """重置为监听所有群"""
        self._bound.clear()
        self._all_groups = True

    # ---- 属性 ----

    @property
    def all_groups(self) -> bool:
        return self._all_groups

    @property
    def bound_groups(self) -> set[int]:
        return set(self._bound)

    @property
    def count(self) -> int:
        return len(self._bound)

    def __contains__(self, group_id: int) -> bool:
        return self.is_bound(group_id)

    def __repr__(self) -> str:
        if self._all_groups:
            return "GroupBinding(所有群)"
        return f"GroupBinding(已绑定 {len(self._bound)} 个群)"


# ==================== 主客户端 ====================

class NapCatClient:
    """NapCatQQ Python 中间件主客户端。"""

    def __init__(self, *, ws_url: str = None,
                 token: str = None,
                 plugin_dir: str = None,
                 superusers: list[int] = None,
                 command_prefixes: list[str] = None,
                 bound_groups: list[int] = None,
                 all_groups: bool = None,
                 config_path: str = "config.json",
                 max_concurrency: int = 100,
                 reconnect_interval: float = None,
                 max_reconnect_interval: float = None,
                 api_delay=None,
                 offline_hold_timeout: float = None,
                 offline_queue_max: int = None,
                 restart_enabled: bool = None,
                 restart_command: str = None,
                 restart_mode: str = None,
                 restart_timeout: float = None,
                 restart_cooldown: float = None,
                 short_connection_threshold: float = None):
        # ---- 配置 ---- 参数优先级：构造参数 > 配置文件 > 硬编码默认值
        from .config import ConfigManager
        self.cfg = ConfigManager(config_path)

        ws_url = ws_url if ws_url is not None else self.cfg.get("ws_url", "ws://127.0.0.1:3001")
        token = token if token is not None else self.cfg.get("token", "")
        plugin_dir = plugin_dir if plugin_dir is not None else self.cfg.get("plugin_dir", "./plugins")
        superusers = superusers if superusers is not None else self.cfg.get("superusers", [])
        command_prefixes = (command_prefixes if command_prefixes is not None
                            else self.cfg.get("command_prefixes", ["/", "!", "。", "#"]))
        bound_groups = bound_groups if bound_groups is not None else self.cfg.get("bound_groups", [])
        all_groups = (all_groups if all_groups is not None
                      else self.cfg.get("all_groups", not bool(bound_groups)))
        # 所有 API 上报的节流区间：默认 0.5–1.0s（数字 = 固定值，[min,max] = 随机区间）
        api_delay = (api_delay if api_delay is not None
                     else self.cfg.get("api_delay", list(DEFAULT_API_DELAY)))
        # 重连退避上限（指数退避从这里封顶）
        max_reconnect_interval = (
            max_reconnect_interval if max_reconnect_interval is not None
            else self.cfg.get("max_reconnect_interval", 60.0))
        # 断线后首次重连等待（秒）；配置文件优先于硬编码默认值
        reconnect_interval = (reconnect_interval if reconnect_interval is not None
                              else self.cfg.get("reconnect_interval", 5.0))
        # 「连上就断」判定阈值：见 WsHandler.run_forever
        short_connection_threshold = (
            short_connection_threshold if short_connection_threshold is not None
            else self.cfg.get("short_connection_threshold",
                              DEFAULT_SHORT_CONNECTION_THRESHOLD))
        # 掉线期间：API 调用排队等待重连（offline_hold_timeout=0 表示一直等）
        offline_hold_timeout = (offline_hold_timeout if offline_hold_timeout is not None
                                else self.cfg.get("offline_hold_timeout", 0))
        offline_queue_max = (offline_queue_max if offline_queue_max is not None
                             else self.cfg.get("offline_queue_max", 1000))
        # 服务器拉起：WebSocket 连不上时执行（见 napcat/launcher.py）
        restart_enabled = (restart_enabled if restart_enabled is not None
                           else self.cfg.get("restart_enabled", True))
        restart_command = (restart_command if restart_command is not None
                           else self.cfg.get("restart_command", DEFAULT_RESTART_COMMAND))
        restart_mode = (restart_mode if restart_mode is not None
                        else self.cfg.get("restart_mode", DEFAULT_RESTART_MODE))
        restart_timeout = (restart_timeout if restart_timeout is not None
                           else self.cfg.get("restart_timeout", 60.0))
        restart_cooldown = (restart_cooldown if restart_cooldown is not None
                            else self.cfg.get("restart_cooldown", 60.0))

        # 回写默认值到文件（首次启动）
        self.cfg.setdefault("ws_url", ws_url)
        self.cfg.setdefault("token", token)
        self.cfg.setdefault("plugin_dir", plugin_dir)
        self.cfg.setdefault("superusers", superusers)
        self.cfg.setdefault("command_prefixes", command_prefixes)
        self.cfg.setdefault("bound_groups", bound_groups)
        self.cfg.setdefault("all_groups", all_groups)
        self.cfg.setdefault("api_delay", list(parse_api_delay(api_delay)))
        self.cfg.setdefault("reconnect_interval", reconnect_interval)
        self.cfg.setdefault("max_reconnect_interval", max_reconnect_interval)
        self.cfg.setdefault("short_connection_threshold", short_connection_threshold)
        self.cfg.setdefault("offline_hold_timeout", offline_hold_timeout)
        self.cfg.setdefault("offline_queue_max", offline_queue_max)
        self.cfg.setdefault("restart_enabled", restart_enabled)
        self.cfg.setdefault("restart_command", restart_command)
        self.cfg.setdefault("restart_mode", restart_mode)
        self.cfg.setdefault("restart_timeout", restart_timeout)
        self.cfg.setdefault("restart_cooldown", restart_cooldown)
        self.cfg.setdefault("restart_clear_cache_dir", "/opt/ncqq/napcat/cache")
        self.cfg.setdefault("watchdog_enabled", True)
        self.cfg.setdefault("watchdog", {})
        # 错误反馈策略（见 napcat/errors.py）：enabled 关掉后框架不再自动回错误
        # 文案（只写日志）；throttle_seconds 是同一类错误在同一会话的最小间隔，
        # 窗口内重复出现只回一句带编号的短文案 —— 不静默，也不刷屏。
        self.cfg.setdefault("error_report", {
            "enabled": True, "throttle_seconds": 60,
            "show_reason": True, "notify_superusers": True,
        })

        # ---- 日志 ----
        self.logger = _get_logger("napcat")

        # ---- 群组绑定 ----
        self.groups = GroupBinding(
            bound=set(bound_groups or []),
            all_groups=all_groups if bound_groups is None else not bool(bound_groups),
        )

        # ---- WebSocket ----
        self._ws = WsHandler(ws_url, token=token,
                             reconnect_interval=reconnect_interval,
                             max_reconnect_interval=max_reconnect_interval,
                             short_connection_threshold=short_connection_threshold,
                             api_delay=api_delay,
                             offline_hold_timeout=offline_hold_timeout,
                             offline_queue_max=offline_queue_max,
                             logger=self.logger)
        # WebSocket 连不上时拉起服务器（可配置；框架只负责「第一次失败拉起一次」）
        self.launcher = ServerLauncher(
            command=restart_command, mode=restart_mode, enabled=restart_enabled,
            timeout=restart_timeout, cooldown=restart_cooldown,
            clear_cache_dir=self.cfg.get("restart_clear_cache_dir"),
            logger=self.logger)
        # 看门狗（框架内置）：掉线 → 重启 → 取二维码 → ilink 发送 → 等连接
        self.watchdog = None
        if self.cfg.get("watchdog_enabled", True):
            from .watchdog import Watchdog
            wd_cfg = dict(self.cfg.get("watchdog", {}) or {})
            wd_cfg.setdefault("enabled", True)
            self.watchdog = Watchdog(self, wd_cfg, logger=self.logger)

        # ---- 子系统 ----
        # 自己发出消息的日志：引用「被切成几段」的推送时靠它还原全文/全部图片
        # （`message_id` 不保证递增，所以按**发送顺序**分组，见 `SentLog`）
        # 落盘位置：工作目录下的 `data/sent_log.json`（与其它运行期状态同处；
        # 目录不存在时 `SentLog.save()` 自己建）
        sent_log_path = os.environ.get("NCQQ_SENT_LOG") or os.path.join(
            "data", "sent_log.json")
        self.api = ApiClient(self._ws, sent_log=SentLog(sent_log_path,
                                                        logger=self.logger))
        self.perm = PermissionManager(superusers=superusers or [])
        # 用户级权限覆盖（/userperm 写进 config.json 的 user_permissions）：
        # 非法条目只告警、不阻断启动 —— 配置是给人手改的。
        for _note in self.perm.import_user_levels(
                self.cfg.get("user_permissions", {}) or {}):
            self.logger.warning("[perm] %s", _note)
        # 错误反馈业主：所有「插件/框架出错了该跟谁说、说什么、多久内不重复」
        # 的判断都收敛在这里（见 napcat/errors.py）。子系统只负责把异常交给它。
        error_cfg = self.cfg.get("error_report", {}) or {}
        self.error_reporter = ErrorReporter(
            logger=self.logger, perm=self.perm,
            send=self._send_error_reply, send_private=self._send_error_private,
            enabled=bool(error_cfg.get("enabled", True)),
            throttle_seconds=float(error_cfg.get("throttle_seconds", 60) or 0),
            show_reason=bool(error_cfg.get("show_reason", True)),
            notify_superusers=bool(error_cfg.get("notify_superusers", True)),
        )
        self.event_bus = EventBus(max_concurrency=max_concurrency)
        self.event_bus.set_error_reporter(self.error_reporter)
        self.timer = TimerManager()
        self.timer.start()
        self.timer.set_error_reporter(self.error_reporter)
        self.cmds = CommandManager(prefixes=command_prefixes, perm_manager=self.perm,
                                   groups_ref=self.groups, cfg_ref=self.cfg,
                                   event_bus_ref=self.event_bus,
                                   timer_ref=self.timer)
        self.cmds.set_error_reporter(self.error_reporter)
        self.files = FileManager(self.api)
        self._plugin_dir = plugin_dir
        self.plugins = PluginManager(
            plugin_dir=plugin_dir, api=self.api, event_bus=self.event_bus,
            timer=self.timer, cmd_manager=self.cmds, perm_manager=self.perm,
            file_manager=self.files, logger=self.logger, groups=self.groups,
            cfg=self.cfg, client=self, error_reporter=self.error_reporter,
        )
        self.cmds._plugins_ref = self.plugins

        # ---- 内部状态 ----
        self._running = False
        #: start() 是否已经跑过（防重复调用导致插件二次 load/enable）
        self._started = False
        self._main_task: Optional[asyncio.Task] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._self_id: int = 0
        # 连接状态观察者（插件用；见 on_ws_connect_failed / on_ws_connected / ...）
        self._ws_failed_hooks: list[Callable] = []
        self._ws_connected_hooks: list[Callable] = []
        self._ws_disconnected_hooks: list[Callable] = []
        self._restart_task: Optional[asyncio.Task] = None
        self.last_launch_result = None

        # ---- 注册内置处理 ----
        self._register_builtin_handlers()

    def _register_builtin_handlers(self):
        bus = self.event_bus

        @bus.on(LifeCycleEvent, hid="builtin.lifecycle",
                description="日志记录生命周期事件")
        async def on_lifecycle(event: LifeCycleEvent):
            self.logger.info(f"生命周期事件: {event.sub_type}")

        # ---- 命令分发 ----
        @bus.on_any(hid="builtin.command_dispatcher",
                    description="命令分发器")
        async def command_dispatcher(event: NapCatEvent):
            if isinstance(event, MessageEvent):
                # 群聊中必须 @bot 才能触发命令
                if event.message_type == "group" and not self._is_at_bot(event):
                    return
                result = await self.cmds.handle(event)
                if result is not None:
                    if isinstance(result, tuple) and result[0] == "_not_found":
                        await self._reply_command(event, f"未知命令: /{result[1]}，发 /help 查看可用命令")
                    else:
                        await self._reply_command(event, result)

        # ---- 群角色缓存更新 ----
        @bus.on(GroupAdminEvent, hid="builtin.update_admin_cache", description="同步群管理员缓存")
        async def update_admin_cache(event: GroupAdminEvent):
            role = "admin" if event.sub_type == "set" else "member"
            self.perm.set_group_role(event.group_id, event.user_id, role)

    # ---- 快捷装饰器 ----

    def on_message(self, group_id: int = None, private: bool = False):
        def decorator(func):
            async def wrapper(event: MessageEvent):
                if not isinstance(event, MessageEvent):
                    return
                if group_id and event.group_id != group_id:
                    return
                if private and event.message_type != "private":
                    return
                event._api = self.api
                result = func(event)
                if asyncio.iscoroutine(result):
                    await result
            self.event_bus.on(MessageEvent)(wrapper)
            return wrapper
        return decorator

    def on(self, event_type):
        return self.event_bus.on(event_type)

    def on_any(self):
        return self.event_bus.on_any()

    def register_command(self, name: str, **kwargs):
        return self.cmds.register(name, **kwargs)

    # ---- @bot 检测 ----

    def _is_at_bot(self, event: MessageEvent) -> bool:
        """检查消息是否 @了当前机器人"""
        if self._self_id == 0:
            return True  # 未获取到 self_id 时放行
        if isinstance(event.message, list):
            for seg in event.message:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    qq = str(seg.get("data", {}).get("qq", ""))
                    if qq == str(self._self_id) or qq == "all":
                        return True
        return False

    def bind_group(self, group_id: int) -> bool:
        ok = self.groups.bind(group_id)
        if ok:
            self.cfg.set("bound_groups", list(self.groups._bound))
        return ok

    def unbind_group(self, group_id: int) -> bool:
        ok = self.groups.unbind(group_id)
        if ok:
            self.cfg.set("bound_groups", list(self.groups._bound))
        return ok

    # ---- WS 事件回调 ----

    async def _on_ws_event(self, event: NapCatEvent):
        """从 WsHandler 收到事件 → 群绑定过滤 → 分发到 EventBus"""
        if isinstance(event, MessageEvent):
            event._api = self.api
            # 群角色缓存：OneBot 在**每条消息**里都带 sender.role。只靠
            # GroupAdminEvent 的话，启动后没发生过管理员变更的群，群主/管理员
            # 会被当成普通成员 —— 「群主/管理员默认管理员权限」就失效了。
            if event.message_type == "group" and event.group_id is not None:
                role = getattr(event.sender, "role", None) if event.sender else None
                role = getattr(role, "value", role)
                if role in ("owner", "admin", "member"):
                    self.perm.set_group_role(int(event.group_id),
                                             int(event.user_id), str(role))
            if (event.message_type == "group"
                    and event.group_id is not None
                    and not self.groups.is_bound(event.group_id)):
                return
        await self.event_bus.dispatch(event)

    async def _on_ws_event_safe(self, event: NapCatEvent) -> None:
        """`_on_ws_event` 的兜底包装。

        `WsHandler._process_loop` 是 `asyncio.create_task(on_event(event))` 起的
        **游离任务**：`_on_ws_event` 自己抛异常（群过滤/字段访问/总线内部）时
        没有任何人接，只在事件循环关闭时留一句 "Task exception was never
        retrieved" —— 用户侧同样是「问了没反应」。这里把它收进错误反馈。
        """
        try:
            await self._on_ws_event(event)
        except Exception as exc:  # noqa: BLE001 - 事件处理的最后一道兜底
            reporter = getattr(self, "error_reporter", None)
            if reporter is None:
                self.logger.error("事件处理异常", exc_info=True)
                return
            error = exc if isinstance(exc, PluginError) else PluginRuntimeError(
                short_reason(exc), cause=exc)
            await reporter.report(error, event=event, what="分发事件时")

    @staticmethod
    def _as_chain(result) -> MessageChain:
        if isinstance(result, MessageChain):
            return result
        if isinstance(result, str):
            return MessageChain().text(result)
        return MessageChain().text(str(result))

    async def _reply_command(self, event: MessageEvent, result):
        """将命令返回值作为回复发送。

        返回值是 `list` 时按顺序分段发送（长内容拆成多条消息，见
        `MessageChain.split()`）；其余情况发一条。
        """
        items = result if isinstance(result, list) else [result]
        # 群聊临时会话（小窗）必须带上来源群号，否则会当成好友私聊发送（非好友失败）
        temp_group = str(event.group_id) if event.is_temp_session else None
        for item in items:
            message = self._as_chain(item)
            try:
                if event.message_type == "group" and event.group_id:
                    await self.api.send_group_msg(
                        group_id=str(event.group_id), message=message)
                else:
                    await self.api.send_private_msg(
                        user_id=str(event.user_id), message=message,
                        group_id=temp_group)
            except ConnectionError as e:
                # 断线时这条回复**已经丢了**（排队中的调用会补发，断线瞬间发出的
                # 不会重试）。用户侧就是「问了一句、没有回答」，而日志里只有一行
                # 断线 —— 所以要告诉超管「谁的回复没发出去」。
                await self._report_lost_reply(event, e)
                return                        # 断线：后续分段不再尝试
            except Exception as e:
                self.logger.error(f"发送命令回复失败: {type(e).__name__}: {e}")
                await self._report_lost_reply(event, e)
                # 其余分段继续尝试，避免用户看到半截内容后什么都没有

    async def _report_lost_reply(self, event, exc: BaseException) -> None:
        """回复发不出去 → 让超管知道（触发者本人此刻联系不上）。"""
        reporter = getattr(self, "error_reporter", None)
        if reporter is None:
            return
        error = exc if isinstance(exc, PluginError) else PluginRuntimeError(
            short_reason(exc), cause=exc)
        where = f"给 {getattr(event, 'user_id', '?')} 的回复发送失败"
        await reporter.notify_operators(error, where=where)

    async def _send_error_reply(self, event, text: str) -> None:
        """错误反馈的投递口（ErrorReporter 的 `send`）。

        @ 触发者是**这里**的约定：群聊里 @ 出错时提问的那个人（他才有上下文
        回答「我刚才做了什么」），私聊直接回。事件不一定是消息事件（也可能是
        入群通知之类），所以只按有没有 `group_id` / `user_id` 判断。
        """
        user_id = getattr(event, "user_id", None)
        group_id = getattr(event, "group_id", None)
        is_group = bool(group_id) and getattr(event, "message_type", "group") != "private"
        chain = MessageChain()
        if is_group and user_id:
            chain.at(str(user_id)).text(" ")
        chain.text(text)
        if is_group:
            await self.api.send_group_msg(group_id=str(group_id), message=chain)
            return
        if user_id:
            await self.api.send_private_msg(
                user_id=str(user_id), message=chain,
                group_id=str(group_id) if getattr(event, "is_temp_session", False) else None)

    async def _send_error_private(self, user_id: int, text: str) -> None:
        """超管通知的投递口（ErrorReporter 的 `send_private`）。"""
        await self.api.send_private_msg(user_id=str(user_id),
                                        message=MessageChain().text(text))

    # ---- 生命周期 ----

    async def _on_ws_connected(self):
        """每次（重）连成功后刷新登录信息 —— self_id 决定群聊 @bot 是否放行。"""
        try:
            info = await self.api.get_login_info()
        except Exception as e:
            self.logger.warning(f"无法获取登录信息: {type(e).__name__}: {e}")
            return
        if self._self_id and info.user_id != self._self_id:
            self.logger.warning(f"登录账号已变更: {self._self_id} → {info.user_id}")
        self._self_id = info.user_id
        self.logger.info(f"登录账号: {info.nickname} ({self._self_id})")

    # ---- WebSocket 连接状态（供插件观察） ----

    def on_ws_connect_failed(self, callback):
        """注册「连接建立不起来」回调：`async callback(attempt, exc)`。

        **每轮连续失败只触发第 1 次**（拉起服务器只需要触发一次）。可多次注册。
        """
        chain = self._ws_failed_hooks
        chain.append(callback)
        return callback

    def on_ws_connected(self, callback):
        """注册「连接就绪」回调：`async callback()`（首次连上 + 每次重连）。"""
        self._ws_connected_hooks.append(callback)
        return callback

    def on_ws_disconnected(self, callback):
        """注册「曾经连上过之后断开」回调：`async callback()`。"""
        self._ws_disconnected_hooks.append(callback)
        return callback

    @staticmethod
    def _remove_hook(hooks: list, callback) -> bool:
        try:
            hooks.remove(callback)
            return True
        except ValueError:
            return False

    def off_ws_connect_failed(self, callback) -> bool:
        """注销「连接失败」回调（插件卸载时用，避免旧实例继续被调用）。"""
        return self._remove_hook(self._ws_failed_hooks, callback)

    def off_ws_connected(self, callback) -> bool:
        """注销「连接就绪」回调。"""
        return self._remove_hook(self._ws_connected_hooks, callback)

    def off_ws_disconnected(self, callback) -> bool:
        """注销「断开」回调。"""
        return self._remove_hook(self._ws_disconnected_hooks, callback)

    @property
    def ws_connected(self) -> bool:
        """当前 WebSocket 是否可用（掉线/重连期间为 False；未 start 时也是 False）。"""
        return bool(self._ws is not None and self._ws.connected)

    async def _on_ws_connect_failed(self, attempt: int, exc: BaseException):
        """WebSocket 第一次连接失败 → 拉起服务器 + 通知观察者（同样不碰插件生命周期）。

        「只拉起一次」由 `WsHandler` 保证（`failures == 1` 才回调）；这里额外挂
        `ServerLauncher` 自己的冷却时间，避免服务端反复重启。拉起是后台 task，
        不会阻塞重连循环。
        """
        self.logger.warning(f"WebSocket 第 {attempt} 次连接失败："
                            f"{type(exc).__name__}: {exc}")
        # 重启编排归看门狗所有（它才有「重启 → 发码 → 等连接 → 超时重试」的状态机）。
        # 没有看门狗时才退回框架的单次拉起，避免两条路径同时重启。
        if self.watchdog is None and (
                self._restart_task is None or self._restart_task.done()):
            self._restart_task = asyncio.create_task(
                self._restart_server(attempt, exc))
        for hook in list(self._ws_failed_hooks):
            try:
                await hook(attempt, exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.error(f"连接失败回调异常:\n{traceback.format_exc()}")

    async def _restart_server(self, attempt: int, exc: BaseException):
        result = await self.launcher.launch(
            reason=f"WebSocket 连接失败（连续 {attempt} 次）: {type(exc).__name__}: {exc}")
        self.last_launch_result = result
        return result

    async def _on_ws_disconnected(self):
        """连接断开：只通知观察者。**不**停用/重载插件（生命周期与连接解耦）。"""
        for hook in list(self._ws_disconnected_hooks):
            try:
                await hook()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.error(f"断开回调异常:\n{traceback.format_exc()}")

    async def _notify_ws_connected(self):
        """连接就绪：先刷新登录信息，再通知观察者（插件需要 self_id 已就绪）。

        ⚠️ **不碰插件生命周期**：断线重连只影响连接，绝不 load/enable/disable/reload
        插件。插件自己的恢复动作放在 `self.on_ws_connected(...)` 回调里。
        """
        await self._on_ws_connected()
        for hook in list(self._ws_connected_hooks):
            try:
                await hook()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.error(f"连接就绪回调异常:\n{traceback.format_exc()}")

    async def start(self):
        """启动：**先插件、后连接**。

        顺序是刻意的（与 WebSocket 的生命周期解耦）：

        1. 插件 `load` / `enable` **在任何连接之前**跑完 —— 插件的生命周期只由
           框架决定，不看「连没连上」；`on_load` / `on_enable` 里读
           `ws_connected` 必然是 False（不要在钩子里依赖它，见文档 §2.2）；
        2. 看门狗先挂上连上/断开回调，再起连接循环 —— 避免「连上了但没人监听」；
        3. 最后才建立 WebSocket：**断线重连只影响连接本身，不会再碰插件生命周期**
           （不重载、不停用插件；重连后的恢复动作交给 `on_ws_*` 回调）。
        """
        if self._started:
            self.logger.warning("start() 被重复调用：插件已加载，忽略（不会重复 load/enable）")
            return
        self._started = True
        self._running = True
        self.logger.info(f"ncqqpy 正在启动... [{self.groups}]")

        # ① 插件先于连接就绪（生命周期与连接解耦）
        await self.plugins.load_all()
        await self.plugins.enable_all()
        await self.plugins.watch(interval=2.0)

        # ② 看门狗先注册回调，再起连接
        if self.watchdog is not None:
            await self.watchdog.start()

        # ③ 最后建立连接。WebSocket 由 run_forever() 托管：连不上就后台重试，
        #    断线自动重连 —— 这些都不会触发插件重载。
        self._ws._running = True
        self._ws.on_connected = self._notify_ws_connected
        self._ws.on_connect_failed = self._on_ws_connect_failed
        self._ws.on_disconnected = self._on_ws_disconnected
        self._ws_task = asyncio.create_task(self._ws.run_forever())
        self._ws._processor_task = asyncio.create_task(
            self._ws._process_loop(on_event=self._on_ws_event_safe))

        self._main_task = asyncio.current_task()
        self.logger.info("ncqqpy 启动完成")

        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await _stop_task(self._ws_task)
            await _stop_task(self._ws._processor_task)

    async def stop(self):
        self.logger.info("ncqqpy 正在关闭...")
        self._running = False
        # 允许显式 stop → start 重启（这是**框架**重启，不是 WS 重连；
        # 那种情况下插件本来就会被卸载/重新加载，符合预期）。
        self._started = False
        self._ws._running = False            # 先停重连循环，避免关闭时又连上
        if self.watchdog is not None:
            with contextlib.suppress(Exception):
                await self.watchdog.stop()
        await self.plugins.shutdown()
        self.timer.stop()
        await _stop_task(self._ws_task)
        await _stop_task(self._ws._processor_task)
        await _stop_task(self._restart_task)     # 关停时别留下拉起的子进程
        await self._ws.disconnect()
        if self._main_task:
            self._main_task.cancel()
        self.logger.info("ncqqpy 已关闭")

    async def run(self):
        try:
            await self.start()
        except KeyboardInterrupt:
            self.logger.info("收到中断信号")
        finally:
            await self.stop()

    def run_blocking(self):
        asyncio.run(self.run())

    @property
    def self_id(self) -> int:
        return self._self_id


def _get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
    return logger
