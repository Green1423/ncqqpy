"""运行时权限覆盖（`/perm` + `command_permissions`）行为基线。

存在的原因：框架第一次允许**运行时改写**命令权限，并把改动写进 config.json、
在插件热重载后重新套用。这里钉住四件事：
- 覆盖值 > 代码声明值，且重放点只有命令登记处（模拟 /reload 重新注册）；
- 取值白名单：未注册节点当场拒绝；配置里的脏条目只警告、保留、可清；
- 命令组本体拒绝覆盖（本体不校验权限，设了也不生效）；
- /perm 自身是 superuser + 私聊；非超级用户在群里/私聊都看不到。

跑法：
    cd /ncqqpy && ./venv-arm/bin/python -B tests/test_command_permissions.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP_DIR = ROOT / "tests" / ".test-tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)   # 新 clone 里没有它（.gitignore 排除）

from napcat.command import CommandManager          # noqa: E402
from napcat.config import ConfigManager            # noqa: E402
from napcat.models import MessageEvent             # noqa: E402
from napcat.permission import PermissionManager    # noqa: E402

TMP = ROOT / "tests" / ".test-tmp" / "command-permissions"
SUPERUSER = 10000001
GROUP_ADMIN = 20001
GROUP_MEMBER = 30001
GROUP = 1000000001

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name} — {detail}")


def build(cfg_path: Path):
    """新建一套 CommandManager —— 等价于「进程重启后重新读配置 + 重新注册命令」。"""
    cfg = ConfigManager(str(cfg_path))
    perm = PermissionManager(superusers=[SUPERUSER])
    cmds = CommandManager(prefixes=["/", "#"], perm_manager=perm, cfg_ref=cfg)
    perm.set_group_role(GROUP, GROUP_ADMIN, "admin")   # 群里一个管理员，其余是成员
    return cfg, perm, cmds


def register_demo(cmds: CommandManager):
    """注册本测试用到的命令 —— 重复调用即等价于插件 /reload 后重新注册。"""

    @cmds.register("plain", permission="admin", scope="all",
                   description="演示叶子命令")
    async def cmd_plain(packet=None):
        return "plain-ok"

    # 先注册子命令：父命令本体此时还不存在 → 走「占位父命令」分支
    @cmds.register("ns.one", permission="superuser", scope="all",
                   description="命名空间子命令一")
    async def cmd_ns_one(packet=None):
        return "ns-one-ok"

    @cmds.register("ns.two", permission="", scope="all",
                   description="命名空间子命令二")
    async def cmd_ns_two(packet=None):
        return "ns-two-ok"

    # 再注册本体：占位 handler 的子命令要继承过来（覆盖必须还在）
    @cmds.register("ns", description="演示命令组", scope="all")
    async def cmd_ns(packet=None):
        return "ns-body"

    @cmds.register("aliasdemo", permission="admin", aliases=["ad"], scope="all",
                   description="演示别名")
    async def cmd_alias(packet=None):
        return "alias-ok"


async def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    cfg_path = TMP / "config.json"
    cfg, perm, cmds = build(cfg_path)
    register_demo(cmds)

    async def dispatch(cmd, message_type="private", user_id=SUPERUSER, group_id=None):
        """返回 (原始结果, 文本) —— 元组即框架的隐藏命令哨兵。"""
        ev = MessageEvent(
            message_type=message_type, user_id=user_id, group_id=group_id,
            message=[{"type": "text", "data": {"text": cmd}}],
            raw_message=cmd,
        )
        r = await cmds.handle(ev)
        if isinstance(r, tuple):
            return r, ""
        return r, str(r or "")

    def table():
        """config.json 里的覆盖表（不存在 = 空表）。"""
        if not cfg_path.exists():
            return {}
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return data.get("command_permissions", {})

    print("\n[1] 基线：未覆盖时一切照声明值")
    _, txt = await dispatch("/help plain")
    check("未覆盖：/help 显示声明值 admin", "权限：admin" in txt, txt[:120])
    check("未覆盖：/help 不出现覆盖标注", "已覆盖" not in txt, txt[:120])
    res, _ = await dispatch("/plain", "group", GROUP_MEMBER, GROUP)
    check("未覆盖：群成员执行 admin 命令 → 隐藏哨兵",
          isinstance(res, tuple) and res[0] == "_not_found", repr(res))
    _, txt = await dispatch("/plain", "group", GROUP_ADMIN, GROUP)
    check("未覆盖：群管理员可执行", txt == "plain-ok", txt)
    _, txt = await dispatch("/perm list")
    check("/perm 无覆盖时的提示", "当前没有权限覆盖" in txt, txt[:80])

    print("\n[2] set：覆盖立刻改变拦截口径并落盘")
    _, txt = await dispatch("/perm set plain member")
    check("set 返回含新值与声明值",
          "已设为 member" in txt and "声明值：admin" in txt, txt[:120])
    _, txt = await dispatch("/plain", "group", GROUP_MEMBER, GROUP)
    check("覆盖后群成员可执行", txt == "plain-ok", txt)
    check("config.json 落盘覆盖", table() == {"plain": "member"}, str(table()))
    _, txt = await dispatch("/perm get plain")
    check("/perm get 显示生效值 + 声明值",
          "member" in txt and "声明值：admin" in txt, txt[:120])
    _, txt = await dispatch("/help plain")
    # ⚠️ 不要写死整行格式：`/help` 的权限行里现在还会带「· 作用域：…」
    # （框架的命令需求描述改成「权限 + 作用域」两段了）。这里要验的是**两件事**：
    # 生效值变成了 member、且标注了它是覆盖来的。
    check("/help 标注覆盖来源",
          "权限：member" in txt and "已覆盖，声明值：admin" in txt, txt[:140])

    print("\n[3] 取值校验：白名单 + 自定义节点 + 公开归一化")
    _, txt = await dispatch("/perm set plain viip")
    check("未注册节点当场拒绝", "无效的权限值" in txt and "superuser" in txt, txt[:120])
    check("拒绝后不写盘", table() == {"plain": "member"}, str(table()))
    _, txt = await dispatch("/perm set plain")
    check("缺值 → 用法提示", "用法：/perm set" in txt, txt[:80])
    _, txt = await dispatch("/perm set nosuchcmd admin")
    check("未知命令", "未知命令" in txt, txt[:80])

    @perm.register("vip")
    async def check_vip(user_id, group_id, is_group):
        return True

    _, txt = await dispatch("/perm set plain vip")
    check("已注册的自定义节点可用", "已设为 vip" in txt, txt[:120])
    _, txt = await dispatch("/plain", "group", GROUP_MEMBER, GROUP)
    check("自定义节点放行群成员", txt == "plain-ok", txt)
    perm._custom_checkers.pop("vip", None)          # 模拟插件卸载后节点消失
    res, _ = await dispatch("/plain", "group", GROUP_MEMBER, GROUP)
    check("节点消失后非超级用户被挡（未知节点 → False）",
          isinstance(res, tuple) and res[0] == "_not_found", repr(res))
    _, txt = await dispatch("/plain", "group", SUPERUSER, GROUP)
    check("节点消失也不影响超级用户", txt == "plain-ok", txt)

    _, txt = await dispatch("/perm set plain public")
    check("public 归一化为公开", "已设为 公开" in txt, txt[:120])
    check("公开值落盘为空串", table() == {"plain": ""}, str(table()))
    _, txt = await dispatch("/plain", "group", GROUP_MEMBER, GROUP)
    check("公开后群成员可执行", txt == "plain-ok", txt)

    print("\n[4] 命令组：本体拒绝，子命令可覆盖且互不影响")
    _, txt = await dispatch("/perm set ns admin")
    check("命令组本体拒绝并列出子命令",
          "命令组" in txt and "ns.one" in txt and "ns.two" in txt, txt[:140])
    _, txt = await dispatch("/perm set ns.one member")
    check("子命令可覆盖", "已设为 member" in txt, txt[:120])
    check("子命令覆盖按 `父.子` 落盘",
          table().get("ns.one") == "member", str(table()))
    _, txt = await dispatch("/ns one", "group", GROUP_MEMBER, GROUP)
    check("子命令覆盖生效（占位父命令注册路径）", txt == "ns-one-ok", txt)
    _, txt = await dispatch("/ns two", "group", GROUP_MEMBER, GROUP)
    check("兄弟子命令不受影响", txt == "ns-two-ok", txt)
    _, txt = await dispatch("/ns", "group", GROUP_MEMBER, GROUP)
    check("命令组本体仍无需权限（裸调重定向到帮助页，而不是被拦）",
          txt.startswith("📖 /ns") and "未知命令" not in txt
          and "/ns one" in txt and "/ns two" in txt, txt[:140])
    _, txt = await dispatch("/perm get ns.one")
    check("/perm get 子命令显示生效/声明值",
          "member" in txt and "superuser" in txt, txt[:120])

    print("\n[5] 别名解析到规范名")
    _, txt = await dispatch("/perm set ad owner")
    check("别名可设置", "已设为 owner" in txt, txt[:120])
    check("存储用规范名（不是别名）",
          table().get("aliasdemo") == "owner" and "ad" not in table(), str(table()))
    _, txt = await dispatch("/perm list")
    check("列表显示规范名", "/aliasdemo" in txt and "/ad " not in txt, txt[:160])

    print("\n[6] reset：恢复声明值 + 空表删键")
    _, txt = await dispatch("/perm reset plain")
    check("reset 恢复声明值", "已恢复声明值：admin" in txt, txt[:120])
    check("reset 后该键消失", "plain" not in table(), str(table()))
    res, _ = await dispatch("/plain", "group", GROUP_MEMBER, GROUP)
    check("reset 后群成员重新被挡",
          isinstance(res, tuple) and res[0] == "_not_found", repr(res))
    _, txt = await dispatch("/perm reset plain")
    check("重复 reset 有明确提示", "没有权限覆盖" in txt, txt[:80])
    _, txt = await dispatch("/perm reset all")
    check("reset all 清除全部", "已清除全部" in txt, txt[:80])
    check("空表时删除配置键", "command_permissions" not in
          json.loads(cfg_path.read_text(encoding="utf-8")), str(table()))

    print("\n[7] 持久化：重启（重建 CommandManager）后仍生效")
    await dispatch("/perm set plain member")
    await dispatch("/perm set ns.one owner")
    cfg2, perm2, cmds2 = build(cfg_path)
    register_demo(cmds2)
    plain2 = cmds2.list_commands()["plain"]
    check("重启后叶子命令覆盖仍在",
          plain2.permission == "member" and plain2.declared_permission == "admin",
          f"{plain2.permission}/{plain2.declared_permission}")
    ns2 = cmds2.list_commands()["ns"]
    check("重启后子命令覆盖仍在",
          ns2.subcommands["one"].permission == "owner",
          ns2.subcommands["one"].permission)
    check("重启后兄弟子命令未被牵连",
          ns2.subcommands["two"].permission == "",
          repr(ns2.subcommands["two"].permission))

    print("\n[8] 插件热重载：重新注册同名命令后覆盖必须重新套用")
    register_demo(cmds)                       # 同一 manager 上重建 handler = /reload 路径
    plain = cmds.list_commands()["plain"]
    check("reload 后覆盖未回退",
          plain.permission == "member" and plain.declared_permission == "admin",
          f"{plain.permission}/{plain.declared_permission}")
    check("reload 后子命令覆盖未回退",
          cmds.list_commands()["ns"].subcommands["one"].permission == "owner",
          cmds.list_commands()["ns"].subcommands["one"].permission)

    print("\n[9] 脏条目容错：未注册命令 / 未知节点")
    cfg_path.write_text(json.dumps(
        {"command_permissions": {"ghost": "admin", "plain": "notanode"}},
        ensure_ascii=False), encoding="utf-8")
    cfg3, perm3, cmds3 = build(cfg_path)
    register_demo(cmds3)
    check("未知节点条目保留（不擅自删用户数据）",
          cmds3.list_commands()["plain"].permission == "notanode",
          cmds3.list_commands()["plain"].permission)

    async def dispatch3(cmd, message_type="private", user_id=SUPERUSER, group_id=None):
        ev = MessageEvent(message_type=message_type, user_id=user_id, group_id=group_id,
                          message=[{"type": "text", "data": {"text": cmd}}],
                          raw_message=cmd)
        r = await cmds3.handle(ev)
        return (r, "") if isinstance(r, tuple) else (r, str(r or ""))

    res, _ = await dispatch3("/plain", "group", GROUP_MEMBER, GROUP)
    check("未知节点 → 非超级用户被挡",
          isinstance(res, tuple) and res[0] == "_not_found", repr(res))
    _, txt = await dispatch3("/perm list")
    check("列表标注未注册条目", "未注册" in txt and "ghost" in txt, txt[:200])
    _, txt = await dispatch3("/perm reset ghost")
    check("残留覆盖可清除", "已清除残留覆盖" in txt, txt[:120])
    check("清除后配置只剩另一条", table() == {"plain": "notanode"}, str(table()))

    print("\n[10] /perm 自身的权限边界")
    res, _ = await dispatch("/perm", "group", SUPERUSER, GROUP)
    check("群里（含超级用户）看不到 /perm（scope=private）",
          isinstance(res, tuple) and res[0] == "_not_found", repr(res))
    res, _ = await dispatch("/perm", "private", GROUP_MEMBER)
    check("私聊非超级用户看不到 /perm",
          isinstance(res, tuple) and res[0] == "_not_found", repr(res))
    _, txt = await dispatch("/perm list", "private", SUPERUSER)
    check("私聊超级用户可用", "权限覆盖" in txt, txt[:80])

    print("\n[11] 三级命令名守卫")
    raised = ""
    try:
        cmds.register("a.b.c", permission="admin")
    except ValueError as exc:
        raised = str(exc)
    check("三级名显式报错（不再静默截断成 a.b）",
          "超过两级" in raised, raised or "没有抛异常")
    check("二级名仍正常", cmds.find_command("ns.one") is not None,
          "find_command 返回 None")

    print(f"\n{'=' * 46}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
