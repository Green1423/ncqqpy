"""
服务器看门狗（框架内置）—— 掉线自动恢复的状态机。

    websocket 断开
      → 重启 ncqq（先清 cache，再执行 restart_command）
      → 取新 qrcode（cache 已清空，所以必是本次生成）
      → ilink 发送二维码
      → 等连接成功
          成功 → ilink 发送「登录成功」
          超时 → 回到「重启 ncqq」，默认不限次数直到连上

刻意**只有一个任务**推进恢复流程（`_run_episode`）：早期版本用
「拉起跟进 / 恢复跟进 / 定时巡检」多条并行路径，彼此抢同一个二维码，
既会重复发送也会互相挡掉。现在由事件驱动：框架回调只置位
（`kick()` / `notify_online()`），状态机顺序执行。

配置在 `config.json` 顶层（不属于任何插件）：

    {
      "watchdog_enabled": true,
      "restart_enabled": true,
      "restart_mode": "systemd",
      "restart_command": "ncqq",
      "restart_cooldown": 180.0,
      "restart_clear_cache_dir": "/opt/ncqq/napcat/cache",
      // 连接断开后先观察 reconnect_grace 秒，能自行重连就不重启服务端
      "reconnect_grace": 30.0,
      // 启动重启前先检查是否已在线（已在线则跳过，避免踢掉已登录会话）
      "startup_restart_check": true,
      "startup_restart_probe_attempts": 3,
      "watchdog": {
        "qrcode_paths": ["/opt/ncqq/napcat/cache/qrcode.png"],
        "qrcode_after_launch_delay": 20.0,
        "qrcode_wait_seconds": 180,
        "qrcode_upscale": 8,
        "max_restart_cycles": 0,   // 0 = 不限次数
        "connect_timeout": 60.0,
        "notify": {"on_offline": true, "on_reconnect": true,
                   "on_qrcode": true, "on_launch_failed": true,
                   "on_login_ok": true},
        "wechat": {"enabled": true, "target": "", "auto_context": true}
      }
    }
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
import traceback
from pathlib import Path

from .ilink import Notifier
from .qr_image import upscale_image

# 数据目录（凭据 / 已发送记录）；可用环境变量覆盖（测试用）
DATA_DIR_ENV = "NCQQ_WATCHDOG_DATA_DIR"
_SENT_STATE_FILE = "qrcode_sent_state.json"
_SENT_STATE_MAX = 10
LOGIN_NOTIFY_COOLDOWN = 120.0

DEFAULT_QR_PATHS = [
    "/opt/ncqq/napcat/cache/qrcode.png",
    "/opt/ncqq/cache/qrcode.png",
]


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"


class Watchdog:
    """NapCatQQ 服务端看门狗（框架内置）。"""

    def __init__(self, client=None, cfg_data: dict = None, logger=None):
        self.client = client
        self.logger = logger or logging.getLogger("napcat.watchdog")
        # 配置：`config.json` 顶层 `watchdog` 段（不属于任何插件）
        self.cfg: dict = dict(cfg_data or {})
        # 数据目录可用环境变量覆盖（测试用：避免测试写脏生产状态）；
        # 默认在**包目录**下的 data/ —— 不是写死的部署路径，换目录 clone 也能跑。
        override = os.environ.get(DATA_DIR_ENV)
        self._data_dir = (Path(override) if override
                          else Path(__file__).resolve().parent / "data")
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # 运行状态
        self._offline_since: float = 0.0        # 本轮掉线开始时间（0 = 在线）
        self._offline_reason: str = ""
        self._offline_count: int = 0            # 累计掉线次数
        self._notified_offline: bool = False    # 本轮是否真的发过「掉线」通知
        # 去重按**内容哈希**而不是 mtime：秒级粒度下同一秒内的两次写入 mtime 会相同，
        # 只看 mtime 可能把「刚生成的新码」误判成「已发过的旧码」。
        self._qr_sent_hashes: list[str] = []
        self._qr_sent_path = _SENT_STATE_FILE     # 已发送记录的落盘文件
        self._last_launch_at: float = 0.0         # 最近一次拉起的时刻（二维码只在之后递）
        self._logged_in_hash: str = ""            # 最近一次「登录成功」时的二维码指纹
        self._last_login_notify: float = 0.0      # 上次发「登录成功」的时间
        self._qr_reserved: set[str] = set()    # 正在发送中的二维码指纹（并发去重）
        self._qr_last_hash: str = ""            # 上次见到的二维码哈希（变了才发）
        self._health_failures: int = 0           # 连续健康检查失败次数
        self._health_task: asyncio.Task | None = None
        self._grace_task: asyncio.Task | None = None     # 掉线观察期
        self._episode_task: asyncio.Task | None = None   # 当前恢复流程
        self._loop_task: asyncio.Task | None = None      # 常驻循环（单任务）
        self._wake = asyncio.Event()                     # 唤醒循环（启动重启/掉线）
        self._trigger: str = ""                          # 下次恢复流程的触发原因
        self._cycle: int = 0                             # 当前第几轮重启
        # 关键：任务不得自己清掉「唯一引用」。观察任务退出时会执行 finally，
        # 若任务自己清掉唯一引用，关停时就再也抓不到它了
        # （实测会留下一个孤儿任务，在插件卸载后继续跑并访问已拆解的资源）。
        self._tasks: set[asyncio.Task] = set()
        self._shutting_down = False             # 插件下线中：观察任务要立刻收手
        self._last_qr_note: str = ""

        self.cfg.setdefault("qrcode_paths", DEFAULT_QR_PATHS)
        self.cfg.setdefault("qrcode_wait_seconds", 180)
        self.cfg.setdefault("qrcode_poll_interval", 1.0)
        # 二维码发出后、等用户扫码期间：WebSocket 固定间隔重连（不退避）
        self.cfg.setdefault("qrcode_scan_retry_interval", 5.0)
        self.cfg.setdefault("qrcode_upscale", 8)
        # 每次拉起服务端后：等新二维码并递到微信（重启 QQ 必然要重新扫码）
        # 状态机：每轮失败后重试几次、每轮等连接多久
        # 0 = 不限次数（默认）：一直重启直到连上
        self.cfg.setdefault("max_restart_cycles", 0)
        # 不限制次数时，每这么多轮发一条微信提示（避免刷屏）
        self.cfg.setdefault("retry_notify_every_rounds", 10)
        self.cfg.setdefault("connect_timeout", 60.0)
        # 启动重启：程序起来后**执行一次**恢复流程（重启 ncqq → 发二维码 → 等登录）
        # iLink 发送失败：在超时前每 interval 秒重试一次
        self.cfg.setdefault("ilink_retry_timeout", 300.0)
        self.cfg.setdefault("ilink_retry_interval", 5.0)
        # 主动健康检查：每 health_interval 秒 get_status，要求 online 且 good
        self.cfg.setdefault("health_check", True)
        self.cfg.setdefault("health_interval", 5.0)
        self.cfg.setdefault("health_max_failures", 3)
        self.cfg.setdefault("restart_on_startup", True)
        # 启动重启前先探一次「是不是已经在线」：已在线就不打扰（否则每次框架
        # 重启都把已登录的 QQ 踢下线、逼用户重新扫码）。
        self.cfg.setdefault("startup_restart_check", True)
        # 探测重试：避免一次 get_status 超时就把在线会话判死
        self.cfg.setdefault("startup_restart_probe_attempts", 3)
        self.cfg.setdefault("startup_restart_probe_interval", 1.0)
        # 启动后先等这么久再重启（让首次连接先稳定，或先失败）
        self.cfg.setdefault("startup_restart_delay", 10.0)
        # 连接断开后先等这么久，看 WebSocket 能否自己重连；
        # 自行恢复就不重启服务端（重启 = 踢下线 + 重新扫码）。
        self.cfg.setdefault("reconnect_grace", 30.0)
        # 拉起后固定等这么久再发（给 QQ 启动 + 出码留时间），到点**直接发**
        self.cfg.setdefault("qrcode_after_launch_delay", 20.0)
        self.cfg.setdefault("notify", {
            "on_offline": True, "on_reconnect": True,
            "on_qrcode": True, "on_launch_failed": True,
        })
        self.cfg.setdefault("wechat", {"enabled": True, "target": "",
                                          "auto_context": True})

        self._qr_sent_path = self._data_dir / _SENT_STATE_FILE
        self._load_qr_sent()
        self.notifier = Notifier(logger=self.logger, data_dir=self._data_dir)
        self.notifier.apply_config(self.cfg.get("wechat", {}) or {})
        self.logger.info(
            f"[watchdog] 已加载：微信通知={'可用' if self.notifier.configured else '未配置'}，"
            f"二维码候选路径={self._qr_paths()}")

    async def start(self):
        """挂上连接状态回调、启动微信通道（由 NapCatClient.start() 调用）。"""
        self._shutting_down = False
        client = self.client
        client.on_ws_connect_failed(self._on_connect_failed)
        client.on_ws_disconnected(self._on_disconnected)
        client.on_ws_connected(self._on_reconnected)
        self.notifier.start()
        # 常驻循环：既处理「掉线恢复」，也处理「周期性自动重启」
        self._loop_task = self._spawn(self._run_loop())
        self.logger.info(
            f"[watchdog] 已启动：微信通知="
            f"{'可用' if self.notifier.configured else '未配置'}，"
            f"每轮等连接 {self._connect_timeout():.0f}s，"
            + (f"最多 {int(self.cfg.get('max_restart_cycles'))} 轮"
               if int(self.cfg.get("max_restart_cycles", 0) or 0) > 0
               else "重启次数不限"))

    async def stop(self):
        """收尾：摘回调、停微信通道、等状态机退出（可重复调用）。"""
        self._unregister_hooks()
        self._shutting_down = True
        self.notifier.stop()
        await self._drain_tasks()

    def _unregister_hooks(self):
        """把框架上的连接状态回调摘掉。"""
        client = self.client
        if client is None:
            return
        for off, cb in (
            (getattr(client, "off_ws_connect_failed", None), self._on_connect_failed),
            (getattr(client, "off_ws_disconnected", None), self._on_disconnected),
            (getattr(client, "off_ws_connected", None), self._on_reconnected),
        ):
            if off is not None:
                with contextlib.suppress(Exception):
                    off(cb)

    async def _drain_tasks(self):
        """取消并等待**所有**后台任务真正结束（不能只 cancel 就走）。

        按 `_tasks` 集合收：只看某个具体字段会漏掉被重新赋值、
        或被任务自身清空引用的那些，留下在卸载后继续运行的孤儿任务。
        """
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()

    # ------------------------------------------------------------------
    # 连接状态回调（只记账 + 唤醒观察任务，不在回调里发通知）
    # ------------------------------------------------------------------
    async def _on_connect_failed(self, attempt: int, exc: BaseException):
        """服务器压根连不上 —— 框架会据此拉起服务端，这里只记账。"""
        self._kick_episode(f"WebSocket 连接失败（连续 {attempt} 次）: "
                           f"{type(exc).__name__}: {exc}", grace=True)

    async def _on_disconnected(self):
        """连上过之后又断了（QQ 掉线 / 服务端重启 / 抖动）。

        先进入观察期（`reconnect_grace`）：框架自身会退避重连，很多「断开」只是
        网络抖动，能自行恢复就不该重启服务端（重启意味着 QQ 重新扫码登录）。
        """
        self._kick_episode("WebSocket 连接已断开", grace=True)

    async def _on_reconnected(self):
        """连上了（说明 `get_login_info` 已经成功 —— QQ 登录态可用）。

        这里只做状态交接，真正的通知交给观察任务（要保证「掉线」先于「登录成功」）。
        另外记下「登录成功」时的二维码指纹：本次推送的码从此不再有意义。
        """
        if self._shutting_down:
            return
        if self._grace_task is not None and not self._grace_task.done():
            self._grace_task.cancel()
        self._grace_task = None
        if self._offline_since <= 0:
            return
        latest = self._find_latest_qr()
        if latest:
            self._logged_in_hash = self._file_hash(Path(latest))
        # 连接恢复 = 本轮结束：状态机据此收手。
        # 只有本轮真的发过「掉线」通知才报平安 —— 抖动在观察期内自行恢复、
        # 或没开掉线通知时，不该凭空冒出一条「QQ 已登录成功」。
        notify_ok = self._notified_offline
        self._mark_online()
        if notify_ok and self._notify_enabled("on_login_ok"):
            with contextlib.suppress(Exception):
                await self._notify_login_ok()

    async def _notify_login_ok(self):
        """登录成功反馈：明确告诉对方已经登进去了、二维码已失效。

        去抖：`LOGIN_NOTIFY_COOLDOWN` 内只发一次，避免连接抖动时反复刷屏。
        """
        now = time.time()
        if now - self._last_login_notify < LOGIN_NOTIFY_COOLDOWN:
            self.logger.info("[watchdog] 登录成功反馈在冷却期内，跳过")
            return False
        self._last_login_notify = now
        self._save_qr_state()
        self.logger.info("[watchdog] QQ 登录成功，发送反馈")
        return await self._ilink_text(
            "✅ QQ 已登录成功\n"
            f"时间：{_fmt_time(now)}\n"
            "登录二维码已失效，机器人已恢复收发消息。")

    def cycle_idle(self) -> bool:
        """当前是否不在恢复流程中（供状态展示/测试）。"""
        return self._cycle == 0

    def _mark_online(self):
        self._offline_since = 0.0
        self._offline_reason = ""
        self._notified_offline = False
        self._cycle = 0
        self._health_failures = 0            # 新会话从干净状态开始计数
        self._qr_last_hash = ""              # 下一轮重新以「与上次不同」判定
        self._set_scan_waiting(False)        # 已登录：恢复正常退避

    # ------------------------------------------------------------------
    # 二维码
    # ------------------------------------------------------------------
    # ---- 已发送二维码的去重（跨进程持久化） ----

    def _load_qr_sent(self):
        try:
            if self._qr_sent_path.exists():
                data = json.loads(self._qr_sent_path.read_text(encoding="utf-8"))
                self._qr_sent_hashes = [str(x) for x in (data.get("hashes") or [])]
                self._logged_in_hash = str(data.get("logged_in_hash") or "")
                self._last_login_notify = float(data.get("last_login_notify") or 0.0)
        except Exception as e:
            self.logger.warning(f"[watchdog] 读取二维码发送记录失败: {e}")
            self._qr_sent_hashes = []

    def _save_qr_state(self):
        try:
            tmp = self._qr_sent_path.with_suffix(self._qr_sent_path.suffix + ".tmp")
            tmp.write_text(json.dumps({
                "hashes": self._qr_sent_hashes,
                "logged_in_hash": self._logged_in_hash,
                "last_login_notify": self._last_login_notify,
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._qr_sent_path)
        except Exception as e:
            self.logger.warning(f"[watchdog] 写入二维码状态失败: {e}")

    @staticmethod
    def _file_hash(path: Path) -> str:
        """二维码内容指纹（读不到就返回空串）。"""
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()[:32]
        except OSError:
            return ""

    def _remember_qr_sent(self, digest: str):
        if digest and digest not in self._qr_sent_hashes:
            self._qr_sent_hashes.append(digest)
        self._qr_sent_hashes = self._qr_sent_hashes[-_SENT_STATE_MAX:]
        self._save_qr_state()

    def _prune_sent_copies(self, keep: int = 3):
        """只保留最近几份「放大后发出的二维码」副本，别堆着占地方。"""
        try:
            copies = sorted(self._data_dir.glob("qrcode_sent_*.png"),
                            key=lambda f: f.stat().st_mtime, reverse=True)
        except OSError:
            return
        for stale in copies[max(0, keep):]:
            try:
                stale.unlink()
            except OSError:
                pass

    def _qr_paths(self) -> list[str]:
        paths = self.cfg.get("qrcode_paths", DEFAULT_QR_PATHS)
        if isinstance(paths, str):
            paths = [paths]
        return [str(p) for p in (paths or DEFAULT_QR_PATHS)]

    def _find_latest_qr(self) -> str:
        """返回 mtime 最新的那个存在且非空的二维码文件。"""
        best, best_mtime = "", -1.0
        for raw in self._qr_paths():
            path = Path(raw)
            try:
                if path.is_file() and path.stat().st_size > 0:
                    mtime = path.stat().st_mtime
                    if mtime > best_mtime:
                        best, best_mtime = str(path), mtime
            except OSError:
                continue
        return best

    # ------------------------------------------------------------------
    # 恢复状态机（唯一入口）
    #
    #   websocket 断开 → 重启 ncqq → 取新二维码 → ilink 发送
    #     → 等连接成功 → 成功：ilink 报成功  /  超时：回到「重启 ncqq」
    #
    # 只有这一个任务在推进恢复流程，不再有「拉起跟进 / 恢复跟进 / 定时巡检」
    # 多条路径互相抢（早期那套会重复发码、也会互相挡）。
    # ------------------------------------------------------------------
    def _kick_episode(self, reason: str, *, grace: bool = False):
        """掉线入口：记账 + 请常驻循环跑一次恢复（回调里不能 await）。

        `grace=True`：先等 `reconnect_grace` 秒看连接能否自行恢复；能恢复就不重启
        服务端（避免网络抖动导致整台 QQ 重新登录）。
        

        注意这里判断的是「**当前有没有恢复流程在跑**」，不能拿 `_mark_offline()`
        的返回值当闸门 —— `_offline_since` 会被置位直到真正登录成功，
        用它去重会让「第一轮之后的所有触发」被永久丢弃（实测：健康检查每 15s
        报一次异常，但再也起不来恢复流程）。
        """
        if self._shutting_down:
            return
        first = self._mark_offline(reason)        # 只记账/更新原因
        if self._episode_running():
            self.logger.info("[watchdog] 恢复流程已在运行，本次触发只更新原因")
            return
        if self._grace_task is not None and not self._grace_task.done():
            if grace:
                self.logger.info("[watchdog] 掉线观察期内，等待连接自行恢复")
                return
            # 已确认异常（健康检查/手动触发）：终止观察期，立即恢复
            self._grace_task.cancel()
            self._grace_task = None
        seconds = self._reconnect_grace() if grace else 0.0
        if first and seconds > 0:
            self.logger.warning(
                f"[watchdog] 连接异常，先等 {seconds:.0f}s 看能否自行恢复"
                "（不重启服务端）")
            self._grace_task = self._spawn(self._grace_then_recover(reason, seconds))
            return
        self._request_episode(f"掉线：{reason}")

    def _episode_running(self) -> bool:
        """恢复流程是否正在跑（跑完的任务不算）。"""
        task = self._episode_task
        return task is not None and not task.done()

    def _request_episode(self, trigger: str):
        """请求跑一次恢复流程（由 `_run_loop` 串行执行）。"""
        if self._shutting_down:
            return
        self._trigger = trigger
        self._wake.set()

    def _spawn_episode(self, trigger: str):
        """直接起一个恢复流程任务（供 `_run_loop` 调用）。"""
        self._episode_task = self._spawn(self._run_episode(trigger))

    def _reconnect_grace(self) -> float:
        return max(0.0, float(self.cfg.get("reconnect_grace", 30.0) or 0))

    async def _grace_then_recover(self, reason: str, seconds: float):
        """观察期结束：连接自行恢复就取消恢复流程，否则才重启服务端。"""
        task = asyncio.current_task()
        try:
            await asyncio.sleep(seconds)
            if self._shutting_down:
                return
            if getattr(self.client, "ws_connected", False):
                self.logger.info("[watchdog] 连接已自行恢复，跳过重启")
                self._mark_online()
                return
            if self._episode_running():
                return
            self._request_episode(f"掉线：{reason}（{seconds:.0f}s 内未恢复）")
        except asyncio.CancelledError:
            raise
        finally:
            if self._grace_task is task:
                self._grace_task = None

    async def _health_loop(self):
        """每 `health_interval` 秒主动 `get_status`，要求 `online` 且 `good`。

        只要有一项不为 true（或调用本身失败），就触发**与 websocket 断联同款**的
        恢复链路：重启 ncqq → 取二维码 → ilink 发送 → 等登录成功。

        为什么要主动查：websocket 连着不代表账号状态正常（例如 QQ 掉线但
        OneBot 连接还在），这类「假在线」只有 get_status 能看出来。
        连续失败 `health_max_failures` 次才动手，避免单次抖动误判。
        """
        try:
            # 下限 0.1s：生产用 5s，测试可以调快
            interval = max(0.1, float(self.cfg.get("health_interval", 5.0) or 5.0))
            need = max(1, int(self.cfg.get("health_max_failures", 3) or 3))
            while not self._shutting_down:
                await asyncio.sleep(interval)
                if self._shutting_down:
                    return
                if self._episode_running():
                    # 恢复流程正在跑：连接状态必然不稳定，重复计数只会制造噪声
                    self._health_failures = 0
                    continue
                healthy, detail = await self._probe_status()
                if healthy:
                    if self._health_failures:
                        self.logger.info(
                            f"[watchdog] 健康检查恢复正常"
                            f"（此前连续失败 {self._health_failures} 次）")
                    self._health_failures = 0
                    continue
                self._health_failures += 1
                self.logger.warning(
                    f"[watchdog] 健康检查未通过（连续 {self._health_failures}/{need}）:"
                    f" {detail}")
                if self._health_failures < need:
                    continue
                self._health_failures = 0
                self.logger.error(f"[watchdog] 账号状态异常，按「断联」处理：{detail}")
                self._kick_episode(f"健康检查失败：{detail}")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.error(f"[watchdog] 健康检查异常:\n{traceback.format_exc()}")
        finally:
            self._health_task = None

    async def _probe_status(self) -> tuple[bool, str]:
        """调一次 `get_status`，返回 (是否健康, 说明)。

        `online` 与 `good` **都**必须为 true 才算健康。
        """
        client = self.client
        api = getattr(client, "api", None)
        if api is None:
            return False, "拿不到 api 客户端"
        if not getattr(client, "ws_connected", False):
            return False, "websocket 未连接"
        try:
            status = await asyncio.wait_for(api.get_status(), timeout=10.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return False, f"get_status 调用失败：{type(e).__name__}: {e}"
        online = bool(getattr(status, "online", False))
        good = bool(getattr(status, "good", False))
        if online and good:
            return True, ""
        return False, f"online={online} good={good}"

    async def _run_loop(self):
        """常驻循环：**程序启动后执行一次重启**，之后按需处理掉线恢复。

        两条路径共用同一个 `_run_episode()`，所以不会并发抢同一个二维码：

        - `restart_on_startup`：启动后等 `startup_restart_delay` 秒，主动跑一次
          （重启 ncqq → 取二维码 → ilink 发送 → 等登录成功）；
        - 掉线（连接失败 / 连接断开）：立即触发同一条流程。
        """
        try:
            if self.cfg.get("restart_on_startup", True):
                delay = max(0.0, float(self.cfg.get("startup_restart_delay", 10.0) or 0))
                self.logger.info(f"[watchdog] 启动后 {delay:.0f}s 执行一次重启")
                if delay and not self._wake.is_set():
                    # 期间若掉线则提前进入恢复流程（不必再等满延迟）
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._wake.wait(), timeout=delay)
                if not self._shutting_down:
                    if self._wake.is_set():
                        # 等待期间已经掉线：走恢复流程（原因用掉线本身）
                        trigger = self._trigger or "程序启动后的自动重启"
                        self._wake.clear()
                        self._trigger = ""
                        self._spawn_episode(trigger)
                    elif await self._should_skip_startup_restart():
                        self.logger.info(
                            "[watchdog] 启动时已经在线（online+good），跳过启动重启")
                    else:
                        self._spawn_episode("程序启动后的自动重启")

            # 主动健康检查与恢复流程并行：它只负责「发现问题就触发同一条链路」
            if self.cfg.get("health_check", True) and self._health_task is None:
                self._health_task = self._spawn(self._health_loop())

            while not self._shutting_down:
                await self._wake.wait()
                if self._shutting_down:
                    break
                trigger = self._trigger or "掉线恢复"
                self._wake.clear()
                self._trigger = ""
                if self._episode_running():
                    self.logger.info("[watchdog] 恢复流程已在运行，跳过本次触发")
                    continue
                self._spawn_episode(trigger)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.error(f"[watchdog] 恢复循环异常:\n{traceback.format_exc()}")
        finally:
            self._loop_task = None

    async def _should_skip_startup_restart(self) -> bool:
        """启动重启前的在线判定：已在线就不重启服务端。

        背景：登录态在 NapCat 进程里，重启 `ncqq` 等于把已登录的 QQ 踢下线，
        下一次登录要重新扫码。所以「框架进程重启」不应该无条件再重启服务端。

        判据与健康检查一致：WebSocket 已连接，且 `get_status` 返回
        `online=true`、`good=true`。探测允许重试，避免一次超时误判。
        """
        if not self.cfg.get("startup_restart_check", True):
            return False
        if self._offline_since > 0:
            return False                 # 启动期间已经掉线：必须恢复
        if not getattr(self.client, "ws_connected", False):
            self.logger.info("[watchdog] 启动检查：WebSocket 未连接，执行启动重启")
            return False
        checks = max(1, int(self.cfg.get("startup_restart_probe_attempts", 3) or 3))
        gap = max(0.0, float(
            self.cfg.get("startup_restart_probe_interval", 1.0) or 0))
        detail = ""
        for attempt in range(1, checks + 1):
            healthy, detail = await self._probe_status()
            if healthy:
                return True
            if attempt < checks and gap:
                await asyncio.sleep(gap)
        self.logger.info(
            f"[watchdog] 启动检查：当前不健康（{detail}），执行启动重启")
        return False

    def _spawn(self, coro) -> asyncio.Task:
        """启动后台任务并登记（关停时按集合逐个收掉）。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run_episode(self, trigger: str = "掉线恢复"):
        """重启 ncqq → 取二维码 → ilink 发送 → 等登录成功，直到成功或跑满轮次。"""
        # 先登记自己：_kick_episode() 靠它判断「已有恢复流程在跑」；
        # 同时取消掉线观察期，避免流程结束后又被观察期重复触发。
        self._episode_task = asyncio.current_task()
        if self._grace_task is not None and not self._grace_task.done():
            self._grace_task.cancel()
        self._grace_task = None
        try:
            self.logger.warning(f"[watchdog] 开始恢复流程（{trigger}）")
            if self._offline_since <= 0:
                # 启动重启（并非掉线引起）：先记一笔，通知/状态才有依据
                self._mark_offline(trigger)
            if self._notify_enabled("on_offline"):
                await self._notify_offline()

            # 默认**不限次数**：一直「重启 → 等登录」直到连上。
            # `max_restart_cycles > 0` 时才作为上限（调试/演练用）。
            max_cycles = int(self.cfg.get("max_restart_cycles", 0) or 0)
            limit = max_cycles if max_cycles > 0 else None
            cycle = 0
            notified_rounds = 0
            while limit is None or cycle < limit:
                if self._shutting_down:
                    return
                cycle += 1
                self._cycle = cycle
                # ---- 2. 重启 ncqq（框架负责：清 cache → 执行 restart_command）----
                launch = await self._restart_ncqq(cycle)
                if self._shutting_down:
                    return
                # ---- 3+4. 取新二维码并 ilink 发送 ----
                await self._deliver_qrcode(cycle, launch)
                if self._shutting_down:
                    return
                # ---- 5. 等连接成功 ----
                if await self._wait_online(cycle):
                    return               # 成功：_on_reconnected 里已发成功反馈
                self.logger.warning(
                    f"[watchdog] 第 {cycle} 轮：{self._connect_timeout():.0f}s 内未连上，"
                    f"回到「重启 ncqq」重试"
                    + ("" if limit is None else f"（上限 {limit} 轮）"))
                # 不限制次数时不能每轮都发微信（会刷屏）：每 N 轮提示一次
                notified_rounds += 1
                if notified_rounds >= self._round_notify_every():
                    notified_rounds = 0
                    await self._ilink_text(
                        f"⏳ 已连续重启 {cycle} 轮仍未登录成功，继续重试中…\n"
                        f"最近一次：{self._launch_note()}")

            self.logger.error(f"[watchdog] 已重试 {cycle} 轮仍未连上，本轮结束")
            if self._notify_enabled("on_launch_failed"):
                await self._ilink_text(
                    f"⚠️ 已连续重启 {cycle} 轮仍未登录成功，本轮结束。\n"
                    f"最后一次：{self._launch_note()}\n"
                    f"下一条掉线/健康检查告警会重新开始恢复。")
            # 不限次数时本轮结束要复位，好让下一条告警能重新开始恢复；
            # 设了上限就保持「已放弃」状态，真正停下来等人工介入。
            if limit is None and not self._shutting_down:
                self._offline_since = 0.0
                self._offline_reason = ""
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.error(f"[watchdog] 恢复流程异常:\n{traceback.format_exc()}")
        finally:
            self._cycle = 0
            # 只回收「自己」：赋值与 finally 之间可能交错，按身份判断最稳
            task = asyncio.current_task()
            if self._episode_task is task:
                self._episode_task = None

    def _retry_timeout(self) -> float:
        return max(5.0, float(self.cfg.get("ilink_retry_timeout", 300.0) or 300.0))

    def _retry_interval(self) -> float:
        return max(1.0, float(self.cfg.get("ilink_retry_interval", 5.0) or 5.0))

    async def _ilink_text(self, text: str) -> bool:
        """发文本（带重试）：失败就每 `ilink_retry_interval` 秒重试到超时。"""
        return await self.notifier.send_text_retry(
            text, timeout=self._retry_timeout(), interval=self._retry_interval())

    async def _ilink_image(self, path, caption: str = "") -> bool:
        """发图片（带重试）。"""
        return await self.notifier.send_image_retry(
            path, caption=caption, timeout=self._retry_timeout(),
            interval=self._retry_interval())

    def _round_notify_every(self) -> int:
        return max(1, int(self.cfg.get("retry_notify_every_rounds", 10) or 10))

    def _connect_timeout(self) -> float:
        # 每轮「等登录成功」的时限（默认 1 分钟）
        return max(5.0, float(self.cfg.get("connect_timeout", 60.0) or 60.0))

    async def _restart_ncqq(self, cycle: int) -> bool:
        """执行一次拉起（框架内部：先清 cache，再跑 restart_command）。"""
        launcher = getattr(self.client, "launcher", None)
        if launcher is None:
            self.logger.error("[watchdog] 拿不到 launcher，无法重启服务端")
            return False
        before = launcher.attempts
        result = await launcher.launch(reason=f"WebSocket 掉线（第 {cycle} 轮）")
        if launcher.attempts == before:      # 被冷却拦住 → 等它过了再来
            wait = launcher._cooldown_remaining()
            if wait > 0:
                self.logger.info(f"[watchdog] 拉起冷却中，等 {wait:.0f}s")
                await asyncio.sleep(wait)
                result = await launcher.launch(reason=f"WebSocket 掉线（第 {cycle} 轮）")
        if not (result and result.started):
            self.logger.error(f"[watchdog] 第 {cycle} 轮拉起失败："
                              f"{(result.reason if result else '未知')}")
            if getattr(result, "hint", ""):
                self.logger.error(f"[watchdog] 拉起失败原因（权限）: {result.hint}")
            if self._notify_enabled("on_launch_failed"):
                lines = [f"⚠️ 重启 ncqq 失败（第 {cycle} 轮）"]
                if result is not None:
                    lines.append(f"原因：{result.reason[:300]}")
                if getattr(result, "hint", ""):
                    lines.append("")
                    lines.append(result.hint)
                await self._ilink_text("\n".join(lines))
        return bool(result and result.started)

    async def _deliver_qrcode(self, cycle: int, launched: bool) -> bool:
        """等 `qrcode_after_launch_delay` → 取二维码 → ilink 发送。"""
        self._last_launch_at = time.time()
        delay = float(self.cfg.get("qrcode_after_launch_delay", 20.0) or 0)
        if delay > 0:
            self.logger.info(f"[watchdog] 第 {cycle} 轮：等 {delay:.0f}s 后取二维码")
            await asyncio.sleep(delay)
        if not self._notify_enabled("on_qrcode"):
            return False
        latest, mtime, digest = await self._wait_qrcode()
        if not latest:
            self.logger.warning(f"[watchdog] 第 {cycle} 轮：没等到二维码"
                                f"（{self._qr_paths()}）")
            return False
        if digest in self._qr_sent_hashes:
            self.logger.info(f"[watchdog] 第 {cycle} 轮：这张二维码已发过，跳过")
            return False
        tag = f"第 {cycle} 次重启 ncqq 后"
        note = await self._send_qrcode(Path(latest), tag=tag)
        if note:
            self.logger.info(f"[watchdog] 第 {cycle} 轮：二维码已通过 ilink 发出")
            # 二维码已经递到用户手上：期间重连用固定间隔，用户扫完立刻能连上
            self._set_scan_waiting(True)
            return True
        return False

    def _set_scan_waiting(self, waiting: bool):
        """切换「等用户扫码」模式：期间 WebSocket 固定间隔重连（不退避）。"""
        ws = getattr(self.client, "_ws", None)
        if ws is None:
            return
        ws.qr_scan_retry_interval = float(
            self.cfg.get("qrcode_scan_retry_interval", 5.0) or 5.0)
        if ws.awaiting_qr_scan != waiting:
            ws.awaiting_qr_scan = waiting
            self.logger.info(
                f"[watchdog] {'进入' if waiting else '退出'}等扫码模式"
                f"（重连间隔固定 {ws.qr_scan_retry_interval:.0f}s）")

    async def _wait_online(self, cycle: int) -> bool:
        """等 WebSocket 恢复（= `get_login_info` 成功 = 登录可用）。

        直接看框架的连接状态而不是等事件：`on_ws_connected` 回调是异步派发的，
        可能在进入本方法之前就已经触发过，事件会漏掉（实测踩到）。
        """
        timeout = self._connect_timeout()
        # 无论本轮有没有发出二维码，等待期间都用固定间隔重连：
        # 退避到 60s 时用户扫完还要干等一分钟，体验很差。
        self._set_scan_waiting(True)
        self.logger.info(f"[watchdog] 第 {cycle} 轮：等待登录成功（最多 {timeout:.0f}s）")
        deadline = time.monotonic() + timeout
        while not self._shutting_down:
            if getattr(self.client, "ws_connected", False):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)
        return getattr(self.client, "ws_connected", False)

    async def _wait_qrcode(self, max_wait: float = None) -> tuple[str, float, str]:
        """等一张**与上次不同**的二维码，返回 (路径, mtime, 指纹)。

        判定方式就是**比对图片哈希**：

        - 记录上次见过的哈希（`_qr_last_hash`）；
        - 轮询二维码文件，一旦哈希与上次不同 → 说明 NapCat 已经写出新码，
          此刻即可发送（不需要判断「写没写完」或 mtime）；
        - 哈希始终不变（登录态还在、没换码）→ 等到 `qrcode_wait_seconds` 超时。

        哈希同时用于发送去重：同一张码不会因为重试而重发。
        """
        if max_wait is None:
            max_wait = float(self.cfg.get("qrcode_wait_seconds", 180) or 0)
        interval = max(0.2, float(self.cfg.get("qrcode_poll_interval", 1.0) or 1.0))
        deadline = time.monotonic() + max(0.0, max_wait)
        while not self._shutting_down:
            latest = self._find_latest_qr()
            if latest:
                path = Path(latest)
                digest = self._file_hash(path)
                if digest and digest != self._qr_last_hash:
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    self.logger.info(
                        f"[watchdog] 检测到新的二维码（哈希 {digest[:12]}… "
                        f"≠ 上次 {self._qr_last_hash[:12] or '无'}），可以发送")
                    self._qr_last_hash = digest
                    return latest, mtime, digest
            if time.monotonic() >= deadline:
                return ("", 0.0, "")
            await asyncio.sleep(interval)
        return ("", 0.0, "")

    async def _send_qrcode(self, path: Path, *, tag: str = "") -> str:
        """把二维码发到微信：优先发图片，发不了就发文字说明。

        `tag` 用来区分「第 N 次重启后」这类场景，让用户知道该扫哪张码。
        同一张图（mtime 相同）只发一次，避免恢复流程与拉起流程各发一遍。
        """
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = time.time()

        digest = self._file_hash(path)
        if not digest:
            self.logger.warning(f"[watchdog] 二维码文件读不到内容，跳过: {path}")
            return ""
        if digest in self._qr_sent_hashes or digest in self._qr_reserved:
            self.logger.info(f"[watchdog] 该二维码已发送/正在发送，跳过（{path}）")
            return ""
        # 不再判断「新鲜度」：cache 每次启动前被清空，能读到的码就是本次生成的。
        # 只在「文件比拉起还老」时提醒一句 —— 说明清 cache 没生效，
        # 那张码可能是上一轮的（此时仍照发，宁可多发一张作废的码，
        # 也不冒险漏发导致一直登不上去）。
        if self._last_launch_at and mtime and mtime < self._last_launch_at - 5:
            self.logger.warning(
                f"[watchdog] 二维码比本次拉起更早（mtime={mtime:.0f} < "
                f"拉起={self._last_launch_at:.0f}）—— 检查 systemd ExecStartPre "
                f"是否在清 cache；本条仍会发送: {path}")

        caption = ("📱 QQ 需要重新登录\n"
                   + (f"{tag}\n" if tag else "")
                   + f"请用**手机 QQ** 扫码登录（{_fmt_time(mtime)}）")
        # 发送前先占位：并发等待者看到同一张码时只有一个会真正发出去
        self._qr_reserved.add(digest)
        sent = False
        factor = int(self.cfg.get("qrcode_upscale", 8) or 1)
        if factor > 1:
            try:
                dst = self._data_dir / f"qrcode_sent_{int(mtime)}.png"
                self._data_dir.mkdir(parents=True, exist_ok=True)
                upscale_image(path, dst, factor=factor, white_border=1)
                sent = await self._ilink_image(dst, caption)
            except Exception as e:
                self.logger.warning(f"[watchdog] 二维码放大失败（发原图）: "
                                    f"{type(e).__name__}: {e}")
        if not sent:
            sent = await self._ilink_image(path, caption)

        self._qr_reserved.discard(digest)
        if sent:
            self._remember_qr_sent(digest)
            self._prune_sent_copies()
            self._last_qr_note = f"已发送二维码（{path}）{' ' + tag if tag else ''}"
            self.logger.info(f"[watchdog] 二维码已发送到微信: {path} {tag}")
            return self._last_qr_note

        # 图片发不出去（没装 cryptography / 没有 context_token）→ 至少给个文字线索
        hint = self.notifier.last_error or "未知原因"
        self._last_qr_note = f"二维码发送失败：{hint}"
        self.logger.error(f"[watchdog] {self._last_qr_note}")
        await self._ilink_text(
            f"📱 QQ 需要重新登录，但二维码图片发送失败：{hint}\n"
            f"二维码文件：{path}\n登录失败可手动查看该文件。")
        return ""

    # 通知文案
    # ------------------------------------------------------------------
    def _notify_enabled(self, key: str) -> bool:
        notify = self.cfg.get("notify", {}) or {}
        return bool(notify.get(key, True))

    def _mark_offline(self, reason: str) -> bool:
        """记一笔掉线，并唤醒观察任务。返回 True 表示**这是本轮掉线的开始**。

        「断开」之后紧跟的第一次「连接失败」属于同一轮掉线，只更新原因。
        注意：这里**不发通知** —— 是否值得打扰用户由 `_monitor()` 按观察期判定。
        """
        if self._offline_since:
            # 已经在掉线状态：只更新原因（连续失败不重复通知）
            self._offline_reason = reason
            return False
        self._offline_since = time.time()
        self._offline_reason = reason
        self._offline_count += 1
        self.logger.warning(f"[watchdog] 判定掉线：{reason}")
        return True

    def _launch_note(self) -> str:
        """把框架的拉起结果整理成一行说明。"""
        launcher = getattr(self.client, "launcher", None)
        if launcher is None:
            return ""
        info = launcher.status()
        if not info["attempts"]:
            return "本次未触发服务器拉起"
        last = info["last_result"] or {}
        state = "成功执行" if last.get("started") else f"失败（{last.get('reason') or '未知'}）"
        note = (f"拉起服务器：mode={info['mode']} command={info['command']} "
                f"第 {info['attempts']} 次，{state}")
        if not last.get("started") and last.get("hint"):
            note += f"\n{last['hint'].splitlines()[0]}"
        return note

    def _notify_ready(self) -> bool:
        """微信通道是否真的能发（用于「发不了就别刷日志」）。"""
        n = getattr(self, "notifier", None)
        return bool(n is not None and n.enabled and n.credentials)

    def _channel_problem(self) -> str:
        n = getattr(self, "notifier", None)
        if n is None:
            return "插件未初始化完成"
        return (f"微信通道不可用：{n.last_error}"
                if n.last_error else "微信通道不可用（未完成配置）")

    async def _notify_offline(self):
        if self._shutting_down or not hasattr(self, "notifier"):
            return
        launcher = getattr(self.client, "launcher", None)
        status = launcher.status() if launcher else {}
        command = status.get("command", "（未配置）")
        mode = status.get("mode", "?")
        enabled = status.get("enabled", False)

        lines = [
            f"❌ QQ 服务器掉线",
            f"时间：{_fmt_time(self._offline_since)}",
            f"原因：{self._offline_reason or '未知'}",
            f"累计掉线：第 {self._offline_count} 次",
        ]
        if enabled:
            lines.append(f"处理：正在拉起（{mode}: {command}）")
        else:
            lines.append("⚠️ 服务器拉起已关闭（restart_enabled=false），请手动处理")

        ok = await self._ilink_text("\n".join(lines))
        if not ok:
            # 注意：这里只是「这条没发出去」，状态机必须照常往下走 ——
            # 早期版本直接 return，导致 _offline_since 永不复位、每轮掉线都重发一次。
            self.logger.error(f"[watchdog] 掉线微信通知发送失败：{self.notifier.last_error}")
            return
        self._notified_offline = True

        # 拉起失败告警由状态机在 `_restart_ncqq()` 里发（它知道是哪一轮、失败原因），
        # 这里只负责「掉线」这一条。

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _status_text(self) -> str:
        client = self.client
        launcher = getattr(client, "launcher", None)
        info = launcher.status() if launcher else {}
        ws_connected = bool(getattr(client, "ws_connected", False))
        notifier = self.notifier.status()
        latest_qr = self._find_latest_qr()

        lines = [
            "🐶 ncqq_watchdog",
            f"WebSocket：{'已连接' if ws_connected else '未连接'}",
            f"掉线中：{'是（' + _fmt_duration(time.time() - self._offline_since) + '）' if self._offline_since else '否'}"
            f" · 累计 {self._offline_count} 次",
            f"恢复流程：{'第 ' + str(self._cycle) + ' 轮重启/等连接中' if self._cycle else '空闲'}",
            f"健康检查：{'每 ' + str(int(self.cfg.get('health_interval', 5) or 5)) + 's' if self.cfg.get('health_check', True) else '关闭'}"
            f" · 连续失败 {self._health_failures}",
            f"每轮等待登录：{self._connect_timeout():.0f}s · "
            + (f"最多 {int(self.cfg.get('max_restart_cycles'))} 轮"
               if int(self.cfg.get("max_restart_cycles", 0) or 0) > 0
               else "不限次数"),
            "",
            "— 服务器拉起 —",
            f"启用：{'是' if info.get('enabled') else '否'}（{info.get('mode')}）",
            f"命令：{info.get('command')}",
            f"尝试：{info.get('attempts', 0)} 次"
            + (f" · 最近：{'成功' if (info.get('last_result') or {}).get('started') else '失败'}"
               if info.get("last_result") else ""),
            "",
            "— 微信通知 —",
            f"配置：{'已就绪' if notifier['configured'] else '未完成'}"
            f"（account={notifier['account'] or '-'}）",
            f"接收人：{notifier['target'] or '（未设置）'}"
            f" · context_token：{'有' if notifier['has_context'] else '无'}",
            f"发送：成功 {notifier['sends_ok']} / 失败 {notifier['sends_failed']}",
        ]
        if notifier["last_error"]:
            lines.append(f"最近错误：{notifier['last_error']}")
        if latest_qr:
            try:
                mtime = Path(latest_qr).stat().st_mtime
                lines.append(f"二维码：{latest_qr}（{_fmt_time(mtime)}）")
            except OSError:
                lines.append(f"二维码：{latest_qr}")
        else:
            lines.append(f"二维码：未找到（候选 {'/'.join(self._qr_paths())}）")
        if self._last_qr_note:
            lines.append(f"最近二维码动作：{self._last_qr_note}")
        last = info.get("last_result") or {}
        if last.get("hint"):
            lines.append("")
            lines.append("⚠️ 拉起权限不足，需要 root 处理：")
            lines.extend(last["hint"].splitlines())
        return "\n".join(lines)
