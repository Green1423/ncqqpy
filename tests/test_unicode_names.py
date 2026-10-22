"""命令名 / 子命令名 / 别名支持非 ASCII（中文等 Unicode 字母）。

规则（`valid_command_name`，解析器/别名/注册三处共用一条）：

    Unicode 字母开头，后续可含字母、数字、下划线、连字符

所以 `/签到`、`点歌.列表`、别名 `/签` 都可用；`//`、`/签到！`、`/2签到`、`/🎵`
这类「前缀后不是名字」的输入仍然不是命令（静默丢弃）。

契约：
1. 解析器认中文命令名与中文子命令；
2. `register()` 接受中文名/中文子命令，注册期就拒绝非法名（否则永远收不到消息）；
3. `/alias` 的增删改查支持中文别名，并落盘 `command_aliases`；
4. `/help`、`/perm` 按中文名解析（与 ASCII 名同一路径，不区分脚本）。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / ".test-tmp" / "unicode-names"

from napcat.command import (                                  # noqa: E402
    CommandManager, CommandParser, valid_command_name,
)
from napcat.config import ConfigManager                       # noqa: E402
from napcat.models import MessageEvent                        # noqa: E402
from napcat.permission import PermissionManager               # noqa: E402

PASSED = 0
FAILED = 0
SUPER = 10000009


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


async def ask(cmds, text: str, *, user_id: int = SUPER):
    ev = MessageEvent(message_type="private", user_id=user_id, message=text)
    result = await cmds.handle(ev)
    if isinstance(result, tuple):
        return result[0]
    return str(result or "")


def test_name_rule():
    print("\n[1] 名字规则：Unicode 字母开头")
    valid = ["签到", "点歌", "签到2", "点-歌", "hello", "中文English", "签_到", "ñandú"]
    invalid = ["_签到", "2签到", "签到！", "签到 啊", "🎵", "", "  ", "-签到",
               "签到?", "签.到"]
    for n in valid:
        check(f"合法：{n!r}", valid_command_name(n))
    for n in invalid:
        check(f"非法：{n!r}", not valid_command_name(n), repr(n))
    check("长度上限生效", valid_command_name("签" * 64) and
          not valid_command_name("签" * 65))


def test_parser():
    print("\n[2] 解析器：中文命令 / 中文子命令 / 非法输入")
    parser = CommandParser(prefixes=["/"])
    pkt = parser.parse("/签到")
    check("中文命令名", pkt is not None and pkt.command == "签到" and not pkt.illegal)
    pkt = parser.parse("/点歌 列表 晴天")
    check("中文子命令 + 参数",
          pkt is not None and pkt.command == "点歌"
          and pkt.subcommand == "列表" and pkt.args == ["晴天"],
          repr(pkt))
    for text in ("/签到！", "/🎵", "/2签到"):
        pkt = parser.parse(text)
        check(f"非法输入静默：{text}",
              pkt is not None and pkt.illegal and pkt.illegal_kind == "name",
              repr(pkt))
    pkt = parser.parse("/" + "签" * 70)
    check("超长命令名判定 too_long",
          pkt is not None and pkt.illegal and pkt.illegal_kind == "too_long",
          repr(pkt))


async def test_execution(tmp: Path):
    print("\n[3] 注册与执行：中文命令 / 中文子命令 / 参数回落")
    cfg, perm, cmds = make(tmp)

    @cmds.register("签到", permission="", scope="all", description="每日签到")
    async def _sign(packet):
        return "签到成功"

    @cmds.register("回声", permission="", scope="all", description="回显参数")
    async def _echo(packet):
        return "args=" + ",".join(packet.args)

    @cmds.register("点歌.列表", permission="", scope="all", description="查歌单")
    async def _list(packet):
        return "列表" + (":" + ",".join(packet.args) if packet.args else "")

    @cmds.register("点歌.点播", permission="", scope="all", description="点一首")
    async def _play(packet):
        return "点播" + (":" + ",".join(packet.args) if packet.args else "")

    check("中文命令可执行", await ask(cmds, "/签到") == "签到成功")
    check("中文命令的普通参数回落正确（不误判成子命令）",
          await ask(cmds, "/回声 你好 世界") == "args=你好,世界")
    check("中文子命令可执行", await ask(cmds, "/点歌 列表") == "列表")
    check("中文子命令带参数", await ask(cmds, "/点歌 点播 晴天") == "点播:晴天")
    check("裸调中文命令组 → 帮助页",
          (await ask(cmds, "/点歌")).startswith("📖 /点歌"), await ask(cmds, "/点歌"))
    check("中文名进 /help 列表", "/签到" in await ask(cmds, "/help"),
          (await ask(cmds, "/help"))[:120])
    check("中文单命令帮助", "每日签到" in await ask(cmds, "/help 签到"))
    check("中文子命令帮助", "查歌单" in await ask(cmds, "/help 点歌 列表"))
    check("中文命令的权限覆盖可查",
          "member" in await ask(cmds, "/perm set 签到 member"))
    check("覆盖后帮助显示生效值",
          "权限：member" in await ask(cmds, "/help 签到"))


def test_registration_validation(tmp: Path):
    print("\n[4] 注册期校验：非法名字直接报错（而不是永远收不到消息）")
    _, _, cmds = make(tmp / "reg")
    for bad in ("签到！", "_签到", "2签到", "签到 啊", "签" * 65):
        try:
            cmds.register(bad)(lambda packet: "x")
        except ValueError as exc:
            check(f"拒绝注册 {bad!r}", "非法" in str(exc) or "过长" in str(exc),
                  str(exc)[:80])
        else:
            check(f"拒绝注册 {bad!r}", False, "没有抛 ValueError")
    try:
        cmds.register("a.b.c")(lambda packet: "x")
    except ValueError as exc:
        check("两级以上仍然拒绝", "两级" in str(exc), str(exc)[:80])
    else:
        check("两级以上仍然拒绝", False)


async def test_alias(tmp: Path):
    print("\n[5] 别名：中文 / 连字符 / 校验 / 落盘")
    cfg, perm, cmds = make(tmp / "alias")

    @cmds.register("签到", permission="", scope="all", description="每日签到")
    async def _sign(packet):
        return "签到成功"

    @cmds.register("点歌", permission="", scope="all", description="点歌")
    async def _song(packet):
        return "点歌成功"

    text = await ask(cmds, "/alias add 签 签到")
    check("中文别名可添加", "已给" in text, text[:120])
    check("中文别名可执行", await ask(cmds, "/签") == "签到成功")
    check("中文别名落盘", cfg.get("command_aliases.签到") == ["签"],
          str(cfg.get("command_aliases")))
    check("别名帮助里显示", "别名" in await ask(cmds, "/help 签到"))

    text = await ask(cmds, "/alias add 歌-单 点歌")
    check("连字符中文别名可添加", "已给" in text, text[:120])
    check("连字符中文别名可执行", await ask(cmds, "/歌-单") == "点歌成功")

    text = await ask(cmds, "/alias add 签！ 签到")
    check("中文别名含标点被拒", "非法" in text, text[:120])
    text = await ask(cmds, "/alias add 签到 点歌")
    check("别名与命令名冲突被拒", "占用" in text or "相同" in text, text[:120])
    text = await ask(cmds, "/alias list 签到")
    check("按中文命令查别名", "/签" in text, text[:120])
    text = await ask(cmds, "/alias del 签")
    check("删除中文别名", "已删除" in text, text[:120])
    check("删除后不可用", await ask(cmds, "/签") == "_not_found")

    # 新管理器（模拟重启）从配置恢复中文别名
    cmds2 = CommandManager(prefixes=["/"], perm_manager=PermissionManager(superusers=[SUPER]),
                           cfg_ref=ConfigManager(str(tmp / "alias" / "config.json")))
    cmds2.register("签到", permission="", scope="all")(lambda packet: "签到成功")
    cmds2.register("点歌", permission="", scope="all")(lambda packet: "点歌成功")
    check("重启后中文别名恢复（已删的不回来）",
          await ask(cmds2, "/歌-单") == "点歌成功"
          and await ask(cmds2, "/签") == "_not_found")


async def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    for child in TMP.iterdir():
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
    test_name_rule()
    test_parser()
    await test_execution(TMP / "exec")
    test_registration_validation(TMP)
    await test_alias(TMP)
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
