"""插件生命周期与 WebSocket 生命周期解耦。

要求（本次改动）：

1. 插件 `load` / `enable` 在**任何连接之前**完成 —— `on_enable` 里读
   `ws_connected` 必然是 False（不因连接快慢而变）；
2. **断线/重连不重载插件**：不触发 `on_load/on_enable/on_disable/on_unload`，
   插件实例不换、命令照常可用；
3. 重连后的恢复动作由 `self.on_ws_*` 回调承载，且这些回调**归插件所有**：
   卸载/停用时框架自动注销（不会留下旧实例继续收连接事件）；
4. `start()` 重复调用不会二次 load/enable。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / ".test-tmp" / "plugin-ws-decoupling"

from websockets.asyncio.server import serve                       # noqa: E402

from napcat import MessageEvent, NapCatClient                     # noqa: E402

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


async def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def onebot_handler(ws):
    """最小 OneBot 服务端：应答带 echo 的请求（`get_login_info` 要等它）。"""
    try:
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            if payload.get("echo") is not None:
                await ws.send(json.dumps({
                    "status": "ok", "retcode": 0, "echo": payload["echo"],
                    "data": {"user_id": 10001, "nickname": "测试机器人"},
                }, ensure_ascii=False))
    except Exception:
        pass


def kill(clients) -> None:
    """粗暴断开（模拟掉线：1006 不能主动发，只能掐传输层）。"""
    for ws in list(clients):
        tr = getattr(ws, "transport", None)
        if tr is not None:
            tr.abort()


PROBE_PLUGIN = '''\
"""探针插件：把生命周期事件写进日志文件。"""
import os

from napcat import PluginBase

LOG = os.environ["WS_PROBE_LOG"]


def _log(what: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(what + "\\n")


class ProbePlugin(PluginBase):
    name = "probe"
    version = "1.0.0"

    async def on_load(self):
        _log("load")

    async def on_enable(self):
        # 关键断言：插件启用发生在「任何连接」之前，所以这里必然是 False
        _log(f"enable ws={self.client.ws_connected}")

        @self.on_ws_connected
        async def _connected():
            _log("hook connected")

        @self.on_ws_disconnected
        async def _disconnected():
            _log("hook disconnected")

        @self.register_command("probe.ping", permission="", scope="all")
        async def _ping(packet):
            return "pong"

    async def on_disable(self):
        _log("disable")

    async def on_unload(self):
        _log("unload")
'''


def write_plugin(plugin_dir: Path) -> None:
    pkg = plugin_dir / "probe"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        "from .plugin import ProbePlugin\n\nPLUGIN = ProbePlugin\n", encoding="utf-8")
    (pkg / "plugin.py").write_text(PROBE_PLUGIN, encoding="utf-8")


def write_config(path: Path, ws_port: int, plugin_dir: Path) -> Path:
    cfg = {
        "ws_url": f"ws://127.0.0.1:{ws_port}", "token": "",
        "plugin_dir": str(plugin_dir), "superusers": [1],
        "command_prefixes": ["/"], "bound_groups": [], "all_groups": True,
        "api_delay": 0,
        # 只测生命周期解耦：不要看门狗/拉起，避免服务端重启干扰
        "watchdog_enabled": False, "restart_enabled": False,
        "reconnect_interval": 0.2, "max_reconnect_interval": 0.6,
    }
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def read_log(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]


async def wait_until(predicate, timeout: float = 10.0, step: float = 0.05) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


async def run_case() -> None:
    port = await free_port()
    plugin_dir = TMP / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    cfg = write_config(TMP / "config.json", port, plugin_dir)
    write_plugin(plugin_dir)
    log = TMP / "lifecycle.log"
    log.write_text("", encoding="utf-8")
    os.environ["WS_PROBE_LOG"] = str(log)

    clients: list = []

    async def handler(ws):
        clients.append(ws)
        await onebot_handler(ws)

    server = await serve(handler, "127.0.0.1", port)
    client = NapCatClient(config_path=str(cfg))
    runner = asyncio.create_task(client.start())
    try:
        # —— 1) 插件先于连接启用 ——
        ok = await wait_until(lambda: "enable ws=False" in read_log(log))
        check("插件启用发生在连接之前（on_enable 里 ws_connected=False）", ok, str(read_log(log)))
        lines = read_log(log)
        check("load 在 enable 之前", lines[:2] == ["load", "enable ws=False"], str(lines[:3]))
        check("连接回调发生在启用之后（顺序正确）",
              "hook connected" not in lines or
              lines.index("hook connected") > lines.index("enable ws=False"), str(lines))

        await wait_until(lambda: client.ws_connected)
        await wait_until(lambda: "hook connected" in read_log(log))
        check("连上之后 on_ws_connected 回调触发", "hook connected" in read_log(log),
              str(read_log(log)))
        plugin = client.plugins.get("probe")
        check("插件已加载", plugin is not None)
        plugin_id = id(plugin)
        registration = read_log(log)
        check("此时生命周期各只发生一次",
              registration.count("load") == 1 and len(
                  [x for x in registration if x.startswith("enable")]) == 1,
              str(registration))

        # —— 2) 掉线 → 重连：不许重载插件 ——
        log.write_text("", encoding="utf-8")          # 清空，只看重连这段
        kill(clients)
        await wait_until(lambda: "hook disconnected" in read_log(log), timeout=8)
        check("断开回调触发", "hook disconnected" in read_log(log), str(read_log(log)))
        await wait_until(lambda: client.ws_connected, timeout=10)
        await wait_until(lambda: "hook connected" in read_log(log), timeout=10)
        check("重连后连接恢复", client.ws_connected is True)
        after = read_log(log)
        check("重连期间没有 load/enable/disable/unload",
              not any(x in ("load", "disable", "unload") or x.startswith("enable")
                      for x in after), str(after))
        check("插件实例没换", id(client.plugins.get("probe")) == plugin_id)

        # 命令照常可用（插件没被生命周期动作打乱）
        out = await client.cmds.handle(MessageEvent(
            message_type="private", user_id=1, message="/probe ping"))
        check("重连后命令照常可用", str(out) == "pong", repr(out))

        # —— 3) 重复 start() 不二次加载 ——
        log.write_text("", encoding="utf-8")
        await client.start()
        await asyncio.sleep(0.3)
        check("重复 start() 不会 load/enable 插件", read_log(log) == [], str(read_log(log)))
        check("插件实例仍是同一个", id(client.plugins.get("probe")) == plugin_id)
    finally:
        await asyncio.wait_for(client.stop(), timeout=15)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(runner), timeout=10)
        with contextlib.suppress(Exception):
            server.close()
            await server.wait_closed()

    # —— 4) 停止 = 正常卸载一次；钩子被摘掉 ——
    final = read_log(log)
    check("stop 时插件被卸载", final[-2:] == ["disable", "unload"] or
          ("disable" in final and "unload" in final), str(final))
    check("卸载后连接钩子已注销",
          not client._ws_connected_hooks and not client._ws_disconnected_hooks,
          f"connected={client._ws_connected_hooks} disconnected={client._ws_disconnected_hooks}")


async def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    import shutil
    for child in TMP.iterdir():
        shutil.rmtree(child, ignore_errors=True)
    await run_case()
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
