#!/usr/bin/env python3
"""插件依赖声明（必需 + 可选）验收套件。

覆盖四层：

1. **解析与校验**（`napcat/deps.py`）—— 版本号比较、非法声明、依赖自己、
   必需/可选重复；
2. **拓扑排序** —— 加载顺序「依赖在前」、卸载顺序「使用者在前」、环检测；
3. **加载期行为** —— 必需依赖缺失/未启用 → 插件**不加载**（原因私聊超管、
   `/plugins` 能看到）、依赖就位后自动加载；可选依赖缺失**不影响加载**；
4. **运行期行为** —— 自动启用依赖、`self.deps` 取实例、禁止卸载/禁用被依赖的
   插件、`force` 级联、重载依赖时级联重载使用者、`shutdown()` 逆序。

跑法：
    ./venv-arm/bin/python -B tests/test_plugin_deps.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP_DIR = ROOT / "tests" / ".test-tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

from napcat.command import CommandManager
from napcat.errors import ErrorReporter, PluginDependencyError
from napcat.events import EventBus, TimerManager
from napcat.file_manager import FileManager
from napcat.client import GroupBinding
from napcat.permission import PermissionManager
from napcat.plugin import PluginManager
import napcat.deps as dep
from napcat.config import ConfigManager                           # noqa: E402

_passed = 0
_failed = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        _failures.append(name)
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


class Notified:
    """替代 ErrorReporter：记下「本该私聊超管」的失败。"""

    def __init__(self):
        self.items: list[tuple[str, str]] = []

    def set_plugin_resolver(self, resolver):
        pass

    async def notify_operators(self, error, *, where=""):
        self.items.append((getattr(error, "plugin", ""), str(error),
                           getattr(error, "audience", "")))

    def texts(self) -> str:
        return "\n".join(f"{plugin}: {text}" for plugin, text, _a in self.items)

    def audiences(self) -> list[str]:
        return [a for _p, _t, a in self.items]


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.plugin-deps")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    return logger


PLUGIN_TEMPLATE = '''\
from napcat.plugin import PluginBase

EVENTS = []


class {cls}(PluginBase):
    name = {name!r}
    version = {version!r}
{extra}
    async def on_enable(self):
        EVENTS.append("enable:{name}")

    async def on_disable(self):
        EVENTS.append("disable:{name}")

    async def on_unload(self):
        EVENTS.append("unload:{name}")

    def mark(self) -> str:
        return NAME

NAME = {name!r}
'''


def write_plugin(pdir: Path, name: str, *, version: str = "1.0.0",
                 requires=(), optional=(), provides_version: str = "") -> Path:
    extra = ""
    if requires:
        extra += f"    requires = {list(requires)!r}\n"
    if optional:
        extra += f"    optional_requires = {list(optional)!r}\n"
    if provides_version:
        extra += f"    provides_version = {provides_version!r}\n"
    pkg = pdir / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        f"from .main import {name.capitalize()}Plugin\n", encoding="utf-8")
    (pkg / "main.py").write_text(
        PLUGIN_TEMPLATE.format(cls=f"{name.capitalize()}Plugin", name=name,
                               version=version, extra=extra),
        encoding="utf-8")
    return pkg


def make_mgr(pdir: Path, *, reporter=None):
    cfg = ConfigManager(str(pdir.parent / "config.json"))
    bus, timer = EventBus(), TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[999])
    cmds = CommandManager(prefixes=["/"], perm_manager=perm)
    notifier = reporter or Notified()
    mgr = PluginManager(str(pdir), api=None, event_bus=bus, timer=timer,
                        cmd_manager=cmds, perm_manager=perm,
                        file_manager=FileManager(api=None), logger=_logger(),
                        cfg=cfg, groups=GroupBinding(bound=set(), all_groups=True),
                        # 线上是 NapCatClient：插件通过 `self.client.plugins`
                        # 拿到插件管理器（`self.deps` 就靠它）。这里给个同样形状的。
                        client=SimpleNamespace(plugins=None),
                        error_reporter=notifier)
    mgr._client.plugins = mgr
    cmds._plugins_ref = mgr
    return mgr, cmds, notifier


# ==================== 1. 解析与校验 ====================


def test_parse():
    print("\n[1] 依赖声明解析与版本比较")
    spec = dep.parse_spec("store>=1.2")
    check("解析出名字与版本要求",
          spec.name == "store" and spec.op == ">=" and spec.version == "1.2",
          str(spec))
    check("纯名字没有版本要求",
          dep.parse_spec("store").has_version_requirement is False)
    check("dict 写法也认",
          dep.parse_spec({"name": "store", "version": ">=1.2"}).version == "1.2")

    check("版本号按数字段比较（1.10 > 1.9）",
          dep.version_key("1.10") > dep.version_key("1.9"))
    check("1.0 == 1.0.0（末尾 0 不影响）",
          dep.version_key("1.0") == dep.version_key("1.0.0"))
    check(">= / > / == 判定",
          dep.version_matches("1.2.0", ">=", "1.2")
          and dep.version_matches("1.3", ">", "1.2")
          and not dep.version_matches("1.1", ">=", "1.2")
          and dep.version_matches("v1.2.0", "==", "1.2"))
    check("版本未知（空）时不算满足",
          dep.version_matches("", ">=", "1.0") is False)

    for bad, why in (("", "空字符串"), ("1store", "数字开头"),
                     ("store>=abc!", "版本号含非法字符"),
                     ("store>=", "只有比较符"), ("a b", "名字里有空格")):
        try:
            dep.parse_spec(bad)
            check(f"拒绝非法声明（{why}）", False, "居然通过了")
        except dep.DependencyError:
            check(f"拒绝非法声明（{why}）", True)

    tried = False
    try:
        dep.validate_declaration("a", ["a"], [])
        tried = True
    except dep.DependencyError:
        pass
    check("拒绝依赖自己", not tried)

    tried = False
    try:
        dep.validate_declaration("a", ["b"], ["b"])
        tried = True
    except dep.DependencyError:
        pass
    check("拒绝「同时在必需与可选里」", not tried)

    required, optional = dep.validate_declaration("a", ["b", "b>=1.2"], ["c"])
    check("同名去重且保留版本要求",
          [str(s) for s in required] == ["b>=1.2"] and len(optional) == 1,
          str([str(s) for s in required]))

    class _P:
        name = "p"
        requires = ["store", "chat>=1.2"]
        optional_requires = ["ai_chat"]
    check("dependency_names() 拆成 (必需, 可选)",
          dep.dependency_names(_P()) == (["store", "chat>=1.2"], ["ai_chat"]))


# ==================== 2. 拓扑排序 ====================


def test_order():
    print("\n[2] 拓扑排序与环检测")
    graph = {
        # 使用者 → (必需, 可选)
        "app": (["store", "chat"], []),
        "chat": (["store"], []),
        "store": ([], []),
        "solo": ([], []),
        "opt": ([], ["chat"]),          # 可选依赖也定先后
    }
    order = dep.load_order(list(graph), lambda n: graph[n])
    position = {name: i for i, name in enumerate(order)}
    check("每条依赖边都满足「依赖在使用者之前」",
          all(position[dep] < position[user]
              for user, (req, opt) in graph.items()
              for dep in list(req) + list(opt)),
          str(order))
    check("无依赖的独立插件不受影响", set(order) == set(graph), str(order))
    check("chat 在 store 之后、app 之前",
          order.index("store") < order.index("chat") < order.index("app"),
          str(order))
    check("可选依赖也排在使用者之前",
          order.index("chat") < order.index("opt"), str(order))
    check("无依赖的按字典序（稳定）",
          dep.load_order(["b", "a"], lambda n: ([], [])) == ["a", "b"])

    unload = dep.unload_order(list(graph), lambda n: graph[n])
    check("卸载顺序：使用者在前",
          unload.index("app") < unload.index("chat") < unload.index("store"),
          str(unload))

    cycle = {"a": (["b"], []), "b": (["a"], [])}
    try:
        dep.load_order(list(cycle), lambda n: cycle[n])
        check("必需依赖成环 → 报错", False, "居然排出来了")
    except dep.DependencyError as exc:
        check("必需依赖成环 → 报错", "环" in str(exc) or "→" in str(exc), str(exc))

    # 可选依赖成环不该阻塞（它们不是硬约束）
    soft = {"a": ([], ["b"]), "b": ([], ["a"])}
    try:
        order = dep.load_order(list(soft), lambda n: soft[n])
        check("可选依赖成环不阻塞加载", sorted(order) == ["a", "b"], str(order))
    except dep.DependencyError as exc:
        check("可选依赖成环不阻塞加载", False, str(exc))

    check("缺失的依赖不参与排序",
          dep.load_order(["a"], lambda n: (["不存在"], [])) == ["a"])


# ==================== 3/4. 加载期与运行期 ====================


async def test_runtime(work: Path):
    print("\n[3] 加载期：必需依赖缺失 → 不加载")
    pdir = work / "runtime"
    pdir.mkdir(parents=True, exist_ok=True)
    write_plugin(pdir, "app", requires=["store"])
    mgr, cmds, notifier = make_mgr(pdir)
    await mgr.load_all()
    check("依赖缺失时 app 不在框架里", mgr.get("app") is None)
    check("原因记进 blocked_reason",
          "store" in mgr.blocked_reason("app"), mgr.blocked_reason("app"))
    check("私聊超管收到「缺少 store」的原因",
          "缺少 store" in notifier.texts(), notifier.texts())
    check("通知用的是「只给超管看」的错误类型（群里不出现内部细节）",
          notifier.audiences() == ["operator"], str(notifier.audiences()))
    check("提示里给了安装命令", "/plugin install store" in notifier.texts(),
          notifier.texts())

    print("\n[4] 依赖就位后自动加载")
    write_plugin(pdir, "store")
    await mgr.load_all()
    check("store 起来了", mgr.get("store") is not None)
    check("app 跟着自动加载", mgr.get("app") is not None)
    check("blocked 记录被清掉", mgr.blocked_reason("app") == "")
    check("加载顺序：store 先于 app",
          mgr.load_order().index("store") < mgr.load_order().index("app"),
          str(mgr.load_order()))
    check("启用顺序也带依赖（app 的依赖已启用）",
          await mgr.enable_all() is None and mgr.get("store").enabled
          and mgr.get("app").enabled)

    print("\n[5] 运行期：self.deps 与依赖查询")
    app, store = mgr.get("app"), mgr.get("store")
    check("deps.get 拿到依赖实例", app.deps.get("store") is store)
    check("deps.has / enabled", app.deps.has("store") and app.deps.enabled("store"))
    check("deps.names 含声明的依赖", app.deps.names() == ["store"], str(app.deps.names()))
    check("缺失依赖的 get 返回 None（不抛）", app.deps.get("nope") is None)
    check("dependents 反查", mgr.dependents("store") == ["app"], str(mgr.dependents("store")))
    check("dependencies 正查", mgr.dependencies("app") == ["store"])

    print("\n[6] 不能拆别人的台：卸载/禁用被依赖的插件")
    store._enabled = True
    app._enabled = True
    check("卸载 store 被拒绝", await mgr.unload("store") is False)
    check("store 还在", mgr.get("store") is not None)
    check("禁用 store 被拒绝", await mgr.disable("store") is False)
    check("store 仍是启用状态", mgr.get("store").enabled)
    check("force 卸载会先卸载使用者",
          await mgr.unload("store", force=True) is True and mgr.get("store") is None
          and mgr.get("app") is None)

    print("\n[6.5] 显式禁用依赖 → 依赖它的插件不加载/不启用")
    pdir65 = work / "explicit-disable"
    pdir65.mkdir(parents=True, exist_ok=True)
    write_plugin(pdir65, "store")
    write_plugin(pdir65, "app", requires=["store"])
    mgr65, _c65, _n65 = make_mgr(pdir65)
    await mgr65.load_all()
    await mgr65.enable_all()
    check("先把两个都跑起来", mgr65.get("app").enabled)
    # 强制禁用 store（force）：使用者被级联禁用
    check("force 禁用被依赖的插件 → 级联禁用使用者",
          await mgr65.disable("store", force=True) is True
          and not mgr65.get("store").enabled and not mgr65.get("app").enabled)
    # 使用者还在（只是没启用），再启用 app 会因为依赖被禁用而失败
    check("依赖被禁用时启用失败", await mgr65.enable("app") is False)
    # 把 store 卸载 → app 也不能再启用
    await mgr65.unload("store", force=True)
    check("依赖不在时启用失败", await mgr65.enable("app") is False)
    check("依赖不在时 reload 也会拒绝加载",
          await mgr65.reload("app") is False and mgr65.get("app") is None)

    print("\n[7] 可选依赖：缺失不影响加载")
    pdir2 = work / "optional"
    pdir2.mkdir(parents=True, exist_ok=True)
    write_plugin(pdir2, "rich", optional=["ai_chat"])
    write_plugin(pdir2, "ai_chat")
    mgr2, _cmds2, notifier2 = make_mgr(pdir2)
    await mgr2.load_all()
    check("可选依赖在 → 正常加载", mgr2.get("rich") is not None)
    check("可选依赖缺失 → 也加载（先删掉再试）", True)
    await mgr2.unload("ai_chat", force=True)
    check("禁用/卸载可选依赖不会被拒绝",
          await mgr2.load("ai_chat") is not None
          and await mgr2.reload("ai_chat") is True)
    check("可选依赖没被算成必需（不阻塞）",
          mgr2.blocked_reason("rich") == "")

    pdir3 = work / "optional-missing"
    pdir3.mkdir(parents=True, exist_ok=True)
    write_plugin(pdir3, "rich", optional=["ai_chat"])
    mgr3, _c3, notifier3 = make_mgr(pdir3)
    await mgr3.load_all()
    check("可选依赖完全没装 → 照常加载", mgr3.get("rich") is not None)
    check("可选依赖不报超管", notifier3.texts() == "", notifier3.texts())
    check("运行期 get 返回 None", mgr3.get("rich").deps.get("ai_chat") is None)
    check("require() 对缺失的可选依赖会明确报错",
          await _raises(lambda: mgr3.get("rich").deps.require("ai_chat")))

    print("\n[8] 版本要求")
    pdir4 = work / "versioned"
    pdir4.mkdir(parents=True, exist_ok=True)
    write_plugin(pdir4, "old_store", version="1.0.0")
    write_plugin(pdir4, "needs_new", requires=["old_store>=1.2"])
    mgr4, _c4, notifier4 = make_mgr(pdir4)
    await mgr4.load_all()
    check("版本不满足 → 不加载", mgr4.get("needs_new") is None)
    check("原因里写明需要与实际",
          "1.2" in mgr4.blocked_reason("needs_new")
          and "1.0.0" in mgr4.blocked_reason("needs_new"),
          mgr4.blocked_reason("needs_new"))
    (pdir4 / "old_store" / "main.py").write_text(
        PLUGIN_TEMPLATE.format(cls="Old_storePlugin", name="old_store",
                               version="1.3.0", extra="    requires = []\n"),
        encoding="utf-8")
    await mgr4.reload("old_store")
    await mgr4.load("needs_new")
    check("依赖升级后自动可加载", mgr4.get("needs_new") is not None)

    print("\n[9] provides_version 覆盖对外版本")
    pdir5 = work / "provides"
    pdir5.mkdir(parents=True, exist_ok=True)
    write_plugin(pdir5, "lib", version="0.1.0", provides_version="2.0.0")
    write_plugin(pdir5, "user", requires=["lib>=1.5"])
    mgr5, _c5, _n5 = make_mgr(pdir5)
    await mgr5.load_all()
    check("按 provides_version 比对（0.1.0 也算满足 >=1.5）",
          mgr5.get("user") is not None)

    print("\n[10] 声明写坏时不让整批加载挂掉")
    pdir6 = work / "broken"
    pdir6.mkdir(parents=True, exist_ok=True)
    good = write_plugin(pdir6, "good")
    bad = pdir6 / "bad"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "__init__.py").write_text("from .main import BadPlugin\n", encoding="utf-8")
    (bad / "main.py").write_text(
        "from napcat.plugin import PluginBase\n\n\nclass BadPlugin(PluginBase):\n"
        "    name = 'bad'\n    requires = ['bad']   # 依赖自己\n", encoding="utf-8")
    mgr6, _c6, notifier6 = make_mgr(pdir6)
    await mgr6.load_all()
    check("好插件照常加载", mgr6.get("good") is not None)
    # 这条断言必须在**别的插件加载成功之前**取：blocked_reason 是判定缓存，
    # 成功的 load 会清空整张缓存（见 `_invalidate_deps`），之后就得靠实时判定
    check("坏声明被记下原因（判定缓存里）",
          "非法" in mgr6.blocked_reason("bad"), mgr6.blocked_reason("bad"))
    # ⚠️ `blocked_reason()` 是「上次为什么被挡下」的**缓存**，成功的 load 会清空它
    # （见 `_invalidate_deps`）—— 所以「好插件加载成功」之后不能拿它断言坏插件。
    # 实时判定由 `/plugins` 用的 `dependency_verdict()` 提供。
    check("坏声明被拒（没进框架）", mgr6.get("bad") is None)
    check("/plugins 里能看到它被拒与原因",
          "bad" in _plugins_text(mgr6) and "非法" in _plugins_text(mgr6),
          _plugins_text(mgr6))
    check("坏声明私聊了超管", "非法" in notifier6.texts(), notifier6.texts())

    print("\n[11] 重载依赖 → 级联重载使用者")
    pdir7 = work / "cascade"
    pdir7.mkdir(parents=True, exist_ok=True)
    write_plugin(pdir7, "store")
    write_plugin(pdir7, "app", requires=["store"])
    mgr7, _c7, _n7 = make_mgr(pdir7)
    await mgr7.load_all()
    await mgr7.enable_all()
    old_store = mgr7.get("store")
    old_app = mgr7.get("app")
    await mgr7.reload("store")
    check("store 换了新实例", mgr7.get("store") is not old_store)
    check("使用者 app 也被级联重载（拿到新实例）",
          mgr7.get("app") is not old_app and mgr7.get("app") is not None)
    check("级联后使用者仍启用", mgr7.get("app").enabled)
    check("依赖引用已更新",
          mgr7.get("app").deps.get("store") is mgr7.get("store"))

    print("\n[12] shutdown 按卸载顺序（force 级联）")
    await mgr7.shutdown()
    check("全部卸载干净", mgr7.plugins == {}, str(list(mgr7.plugins)))

    print("\n[13] /plugins 展示依赖与阻塞")
    pdir8 = work / "display"
    pdir8.mkdir(parents=True, exist_ok=True)
    write_plugin(pdir8, "app", requires=["store"], optional=["ai_chat"])
    mgr8, cmds8, _n8 = make_mgr(pdir8)
    await mgr8.load_all()
    text = _plugins_text(mgr8)
    check("/plugins 列出被阻塞的插件与原因",
          "app" in text and "缺少 store" in text and "⛔" in text, text)


def _plugins_text(mgr) -> str:
    """跑一遍内置 `/plugins` 命令，拿到文本（它按依赖序展示 + 标出被阻塞的）。"""
    handler = mgr.cmd_manager._commands.get("plugins")
    return str(handler.func(None)) if handler is not None else ""


async def _raises(func) -> bool:
    try:
        result = func()
        if asyncio.iscoroutine(result):
            await result
        return False
    except Exception:
        return True


def _msg(text: str, *, user_id: int = 999, private: bool = True):
    from napcat.models import MessageEvent, Sender
    return MessageEvent(
        message_type="private" if private else "group",
        user_id=user_id, group_id=None if private else 123456,
        self_id=1, sender=Sender(user_id=user_id, nickname="t"),
        message=[{"type": "text", "data": {"text": text}}], raw_message=text)


def main() -> int:
    work = TMP_DIR / f"plugin-deps-{int(time.time())}"
    work.mkdir(parents=True, exist_ok=True)
    test_parse()
    test_order()
    try:
        asyncio.run(test_runtime(work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{'=' * 60}\n通过 {_passed} 项，失败 {_failed} 项")
    if _failures:
        print("失败项：")
        for name in _failures:
            print(f"  - {name}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
