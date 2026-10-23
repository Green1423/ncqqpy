"""命令别名（`/alias`）：增 / 删 / 改 / 查 + 持久化 + 生命周期。

契约：

1. 别名只是 `_commands` 里指向同一个 handler 的另一个键 —— 权限/schema/作用域
   与目标命令完全一致；
2. `/alias` 仅私聊 + 超管；支持 `add`（增）、`del`（删）、`set`（改/整体替换）、
   `move`（改指向）、`list`（查）；
3. 落盘 `config.json` 的 `command_aliases`：**出现即接管**该命令的全部别名
   （空列表 = 清空，连插件 `register(aliases=…)` 声明的也压掉）；
4. 重放点在登记处：插件热重载/重新注册后别名不丢；
5. 命令被注销/移除时，它的别名条目一起清掉。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / ".test-tmp" / "command-alias"

from napcat.command import CommandManager                     # noqa: E402
from napcat.config import ConfigManager                       # noqa: E402
from napcat.models import MessageEvent                        # noqa: E402
from napcat.permission import PermissionManager               # noqa: E402

PASSED = 0
FAILED = 0
SUPER = 999
USER = 10001


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def make(tmp: Path):
    cfg = ConfigManager(str(tmp / "config.json"))
    perm = PermissionManager(superusers=[SUPER])
    cmds = CommandManager(prefixes=["/"], perm_manager=perm, cfg_ref=cfg)
    return cfg, perm, cmds


def register_demo(cmds: CommandManager) -> None:
    @cmds.register("demo", permission="", scope="all",
                   aliases=["d0"], description="演示命令")
    async def _demo(packet):
        return "demo-ok"

    @cmds.register("other", permission="", scope="all", description="另一条命令")
    async def _other(packet):
        return "other-ok"

    @cmds.register("grp.sub", permission="", scope="all", description="子命令")
    async def _sub(packet):
        return "sub-ok"


async def ask(cmds, text: str, *, user_id: int = SUPER, group_id: int | None = None):
    ev = MessageEvent(
        message_type="group" if group_id else "private", user_id=user_id,
        group_id=group_id, message=text)
    result = await cmds.handle(ev)
    if isinstance(result, tuple):
        return result[0]
    return str(result or "")


async def test_lifecycle_and_validation():
    print("\n[1] 增 / 查 / 执行 / 注册期声明的别名")
    TMP.mkdir(parents=True, exist_ok=True)
    for child in TMP.iterdir():
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
    cfg, perm, cmds = make(TMP)
    register_demo(cmds)

    check("注册期声明的别名可用", await ask(cmds, "/d0") == "demo-ok")
    text = await ask(cmds, "/alias list")
    check("列表显示命令与别名", "/demo" in text and "/d0" in text, text[:200])
    text = await ask(cmds, "/alias list demo")
    check("按命令查询", "/d0" in text, text[:200])

    text = await ask(cmds, "/alias add dm demo")
    check("新增别名成功", "已给" in text and "dm" in text, text[:160])
    check("新别名可执行", await ask(cmds, "/dm") == "demo-ok")
    check("落库到 command_aliases",
          cfg.get("command_aliases.demo") == ["d0", "dm"],
          str(cfg.get("command_aliases")))
    text = await ask(cmds, "/alias add dm demo")
    check("重复添加被拒", "已经有别名" in text, text[:160])

    # 校验
    text = await ask(cmds, "/alias add ping demo")
    check("与现有命令名冲突被拒", "占用" in text, text[:160])
    text = await ask(cmds, "/alias add 9bad demo")
    check("非法别名（数字开头）被拒", "非法" in text, text[:160])
    text = await ask(cmds, "/alias add okay nope")
    check("目标不存在被拒", "未知命令" in text, text[:160])
    text = await ask(cmds, "/alias add g grp")
    check("命令组可以起别名", "已给 /grp" in text, text[:160])
    check("组别名裸调 → 命令组帮助页",
          (await ask(cmds, "/g")).startswith("📖 /grp"), (await ask(cmds, "/g"))[:80])
    check("组别名 + 子命令照常路由", await ask(cmds, "/g sub") == "sub-ok")
    check("组别名写进配置", cfg.get("command_aliases.grp") == ["g"],
          str(cfg.get("command_aliases")))
    check("/help 组别名解析到命令组", "演示" in await ask(cmds, "/help g")
          or "grp" in await ask(cmds, "/help g"))
    text = await ask(cmds, "/alias add s grp.sub")
    check("子命令可以起别名", "已给 /grp.sub" in text, text[:160])
    check("子命令别名在组内可执行", await ask(cmds, "/grp s") == "sub-ok")
    check("子命令别名不会变成顶层命令", await ask(cmds, "/s") == "_not_found")
    check("子命令别名写进配置",
          (cfg.get("command_aliases", {}) or {}).get("grp.sub") == ["s"],
          str(cfg.get("command_aliases")))
    check("组帮助里标注子命令别名", "别名：s" in await ask(cmds, "/help grp"))
    check("`/help 父 子别名` 解析到子命令帮助",
          (await ask(cmds, "/help grp s")).startswith("📖 /grp sub"),
          (await ask(cmds, "/help grp s"))[:80])
    text = await ask(cmds, "/alias del grp.s")
    check("点号写法删除子命令别名", "已删除" in text, text[:120])
    check("删除后子命令别名失效（写错子命令 → 回组帮助页）",
          (await ask(cmds, "/grp s")).startswith("📖 /grp"),
          (await ask(cmds, "/grp s"))[:60])
    text = await ask(cmds, "/alias add a" + "x" * 70 + " demo")
    check("别名过长被拒", "过长" in text, text[:160])


async def test_delete_set_move():
    print("\n[2] 删 / 整体替换 / 改指向")
    TMP2 = TMP / "edit"
    TMP2.mkdir(parents=True, exist_ok=True)
    cfg, perm, cmds = make(TMP2)
    register_demo(cmds)

    text = await ask(cmds, "/alias del d0")
    check("删除注册期声明的别名", "已删除" in text, text[:160])
    check("删除后不再可用", await ask(cmds, "/d0") == "_not_found", await ask(cmds, "/d0"))

    text = await ask(cmds, "/alias set demo d1 d2")
    check("整体替换成功", "/d1" in text and "/d2" in text, text[:160])
    check("替换后旧别名失效、新别名可用",
          await ask(cmds, "/d0") == "_not_found" and await ask(cmds, "/d2") == "demo-ok")
    text = await ask(cmds, "/alias set demo")
    check("清空成功", "已清空" in text, text[:160])
    check("清空后别名全失效", await ask(cmds, "/d2") == "_not_found")

    await ask(cmds, "/alias add old other")
    check("move 前别名指向 other", await ask(cmds, "/old") == "other-ok")
    text = await ask(cmds, "/alias move old demo")
    check("move 改指向成功", "改指向" in text, text[:160])
    check("别名现在执行新目标", await ask(cmds, "/old") == "demo-ok")
    await ask(cmds, "/alias move old other")
    text = await ask(cmds, "/alias move old ping")
    check("move 可以再改到另一条命令", await ask(cmds, "/old") == "pong!", text[:160])
    text = await ask(cmds, "/alias move nope demo")
    check("move 不存在的别名被拒", "没有这个别名" in text, text[:160])


async def test_persistence_and_reregister():
    print("\n[3] 持久化 + 重新登记（模拟插件热重载）")
    TMP3 = TMP / "persist"
    TMP3.mkdir(parents=True, exist_ok=True)
    cfg_path = TMP3 / "config.json"
    cfg = ConfigManager(str(cfg_path))
    perm = PermissionManager(superusers=[SUPER])
    cmds = CommandManager(prefixes=["/"], perm_manager=perm, cfg_ref=cfg)
    register_demo(cmds)
    await ask(cmds, "/alias set demo d1")
    await ask(cmds, "/alias add d2 demo")
    check("两次操作后的配置",
          cfg.get("command_aliases.demo") == ["d1", "d2"],
          str(cfg.get("command_aliases")))

    # 同一管理器里「注销 + 重新注册」= 插件热重载的命令侧
    cmds.unregister("demo")
    check("注销命令连带删掉别名条目",
          cmds.list_commands().get("d1") is None, str(list(cmds.list_commands())))

    # 命令组别名：注销（连带清别名条目）→ 重新注册 → 配置重放
    await ask(cmds, "/alias add g grp")
    cmds.unregister("grp")
    check("注销命令组连带删掉组别名", "g" not in cmds.list_commands(),
          str(list(cmds.list_commands())))
    register_demo(cmds)
    check("重新注册后组别名恢复", (await ask(cmds, "/g")).startswith("📖 /grp"))
    check("恢复后的组别名可路由子命令", await ask(cmds, "/g sub") == "sub-ok")
    text = await ask(cmds, "/alias del g")
    check("删除组别名", "已删除" in text, text[:120])
    check("删除后组别名失效", await ask(cmds, "/g") == "_not_found")
    register_demo(cmds)
    check("重新注册后别名自动恢复（配置接管）",
          await ask(cmds, "/d1") == "demo-ok" and await ask(cmds, "/d2") == "demo-ok")

    # 新管理器（模拟重启）从 config.json 恢复
    cmds2 = CommandManager(prefixes=["/"], perm_manager=PermissionManager(superusers=[SUPER]),
                           cfg_ref=ConfigManager(str(cfg_path)))
    # grp: [] 是「组别名已清空」的显式记录（配置接管该命令的别名全集）
    check("重启后别名表已载入",
          cmds2.alias_overrides() == {"demo": ["d1", "d2"], "grp": []},
          str(cmds2.alias_overrides()))
    register_demo(cmds2)
    check("重启后别名可执行", await ask(cmds2, "/d1") == "demo-ok")
    check("注册期声明值被配置接管（d0 不生效）",
          await ask(cmds2, "/d0") == "_not_found")

    # 未注册命令的别名：保留并提示（直接摘掉命令，模拟插件卸载）
    await ask(cmds2, "/alias add px ping")
    removed = cmds2._commands.pop("ping", None)
    check("人为移除目标命令后别名仍在表里", removed is not None)
    text = await ask(cmds2, "/alias list")
    check("列表提示未注册命令的残留别名", "未注册" in text, text[:300])


async def test_permissions_and_stop():
    print("\n[3.1] 子命令别名：组内作用域 / 歧义 / 跨命名空间 / 重启恢复")
    TMP5 = TMP / "subalias"
    TMP5.mkdir(parents=True, exist_ok=True)
    cfg5, _perm5, cmds5 = make(TMP5)
    register_demo(cmds5)

    @cmds5.register("grp2.sub2", permission="", scope="all", description="另一组")
    async def _sub2(packet):
        return "sub2-ok"

    text = await ask(cmds5, "/alias add s grp.sub")
    check("子命令别名可加", "已给 /grp.sub" in text, text[:160])
    check("组内调用生效", await ask(cmds5, "/grp s") == "sub-ok")
    check("顶层不新增命令", await ask(cmds5, "/s") == "_not_found")
    text = await ask(cmds5, "/alias add other grp.sub")
    check("组内别名可与顶层命令重名（作用域隔离）",
          "已给" in text, text[:160])

    # 同组冲突：别名撞子命令名 / 撞兄弟别名
    text = await ask(cmds5, "/alias add sub grp.sub")
    check("别名不能等于子命令名", "子命令名相同" in text, text[:120])
    text = await ask(cmds5, "/alias add s grp.sub")
    check("重复添加子命令别名被拒", "已经有别名" in text, text[:120])

    # 跨组同名 → 必须用点号写法消歧
    await ask(cmds5, "/alias add s grp2.sub2")
    text = await ask(cmds5, "/alias del s")
    check("跨组同名删除要求消歧",
          "多个命令组" in text and "grp.s" in text and "grp2.s" in text, text[:200])
    text = await ask(cmds5, "/alias del grp.s")
    check("点号写法消歧删除", "已删除" in text, text[:120])
    check("另一个组的同名别名仍在", await ask(cmds5, "/grp2 s") == "sub2-ok")

    # set / move：跨命名空间
    text = await ask(cmds5, "/alias set grp.sub p q")
    check("整体替换子命令别名", "/p" in text and "/q" in text, text[:160])
    check("新别名生效", await ask(cmds5, "/grp q") == "sub-ok")
    text = await ask(cmds5, "/alias move grp.q other")
    check("子命令别名可移到顶层命令", "改指向 /other" in text, text[:160])
    check("移动后顶层可用", await ask(cmds5, "/q") == "other-ok")
    text = await ask(cmds5, "/alias move q grp.sub")
    check("顶层别名可移回子命令", "改指向 /grp.sub" in text, text[:160])
    check("移回后只有组内可用",
          await ask(cmds5, "/q") == "_not_found"
          and await ask(cmds5, "/grp q") == "sub-ok")

    # 重启恢复：新管理器 + 重新注册
    cmds6 = CommandManager(prefixes=["/"],
                           perm_manager=PermissionManager(superusers=[SUPER]),
                           cfg_ref=ConfigManager(str(TMP5 / "config.json")))
    register_demo(cmds6)
    cmds6.register("grp2.sub2", permission="", scope="all")(lambda packet: "sub2-ok")
    check("重启后子命令别名恢复", await ask(cmds6, "/grp q") == "sub-ok")
    check("重启后另一组的别名恢复", await ask(cmds6, "/grp2 s") == "sub2-ok")

    print("\n[4] 权限边界 / /stop 清理 / 帮助展示")
    TMP4 = TMP / "perm"
    TMP4.mkdir(parents=True, exist_ok=True)
    cfg, perm, cmds = make(TMP4)
    register_demo(cmds)

    denied = await ask(cmds, "/alias add x demo", user_id=USER)
    check("非超管 → 当作不存在", denied == "_not_found", denied)
    denied = await ask(cmds, "/alias add x demo", user_id=SUPER, group_id=123)
    check("群聊 → 当作不存在（仅私聊）", denied == "_not_found", denied)

    await ask(cmds, "/alias add dd demo")
    check("先确认别名可用", await ask(cmds, "/dd") == "demo-ok")
    text = await ask(cmds, "/stop command demo")
    check("/stop 移除命令", "已移除" in text, text[:120])
    check("/stop 连带清掉别名", await ask(cmds, "/dd") == "_not_found")
    check("别的命令不受影响", await ask(cmds, "/ping") == "pong!")

    register_demo(cmds)
    text = await ask(cmds, "/help demo")
    check("帮助里展示别名", "别名" in text and "d0" in text, text[:300])


async def main() -> int:
    await test_lifecycle_and_validation()
    await test_delete_set_move()
    await test_persistence_and_reregister()
    await test_permissions_and_stop()
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
