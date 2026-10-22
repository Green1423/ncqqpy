"""`CommandParser` 行为基线。

存在的原因：`ai_chat` 的「结束符」（默认 `//`）与命令前缀 `/` 天然冲突 ——
旧实现把**纯前缀**当成命令名，于是 `//` 被解析成「名为 `/` 的命令」，用户每次
提交都会收到一句「未知命令: //」。修这个问题动了 `CommandParser`，所以这里
把解析口径全部钉住，避免以后调前缀逻辑时把别的写法弄坏。

跑法：
    cd /ncqqpy && ./venv-arm/bin/python -B tests/test_command_parser.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP_DIR = ROOT / "tests" / ".test-tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)   # 新 clone 里没有它（.gitignore 排除）

from napcat.command import CommandManager      # noqa: E402
from napcat.config import ConfigManager        # noqa: E402

PASSED = 0
FAILED = 0


def _plugin_ready(name: str) -> bool:
    """框架仓库单独 checkout 时 `plugins/` 是空的（插件在各自的仓库里）→ 跳过该节。"""
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
        print(f"  ❌ {name} — {detail}")


# (输入, 期望 command 或 None=不是命令, 期望位置参数, 期望 illegal)
CASES = [
    ("/ping", "ping", [], False),
    ("  /ping  ", "ping", [], False),          # 首尾空白要归一化
    ("。ping", "ping", [], False),             # 多前缀
    ("#ping", "ping", [], False),
    ("/PING", "ping", [], False),              # 命令名小写化
    ("/aichat switch s1", "aichat", ["s1"], False),
    ("/ban 123 600", "ban", ["123", "600"], False),
    ("/ping --k=v", "ping", [], False),        # 键值传参
    ("/ping -f", "ping", [], False),           # 短开关
    ("/ping 'a b'", "ping", ["a b"], False),   # 引号内空格
    ("/ping don\\'t", "ping", ["don't"], False),  # 单引号内转义
    # CJK 引号标准化。⚠️ 单个「像名字」的 token（含中文）会先被当作**子命令候选**
    # （命令名/子命令名现在支持非 ASCII），命令没有子命令时执行阶段会回落成参数；
    # 这里用带空格的引号串，验证的是引号归一化，不牵扯子命令判定。
    ("/ping “引 号”", "ping", ["引 号"], False),
    # ---- 本次修的关键行为 ----
    ("/", None, [], False),                    # 纯前缀不是命令
    ("。", None, [], False),
    ("//", "/", [], True),                     # 结束符：非法（静默丢弃）而非「未知命令」
    ("// ", "/", [], True),
    ("/ /", "/", [], True),
    # ---- 非命令 ----
    ("hello", None, [], False),
    ("！其实全角", None, [], False),             # 全角感叹号不是 ! 前缀
]


def test_parser():
    print("\n[1] 命令解析口径")
    cm = CommandManager(prefixes=["/", "!", "。", "#"])
    cm._commands.update({"aichat": None, "end": None, "ping": None, "ban": None})
    for text, want_cmd, want_args, want_illegal in CASES:
        p = cm.parser.parse(text)
        if want_cmd is None:
            ok, got = p is None, (None if p is None else p.command)
        else:
            ok = (p is not None and p.command == want_cmd and p.args == want_args
                  and p.illegal == want_illegal)
            got = None if p is None else (p.command, p.args, p.illegal)
        check(f"parse({text!r})", ok, f"期望 {want_cmd}/{want_args}/{want_illegal}，得到 {got}")

    print("\n[2] 前缀切分助手")
    # 子命令提取仍在（第二个 token 是标识符）
    p = cm.parser.parse("/aichat new 学习计划")
    check("第二个 token 仍按子命令解析",
          p is not None and p.subcommand == "new" and p.args == ["学习计划"],
          f"{p}")
    check("skip_prefix 剥掉前缀与空白",
          cm.parser.skip_prefix("  /aichat new  ") == "aichat new",
          repr(cm.parser.skip_prefix("  /aichat new  ")))
    check("skip_prefix 对非命令返回 None",
          cm.parser.skip_prefix("hello") is None, "应返回 None")
    # `//` 剥掉一个 `/` 后还剩 `/`（不是空白）——所以它是「内容为分隔符」，
    # 由 parse() 用命令名合法性判成 illegal；`/` 才是真正的纯前缀。
    check("skip_prefix 对纯前缀 `/` 返回 None",
          cm.parser.skip_prefix("/") is None, f"{cm.parser.skip_prefix('/')}")
    check("strip_prefix 对非命令返回 None",
          cm.parser.strip_prefix("hello") is None, "应返回 None")
    check("strip_prefix 对纯前缀 `/` 返回 None",
          cm.parser.strip_prefix("/") is None, f"{cm.parser.strip_prefix('/')}")
    check("`//` 剩余内容是分隔符（交由 parse 判 illegal）",
          cm.parser.strip_prefix("//") == ("/", "/"),
          f"{cm.parser.strip_prefix('//')}")
    check("strip_prefix 容忍首尾空白",
          cm.parser.strip_prefix("  /ping  ") == ("/", "ping  "),
          f"{cm.parser.strip_prefix('  /ping  ')}")


def test_plugin_config_all():
    """`PluginConfig.all()` —— 插件读整段配置的唯一正确入口。"""
    print("\n[3] PluginConfig.all() 读整段插件配置")
    path = ROOT / "tests" / ".test-tmp" / "_cfg_all_probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = ConfigManager(str(path))
    cfg.set("plugins.demo", {"llm": {"model": "m1"}, "n": 3})
    pc = cfg.plugin("demo")
    got = pc.all()
    check("all() 返回整个命名空间", got == {"llm": {"model": "m1"}, "n": 3}, f"{got}")
    check("all() 是副本（改它不写回）",
          (got.__setitem__("n", 99), pc.get("n"))[1] == 3, f"{pc.get('n')}")
    check("get('') 确实是坑（返回 None）", pc.get("") is None, f"{pc.get('')}")
    check("空命名空间返回 {}", cfg.plugin("nope").all() == {}, f"{cfg.plugin('nope').all()}")
    try:
        path.unlink()
    except OSError:
        pass


def test_config_reload():
    """`ConfigManager.reload()` —— 「手工改文件后如何生效」的唯一入口。"""
    print("\n[4] ConfigManager.reload()")
    path = ROOT / "tests" / ".test-tmp" / "_cfg_reload_probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"a": 1, "plugins": {"x": {"v": "旧"}}}),
                    encoding="utf-8")
    cfg = ConfigManager(str(path))
    check("初始读文件", cfg.get("a") == 1, f"{cfg.get('a')}")

    # 手工改文件：内存快照**不会**自己变（这正是需要 reload 的原因）
    path.write_text(json.dumps({"a": 2, "plugins": {"x": {"v": "新"}}}),
                    encoding="utf-8")
    check("构造后不自动跟随文件", cfg.get("a") == 1, f"{cfg.get('a')}")

    cfg.reload()
    check("reload 后读到新值", cfg.get("a") == 2, f"{cfg.get('a')}")
    check("reload 覆盖插件命名空间", cfg.plugin("x").get("v") == "新",
          f"{cfg.plugin('x').get('v')}")

    # 文件坏掉时保留内存里的值，不能清空
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    cfg.reload()
    check("文件损坏时保留旧值（不清空）", cfg.get("a") == 2, f"{cfg.get('a')}")
    try:
        path.unlink()
    except OSError:
        pass


async def test_plugin_reload_no_duplicate_handlers():
    """回归：热重载（尤其「新版本加载失败→回滚」）不能让事件监听器翻倍。

    真实故障：`/events` 里同一个插件出现**两对**监听器
    （`ai_chat._on_any_message` 两条、`ai_chat._on_recall` 两条），
    群里每条消息都被处理两次。

    成因：`reload()` 在「新版本加载失败」时会回滚到**旧实例**并再次
    `on_enable()`，而旧实例的处理器刚被 `on_disable()` 摘掉又注册一遍；
    `EventBus` 不按 `hid` 去重，于是累积。每失败一次就多一份。
    """
    import collections
    import shutil
    import tempfile

    from napcat.plugin import PluginManager
    from napcat.events import EventBus, TimerManager
    from napcat.command import CommandManager
    from napcat.permission import PermissionManager
    from napcat.file_manager import FileManager
    from napcat.config import ConfigManager
    from napcat.client import GroupBinding

    if not _plugin_ready("ai_chat"):
        return
    print("\n[5] 热重载不产生重复监听器")
    tmp = Path(tempfile.mkdtemp(prefix="dup-",
                                dir=str(ROOT / "tests" / ".test-tmp")))
    pdir = tmp / "plugins"
    shutil.copytree(ROOT / "plugins" / "ai_chat", pdir / "ai_chat",
                    ignore=shutil.ignore_patterns("__pycache__", "data"))
    cfg = ConfigManager(str(tmp / "config.json"))
    cfg.set("plugins.ai_chat.data_dir", str(tmp / "data"))
    cfg.set("plugins.ai_chat.binding",
            {"groups": [], "users": [], "empty_means": "allow"})
    bus = EventBus()
    timer = TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[1])
    mgr = PluginManager(plugin_dir=str(pdir), api=None, event_bus=bus, timer=timer,
                        cmd_manager=CommandManager(perm_manager=perm),
                        perm_manager=perm, file_manager=FileManager(None), cfg=cfg,
                        groups=GroupBinding(bound=set(), all_groups=True))

    def dup_map():
        c = collections.Counter(m.id for m in bus.list_all())
        return {k: v for k, v in c.items() if v > 1}

    try:
        await mgr.load_all()
        await mgr.enable_all()
        check("启用后没有重复监听器", not dup_map(), f"{dup_map()}")
        baseline = len(bus.list_all())

        # 制造「新版本加载失败」：写坏源码 → reload 走回滚分支
        main_py = pdir / "ai_chat" / "main.py"
        good = main_py.read_text(encoding="utf-8")
        main_py.write_text("这是语法错误 (((\n", encoding="utf-8")
        for i in range(3):
            await mgr.reload("ai_chat")
            check(f"第 {i + 1} 次回滚后仍无重复", not dup_map(), f"{dup_map()}")
        check("回滚不改变监听器总数", len(bus.list_all()) == baseline,
              f"{len(bus.list_all())} != {baseline}")

        # 修好源码 → 正常重载，也不该留下旧实例的监听器
        main_py.write_text(good, encoding="utf-8")
        ok = await mgr.reload("ai_chat")
        check("修好后能正常重载", ok, "reload 返回 False")
        check("正常重载后没有重复监听器", not dup_map(), f"{dup_map()}")
        check("正常重载后监听器总数不变", len(bus.list_all()) == baseline,
              f"{len(bus.list_all())} != {baseline}")

        # on_disable 可重复调用（幂等）：第二次不该把注册好的又摘掉
        inst = mgr.plugins.get("ai_chat")
        await inst.on_disable()
        after_first = len(bus.list_all())
        await inst.on_disable()
        check("on_disable 幂等", len(bus.list_all()) == after_first,
              f"{len(bus.list_all())} != {after_first}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import asyncio

    test_parser()
    test_plugin_config_all()
    test_config_reload()
    asyncio.run(test_plugin_reload_no_duplicate_handlers())
    print(f"\n{'=' * 46}\n通过 {PASSED} / 失败 {FAILED}")
    sys.exit(1 if FAILED else 0)
