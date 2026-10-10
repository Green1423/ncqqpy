"""插件配置模板契约：首次运行即初始化全部配置项。

覆盖三层：

1. `PluginConfig.fill_defaults()` 的语义 —— 递归补缺 / 不覆盖用户值 / 类型冲突只报告 /
   幂等（没缺键不写盘）/ 单次写盘 / 模板深拷贝隔离 / 空模板不动作；
2. 框架契约 —— `PluginBase.config_template()` 在 `on_load()` **之前**补齐、
   每次加载都补（升级新增键也补）、补前按磁盘重读（不覆盖手改内容）、
   模板写错不让插件起不来；
3. 真实插件验收 —— `ai_chat` / `bupt_news` / `byrdocs` / `essential` 首次加载后，
   `config.json` 里该有的配置项一个不缺，且运维改过的值一个没动。
"""
from __future__ import annotations

import asyncio
import json
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
    CommandManager, EventBus, FileManager, GroupBinding, PermissionManager,
    PluginManager, TimerManager, ErrorReporter,
)
from napcat.config import ConfigManager                           # noqa: E402

TMP = ROOT / "tests" / ".test-tmp" / "plugin-config"

_passed = 0
_failed = 0


def _plugin_ready(name: str) -> bool:
    """框架仓库单独 checkout 时 `plugins/` 是空的 → 真实插件验收整节跳过。"""
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
    logger = logging.getLogger("test.plugin-config")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    return logger


def flat_paths(data, prefix: str = "") -> list[str]:
    """展开成叶子路径（空 dict 也算一项，用于「结构可见」的断言）。"""
    out: list[str] = []
    for key, value in (data or {}).items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.extend(flat_paths(value, prefix=f"{path}."))
        else:
            out.append(path)
    return out


def read_section(path: Path, name: str) -> dict:
    return (json.loads(path.read_text(encoding="utf-8")).get("plugins") or {}).get(name) or {}


def make_mgr(plugin_dir: Path, cfg: ConfigManager, *, bus=None, timer=None, cmds=None,
             reporter=None):
    bus = bus or EventBus()
    timer = timer or TimerManager()
    timer.start()
    perm = PermissionManager(superusers=[999])
    cmds = cmds or CommandManager(prefixes=["/"], perm_manager=perm)
    mgr = PluginManager(str(plugin_dir), api=None, event_bus=bus, timer=timer,
                        cmd_manager=cmds, perm_manager=perm,
                        file_manager=FileManager(api=None), logger=_logger(), cfg=cfg,
                        groups=GroupBinding(bound=set(), all_groups=True),
                        error_reporter=reporter)
    cmds._plugins_ref = mgr
    return mgr, bus, timer, cmds


def write_plugin(pdir: Path, name: str, body: str = "", template: str = "",
                 init: str | None = None) -> Path:
    pkg = pdir / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(init if init is not None else (
        "from napcat import PluginBase\n"
        f"class {name.capitalize()}Plugin(PluginBase):\n"
        f"    name = {name!r}\n"
        "    version = '1.0.0'\n" + template + body), encoding="utf-8")
    return pkg


# ==================== 1. fill_defaults 语义 ====================


def test_fill_defaults():
    print("\n[1] fill_defaults：递归补缺 / 不覆盖 / 冲突报告 / 幂等")
    work = TMP / "unit"
    work.mkdir(parents=True, exist_ok=True)
    path = work / "config.json"
    path.write_text(json.dumps({
        "ws_url": "ws://127.0.0.1:1",
        "plugins": {"demo": {"a": 1, "nested": {"keep": True}, "scalar": "not-a-dict"}},
    }, ensure_ascii=False), encoding="utf-8")

    cfg = ConfigManager(str(path))
    pc = cfg.plugin("demo")
    template = {
        "a": 99,                                    # 已存在 → 不动
        "nested": {"keep": False, "fresh": 2},      # 父键在、子键缺 → 逐键补
        "scalar": {"want": "dict"},                 # 类型不符 → 保留用户值
        "new": {"deep": {"leaf": [1, 2]}},          # 整段缺 → 补
    }
    report = pc.fill_defaults(template)
    check("补上了缺的键路径（整段缺记段路径、缺子键记子键路径）",
          sorted(report.filled) == ["nested.fresh", "new"], str(report.filled))
    check("类型不符记进 conflicts", report.conflicts == ["scalar"], str(report.conflicts))
    check("changed 为真", report.changed is True)

    section = read_section(path, "demo")
    check("已有值不覆盖", section["a"] == 1 and section["nested"]["keep"] is True,
          json.dumps(section, ensure_ascii=False))
    check("嵌套子键被逐键补上", section["nested"].get("fresh") == 2)
    check("缺的整段被补上", section["new"]["deep"]["leaf"] == [1, 2])
    check("类型不符时保留用户值", section["scalar"] == "not-a-dict")

    again = pc.fill_defaults(template)
    check("幂等：再补一次什么都不缺", again.filled == [] and again.changed is False,
          str(again.filled))

    # 模板深拷贝：写出去的不能与模板共享对象
    template["new"]["deep"]["leaf"].append(3)
    check("模板与写入值不共享对象", read_section(path, "demo")["new"]["deep"]["leaf"] == [1, 2],
          str(read_section(path, "demo")["new"]["deep"]["leaf"]))

    check("空模板不动作", pc.fill_defaults({}).filled == []
          and pc.fill_defaults({}).changed is False)
    check("None 模板不动作", pc.fill_defaults(None).filled == [])

    # 单次写盘：补 30 个键也只写一次文件
    class CountingConfig(ConfigManager):
        writes = 0

        def _save_locked(self):
            type(self).writes += 1
            super()._save_locked()

    path2 = work / "config2.json"
    path2.write_text("{}", encoding="utf-8")
    cfg2 = CountingConfig(str(path2))
    writes_before = CountingConfig.writes
    cfg2.plugin("demo").fill_defaults({f"k{i}": i for i in range(30)})
    check("补 30 个键只写盘一次", CountingConfig.writes - writes_before == 1,
          str(CountingConfig.writes - writes_before))

    # 用户把整段删了 → 下次加载整段补回（「首次运行即完整」在升级路径上同样成立）
    cfg2.set("plugins.demo", {})
    report3 = cfg2.plugin("demo").fill_defaults({"k0": 0, "sub": {"x": 1}})
    check("整段被删后能补回", sorted(report3.filled) == ["k0", "sub"], str(report3.filled))
    check("补回的整段含子键", cfg2.plugin("demo").all()["sub"] == {"x": 1},
          json.dumps(cfg2.plugin("demo").all(), ensure_ascii=False))


# ==================== 2. 框架契约 ====================


async def test_framework_contract():
    print("\n[2] 框架契约：on_load 之前补齐 / 每次加载都补 / 不覆盖手改 / 模板报错不致命")
    work = TMP / "framework"
    pdir = work / "plugins"
    cfg_path = work / "config.json"
    work.mkdir(parents=True, exist_ok=True)
    cfg = ConfigManager(str(cfg_path))

    # (a) on_load 里就能看到补齐后的配置 + 默认模板为空时不写段
    write_plugin(pdir, "seeded", template=(
        "    def config_template(self):\n"
        "        return {'greeting': 'hi', 'nested': {'a': 1, 'b': 2}}\n"
        "    async def on_load(self):\n"
        "        self.seen = (self.config.get('greeting'),\n"
        "                     self.config.get('nested.b'))\n"))
    write_plugin(pdir, "silent")
    mgr, bus, timer, cmds = make_mgr(pdir, cfg)
    await mgr.load_all()
    plugin = mgr.get("seeded")
    check("on_load 时配置已补齐", getattr(plugin, "seen", None) == ("hi", 2),
          str(getattr(plugin, "seen", None)))
    section = read_section(cfg_path, "seeded")
    check("嵌套段完整写入", section.get("nested") == {"a": 1, "b": 2},
          json.dumps(section, ensure_ascii=False))
    check("没有模板的插件不写空段", "silent" not in
          (json.loads(cfg_path.read_text(encoding="utf-8")).get("plugins") or {}),
          str(sorted((json.loads(cfg_path.read_text(encoding="utf-8")).get("plugins") or {}))))

    # (b) 升级路径：模板新增键 → 下次加载补上；用户值不动
    cfg.set("plugins.seeded.greeting", "用户改过的")
    before = cfg_path.read_text(encoding="utf-8")
    write_plugin(pdir, "seeded", template=(
        "    def config_template(self):\n"
        "        return {'greeting': 'hi', 'nested': {'a': 1, 'b': 2, 'added_later': 3}}\n"
        "    async def on_load(self):\n"
        "        self.seen = (self.config.get('greeting'),\n"
        "                     self.config.get('nested.added_later'))\n"))
    await mgr.reload("seeded")
    section = read_section(cfg_path, "seeded")
    check("升级新增的键被补上", section["nested"].get("added_later") == 3,
          json.dumps(section["nested"], ensure_ascii=False))
    check("用户改过的值没被动", section["greeting"] == "用户改过的", str(section["greeting"]))
    check("插件看到的是新键", mgr.get("seeded").seen == ("用户改过的", 3),
          str(mgr.get("seeded").seen))

    # (c) 手工编辑 config.json 后热重载：手改内容不能被启动快照覆盖
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    on_disk["plugins"]["seeded"]["hand_edited"] = "keep-me"
    cfg_path.write_text(json.dumps(on_disk, ensure_ascii=False, indent=2), encoding="utf-8")
    await mgr.reload("seeded")
    check("手工加进文件的键在补配置后仍在",
          read_section(cfg_path, "seeded").get("hand_edited") == "keep-me",
          json.dumps(read_section(cfg_path, "seeded"), ensure_ascii=False)[:200])

    # (d) 模板写错不让插件起不来（报给超管，插件照常可用）
    api = _FakeOps()
    reporter = ErrorReporter(logger=_logger(),
                             perm=PermissionManager(superusers=[999]),
                             send_private=api.send_private)
    write_plugin(pdir, "badtpl", template=(
        "    def config_template(self):\n"
        "        raise RuntimeError('模板写错了')\n"))
    mgr2, bus2, timer2, cmds2 = make_mgr(pdir, cfg, reporter=reporter)
    check("模板报错但插件仍然加载成功", await mgr2.load("badtpl") is not None)
    check("模板报错已通知超管", bool(api.private), str(api.private))
    timer.stop()
    timer2.stop()

    # (e) 幂等：没有缺键时二次加载不改写文件
    write_plugin(pdir, "stable", template=(
        "    def config_template(self):\n"
        "        return {'x': 1}\n"))
    mgr3, bus3, timer3, cmds3 = make_mgr(pdir, cfg)
    await mgr3.load("stable")
    snapshot = cfg_path.read_text(encoding="utf-8")
    await mgr3.reload("stable")
    check("无缺键时二次加载不改文件", cfg_path.read_text(encoding="utf-8") == snapshot)
    timer3.stop()

    # (f) 直接构造（不经管理器）同样初始化配置：测试/脚本手动 new 的路径
    from napcat import PluginBase                                # noqa: PLC0415

    class DirectPlugin(PluginBase):
        name = "direct"

        def config_template(self):
            return {"x": 1, "nested": {"y": 2}}

    inst = DirectPlugin(api=None, event_bus=EventBus(), timer=TimerManager(),
                        cmd_manager=None, perm_manager=None, file_manager=None,
                        logger=_logger(), cfg=cfg)
    check("直接构造也把配置写进 config.json",
          read_section(cfg_path, "direct") == {"x": 1, "nested": {"y": 2}},
          json.dumps(read_section(cfg_path, "direct"), ensure_ascii=False))
    check("构造期结果留在 config_seed 里", inst.config_seed.filled != []
          and inst.config_seed_error is None, str(inst.config_seed))

    class BrokenTemplate(PluginBase):
        name = "broken"

        def config_template(self):
            raise RuntimeError("模板炸了")

    broken = BrokenTemplate(api=None, event_bus=EventBus(), timer=TimerManager(),
                            cmd_manager=None, perm_manager=None, file_manager=None,
                            logger=_logger(), cfg=cfg)
    check("模板报错不抛异常（插件仍可用）", broken is not None)
    check("失败原因留在 config_seed_error",
          isinstance(broken.config_seed_error, RuntimeError), str(broken.config_seed_error))
    check("模板报错的插件不写配置段",
          not read_section(cfg_path, "broken"), json.dumps(read_section(cfg_path, "broken")))


class _FakeOps:
    def __init__(self):
        self.private: list[tuple[str, str]] = []

    async def send_private(self, user_id, text):
        self.private.append((str(user_id), text))


# ==================== 3. 真实插件验收 ====================


async def test_real_plugins():
    print("\n[3] 真实插件：首次加载后配置项一个不缺，用户值一个不动")
    work = TMP / "real"
    pdir = work / "plugins"
    if work.exists():
        shutil.rmtree(work)
    pdir.mkdir(parents=True)
    # 动态枚举 plugins/ 下**所有**插件（含将来新增的）：契约对每个插件都成立，
    # 不是只对今天这四个名字成立。没有 __init__.py 的目录不是插件（如
    # ncqq_watchdog/ 只剩数据），跳过。
    if not _plugin_ready("ai_chat"):
        return
    names = sorted(p.name for p in (ROOT / "plugins").iterdir()
                   if p.is_dir() and (p / "__init__.py").exists())
    check("发现仓库里的插件", len(names) >= 4, str(names))
    for name in names:
        shutil.copytree(ROOT / "plugins" / name, pdir / name,
                        ignore=shutil.ignore_patterns("__pycache__", "data"))

    cfg_path = work / "config.json"
    cfg = ConfigManager(str(cfg_path))
    # 预置几处「用户改过的值」与一个残缺的嵌套段
    cfg.set("plugins.bupt_news.interval", 999)
    cfg.set("plugins.bupt_news.weather", {"label": "上海"})
    cfg.set("plugins.ai_chat.data_dir", str(work / "ai_chat_data"))
    cfg.set("plugins.bupt_news.data_dir", str(work / "news_data"))
    cfg.set("plugins.byrdocs.data_dir", str(work / "byrdocs_data"))

    mgr, bus, timer, cmds = make_mgr(pdir, cfg)
    await mgr.load_all()
    await mgr.enable_all()

    for name in names:
        plugin = mgr.get(name)
        if not check(f"{name} 已加载", plugin is not None):
            continue
        template = plugin.config_template()
        section = read_section(cfg_path, name)
        missing = sorted(set(flat_paths(template)) - set(flat_paths(section)))
        check(f"{name}：模板 {len(flat_paths(template))} 项全部写进 config.json",
              not missing, f"缺 {missing[:6]}")

    saved = json.loads(cfg_path.read_text(encoding="utf-8"))["plugins"]
    check("用户改过的标量值没动", saved["bupt_news"]["interval"] == 999,
          str(saved["bupt_news"]["interval"]))
    check("用户写过的嵌套段没动，缺的子键补上",
          saved["bupt_news"]["weather"]["label"] == "上海"
          and {"enabled", "latitude", "longitude", "timeout", "umbrella_pop"}
          <= set(saved["bupt_news"]["weather"]),
          json.dumps(saved["bupt_news"]["weather"], ensure_ascii=False))
    check("essential 的按群配置容器已初始化", saved["essential"].get("groups") == {},
          json.dumps(saved["essential"], ensure_ascii=False))
    check("ai_chat 的模板与文件逐叶子一致",
          not (set(flat_paths(mgr.get("ai_chat").config_template()))
               - set(flat_paths(saved["ai_chat"]))))

    # 二次加载（含热重载）不改写文件
    snapshot = cfg_path.read_text(encoding="utf-8")
    await mgr.reload_all()
    check("二次加载不改写 config.json（幂等）",
          cfg_path.read_text(encoding="utf-8") == snapshot)
    await mgr.shutdown()
    timer.stop()


async def main():
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True, exist_ok=True)
    started = time.time()
    test_fill_defaults()
    await test_framework_contract()
    await test_real_plugins()
    print(f"\n{'=' * 60}\n通过 {_passed} 项，失败 {_failed} 项（{time.time() - started:.1f}s）")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
