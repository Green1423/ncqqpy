"""公开 API 的导入分类。

契约：

1. `import napcat` **不**连锁加载重子模块（client / aiohttp / websockets 等）；
2. 每个分类从「拥有它」的子模块导入（`napcat.client` / `napcat.plugin` /
   `napcat.models` / `napcat.events` / `napcat.message` / `napcat.command` /
   `napcat.registry` / `napcat.permission` / `napcat.errors` / `napcat.store` …）；
3. 旧写法 `from napcat import X` 仍可用，但会发 `DeprecationWarning` 并指出正确路径；
4. `napcat.<子模块>` 属性访问（`import napcat` 后直接取）按需加载；
5. 框架自己的模块不依赖顶层 re-export（内部一律相对导入）。
"""
from __future__ import annotations

import subprocess
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


CATEGORIES = {
    "napcat.client": ("NapCatClient", "GroupBinding"),
    "napcat.plugin": ("PluginBase", "PluginManager"),
    "napcat.models": ("MessageEvent", "NapCatEvent", "Sender", "GroupMemberRole",
                      "parse_event", "LifeCycleEvent"),
    "napcat.events": ("EventBus", "TimerManager"),
    "napcat.message": ("MessageChain", "MusicPlatform", "ContactType"),
    "napcat.command": ("CommandManager", "CommandSchema", "StrField", "QQField",
                       "normalize_text", "PLACEHOLDER_LEGEND", "CommandPacket"),
    "napcat.registry": ("command", "event", "every", "daily", "delay", "collect"),
    "napcat.permission": ("PermissionManager", "DefaultPermission",
                          "ASSIGNABLE_USER_LEVELS"),
    "napcat.errors": ("PluginError", "PluginTimeoutError", "ErrorReporter",
                      "error_code"),
    "napcat.store": ("Database", "Model", "CharField", "JSONField",
                     "IntegrityError", "StoreError"),
    "napcat.config": ("ConfigManager",),
    "napcat.file_manager": ("FileManager",),
    "napcat.launcher": ("ServerLauncher", "LaunchResult"),
    "napcat.watchdog": ("Watchdog",),
    "napcat.deps": ("check_dependencies",),
}


def test_categorized_imports():
    print("\n[1] 每个分类都能从自己的模块导入")
    import importlib
    for module_name, names in CATEGORIES.items():
        module = importlib.import_module(module_name)
        missing = [n for n in names if not hasattr(module, n)]
        check(f"{module_name}（{len(names)} 个名字）", not missing, str(missing))


def test_lazy_package_init():
    print("\n[2] `import napcat` 不连锁加载重依赖")
    code = (
        "import sys, napcat\n"
        "print(napcat.__version__)\n"
        "print('napcat.client' in sys.modules, 'aiohttp' in sys.modules,"
        " 'websockets' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=60)
    lines = out.stdout.split()
    check("子进程能 import 并拿到版本", out.returncode == 0 and lines and
          lines[0] == "1.0.0", out.stderr[-300:])
    check("顶层没有 eager 加载 client / aiohttp / websockets",
          lines[1:] == ["False", "False", "False"], " ".join(lines[1:]))
    check("__all__ 只暴露版本号", __import__("napcat").__all__ == ["__version__"],
          str(__import__("napcat").__all__))


def test_legacy_compat():
    print("\n[3] 旧写法仍可用，但会告警并指出正确路径")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from napcat import PluginBase                     # noqa: F401
        from napcat import MessageChain                   # noqa: F401
    messages = [str(w.message) for w in caught
                if issubclass(w.category, DeprecationWarning)]
    check("旧写法可用", PluginBase.__name__ == "PluginBase")
    check("发出了 DeprecationWarning", len(messages) >= 2, str(messages))
    check("提示里给出正确模块",
          any("napcat.plugin import PluginBase" in m for m in messages) and
          any("napcat.message import MessageChain" in m for m in messages),
          str(messages))
    check("未知名属性 → AttributeError",
          _raises(lambda: getattr(__import__("napcat"), "NoSuchThing"),
                  AttributeError))
    check("未知名 from-import → ImportError",
          _raises(lambda: exec("from napcat import NoSuchThing", {}), ImportError))


def test_submodule_attribute():
    print("\n[4] `napcat.<子模块>` 属性访问")
    import napcat
    check("napcat.store 可按需加载", napcat.store.Model.__name__ == "Model")
    check("napcat.client 可按需加载",
          napcat.client.NapCatClient.__name__ == "NapCatClient")
    check("未知属性抛 AttributeError",
          _raises(lambda: getattr(napcat, "no_such_module"), AttributeError))


def test_no_flat_self_import():
    print("\n[5] 框架内部不依赖顶层 re-export")
    offenders = []
    for path in sorted((ROOT / "napcat").glob("*.py")):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("from napcat import") or stripped == "import napcat":
                offenders.append(f"{path.name}: {stripped}")
    check("napcat/ 内部没有 flat import", not offenders, "; ".join(offenders[:3]))


def _raises(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except BaseException:
        return False
    return False


def main() -> int:
    test_categorized_imports()
    test_lazy_package_init()
    test_legacy_compat()
    test_submodule_attribute()
    test_no_flat_self_import()
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
