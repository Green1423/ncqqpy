"""启动重启的「已在线则跳过」与健康检查降噪。

背景：登录态在 NapCat 进程里，重启 `ncqq` = 把已登录的 QQ 踢下线，下次登录
还要重新扫码。看门狗原来的启动重启是无条件的 —— 每次框架进程重启，哪怕
WebSocket 已经连上、`get_status` 也显示 online+good，仍会重启服务端，用户看到
的就是「登上以后莫名其妙重连」。

现在的行为：

1. `startup_restart_check=true`（默认）时，启动重启前先探测一次；已在线则跳过；
2. 探测允许重试（`startup_restart_probe_attempts`），避免一次 `get_status` 超时误判；
3. WebSocket 未连接 / online=false / good=false / 探测全失败 → 照常重启；
4. `startup_restart_check=false` → 保持旧的无条件启动重启；
5. 恢复流程运行期间健康检查不重复计数（避免十五秒一条的「账号状态异常」和误触发）。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from napcat.watchdog import Watchdog                         # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


class _Status:
    def __init__(self, online: bool, good: bool):
        self.online = online
        self.good = good


class _Api:
    def __init__(self, result=None, fail_first: int = 0):
        self.result = result
        self.fail_first = fail_first
        self.calls = 0

    async def get_status(self):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise TimeoutError("probe timeout")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Launcher:
    def __init__(self, started: bool = True):
        self.calls = 0
        self.attempts = 0
        self.started = started

    async def launch(self, reason: str = ""):
        self.calls += 1
        self.attempts += 1
        return type("R", (), {"started": self.started, "reason": "",
                              "hint": ""})()

    def _cooldown_remaining(self) -> float:
        return 0.0


class _Client:
    def __init__(self, connected: bool = True, status=None, fail_first: int = 0,
                 launcher=None):
        self._ws_connected = connected
        self.api = _Api(status, fail_first)
        self.launcher = launcher

    @property
    def ws_connected(self):
        return self._ws_connected


def make_watchdog(tmp: Path, cfg: dict, client: _Client) -> Watchdog:
    os.environ["NCQQ_WATCHDOG_DATA_DIR"] = str(tmp)
    base = {"wechat": {"enabled": False},
            "notify": {"on_offline": False, "on_qrcode": False,
                       "on_login_ok": False, "on_launch_failed": False},
            "health_check": False}
    base.update(cfg)
    wd = Watchdog(client, base)
    wd.notifier.send_text = lambda *a, **k: asyncio.sleep(0, result=True)
    wd.notifier.send_image = lambda *a, **k: asyncio.sleep(0, result=True)
    return wd


async def test_probe(tmp: Path):
    print("\n[1] 启动前探测：已在线才跳过")
    wd = make_watchdog(tmp, {}, _Client(True, _Status(True, True)))
    check("online+good → 跳过重启", await wd._should_skip_startup_restart())

    wd = make_watchdog(tmp, {}, _Client(False, _Status(True, True)))
    check("WebSocket 未连接 → 不跳过", not await wd._should_skip_startup_restart())

    for online, good, label in ((False, True, "online=false"),
                                (True, False, "good=false"),
                                (False, False, "都 false")):
        wd = make_watchdog(tmp, {"startup_restart_probe_attempts": 1},
                           _Client(True, _Status(online, good)))
        check(f"{label} → 不跳过", not await wd._should_skip_startup_restart())

    wd = make_watchdog(tmp, {"startup_restart_probe_attempts": 1},
                       _Client(True, RuntimeError("boom")))
    check("探测抛错 → 不跳过", not await wd._should_skip_startup_restart())

    wd = make_watchdog(tmp, {"startup_restart_check": False},
                       _Client(True, _Status(True, True)))
    check("显式关闭检查 → 不跳过（旧行为）",
          not await wd._should_skip_startup_restart())

    wd = make_watchdog(tmp, {"startup_restart_probe_attempts": 2,
                             "startup_restart_probe_interval": 0.01},
                       _Client(True, _Status(True, True), fail_first=1))
    check("首次超时、重试成功 → 跳过", await wd._should_skip_startup_restart())
    check("确实重试了 2 次", wd.client.api.calls == 2, str(wd.client.api.calls))

    wd = make_watchdog(tmp, {"startup_restart_probe_attempts": 1},
                       _Client(True, _Status(True, True)))
    wd._offline_since = 123.0
    check("启动期间已掉线 → 必须重启", not await wd._should_skip_startup_restart())
    await wd._drain_tasks()


async def test_loop_skip(tmp: Path):
    print("\n[2] 常驻循环：在线时真的不拉起服务端")
    launcher = _Launcher()
    client = _Client(True, _Status(True, True), launcher=launcher)
    wd = make_watchdog(tmp / "skip", {
        "restart_on_startup": True, "startup_restart_delay": 0.05,
        "startup_restart_probe_attempts": 1, "health_check": False,
    }, client)
    task = asyncio.create_task(wd._run_loop())
    await asyncio.sleep(0.4)
    check("已在线：没有调用拉起命令", launcher.calls == 0, str(launcher.calls))
    check("已在线：没有恢复流程在跑", not wd._episode_running())
    check("已在线：没有标记掉线", wd._offline_since == 0, str(wd._offline_since))
    wd._shutting_down = True
    wd._wake.set()
    task.cancel()
    with contextlib.suppress(BaseException):
        await task

    launcher2 = _Launcher()
    client2 = _Client(False, _Status(True, True), launcher=launcher2)
    wd2 = make_watchdog(tmp / "restart", {
        "restart_on_startup": True, "startup_restart_delay": 0.05,
        "startup_restart_probe_attempts": 1, "health_check": False,
        "max_restart_cycles": 1, "connect_timeout": 0.15,
        "qrcode_after_launch_delay": 0.01, "qrcode_wait_seconds": 0.05,
        "qrcode_poll_interval": 0.01,
    }, client2)
    task2 = asyncio.create_task(wd2._run_loop())
    await asyncio.sleep(0.8)
    check("未连接：启动重启照常执行", launcher2.calls >= 1, str(launcher2.calls))
    wd2._shutting_down = True
    wd2._wake.set()
    task2.cancel()
    with contextlib.suppress(BaseException):
        await task2
    await wd._drain_tasks()
    await wd2._drain_tasks()


async def test_health_noise(tmp: Path):
    print("\n[3] 恢复期间健康检查不重复计数")
    bad = _Client(True, _Status(True, False))
    wd = make_watchdog(tmp / "health", {
        "health_check": True, "health_interval": 0.05,
        "health_max_failures": 1,
    }, bad)
    blocker = asyncio.create_task(asyncio.sleep(5))
    wd._episode_task = blocker
    task = asyncio.create_task(wd._health_loop())
    await asyncio.sleep(0.3)
    check("恢复流程运行时不触发新恢复", not wd._wake.is_set(),
          f"wake={wd._wake.is_set()}")
    check("恢复流程运行时失败计数保持 0", wd._health_failures == 0,
          str(wd._health_failures))
    task.cancel()
    blocker.cancel()
    with contextlib.suppress(BaseException):
        await task
        await blocker

    wd._episode_task = None
    wd._mark_online()
    check("登录成功后健康失败计数清零", wd._health_failures == 0,
          str(wd._health_failures))
    await wd._drain_tasks()


async def test_grace(tmp: Path):
    print("\n[4] 掉线观察期：能自行重连就不重启服务端")
    client = _Client(False, _Status(True, True))
    wd = make_watchdog(tmp / "grace", {"reconnect_grace": 0.25}, client)
    await wd._on_disconnected()
    await asyncio.sleep(0.1)
    check("观察期内不触发重启", not wd._wake.is_set(), f"wake={wd._wake.is_set()}")
    check("观察期任务已登记", wd._grace_task is not None)
    await asyncio.sleep(0.3)
    check("观察期结束且未恢复 → 触发恢复", wd._wake.is_set(),
          f"wake={wd._wake.is_set()}")
    await wd._drain_tasks()

    client2 = _Client(False, _Status(True, True))
    wd2 = make_watchdog(tmp / "grace2", {"reconnect_grace": 0.25}, client2)
    await wd2._on_disconnected()
    client2._ws_connected = True
    await wd2._on_reconnected()
    await asyncio.sleep(0.35)
    check("观察期内自行恢复 → 不触发恢复", not wd2._wake.is_set(),
          f"wake={wd2._wake.is_set()}")
    check("自行恢复后掉线状态清零", wd2._offline_since == 0,
          str(wd2._offline_since))
    await wd2._drain_tasks()

    wd3 = make_watchdog(tmp / "grace3", {"reconnect_grace": 0}, _Client(False))
    await wd3._on_disconnected()
    check("grace=0 → 立即恢复（旧行为）", wd3._wake.is_set())
    await wd3._drain_tasks()

    wd4 = make_watchdog(tmp / "grace4", {"reconnect_grace": 5}, _Client(False))
    wd4._kick_episode("健康检查失败：online=false", grace=False)
    check("健康检查等已确认异常 → 不受观察期影响", wd4._wake.is_set())
    await wd4._drain_tasks()

    client5 = _Client(False)
    wd5 = make_watchdog(tmp / "grace5", {"reconnect_grace": 0.15}, client5)
    await wd5._on_connect_failed(3, ConnectionRefusedError("refused"))
    await asyncio.sleep(0.05)
    check("首次连接失败也进观察期", not wd5._wake.is_set())
    await asyncio.sleep(0.2)
    check("观察期后仍未连上 → 触发恢复", wd5._wake.is_set())
    await wd5._drain_tasks()


async def test_login_notify(tmp: Path):
    print("\n[5] 抖动恢复不发「已登录成功」")
    client = _Client(False, _Status(True, True))
    wd = make_watchdog(tmp / "notify", {
        "reconnect_grace": 0.2,
        "notify": {"on_login_ok": True, "on_offline": False,
                   "on_qrcode": False, "on_launch_failed": False},
    }, client)
    calls = []
    async def fake_ok():
        calls.append(True)
        return True
    wd._notify_login_ok = fake_ok
    await wd._on_disconnected()
    client._ws_connected = True
    await wd._on_reconnected()
    check("观察期内自行恢复：不发登录成功", calls == [], str(calls))
    check("状态已复位", wd._offline_since == 0)

    wd._mark_offline("模拟真实掉线")
    wd._notified_offline = True
    await wd._on_reconnected()
    check("真的掉线过并恢复：发登录成功", calls == [True], str(calls))
    await wd._drain_tasks()


async def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "skip").mkdir()
        (tmp / "restart").mkdir()
        (tmp / "health").mkdir()
        await test_probe(tmp)
        await test_loop_skip(tmp)
        await test_health_noise(tmp)
        for name in ("grace", "grace2", "grace3", "grace4", "grace5"):
            (tmp / name).mkdir()
        await test_grace(tmp)
        (tmp / "notify").mkdir()
        await test_login_notify(tmp)
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
