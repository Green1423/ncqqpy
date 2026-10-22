"""声明式注册：注册处只装配、逻辑按类别放独立模块。

覆盖：

1. 一个模块里声明的命令（有 schema / 无 schema）能被 `register_module` 装进来，
   处理器第一个参数拿到**插件实例**；
2. 事件处理器、定时任务（every / daily / delay）同样声明式注册；
3. `hid` 默认名 = `插件名.函数名`；
4. 卸载后命令/事件/定时任务**全部回收**（注册走的是框架既有通道）；
5. `collect()` 按定义顺序、按对象去重（再导出不会重复注册）；
6. 同步处理器也能用（返回值/awaitable 都支持）。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / ".test-tmp" / "plugin-registry"

from napcat.command import CommandManager, CommandSchema, StrField
from napcat.events import EventBus, TimerManager
from napcat.file_manager import FileManager
from napcat.client import GroupBinding
from napcat.models import MessageEvent
from napcat.permission import PermissionManager
from napcat.plugin import PluginBase, PluginManager
from napcat.config import ConfigManager                       # noqa: E402
from napcat.registry import collect, command, daily, delay, event, every  # noqa: E402

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


class Notified:
    def set_plugin_resolver(self, resolver):
        pass

    async def notify_operators(self, error, *, where=""):
        pass


class SaySchema(CommandSchema):
    text = StrField(default="hi", description="要说的话")


CALLS: list[str] = []


# ==================== 「逻辑」模块（模拟 commands/ events/ jobs/）====================

chat_cmds = ModuleType("chat_cmds")


@command("demo.say", schema=SaySchema, permission="", scope="all",
         description="记一句话")
async def say(plugin, params, event):
    CALLS.append(f"say:{params.text}")
    return f"plugin={plugin.name} text={params.text}"


@command("demo.time", permission="", scope="all", description="看时间")
async def show_time(plugin, packet, event):
    CALLS.append("time")
    # 无 schema 的命令拿到的是原始 packet
    return f"packet={packet.command}" if hasattr(packet, "command") else "packet"


@command("demo.sync", schema=SaySchema, permission="", scope="all",
         description="同步处理器")
def sync_cmd(plugin, params, event):
    CALLS.append("sync")
    return f"sync:{params.text}"


chat_cmds.say = say
chat_cmds.show_time = show_time
chat_cmds.sync_cmd = sync_cmd

ev_module = ModuleType("ev_module")


@event(MessageEvent, description="记一条消息")
async def on_message(plugin, event):
    CALLS.append(f"event:{event.user_id}")


ev_module.on_message = on_message

jobs_module = ModuleType("jobs_module")


@every(0.05, name="demo.tick", description="每 50ms")
async def tick(plugin):
    CALLS.append("tick")


@daily("08:00", name="demo.daily")
async def daily_job(plugin):
    CALLS.append("daily")


@delay(0.05, name="demo.once")
async def once(plugin):
    CALLS.append("once")


jobs_module.tick = tick
jobs_module.daily_job = daily_job
jobs_module.once = once


# ==================== 插件（只有装配，没有逻辑）====================

class DemoPlugin(PluginBase):
    name = "demo"
    version = "1.0.0"

    async def on_enable(self):
        self.register_module(chat_cmds, ev_module, jobs_module)


def make_mgr(pdir: Path):
    bus, timer = EventBus(), TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[999])
    cmds = CommandManager(prefixes=["/"], perm_manager=perm)
    mgr = PluginManager(str(pdir), api=None, event_bus=bus, timer=timer,
                        cmd_manager=cmds, perm_manager=perm,
                        file_manager=FileManager(api=None),
                        logger=_quiet_logger(),
                        cfg=ConfigManager(str(pdir.parent / "config.json")),
                        groups=GroupBinding(bound=set(), all_groups=True),
                        client=SimpleNamespace(plugins=None),
                        error_reporter=Notified())
    mgr._client.plugins = mgr
    cmds._plugins_ref = mgr
    return mgr, cmds, bus, timer


def _quiet_logger():
    import logging
    logger = logging.getLogger("test.plugin-registry")
    logger.handlers = [logging.NullHandler()]
    logger.setLevel(600)
    logger.propagate = False
    return logger


async def ask(cmds, text, user_id=999):
    return str(await cmds.handle(MessageEvent(message_type="private",
                                              user_id=user_id, message=text)) or "")


# ==================== 用例 ====================

def test_collect():
    print("\n[1] collect()：顺序 / 去重 / 无装饰器对象被忽略")
    specs = collect(chat_cmds)
    check("模块里 3 条命令都能收集", len(specs) == 3, str(len(specs)))
    check("按定义顺序", [s.name for s in specs] == ["demo.say", "demo.time", "demo.sync"],
          str([s.name for s in specs]))
    doubled = collect(chat_cmds, chat_cmds)
    check("同一模块传两次不重复", len(doubled) == 3, str(len(doubled)))
    plain = ModuleType("plain")
    plain.helper = lambda: None
    plain.VALUE = 1
    check("没有声明对象的模块收集为空", collect(plain) == [])
    check("schema 声明被带上", specs[0].schema is SaySchema)
    check("description 默认取 docstring（这里显式给了）",
          specs[0].description == "记一句话")


async def test_register_module_commands():
    print("\n[2] register_module：命令（schema / 无 schema / 同步）")
    CALLS.clear()
    root = TMP / "cmds"
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    mgr, cmds, _bus, timer = make_mgr(root / "plugins")
    plugin = DemoPlugin(api=None, event_bus=mgr.event_bus, timer=timer,
                        cmd_manager=cmds, perm_manager=PermissionManager(superusers=[999]),
                        file_manager=FileManager(api=None), logger=_quiet_logger(),
                        cfg=ConfigManager(str(root / "config.json")),
                        groups=GroupBinding(bound=set(), all_groups=True))
    await plugin.on_enable()

    out = await ask(cmds, "/demo say 你好")
    check("schema 命令执行了逻辑模块里的函数",
          "plugin=demo text=你好" in out, out[:200])
    check("处理器第一个参数拿到的是插件实例", CALLS[0] == "say:你好", str(CALLS[:1]))

    out = await ask(cmds, "/demo time")
    check("无 schema 命令拿到原始 packet", out.startswith("packet="), out[:200])

    out = await ask(cmds, "/demo sync 同步")
    check("同步处理器也能用", "sync:同步" in out, out[:200])
    timer.stop()


async def test_register_module_events_timers():
    print("\n[3] register_module：事件 / 定时（every、daily、delay）")
    CALLS.clear()
    root = TMP / "evt"
    (root / "plugins").mkdir(parents=True, exist_ok=True)
    bus, timer = EventBus(), TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[999])
    cmds = CommandManager(prefixes=["/"], perm_manager=perm)
    plugin = DemoPlugin(api=None, event_bus=bus, timer=timer, cmd_manager=cmds,
                        perm_manager=perm, file_manager=FileManager(api=None),
                        logger=_quiet_logger(),
                        cfg=ConfigManager(str(root / "config.json")),
                        groups=GroupBinding(bound=set(), all_groups=True))
    await plugin.on_enable()
    await bus.dispatch(MessageEvent(message_type="private", user_id=20001, message="hi"))
    check("事件处理器被调用", any(c == "event:20001" for c in CALLS), str(CALLS))

    metas = {m["id"]: m for m in timer.list_all()}
    check("every 任务已登记", "demo.tick" in metas, str(list(metas)))
    check("daily 任务已登记", "demo.daily" in metas, str(list(metas)))
    check("delay 任务已登记", "demo.once" in metas, str(list(metas)))
    check("hid 默认 = 插件名.函数名",
          any(m.id == "demo.on_message" for m in bus.list_all()),
          str([m.id for m in bus.list_all()]))

    await asyncio.sleep(1.2)          # every/delay 的首轮都在 1s 后
    check("every 真的跑了", CALLS.count("tick") >= 1, str(CALLS))
    check("delay 真的跑了一次", CALLS.count("once") == 1, str(CALLS))
    timer.stop()


async def test_unload_cleans_everything():
    print("\n[4] 卸载后命令 / 事件 / 定时任务全部回收")
    root = TMP / "cleanup"
    pdir = root / "plugins"
    pkg = pdir / "demo"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        "from .plugin import DemoPlugin\n\nPLUGIN = DemoPlugin\n", encoding="utf-8")
    (pkg / "plugin.py").write_text(
        "from napcat.plugin import PluginBase\n"
        "from napcat.registry import command, event, every\n"
        "from napcat.models import MessageEvent\n\n"
        "from . import mods\n\n"
        "class DemoPlugin(PluginBase):\n"
        "    name = 'demo'\n"
        "    async def on_enable(self):\n"
        "        self.register_module(mods)\n",
        encoding="utf-8")
    (pkg / "mods.py").write_text(
        "from napcat.registry import command, event, every\n"
        "from napcat.models import MessageEvent\n\n"
        "@command('demo.hi', permission='', scope='all')\n"
        "async def hi(plugin, packet, event):\n"
        "    return 'hi'\n\n"
        "@event(MessageEvent)\n"
        "async def on_msg(plugin, event):\n"
        "    pass\n\n"
        "@every(0.05, name='demo.job')\n"
        "async def job(plugin):\n"
        "    pass\n", encoding="utf-8")

    mgr, cmds, bus, timer = make_mgr(pdir)
    plugin = await mgr.load("demo")
    await mgr.enable("demo")
    check("命令已注册", "demo" in cmds.list_commands(), str(list(cmds.list_commands())))
    check("事件已注册", any(m.id == "demo.on_msg" for m in bus.list_all()),
          str([m.id for m in bus.list_all()]))
    check("定时已注册", any(m["id"] == "demo.job" for m in timer.list_all()),
          str([m["id"] for m in timer.list_all()]))
    await mgr.unload("demo")
    check("卸载后命令没了", "demo" not in cmds.list_commands(), str(list(cmds.list_commands())))
    check("卸载后事件没了", not any(m.id == "demo.on_msg" for m in bus.list_all()),
          str([m.id for m in bus.list_all()]))
    check("卸载后定时任务没了", not any(m["id"] == "demo.job" for m in timer.list_all()),
          str([m["id"] for m in timer.list_all()]))
    timer.stop()


async def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    for child in TMP.iterdir():
        shutil.rmtree(child, ignore_errors=True)
    test_collect()
    await test_register_module_commands()
    await test_register_module_events_timers()
    await test_unload_cleans_everything()
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
