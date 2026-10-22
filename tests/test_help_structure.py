"""`/help` 三级入口的行为基线 —— 标题 + 整体描述 + 子命令描述/用法。

帮助文档的构成是**三层**，每层只多展开一档信息：

    /help                     命令清单：每条命令的「整体描述」（整体描述写在主命令里）
    /help <主命令>             该命令组的子命令描述（清单由各子命令的描述拼出来）
    /help <主命令> <子命令>     该子命令的描述 + 用法/参数（与 `/help <主命令>.<子命令>` 等价）

存在的原因：第三种入口此前是坏的 —— 解析器把第二个 token 当成 `packet.subcommand`
剥走，`CommandHandler.execute()` 又把它塞回 `args`，而 `_cmd_help` 只读 `args[0]`，
于是 `/help demo alpha` **静默退化成 `/help demo`**（点号写法却正常）。用户以为查了
子命令，看到的却是父命令的清单，而且没有任何报错。这里把三种入口的输出一起钉住，
包括「第二段不是子命令时回落到父命令」（用 `demo` 的取值写法验证；系统命令组如 `/help perm set` 现在有真正的子命令）。

跑法：
    cd /ncqqpy && ./venv-arm/bin/python -B tests/test_help_structure.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from napcat.command import CommandManager, CommandSchema, StrField   # noqa: E402
from napcat.models import MessageEvent                               # noqa: E402
from napcat.permission import PermissionManager                      # noqa: E402

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


class AlphaSchema(CommandSchema):
    name = StrField(required=True, description="对象名字")
    times = StrField(default="1", description="重复次数")


def build():
    """一套最小命令集：一个命令组（三个子命令）+ 一个叶子命令。"""
    perm = PermissionManager(superusers=[SUPERUSER])
    cmds = CommandManager(prefixes=["/"], perm_manager=perm)
    perm.set_group_role(GROUP, GROUP_ADMIN, "admin")

    @cmds.register("demo", description="演示命令组：整体描述写在主命令里")
    async def cmd_demo(packet=None):
        return "demo-body"

    @cmds.register("demo.alpha", schema=AlphaSchema, description="子命令甲：按名字办事")
    async def cmd_alpha(params, event):
        return "alpha"

    @cmds.register("demo.beta", description="子命令乙：无参数")
    async def cmd_beta(packet=None):
        return "beta"

    @cmds.register("demo.admin", permission="admin", description="子命令丙：需管理员")
    async def cmd_admin(packet=None):
        return "admin"

    @cmds.register("demo.secret", permission="", scope="private",
                   description="子命令丁：仅私聊")
    async def cmd_secret(packet=None):
        return "secret"

    @cmds.register("plain", permission="admin", description="叶子命令：无子命令")
    async def cmd_plain(packet=None):
        return "plain"

    @cmds.register("leafy", schema=AlphaSchema, description="叶子命令：带参数")
    async def cmd_leafy(params, event):
        return "leafy"

    # 子命令先注册、本体始终没注册 → 框架的「占位父命令」顶着（ai_chat /
    # bupt_news 的真实写法：父命令描述由插件事后回填）
    @cmds.register("ns.one", description="占位组子命令一")
    async def cmd_ns_one(packet=None):
        return "one"

    @cmds.register("ns.two", description="占位组子命令二")
    async def cmd_ns_two(packet=None):
        return "two"

    return cmds


async def main() -> int:
    cmds = build()

    async def help_of(cmd, message_type="private", user_id=SUPERUSER, group_id=None):
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

    def lines(text):
        return [ln.strip() for ln in text.splitlines()]

    def detail_of(text, needle):
        return next((ln for ln in lines(text) if needle in ln), "")

    def section_of(text, needle):
        """needle 所在行所属的 [分组] 标题。"""
        cur = ""
        for line in lines(text):
            if line.startswith("[") and line.endswith("]"):
                cur = line
            elif needle in line:
                return cur
        return ""

    # ==================== 第一级：/help ====================
    print("\n[1] /help —— 命令组 / 无子命令命令的介绍（列表）")
    _, top = await help_of("/help")
    check("首行是标题", top.splitlines()[0].startswith("📖 可用命令（"), top[:60])
    check("命令组的介绍来自主命令的 description",
          section_of(top, "/demo —") == "[📦 命令组]"
          and detail_of(top, "/demo —") == "/demo — 演示命令组：整体描述写在主命令里",
          detail_of(top, "/demo —"))
    check("没有子命令的命令也在列（含介绍）",
          detail_of(top, "/plain —") == "/plain — 叶子命令：无子命令",
          detail_of(top, "/plain —"))
    check("无子命令的命令与子命令同版式：`[📖 命令]` 段 + `  /名字 — 介绍`",
          section_of(top, "/plain —") == "[📖 命令]"
          and section_of(top, "/leafy —") == "[📖 命令]", top[:400])
    check("第一级不按权限分类（没有权限分组标题）",
          not any(h in top for h in ("[🔓 所有人可用]", "[🛡️ admin]", "[👑 superuser]",
                                     "[🔑 vip]")), top[:400])
    check("第一级只给介绍，不给用法（用法在第二级）",
          detail_of(top, "/leafy —") == "/leafy — 叶子命令：带参数"
          and "<name>" not in detail_of(top, "/leafy —"),
          detail_of(top, "/leafy —"))
    check("子命令不进第一级（只列命令组与无子命令的命令）",
          not any(f"/{p}" in top for p in ("demo alpha", "demo beta", "demo admin",
                                           "demo secret", "ns one", "ns two")),
          top[:400])
    check("第一级不展开参数表（那是第三级的事）",
          "位置传参：" not in top and "键值传参：" not in top, top[:200])
    check("结尾指路：/help <命令名> 看子命令或用法",
          top.rstrip().endswith("查看子命令或用法"), top[-40:])

    # ==================== 第二级：/help <主命令> ====================
    print("\n[2] /help <主命令> —— 子命令清单（名字 + 权限/作用域 + 介绍）")
    _, group = await help_of("/help demo")
    check("首行是主命令标题", group.splitlines()[0].startswith("📖 /demo"), group[:60])
    check("带出写在主命令里的整体描述", "演示命令组：整体描述写在主命令里" in group, group[:200])
    check("命令组本体：写明无需权限 + 作用域",
          "本体无需权限" in group and "作用域：所有场景" in group, group[:200])
    check("子命令甲：名字 + 权限/作用域 + 介绍",
          detail_of(group, "/demo alpha")
          == "/demo alpha [权限：公开 · 作用域：所有场景] — 子命令甲：按名字办事",
          detail_of(group, "/demo alpha"))
    check("子命令乙：名字 + 权限/作用域 + 介绍",
          detail_of(group, "/demo beta")
          == "/demo beta [权限：公开 · 作用域：所有场景] — 子命令乙：无参数",
          detail_of(group, "/demo beta"))
    check("子命令丙：标注自身权限（不是父命令的默认值）",
          detail_of(group, "/demo admin")
          == "/demo admin [权限：admin · 作用域：所有场景] — 子命令丙：需管理员",
          detail_of(group, "/demo admin"))
    check("子命令丁：标注自身作用域",
          "[权限：公开 · 作用域：仅私聊] — 子命令丁：仅私聊"
          in detail_of(group, "/demo secret"), detail_of(group, "/demo secret"))
    check("第二级不带参数 / 用法（那属于第三级）",
          "<name>" not in group and "[times=1]" not in group
          and "位置传参：" not in group, group[:400])

    # ==================== 第三级：/help <主命令> <子命令> ====================
    print("\n[3] /help <主命令> <子命令> —— 子命令描述 + 用法")
    _, sub = await help_of("/help demo alpha")
    check("首行是该子命令的用法（标题 + 用法）",
          sub.splitlines()[0] == "📖 /demo alpha <name> [times=1]", sub[:80])
    check("带出该子命令的描述", "子命令甲：按名字办事" in sub, sub[:200])
    check("第三级写明权限 + 作用域",
          "权限：公开 · 作用域：所有场景" in sub, sub[:200])
    check("不再退化成父命令的清单（这是本次修的 bug）",
          sub != group and "子命令乙" not in sub, sub[:200])
    check("给出位置传参写法", "位置传参：/demo alpha <name> [times=1]" in sub, sub[:300])
    check("给出键值传参写法", "键值传参：/demo alpha --name=<值> [--times=1]" in sub, sub[:400])
    check("展开参数表（名称 / 权限 / 默认值）",
          "<name> StrField 必填 — 对象名字" in sub
          and "[times] StrField 可选 [默认: 1] — 重复次数" in sub, sub[:500])
    check("带占位符图例", "尖括号/方括号不用输入" in sub, sub[:500])

    print("\n[4] 两种写法等价：空格 vs 点号")
    _, dotted = await help_of("/help demo.alpha")
    check("`/help demo alpha` == `/help demo.alpha`", sub == dotted,
          f"{sub[:80]!r} != {dotted[:80]!r}")
    _, dotted_beta = await help_of("/help demo.beta")
    _, spaced_beta = await help_of("/help demo beta")
    check("无 schema 的子命令同样等价（beta）", dotted_beta == spaced_beta,
          f"{dotted_beta[:60]!r} != {spaced_beta[:60]!r}")

    print("\n[5] 边界：第二段不是子命令时回落到主命令")
    _, miss = await help_of("/help demo nosuch")
    check("未知子命令 → 回落到父命令清单（顺便让用户看见正确的子命令名）",
          miss == group, miss[:120])
    raw, _ = await help_of("/help nosuch")
    check("主命令不存在 → 未知命令", isinstance(raw, tuple) and raw[0] == "_not_found"
          or "未知命令" in str(raw), repr(raw))
    _, txt = await help_of("/help nosuch sub")
    check("主命令与子命令都不存在 → 未知命令", txt.startswith("未知命令: /nosuch"), txt[:80])
    _, txt = await help_of("/help help")
    check("帮助自己仍按「未知命令」处理（既有口径不变）",
          txt.startswith("未知命令: /help"), txt[:80])

    # ==================== 权限 / 范围与执行同口径 ====================
    print("\n[6] 权限与范围：帮助与执行同口径")
    raw, txt = await help_of("/help demo admin", "group", GROUP_MEMBER, GROUP)
    # 帮助命令本身可见，所以这里是 _cmd_help 自己渲染的「未知命令」文本
    # （与 handle() 的 _not_found 口径同一句话）。
    check("群成员问 admin 子命令详情 → 未知命令（与执行一致）",
          txt.startswith("未知命令: /demo"), f"{raw!r} {txt[:80]!r}")
    _, member_group = await help_of("/help demo", "group", GROUP_MEMBER, GROUP)
    check("群成员的子命令清单里没有 admin",
          "/demo admin" not in member_group, member_group[:300])
    _, admin_group = await help_of("/help demo admin", "group", GROUP_ADMIN, GROUP)
    check("群管理员能看到 admin 子命令的详情",
          admin_group.splitlines()[0].startswith("📖 /demo admin"), admin_group[:80])
    _, member_private = await help_of("/help demo", user_id=GROUP_MEMBER)
    check("仅私聊的子命令只出现在私聊帮助里",
          "仅私聊" in member_private and "子命令丁" in member_private, member_private[:300])
    check("群聊帮助里没有仅私聊的子命令",
          "子命令丁" not in member_group, member_group[:300])

    print("\n[7] 没有子命令的命令：/help <命令名> 给介绍 + 用法")
    _, leaf = await help_of("/help leafy")
    check("标题带用法", leaf.splitlines()[0] == "📖 /leafy <name> [times=1]", leaf[:80])
    check("带出介绍", "叶子命令：带参数" in leaf, leaf[:200])
    check("叶子命令详情同样写明权限 + 作用域（公开也写全）",
          "权限：公开 · 作用域：所有场景" in leaf, leaf[:200])
    check("展开参数表", "[times] StrField 可选 [默认: 1] — 重复次数" in leaf, leaf[:400])
    _, plain = await help_of("/help plain")
    check("无参数命令：标题就是命令名 + 介绍",
          plain.splitlines()[0] == "📖 /plain" and "叶子命令：无子命令" in plain, plain[:120])
    check("无参数命令也写明权限 + 作用域", "权限：admin · 作用域：所有场景" in plain,
          plain[:200])
    raw, txt = await help_of("/help plain extra")
    check("叶子命令后面的多余 token 不改变结果（回落本体）",
          txt.splitlines()[0].startswith("📖 /plain"), txt[:80])

    # ==================== 占位父命令（本体没注册） ====================
    print("\n[8] 占位父命令：子命令先注册、本体由框架顶着")
    _, ph = await help_of("/help ns")
    check("第二级：占位组照样列出各子命令的名字 + 权限/作用域 + 介绍",
          detail_of(ph, "/ns one")
          == "/ns one [权限：公开 · 作用域：所有场景] — 占位组子命令一"
          and detail_of(ph, "/ns two")
          == "/ns two [权限：公开 · 作用域：所有场景] — 占位组子命令二",
          ph[:300])
    _, ph_sub = await help_of("/help ns one")
    check("第三级：空格写法在占位组上同样生效",
          ph_sub.splitlines()[0].startswith("📖 /ns one"), ph_sub[:80])
    _, ph_dotted = await help_of("/help ns.one")
    check("两种写法在占位组上等价", ph_sub == ph_dotted,
          f"{ph_sub[:60]!r} != {ph_dotted[:60]!r}")

    # ==================== 帮助重定向 ====================
    print("\n[9] 帮助重定向：裸调命令组 / 用法不正确")
    _, page = await help_of("/help demo")
    raw, bare = await help_of("/demo")
    check("裸调命令组 → /help <命令组>（同一页，不执行本体）",
          bare == page and "demo-body" not in bare, bare[:120])
    raw, bare_ns = await help_of("/ns")
    check("占位父命令的组同样重定向到帮助页",
          bare_ns.startswith("📖 /ns") and "/ns one [权限：" in bare_ns, bare_ns[:160])

    raw, unknown_sub = await help_of("/demo nosuch")
    check("子命令名写错 → 回该命令组的帮助页（正好列出可用子命令）",
          unknown_sub == page, unknown_sub[:120])

    raw, bad = await help_of("/demo alpha")
    check("用法不正确 → 「⚠️ 用法不正确：…」+ 对应子命令的帮助页",
          bad.startswith("⚠️ 用法不正确：")
          and "📖 /demo alpha <name> [times=1]" in bad
          and "位置传参：" in bad, bad[:220])
    raw, bad_leaf = await help_of("/leafy")
    check("叶子命令用法不正确 → 它自己的帮助页",
          bad_leaf.startswith("⚠️ 用法不正确：") and "📖 /leafy <name>" in bad_leaf,
          bad_leaf[:160])

    raw, bare_group = await help_of("/demo", "group", SUPERUSER, GROUP)
    check("群聊里的重定向页同样带「需先 @机器人」",
          "@机器人" in bare_group and bare_group.startswith("📖 /demo"), bare_group[-90:])

    # 用不了的命令组（插件过滤器否决）：裸调静默、详情按未知命令
    def _hide_ns(handler, event):
        return handler.name != "ns" and handler.parent_name != "ns"

    cmds.add_visibility_filter(_hide_ns)
    try:
        raw, _txt = await help_of("/ns")
        check("用不了的命令组：裸调静默（不回任何内容）", raw is None, repr(raw))
        raw, txt = await help_of("/help ns")
        check("用不了的命令组：/help 按未知命令处理",
              txt.startswith("未知命令"), txt[:60])
    finally:
        cmds.remove_visibility_filter(_hide_ns)

    print(f"\n{'=' * 46}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
