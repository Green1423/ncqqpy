"""插件目录布局：把主要逻辑拆出 `__init__.py` 的写法。

框架原来靠「按属性名字母序扫第一个 `PluginBase` 子类」找入口 —— 拆分目录后
`__init__.py` 里顺手 `from .base import GenericPlugin`，字母序靠前的**基类**
就会被实例化（症状：插件加载了，但行为不是你的子类）。现在支持显式声明：

    # plugins/<名字>/__init__.py
    from .main import MyPlugin
    PLUGIN = MyPlugin

覆盖：显式 `PLUGIN` 生效、无声明时仍能加载但会告警、`PLUGIN` 写错时忽略并回退、
跨模块相对导入、单文件插件兼容、热重载后子模块的新代码生效。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / ".test-tmp" / "plugin-layout"

from napcat import (                                          # noqa: E402
    CommandManager, EventBus, FileManager, GroupBinding, MessageEvent,
    PermissionManager, PluginManager, TimerManager,
)
from napcat.config import ConfigManager                       # noqa: E402

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


class Capture(logging.Handler):
    """收集框架告警（用来断言「多个候选时有没有告警」）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())

    def text(self) -> str:
        return "\n".join(self.records)


class Notified:
    def set_plugin_resolver(self, resolver):
        pass

    async def notify_operators(self, error, *, where=""):
        pass


def make_mgr(pdir: Path, capture: Capture):
    logger = logging.getLogger("test.plugin-layout")
    logger.handlers = [capture]
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    bus, timer = EventBus(), TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[999])
    cmds = CommandManager(prefixes=["/"], perm_manager=perm)
    mgr = PluginManager(str(pdir), api=None, event_bus=bus, timer=timer,
                        cmd_manager=cmds, perm_manager=perm,
                        file_manager=FileManager(api=None), logger=logger,
                        cfg=ConfigManager(str(pdir.parent / "config.json")),
                        groups=GroupBinding(bound=set(), all_groups=True),
                        client=SimpleNamespace(plugins=None),
                        error_reporter=Notified())
    mgr._client.plugins = mgr
    cmds._plugins_ref = mgr
    return mgr, cmds


def write(pkg: Path, files: dict[str, str]) -> None:
    pkg.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = pkg / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


async def ask(cmds: CommandManager, text: str, user_id: int = 999) -> str:
    return str(await cmds.handle(MessageEvent(message_type="private",
                                              user_id=user_id, message=text)) or "")


# ==================== 用例 ====================

async def test_explicit_plugin_attribute(capture: Capture):
    print("\n[1] 拆分写法：__init__.py 只导出 + `PLUGIN = 类`")
    root = TMP / "explicit"
    write(root / "plugins" / "demo", {
        "__init__.py": '''"""演示插件 —— 主要逻辑在 main.py / helper.py。"""
from .main import DemoPlugin

PLUGIN = DemoPlugin
__all__ = ["DemoPlugin"]
''',
        "helper.py": '''def answer() -> str:
    return "helper-ok"
''',
        "main.py": '''from napcat import PluginBase
from .helper import answer


class DemoPlugin(PluginBase):
    name = "demo"
    version = "1.0.0"
    loaded = 0

    async def on_load(self):
        type(self).loaded += 1

    async def on_enable(self):
        @self.register_command("demo.ping", permission="", scope="all")
        async def _ping(packet):
            return f"pong:{answer()}"
''',
    })
    mgr, cmds = make_mgr(root / "plugins", capture)
    plugin = await mgr.load("demo")
    check("加载成功", plugin is not None)
    check("入口类就是声明的那个", type(plugin).__name__ == "DemoPlugin",
          type(plugin).__name__ if plugin else "None")
    check("on_load 跑了", type(plugin).loaded == 1)
    await mgr.enable("demo")
    check("跨模块相对导入可用（helper）", "helper-ok" in await ask(cmds, "/demo ping"),
          await ask(cmds, "/demo ping"))
    check("plugin_dir 已注入（database() 能落盘）",
          plugin.plugin_dir == root / "plugins" / "demo", str(plugin.plugin_dir))
    await mgr.unload("demo")


async def test_multiple_candidates(capture: Capture):
    print("\n[2] 多个候选：显式 PLUGIN 选对，没有声明会告警")
    root = TMP / "multi"
    base_main = '''from napcat import PluginBase


class BasePlugin(PluginBase):
    name = "multi"
    version = "1.0.0"

    async def on_enable(self):
        @self.register_command("multi.who", permission="", scope="all")
        async def _who(packet):
            return "base"


class ConcretePlugin(BasePlugin):
    async def on_enable(self):
        @self.register_command("multi.who", permission="", scope="all")
        async def _who(packet):
            return "concrete"
'''
    # ① 显式声明 → 选 ConcretePlugin
    write(root / "plugins" / "withdecl", {
        "__init__.py": '''from .main import BasePlugin, ConcretePlugin

PLUGIN = ConcretePlugin
''',
        "main.py": base_main,
    })
    mgr, cmds = make_mgr(root / "plugins", capture)
    plugin = await mgr.load("withdecl")
    check("显式 PLUGIN 选中的是子类", type(plugin).__name__ == "ConcretePlugin",
          type(plugin).__name__)
    await mgr.unload("withdecl")

    # ② 不声明 → 字母序（BasePlugin）优先，且必须告警
    capture.records.clear()
    write(root / "plugins" / "nodecl", {
        "__init__.py": "from .main import BasePlugin, ConcretePlugin\n",
        "main.py": base_main.replace('name = "multi"', 'name = "nodecl"'),
    })
    mgr2, cmds2 = make_mgr(root / "plugins", capture)
    plugin2 = await mgr2.load("nodecl")
    check("无声明时按字母序取 BasePlugin（旧行为）",
          type(plugin2).__name__ == "BasePlugin", type(plugin2).__name__)
    check("多个候选会告警并给出修法",
          "PLUGIN" in capture.text() and "2 个 PluginBase 子类" in capture.text(),
          capture.text()[:300])
    await mgr2.unload("nodecl")


async def test_bad_plugin_attribute(capture: Capture):
    print("\n[3] `PLUGIN` 写错：告警 + 回退扫描（不会让插件消失）")
    root = TMP / "baddecl"
    write(root / "plugins" / "bad", {
        "__init__.py": '''from .main import BadPlugin

PLUGIN = 42          # 写错了
''',
        "main.py": '''from napcat import PluginBase


class BadPlugin(PluginBase):
    name = "bad"
''',
    })
    capture.records.clear()
    mgr, _cmds = make_mgr(root / "plugins", capture)
    plugin = await mgr.load("bad")
    check("回退后仍能加载", plugin is not None and type(plugin).__name__ == "BadPlugin",
          type(plugin).__name__ if plugin else "None")
    check("给出一条「PLUGIN 不是 PluginBase 子类」的告警",
          "PLUGIN" in capture.text() and "不是" in capture.text(), capture.text()[:300])
    await mgr.unload("bad")


async def test_no_plugin_class(capture: Capture):
    print("\n[4] 一个子类都没有：加载失败且提示拆分写法")
    root = TMP / "empty"
    write(root / "plugins" / "empty", {"__init__.py": "VALUE = 1\n"})
    mgr, _ = make_mgr(root / "plugins", capture)
    check("load 返回 None", await mgr.load("empty") is None)
    await asyncio.sleep(0.05)
    check("日志提示用 PLUGIN 声明入口", "PLUGIN" in capture.text(), capture.text()[:300])


async def test_single_file_still_works(capture: Capture):
    print("\n[5] 兼容：单文件插件（plugins/foo.py）仍然可用")
    root = TMP / "single"
    pdir = root / "plugins"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "solo.py").write_text('''from napcat import PluginBase


class SoloPlugin(PluginBase):
    name = "solo"

    async def on_enable(self):
        @self.register_command("solo.ping", permission="", scope="all")
        async def _p(packet):
            return "solo-pong"
''', encoding="utf-8")
    mgr, cmds = make_mgr(pdir, capture)
    await mgr.load("solo")
    await mgr.enable("solo")
    check("单文件插件加载并响应命令", "solo-pong" in await ask(cmds, "/solo ping"))
    check("单文件插件的 plugin_dir 是 plugins/ 目录",
          (mgr.get("solo").plugin_dir == pdir), str(mgr.get("solo").plugin_dir))
    await mgr.unload("solo")


async def test_reload_picks_new_submodule_code(capture: Capture):
    print("\n[6] 热重载：改 main.py 后子模块的新代码生效（整棵模块树被清掉）")
    root = TMP / "reload"
    pkg = root / "plugins" / "demo"
    write(pkg, {
        "__init__.py": '''from .main import DemoPlugin

PLUGIN = DemoPlugin
''',
        "main.py": '''from napcat import PluginBase


class DemoPlugin(PluginBase):
    name = "demo"

    async def on_enable(self):
        @self.register_command("demo.text", permission="", scope="all")
        async def _t(packet):
            return "v1"
''',
    })
    mgr, cmds = make_mgr(root / "plugins", capture)
    await mgr.load("demo")
    await mgr.enable("demo")
    check("重载前是 v1", "v1" in await ask(cmds, "/demo text"))
    (pkg / "main.py").write_text((pkg / "main.py").read_text(encoding="utf-8")
                                 .replace('"v1"', '"v2"'), encoding="utf-8")
    ok = await mgr.reload("demo")
    check("reload 成功", ok is True, str(ok))
    check("重载后是 v2（拿到的是新模块）", "v2" in await ask(cmds, "/demo text"),
          await ask(cmds, "/demo text"))
    await mgr.unload("demo")


async def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    import shutil
    for child in TMP.iterdir():
        shutil.rmtree(child, ignore_errors=True)
    capture = Capture()
    await test_explicit_plugin_attribute(capture)
    await test_multiple_candidates(capture)
    await test_bad_plugin_attribute(capture)
    await test_no_plugin_class(capture)
    await test_single_file_still_works(capture)
    await test_reload_picks_new_submodule_code(capture)
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
