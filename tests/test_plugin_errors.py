"""错误类型 + 统一错误反馈 + PluginManager 缺陷回归。

覆盖：

1. 错误分类：分类名 / 是否可重试 / 受众（触发者 vs 超管）；
2. 命令失败：普通异常不再被吞成 None → 回一句带编号的提示；
3. 事件处理器失败：同样有回应（历史上只 print 堆栈，群里一个字都没有）；
4. 定时任务失败：走超管通道（没有「触发者」可回）；
5. 插件生命周期失败：只私聊超管，群里不出现内部细节；
6. 节流：同编号重复出现 → 短文案（仍然回应，不静默）；
7. 非法输入：参数过多回一句；`#include<...>` 这类正文保持静默；
8. PluginManager 缺陷：stop_event 参数名、加载失败残留、启用失败回滚、
   unload 子模块清理、命令按插件标签回收。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP_DIR = ROOT / "tests" / ".test-tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)   # 新 clone 里没有它（.gitignore 排除）

from napcat import (                                              # noqa: E402
    CommandExecutionError, CommandManager, EventBus, FileManager, MessageChain,
    MessageEvent, PermissionManager, PluginBase, PluginManager, PluginTimeoutError,
    PluginConfigError, PluginLoadError, PluginRuntimeError, TimerManager,
    ErrorReporter,
)
from napcat.errors import PluginAccessError                           # noqa: E402

TMP = ROOT / "tests" / ".test-tmp" / "plugin-errors"

_passed = 0
_failed = 0


def _plugin_ready(name: str) -> bool:
    """框架仓库单独 checkout 时 `plugins/` 是空的 → 依赖真实插件的检查跳过。"""
    if (ROOT / "plugins" / name / "__init__.py").exists():
        return True
    print(f"  ⏭️  跳过：{name} 插件不在 plugins/ 下（在它自己的仓库里）")
    return False


def check(name: str, ok: bool, detail: str = "") -> bool:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def _logger() -> logging.Logger:
    logger = logging.getLogger("test.plugin-errors")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    return logger


class FakeApi:
    """只记录发出去的消息，不碰网络。"""

    def __init__(self):
        self.group: list[tuple[str, list]] = []
        self.private: list[tuple[str, list]] = []

    async def send_group_msg(self, *, group_id, message):
        self.group.append((str(group_id), message.build()))
        return None

    async def send_private_msg(self, *, user_id, message, group_id=None):
        self.private.append((str(user_id), message.build()))
        return None


def make_reporter(api: FakeApi, *, throttle: float = 60.0, enabled: bool = True,
                  superusers=(999,)) -> ErrorReporter:
    async def send(event, text):
        chain = MessageChain()
        if getattr(event, "group_id", None) and getattr(event, "message_type", "") == "group":
            chain.at(str(getattr(event, "user_id", ""))).text(" ")
        chain.text(text)
        await api.send_group_msg(group_id=str(event.group_id), message=chain)

    async def send_private(user_id, text):
        await api.send_private_msg(user_id=str(user_id), message=MessageChain().text(text))

    return ErrorReporter(logger=_logger(), perm=PermissionManager(superusers=list(superusers)),
                         send=send, send_private=send_private, enabled=enabled,
                         throttle_seconds=throttle)


def text_of(chain: list) -> str:
    return "".join(seg.get("data", {}).get("text", "") for seg in chain
                   if seg.get("type") == "text")


def at_of(chain: list) -> list[str]:
    return [str(seg["data"].get("qq")) for seg in chain if seg.get("type") == "at"]


# ==================== 1. 错误分类 ====================


def test_taxonomy():
    print("\n[1] 错误分类：分类名 / 重试 / 受众")
    check("超时算「可重试」", PluginTimeoutError("t").retryable is True)
    check("配置错误不算可重试", PluginConfigError("c").retryable is False)
    check("生命周期错误受众是超管", PluginLoadError("x").audience == "operator")
    check("运行错误受众是触发者", PluginRuntimeError("x").audience == "trigger")
    check("CommandExecutionError 构造参数就是给用户的话",
          CommandExecutionError("会话列表读取失败").user_message == "会话列表读取失败")
    rep = make_reporter(FakeApi())
    same = [rep.code(PluginTimeoutError("a"), "p1"), rep.code(PluginTimeoutError("b"), "p1")]
    check("同插件同类型的编号稳定（便于 grep 日志）", same[0] == same[1], str(same))
    other = rep.code(PluginTimeoutError("a"), "p2")
    check("不同插件编号不同", other != same[0], f"{other} vs {same[0]}")


# ==================== 2-3. 命令 / 事件失败一定有回应 ====================


async def test_command_failure():
    print("\n[2] 命令失败：不再吞成 None，回一句带编号的提示")
    api, rep = FakeApi(), None
    rep = make_reporter(api)
    cmds = CommandManager(perm_manager=PermissionManager(superusers=[999]))
    cmds.set_error_reporter(rep)

    @cmds.register("boom", plugin="demo")
    async def boom(packet):
        raise ValueError("内部状态不一致")

    @cmds.register("slow", plugin="demo")
    async def slow(packet):
        raise PluginTimeoutError("上游 120s 没响应", plugin="demo")

    @cmds.register("nope", plugin="demo")
    async def nope(packet):
        raise CommandExecutionError("会话不存在，先 /aichat new")

    ev = lambda text: MessageEvent(message_type="group", group_id=100000003,  # noqa: E731
                                   user_id=10000001, message=text)

    r1 = await cmds.handle(ev("/boom"))
    check("普通异常有回应（不是 None）", r1 is not None, repr(r1))
    check("回应里带错误编号", "E-" in (r1 or ""), repr(r1))
    check("回应里带短原因", "内部状态不一致" in (r1 or ""), repr(r1))
    check("群里 @ 了触发的人",
          at_of(api.group[-1][1]) == ["10000001"], str(api.group[-1][1]))

    r2 = await cmds.handle(ev("/slow"))
    check("可重试错误提示「稍后再试」", "稍后再试" in (r2 or ""), repr(r2))

    r3 = await cmds.handle(ev("/nope"))
    check("CommandExecutionError 的文案原样回给用户",
          "会话不存在" in (r3 or ""), repr(r3))

    r4 = await cmds.handle(ev("/boom"))
    check("重复同一错误仍然回应（节流成短文案）", r4 is not None and "又出错了" in r4,
          repr(r4))

    r5 = await cmds.handle(ev("/unknowncmd"))
    check("未知命令仍走原来的未知命令提示", r5 == ("_not_found", "unknowncmd"), repr(r5))


async def test_event_failure():
    print("\n[3] 事件处理器失败：群里也能看到（以前只有 traceback）")
    api = FakeApi()
    rep = make_reporter(api)
    bus = EventBus()
    bus.set_error_reporter(rep)

    @bus.on(MessageEvent, hid="demo._on_any")
    async def handler(event):
        raise RuntimeError("处理器炸了")

    await bus.dispatch(MessageEvent(message_type="group", group_id=123, user_id=456,
                                    message="@bot 你好"))
    check("事件失败也有回复", bool(api.group), str(api.group))
    if api.group:
        body = text_of(api.group[-1][1])
        check("回复带编号", "E-" in body, body)
        check("回复里写明出错位置", "demo._on_any" in body, body)
        check("群里 @ 了触发的人", at_of(api.group[-1][1]) == ["456"],
              str(api.group[-1][1]))

    # 没有 error_reporter 时保持旧行为（只打 traceback），不抛异常
    bus2 = EventBus()

    @bus2.on(MessageEvent, hid="demo.raw")
    async def raw(event):
        raise RuntimeError("x")

    await bus2.dispatch(MessageEvent(message_type="private", user_id=1, message="hi"))
    check("未注入 reporter 时不抛异常（退化为日志）", True)


async def test_timer_and_lifecycle():
    print("\n[4-5] 定时任务失败 → 超管；插件生命周期失败 → 超管")
    api = FakeApi()
    rep = make_reporter(api)
    timer = TimerManager()
    timer.set_error_reporter(rep)
    timer.start()

    @timer.every(0.01, name="demo_timer")
    async def tick():
        raise RuntimeError("定时任务炸了")

    # `TimerManager.every` 的包装器先 sleep(1) 再跑第一轮，所以最多等 3 秒
    for _ in range(150):
        await asyncio.sleep(0.02)
        if api.private:
            break
    timer.stop()
    check("定时任务失败私聊超管", bool(api.private), str(api.private))
    if api.private:
        check("超管收到的是带编号的文案", "E-" in text_of(api.private[-1][1]),
              text_of(api.private[-1][1]))
        check("超管 id 正确", api.private[-1][0] == "999", api.private[-1][0])
        check("定时任务失败不发群", not api.group, str(api.group))

    # 生命周期：加载失败 → 只通知超管
    api2 = FakeApi()
    rep2 = make_reporter(api2)
    pdir = TMP / "lifecycle" / "plugins"
    pdir.mkdir(parents=True, exist_ok=True)
    broken = pdir / "brokenplug"
    broken.mkdir(exist_ok=True)
    (broken / "__init__.py").write_text("raise RuntimeError('导入就炸')\n", encoding="utf-8")

    mgr = PluginManager(str(pdir), api=None, event_bus=EventBus(), timer=TimerManager(),
                        cmd_manager=CommandManager(perm_manager=PermissionManager()),
                        perm_manager=PermissionManager(superusers=[999]),
                        file_manager=FileManager(api=None), logger=_logger(),
                        error_reporter=rep2)
    check("加载失败返回 None", await mgr.load("brokenplug") is None)
    check("加载失败已私聊超管", bool(api2.private), str(api2.private))
    check("加载失败不发群", not api2.group, str(api2.group))


# ==================== 6. 非法输入 ====================


async def test_illegal_input():
    print("\n[6] 非法输入：参数过多回一句，正文保持静默")
    cmds = CommandManager(perm_manager=PermissionManager())
    cmds.set_error_reporter(make_reporter(FakeApi()))
    ev = lambda text: MessageEvent(message_type="group", group_id=1, user_id=1,  # noqa: E731
                                   message=text)
    many = "/ping " + " ".join(str(i) for i in range(25))
    r = await cmds.handle(ev(many))
    check("参数过多有回应", isinstance(r, str) and "参数过多" in r, repr(r))
    silent = await cmds.handle(ev("#include<bits/stdc++.h>"))
    check("`#include<...>` 保持静默（ai_chat 当普通对话处理）", silent is None, repr(silent))
    terminator = await cmds.handle(ev("//"))
    check("结束符 `//` 保持静默", terminator is None, repr(terminator))


# ==================== 7. PluginManager 缺陷回归 ====================


def write_plugin(pdir: Path, name: str, body: str = "", sub: dict | None = None,
                 init: str | None = None) -> Path:
    pkg = pdir / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(init if init is not None else (
        "from napcat import PluginBase\n"
        f"class {name.capitalize()}Plugin(PluginBase):\n"
        f"    name = {name!r}\n"
        "    version = '1.0.0'\n" + body), encoding="utf-8")
    for fname, text in (sub or {}).items():
        (pkg / fname).write_text(text, encoding="utf-8")
    return pkg


def make_mgr(plugin_dir: Path):
    bus, timer = EventBus(), TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[999])
    cmds = CommandManager(prefixes=["/"], perm_manager=perm)
    mgr = PluginManager(str(plugin_dir), api=None, event_bus=bus, timer=timer,
                        cmd_manager=cmds, perm_manager=perm,
                        file_manager=FileManager(api=None), logger=_logger(),
                        error_reporter=make_reporter(FakeApi()))
    cmds._plugins_ref = mgr
    return mgr, bus, timer, cmds


async def test_plugin_manager_defects():
    print("\n[7] PluginManager 缺陷回归")

    # (a) stop_event / pause_event / resume_event 以前传的是内建 `id`
    pdir = TMP / "defects" / "plugins"
    mgr, bus, timer, cmds = make_mgr(pdir)
    p = PluginBase.__new__(PluginBase)
    p.event_bus, p._handlers, p._handlers_base, p.name = bus, [], 0, "p"

    @bus.on(MessageEvent, hid="p.h")
    async def handler(event):
        pass

    check("stop_event 能按 hid 移除监听器", p.stop_event("p.h") is True)
    check("移除后监听器列表为空", not bus.list_all(), str(bus.list_all()))

    @bus.on(MessageEvent, hid="p.h2")
    async def handler2(event):
        pass

    check("pause_event 能暂停", p.pause_event("p.h2") is True
          and bus.list_all()[0].paused is True)
    check("resume_event 能恢复", p.resume_event("p.h2") is True
          and bus.list_all()[0].paused is False)

    # (b) 加载失败：事件/定时器/命令都必须回收（且命令按插件标签回收）
    api = FakeApi()
    mgr, bus, timer, cmds = make_mgr(pdir)
    write_plugin(pdir, "badplug", body=(
        "    async def on_load(self):\n"
        "        @self.on_event(__import__('napcat').LifeCycleEvent, hid='badplug.life')\n"
        "        async def _h(ev): ...\n"
        "        @self.schedule(30, name='badplug_timer')\n"
        "        async def _t(): ...\n"
        "        @self.register_command('weird_global_name')\n"
        "        async def _c(packet): return 'x'\n"
        "        raise RuntimeError('加载期故意失败')\n"))
    check("加载失败 → load() 返回 None", await mgr.load("badplug") is None)
    check("加载失败 → 事件处理器已回收", not bus.list_all(), str(bus.list_all()))
    check("加载失败 → 定时任务已回收", not timer._metas, str(timer._metas))
    check("加载失败 → 命令已按插件标签回收（哪怕命令名与插件名无关）",
          "weird_global_name" not in cmds.list_commands(),
          str(sorted(cmds.list_commands())))

    # (c) 启用失败：回滚本次已注册的处理器/定时器/命令
    write_plugin(pdir, "enfail", body=(
        "    async def on_enable(self):\n"
        "        @self.on_message()\n"
        "        async def _m(event): ...\n"
        "        @self.schedule(30, name='enfail_timer')\n"
        "        async def _t(): ...\n"
        "        @self.register_command('enfail_cmd')\n"
        "        async def _c(packet): return 'x'\n"
        "        raise RuntimeError('启用期故意失败')\n"))
    await mgr.load("enfail")
    p = mgr.get("enfail")
    check("启用失败 → enable() 返回 False", await mgr.enable("enfail") is False)
    check("启用失败 → 已注册的处理器被回滚", len(p._handlers) == 0, str(p._handlers))
    check("启用失败 → 事件总线里没有残留", not bus.list_all(), str(bus.list_all()))
    check("启用失败 → 定时任务被回滚", not timer._metas, str(timer._metas))
    check("启用失败 → 命令被回滚", "enfail_cmd" not in cmds.list_commands(),
          str(sorted(cmds.list_commands())))
    check("启用失败 → _handlers_base 归零", p._handlers_base == 0)

    # 加载成功但 on_enable 失败时，热重载不能报「成功」（否则 /reload 与
    # watcher 都以为没事了，这份代码再也不会被重试）
    pdir5 = TMP / "reload_enfail" / "plugins"
    write_plugin(pdir5, "renf", body="    async def on_enable(self): ...\n")
    mgr5, bus5, timer5, cmds5 = make_mgr(pdir5)
    await mgr5.load("renf")
    await mgr5.enable("renf")
    write_plugin(pdir5, "renf", body=(
        "    async def on_enable(self):\n"
        "        raise RuntimeError('新版本启用就炸')\n"))
    check("新版本加载成功但启用失败 → reload() 返回 False",
          await mgr5.reload("renf") is False)
    check("此时插件处于「已加载但未启用」", mgr5.get("renf") is not None
          and mgr5.get("renf").enabled is False)
    timer5.stop()

    # (d) unload 清理整棵模块子树（以前只 pop 父模块，改子模块不生效）
    pdir2 = TMP / "submodules" / "plugins"
    write_plugin(pdir2, "staleplug", init=(
        "from napcat import PluginBase\n"
        "from .sub import MARK\n"
        "class StaleplugPlugin(PluginBase):\n"
        "    name = 'staleplug'\n"
        "    version = MARK\n"), sub={"sub.py": "MARK = '1'\n"})
    mgr2, bus2, timer2, cmds2 = make_mgr(pdir2)
    await mgr2.load("staleplug")
    check("首次加载拿到子模块常量 1", mgr2.get("staleplug").version == "1")
    await mgr2.unload("staleplug")
    check("unload 后子模块不再残留于 sys.modules",
          not [k for k in sys.modules if k.startswith("napcat_plugin_staleplug")],
          str([k for k in sys.modules if k.startswith("napcat_plugin_staleplug")]))
    (pdir2 / "staleplug" / "sub.py").write_text("MARK = '2'\n", encoding="utf-8")
    await mgr2.load("staleplug")
    check("unload→load 之后拿到的是新子模块代码（2，不是 1）",
          mgr2.get("staleplug").version == "2", mgr2.get("staleplug").version)

    # (e) 热重载卸载旧版本时，旧命令必须回收（否则旧代码还能被调用）
    pdir3 = TMP / "stale_command" / "plugins"
    write_plugin(pdir3, "cmdplug", body=(
        "    async def on_enable(self):\n"
        "        @self.register_command('cmdplug_old')\n"
        "        async def _c(packet): return 'old'\n"))
    mgr3, bus3, timer3, cmds3 = make_mgr(pdir3)
    await mgr3.load("cmdplug")
    await mgr3.enable("cmdplug")
    check("命令已注册", "cmdplug_old" in cmds3.list_commands())
    write_plugin(pdir3, "cmdplug", body=(
        "    async def on_enable(self):\n"
        "        @self.register_command('cmdplug_new')\n"
        "        async def _c(packet): return 'new'\n"))
    check("热重载成功", await mgr3.reload("cmdplug") is True)
    check("旧版本的命令已被回收", "cmdplug_old" not in cmds3.list_commands(),
          str(sorted(cmds3.list_commands())))
    check("新版本的命令可用", "cmdplug_new" in cmds3.list_commands(),
          str(sorted(cmds3.list_commands())))

    # (f) 卸载时定时任务被回收
    pdir4 = TMP / "timer_reclaim" / "plugins"
    write_plugin(pdir4, "timedplug", body=(
        "    async def on_enable(self):\n"
        "        @self.schedule(30, name='timedplug_tick')\n"
        "        async def _t(): ...\n"))
    mgr4, bus4, timer4, cmds4 = make_mgr(pdir4)
    await mgr4.load("timedplug")
    await mgr4.enable("timedplug")
    check("定时任务已注册", "timedplug_tick" in timer4._metas, str(timer4._metas))
    await mgr4.unload("timedplug")
    check("unload 回收定时任务", "timedplug_tick" not in timer4._metas, str(timer4._metas))
    timer4.stop()


# ==================== 8. 归属解析与 ai_chat 的 @ ====================


class CapturingLogger(logging.Logger):
    def __init__(self, name: str):
        super().__init__(name)
        self.records: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: self.records.append(record.getMessage())
        self.addHandler(handler)
        self.propagate = False
        self.setLevel(logging.DEBUG)


async def test_attribution_and_ai_chat():
    if not _plugin_ready("ai_chat"):
        return
    print("\n[8] 归属解析（日志里认得出是哪个插件）+ ai_chat 群里 @ 提问者")
    pdir = TMP / "attribution" / "plugins"
    api = FakeApi()
    capturing = CapturingLogger("test.plugin-errors.capture")
    rep = ErrorReporter(logger=capturing, perm=PermissionManager(superusers=[999]),
                        send=make_reporter(api)._send, send_private=None)
    _, bus, timer, cmds = make_mgr(pdir)
    bus.set_error_reporter(rep)

    write_plugin(pdir, "attrib", body=(
        "    async def on_enable(self):\n"
        "        @self.on_message()\n"
        "        async def _handler(event): ...\n"))
    mgr2 = PluginManager(str(pdir), api=None, event_bus=bus, timer=timer,
                         cmd_manager=cmds, perm_manager=PermissionManager(superusers=[999]),
                         file_manager=FileManager(api=None), logger=_logger(),
                         error_reporter=rep)
    await mgr2.load("attrib")
    await mgr2.enable("attrib")
    func = mgr2.get("attrib")._handlers[0][1]
    check("plugin_of_handler 精确认出处理器归属",
          mgr2.plugin_of_handler(func) == "attrib", mgr2.plugin_of_handler(func))
    check("框架内置处理器归属为空串（不误报）", mgr2.plugin_of_handler(lambda e: None) == "")

    @bus.on(MessageEvent, hid="attrib.boom")
    async def boom(event):
        raise RuntimeError("归属测试")

    # 让这个处理器能被归属到 attrib（模拟 on_enable 注册的处理器）
    mgr2.get("attrib")._handlers.append((MessageEvent, boom))
    await bus.dispatch(MessageEvent(message_type="group", group_id=5, user_id=6, message="x"))
    joined = "\n".join(capturing.records)
    check("日志里带插件名（可按插件过滤）", "[attrib]" in joined, joined[:200])
    check("日志里带错误编号（可按编号 grep）", "E-" in joined, joined[:200])
    timer.stop()

    # ai_chat：错误文案走框架渲染，群里 @ 提问者、私聊不 @
    from plugins.ai_chat.main import AiChatPlugin          # noqa: PLC0415
    from plugins.ai_chat.store import ChatTarget           # noqa: PLC0415
    plugin = AiChatPlugin.__new__(AiChatPlugin)
    plugin.name = "ai_chat"
    plugin.errors = make_reporter(FakeApi())

    group_chain = plugin._error_chain(
        ChatTarget("group", 100000003), 10000001,
        PluginTimeoutError("上游 120s 没响应", plugin="ai_chat"),
        where="AI 对话").build()
    body = text_of(group_chain)
    check("ai_chat 群聊错误文案带编号", "E-" in body, body)
    check("ai_chat 群聊错误文案说明可重试", "稍后再试" in body, body)
    check("ai_chat 群聊错误 @ 了提问者", at_of(group_chain) == ["10000001"],
          str(group_chain))

    private_chain = plugin._error_chain(
        ChatTarget("private", 555), 555,
        PluginRuntimeError("炸了", plugin="ai_chat"), where="AI 对话").build()
    check("ai_chat 私聊错误不 @ 人", at_of(private_chain) == [], str(private_chain))
    check("ai_chat 私聊错误同样带编号", "E-" in text_of(private_chain),
          text_of(private_chain))


async def main():
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    started = time.time()
    test_taxonomy()
    await test_command_failure()
    await test_event_failure()
    await test_timer_and_lifecycle()
    await test_illegal_input()
    await test_plugin_manager_defects()
    await test_attribution_and_ai_chat()
    print(f"\n{'=' * 60}\n通过 {_passed} 项，失败 {_failed} 项（{time.time() - started:.1f}s）")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
