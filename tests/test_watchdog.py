"""
框架看门狗测试（真起 WebSocket 服务端 + 真执行进程，不打桩网络层）。

跑法：
    cd /ncqqpy && ./venv-arm/bin/python tests/test_watchdog.py
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 看门狗数据目录指到临时目录：否则测试会把状态写进 napcat/data/，污染生产
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="watchdog-data-"))
os.environ.setdefault("NCQQ_WATCHDOG_DATA_DIR", str(_TEST_DATA_DIR))

from napcat.client import NapCatClient          # noqa: E402
from napcat.launcher import ServerLauncher      # noqa: E402
from napcat.watchdog import Watchdog            # noqa: E402

from websockets.asyncio.server import serve     # noqa: E402

PASSED = 0
FAILED = 0


def _plugin_ready(name: str) -> bool:
    """框架仓库单独 checkout 时 `plugins/` 是空的（插件在各自的仓库里）。

    依赖真实插件的用例在这种情况下**跳过**而不是失败：框架自身的回归不该
    因为「没 clone 插件仓库」而变红。把插件 clone 到 `plugins/` 下即恢复全量。
    """
    if (ROOT / "plugins" / name / "__init__.py").exists():
        return True
    print(f"  ⏭️  跳过：{name} 插件不在 plugins/ 下（在它自己的仓库里）")
    return False


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def _logger():
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    lg = logging.getLogger("test.watchdog")
    lg.setLevel(logging.WARNING)
    return lg


async def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_config(path: Path, ws_port: int, restart_command: str) -> Path:
    plugin_dir = path.parent / "plugins"
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "_empty.py").write_text("# 跳过加载\n", encoding="utf-8")
    cfg = {
        "ws_url": f"ws://127.0.0.1:{ws_port}", "token": "",
        "plugin_dir": str(plugin_dir), "superusers": [1],
        "command_prefixes": ["/"], "bound_groups": [], "all_groups": True,
        "api_delay": 0, "offline_hold_timeout": 0.5, "offline_queue_max": 10,
        "restart_enabled": True, "restart_command": restart_command,
        "restart_mode": "launcher", "restart_timeout": 10, "restart_cooldown": 0,
        "reconnect_interval": 0.2, "max_reconnect_interval": 1.0,
        # 这些用例测框架自身行为 → 关掉看门狗，避免两条路径都重启
        "watchdog_enabled": False,
    }
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def make_launcher(path: Path, marker: Path) -> str:
    path.write_text(f"#!/bin/bash\necho call >> {marker}\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return f"bash {path}"


def _kill(clients) -> None:
    """粗暴断开连接（1006 不能主动发，只能掐传输层）。"""
    for ws in list(clients):
        tr = getattr(ws, "transport", None)
        if tr is not None:
            tr.abort()


async def onebot_handler(ws):
    """最小 OneBot 服务端：应答带 echo 的请求（真 NapCat 也应答）。

    on_ws_connected 回调要等 get_login_info 回来才触发，所以 mock 必须应答。
    """
    try:
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            echo = payload.get("echo")
            if echo is not None:
                await ws.send(json.dumps({
                    "status": "ok", "retcode": 0, "echo": echo,
                    "data": {"user_id": 3687155202, "nickname": "玄虥"},
                }, ensure_ascii=False))
    except Exception:
        pass


class _FakeCfg:
    """最小 ConfigManager 替身：实现插件用到的 get/set/setdefault。

    ⚠️ 必须像真实 `ConfigManager` 一样把**点号当路径**（`setdefault("a.b.c", v)`
    要写进嵌套字典）。早期版本按字面键存取，于是插件里
    `cfg.setdefault("summarize.push_budget", …)` 这类逐键默认值在测试里静默失效，
    测出来的行为和线上不一致。
    """

    def __init__(self, data: dict):
        self._data = dict(data)

    def _resolve(self, key, create=False):
        parts = str(key).split(".")
        node = self._data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                if not create:
                    return None, parts[-1]
                nxt = {}
                node[part] = nxt
            node = nxt
        return node, parts[-1]

    def get(self, key, default=None):
        parent, last = self._resolve(key)
        if isinstance(parent, dict):
            return parent.get(last, default)
        return default

    def setdefault(self, key, default):
        parent, last = self._resolve(key, create=True)
        return parent.setdefault(last, default)

    def set(self, key, value):
        parent, last = self._resolve(key, create=True)
        parent[last] = value



async def make_watchdog(tmp: Path, name: str, cfg_data: dict):
    wd = Watchdog(None, dict(cfg_data), logger=_logger())
    wd._data_dir = tmp / f"data-{name}"
    wd._data_dir.mkdir(parents=True, exist_ok=True)
    wd._qr_sent_path = wd._data_dir / "qrcode_sent_state.json"
    wd._qr_sent_hashes = []
    wd._logged_in_hash = ""
    wd._last_login_notify = 0.0
    wd._qr_reserved = set()
    wd._load_qr_sent()
    return wd


# ---- 1) 连不上：框架拉起只执行一次 ----
async def test_launch_once_on_failure(tmp: Path):
    print("\n[1] 服务器连不上 → 框架拉起只执行一次")
    port = await free_port()
    marker = tmp / "calls.txt"
    command = make_launcher(tmp / "fake_launcher.sh", marker)
    cfg = make_config(tmp / "config1.json", port, command)

    client = NapCatClient(config_path=str(cfg))
    failed = []

    async def on_failed(attempt, exc):
        failed.append((attempt, type(exc).__name__))

    client.on_ws_connect_failed(on_failed)
    runner = asyncio.create_task(client.start())
    await asyncio.sleep(3.0)
    await asyncio.wait_for(client.stop(), timeout=15)
    stopped = True
    try:
        await asyncio.wait_for(asyncio.shield(runner), timeout=10)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        stopped = False
    check("stop() 能正常结束主循环", stopped)
    calls = marker.read_text().split() if marker.exists() else []
    check("拉起命令被执行", len(calls) >= 1, f"calls={len(calls)}")
    check("连续失败只拉起一次", len(calls) == 1, f"calls={len(calls)}")
    check("失败回调只触发一次", len(failed) == 1, f"{failed}")


# ---- 2) 连上 → 断开 → 重连 ----
async def test_disconnect_and_reconnect(tmp: Path):
    print("\n[2] 连上 → 服务端断开 → 重连")
    port = await free_port()
    marker = tmp / "calls2.txt"
    command = make_launcher(tmp / "l2.sh", marker)
    cfg = make_config(tmp / "config2.json", port, command)
    clients = []
    connected = asyncio.Event()

    async def handler(ws):
        clients.append(ws)
        await onebot_handler(ws)

    server = await serve(handler, "127.0.0.1", port)
    events = []

    async def on_connected():
        events.append("connected"); connected.set()

    async def on_disconnected():
        events.append("disconnected")

    async def on_failed(attempt, exc):
        events.append(f"failed{attempt}")

    client = NapCatClient(config_path=str(cfg))
    client.on_ws_connected(on_connected)
    client.on_ws_disconnected(on_disconnected)
    client.on_ws_connect_failed(on_failed)
    runner = asyncio.create_task(client.start())
    try:
        await asyncio.wait_for(connected.wait(), timeout=8)
        check("首次连接成功", True)
        check("连接后 ws_connected=True", client.ws_connected)
        events.clear(); _kill(clients)
        await asyncio.sleep(2.0)
        check("断开回调被触发", "disconnected" in events, f"{events}")
        check("服务端快速恢复时自动重连", "connected" in events, f"{events}")
        calls = marker.read_text().split() if marker.exists() else []
        check("服务端秒回时不触发拉起", len(calls) == 0, f"{calls}")
        events.clear()
        with contextlib.suppress(Exception):
            server.close(); await server.wait_closed()
        _kill(clients); clients.clear()
        await asyncio.sleep(6.0)
        check("真下线触发 disconnected", "disconnected" in events, f"{events}")
        check("连不上触发 failed", any(e.startswith("failed") for e in events), f"{events}")
        calls = marker.read_text().split() if marker.exists() else []
        check("真下线触发拉起（且只一次）", len(calls) == 1, f"{calls}")
    finally:
        await asyncio.wait_for(client.stop(), timeout=15)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(runner), timeout=10)
        with contextlib.suppress(Exception):
            server.close(); await server.wait_closed()


# ---- 3) 从未连上不触发断开 ----
async def test_no_disconnect_without_connection(tmp: Path):
    print("\n[3] 从未连上 → 不应触发「断开」回调")
    port = await free_port()
    command = make_launcher(tmp / "l3.sh", tmp / "calls3.txt")
    cfg = make_config(tmp / "config3.json", port, command)
    seen = {"disconnected": 0, "connected": 0}

    async def on_disconnected():
        seen["disconnected"] += 1

    async def on_connected():
        seen["connected"] += 1

    client = NapCatClient(config_path=str(cfg))
    client.on_ws_disconnected(on_disconnected)
    client.on_ws_connected(on_connected)
    runner = asyncio.create_task(client.start())
    await asyncio.sleep(1.5)
    await asyncio.wait_for(client.stop(), timeout=15)
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(asyncio.shield(runner), timeout=10)
    check("没有 disconnected 回调", seen["disconnected"] == 0, str(seen))
    check("没有 connected 回调", seen["connected"] == 0, str(seen))


# ---- 4) 拉起失败 / 冷却 / argv 容错 ----
async def test_launcher_failure_and_cooldown(tmp: Path):
    print("\n[4] 拉起失败、冷却、argv 容错")
    marker = tmp / "calls4.txt"
    script = tmp / "fail.sh"
    script.write_text(f"#!/bin/bash\necho call >> {marker}\nexit 3\n", encoding="utf-8")
    script.chmod(0o755)
    lc = ServerLauncher(command=f"bash {script}", mode="launcher", cooldown=30, timeout=5)
    r = await lc.launch(reason="测试")
    check("命令非 0 退出被判为失败", not r.started, repr(r))
    check("失败原因含退出码", "3" in r.reason, r.reason)
    before = len(marker.read_text().split())
    r2 = await lc.launch(reason="测试2")
    after = len(marker.read_text().split())
    check("冷却期内跳过执行", after == before, f"{before}->{after}")
    check("跳过原因含冷却", "冷却" in r2.reason, r2.reason)
    check("systemd 模式 argv 正确",
          ServerLauncher(command="ncqq", mode="systemd").argv == ["systemctl", "restart", "ncqq"])
    check("launcher 模式 argv 正确",
          ServerLauncher(command="bash /opt/ncqq/launcher.sh", mode="launcher").argv
          == ["bash", "/opt/ncqq/launcher.sh"])
    for wrong in ("systemctl restart ncqq", "systemctl restart ncqq.service",
                  "ncqq.service", "systemctl --user restart ncqq"):
        argv = ServerLauncher(command=wrong, mode="systemd").argv
        check(f"systemd 模式容错 {wrong!r}", argv == ["systemctl", "restart", "ncqq"], str(argv))
    check("默认拉起间隔 180s（3 分钟）",
          ServerLauncher(command="true", mode="launcher").cooldown == 180.0)


# ---- 5) 抖动退避 ----
async def test_flap_backoff(tmp: Path):
    print("\n[5] 「连上就断」抖动 → 退避递增")
    port = await free_port()
    command = make_launcher(tmp / "l5.sh", tmp / "calls5.txt")
    cfg_path = tmp / "config5.json"
    make_config(cfg_path, port, command)
    d = json.loads(cfg_path.read_text(encoding="utf-8"))
    d.update({"restart_enabled": False, "reconnect_interval": 0.2,
              "max_reconnect_interval": 3.0, "short_connection_threshold": 1.0})
    cfg_path.write_text(json.dumps(d), encoding="utf-8")

    async def handler(ws):
        await ws.close()

    server = await serve(handler, "127.0.0.1", port)
    client = NapCatClient(config_path=str(cfg_path))
    runner = asyncio.create_task(client.start())
    try:
        await asyncio.sleep(6.0)
        attempts = client._ws.connect_count
        check("抖动被识别（flap_count > 0）", client._ws.flap_count > 0,
              f"flap={client._ws.flap_count}")
        check("重连次数被退避压住（< 12 次 / 6s）", attempts < 12, f"连了 {attempts} 次")
        check("抖动期间没有反复拉起", client.launcher.attempts == 0,
              f"attempts={client.launcher.attempts}")
        server.close(); await server.wait_closed()
        server2 = await serve(onebot_handler, "127.0.0.1", port)
        ok = await client._ws.wait_stable(timeout=30)
        check("恢复后记录为稳定连接", ok, f"stable={client._ws.stable_connect_count}")
        check("恢复后 flap_count 清零", client._ws.flap_count == 0,
              f"flap={client._ws.flap_count}")
        with contextlib.suppress(Exception):
            server2.close(); await server2.wait_closed()
    finally:
        await asyncio.wait_for(client.stop(), timeout=15)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(runner), timeout=10)
        await asyncio.sleep(1.5)
        with contextlib.suppress(Exception):
            server.close(); await server.wait_closed()


# ---- 6) 恢复状态机端到端 ----
async def test_state_machine_end_to_end(tmp: Path):
    print("\n[6] 恢复状态机：断开 → 重启 → 发码 → 等连接")
    port = await free_port()
    marker = tmp / "calls-sm.txt"
    qr_path = tmp / "sm-qr.png"
    gen = tmp / "sm-gen-qr.py"
    gen.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from napcat.qr_text import text_to_png\n"
        "time.sleep(0.2)\n"
        "text_to_png('https://liteapp.weixin.qq.com/q/sm?qrcode=' + str(time.time()), "
        f"{str(qr_path)!r}, scale=2)\n", encoding="utf-8")
    launcher = tmp / "sm-launcher.sh"
    launcher.write_text(
        "#!/bin/bash\n"
        f"echo call >> {marker}\n"
        f"nohup {sys.executable} {gen} >> {tmp}/sm-gen.log 2>&1 &\n"
        "exit 0\n", encoding="utf-8")
    launcher.chmod(0o755)

    cfg_path = tmp / "config-sm.json"
    make_config(cfg_path, port, f"bash {launcher}")
    d = json.loads(cfg_path.read_text(encoding="utf-8"))
    d["restart_cooldown"] = 0
    data_dir = Path(os.environ["NCQQ_WATCHDOG_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    creds = data_dir / "ilink_credentials.json"
    creds.write_text(json.dumps({"bot_token": "t", "account_id": "a@im.bot",
                                 "base_url": "https://ilinkai.weixin.qq.com"}),
                     encoding="utf-8")
    for stale in data_dir.glob("qrcode_sent_state.json"):
        stale.unlink()
    d["watchdog_enabled"] = True
    d["watchdog"] = {
        "restart_on_startup": False,
        "qrcode_paths": [str(qr_path)],
        "qrcode_after_launch_delay": 0.5,
        "qrcode_wait_seconds": 6,
        "qrcode_upscale": 2,
        "max_restart_cycles": 2,
        "connect_timeout": 12,
        "ilink_retry_timeout": 2,             # 测试缩短重试窗口
        "ilink_retry_interval": 1.0,
        "notify": {"on_offline": True, "on_qrcode": True},
        "wechat": {"enabled": True, "target": "tester@im.wechat",
                   "auto_context": False, "credentials_file": str(creds)},
    }
    cfg_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    if qr_path.exists():
        qr_path.unlink()

    client = NapCatClient(config_path=str(cfg_path))
    images = []
    runner = asyncio.create_task(client.start())
    try:
        wd = client.watchdog
        check("看门狗已随框架启动", wd is not None)
        if wd is None:
            return

        async def fake_image(path, caption="", target=""):
            images.append(str(path)); return True

        async def fake_text(text, target=""):
            return True

        wd.notifier.send_image = fake_image
        wd.notifier.send_text = fake_text
        for _ in range(200):
            if client.launcher.attempts >= 2 and images:
                break
            await asyncio.sleep(0.2)
        check("状态机执行了重启", client.launcher.attempts >= 1,
              f"attempts={client.launcher.attempts}")
        check("重启后发出了二维码", bool(images), f"images={images}")
        check("超时后回到「重启 ncqq」重试", client.launcher.attempts >= 2,
              f"attempts={client.launcher.attempts}")
        check("重试次数受 max_restart_cycles 限制",
              1 <= client.launcher.attempts <= 2, f"attempts={client.launcher.attempts}")
    finally:
        await asyncio.wait_for(client.stop(), timeout=20)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(runner), timeout=10)


# ---- 7) 权限不足要给建议 ----
async def test_permission_failure_is_actionable(tmp: Path):
    print("\n[7] 拉起权限不足 → 给出可操作建议")
    deny = tmp / "deny.sh"
    deny.write_text('#!/bin/bash\necho "Failed to restart ncqq.service: Access denied as '
                    'the requested operation requires interactive authentication." >&2\n'
                    'exit 1\n', encoding="utf-8")
    deny.chmod(0o755)
    lc = ServerLauncher(command=f"bash {deny}", mode="launcher", cooldown=0)
    r = await lc.launch(reason="测试")
    check("权限失败被判为未拉起", not r.started, repr(r))
    check("给出处理建议", bool(r.hint), r.hint)
    check("建议含正确的服务名", "ncqq" in r.hint, r.hint)
    check("建议含两种方案", "polkit" in r.hint and "sudoers" in r.hint, r.hint)
    boom = tmp / "boom.sh"
    boom.write_text('#!/bin/bash\necho "other failure" >&2\nexit 2\n', encoding="utf-8")
    boom.chmod(0o755)
    r3 = await ServerLauncher(command=f"bash {boom}", mode="launcher", cooldown=0).launch(reason="x")
    check("普通失败不误报权限建议", not r3.hint, r3.hint)


# ---- 8) 拉起前清 cache ----
async def test_cache_cleared_before_launch(tmp: Path):
    print("\n[8] 拉起前清空 cache")
    cache = tmp / "napcat" / "cache"
    cache.mkdir(parents=True)
    (cache / "qrcode.png").write_bytes(b"old-qr" * 40)
    marker = tmp / "calls-cache.txt"
    script = tmp / "cache-l.sh"
    script.write_text(f"#!/bin/bash\necho call >> {marker}\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    lc = ServerLauncher(command=f"bash {script}", mode="launcher", cooldown=0,
                        clear_cache_dir=str(cache))
    r = await lc.launch(reason="测试")
    check("拉起成功执行", r.started, repr(r))
    check("cache 被清空", not cache.exists(), str(cache))
    check("拉起脚本仍然执行", marker.exists())
    check("目录不存在时视为无需清理",
          ServerLauncher(command="true", mode="launcher", cooldown=0,
                         clear_cache_dir=str(tmp / "never")).clear_cache() is True)
    for bad, expect in {"": "未配置", "/": "过浅", "/etc": "过浅",
                        "/root": "过浅", "relative/path": "绝对路径"}.items():
        reason = ServerLauncher(command="true", mode="launcher",
                                clear_cache_dir=bad)._refuse_reason()
        check(f"拒绝清理 {bad!r}", expect in reason, f"{reason!r}")
    # 下面两条校验的是**本部署**的 config.json（含密钥，不进仓库）：
    # fresh clone 里没有这个文件 → 跳过，别让框架回归依赖部署配置。
    if (ROOT / "config.json").exists():
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        check("config.json 配置了清理目录",
              cfg.get("restart_clear_cache_dir") == "/opt/ncqq/napcat/cache",
              str(cfg.get("restart_clear_cache_dir")))
        check("config.json 拉起间隔 3 分钟",
              float(cfg.get("restart_cooldown", 0)) == 180.0,
              str(cfg.get("restart_cooldown")))
    else:
        print("  ⏭️  跳过：本部署 config.json 不存在（fresh clone）")


# ---- 9) 登录成功反馈 ----
async def test_login_feedback(tmp: Path):
    print("\n[9] 登录成功 → ilink 反馈")
    qr_path = tmp / "fb-qr.png"
    data_dir = Path(os.environ["NCQQ_WATCHDOG_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "ilink_credentials.json").write_text(json.dumps(
        {"bot_token": "t", "account_id": "a@im.bot",
         "base_url": "https://ilinkai.weixin.qq.com"}), encoding="utf-8")
    from napcat.qr_text import text_to_png
    text_to_png("https://liteapp.weixin.qq.com/q/fb?qrcode=X&bot_type=3", qr_path, scale=2)
    wd = await make_watchdog(tmp, "fb", {
        "enabled": True, "qrcode_paths": [str(qr_path)],
        "notify": {"on_login_ok": True, "on_qrcode": True},
        "wechat": {"enabled": True, "target": "tester@im.wechat", "auto_context": False,
                   "credentials_file": str(data_dir / "ilink_credentials.json")}})
    sent = []

    async def fake_text(text, target=""):
        sent.append(text); return True

    wd.notifier.send_text = fake_text
    wd._offline_since = time.time() - 30
    wd._offline_count = 1
    wd._notified_offline = True          # 本轮确实发过「掉线」通知
    await wd._on_reconnected()
    check("恢复后发出「登录成功」反馈", bool(sent), f"{sent}")
    check("反馈文案含登录成功", bool(sent) and "登录成功" in sent[0], f"{sent[:1]}")
    check("恢复后状态复位", wd._offline_since == 0, f"{wd._offline_since}")
    sent.clear()
    await wd._on_reconnected()
    check("已在线时不重复反馈", sent == [], f"{sent}")
    wd._offline_since = time.time() - 30
    wd._notified_offline = True
    wd._last_login_notify = time.time()
    await wd._on_reconnected()
    check("冷却期内不重复反馈", sent == [], f"{sent}")
    await wd._drain_tasks()


# ---- 10) 启动后重启一次 ----
async def test_restart_on_startup(tmp: Path):
    print("\n[10] 启动后自动执行一次重启")
    tmp_i = tmp / "su-on"
    tmp_i.mkdir(parents=True, exist_ok=True)
    marker = tmp_i / "calls.txt"
    cmd = make_launcher(tmp_i / "l.sh", marker)
    cfg = tmp_i / "c.json"
    make_config(cfg, await free_port(), cmd)
    d = json.loads(cfg.read_text(encoding="utf-8"))
    d["restart_cooldown"] = 0
    d["watchdog_enabled"] = True
    d["watchdog"] = {
        "restart_on_startup": True, "startup_restart_delay": 0.5,
        "qrcode_after_launch_delay": 0.2, "qrcode_wait_seconds": 2,
        "max_restart_cycles": 1, "connect_timeout": 3,
        "ilink_retry_timeout": 1, "ilink_retry_interval": 0.2,
        "notify": {"on_offline": False, "on_qrcode": False, "on_login_ok": False},
        "wechat": {"enabled": False},
    }
    cfg.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    client = NapCatClient(config_path=str(cfg))
    runner = asyncio.create_task(client.start())
    try:
        await asyncio.sleep(4.0)
        calls = marker.read_text().split() if marker.exists() else []
        check("启动后自动执行了重启", len(calls) >= 1, f"calls={len(calls)}")
        check("重启次数有界（不会刷屏）", len(calls) <= 2, f"calls={len(calls)}")
    finally:
        await asyncio.wait_for(client.stop(), timeout=15)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(runner), timeout=10)
    wd = await make_watchdog(tmp, "su-default", {"wechat": {"enabled": False}})
    check("默认开启启动重启", wd.cfg.get("restart_on_startup") is True,
          repr(wd.cfg.get("restart_on_startup")))
    check("默认启动延迟 10s",
          float(wd.cfg.get("startup_restart_delay")) == 10.0,
          repr(wd.cfg.get("startup_restart_delay")))
    check("显式关闭生效",
          Watchdog(None, {"restart_on_startup": False},
                   logger=_logger()).cfg.get("restart_on_startup") is False)
    await wd._drain_tasks()


# ---------------------------------------------------------------------------
# 11) iLink 发送失败 → 超时前每 5 秒重试
# ---------------------------------------------------------------------------
async def test_ilink_retry(tmp: Path):
    print("\n[11] iLink 失败重试（超时前每 N 秒一次）")
    data_dir = Path(os.environ["NCQQ_WATCHDOG_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    creds = data_dir / "ilink_credentials.json"
    creds.write_text(json.dumps({"bot_token": "t", "account_id": "a@im.bot",
                                 "base_url": "https://ilinkai.weixin.qq.com"}),
                     encoding="utf-8")
    wd = await make_watchdog(tmp, "retry", {
        "enabled": True,
        "ilink_retry_timeout": 10,
        "ilink_retry_interval": 1.0,
        "wechat": {"enabled": True, "target": "tester@im.wechat",
                   "auto_context": False, "credentials_file": str(creds)},
    })
    check("重试间隔读的是配置", wd._retry_interval() == 1.0, str(wd._retry_interval()))
    check("重试超时读的是配置", wd._retry_timeout() == 10, str(wd._retry_timeout()))

    # (a) 前两次失败、第三次成功 → 应该在超时前重试并最终成功
    calls = {"n": 0}

    async def flaky(text, target=""):
        calls["n"] += 1
        if calls["n"] < 3:
            wd.notifier.last_error = f"模拟失败#{calls['n']}"
            return False
        return True

    wd.notifier.send_text = flaky
    started = time.time()
    ok = await wd._ilink_text("测试消息")
    elapsed = time.time() - started
    check("失败后重试直到成功", ok, f"calls={calls['n']}")
    check("重试了 3 次", calls["n"] == 3, f"calls={calls['n']}")
    check("间隔生效（≈2×1s）", 1.5 <= elapsed < 6, f"elapsed={elapsed:.2f}s")

    # (b) 一直失败 → 到超时就放弃，不会无限重试
    calls["n"] = 0

    async def always_fail(text, target=""):
        calls["n"] += 1
        wd.notifier.last_error = "模拟持续失败"
        return False

    wd.notifier.send_text = always_fail
    wd.cfg["ilink_retry_timeout"] = 2.0
    started = time.time()
    ok = await wd._ilink_text("测试消息")
    elapsed = time.time() - started
    check("持续失败最终放弃", not ok)
    check("放弃发生在超时附近", 1.8 <= elapsed < 6, f"elapsed={elapsed:.2f}s")
    check("重试次数有界", 1 <= calls["n"] <= 8, f"calls={calls['n']}")

    # (c) 配置类问题（没 target）不应重试空转
    calls["n"] = 0
    wd.notifier.target = ""
    wd.notifier.config["target"] = ""

    from napcat.ilink.notifier import Notifier as _N
    wd.notifier.send_text = _N.send_text.__get__(wd.notifier, _N)   # 恢复真实现
    started = time.time()
    ok = await wd._ilink_text("测试消息")
    check("配置缺失时立即返回（不重试）", not ok, f"ok={ok}")
    check("配置缺失时不空转", time.time() - started < 1.0,
          f"elapsed={time.time() - started:.2f}s")

    # (d) 图片发送同样带重试
    wd.notifier.target = "tester@im.wechat"
    wd.notifier.config["target"] = "tester@im.wechat"
    from napcat.qr_text import text_to_png
    img = tmp / "retry.png"
    text_to_png("https://liteapp.weixin.qq.com/q/r?qrcode=R&bot_type=3", img, scale=2)
    calls["n"] = 0

    async def img_flaky(path, caption="", target=""):
        calls["n"] += 1
        return calls["n"] >= 2

    wd.notifier.send_image = img_flaky
    ok = await wd._ilink_image(img, "caption")
    check("图片失败后也会重试", ok and calls["n"] == 2, f"calls={calls['n']}")

    await wd._drain_tasks()



# ---------------------------------------------------------------------------
# 12) 二维码靠哈希比对判定「可以发了」；发出后重连固定 5s
# ---------------------------------------------------------------------------
async def test_qrcode_hash_detection_and_scan_retry(tmp: Path):
    print("\n[12] 二维码按哈希比对取码 + 发出后固定 5s 重连")
    qr_path = tmp / "hash-qr.png"
    data_dir = Path(os.environ["NCQQ_WATCHDOG_DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    creds = data_dir / "ilink_credentials.json"
    creds.write_text(json.dumps({"bot_token": "t", "account_id": "a@im.bot",
                                 "base_url": "https://ilinkai.weixin.qq.com"}),
                     encoding="utf-8")
    from napcat.qr_text import text_to_png

    wd = await make_watchdog(tmp, "hash", {
        "enabled": True,
        "qrcode_paths": [str(qr_path)],
        "qrcode_poll_interval": 0.2,
        "qrcode_wait_seconds": 5,
        "qrcode_upscale": 2,
        "qrcode_scan_retry_interval": 5.0,
        "wechat": {"enabled": True, "target": "tester@im.wechat",
                   "auto_context": False, "credentials_file": str(creds)},
    })
    sent = []

    async def fake_image(path, caption="", target=""):
        sent.append(str(path)); return True

    async def fake_text(text, target=""):
        return True

    wd.notifier.send_image = fake_image
    wd.notifier.send_text = fake_text

    # (a) 第一次见到某张码 → 哈希与「上次（空）」不同 → 可发
    text_to_png("https://liteapp.weixin.qq.com/q/h?qrcode=A&bot_type=3", qr_path, scale=2)
    latest, mtime, digest = await wd._wait_qrcode(max_wait=3)
    check("首次见到二维码即可取到", bool(latest) and bool(digest), f"{latest}")
    check("取到后记住了哈希", wd._qr_last_hash == digest, wd._qr_last_hash[:12])

    # (b) 内容不变 → 哈希相同 → 不该重复取（等到超时）
    started = time.time()
    latest2, _, _ = await wd._wait_qrcode(max_wait=1.0)
    elapsed = time.time() - started
    check("哈希未变时不重复取码", not latest2, f"latest={latest2}")
    check("哈希未变时等到超时", elapsed >= 0.9, f"elapsed={elapsed:.2f}s")

    # (c) 内容改变 → 哈希不同 → 立刻可取（不等超时）
    text_to_png("https://liteapp.weixin.qq.com/q/h?qrcode=B&bot_type=3", qr_path, scale=2)
    started = time.time()
    latest3, _, digest3 = await wd._wait_qrcode(max_wait=5)
    elapsed = time.time() - started
    check("哈希变化后立刻取到", bool(latest3), f"latest={latest3}")
    check("没有等到超时就返回", elapsed < 2.0, f"elapsed={elapsed:.2f}s")
    check("哈希已更新", digest3 != digest and wd._qr_last_hash == digest3)

    # (d) 发出二维码后进入「等扫码」模式：重连固定间隔
    class _FakeWs:
        awaiting_qr_scan = False
        qr_scan_retry_interval = 0.0

    class _FakeClient:
        _ws = _FakeWs()

    wd.client = _FakeClient()
    wd._set_scan_waiting(True)
    check("发出二维码后进入等扫码模式", wd.client._ws.awaiting_qr_scan)
    check("等扫码期间重连间隔来自配置",
          wd.client._ws.qr_scan_retry_interval == 5.0,
          str(wd.client._ws.qr_scan_retry_interval))
    wd._mark_online()
    check("登录成功后退出等扫码模式", not wd.client._ws.awaiting_qr_scan)

    # (e) 框架侧：等扫码期间退避被固定间隔取代
    from napcat.client import WsHandler
    h = WsHandler("ws://x", reconnect_interval=5.0, max_reconnect_interval=60.0)
    h.qr_scan_retry_interval = 5.0
    backoff = [h._retry_delay(i) for i in range(1, 6)]
    check("未等扫码时退避会递增到 60s", backoff[-1] == 60.0, str(backoff))
    h.awaiting_qr_scan = True
    fixed = max(1.0, float(h.qr_scan_retry_interval or 5.0))
    check("等扫码期间固定 5s（不退避）", fixed == 5.0, str(fixed))

    await wd._drain_tasks()



# ---------------------------------------------------------------------------
# 13) 每 5s 主动 get_status，要求 online 且 good；不满足则走同款恢复链路
# ---------------------------------------------------------------------------
async def test_health_check_triggers_recovery(tmp: Path):
    print("\n[13] 主动 get_status 健康检查 → 不健康则触发同款恢复")
    wd = await make_watchdog(tmp, "health", {
        "enabled": True,
        "health_check": True,
        "health_interval": 0.3,
        "health_max_failures": 2,
        "wechat": {"enabled": False},
    })
    check("默认每 5s 检查一次",
          float(Watchdog(None, {"wechat": {"enabled": False}},
                         logger=_logger()).cfg.get("health_interval")) == 5.0)

    class _Status:
        def __init__(self, online, good):
            self.online, self.good = online, good

    class _Api:
        def __init__(self, result):
            self.result = result
            self.calls = 0

        async def get_status(self):
            self.calls += 1
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    class _Client:
        def __init__(self, result):
            self.api = _Api(result)
            self._ws_connected = True

        @property
        def ws_connected(self):
            return self._ws_connected

    # (a) online/good 都为 true → 健康
    wd.client = _Client(_Status(True, True))
    ok, detail = await wd._probe_status()
    check("online+good 视为健康", ok, detail)

    # (b) 任一不为 true → 不健康
    for online, good, label in ((False, True, "online=False"),
                                (True, False, "good=False"),
                                (False, False, "都 False")):
        wd.client = _Client(_Status(online, good))
        ok2, detail2 = await wd._probe_status()
        check(f"{label} 视为不健康", not ok2, detail2)
        check(f"{label} 说明里带上两项取值",
              "online=" in detail2 and "good=" in detail2, detail2)

    # (c) get_status 抛错 / 未连接 → 也算不健康
    wd.client = _Client(RuntimeError("boom"))
    ok3, detail3 = await wd._probe_status()
    check("调用失败视为不健康", not ok3, detail3)
    check("说明里含异常类型", "RuntimeError" in detail3, detail3)

    # (d) 连续失败到阈值 → 触发「断线同款」链路
    wd.cfg["health_interval"] = 0.2
    wd.cfg["health_max_failures"] = 2
    wd.client = _Client(_Status(True, False))
    task = asyncio.create_task(wd._health_loop())
    await asyncio.sleep(1.0)
    task.cancel()
    with contextlib.suppress(BaseException):
        await task
    check("不健康时触发了恢复流程", wd._wake.is_set(),
          f"wake={wd._wake.is_set()} trigger={wd._trigger!r}")
    check("触发原因标注为健康检查",
          "健康检查失败" in (wd._trigger or ""), repr(wd._trigger))

    # (e) 单次抖动不该触发（阈值 3）
    wd2 = await make_watchdog(tmp, "health2", {
        "enabled": True, "health_check": True,
        "health_interval": 0.2, "health_max_failures": 3,
        "wechat": {"enabled": False},
    })
    wd2.client = _Client(_Status(False, False))
    task2 = asyncio.create_task(wd2._health_loop())
    await asyncio.sleep(0.5)
    task2.cancel()
    with contextlib.suppress(BaseException):
        await task2
    check("未达阈值不触发恢复", not wd2._wake.is_set(),
          f"failures={wd2._health_failures} wake={wd2._wake.is_set()}")

    await wd._drain_tasks()
    await wd2._drain_tasks()



# ---------------------------------------------------------------------------
# 14) 群消息 @全体成员；次数不足则补醒目提示
# ---------------------------------------------------------------------------
async def test_bupt_news_at_all(tmp: Path):
    if not _plugin_ready("bupt_news"):
        return
    print("\n[14] 群推送 @全体成员；次数不足补提示")
    from plugins.bupt_news.main import (
        BuptNewsPlugin, _parse_at_all_remain, _AT_ALL_EXHAUSTED_HINT)
    from napcat.message import MessageChain

    # (a) 返回字段解析：既要拿到「能不能用」，也要拿到剩余次数
    for data, want in (({"can_at_all": True}, True),
                       ({"can_at_all": False}, False),
                       ({"remain_at_all_count_for_group": 3}, True),
                       ({"remain_at_all_count_for_group": 0}, False),
                       ({"remain_at_all_count_for_uin": "2"}, True),
                       ({"remain_at_all_count_for_group": "x"}, None),
                       ({}, None), (None, None), ({"unknown": 5}, None)):
        got = _parse_at_all_remain(data)
        check(f"解析 {data} → can_at_all={want}", got.can_at_all is want,
              f"got={got!r}")

    # (a2) NapCat 的真实返回：两个剩余次数都要解析出来
    napcat = {"can_at_all": True, "remain_at_all_count_for_group": 5,
              "remain_at_all_count_for_uin": 20}
    got = _parse_at_all_remain(napcat)
    check("解析出本群剩余次数", got.group_remain == 5, repr(got))
    check("解析出账号剩余次数", got.uin_remain == 20, repr(got))
    check("可用时 exhausted 为假", got.exhausted is False, repr(got))
    check("日志摘要同时含可用性与两个次数",
          "可用" in got.summary and "5" in got.summary and "20" in got.summary,
          got.summary)

    # (a3) 服务端说不能 @all 时，以 can_at_all 为准（不拿剩余次数反推）
    got = _parse_at_all_remain({"can_at_all": False,
                                "remain_at_all_count_for_group": 0,
                                "remain_at_all_count_for_uin": 0})
    check("不可用时 exhausted 为真", got.exhausted is True, repr(got))
    check("不可用时的摘要写明不可用",
          got.summary.startswith("不可用") and "0" in got.summary, got.summary)
    check("查不到时摘要说明原因",
          _parse_at_all_remain({}).summary == "未知（接口未返回可用字段）",
          _parse_at_all_remain({}).summary)

    # (a4) 接口拿不到时绝不能当成「已用尽」（否则每轮都刷提示）
    check("未知配额不判为用尽",
          _parse_at_all_remain({}).exhausted is False,
          repr(_parse_at_all_remain({})))

    # (a5) 真值为假是个陷阱：can_at_all=False 的对象 bool() 为 False，
    # 用 `q or 默认值` 兜底会把「已用尽」悄悄换成「不知道」而吞掉提示。
    check("已用尽的配额对象 bool() 为假（所以必须显式判 None）",
          bool(_parse_at_all_remain({"can_at_all": False})) is False,
          repr(_parse_at_all_remain({"can_at_all": False})))

    # (b) @全体成员 挂在消息最前面
    chain = BuptNewsPlugin._prefix_at_all(MessageChain().text("正文"))
    segs = chain.build()
    check("前缀段是 at all",
          segs and segs[0]["type"] == "at" and segs[0]["data"]["qq"] == "all",
          str(segs[:1]))
    check("正文仍然保留", "正文" in chain.to_cq(), chain.to_cq())

    # (c) 三种配额下的推送行为
    wd = Watchdog(None, {"wechat": {"enabled": False}}, logger=_logger())
    plugin = BuptNewsPlugin(api=None, event_bus=None, timer=None, cmd_manager=None,
                            perm_manager=None, file_manager=None, logger=_logger(),
                            groups=None, cfg=_FakeCfg({"at_all": True}))
    sent: list[str] = []

    class _Api:
        def __init__(self):
            self.mode = "ok"
            self.quota_calls: list[str] = []

        async def get_group_at_all_remain(self, gid):
            self.quota_calls.append(str(gid))
            if self.mode == "ok":
                return {"can_at_all": True, "remain_at_all_count_for_group": 4,
                        "remain_at_all_count_for_uin": 9}
            if self.mode == "out":
                return {"can_at_all": False, "remain_at_all_count_for_group": 0,
                        "remain_at_all_count_for_uin": 0}
            raise RuntimeError("boom")

        async def send_group_msg(self, gid, msg, **kw):
            sent.append(msg.to_cq() if hasattr(msg, "to_cq") else str(msg))

        async def send_private_msg(self, uid, msg, **kw):
            sent.append(msg.to_cq() if hasattr(msg, "to_cq") else str(msg))

    api = _Api()
    plugin.api = api
    plugin._int_list = lambda key: [123] if key == "groups" else []
    plugin._split_chain = lambda chain: chain

    for mode, label in (("ok", "有次数"), ("out", "已用尽"), ("bad", "查不到")):
        api.mode = mode
        sent.clear()
        api.quota_calls.clear()
        await plugin._push_chain(MessageChain().text("通知正文"))
        check(f"{label} → 推送前确实查过 @全体成员 配额",
              api.quota_calls == ["123"], f"quota_calls={api.quota_calls}")
        if mode == "ok":
            check("有次数 → 首条带 @全体成员",
                  len(sent) == 1 and "[CQ:at,qq=all]" in sent[0], f"{sent}")
        elif mode == "out":
            check("已用尽 → 不 @all，但补一条醒目提示",
                  len(sent) == 2 and "[CQ:at,qq=all]" not in sent[0]
                  and _AT_ALL_EXHAUSTED_HINT in sent[1], f"{sent}")
        else:
            check("查不到 → 既不 @all 也不刷提示",
                  len(sent) == 1 and "[CQ:at,qq=all]" not in sent[0], f"{sent}")

    # (d) 多群推送：每个群只该有**一个** @all（回归：共享分段曾被反复加前缀）
    api.mode = "ok"
    plugin._int_list = lambda key: [111, 222, 333] if key == "groups" else []
    sent.clear()
    await plugin._push_chain(MessageChain().text("通知正文"))
    counts = [s.count("[CQ:at,qq=all]") for s in sent]
    check("三个群各收到一条消息", len(sent) == 3, f"sent={len(sent)}")
    check("每个群只带一个 @全体成员", counts == [1, 1, 1], f"counts={counts}")

    # (e) 私聊绝不该带 @全体成员（回归：私聊复用了加过前缀的分段）
    sent.clear()
    plugin._int_list = lambda key: [111] if key == "groups" else [999]
    await plugin._push_chain(MessageChain().text("通知正文"))
    group_msgs = [s for s in sent if "[CQ:at,qq=all]" in s]
    check("群消息带 @all、私聊消息不带",
          len(group_msgs) == 1 and len(sent) == 2, f"sent={sent}")

    # (f) 前缀不修改原链（MessageChain 是可变的，必须用 __add__ 生成新链）
    src = MessageChain().text("正文A").text("正文B")
    before = len(src)
    _ = BuptNewsPlugin._prefix_at_all(src)
    check("加前缀不改动原链", len(src) == before, f"{before} -> {len(src)}")

    await wd._drain_tasks()



async def test_unlimited_restart_cycles(tmp: Path):
    print("\n[15] 重启次数默认不限；设上限时到顶即停")
    wd = await make_watchdog(tmp, "unlimited", {"wechat": {"enabled": False}})
    check("默认 max_restart_cycles=0（不限）",
          int(wd.cfg.get("max_restart_cycles", -1)) == 0,
          repr(wd.cfg.get("max_restart_cycles")))
    check("每隔若干轮才发一次微信（不刷屏）",
          wd._round_notify_every() >= 2, str(wd._round_notify_every()))
    check("登录超时默认 60s（1 分钟）",
          wd._connect_timeout() == 60.0, str(wd._connect_timeout()))
    if (ROOT / "config.json").exists():          # 同上：fresh clone 里没有该文件
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        check("config.json 里登录超时也是 60s",
              float(cfg["watchdog"].get("connect_timeout", 0)) == 60.0,
              str(cfg["watchdog"].get("connect_timeout")))
        check("config.json 里也是不限次数",
              int(cfg["watchdog"].get("max_restart_cycles", -1)) == 0,
              str(cfg["watchdog"].get("max_restart_cycles")))
    else:
        print("  ⏭️  跳过：本部署 config.json 不存在（fresh clone）")

    # 设了上限时仍会停：跑一次「永远连不上」的恢复流程，统计轮次
    port = await free_port()
    marker = tmp / "calls-cap.txt"
    cmd = make_launcher(tmp / "cap-l.sh", marker)
    cfg_path = tmp / "config-cap.json"
    make_config(cfg_path, port, cmd)
    d = json.loads(cfg_path.read_text(encoding="utf-8"))
    d["restart_cooldown"] = 0
    d["watchdog_enabled"] = True
    d["watchdog"] = {
        "restart_on_startup": False,
        "qrcode_after_launch_delay": 0.1,
        "qrcode_poll_interval": 0.2,
        "qrcode_wait_seconds": 1,
        "max_restart_cycles": 2,          # 显式上限（每轮恢复流程内）
        "health_check": False,            # 关掉健康检查，避免它再起新一轮
        "connect_timeout": 3,
        "retry_notify_every_rounds": 1,
        "notify": {"on_offline": False, "on_qrcode": False, "on_login_ok": False},
        "wechat": {"enabled": False},
    }
    cfg_path.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    client = NapCatClient(config_path=str(cfg_path))
    runner = asyncio.create_task(client.start())
    try:
        await asyncio.sleep(1.5)
        client.watchdog._kick_episode("测试：模拟掉线")
        await asyncio.sleep(20)
        calls = marker.read_text().split() if marker.exists() else []
        check("设了上限时到顶即停（不会无限重试）",
              1 <= len(calls) <= 2, f"calls={len(calls)}")
        check("停止后不再自动重试（等人工介入或下一条告警）",
              not client.watchdog.cycle_idle() or len(calls) == 2,
              f"calls={len(calls)} cycle={client.watchdog._cycle}")
    finally:
        await asyncio.wait_for(client.stop(), timeout=15)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(runner), timeout=10)
    await wd._drain_tasks()

async def test_repeated_triggers_restart_recovery(tmp: Path):
    print("\n[16] 第一轮之后再次触发仍能起恢复流程（回归）")
    port = await free_port()
    marker = tmp / "calls-rep.txt"
    cmd = make_launcher(tmp / "rep-l.sh", marker)
    cfg = tmp / "config-rep.json"
    make_config(cfg, port, cmd)
    d = json.loads(cfg.read_text(encoding="utf-8"))
    d["restart_cooldown"] = 0
    d["watchdog_enabled"] = True
    d["watchdog"] = {
        "restart_on_startup": False,
        "qrcode_after_launch_delay": 0.1,
        "qrcode_poll_interval": 0.2,
        "qrcode_wait_seconds": 1,
        "max_restart_cycles": 1,      # 每轮只重启一次，便于数次数
        "health_check": False,        # 由测试手动触发
        "connect_timeout": 2,
        "notify": {"on_offline": False, "on_qrcode": False, "on_login_ok": False},
        "wechat": {"enabled": False},
    }
    cfg.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    client = NapCatClient(config_path=str(cfg))
    runner = asyncio.create_task(client.start())
    try:
        await asyncio.sleep(1.0)
        wd = client.watchdog
        # 第一轮：掉线 → 恢复（把 _offline_since 置位）
        wd._kick_episode("测试：第一次掉线")
        for _ in range(60):
            if marker.exists() and len(marker.read_text().split()) >= 1:
                break
            await asyncio.sleep(0.2)
        first = len(marker.read_text().split()) if marker.exists() else 0
        check("第一次触发起了恢复流程", first >= 1, f"calls={first}")

        # 等第一轮结束（含 _offline_since 复位）
        for _ in range(80):
            if wd._episode_task is None or wd._episode_task.done():
                break
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.5)

        # 第二次触发：这正是曾经失效的场景
        wd._kick_episode("测试：第二次掉线")
        for _ in range(60):
            if len(marker.read_text().split()) > first:
                break
            await asyncio.sleep(0.2)
        second = len(marker.read_text().split()) if marker.exists() else 0
        check("第一轮之后再次触发仍会重启（回归）", second > first,
              f"{first} -> {second}")

        # 已有恢复流程在跑时，重复触发不该叠加新流程
        running_before = second
        wd._kick_episode("测试：恢复中重复触发")
        wd._kick_episode("测试：恢复中重复触发2")
        await asyncio.sleep(0.5)
        check("恢复流程运行中不叠加新流程",
              len(marker.read_text().split()) <= running_before + 1,
              f"calls={len(marker.read_text().split())}")
    finally:
        await asyncio.wait_for(client.stop(), timeout=15)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(runner), timeout=10)

async def test_placeholder_convention(tmp: Path):
    if not _plugin_ready("bupt_news"):
        return
    print("\n[17] 命令占位符统一：<> 必填、[] 可选，且 help 明确说明")

    class _FakeGroups:
        """只为让 /bind、/unbind 走到「缺少群号」分支的最小替身。"""

        all_groups = False
        _bound: set = set()

        def list_bound(self):
            return []

        def bind_all(self):
            return True

        def bind(self, gid):
            return True

        def unbind(self, gid):
            return False

    import asyncio as _asyncio
    from napcat.command import (
        CommandManager, PLACEHOLDER_LEGEND, build_usage, format_field_usage,
        describe_fields)
    from napcat.command import (
        CommandSchema, QQField, DurationField, StrField, IntField, BoolField,
        ChoiceField, VariadicField,
    )
    from napcat.models import MessageEvent

    # (a) 两种记号是唯一约定，图例必须说清「不用输入符号」
    check("图例说明 <> 是必填", "<名称>" in PLACEHOLDER_LEGEND
          and "必填" in PLACEHOLDER_LEGEND, PLACEHOLDER_LEGEND)
    check("图例说明 [] 是可选", "[名称]" in PLACEHOLDER_LEGEND
          and "可选" in PLACEHOLDER_LEGEND, PLACEHOLDER_LEGEND)
    check("图例说明括号本身不用输入", "不用输入" in PLACEHOLDER_LEGEND,
          PLACEHOLDER_LEGEND)

    # (b) 字段 → 记号：必填 <>、可选 []、可变 [name ...]
    class _S(CommandSchema):
        uid = QQField(required=True, description="目标QQ")
        duration = DurationField(default=600, description="时长")
        reason = StrField(default="", description="原因")
        kind = ChoiceField(["group", "user"], default="group", description="类型")
        rest = VariadicField(description="剩余")

    usage = build_usage("demo", _S)
    check("必填字段用 <>", "<uid>" in usage, usage)
    check("带默认值的可选字段用 []", "[duration=600]" in usage, usage)
    check("默认值为空的可选字段用 []", "[reason]" in usage, usage)
    check("枚举可选字段也用 []", "[kind=group]" in usage, usage)
    check("可变参数用 [name ...]", "[rest ...]" in usage, usage)
    check("用法里不出现裸字段名（除必填/可选记号外）",
          "<uid>" in usage and "uid>" in usage and "uid" not in
          usage.replace("<uid>", "").replace("[uid]", ""), usage)

    # (c) 参数表与用法行口径一致：必填 <name>、可选 [name]
    rows = "\n".join(describe_fields(_S.get_fields()))
    check("参数表必填行用 <uid>", "<uid> " in rows, rows)
    check("参数表可选行用 [duration]", "[duration] " in rows, rows)
    check("参数表不再出现裸 <name> 形式的可选字段",
          "<duration>" not in rows and "<reason>" not in rows, rows)

    # (d) /help 首屏必须带图例，且 <命令名> 自身也符合约定
    cm = CommandManager()

    @cm.register("demo", description="演示命令组")
    async def _root(p):
        return "x"

    @cm.register("demo.get", schema=_S, description="取值")
    async def _get(p):
        return "y"

    @cm.register("ping", description="探活", usage="ping [次数]")
    async def _ping(p):
        return "pong"

    first = await cm.handle(MessageEvent(message_type="private", user_id=1,
                                         message="/help"))
    check("/help 首屏带占位符图例", PLACEHOLDER_LEGEND in first, first[-200:])
    check("/help 首屏自身用 <命令名>", "/help <命令名>" in first, first[-200:])

    # (e) 单命令详情带图例（否则 <编号> 会被原样照抄）
    detail = await cm.handle(MessageEvent(message_type="private", user_id=1,
                                          message="/help demo.get"))
    check("单命令详情带占位符图例", PLACEHOLDER_LEGEND in detail, detail)
    check("详情的位置传参用统一记号",
          "/demo get <uid>" in detail and "[duration=600]" in detail, detail)

    # (f) 回归：子命令注册时 usage= 曾被 register() 丢弃，
    #     导致 /help 里子命令看不出要传什么。现在：
    #     第二级（/help <命令组>）只给名字与介绍，参数在第三级（/help <组>.<子>）。
    legacy = CommandManager()

    @legacy.register("old", description="旧式命令")
    async def _oldroot(p):
        return "x"

    @legacy.register("old.run", description="跑一次", usage="run <目标> [次数]")
    async def _run(p):
        return "y"

    first_page = await legacy.handle(MessageEvent(message_type="private", user_id=1,
                                                  message="/help"))
    check("第一级只列命令组（子命令不展开）",
          "/old — 旧式命令" in first_page and "/old run" not in first_page, first_page)
    listing = await legacy.handle(MessageEvent(message_type="private", user_id=1,
                                               message="/help old"))
    check("第二级只给子命令名字 + 权限/作用域 + 介绍（不摊参数）",
          "/old run [权限：公开 · 作用域：所有场景] — 跑一次" in listing
          and "<目标>" not in listing, listing)
    sub_usage_page = await legacy.handle(MessageEvent(message_type="private", user_id=1,
                                                      message="/help old.run"))
    check("子命令的 usage= 不再被丢弃（第三级详情显示参数）",
          "/old run <目标> [次数]" in sub_usage_page, sub_usage_page)
    sub_detail = legacy.list_commands()["old"].subcommands["run"]
    check("子命令 usage 保留原样", sub_detail.usage == "run <目标> [次数]",
          repr(sub_detail.usage))

    # (g) 无 schema 的子命令：详情页头部就是完整用法（含占位符），必须有图例
    sub_help = await legacy.handle(MessageEvent(message_type="private", user_id=1,
                                                message="/help old.run"))
    check("无 schema 子命令详情头部显示完整用法",
          "/old run <目标> [次数]" in sub_help, sub_help)
    check("无 schema 子命令详情也带图例",
          "不用输入" in sub_help, sub_help)

    # (h) 面向用户的运行期文案：报错提示也必须用同一记号
    #     （`请指定群号: /bind 群号` 这种裸写法会让人不知道要不要输入「群号」两个字）。
    #     注：源码 docstring 里的用法示例不做自动校验 —— 那些行是「命令 + 空格 +
    #     中文说明」，没有可靠的语义边界来区分参数与说明，规则会误伤。
    binder = CommandManager()
    binder._groups_ref = _FakeGroups()          # 只为走到「缺少群号」这一分支
    bind_msg = await binder.handle(MessageEvent(message_type="private", user_id=1,
                                                message="/bind"))
    check("/bind 缺参数时提示用 <群号>", "<群号>" in bind_msg, bind_msg)
    check("/bind 提示同时给出 all 的写法", "/bind all" in bind_msg, bind_msg)
    unbind_msg = await binder.handle(MessageEvent(message_type="private", user_id=1,
                                                  message="/unbind"))
    check("/unbind 缺参数时提示用 <群号>", "<群号>" in unbind_msg, unbind_msg)

    # (i) bupt_news 的子命令参数必须出现在 /help 列表里（回归：usage 曾被丢弃）
    from plugins.bupt_news.schemas import (GetSchema, SyncSchema,
                                           AddTargetSchema, ImagesSchema)
    check("buptnews get 显示 <id>",
          "<id>" in build_usage("get", GetSchema),
          build_usage("get", GetSchema))
    check("buptnews sync 显示可选 [pages=1]",
          "[pages=1]" in build_usage("sync", SyncSchema),
          build_usage("sync", SyncSchema))
    check("buptnews add 显示 [kind=group] [target]",
          "[kind=group]" in build_usage("add", AddTargetSchema)
          and "[target]" in build_usage("add", AddTargetSchema),
          build_usage("add", AddTargetSchema))
    check("buptnews images 显示 [action=stats] [limit=0]",
          "[action=stats]" in build_usage("images", ImagesSchema),
          build_usage("images", ImagesSchema))

    # (j) 命令组本体不能再自己追加一次图例（框架 render_help 已按需附上），
    #     否则帮助里会连出两行同样的说明。
    from napcat.command import PLACEHOLDER_LEGEND as _LEGEND
    for path in ("plugins/bupt_news/main.py", "plugins/essential/main.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        check(f"{path} 不再手写图例（避免重复两行）",
              "+ PLACEHOLDER_LEGEND" not in src, path)
    grouped = CommandManager()

    @grouped.register("grp", description="命令组")
    async def _grp(p):
        return "x"

    @grouped.register("grp.sub", usage="sub <必填>", description="子命令")
    async def _sub(p):
        return "y"

    body = grouped.list_commands()["grp"].render_help()
    check("命令组帮助不出现图例（第二级不摊参数 → 本页没有占位符）",
          body.count(_LEGEND) == 0, body)
    sub_body = grouped.list_commands()["grp"].subcommands["sub"].render_help()
    check("子命令详情带图例且只出现一次（框架按需附上，不重复）",
          sub_body.count(_LEGEND) == 1, sub_body)

    await _asyncio.sleep(0)


async def test_plugin_reload_resilience(tmp: Path):
    print("\n[18] 插件热重载：新代码有错时不消失、能回滚、修好后自动重载")
    from napcat.command import CommandManager
    from napcat.events import EventBus, TimerManager
    from napcat.permission import PermissionManager
    from napcat.file_manager import FileManager
    from napcat.plugin import PluginManager
    from napcat.models import MessageEvent

    plugin_dir = tmp / "hotreload" / "plugins"
    plugin_dir.mkdir(parents=True)
    pkg = plugin_dir / "demoplug"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .main import DemoPlugin\n",
                                     encoding="utf-8")
    main_py = pkg / "main.py"

    def write_plugin(version: str, body: str = "", extra: str = "") -> None:
        main_py.write_text(
            "from napcat.plugin import PluginBase\n"
            f"VERSION_MARK = {version!r}\n"
            f"{extra}\n"
            "class DemoPlugin(PluginBase):\n"
            "    name = 'demoplug'\n"
            f"    version = {version!r}\n"
            "    async def on_enable(self):\n"
            "        @self.register_command('demo', description='演示')\n"
            "        async def _cmd(packet):\n"
            f"            return {body or version!r}\n",
            encoding="utf-8")

    write_plugin("1.0.0")
    cmds = CommandManager()
    mgr = PluginManager(str(plugin_dir), api=None, event_bus=EventBus(),
                        timer=TimerManager(), cmd_manager=cmds,
                        perm_manager=PermissionManager(),
                        file_manager=FileManager(api=None),
                        logger=_logger())
    await mgr.load_all()
    await mgr.enable_all()
    check("插件首次加载成功", "demoplug" in mgr.plugins, str(list(mgr.plugins)))

    async def ask() -> str:
        return await cmds.handle(MessageEvent(message_type="private", user_id=1,
                                              message="/demo"))

    check("插件命令可用", await ask() == "1.0.0", await ask())

    # (a) 写入**语法错误**的新版本 → 热重载失败，但插件必须还在、旧代码仍在服务
    main_py.write_text("this is not valid python !!!\n", encoding="utf-8")
    ok = await mgr.reload("demoplug")
    check("语法错误时 reload 返回失败", ok is False, repr(ok))
    check("★ 加载失败后插件没有消失", "demoplug" in mgr.plugins,
          str(list(mgr.plugins)))
    check("★ 回滚后旧版本命令仍可用", await ask() == "1.0.0", await ask())

    # (b) 写入**导入错误**的新版本（本次线上事故的形态）→ 同样必须回滚
    write_plugin("2.0.0", extra="from napcat.plugin import NO_SUCH_SYMBOL_XYZ")
    ok = await mgr.reload("demoplug")
    check("导入错误时 reload 返回失败", ok is False, repr(ok))
    check("导入错误后插件仍在且旧命令可用",
          "demoplug" in mgr.plugins and await ask() == "1.0.0", await ask())

    # (c) 修好新版本 → 文件变更监控自动重载，并且拿到新代码
    write_plugin("3.0.0", body="v3")
    await mgr.watch(interval=0.1)
    for _ in range(80):                     # 最多等 8s，避免依赖精确时序
        await asyncio.sleep(0.1)
        if await ask() == "v3":
            break
    check("★ 修好后 watcher 自动重载到新版本", await ask() == "v3", await ask())
    mgr.stop_watch()

    # (d) 模块树必须干净：加载成功后 sys.modules 里是**新版本**的模块，
    #     失败版本不留残留（否则下次导入会拿到坏模块）。
    mod = sys.modules.get("napcat_plugin_demoplug.main")
    check("重载后 sys.modules 里是新版本模块",
          mod is not None and getattr(mod, "VERSION_MARK", None) == "3.0.0",
          f"VERSION_MARK={getattr(mod, 'VERSION_MARK', None)!r}")
    check("半成品模块没有残留（父模块与子模块各一）",
          sorted(k for k in sys.modules if k.startswith("napcat_plugin_demoplug"))
          == ["napcat_plugin_demoplug", "napcat_plugin_demoplug.main"],
          str(sorted(k for k in sys.modules if k.startswith("napcat_plugin_demoplug"))))

    await mgr.shutdown()


async def test_summary_retry(tmp: Path):
    if not _plugin_ready("bupt_news"):
        return
    print("\n[19] AI 摘要重试：同步重试 5 次、失败不落降级、调取时再重试")
    from plugins.bupt_news.main import BuptNewsPlugin, fallback_summary
    from plugins.bupt_news.store import StateStore

    data_dir = tmp / "summary-store"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(str(data_dir / "state.json"), str(data_dir / "notices.jsonl"))

    def add(nid_news: str, title: str, ai: bool = False, summary: str = "",
            rid: int | None = None) -> int:
        return store.add_notice({
            "key": f"1154:{nid_news}", "newsid": nid_news, "treeid": "1154",
            "title": title, "dept": "", "date": "2026-09-26", "url": "",
            "body": "正文", "summary": summary, "summary_ai": ai, "images": [],
            **({"id": rid} if rid else {}),
        })

    class _FakeSummarizer:
        """可编排的假摘要器：按调用序号决定成功/失败。"""

        def __init__(self, script):
            self.script = list(script)      # 每项: "ok" | "raise" | dict
            self.calls: list[list] = []
            self.last_error = ""
            self.enabled = True

        async def summarize(self, notices):
            from plugins.bupt_news.summarize import NoticeDigest
            items = list(notices)
            self.calls.append([str(n.get("newsid")) for n in items])
            mode = self.script.pop(0) if self.script else "raise"
            if mode == "raise":
                self.last_error = "HTTP 429: rate limited"
                raise RuntimeError(self.last_error)
            if isinstance(mode, dict):
                # 脚本给的 {"id": "摘要"} → 补上学生判定（默认发给学生）
                return {k: NoticeDigest(v, True) for k, v in mode.items()}
            return {str(n.get("newsid")): NoticeDigest(
                f"AI摘要-{n.get('newsid')}", True) for n in items}

    plugin = BuptNewsPlugin(api=None, event_bus=None, timer=None, cmd_manager=None,
                            perm_manager=None, file_manager=None, logger=_logger(),
                            groups=None, cfg=_FakeCfg({"plugins": {"bupt_news": {
                                "summarize": {"retry": 5, "retry_delay": 0,
                                              "retry_on_notice": 2,
                                              "retry_scan_max": 60}}}}))
    plugin._store = store
    # 该实例由测试手工构造（不经过 on_load），补上补试节流用的属性
    plugin._summary_retry_next_at = 0.0

    # (a) 同步路径：连失败 6 次（1 + 5 重试）后**不写降级摘要**，可被下次重试
    rid = add("9001", "关于某某工作的通知")
    sm = _FakeSummarizer(["raise"] * 6)
    plugin._summarizer = sm
    rec = store.get_notice(rid)
    out = await plugin._summarize_records([rec])
    check("同步摘要：失败 6 次（首次 + 5 次重试）", len(sm.calls) == 6, f"{len(sm.calls)}")
    check("同步摘要：重试用尽后不落库（留待下次重试）",
          not (store.get_notice(rid) or {}).get("summary"),
          repr((store.get_notice(rid) or {}).get("summary")))
    check("同步摘要：未标记 summary_ai",
          not (store.get_notice(rid) or {}).get("summary_ai"),
          repr((store.get_notice(rid) or {}).get("summary_ai")))
    check("同步摘要：本次渲染仍给出降级文本（不留空）",
          out.get("9001") == fallback_summary("关于某某工作的通知"), repr(out))

    # (b) 调取该条（/buptnews get）时重试：2 次后成功 → 写回库
    sm2 = _FakeSummarizer(["raise", "raise", "ok"])
    plugin._summarizer = sm2
    ok = await plugin._retry_summary_for(store.get_notice(rid))
    check("调取时重试成功", ok is True, repr(ok))
    check("调取时按 retry_on_notice=2 重试（共 3 次调用）",
          len(sm2.calls) == 3, f"{len(sm2.calls)}")
    saved = store.get_notice(rid) or {}
    check("调取成功后摘要写回库", saved.get("summary") == "AI摘要-9001", repr(saved.get("summary")))
    check("调取成功后标记 summary_ai", saved.get("summary_ai") is True,
          repr(saved.get("summary_ai")))

    # (c) 调取时也拿不到 → 返回 False 且不落库（下次还能再试）
    rid2 = add("9002", "另一条通知")
    sm3 = _FakeSummarizer(["raise"] * 3)
    plugin._summarizer = sm3
    ok = await plugin._retry_summary_for(store.get_notice(rid2))
    check("调取时仍失败返回 False", ok is False, repr(ok))
    check("调取失败也不落降级摘要",
          not (store.get_notice(rid2) or {}).get("summary"),
          repr((store.get_notice(rid2) or {}).get("summary")))

    # (d) 下次同步会**自动补试**上次失败的记录（无需用户调取）
    plugin._summarizer = _FakeSummarizer(["ok"])
    ok = await plugin._retry_missing_summaries()
    check("同步补试覆盖历史失败记录", ok == 1, f"ok={ok}")
    check("补试成功后该条有 AI 摘要",
          (store.get_notice(rid2) or {}).get("summary") == "AI摘要-9002",
          repr((store.get_notice(rid2) or {}).get("summary")))

    # (e) 已有 AI 摘要的记录不该重复花钱（不再次调用 API）
    sm4 = _FakeSummarizer(["ok"])
    plugin._summarizer = sm4
    out = await plugin._summarize_records([store.get_notice(rid)])
    check("已有 AI 摘要不重复调用", len(sm4.calls) == 0, f"{len(sm4.calls)}")
    check("已有 AI 摘要直接复用", out.get("9001") == "AI摘要-9001", repr(out))

    # (f) AI 少返回某条也算失败（否则那条会被静默跳过、且不会被标记为待重试）
    add("9101", "第三条")
    add("9102", "第四条")
    sm5 = _FakeSummarizer([{"9101": "只有这一条"}, "raise", "raise", "raise",
                           "raise", "raise", "raise"])
    plugin._summarizer = sm5
    recs = [r for r in store.all_records()
            if str(r.get("newsid")) in ("9101", "9102")]
    out = await plugin._summarize_records(recs)
    check("部分返回视为失败并重试", len(sm5.calls) >= 2, f"{len(sm5.calls)}")
    r9003 = [r for r in store.all_records() if str(r.get("newsid")) == "9101"][0]
    r9004 = [r for r in store.all_records() if str(r.get("newsid")) == "9102"][0]
    check("部分返回：成功的那条写库",
          r9003.get("summary_ai") is True and r9003.get("summary") == "只有这一条",
          repr(r9003.get("summary")))
    check("部分返回：缺失的那条不写降级（留待重试）",
          not r9004.get("summary") and not r9004.get("summary_ai"),
          repr(r9004.get("summary")))

    # (g) 旧数据迁移：`summary_ai` 是后加的标记，历史记录必须免费补上，
    #     否则 /buptnews list 会全部退回标题、且会被反复问 AI（几百次多余调用）。
    bf_dir = tmp / "summary-backfill"
    bf_dir.mkdir(parents=True, exist_ok=True)
    bf = StateStore(str(bf_dir / "state.json"), str(bf_dir / "notices.jsonl"))
    old_title = "关于开展某项工作的通知"
    for newsid, summary in (("8001", "AI 生成过的摘要"),
                            ("8002", fallback_summary(old_title)),
                            ("8003", "")):
        bf.add_notice({"key": f"1154:{newsid}", "newsid": newsid, "treeid": "1154",
                       "title": old_title, "body": "b", "summary": summary,
                       "images": []})
    p2 = BuptNewsPlugin(api=None, event_bus=None, timer=None, cmd_manager=None,
                        perm_manager=None, file_manager=None, logger=_logger(),
                        groups=None,
                        cfg=_FakeCfg({"plugins": {"bupt_news": {
                            "data_dir": str(bf_dir)}}}))
    p2._store = bf
    moved = p2._backfill_summary_flags()
    check("旧数据补标记：只认「不像降级文本」的摘要", moved == 1, f"{moved}")
    r1 = bf.get_notice(1) or {}
    r2 = bf.get_notice(2) or {}
    r3 = bf.get_notice(3) or {}
    check("像 AI 摘要的旧记录被补标记",
          r1.get("summary_ai") is True, repr(r1.get("summary_ai")))
    check("等于降级文本的旧记录不补标记（要重试）",
          not r2.get("summary_ai"), repr(r2.get("summary_ai")))
    check("没有摘要的旧记录不补标记（要重试）",
          not r3.get("summary_ai"), repr(r3.get("summary_ai")))
    check("补标记是幂等的（再跑一次为 0）",
          p2._backfill_summary_flags() == 0, "second run")

    # (h) 配置项默认值：同步 5 次重试、调取 2 次
    #     ⚠️ 默认值由**构造期**的 config_template() 补齐（框架契约），所以这里
    #     故意不经 PluginManager 直接 new —— 这正是「任何构造路径都拿得到完整
    #     配置」的那条路径。
    fresh = BuptNewsPlugin(api=None, event_bus=None, timer=None, cmd_manager=None,
                           perm_manager=None, file_manager=None, logger=_logger(),
                           groups=None,
                           cfg=_FakeCfg({"plugins": {"bupt_news": {
                               "data_dir": str(tmp / "cfg-default")}}}))
    await fresh.on_load()
    s = fresh.config.get("summarize") or {}
    check("默认 retry=5", int(s.get("retry", 0)) == 5, repr(s.get("retry")))
    check("默认 retry_on_notice=2", int(s.get("retry_on_notice", -1)) == 2,
          repr(s.get("retry_on_notice")))
    check("默认有退避间隔 retry_delay>0", float(s.get("retry_delay", 0)) > 0,
          repr(s.get("retry_delay")))


async def test_student_judgement(tmp: Path):
    if not _plugin_ready("bupt_news"):
        return
    print("\n[20] AI 判定「是否发给学生」：非学生加提示且不 @全体成员")
    from plugins.bupt_news.main import (BuptNewsPlugin, _NOT_STUDENT_HINT,
                                        fallback_summary)
    from plugins.bupt_news.store import StateStore
    from plugins.bupt_news.summarize import NoticeDigest, parse_digests
    from napcat.message import MessageChain

    # (a) 解析：student 字段 → NoticeDigest.for_students
    got = parse_digests('[{"id":"1","s":"摘要一","student":false},'
                        '{"id":"2","s":"摘要二","student":true}]')
    check("解析出 student=false", got["1"].for_students is False, repr(got.get("1")))
    check("解析出 student=true", got["2"].for_students is True, repr(got.get("2")))
    check("摘要文本照常解析", got["1"].summary == "摘要一", repr(got["1"].summary))
    # 没有 student 字段 → None（调用方据此继续重试）
    got2 = parse_digests('[{"id":"3","s":"摘要三"}]')
    check("缺 student 字段时为 None", got2["3"].for_students is None, repr(got2.get("3")))
    # 容错写法（中英文、数字、for_students 别名）
    for raw, key, want in (('[{"id":"4","s":"x","student":"否"}]', "4", False),
                           ('[{"id":"5","s":"x","student":"是"}]', "5", True),
                           ('[{"id":"6","s":"x","for_students":false}]', "6", False)):
        check(f"容错解析 {raw[:34]}…",
              parse_digests(raw)[key].for_students is want, raw)

    data_dir = tmp / "student-judge"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(str(data_dir / "state.json"), str(data_dir / "notices.jsonl"))

    class _Sum:
        enabled = True
        last_error = ""

        def __init__(self, digests):
            self.digests = digests
            self.calls = 0

        async def summarize(self, notices):
            self.calls += 1
            return dict(self.digests)

    plugin = BuptNewsPlugin(api=None, event_bus=None, timer=None, cmd_manager=None,
                            perm_manager=None, file_manager=None, logger=_logger(),
                            groups=None,
                            cfg=_FakeCfg({"plugins": {"bupt_news": {
                                "data_dir": str(data_dir),
                                "summarize": {"retry": 5, "retry_delay": 0}}}}))
    plugin._store = store

    def add(newsid, title):
        return store.add_notice({
            "key": f"1154:{newsid}", "newsid": newsid, "treeid": "1154",
            "title": title, "body": "b", "summary": "", "images": []})

    rid = add("7001", "关于机构编制设置的通知")
    plugin._summarizer = _Sum({"7001": NoticeDigest("机构编制调整", False)})
    out = await plugin._summarize_records([store.get_notice(rid)])
    saved = store.get_notice(rid) or {}
    check("判定写入库 for_students=False", saved.get("for_students") is False,
          repr(saved.get("for_students")))
    check("标记 student_ai=True", saved.get("student_ai") is True,
          repr(saved.get("student_ai")))
    check("摘要照常写入", saved.get("summary") == "机构编制调整", repr(saved.get("summary")))
    check("内存记录同步更新（下游直接读它）",
          (store.get_notice(rid) or {}).get("for_students") is False, "")

    # (b) 只有摘要、没有 student 判定 → 视为未完成并重试
    rid2 = add("7002", "某通知")
    sm = _Sum({"7002": NoticeDigest("只有摘要", None)})
    plugin._summarizer = sm
    await plugin._summarize_records([store.get_notice(rid2)])
    check("缺 student 判定会重试（按 retry=5 → 6 次）", sm.calls == 6, f"{sm.calls}")
    check("缺 student 判定时不落库",
          not (store.get_notice(rid2) or {}).get("summary_ai"),
          repr((store.get_notice(rid2) or {}).get("summary_ai")))

    # (c) 渲染：全部非学生 → 加提示；混合 → 不加提示
    def rec(i, students, ai=True):
        r = {"id": i, "newsid": f"80{i}", "title": f"通知{i}",
             "summary": f"摘要{i}", "summary_ai": True}
        if students is not None:
            r["student_ai"] = True
            r["for_students"] = students
        return r

    only_admins = [rec(1, False), rec(2, False)]
    mixed = [rec(1, False), rec(2, True)]
    unknown = [rec(1, None)]
    check("全非学生 → 列表消息带提示",
          _NOT_STUDENT_HINT in plugin._build_list(only_admins, {}).to_cq(),
          plugin._build_list(only_admins, {}).to_cq())
    check("全非学生 → 早报也带提示",
          _NOT_STUDENT_HINT in plugin._build_digest("☀️", only_admins, {}).to_cq(), "")
    check("混合（含学生通知）→ 不加提示",
          _NOT_STUDENT_HINT not in plugin._build_list(mixed, {}).to_cq(),
          plugin._build_list(mixed, {}).to_cq())
    check("判定缺失 → 按学生处理，不加提示",
          _NOT_STUDENT_HINT not in plugin._build_list(unknown, {}).to_cq(),
          plugin._build_list(unknown, {}).to_cq())
    check("_any_for_students 判定正确",
          plugin._any_for_students(only_admins) is False
          and plugin._any_for_students(mixed) is True
          and plugin._any_for_students(unknown) is True, "")

    # (d) 推送：全非学生 → 群消息不带 @全体成员（配额可用也不 @）
    sent: list[str] = []

    class _Api:
        async def get_group_at_all_remain(self, gid):
            return {"can_at_all": True, "remain_at_all_count_for_group": 5,
                    "remain_at_all_count_for_uin": 9}

        async def send_group_msg(self, gid, msg, **kw):
            sent.append(msg.to_cq() if hasattr(msg, "to_cq") else str(msg))

        async def send_private_msg(self, uid, msg, **kw):
            sent.append(msg.to_cq() if hasattr(msg, "to_cq") else str(msg))

    plugin.api = _Api()
    plugin._int_list = lambda key: [123] if key == "groups" else []
    plugin._split_chain = lambda chain: chain

    sent.clear()
    await plugin._push_chain(MessageChain().text("正文"), records=only_admins)
    check("★ 全非学生 → 即使配额可用也不 @全体成员",
          len(sent) == 1 and "[CQ:at,qq=all]" not in sent[0], f"{sent}")

    sent.clear()
    await plugin._push_chain(MessageChain().text("正文"), records=mixed)
    check("含学生通知 → 照常 @全体成员",
          len(sent) == 1 and "[CQ:at,qq=all]" in sent[0], f"{sent}")

    sent.clear()
    await plugin._push_chain(MessageChain().text("正文"), records=unknown)
    check("判定缺失 → 照常 @全体成员（拿不准宁可按学生）",
          len(sent) == 1 and "[CQ:at,qq=all]" in sent[0], f"{sent}")

    sent.clear()
    await plugin._push_chain(MessageChain().text("正文"))
    check("不传 records（兼容旧调用）→ 行为不变，仍 @全体成员",
          len(sent) == 1 and "[CQ:at,qq=all]" in sent[0], f"{sent}")

    # (e) /buptnews list 的标记
    check("list 详情对非学生条目打标记",
          plugin._for_students_of(rec(1, False)) is False
          and plugin._for_students_of(rec(1, True)) is True, "")

    # (f) 「摘要已有、只缺学生判定」的记录走**只问判定**的轻量路径：
    #     不重新生成摘要（省 token，也不会把已发出去的摘要改写掉）。
    from plugins.bupt_news.summarize import parse_students, STUDENT_ONLY_PROMPT

    check("parse_students 解析 {id: bool}",
          parse_students('[{"id":"1","student":false},{"id":"2","student":true},'
                         '{"id":"3"}]') == {"1": False, "2": True},
          str(parse_students('[{"id":"1","student":false}]')))
    check("只问判定的提示词已定义", bool(STUDENT_ONLY_PROMPT), "")

    class _JudgeSum:
        enabled = True
        last_error = ""

        def __init__(self):
            self.judge_calls: list[list] = []
            self.full_calls = 0

        async def summarize(self, notices):
            self.full_calls += 1
            return {}

        async def summarize_students(self, notices):
            self.judge_calls.append([str(n.get("newsid")) for n in notices])
            return {str(n.get("newsid")): False for n in notices}

    # 清掉前面故意留成"缺摘要"的 7002，隔离出「只缺判定」这一种情况
    store._notices.pop(2, None)
    store._rewrite_notices()
    rid3 = add("7003", "某已有摘要的通知")
    store.update_notice(rid3, summary="早就有的摘要", summary_ai=True)
    js = _JudgeSum()
    plugin._summarizer = js
    n = await plugin._retry_missing_summaries()
    check("只缺判定的记录被补上", n == 1, f"n={n}")
    check("走的是只问判定（没有重新摘要）",
          len(js.judge_calls) == 1 and js.full_calls == 0,
          f"judge={len(js.judge_calls)} full={js.full_calls}")
    saved3 = store.get_notice(rid3) or {}
    check("只问判定也能落库 for_students",
          saved3.get("for_students") is False and saved3.get("student_ai") is True,
          repr(saved3.get("for_students")))
    check("原摘要没被改写", saved3.get("summary") == "早就有的摘要",
          repr(saved3.get("summary")))

    # (g) 补试节流：上一轮一条都没补成功 → 进入冷却，不再每轮重复烧 API
    plugin._summary_retry_next_at = 0.0
    store.update_notice(rid3, student_ai=False, for_students=None)

    class _Poor:
        """独立 spy：_JudgeSum.judge_calls 是**类属性**（跨实例共享），
        用它判断"这次有没有调用"会被前一节的调用污染。"""

        enabled = True
        last_error = ""

        def __init__(self):
            self.calls = 0

        async def summarize(self, notices):
            self.calls += 1
            return {}

        async def summarize_students(self, notices, **kw):
            self.calls += 1
            return {}

    poor = _Poor()
    plugin._summarizer = poor
    n1 = await plugin._retry_missing_summaries()
    check("补试全失败 → 本轮为 0", n1 == 0, f"{n1}")
    check("补试全失败 → 记下冷却时间", plugin._summary_retry_next_at > 0, "no cooldown")
    calls_after_first = poor.calls
    n2 = await plugin._retry_missing_summaries()
    check("★ 冷却期内不再重复补试",
          n2 == 0 and poor.calls == calls_after_first,
          f"n2={n2} calls={calls_after_first}→{poor.calls}")

    # (h) 只问判定必须用 parse_students：回复里没有 `s` 字段，
    #     用 parse_digests 会把整条丢掉（表现为"0 条成功且无任何报错"）
    from plugins.bupt_news import summarize as smod

    raw_judge = '[{"id":"1","student":true},{"id":"2","student":false}]'
    check("只问判定的回复能被 parse_students 解析",
          smod.parse_students(raw_judge) == {"1": True, "2": False},
          str(smod.parse_students(raw_judge)))
    check("同一回复用 parse_digests 会丢光（所以两者不能混用）",
          smod.parse_digests(raw_judge) == {}, str(smod.parse_digests(raw_judge)))

    class _Resp:
        status = 200
        def __init__(self, content):
            self._c = content
        async def json(self):
            return {"choices": [{"message": {"content": self._c},
                                 "finish_reason": "stop"}]}
        async def text(self):
            return ""

    class _Sess:
        closed = False          # _ensure_session 会读它

        def __init__(self, content):
            self._c = content
        def post(self, *a, **k):
            outer = self
            class _Ctx:
                async def __aenter__(self_inner):
                    return _Resp(outer._c)
                async def __aexit__(self_inner, *a):
                    return False
            return _Ctx()

    real = smod.Summarizer(base_url="http://x", model="m", api_key="k")
    real._session = _Sess(raw_judge)
    got = await real._one_batch([("1", "标题一"), ("2", "标题二")],
                                system=smod.STUDENT_ONLY_PROMPT,
                                ask="请判断以下通知是否面向学生：",
                                parse=smod.parse_students)
    check("★ 只问判定走 parse_students（不再静默丢光）",
          got == {"1": True, "2": False}, str(got))

    # 有内容但解析不出条目 → 必须显式报错（否则又是"0 条成功、无报错"）
    real2 = smod.Summarizer(base_url="http://x", model="m", api_key="k")
    real2._session = _Sess("我不知道该怎么判断")
    failed = False
    try:
        await real2._one_batch([("1", "标题一")])
    except RuntimeError as e:
        failed = "无法解析" in str(e)
    check("★ 解析不出条目时显式报错", failed, "no error raised")


async def test_digest_not_blocked_by_backfill(tmp: Path):
    if not _plugin_ready("bupt_news"):
        return
    print("\n[21] 早报不被补试阻塞；补试有预算；致命错误不空转")
    import time as _time
    from plugins.bupt_news.main import BuptNewsPlugin, SEEN_SCOPE
    from plugins.bupt_news.store import StateStore

    data_dir = tmp / "digest-order"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(str(data_dir / "state.json"), str(data_dir / "notices.jsonl"))

    plugin = BuptNewsPlugin(api=None, event_bus=None, timer=None, cmd_manager=None,
                            perm_manager=None, file_manager=None, logger=_logger(),
                            groups=None,
                            cfg=_FakeCfg({"plugins": {"bupt_news": {
                                "data_dir": str(data_dir),
                                "summarize": {"retry": 5, "retry_delay": 0,
                                              "push_budget": 2.0,
                                              "retry_budget": 2.0}}}}))
    plugin._store = store
    plugin._summary_retry_next_at = 0.0
    # 门户/天气/图片桩：_do_sync 开头校验凭据，结尾会跑图片补齐（需要 _images）
    plugin.config.set("username", "u")
    plugin.config.set("password", "p")

    class _Images:
        enabled = False

        async def download_missing(self, *a, **k):
            return 0

    plugin._images = _Images()
    plugin._img_failures = None

    from plugins.bupt_news.portal import Notice

    class _Portal:
        username = password = ""

        async def fetch_list(self, column, pages=1):
            # 至少要拉到一条：`_do_sync` 在「一条都没拉到」时会直接返回，
            # 根本走不到早报（测试曾因此拿到空 order）
            return [Notice(column=str(column), newsid="7001", treeid="1154",
                           title="测试通知", dept="", date="2026-09-30",
                           url="", body="正文")]

        async def fetch_detail(self, url):
            return {"body": "正文", "images": []}

    plugin._portal = _Portal()

    class _W:
        async def today(self):
            return None
    plugin._weather = _W()

    order: list[str] = []

    class _Sum:
        enabled = True
        last_error = ""

        async def summarize(self, notices):
            order.append("summarize")
            return {}

    plugin._summarizer = _Sum()

    # 造一条历史记录「只缺学生判定」→ 补试必然要跑
    hist = store.add_notice({"key": "h", "newsid": "6001", "title": "历史通知",
                             "summary": "老摘要", "summary_ai": True, "images": []})
    # 让本轮是「有积压要推」而不是「首次建立基线」（首次基线不推送）
    store.mark_seen(SEEN_SCOPE, ["1154:7001"])
    store.mark_initialized("1154")

    # 打点：_push_chain 被调用时记一笔
    pushed: list[int] = []

    async def _spy_push(chain, **kw):
        order.append("push")
        pushed.append(1)
        return 1, 0

    plugin._push_chain = _spy_push

    # 早报到期（lastDigestDate 为空 ≠ 今天）
    store.note_digest("2000-01-01")
    # 强制不在免打扰窗口：把窗口设成不可能命中的区间
    plugin.config.set("quiet_hours", {"start": "00:00", "end": "00:01"})

    # 让补试很慢：判定一次睡 0.4s（60 条预算内会跑很多次）
    async def _slow_judge(notices, **kw):
        order.append("judge")
        await asyncio.sleep(0.4)
        return {}

    plugin._summarizer.summarize_students = _slow_judge

    t0 = _time.monotonic()
    await plugin._sync_once(reason="test")
    elapsed = _time.monotonic() - t0

    first_push = order.index("push") if "push" in order else -1
    first_judge = order.index("judge") if "judge" in order else -1
    # 注意：摘要在推送**之前**是正常的（消息内容需要它）；
    # 要断言的是**补试**（judge）不能排在推送前面。
    check("推送发生了", first_push >= 0, f"order={order[:6]}")
    check("★ 补试在推送之后才开始（不再挡住早报）",
          first_judge == -1 or first_judge > first_push, f"order={order[:6]}")
    check("早报确实发出", len(pushed) == 1, f"pushed={len(pushed)}")
    # push_budget=2s + retry_budget=2s：整轮同步必须明显受预算约束
    # （没有预算时补试会一直跑下去，之前实测 62.5s）
    check("★ 整轮同步受预算约束（不会因 AI 重试拖到分钟级）",
          elapsed < 12, f"{elapsed:.1f}s")

    # 预算：`_judge_only` 超预算即收工（用假时钟确定性验证）
    store.update_notice(hist, student_ai=False, for_students=None)
    plugin._summary_retry_next_at = 0.0
    calls = {"n": 0}

    async def _count_judge(notices, **kw):
        calls["n"] += 1
        return {}

    plugin._summarizer.summarize_students = _count_judge
    real_monotonic = _time.monotonic
    clock = {"t": 1000.0}

    def _fake_monotonic():
        clock["t"] += 30.0            # 每次读表前进 30s → 预算 90s 只允许 3 批
        return clock["t"]

    _time.monotonic = _fake_monotonic
    try:
        n = await plugin._judge_only([store.get_notice(hist)], deadline=clock["t"] + 90.0)
    finally:
        _time.monotonic = real_monotonic
    check("★ 学生判定补试受预算限制（不会一直跑下去）",
          0 < calls["n"] <= 5, f"calls={calls['n']} n={n}")

    # 致命错误（DNS/连接类）不空转重试
    class _Hopeless:
        enabled = True
        last_error = ""

        def __init__(self):
            self.calls = 0

        async def summarize(self, notices):
            self.calls += 1
            raise RuntimeError(
                "Cannot connect to host myai.bupt.edu.cn:443 [Name or service not known]")

    hop = _Hopeless()
    plugin._summarizer = hop
    got = await plugin._summarize_with_retry(
        [{"id": 1, "newsid": "1", "title": "t"}], attempts=6)
    check("★ 链路不通时只试 1 次（不按 2s/4s/8s 空转）",
          hop.calls == 1 and got == {}, f"calls={hop.calls}")

    # str(e) 会炸的异常也不能把日志/流程带崩
    import aiohttp
    dns = aiohttp.ClientConnectorDNSError(
        None, OSError("Cannot connect to host x [Name or service not known]"))
    check("识别 DNS 类致命错误", BuptNewsPlugin._is_hopeless_error(dns) is True, "")
    from plugins.bupt_news.summarize import _err_text
    txt = _err_text(dns)
    check("★ str(e) 抛异常的异常也能安全转成文本", isinstance(txt, str) and txt,
          repr(txt)[:60])


async def test_table_to_image(tmp: Path):
    if not _plugin_ready("bupt_news"):
        return
    print("\n[22] 正文表格 → 图片：解析、噪声过滤、按 pos 插入、渲染")
    from plugins.bupt_news.table_image import (
        TableImageStore, parse_table, is_renderable, TABLE_SCHEME)
    from plugins.bupt_news import portal as _portal

    # (a) 网格解析：rowspan/colspan 必须铺满，占位格不能算作空格子
    grid_html = (
        '<table border=1><tr><th>时间</th><th>10月10日</th><th>10月18日</th></tr>'
        '<tr><td>9:00-9:30</td><td>经济管理学院</td>'
        '<td>物理科学与技术学院、数学科学学院</td></tr>'
        '<tr><td>10:00-10:30</td><td>智能工程与自动化学院</td>'
        '<td rowspan=3>计算机学院</td></tr>'
        '<tr><td>10:30-11:00</td><td>人文学院（含预科）</td></tr>'
        '<tr><td>11:00-12:00</td><td>未来学院</td></tr></table>')
    t = parse_table(grid_html)
    check("表格解析出 5×3 网格", (t.rows, t.cols) == (5, 3), f"{t.rows}×{t.cols}")
    check("表格可渲染", is_renderable(t) is True, "")
    cell = [c for c in t.cells if c.row == 2 and c.col == 2][0]
    check("rowspan=3 被记录", cell.rowspan == 3 and cell.text == "计算机学院",
          f"rowspan={cell.rowspan} text={cell.text!r}")
    below = [c for c in t.cells if c.row == 3 and c.col == 2][0]
    check("跨行覆盖的位置是占位格（渲染时跳过）",
          below.is_placeholder is True, repr(below))
    check("占位格不计入可见文字",
          t.text_len == sum(len(x.text) for x in t.cells if not x.is_placeholder),
          str(t.text_len))
    check("同一表格指纹稳定", t.signature() == parse_table(grid_html).signature(), "")

    colspan = parse_table('<table><tr><td colspan=2>标题</td></tr>'
                          '<tr><td>a</td><td>b</td></tr></table>')
    check("colspan 铺出正确的列数", (colspan.rows, colspan.cols) == (2, 2),
          f"{colspan.rows}×{colspan.cols}")
    check("colspan 右侧是占位格",
          [c for c in colspan.cells if c.row == 0 and c.col == 1][0].is_placeholder,
          "")

    # (b) 噪声过滤：工具栏表 / 空布局表 / 极短表单不渲染
    for label, html, want in (
            ("打印工具栏表", '<table><tr><td>打印&nbsp;&nbsp;</td></tr></table>', False),
            ("空布局表", '<table><tr><td></td><td></td></tr></table>', False),
            ("单格两字", '<table><tr><td>确定</td></tr></table>', False),
            ("真表格", grid_html, True)):
        tb = parse_table(html)
        got = is_renderable(tb) if tb is not None else False
        check(f"{label} 可渲染={want}", got is want, f"got={got}")

    # (c) 正文提取：表格以 {pos, table} 混在 images 里，位置在表格前
    page = ('<html><body><div id="vsb_content">'
            '<p>接种日期如下：</p>' + grid_html + '<p>知情同意填写：N102室</p>'
            '</div></body></html>')
    body, media, tables = _portal.extract_body_with_tables(page, "http://x/")
    check("提取到 1 个表格", len(tables) == 1, f"{len(tables)}")
    refs = [it for it in media if it.get("table") is not None]
    check("正文素材里有表格引用", len(refs) == 1, f"{media}")
    check("表格位置落在正文范围内",
          0 <= refs[0]["pos"] <= len(body), f"pos={refs[0]['pos']} len={len(body)}")
    check("表格前的正文是那句话",
          body[:refs[0]["pos"]].rstrip().endswith("接种日期如下："),
          repr(body[:refs[0]["pos"]][-30:]))
    check("表格后的正文接在后面",
          body[refs[0]["pos"]:].lstrip().startswith("知情同意填写"),
          repr(body[refs[0]["pos"]:][:30]))
    check("表格 HTML 不混进正文文本",
          "<table" not in body and "colspan" not in body, repr(body[:80]))

    # 旧接口（只要图片）不应把表格当成图片吐出来
    _b2, imgs_only = _portal.extract_body_with_images(page, "http://x/")
    check("extract_body_with_images 只返回图片", imgs_only == [], f"{imgs_only}")

    # (d) 发送链：表格引用渲染成图片段；渲染不可用时给提示、正文照发
    from plugins.bupt_news.main import BuptNewsPlugin
    from plugins.bupt_news.store import StateStore

    data_dir = tmp / "tbl-chain"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(str(data_dir / "state.json"), str(data_dir / "notices.jsonl"))
    plugin = BuptNewsPlugin(api=None, event_bus=None, timer=None, cmd_manager=None,
                            perm_manager=None, file_manager=None, logger=_logger(),
                            groups=None,
                            cfg=_FakeCfg({"plugins": {"bupt_news": {
                                "data_dir": str(data_dir)}}}))
    plugin._store = store

    class _ImgCache:
        enabled = True

        async def ensure(self, url):
            return None

    plugin._images = _ImgCache()

    class _TblStore:
        enabled = True
        available = True

        def __init__(self, path):
            self.path = path

        async def ensure(self, html):
            return self.path

        def path_for(self, html):
            return self.path

    fake_png = tmp / "tbl-chain" / "t.png"
    fake_png.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    plugin._tables = _TblStore(str(fake_png))

    chain = await plugin._build_notice_chain(
        ["标题"], body, media, "🔗 原文", tables=tables)
    segs = chain.build()
    kinds = [x.get("type") for x in segs]
    check("★ 表格渲染成图片段发出去", "image" in kinds, str(kinds))
    check("图片段引用的是渲染结果",
          any(str(x.get("data", {}).get("file", "")).endswith(".png")
              for x in segs if x.get("type") == "image"), str(segs)[:200])
    text = " ".join(str(x.get("data", {}).get("text", ""))
                    for x in segs if x.get("type") == "text")
    check("正文文字仍然完整", "知情同意填写" in text and "接种日期" in text, text[:80])
    check("没有多余的失败提示", "未能渲染" not in text, text[-60:])

    # 渲染不可用 → 不能假装成功，要给可读提示
    class _NoTbl:
        enabled = True
        available = False

        async def ensure(self, html):
            return None

        def path_for(self, html):
            return ""

    plugin._tables = _NoTbl()
    chain2 = await plugin._build_notice_chain(
        ["标题"], body, media, "🔗 原文", tables=tables)
    text2 = " ".join(str(x.get("data", {}).get("text", ""))
                     for x in chain2.build() if x.get("type") == "text")
    check("渲染不可用时正文照发", "知情同意填写" in text2, text2[:60])
    check("渲染不可用时明确提示表格未渲染",
          "未能渲染成图片" in text2, text2[-80:])

    # (d2) 单条刷新：指定 id 立即补齐该条的图片与表格（不必等批量补抓轮到）
    from plugins.bupt_news.backfill import refresh_one_record

    class _OnePortal:
        def __init__(self, detail):
            self.detail = detail
            self.calls = []

        async def fetch_detail(self, url):
            self.calls.append(url)
            return self.detail

    # 用这个 store 里真实存在的记录（前面的用例刚写进去的那条），
    # 顺手把图片清空，好验证"刷新会把素材补齐"
    fresh = store.add_notice({"key": "1154:refresh", "newsid": "142679",
                              "treeid": "1154", "title": "接种疫苗通知",
                              "url": "http://x/n.jsp?wbnewsid=142679",
                              "body": "旧正文", "images": [], "tables": []})
    store.update_notice(fresh, url="http://x/n.jsp?wbnewsid=142679")
    portal_one = _OnePortal({"body": "新正文", "tables": [grid_html],
                             "images": [{"pos": 3, "url": "http://x/a.png"},
                                        {"pos": 20, "table": 0}],
                             "dept": "后勤部", "date": "2026-10-08"})
    got = await refresh_one_record(store, portal_one, fresh, logger=_logger())
    check("单条刷新成功", got.get("ok") is True, str(got))
    check("刷新后图片与表格都写回库",
          got.get("images") == 1 and got.get("tables") == 1, str(got))
    rec476 = store.get_notice(fresh) or {}
    check("刷新后正文已更新", rec476.get("body") == "新正文", repr(rec476.get("body")))
    check("刷新后 tables 原文在库", len(rec476.get("tables") or []) == 1, "")
    check("刷新后素材含表格引用",
          any(it.get("table") is not None for it in (rec476.get("images") or [])),
          str(rec476.get("images")))
    check("刷新会带上部门/日期", rec476.get("dept") == "后勤部", rec476.get("dept"))

    missing = await refresh_one_record(store, portal_one, 999999, logger=_logger())
    check("刷新不存在的 id 明确失败", missing.get("ok") is False, str(missing))
    store.update_notice(476, url="")
    nolink = await refresh_one_record(store, portal_one, 476, logger=_logger())
    check("没有原文链接时明确失败", nolink.get("ok") is False, str(nolink))
    # 用这个 store 里真实存在的记录（前面的用例刚写进去的那条），
    # 顺手把图片清空，好验证"刷新会把素材补齐"
    fresh = store.add_notice({"key": "1154:refresh", "newsid": "142679",
                              "treeid": "1154", "title": "接种疫苗通知",
                              "url": "http://x/n.jsp?wbnewsid=142679",
                              "body": "旧正文", "images": [], "tables": []})
    store.update_notice(fresh, url="http://x/n.jsp?wbnewsid=142679")

    # (e) 表格图片按内容哈希缓存/复用（同内容不重复渲染）
    tbl_dir = tmp / "tbl-store"
    st = TableImageStore(str(tbl_dir), logger=_logger(), browser="/bin/true")
    p1 = st.path_for(grid_html)
    check("同内容 → 同一路径（天然去重）", p1 == st.path_for(grid_html), p1)
    check("不同内容 → 不同路径", p1 != st.path_for(grid_html + "<!--x-->"), "")
    check("路径以内容哈希命名", os.path.basename(p1).endswith(".png")
          and len(os.path.basename(p1)) == 20, os.path.basename(p1))

    # 真实渲染（有 chromium 才跑，否则跳过并说明）
    import shutil as _shutil
    if _shutil.which("chromium") or _shutil.which("chromium-browser"):
        real = TableImageStore(str(tbl_dir), logger=_logger())
        got = await real.ensure(grid_html)
        check("★ 真实渲染出 PNG", bool(got) and os.path.getsize(got) > 1000,
              f"{got} {os.path.getsize(got) if got else 0}B")
        if got:
            with open(got, "rb") as f:
                head = f.read(8)
            check("渲染结果是合法 PNG", head == b"\x89PNG\r\n\x1a\n", repr(head))
            again = await real.ensure(grid_html)
            check("第二次命中缓存（同路径）", again == got, f"{again}")
    else:
        check("未安装 chromium → 渲染能力标记为不可用",
              TableImageStore(str(tbl_dir)).available is False, "")


async def test_pin_and_search(tmp: Path):
    print("\n[23] 置顶持久化 + 关键词搜索")
    from plugins.bupt_news.main import BuptNewsPlugin
    from plugins.bupt_news.store import StateStore

    data_dir = tmp / "pin-search"
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "state.json"
    notices_path = data_dir / "notices.jsonl"
    store = StateStore(str(state_path), str(notices_path))

    def add(title, body="", summary="", date="2026-10-08",
            students=True, summary_ai=True):
        return store.add_notice({
            "key": f"1154:{title}", "newsid": str(abs(hash(title)) % 10 ** 6),
            "treeid": "1154", "title": title, "body": body,
            "summary": summary, "summary_ai": summary_ai,
            "student_ai": True, "for_students": students,
            "date": date, "images": [], "tables": [],
        })

    a = add("关于退补选选修课的通知", "本科生退补选安排", "退补选安排")
    b = add("疫苗接种通知", "麻腮风疫苗和乙脑疫苗", "疫苗接种")
    c = add("干部任免通知", "王尚广同志任职", "干部任免", students=False)
    d = add("历史通知", "旧内容", "旧", date="2026-09-01")

    # (a) 置顶：持久化到 state.json，重载后仍在
    check("初始没有置顶", store.pinned_ids() == [], str(store.pinned_ids()))
    check("置顶一条成功（返回置顶编号 P1）", store.pin(b) == 1, repr(store.pin(b)))
    check("重复置顶返回 None", store.pin(b) is None, "")
    check("置顶不存在的 id 返回 None", store.pin(99999) is None, "")
    check("pinned_ids 记下了", store.pinned_ids() == [b], str(store.pinned_ids()))
    check("is_pinned 正确", store.is_pinned(b) and not store.is_pinned(a), "")

    reloaded = StateStore(str(state_path), str(notices_path))
    check("★ 置顶在重载后仍然存在（持久化）",
          reloaded.pinned_ids() == [b], str(reloaded.pinned_ids()))
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    # pins 现在是 [{n, id}]：编号与通知 id 一起持久化
    check("★ 置顶写在 state.json 的 pins 字段",
          raw.get("pins") == [{"n": 1, "id": b}], str(raw.get("pins")))

    # 多条置顶保持顺序（最近的在后）
    reloaded.pin(a)
    check("多条置顶按顺序追加", reloaded.pinned_ids() == [b, a],
          str(reloaded.pinned_ids()))
    check("newest_first 反转顺序", reloaded.pinned_ids(newest_first=True) == [a, b],
          str(reloaded.pinned_ids(newest_first=True)))
    check("取消置顶成功", reloaded.unpin(b) is True, "")
    check("取消未置顶的返回 False", reloaded.unpin(b) is False, "")
    check("取消后只剩一条", reloaded.pinned_ids() == [a], str(reloaded.pinned_ids()))
    check("clear_pins 返回条数", reloaded.clear_pins() == 1, "")
    check("清空后为空", reloaded.pinned_ids() == [], str(reloaded.pinned_ids()))

    # (b) 失效置顶清理：记录被淘汰后不该留着幽灵置顶
    reloaded.pin(b)
    reloaded.pin(d)
    reloaded._notices.pop(d, None)                  # 模拟 retention 淘汰
    dropped = reloaded.prune_pins()
    check("清理失效置顶", dropped == 1 and reloaded.pinned_ids() == [b],
          f"dropped={dropped} pins={reloaded.pinned_ids()}")
    raw2 = json.loads(state_path.read_text(encoding="utf-8"))
    check("清理结果已落盘",
          [p.get("id") for p in raw2.get("pins") or []] == [b],
          str(raw2.get("pins")))

    # (b2) 置顶**独立编号**：P1…递增、取消后空号复用、可按 P 取消
    from plugins.bupt_news.store import external_code, decode_external, external_url
    reloaded.clear_pins()
    n1 = reloaded.pin(a)
    n2 = reloaded.pin(b)
    n3 = reloaded.pin(c)
    check("置顶编号从 P1 起递增", (n1, n2, n3) == (1, 2, 3), f"{n1},{n2},{n3}")
    check("pinned_entries 带编号",
          [(e["n"], e["id"]) for e in reloaded.pinned_entries()]
          == [(1, a), (2, b), (3, c)], str(reloaded.pinned_entries()))
    check("取消 P2 生效", reloaded.unpin_number(2) is True, "")
    check("取消后 P2 空出", [e["n"] for e in reloaded.pinned_entries()] == [1, 3],
          str([e["n"] for e in reloaded.pinned_entries()]))
    # 注意 add() 往 `store` 写，而这里用的是 `reloaded` —— 直接写它
    e = reloaded.add_notice({
        "key": "1154:新占位", "newsid": "999001", "treeid": "1154",
        "title": "新的占位通知", "body": "新内容", "summary": "新",
        "summary_ai": True, "student_ai": True, "for_students": True,
        "date": "2026-10-09", "images": [], "tables": []})
    reused = reloaded.pin(e)
    check("★ 空号被复用（新置顶拿到 P2）", reused == 2, repr(reused))
    check("pin_entry 按编号查", (reloaded.pin_entry("P3") or {}).get("id") == c, 
          str(reloaded.pin_entry("P3")))
    check("pin_number_of 反查编号", reloaded.pin_number_of(c) == 3, "")
    # 重载后编号不变（持久化的是编号本身，不是推导出来的）
    re2 = StateStore(str(state_path), str(notices_path))
    # 持久化保留**置顶顺序**（P2 是后置顶的，排在末尾是正常的）
    check("★ 置顶编号在重载后保持",
          sorted((x["n"], x["id"]) for x in re2.pinned_entries())
          == [(1, a), (2, e), (3, c)], str(re2.pinned_entries()))

    # (b3) 非本地编号：**可解码**，不依赖映射表
    code = external_code({"treeid": "1759", "newsid": "142679"})
    check("外部编号形如 E<treeid>-<newsid>", code == "E1759-142679", repr(code))
    check("★ 编号可解码出 treeid/newsid", decode_external(code) == ("1759", "142679"),
          repr(decode_external(code)))
    check("★ 编号能还原原文链接",
          "wbtreeid=1759" in external_url(code)
          and "wbnewsid=142679" in external_url(code), external_url(code))
    check("本地编号/置顶编号不是外部编号",
          decode_external("476") is None and decode_external("P1") is None, "")
    check("缺 treeid/newsid 时返回空编号", external_code({"title": "x"}) == "", "")
    # 缓存被清掉也不影响：编号自带链接
    re2.external_put({"treeid": "1196", "newsid": "45767", "title": "旧通知",
                      "url": external_url("E1196-45767"), "images": [], "tables": []})
    check("external_get 缓存元数据",
          (re2.external_get("E1196-45767") or {}).get("title") == "旧通知", "")
    check("sweep 可回收（编号仍可解码）",
          isinstance(re2.external_sweep(ttl=0), int), "")
    check("缓存回收后编号依然可解码",
          decode_external("E1196-45767") == ("1196", "45767"), "")

    # (b4) 外部置顶：门户结果也能置顶（独立编号 + 快照），list 不因它崩
    ext_notice = {"treeid": "1036", "newsid": "95475", "title": "扫描件通知",
                  "date": "2021-12-09", "body": "",
                  "url": external_url("E1036-95475"),
                  "images": [{"pos": 0, "url": "http://x/a.jpg"}], "tables": []}
    ext_num = reloaded.pin_external(ext_notice)
    check("外部置顶分配到独立编号", ext_num == 4, repr(ext_num))
    check("重复外部置顶返回 None",
          reloaded.pin_external(ext_notice) is None, "")
    check("外部置顶可按 E 编号取消",
          reloaded.is_pinned_external("E1036-95475") is True, "")
    check("pinned_ids 跳过外部置顶（无本地 id 不 KeyError）",
          all(isinstance(i, int) for i in reloaded.pinned_ids()),
          str(reloaded.pinned_ids()))
    check("pinned_entries 带外部快照",
          any(e.get("ext") == "E1036-95475" and (e.get("notice") or {}).get("title")
              == "扫描件通知" for e in reloaded.pinned_entries()), "")

    # (b5) E 编号置顶要带 AI 摘要：摘要写进置顶快照，且能持久化
    from plugins.bupt_news.summarize import NoticeDigest

    class _ExtSum:
        enabled = True
        last_error = ""

        def __init__(self, digests):
            self.digests = digests
            self.calls = 0

        async def summarize(self, notices):
            self.calls += 1
            return dict(self.digests)

    plugin2 = BuptNewsPlugin(api=None, event_bus=None, timer=None, cmd_manager=None,
                             perm_manager=None, file_manager=None, logger=_logger(),
                             groups=None, cfg=_FakeCfg({"plugins": {"bupt_news": {
                                 "data_dir": str(tmp / "ext-sum")}}}))
    await plugin2.on_load()
    plugin2._store = StateStore(
        str(tmp / "ext-sum" / "s.json"), str(tmp / "ext-sum" / "n.jsonl"))
    plugin2._summary_retry_next_at = 0.0

    ext = {"treeid": "1196", "newsid": "45767", "title": "2017级研究生注射麻腮风疫苗",
           "date": "2017-10-09", "body": "校医院定于10月13日注射麻腮风疫苗",
           "url": external_url("E1196-45767"), "images": [], "tables": []}
    plugin2._summarizer = _ExtSum({"45767": NoticeDigest("研究生麻腮风疫苗", False)})
    got = await plugin2._summarize_external(ext)
    check("★ 外部通知能补出 AI 摘要", got.get("summary") == "研究生麻腮风疫苗",
          repr(got.get("summary")))
    check("外部通知补出学生判定", got.get("for_students") is False,
          repr(got.get("for_students")))

    # 置顶它 → 摘要进快照并落盘
    num = plugin2._store.pin_external(got)
    check("置顶外部通知返回编号", isinstance(num, int) and num >= 1, repr(num))
    snap = (plugin2._store.pin_entry(num) or {}).get("notice") or {}
    check("★ 置顶快照带摘要", snap.get("summary") == "研究生麻腮风疫苗",
          repr(snap.get("summary")))
    plugin2._store.save()
    re_ext = StateStore(str(tmp / "ext-sum" / "s.json"),
                        str(tmp / "ext-sum" / "n.jsonl"))
    persisted = (re_ext.pin_entry(num) or {}).get("notice") or {}
    check("★ 外部置顶的摘要持久化（重载后仍在）",
          persisted.get("summary") == "研究生麻腮风疫苗",
          repr(persisted.get("summary")))

    # 补摘要失败 → 保持 summary_ai 假，不写假摘要
    ext2 = dict(ext, newsid="45768")
    plugin2._summarizer = _ExtSum({})          # AI 什么都没返回
    got2 = await plugin2._summarize_external(ext2)
    check("摘要不可得时不写假摘要",
          not got2.get("summary") and not got2.get("summary_ai"),
          f"{got2.get('summary')!r} {got2.get('summary_ai')!r}")

    # update_external_notice：缓存与置顶快照都要写回（曾只改了副本）
    changed = plugin2._store.update_external_notice(
        {"treeid": "1196", "newsid": "45767", "summary": "改过的摘要",
         "summary_ai": True})
    check("外部通知更新命中置顶快照", changed >= 1, str(changed))
    check("★ 更新确实落到快照（不是改副本）",
          ((plugin2._store.pin_entry(num) or {}).get("notice") or {}).get("summary")
          == "改过的摘要", "")
    check("更新已持久化",
          ((StateStore(str(tmp / "ext-sum" / "s.json"),
                       str(tmp / "ext-sum" / "n.jsonl")).pin_entry(num)
            or {}).get("notice") or {}).get("summary") == "改过的摘要", "")

    # (b6) 置顶上限：到达上限拒绝；加 force 才替换最早置顶的（FIFO）
    from plugins.bupt_news.store import PIN_LIMIT_REACHED

    lim = StateStore(str(tmp / "pin-limit" / "s.json"),
                     str(tmp / "pin-limit" / "n.jsonl"))
    ids = []
    for i in range(6):
        ids.append(lim.add_notice({
            "key": f"k{i}", "newsid": str(8800 + i), "treeid": "1154",
            "title": f"通知{i}", "body": "b", "summary": f"概要{i}",
            "summary_ai": True, "student_ai": True, "for_students": True,
            "images": [], "tables": []}))
    check("上限内正常置顶",
          [lim.pin(n, limit=3) for n in ids[:3]] == [1, 2, 3], "")
    check("★ 到达上限时拒绝（返回 PIN_LIMIT_REACHED）",
          lim.pin(ids[3], limit=3) == PIN_LIMIT_REACHED, "")
    check("拒绝时没有改动置顶",
          [(e["n"], e["id"]) for e in lim.pinned_entries()]
          == [(1, ids[0]), (2, ids[1]), (3, ids[2])], "")
    check("★ force 替换掉最早置顶的那条",
          lim.pin(ids[3], limit=3, force=True) == 1, "")
    check("替换项被记下（给用户提示用）",
          (lim.last_pin_evicted or {}).get("id") == ids[0],
          str(lim.last_pin_evicted))
    check("★ 替换后仍是 3 条且编号紧凑",
          sorted((e["n"], e["id"]) for e in lim.pinned_entries())
          == [(1, ids[3]), (2, ids[1]), (3, ids[2])],
          str([(e["n"], e["id"]) for e in lim.pinned_entries()]))
    check("★ FIFO：再 force 一次淘汰第二个",
          lim.pin(ids[4], limit=3, force=True) == 2, "")
    check("FIFO 淘汰的是最早置顶的",
          (lim.last_pin_evicted or {}).get("id") == ids[1],
          str(lim.last_pin_evicted))
    check("limit=0 表示不限", lim.pin(ids[5], limit=0) is not None, "")
    check("外部置顶同样受上限约束",
          lim.pin_external({"treeid": "1196", "newsid": "45767",
                            "title": "外部", "url": external_url("E1196-45767"),
                            "images": [], "tables": []},
                           limit=3) == PIN_LIMIT_REACHED, "")
    check("外部置顶加 force 可替换",
          lim.pin_external({"treeid": "1196", "newsid": "45767",
                            "title": "外部", "url": external_url("E1196-45767"),
                            "images": [], "tables": []},
                           limit=3, force=True) is not None, "")

    # 命令层：拒绝提示 + force 生效 + 裸 --force/-f 识别
    check("裸 --force 能被识别", BuptNewsPlugin._wants_force("/buptnews pin 4 --force"),
          "")
    check("短选项 -f 能被识别", BuptNewsPlugin._wants_force("/buptnews pin 4 -f"), "")
    check("普通命令不会被误判", not BuptNewsPlugin._wants_force("/buptnews pin 4"), "")
    check("--force 不会混进 ids",
          "4" in BuptNewsPlugin._parse_tokens(["--force", "4", "-f"]), "")

    # (c) 搜索：标题/摘要/正文/单位/编号都可命中，大小写不敏感
    hits, total = store.search("疫苗")
    check("正文关键词命中", [r["id"] for r in hits] == [b] and total == 1,
          f"{[r['id'] for r in hits]} total={total}")
    hits, total = store.search("退补选")
    check("摘要关键词命中", [r["id"] for r in hits] == [a], f"{[r['id'] for r in hits]}")
    hits, total = store.search("同志")
    check("正文独有关键词命中", [r["id"] for r in hits] == [c], f"{[r['id'] for r in hits]}")
    check("大小写不敏感", store.search("ABC")[1] == store.search("abc")[1], "")
    check("无命中返回空", store.search("绝对不存在的词") == ([], 0), "")
    check("空关键词返回空", store.search("   ") == ([], 0), "")

    hits, total = store.search("通知")               # 四条标题都含"通知"
    check("按最新在前排序", [r["id"] for r in hits] == sorted([a, b, c, d],
                                                            reverse=True),
          f"{[r['id'] for r in hits]}")
    page1, total1 = store.search("通知", limit=2, page=1)
    page2, _ = store.search("通知", limit=2, page=2)
    check("分页：第 1 页 2 条", len(page1) == 2 and total1 == 4, f"{len(page1)}/{total1}")
    check("分页：第 2 页不重复", not ({r["id"] for r in page1}
                                 & {r["id"] for r in page2}), "")
    dated, dtot = store.search("通知", date="2026-09-01")
    check("按日期过滤", [r["id"] for r in dated] == [d] and dtot == 1,
          f"{[r['id'] for r in dated]}")

    # (d) 命令层：pin/unpin/search 的实际回复
    from napcat.command import CommandManager
    from napcat.permission import PermissionManager
    from napcat.models import MessageEvent

    perm = PermissionManager()
    perm.set_superusers([1])
    cmds = CommandManager(perm_manager=perm)
    plugin = BuptNewsPlugin(api=None, event_bus=None, timer=None, cmd_manager=cmds,
                            perm_manager=perm, file_manager=None, logger=_logger(),
                            groups=None, cfg=_FakeCfg({"plugins": {"bupt_news": {
                                "data_dir": str(data_dir),
                                "list_page_size": 3}}}))
    await plugin.on_load()
    plugin._store = store
    plugin._register_commands()

    def ev(text):
        return MessageEvent(message_type="private", user_id=1, message=text)

    async def say(text):
        r = await cmds.handle(ev(text))
        return r if isinstance(r, str) else str(r)

    store.clear_pins()
    out = await say("/buptnews pin 2 3")
    check("pin 支持多个编号", store.pinned_ids() == [2, 3] and "已置顶 2 条" in out,
          f"{store.pinned_ids()} {out[:60]}")
    out = await say("/buptnews pin 2,3，4")          # 逗号/中文逗号/去重
    check("pin 支持逗号分隔并去重",
          store.pinned_ids() == [2, 3, 4] and "已在置顶" in out,
          f"{store.pinned_ids()} {out[:80]}")

    out = await say("/buptnews list")
    check("★ list 里置顶条目前置", "📌 置顶（3）" in out, out[:120])
    check("list 置顶区按 P 编号列出",
          out.count("📌") >= 3 and "P1 📌" in out or "P2 📌" in out, out[:200])

    out = await say("/buptnews pin")
    check("pin 无参查看置顶列表", "置顶通知（3）" in out, out[:80])

    # 置顶展示：**只有 P 编号 + 概要**（不带 E 编号/日期/本地通知编号）
    plugin._store.clear_pins()
    num_local = plugin._store.pin(a)
    num_ext = plugin._store.pin_external({
        "treeid": "1196", "newsid": "45767", "title": "很长的原标题不该出现",
        "date": "2017-10-09", "body": "", "url": external_url("E1196-45767"),
        "summary": "外部摘要", "summary_ai": True,
        "images": [], "tables": []})
    out = await say("/buptnews pin")
    check("★ 置顶行只显示 P 编号 + 概要",
          f"P{num_local} 📌 " in out and f"P{num_ext} 📌 外部摘要" in out, out[:220])
    check("★ 置顶行不显示 E 编号", "E1196-45767" not in out, out[:220])
    check("★ 置顶行不显示日期", "2017-10-09" not in out, out[:220])
    check("★ 置顶行不显示本地通知编号",
          f"P{num_local} 📌 {a}" not in out and f"P{num_local} 📌 #{a}" not in out,
          out[:220])
    check("外部置顶有摘要时不再显示原标题", "很长的原标题不该出现" not in out,
          out[:220])

    out = await say("/buptnews list")
    check("★ list 的置顶区同口径（P + 概要）",
          f"P{num_ext} 📌 外部摘要" in out and "E1196-45767" not in out, out[:300])
    # 恢复后续用例依赖的置顶（编号会重新分配，只要求集合一致）
    plugin._store.clear_pins()
    for i in (2, 3, 4):
        plugin._store.pin(i)

    # 外部置顶也要能在 list 的置顶区显示（不能 KeyError）
    plugin._store.pin_external({"treeid": "1036", "newsid": "95475",
                                "title": "扫描件通知", "date": "2021-12-09",
                                "body": "", "url": external_url("E1036-95475"),
                                "images": [], "tables": []})
    out = await say("/buptnews list")
    check("★ list 能显示外部置顶（不再 KeyError）",
          "📌" in out and "扫描件通知" in out, out[:200])
    plugin._store.unpin("E1036-95475")

    out = await say("/buptnews unpin 3")
    check("unpin 单条", store.pinned_ids() == [2, 4] and "已取消置顶" in out,
          f"{store.pinned_ids()}")
    out = await say("/buptnews unpin")
    check("unpin 无参清空全部", store.pinned_ids() == [] and "全部置顶" in out,
          f"{store.pinned_ids()} {out[:60]}")

    # 默认会同时搜门户（这里没有门户桩），所以显式限定本地
    out = await say("/buptnews search 疫苗 --source=local")
    check("★ search 命中并显示标题", "疫苗接种通知" in out and "命中 1 条" in out,
          out[:120])
    out = await say("/buptnews search 绝对不存在的词 --source=local")
    check("search 无命中给明确提示", "没有找到" in out, out[:60])
    out = await say("/buptnews search 通知 1 2 --source=local")
    check("search 分页参数生效", "第 2 页" in out and "每页 1" in out, out[:90])

    # 置顶标记也要出现在搜索结果里
    store.pin(b)
    out = await say("/buptnews search 疫苗")
    check("搜索结果标出置顶", "📌" in out, out[:80])
    store.clear_pins()


async def test_portal_search(tmp: Path):
    print("\n[24] 门户检索：解析/分页/合并/降级/get 链接")
    from plugins.bupt_news import portal as pt
    from plugins.bupt_news.main import BuptNewsPlugin
    from plugins.bupt_news.store import StateStore

    # (a) 结果解析：日期在 <li> **外面**（独立 <td>），不能复用 parse_list
    row = ('<tr><li><td><a href="xntz_content.jsp?urltype=news.NewsContentUrl'
           '&wbtreeid=1759&wbnewsid=142679" style="x">疫苗通知标题</a>&nbsp;'
           '<span>点击数：7271</span></td></li>'
           '<td align="right"><span>2026-10-08</span></td></tr>')
    got = pt.parse_search_results(row)
    check("门户结果解析出 1 条", len(got) == 1, str(len(got)))
    if got:
        n = got[0]
        check("门户结果带日期（在 <li> 外也能取到）", n.date == "2026-10-08", repr(n.date))
        check("门户结果带 treeid/newsid", (n.treeid, n.newsid) == ("1759", "142679"),
              f"{n.treeid}:{n.newsid}")
        check("门户结果标题正确", n.title == "疫苗通知标题", repr(n.title))
        check("门户结果 URL 绝对化", n.url.startswith("http://"), n.url[:40])

    check("总数解析「共 23816 条」", pt.parse_search_total("x 共23816条 y") == 23816, "")
    check("总数缺失时为 0", pt.parse_search_total("no count") == 0, "")
    # 同一通知被挂到多个子站时，按 treeid:newsid 去重
    dup = row + row.replace("1759", "1759")
    check("重复结果被去重", len(pt.parse_search_results(dup)) == 1, "")

    # (b) 表单树 id 提取（写死会漏栏目）
    page_html = ('<form action="fz_ssjg.jsp?wbtreeid=1154" method="post">'
                 "<input type='hidden' name='wbtreeids' value=1155,1158,1159>"
                 "</form>")
    form = pt._search_form(page_html, "1154")
    check("从页面读到栏目树 id", form["treeids"] == "1155,1158,1159", str(form))
    check("没有表单时返回空（退回全站检索）",
          pt._search_form("<html></html>", "1154")["treeids"] == "", "")

    # (c) 命令层：本地 + 门户合并、门户失败降级、source/content 生效
    from napcat.command import CommandManager
    from napcat.permission import PermissionManager
    from napcat.models import MessageEvent

    data_dir = tmp / "portal-search"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(str(data_dir / "state.json"), str(data_dir / "notices.jsonl"))
    local_id = store.add_notice({
        "key": "1154:142679", "newsid": "142679", "treeid": "1759",
        "title": "本地：2026级新生接种麻腮风疫苗", "body": "本地正文",
        "summary": "本地摘要", "summary_ai": True, "student_ai": True,
        "for_students": True, "date": "2026-10-08", "images": [], "tables": []})

    perm = PermissionManager()
    perm.set_superusers([1])
    cmds = CommandManager(perm_manager=perm)
    plugin = BuptNewsPlugin(api=None, event_bus=None, timer=None, cmd_manager=cmds,
                            perm_manager=perm, file_manager=None, logger=_logger(),
                            groups=None, cfg=_FakeCfg({"plugins": {"bupt_news": {
                                "data_dir": str(data_dir)}}}))
    await plugin.on_load()
    plugin._store = store
    plugin._register_commands()

    class _PortalStub:
        def __init__(self):
            self.calls = []
            self.fail = False

        async def search(self, keyword, *, page=1, page_size=10, content=True):
            self.calls.append((keyword, page, content))
            if self.fail:
                raise RuntimeError("门户 500")
            items = [pt.Notice(column="1759", newsid="142679", treeid="1759",
                               title="本地：2026级新生接种麻腮风疫苗",
                               date="2026-10-08",
                               url="http://my.bupt.edu.cn/xntz_content.jsp"
                                   "?urltype=news.NewsContentUrl&wbtreeid=1759"
                                   "&wbnewsid=142679"),
                     pt.Notice(column="1196", newsid="45767", treeid="1196",
                               title="2017级研究生注射麻腮风疫苗",
                               date="2017-10-09",
                               url="http://my.bupt.edu.cn/xntz_content.jsp"
                                   "?urltype=news.NewsContentUrl&wbtreeid=1196"
                                   "&wbnewsid=45767")]
            return {"total": 2, "page": page, "pages": 1, "size": page_size,
                    "items": items}

    stub = _PortalStub()
    plugin._portal = stub

    async def say(text):
        r = await cmds.handle(MessageEvent(message_type="private", user_id=1,
                                           message=text))
        return r if isinstance(r, str) else str(r)

    out = await say("/buptnews search 麻腮风")
    check("★ 默认同时搜本地与门户",
          "本地" in out and "信息门户" in out, out[:120])
    check("★ 本地已有编号直接给出",
          f"{local_id}" in out, out[:200])
    # 门户结果现在给**可解码的 E 编号**（不再给长链接）
    check("门户独有结果给出 E 编号",
          "E1196-45767" in out, out[-300:])
    check("门户结果提示可用 E 编号 get/pin",
          "E 编号" in out, out[-200:])
    check("本地库里已有的门户结果标出编号（可 get 编号）",
          f"#{local_id}" in out, out[:300])

    stub.calls.clear()
    out = await say("/buptnews search 麻腮风 --source=local")
    check("source=local 不请求门户", stub.calls == [], str(stub.calls))
    check("source=local 不含门户段", "信息门户" not in out, out[:80])

    plugin._portal_search_cache.clear()      # 上一步 all 已缓存同参数结果
    out = await say("/buptnews search 麻腮风 --source=portal")
    check("source=portal 只请求门户", len(stub.calls) == 1, str(stub.calls))
    check("source=portal 不含本地库条目",
          "📚 本地库" not in out and "🌐 信息门户" in out, out[:160])

    stub.calls.clear()
    out = await say("/buptnews search 麻腮风 --content=false")
    check("content=false 传给门户", stub.calls and stub.calls[0][2] is False,
          str(stub.calls))
    # 5 分钟内重复搜索命中缓存（同样参数不再打门户）
    stub.calls.clear()
    await say("/buptnews search 麻腮风 --content=false")
    check("★ 同参数短时缓存命中（不重复请求门户）", stub.calls == [], str(stub.calls))

    # 门户故障 → 降级为本地结果 + 明确提示
    stub.fail = True
    plugin._portal_search_cache.clear()
    out = await say("/buptnews search 麻腮风 --content=true")
    check("★ 门户失败时仍给本地结果", "本地库" in out and str(local_id) in out, out[:160])
    check("门户失败有明确提示", "门户检索失败" in out, out[-200:])
    stub.fail = False

    # (d) get 支持链接（门户结果没有本地编号）
    class _DetailPortal:
        def __init__(self):
            self.urls = []

        async def fetch_detail(self, url):
            self.urls.append(url)
            return {"title": "门户里的旧通知", "dept": "后勤部",
                    "date": "2017-10-09", "body": "旧通知正文内容",
                    "images": [], "tables": []}

    detail = _DetailPortal()
    plugin._portal = detail
    chain = await plugin._render_notice(
        {"title": "门户里的旧通知", "dept": "后勤部", "date": "2017-10-09",
         "body": "旧通知正文内容", "url": "http://x/1", "images": [], "tables": [],
         "student_ai": False},
        prefix="🔗 ")
    txt = " ".join(str(x.get("data", {}).get("text", ""))
                   for x in (chain.build() if hasattr(chain, "build")
                             else sum((c.build() for c in chain), []))
                   if x.get("type") == "text")
    check("★ 门户临时记录能渲染出全文", "旧通知正文内容" in txt, txt[:80])
    check("★ 门户临时记录不显示假摘要（降级标题）",
          "🤖" not in txt, txt[:80])


async def test_afterclass(tmp: Path):
    print("\n[25] 第二课堂：JWT 解析/活动归一化/去重基线与推送")
    from plugins.bupt_news.activity_store import ActivityStore
    from plugins.bupt_news.dekt import (DektAuthError, DektClient, decode_jwt,
                                        format_activity, load_token,
                                        normalize_activity, to_local_time)

    # (a) JWT 只用来取 sub/exp，不校验签名
    def make_jwt(payload):
        def b64(obj):
            raw = json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"{b64({'alg': 'HS256'})}.{b64(payload)}.sig"

    tok = make_jwt({"sub": 2026000000, "exp": 1900000000})
    check("JWT 解出 sub", decode_jwt(tok).get("sub") == 2026000000, "")
    check("坏 token 解出空 dict",
          decode_jwt("garbage") == {} and decode_jwt("") == {}, "")
    client = DektClient(token=tok)
    check("client 取到 user_id", client.user_id == "2026000000", client.user_id)
    check("有 token 时 available", client.available is True, "")
    # ⚠️ 必须给一个**不存在**的 token_file：默认路径下用户可能真的放了 token，
    # 那样这条断言就会因为环境而非代码失败。
    check("无 token 时 unavailable",
          DektClient(token="", token_file=str(tmp / "no-token.txt")).available
          is False, "")
    check("token 走参数优先于文件",
          load_token(tok, path=str(tmp / "no-such-token")) == tok, "")
    # 粘贴容错：控制台复制带引号、设置页带 Bearer 前缀、混进换行
    from plugins.bupt_news.dekt import clean_token
    check("粘贴带引号能清洗", clean_token(f'"{tok}"') == tok, "")
    check("粘贴带 Bearer 前缀能清洗", clean_token(f"Bearer {tok}") == tok, "")
    check("多行粘贴只取 JWT 那行", clean_token(f"token =\n{tok}\n") == tok, "")

    # 热更新：覆盖 token.txt 即可，不需要重启；内容不变不算变化
    tok_file = tmp / "token-hot.txt"
    tok_file.write_text(tok, encoding="utf-8")
    hot = DektClient(token_file=str(tok_file))
    check("从文件载入 token", hot.available is True and hot.user_id == "2026000000", "")
    check("内容没变时不报变化", hot.reload_token() is False, "")
    newer = make_jwt({"sub": 2026000000, "exp": 1900000777})   # 必须与上面的不同
    tok_file.write_text(f'"{newer}"\n', encoding="utf-8")     # 带引号也能用
    check("★ 覆盖 token.txt 即生效（无需重启）", hot.reload_token() is True, "")
    check("热更新后仍是有效 JWT", hot.available is True, "")
    check("token 过期时间可读", hot.expiry() is not None, "")

    # (b) 活动归一化：字段别名 + 时间戳 + 枚举翻译
    norm = normalize_activity({
        "id": 42, "title": "实验室开放日", "place": "沙河 S203", "area": 1,
        "class_id": 4, "org_name": "计算机学院", "demands": 9,
        "start_t_ms": 1791000000000, "end_t_ms": 1791007200000,
        "apply_start_t_ms": 1790000000000, "apply_end_t_ms": 1790900000000,
    }, {"4": "学术科创"})
    check("活动 id 归一化", norm["id"] == "42", str(norm["id"]))
    check("活动标题/地点", norm["title"] == "实验室开放日" and norm["place"] == "沙河 S203",
          "")
    check("area 枚举翻成校区名", norm["area"] == "沙河", str(norm["area"]))
    check("class_id 翻成分类名", norm["class_name"] == "学术科创", "")
    check("demands 位包含报名/频道",
          norm["can_apply"] is True and norm["demands"] == 9, str(norm["demands"]))
    check("毫秒时间戳转本地时间",
          norm["start"] == "2026-10-03 12:00", str(norm["start"]))
    check("秒级时间戳也能转", to_local_time(1791000000).startswith("2026-10-03"),
          to_local_time(1791000000))
    check("非时间戳原样返回", to_local_time("待定") == "待定", "")
    # 真实格式是 ISO8601 UTC（`2026-10-26T07:00:00Z`），要转成本机时区
    iso = to_local_time("2026-10-26T07:00:00Z")
    check("ISO8601 转本地时间（日期不变、带时分）",
          iso.startswith("2026-10-26") and len(iso) == 16, iso)
    check("★ 1970 哨兵视为没有时间",
          to_local_time("1970-01-01T00:00:00Z") == "", "")

    # 按**真实响应**核对过的字段名（不是猜的）：activity_class / activity_*_time
    real = normalize_activity({
        "id": 13452, "name": "音乐实践工作坊", "activity_class": 30,
        "location": "学生活动中心201教室", "attend_limit": 20, "demands": 23,
        "area": 1, "activity_start_time": "2026-10-26T07:00:00Z",
        "activity_end_time": "2026-10-26T08:30:30Z",
        "signup_start_time": "1970-01-01T00:00:00Z",
        "signup_end_time": "2026-10-26T08:00:00Z",
    }, {"30": "文艺活动"})
    check("真实字段 activity_class 映射分类", real["class_name"] == "文艺活动",
          repr(real["class_name"]))
    check("真实字段 location/attend_limit",
          real["place"] == "学生活动中心201教室" and real["attend_limit"] == 20,
          f'{real["place"]}/{real["attend_limit"]}')
    check("真实字段 activity_*_time 解析", real["start"] != "" and real["end"] != "",
          f'{real["start"]}/{real["end"]}')
    check("报名起点是哨兵时不显示", real["apply_start"] == "",
          repr(real["apply_start"]))
    check("报名终点正常显示", real["apply_end"] != "", repr(real["apply_end"]))
    check("demands=23 含报名位", real["can_apply"] is True, "")
    # 别名：不同字段名都能命中
    alt = normalize_activity({"activity_id": 7, "name": "别名活动",
                             "enroll_end_t_ms": 1790900000000, "address": "教三"})
    check("别名表能命中 id/标题/地址",
          alt["id"] == "7" and alt["title"] == "别名活动" and alt["place"] == "教三",
          f'{alt["id"]}/{alt["title"]}/{alt["place"]}')
    check("别名表能命中报名截止", alt["apply_end"] != "", str(alt["apply_end"]))

    text = format_activity(norm)
    check("推送文案含标题/地点/链接",
          "实验室开放日" in text and "沙河 S203" in text and "/activity/42" in text,
          text[:80])
    check("推送文案标题不用 🎪（消息头已用）", text.startswith("▸ "), text[:12])

    # (c) 存储：去重、已推送、积压
    store = ActivityStore(str(tmp / "acts" / "activities.json"), max_records=5)
    first = store.upsert([{"id": "1", "title": "A"}, {"id": "2", "title": "B"}])
    check("首次入库返回全部为新增", [r["id"] for r in first] == ["1", "2"], "")
    again = store.upsert([{"id": "1", "title": "A改"}, {"id": "3", "title": "C"}])
    check("★ 重复活动不算新增（只报新出现的）",
          [r["id"] for r in again] == ["3"], str([r["id"] for r in again]))
    check("重复活动会就地刷新内容",
          next(r for r in store.all_records() if r["id"] == "1")["title"] == "A改", "")
    check("未推送的全部在积压里",
          [r["id"] for r in store.backlog()] == ["1", "2", "3"], "")
    store.mark_pushed(["1"])
    check("★ 标记已推送后退出积压",
          [r["id"] for r in store.backlog()] == ["2", "3"], "")
    reloaded = ActivityStore(str(tmp / "acts" / "activities.json"), max_records=5)
    check("★ 积压跨实例保持（免打扰补发的基础）",
          [r["id"] for r in reloaded.backlog()] == ["2", "3"], "")
    check("冗余记录按上限裁剪", len(reloaded.all_records()) <= 5, "")

    # (d) 独立绑定：一个绑定 = 一个目标 + 它自己的 JWT
    from napcat.command import CommandManager
    from napcat.models import MessageEvent
    from napcat.permission import PermissionManager
    from plugins.bupt_news.main import BuptNewsPlugin

    class _FakeApi:
        def __init__(self):
            self.sent = []

        async def send_group_msg(self, gid, chain):
            self.sent.append(("group", str(gid), chain))

        async def send_private_msg(self, uid, chain, group_id=None):
            self.sent.append(("user", str(uid), chain))

    class _FakeDekt:
        available = True

        def __init__(self, acts):
            self.acts = list(acts)

        def reload_token(self):
            return False

        def expiry(self):
            return None

        async def me(self):
            return {"user": {}, "digest": "D"}

        async def activities(self, **_kw):
            return list(self.acts)

        async def activity_classes(self):
            return [{"id": 1, "name": "文体活动"}]

    def act(i, title):
        return {"id": i, "name": title, "location": "P", "area": 1,
                "activity_class": 1, "demands": 23,
                "activity_start_time": "2026-10-26T07:00:00Z"}

    data_dir = tmp / "dekt"
    api = _FakeApi()
    perm = PermissionManager()
    perm.set_superusers([1])
    plugin = BuptNewsPlugin(api=api, event_bus=None, timer=None,
                            cmd_manager=CommandManager(perm_manager=perm),
                            perm_manager=perm, file_manager=None,
                            logger=_logger(), groups=None,
                            cfg=_FakeCfg({"plugins": {"bupt_news": {
                                "data_dir": str(data_dir), "groups": [999],
                                "afterclass": {"enabled": True,
                                               "max_per_round": 2}}}}))
    await plugin.on_load()
    plugin._in_quiet_hours = lambda now=None: False

    def make_binding(kind, target, client):
        return {"name": f"{kind}{target}", "kind": kind, "target": target,
                "token_file": f"{plugin._data_dir}/t-{kind}.txt",
                "client": client,
                "store": ActivityStore(
                    plugin._binding_store_path(kind, target))}

    plugin._act_bindings = [
        make_binding("group", 111, _FakeDekt([act(1, "群1活动")])),
        make_binding("user", 222, _FakeDekt([act(2, "私聊A"), act(3, "私聊B")])),
    ]

    # 首轮：每个绑定各自建基线，一条都不发
    check("★ 每个绑定各自建基线、一条都不发",
          await plugin._sync_activities() == (3, 0, True) and api.sent == [],
          f"sent={api.sent}")
    check("基线后各绑定积压都空",
          all(not b["store"].backlog() for b in plugin._act_bindings), "")

    # 每绑定各来一条新的 → 各自发到**自己的**目标
    plugin._act_bindings[0]["client"].acts.append(act(10, "群1新活动"))
    plugin._act_bindings[1]["client"].acts.append(act(20, "私聊新活动"))
    check("★ 两个绑定各推 1 条", await plugin._sync_activities() == (2, 2, True),
          str(len(api.sent)))
    targets = sorted((k, t) for k, t, _ in api.sent)
    check("★ 各绑定只发自己的目标", targets == [("group", "111"),
                                              ("user", "222")], str(targets))
    check("★ 活动不发给通知的 groups（999）",
          all(t != "999" for _, t, _ in api.sent), str(targets))
    texts = ["".join(x.get("data", {}).get("text", "") for x in ch.build()
                     if x.get("type") == "text") for _, _, ch in api.sent]
    check("各绑定只收到自己的活动",
          any("群1新活动" in t and "私聊新活动" not in t for t in texts)
          and any("私聊新活动" in t and "群1新活动" not in t for t in texts), "")
    check("活动推送不带 @全体成员",
          all("@全体成员" not in t for t in texts), "")

    # 去重是**按绑定**的：同一个活动 id 在另一个绑定里仍算新
    plugin._act_bindings[1]["client"].acts.append(act(10, "群1新活动"))
    await plugin._sync_activities()
    check("★ 去重按绑定隔离（同 id 在别的绑定算新）",
          plugin._act_bindings[1]["store"].is_pushed(10) is True, "")

    # 单轮上限也是按绑定
    plugin._act_bindings[0]["client"].acts += [act(30, "a"), act(31, "b"),
                                              act(32, "c")]
    before = len(api.sent)
    await plugin._sync_activities()
    got = [t for k, t, _ in api.sent[before:]]
    check("★ 单轮上限按绑定生效（3 条新只推 2）", len(got) == 1, str(len(got)))
    check("剩余 1 条留在该绑定积压",
          len(plugin._act_bindings[0]["store"].backlog()) == 1,
          str(len(plugin._act_bindings[0]["store"].backlog())))

    # 免打扰：只入库不发送
    before = len(api.sent)
    plugin._in_quiet_hours = lambda now=None: True
    plugin._act_bindings[0]["client"].acts.append(act(40, "静默期活动"))
    await plugin._sync_activities()
    check("★ 免打扰期间不发送", len(api.sent) == before, str(len(api.sent)))
    check("免打扰期间入库成积压",
          any(r["id"] == "40" for r in plugin._act_bindings[0]["store"].backlog()),
          "")
    plugin._in_quiet_hours = lambda now=None: False
    await plugin._sync_activities()
    check("★ 醒来后补发该绑定的积压", len(api.sent) > before, "")

    # 一个绑定坏掉不影响另一个
    class _DeadDekt:
        available = True

        def reload_token(self):
            return False

        def expiry(self):
            return None

        async def me(self):
            raise DektAuthError("token 无效或已过期")

    plugin._act_bindings[0]["client"] = _DeadDekt()
    plugin._act_bindings[1]["client"].acts.append(act(50, "另一个绑定的新活动"))
    before = len(api.sent)
    await plugin._sync_activities()
    check("★ 一个绑定 token 失效不影响另一个",
          len(api.sent) == before + 1, str(len(api.sent) - before))

    # 状态行：多绑定汇总
    line = plugin._afterclass_status_line()
    check("状态行含绑定数与活动数",
          "2 个绑定" in line and "条活动" in line, line)

    # (e) 绑定表：targets 归一化 + 活动绑定构建
    plugin.config.set("targets", {
        "groups": {"123456": {"message": True, "event": True,
                              "token_file": str(tmp / "g.txt")},
                   "0": {"message": True},            # 非法 id → 丢
                   "abc": {"message": True}},          # 非数字 → 丢
        "users": {"654321": {"message": False, "event": True},
                  "-5": {"message": True}},            # 负数 → 丢
    })
    t = plugin._targets()
    check("targets 只留下合法 id",
          sorted(t["groups"]) == [123456] and sorted(t["users"]) == [654321],
          f"groups={sorted(t['groups'])} users={sorted(t['users'])}")
    check("event 绑定的数量正确",
          [(k, i) for k, i, _ in plugin._event_targets()]
          == [("group", 123456), ("user", 654321)],
          str([(k, i) for k, i, _ in plugin._event_targets()]))
    check("只把 message=true 的当推送目标",
          plugin._message_targets() == ([123456], []),
          str(plugin._message_targets()))

    built = plugin._build_activity_bindings(
        {"base": "https://dekt.bupt.edu.cn", "max_records": 10})
    check("活动绑定只取 event=true 的",
          [(b["kind"], b["target"]) for b in built]
          == [("group", 123456), ("user", 654321)],
          str([(b["kind"], b["target"]) for b in built]))
    check("显式 token_file 生效",
          built[0]["token_file"] == str(tmp / "g.txt"), built[0]["token_file"])
    check("没配 token_file 时落到数据目录",
          built[1]["token_file"] == os.path.join(
              plugin._data_dir, "dekt-tokens", "user-654321.txt"),
          built[1]["token_file"])

    # 旧配置兜底：groups/users/binding/afterclass.bindings 合成出 targets
    plugin.config.set("targets", None)
    plugin.config.set("groups", [111])
    plugin.config.set("users", [222])
    plugin.config.set("binding", {"groups": [333], "users": [444]})
    plugin.config.set("afterclass", {"enabled": True, "bindings": [
        {"group": 111, "token_file": str(tmp / "legacy.txt")}]})
    t2 = plugin._targets()
    check("★ 旧配置能合成 targets（推送目标 + 白名单 + 活动绑定）",
          sorted(t2["groups"]) == [111, 333] and sorted(t2["users"]) == [222, 444],
          f"groups={sorted(t2['groups'])} users={sorted(t2['users'])}")
    check("旧活动绑定映射成 event=true 并保留 token 文件",
          t2["groups"][111]["event"] is True
          and t2["groups"][111]["token_file"] == str(tmp / "legacy.txt"),
          str(t2["groups"][111]))
    check("白名单条目只给准入、不推通知",
          t2["groups"][333]["message"] is False, str(t2["groups"][333]))
    plugin.config.set("groups", [])
    plugin.config.set("users", [])
    plugin.config.set("binding", {"groups": [], "users": []})
    plugin.config.set("afterclass", {"enabled": True})

    # (f2) 活动刷新节奏：随机 10–60 分钟，**不跟着通知每轮跑**
    import time as _time
    from plugins.bupt_news.dekt import USER_AGENT

    check("★ UA 锁死为微信客户端（MicroMessenger + miniProgram）",
          "MicroMessenger" in USER_AGENT and "miniProgram" in USER_AGENT,
          USER_AGENT[:60])
    check("UA 不含自相矛盾的桌面平台串（Linux/Windows/Chrome）",
          "X11" not in USER_AGENT and "Windows" not in USER_AGENT
          and "Chrome/" not in USER_AGENT, USER_AGENT[:60])

    plugin._act_bindings = []
    check("没有绑定时不排活动刷新", plugin._activity_due() is False, "")

    plugin._act_bindings = [make_binding("group", 111, _FakeDekt([]))]
    plugin._store.meta_set("acts_next_at", 0)
    check("★ 首次（没有下次时间）立刻可以刷", plugin._activity_due() is True, "")

    plugin.config.set("afterclass", {"enabled": True,
                                     "interval_min": 600, "interval_max": 3600})
    now = _time.time()
    nxt = plugin._schedule_next_activity(now)
    check("★ 下次刷新落在 10–60 分钟内",
          600 <= nxt - now <= 3600, f"{nxt - now:.0f}s")
    check("★ 刷新后同轮不再重复刷", plugin._activity_due(now + 1) is False, "")

    # 抖动：多次取样不应恒定（随机而不是固定周期）
    samples = {round(plugin._schedule_next_activity(now) - now) for _ in range(6)}
    check("刷新间隔是随机的（不是固定周期）", len(samples) > 1, str(sorted(samples)))

    # 边界：min == max 时退化成固定值
    plugin.config.set("afterclass", {"enabled": True,
                                     "interval_min": 60, "interval_max": 60})
    check("min=max 时退化为固定间隔",
          round(plugin._schedule_next_activity(now) - now) == 60, "")

    # 落盘：热重载/重启后不该立刻又刷一遍
    plugin.config.set("afterclass", {"enabled": True,
                                     "interval_min": 600, "interval_max": 3600})
    plugin._schedule_next_activity(now)
    saved = plugin._store.meta_get("acts_next_at")
    # 直接重开**同一个 state.json**：插件热重载/重启后读的就是它
    from plugins.bupt_news.store import StateStore
    again_store = StateStore(plugin._store.state_path, plugin._store.notices_path)
    check("★ 下次刷新时间跨实例保持（重载后不立刻重刷）",
          abs(float(again_store.meta_get("acts_next_at")) - float(saved)) < 1e-6,
          str(again_store.meta_get("acts_next_at")))
    plugin._store = again_store          # 换成"重载后"的实例继续断言
    check("重载后同样判为「还没到点」", plugin._activity_due(now + 1) is False, "")
    check("状态行显示下次刷新时间",
          "下次刷新" in plugin._afterclass_status_line(),
          plugin._afterclass_status_line())

    # 没有绑定 / 关闭时的状态行
    plugin._act_bindings = []
    check("没有绑定时状态行给出提示",
          "没有可用绑定" in plugin._afterclass_status_line(),
          plugin._afterclass_status_line())
    plugin.config.set("afterclass", {"enabled": False})
    check("关闭时状态行显示未开启",
          "未开启" in plugin._afterclass_status_line(),
          plugin._afterclass_status_line())

    # (f2b) get 的标题行不能重复编号（线上出现过 `📢 #476 #476 标题`）
    #       编号由 `_render_notice` 统一拼，`_load_reference` 的 prefix 只带
    #       "渲染层不知道的信息"（P 置顶号 / 🔗 门户编号）。
    head = await plugin._render_notice({"id": 7, "title": "标题A", "summary_ai": False},
                                       prefix="", retry_summary=False)
    parts = head if isinstance(head, list) else [head]
    head_text = "".join(seg.get("data", {}).get("text", "")
                        for part in parts for seg in part.build())
    check("★ get 标题行只有一个编号",
          head_text.count("#7") == 1, head_text.splitlines()[0][:60])
    rec_p, prefix_p, _ = await plugin._load_reference("P1")
    check("P 编号仍带前缀（置顶号不能丢）", prefix_p.startswith("P1"), prefix_p)
    rec_l, prefix_l, _ = await plugin._load_reference("7")
    check("★ 本地编号不再重复塞进 prefix", prefix_l == "", repr(prefix_l))

    # (f3) 免打扰时段：**连访问都不做**（不只是不推送）
    class _CountingDekt(_FakeDekt):
        calls = 0

        async def me(self):
            type(self).calls += 1
            return {"user": {}, "digest": "D"}

    plugin.config.set("afterclass", {"enabled": True, "quiet_hours": {
        "start": "23:00", "end": "07:00"}})
    counted = _CountingDekt([])
    plugin._act_bindings = [make_binding("group", 111, counted)]
    _CountingDekt.calls = 0
    plugin._store.meta_set("acts_next_at", 0)          # 早就该刷了
    plugin._in_quiet_hours = lambda now=None: True
    await plugin._sync_once(reason="定时")
    check("★ 免打扰时段不访问第二课堂（0 次请求）",
          _CountingDekt.calls == 0, str(_CountingDekt.calls))
    check("★ 免打扰时不推进下次刷新（醒来立刻补拉）",
          plugin._store.meta_get("acts_next_at") == 0,
          str(plugin._store.meta_get("acts_next_at")))
    check("状态行标出「免打扰中·不访问」",
          "免打扰中" in plugin._afterclass_status_line(),
          plugin._afterclass_status_line())

    _CountingDekt.calls = 0
    await plugin._sync_once(reason="手动", force_acts=True)
    check("★ 夜里手动同步也不访问（严格不打扰）",
          _CountingDekt.calls == 0, str(_CountingDekt.calls))

    plugin._in_quiet_hours = lambda now=None: False
    _CountingDekt.calls = 0
    await plugin._sync_once(reason="定时")
    check("★ 过了免打扰立刻补拉", _CountingDekt.calls > 0, str(_CountingDekt.calls))
    check("补拉后排好下次刷新",
          float(plugin._store.meta_get("acts_next_at") or 0) > _time.time(), "")

    # (f3b) 拉取失败 → 短间隔重试（否则一次抖动白等 10–60 分钟，活动漏掉）
    class _NetFail(_FakeDekt):
        async def activities(self, **_kw):
            raise RuntimeError("Cannot connect to host dekt.bupt.edu.cn")

    plugin.config.set("afterclass", {"enabled": True, "interval_min": 600,
                                     "interval_max": 3600,
                                     "interval_retry": 300})
    plugin._in_quiet_hours = lambda now=None: False
    plugin._act_bindings = [make_binding("group", 111, _NetFail([]))]
    plugin._store.meta_set("acts_next_at", 0)
    ok = await plugin._sync_activities()
    check("★ 网络失败时该绑定标记为失败（ok=False）",
          ok == (0, 0, False), str(ok))

    plugin._store.meta_set("acts_next_at", 0)
    before = _time.time()
    await plugin._sync_once(reason="定时")
    nxt = float(plugin._store.meta_get("acts_next_at") or 0)
    gap = nxt - before
    check("★ 失败后按短间隔重试（≈interval_retry，而不是 10–60 分钟）",
          0 < gap <= 310, f"{gap:.0f}s")

    # 成功时仍按 10–60 分钟
    plugin._act_bindings = [make_binding("group", 111, _FakeDekt([]))]
    plugin._store.meta_set("acts_next_at", 0)
    before = _time.time()
    await plugin._sync_once(reason="定时")
    gap = float(plugin._store.meta_get("acts_next_at") or 0) - before
    check("成功时按 10–60 分钟排下一次", 600 <= gap <= 3610, f"{gap:.0f}s")

    # (f4) 活动排序：按**活动时间**，不是按入库时间
    order_recs = [
        {"id": "1", "title": "三周后", "start": "2026-11-12 10:00", "ts": 1},
        {"id": "2", "title": "明天", "start": "2026-10-24 09:00", "ts": 2},
        {"id": "3", "title": "上周", "start": "2026-10-15 09:00", "ts": 3},
        {"id": "4", "title": "没时间", "start": "", "ts": 4},
        {"id": "5", "title": "今天下午", "start": "2026-10-23 15:00", "ts": 5},
        {"id": "6", "title": "昨天", "start": "2026-10-22 09:00", "ts": 6},
    ]
    ordered = BuptNewsPlugin._sort_activities(order_recs, now="2026-10-23 12:00")
    check("★ 活动按时间排：未来升序 → 已结束降序 → 无时间兜底",
          [r["id"] for r in ordered] == ["5", "2", "1", "6", "3", "4"],
          str([r["id"] for r in ordered]))
    check("排序不改动入参", [r["id"] for r in order_recs] == ["1", "2", "3", "4", "5", "6"],
          str([r["id"] for r in order_recs]))

    # 列表里也按这个顺序显示（用**独立 store**；日期取极值，避免"跑测试的日子"影响结论）
    far = [
        {"id": "f1", "title": "远期1", "start": "2099-01-01 09:00", "ts": 1},
        {"id": "f2", "title": "远期2", "start": "2099-02-01 09:00", "ts": 2},
        {"id": "p1", "title": "远古1", "start": "2000-01-01 09:00", "ts": 3},
        {"id": "p2", "title": "远古2", "start": "2000-02-01 09:00", "ts": 4},
        {"id": "n1", "title": "没时间", "start": "", "ts": 5},
    ]
    order_store = ActivityStore(str(tmp / "order-only.json"))
    order_store.upsert(far)
    order_store.mark_pushed([r["id"] for r in far])
    plugin._act_bindings = [make_binding("group", 111, _FakeDekt([]))]
    plugin._act_bindings[0]["store"] = order_store
    shown = plugin._activity_list_lines(10)
    ids = [line.split()[0] for line in shown[1:]]
    check("★ list 的活动区同样按活动时间排（未来升序→过去降序→无时间）",
          ids == ["f1", "f2", "p2", "p1", "n1"], str(ids))

    # 推送：单轮上限内优先推时间最靠前的
    order_store._data["pushed"] = []
    backlog = order_store.backlog()
    batch = BuptNewsPlugin._sort_activities(backlog)[:3]
    check("★ 单轮上限内优先推时间最靠前的活动",
          [r["id"] for r in batch] == ["f1", "f2", "p2"],
          str([r["id"] for r in batch]))

    # (f) list 的 scope：默认只有置顶+通知，--scope 才出活动
    plugin.config.set("afterclass", {"enabled": True})
    plugin._act_bindings = [
        make_binding("group", 111, _FakeDekt([])),
        make_binding("user", 222, _FakeDekt([])),
    ]
    for idx, (b, recs) in enumerate(zip(plugin._act_bindings, (
            [{"id": "900", "title": "群活动", "start": "2026-10-26 15:00", "ts": 2}],
            [{"id": "901", "title": "私聊活动", "start": "2026-10-27 09:00", "ts": 3}]))):
        b["store"].upsert(recs)
        b["store"].mark_pushed([r["id"] for r in recs])
    for i in (1, 2):
        plugin._store.add_notice({
            "key": f"n{i}", "newsid": str(7000 + i), "treeid": "1154",
            "title": f"通知{i}", "body": "b", "summary": f"摘要{i}",
            "summary_ai": True, "student_ai": True, "for_students": True,
            "images": [], "tables": []})
    plugin._register_commands()

    # 插件把构造时传入的 manager 存在 `self.cmds`；复用同一个实例
    cmds = plugin.cmds
    plugin._register_commands()

    async def say(text):
        r = await cmds.handle(
            MessageEvent(message_type="private", user_id=1, message=text))
        return r if isinstance(r, str) else str(r)

    out = await say("/buptnews list")
    # 注意：list 每行显示的是**摘要**（不是标题），所以这里断言 "摘要1"
    check("★ 默认 list 只有置顶+通知（不含活动）",
          "摘要1" in out and "🎪 第二课堂" not in out, out[:160])
    check("默认 list 提示可用 --scope=acts",
          "--scope=acts" in out, out[-160:])

    out = await say("/buptnews list --scope=acts")
    check("★ --scope=acts 只出活动",
          "🎪 第二课堂" in out and "摘要1" not in out, out[:200])
    check("多绑定时标出活动归属",
          "[group111]" in out or "[user222]" in out, out[:200])
    # 口径已改为"即将开始的在前（升序）"：900 是 10-26、901 是 10-27 → 900 在前
    check("活动按活动时间升序（最近的在前）",
          out.index("900") < out.index("901"), out[:200])

    out = await say("/buptnews list --scope=all")
    check("★ --scope=all 同时有通知与活动",
          "摘要1" in out and "🎪 第二课堂" in out, out[:200])

    out = await say("/buptnews list -a")
    check("-a 简写等价于 --scope=acts",
          "🎪 第二课堂" in out and "摘要1" not in out, out[:160])

    out = await say("/buptnews list 2")
    check("页码参数仍生效", "第 2/1 页" in out or "第 1/1 页" in out, out[:120])
    plugin._act_bindings = []


async def test_targets_commands(tmp: Path):
    print("\n[26] 绑定命令：add/set/del/targets + dektoken 权限")
    from napcat.command import CommandManager
    from napcat.models import GroupMember, GroupMemberRole, MessageEvent
    from napcat.permission import PermissionManager
    from plugins.bupt_news.main import BuptNewsPlugin

    def jwt(sub):
        def b64(obj):
            raw = json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return f"{b64({'alg': 'HS256'})}.{b64({'sub': sub, 'exp': 1900000000})}.sig"

    class _Api:
        def __init__(self):
            self.roles = {}

        async def get_group_member_info(self, gid, uid):
            role = self.roles.get((int(gid), int(uid)))
            if role is None:
                raise RuntimeError("机器人不在该群")
            return GroupMember(group_id=int(gid), user_id=int(uid),
                               nickname="x", role=GroupMemberRole(role))

        async def send_group_msg(self, *a, **k):
            return {}

        async def send_private_msg(self, *a, **k):
            return {}

    data_dir = tmp / "targets-cmd"
    perm = PermissionManager()
    perm.set_superusers([1])
    cmds = CommandManager(perm_manager=perm)
    api = _Api()
    plugin = BuptNewsPlugin(api=api, event_bus=None, timer=None, cmd_manager=cmds,
                            perm_manager=perm, file_manager=None, logger=_logger(),
                            groups=None, cfg=_FakeCfg({"plugins": {"bupt_news": {
                                "data_dir": str(data_dir), "enabled": False,
                                "afterclass": {"enabled": True}}}}))
    await plugin.on_load()
    plugin._register_commands()

    async def say(text, uid=1):
        r = await cmds.handle(MessageEvent(message_type="private", user_id=uid,
                                          message=text))
        return r if isinstance(r, str) else str(r)

    # (a) add：一项绑定 = 准入 + 两个开关
    out = await say("/buptnews add user 100000555")
    spec = plugin._targets()["users"].get(100000555) or {}
    check("★ add 建立绑定（默认推通知、不推活动）",
          spec.get("message") is True and spec.get("event") is False,
          f"{spec} | {out[:60]}")
    check("★ 绑定即准入：add 之后该用户可私聊用命令",
          plugin.check_access(MessageEvent(message_type="private",
                                           user_id=100000555, message="")))
    before = len(plugin._targets()["users"])
    await say("/buptnews add user 100000555")
    check("重复 add 幂等（不新增条目）",
          len(plugin._targets()["users"]) == before, str(before))

    await say("/buptnews add user 100000666 event=on message=off")
    spec = plugin._targets()["users"].get(100000666) or {}
    check("add 能一次给两个开关", spec.get("event") is True
          and spec.get("message") is False, str(spec))
    bad = await say("/buptnews add user 100000777 event=看情况")
    check("非法的开关值有明确提示", "认不出" in bad, bad[:80])

    # (b) set：只改给了的开关
    await say("/buptnews set user 100000555 event=on")
    check("set 单开活动推送",
          plugin._targets()["users"][100000555]["event"] is True,
          str(plugin._targets()["users"][100000555]))
    check("set 不动没给的开关（message 仍为 True）",
          plugin._targets()["users"][100000555]["message"] is True, "")
    out = await say("/buptnews set user 100000555")
    check("set 不给开关时提示用法而不改",
          "没有要改的开关" in out, out[:60])
    out = await say("/buptnews set user 999999999 message=off")
    check("set 未绑定的目标给出提示", "还没有绑定" in out, out[:60])

    # (c) dektoken：user 侧权限
    out = await say(f"/buptnews dektoken user {jwt(100000555)}", 100000999)
    check("★ 未绑定用户只能得到说明、不能设 token",
          "还没有绑定" in out, out[:60])
    await say("/buptnews set user 100000666 event=off")
    out = await say(f"/buptnews dektoken user {jwt(100000666)}", 100000666)
    check("★ 活动未开启时拒绝设 token", "event=off" in out, out[:60])
    await say("/buptnews set user 100000666 event=on")
    # ⚠️ `sub` 是**学号**，与 QQ 号无关：不能拿 sub == QQ 做"是不是本人"的判定，
    # 否则所有正常 token 都会被误拒（踩过）。归属只能靠"只能写自己名下的绑定"。
    out = await say(f"/buptnews dektoken user {jwt(2026999999)}", 100000666)
    check("★ 学号与 QQ 不同也能保存（不误拒）",
          "已保存" in out, out[:70])
    check("★ 保存时记录 token 的学号（sub）",
          plugin._targets()["users"][100000666].get("dekt_sub") == "2026999999",
          str(plugin._targets()["users"][100000666].get("dekt_sub")))
    out = await say(f"/buptnews dektoken user {jwt(100000666)}", 100000666)
    check("★ 自己的 token 可保存并立即生效",
          "已保存" in out and any(b["kind"] == "user"
                                 and b["target"] == 100000666
                                 and b["client"].available
                                 for b in plugin._act_bindings), out[:80])

    # (d) dektoken：group 侧权限
    await say("/buptnews add group 100000777 event=on")
    out = await say(f"/buptnews dektoken group 100000777 {jwt(1)}", 100000666)
    check("★ 机器人不在群 → 拒绝", "群主或管理员" in out, out[:70])
    api.roles[(100000777, 100000666)] = "member"
    out = await say(f"/buptnews dektoken group 100000777 {jwt(1)}", 100000666)
    check("★ 普通群成员 → 拒绝", "群主或管理员" in out, out[:70])
    api.roles[(100000777, 100000666)] = "admin"
    out = await say(f"/buptnews dektoken group 100000777 {jwt(1)}", 100000666)
    check("★ 群管理员 → 允许", "已保存" in out, out[:70])
    # 一个群只能一个人设置：群主也不能直接覆盖别人的
    api.roles[(100000777, 100000888)] = "owner"
    out = await say(f"/buptnews dektoken group 100000777 {jwt(1)}", 100000888)
    check("★ 已有人设置时，群主也不能直接覆盖",
          "一个群只能有一个人设置" in out, out[:80])
    out = await say("/buptnews dektoken group 100000777 delete", 100000888)
    check("★ 群主可以删除当前 token（并说明原设置者）",
          "已删除" in out and "100000666" in out, out[:90])
    out = await say(f"/buptnews dektoken group 100000777 {jwt(1)}", 100000888)
    check("★ 删除后群主可以设置", "已保存" in out, out[:70])
    # 原作者可以重设（续期），不需要先删
    out = await say(f"/buptnews dektoken group 100000777 {jwt(1)}", 100000888)
    check("★ 设置者本人可重设（续期不用先删）", "已保存" in out, out[:70])
    out = await say(f"/buptnews dektoken group 100000777 {jwt(1)} 多了一个参数", 100000888)
    check("group 形态参数多了会报用法", "用法" in out, out[:60])

    # 过期通知：私聊提醒**设置它的人**
    data = plugin._targets()
    data["groups"][100000777]["token_by"] = 100000888
    plugin._save_targets(data)
    plugin._refresh_activity_bindings()

    class _Expired:
        available = True

        def reload_token(self):
            return False

        def expiry(self):
            from datetime import datetime, timedelta
            return datetime.now() - timedelta(minutes=5)

        async def me(self):
            raise DektAuthError("expired")

        async def activity_classes(self):
            return []

    plugin._act_bindings[0]["client"] = _Expired() \
        if plugin._act_bindings else None
    if not plugin._act_bindings:
        plugin._act_bindings = [{"name": "群100000777", "kind": "group",
                                 "target": 100000777, "token_file": "x",
                                 "client": _Expired(),
                                 "store": plugin._act_bindings and None}]
    sent = []

    class _NotifyApi(_Api):
        async def send_private_msg(self, uid, chain, group_id=None):
            sent.append((str(uid), "".join(
                s.get("data", {}).get("text", "") for s in chain.build())))
            return {}

    plugin.api = _NotifyApi()
    plugin._token_warn_dates = {}
    await plugin._warn_token_expiry()
    check("★ 过期时私聊通知设置者",
          any(uid == "100000888" and "过期" in text for uid, text in sent),
          str(sent)[:120])
    # 历史 token（没记设置者）→ 退给超管，不能没人管
    data = plugin._targets()
    data["groups"][100000777]["token_by"] = 0
    plugin._save_targets(data)
    plugin._store.meta_set("token_warn_dates", {})   # 模拟"到了新的一天"
    sent.clear()
    await plugin._warn_token_expiry()
    check("★ 没记设置者时通知超管（兜底）",
          any(uid == "1" and "过期" in text for uid, text in sent), str(sent)[:120])
    sent.clear()
    await plugin._warn_token_expiry()          # 不能清已记录的日期
    check("同一天不重复提醒（避免刷屏）", sent == [], str(sent))
    # ★ 热重载会重建插件实例：提醒日期必须落在 state.json 里，否则会重复发
    check("★ 提醒日期已持久化到 state.json",
          (plugin._store.meta_get("token_warn_dates") or {}).get("group-100000777")
          == plugin._today(),
          str(plugin._store.meta_get("token_warn_dates")))
    fresh_plugin = BuptNewsPlugin(api=plugin.api, event_bus=None, timer=None,
                                  cmd_manager=None, perm_manager=None,
                                  file_manager=None, logger=_logger(),
                                  groups=None, cfg=plugin.config)
    await fresh_plugin.on_load()               # 模拟重载后的新实例
    fresh_plugin._act_bindings = plugin._act_bindings
    fresh_plugin.api = plugin.api
    sent.clear()
    await fresh_plugin._warn_token_expiry()
    check("★ 重载后不会重复提醒（状态跨实例）", sent == [], str(sent))

    # token 文件落在数据目录且权限收紧
    tokens = sorted(Path(data_dir, "dekt-tokens").glob("*.txt"))
    check("token 写进数据目录", len(tokens) >= 2, str(tokens))
    check("★ token 文件权限 0600",
          all(f.stat().st_mode & 0o777 == 0o600 for f in tokens),
          str([oct(f.stat().st_mode & 0o777) for f in tokens]))

    # (e) del：连带删掉数据目录里的 token
    user_tok = plugin._token_path("user", 100000666,
                                  plugin._target("user", 100000666))
    out = await say("/buptnews del user 100000666")
    check("★ del 移除绑定并删除 token 文件",
          100000666 not in plugin._targets()["users"]
          and not os.path.exists(user_tok), f"{out[:40]} exists={os.path.exists(user_tok)}")
    check("del 后立即失去准入",
          not plugin.check_access(MessageEvent(message_type="private",
                                             user_id=100000666, message="")))

    # (f) targets 展示开关
    out = await say("/buptnews targets")
    check("targets 标出通知/活动状态",
          "通知" in out and "活动" in out and "缺token" not in out.split("群（")[0],
          out[:200])

    # (g) 非超管不能动绑定
    out = await say("/buptnews add user 100000999", 100000555)
    check("★ 非超管不能 add",
          100000999 not in plugin._targets()["users"]
          and "_not_found" in out, repr(out)[:60])
    out = await say("/buptnews dektoken user " + jwt(100000555), 100000555)
    check("★ dektoken 不因非超管而被拦（命令内部判定）",
          "_not_found" not in out and "已保存" in out, repr(out)[:60])


async def test_skills(tmp: Path):
    print("\n[27] ai_chat 技能：命令映射（只读面是否补齐）")
    from napcat.command import CommandManager
    from napcat.permission import PermissionManager
    from plugins.bupt_news.main import BuptNewsPlugin

    d = tmp / "skills"
    d.mkdir(parents=True, exist_ok=True)
    perm = PermissionManager()
    perm.set_superusers([1])
    from napcat.events import TimerManager
    timer = TimerManager()
    plugin = BuptNewsPlugin(api=None, event_bus=None, timer=timer,
                            cmd_manager=CommandManager(perm_manager=perm),
                            perm_manager=perm, file_manager=None,
                            logger=_logger(), groups=None,
                            cfg=_FakeCfg({"plugins": {
                                "bupt_news": {"data_dir": str(d),
                                              "enabled": False}}}))
    await plugin.on_load()
    timer.start()
    await plugin.on_enable()        # 技能在 on_enable 里注册（命令也是）
    cmds = plugin.cmds
    skills = {s.name: s for s in plugin.skills.list()}
    check("★ 注册了 5 个技能（只读面齐全）",
          set(skills) == {"bupt_news.search", "bupt_news.recent",
                          "bupt_news.notice", "bupt_news.pins",
                          "bupt_news.status"}, str(sorted(skills)))

    expected = {
        "bupt_news.search": "buptnews.search",
        "bupt_news.recent": "buptnews.list",
        "bupt_news.notice": "buptnews.get",
        "bupt_news.pins": "buptnews.pin",
        "bupt_news.status": "buptnews.status",
    }
    check("★ 每个技能都映射到本插件的真实命令",
          all(skills[k].command == v for k, v in expected.items()),
          str({k: skills[k].command for k in skills}))
    check("★ 映射的命令都能解析到（不会暴露死工具）",
          all(cmds.find_command(skills[k].command) for k in expected),
          str([k for k in expected if not cmds.find_command(skills[k].command)]))
    import re as _re
    check("★ 工具名合法（OpenAI 函数名约束）",
          all(_re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sk.tool_name) for sk in skills.values()),
          str([sk.tool_name for sk in skills.values()]))

    # 参数 schema 从命令的 Schema 翻译，不另写一份
    from plugins.ai_chat.tools import command_tool_schema
    recent_handler = cmds.find_command("buptnews.list")[1]
    props = command_tool_schema(recent_handler)["properties"]
    check("★ recent 的参数来自 list 的命令 Schema（page/scope）",
          {"page", "scope"} <= set(props), str(sorted(props)))
    check("scope 是枚举（模型看得到 acts/all 这些可选值）",
          props["scope"].get("enum") and "acts" in props["scope"]["enum"],
          str(props["scope"]))
    notice_props = command_tool_schema(cmds.find_command("buptnews.get")[1])["properties"]
    check("notice 的参数来自 get 的命令 Schema（id）", "id" in notice_props,
          str(sorted(notice_props)))

    # 写操作**不能**被模型触发（这是"只读面"的边界）
    for forbidden in ("buptnews.add", "buptnews.set", "buptnews.del",
                      "buptnews.dektoken", "buptnews.on", "buptnews.off",
                      "buptnews.interval", "buptnews.sync", "buptnews.images"):
        check(f"写操作 {forbidden} 没有被挂成技能",
              forbidden not in {sk.command for sk in skills.values()}, "")

    # 权限：技能只是映射，判定仍由命令系统做（普通用户拿不到 superuser 命令）
    from napcat.command import CommandPacket
    from napcat.models import MessageEvent
    ev = MessageEvent(message_type="private", user_id=100000999,
                      message=[{"type": "text", "data": {"text": "/buptnews status"}}],
                      raw_message="/buptnews status")
    res = await cmds.handle(ev)
    check("★ 普通用户触发 superuser 命令 → 统一「未知命令」口径",
          isinstance(res, tuple) and res[0] == "_not_found", repr(res)[:60])


async def _cleanup_artifacts():
    for stale in (ROOT / "napcat" / "data").glob("qrcode_sent_*.png"):
        try:
            stale.unlink()
        except OSError:
            pass
    for stale in (ROOT / "napcat" / "data").glob("qrcode_sent_*.png"):
        try:
            stale.unlink()
        except OSError:
            pass


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="watchdog-test-"))
    print(f"临时目录: {tmp}")
    await test_launch_once_on_failure(tmp)
    await test_disconnect_and_reconnect(tmp)
    await test_no_disconnect_without_connection(tmp)
    await test_launcher_failure_and_cooldown(tmp)
    await test_flap_backoff(tmp)
    await test_state_machine_end_to_end(tmp)
    await test_permission_failure_is_actionable(tmp)
    await test_cache_cleared_before_launch(tmp)
    await test_login_feedback(tmp)
    await test_restart_on_startup(tmp)
    await test_ilink_retry(tmp)
    await test_qrcode_hash_detection_and_scan_retry(tmp)
    await test_health_check_triggers_recovery(tmp)
    await test_bupt_news_at_all(tmp)
    await test_unlimited_restart_cycles(tmp)
    await test_repeated_triggers_restart_recovery(tmp)
    await test_placeholder_convention(tmp)
    await test_plugin_reload_resilience(tmp)
    await test_summary_retry(tmp)
    await test_student_judgement(tmp)
    await test_digest_not_blocked_by_backfill(tmp)
    await test_table_to_image(tmp)
    await test_pin_and_search(tmp)
    await test_portal_search(tmp)
    await test_afterclass(tmp)
    await test_targets_commands(tmp)
    await test_skills(tmp)
    await _cleanup_artifacts()
    print(f"\n===== 通过 {PASSED} 项，失败 {FAILED} 项 =====")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
