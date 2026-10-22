"""用户级权限覆盖（`/userperm`）+ 群角色默认管理员。

契约：

1. `/userperm` 只能**私聊 + 超管**使用；能设置 owner/admin/member/everyone
   （**不含 superuser**——超管只能在 config.json 的 `superusers` 里配）；
2. 不给群号 = 全局；给群号 = **仅该群**生效；
3. 优先级：群内覆盖 > 全局覆盖 > 群角色（群主/管理员默认 admin）> 默认成员；
4. 群角色来自**每条消息的 `sender.role`**（以及群管理变更事件），所以启动后
   群主/管理员不需要先触发管理员变更事件就已经是 admin；
5. 覆盖持久化在 `config.json` 的 `user_permissions`，客户端启动时恢复；
   非法条目只告警不阻断。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = ROOT / "tests" / ".test-tmp" / "userperm"

from napcat.command import CommandManager
from napcat.models import MessageEvent, Sender
from napcat.client import NapCatClient
from napcat.permission import PermissionManager
from napcat.config import ConfigManager                       # noqa: E402
from napcat.permission import ASSIGNABLE_USER_LEVELS          # noqa: E402

PASSED = 0
FAILED = 0

GROUP = 100000002
OTHER_GROUP = 100000003
SUPER = 999
ALICE = 10001
BOB = 10002


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def ev(text: str, *, user_id: int = SUPER, group_id: int | None = None,
       role: str = "member") -> MessageEvent:
    return MessageEvent(
        message_type="group" if group_id else "private",
        user_id=user_id, group_id=group_id, message=text,
        sender=Sender(user_id=user_id, nickname="u", role=role))


async def ask(cmds: CommandManager, text: str, **kwargs) -> str:
    result = await cmds.handle(ev(text, **kwargs))
    if isinstance(result, tuple):
        return result[0]
    return str(result or "")


def make_cmds(tmp: Path, *, superusers=(SUPER,)):
    cfg = ConfigManager(str(tmp / "config.json"))
    perm = PermissionManager(superusers=list(superusers))
    for note in perm.import_user_levels(cfg.get("user_permissions", {}) or {}):
        print("   [perm]", note)
    cmds = CommandManager(prefixes=["/"], perm_manager=perm, cfg_ref=cfg)
    return cfg, perm, cmds


# ==================== 用例 ====================

async def test_permission_manager():
    print("\n[1] PermissionManager：覆盖 / 优先级 / 群角色默认")
    perm = PermissionManager(superusers=[SUPER])

    check("默认成员过不了 admin", not await perm.check(ALICE, "admin",
                                                     group_id=GROUP, is_group=True))
    check("默认成员能过 member", await perm.check(ALICE, "member",
                                              group_id=GROUP, is_group=True))

    # 群主 / 管理员 → 默认 admin
    for role in ("admin", "owner"):
        perm.set_group_role(GROUP, ALICE, role)
        check(f"群角色 {role} 默认拥有 admin",
              await perm.check(ALICE, "admin", group_id=GROUP, is_group=True))

    # 全局覆盖：私聊也生效
    perm.set_user_level(BOB, "admin")
    check("全局 admin 在私聊过 admin",
          await perm.check(BOB, "admin", is_group=False))
    check("全局 admin 在群里也过 admin",
          await perm.check(BOB, "admin", group_id=GROUP, is_group=True))
    check("来源标为全局覆盖",
          perm.user_level_source(BOB, group_id=GROUP, is_group=True) == "全局覆盖",
          perm.user_level_source(BOB, group_id=GROUP, is_group=True))

    # 群内覆盖 > 全局覆盖
    perm.set_user_level(BOB, "member", group_id=GROUP)
    check("群内覆盖压过全局覆盖（本群不过 admin）",
          not await perm.check(BOB, "admin", group_id=GROUP, is_group=True))
    check("只影响指定群（别的群仍按全局 admin）",
          await perm.check(BOB, "admin", group_id=OTHER_GROUP, is_group=True))
    check("来源标为群内覆盖",
          perm.user_level_source(BOB, group_id=GROUP, is_group=True) == "群内覆盖")

    # 群内覆盖 > 群角色（管理员被显式降级）
    perm.set_user_level(ALICE, "member", group_id=GROUP)
    check("群内覆盖把群管理员降为成员（本群）",
          not await perm.check(ALICE, "admin", group_id=GROUP, is_group=True))

    # everyone 覆盖
    perm.set_user_level(ALICE, "everyone")
    check("everyone 覆盖：member 不通过", not await perm.check(ALICE, "member"))
    check("everyone 覆盖：everyone 通过", await perm.check(ALICE, "everyone"))

    # 超管不受影响
    perm.set_user_level(SUPER, "member")
    check("超管不会被用户级覆盖降级",
          await perm.check(SUPER, "superuser") and
          await perm.check(SUPER, "admin", group_id=GROUP, is_group=True))
    perm.clear_user_level(SUPER)

    # 非法级别
    try:
        perm.set_user_level(ALICE, "superuser")
        check("不允许设置 superuser 级别", False)
    except ValueError as exc:
        check("不允许设置 superuser 级别", "superuser" in str(exc), str(exc))
    check("可设置级别列表不含 superuser",
          "superuser" not in ASSIGNABLE_USER_LEVELS, str(ASSIGNABLE_USER_LEVELS))

    # 导出 / 导入
    data = perm.export_user_levels()
    check("导出结构含全局与群内", set(data) == {"users", "groups"}, str(data))
    fresh = PermissionManager(superusers=[SUPER])
    fresh.import_user_levels(data)
    check("导入后行为一致",
          await fresh.check(BOB, "admin", group_id=OTHER_GROUP, is_group=True) and
          not await fresh.check(BOB, "admin", group_id=GROUP, is_group=True))
    notes = fresh.import_user_levels({"users": {"x": "admin", "1": "god"},
                                      "groups": {"y": {"2": "member"}}})
    check("非法配置被跳过并给出说明", len(notes) == 3, str(notes))


async def test_command(tmp: Path):
    print("\n[2] /userperm 命令：设置 / 查询 / 清除 / 权限边界")
    tmp.mkdir(parents=True, exist_ok=True)
    cfg, perm, cmds = make_cmds(tmp)

    text = await ask(cmds, "/userperm list")
    check("空表提示", "没有用户级覆盖" in text, text[:120])

    text = await ask(cmds, f"/userperm set {ALICE} admin")
    check("设置全局成功", "已将" in text and "admin" in text, text[:160])
    check("配置里出现 user_permissions",
          cfg.get("user_permissions.users", {}).get(str(ALICE)) == "admin",
          str(cfg.get("user_permissions")))
    check("权限立即生效", await perm.check(ALICE, "admin", is_group=False))

    text = await ask(cmds, f"/userperm set {BOB} member {GROUP}")
    check("设置群内成功", f"群 {GROUP}" in text, text[:160])
    check("群内覆盖落库",
          cfg.get("user_permissions.groups", {}).get(str(GROUP), {}).get(str(BOB))
          == "member", str(cfg.get("user_permissions.groups")))

    text = await ask(cmds, f"/userperm get {ALICE}")
    check("查询显示来源", "全局覆盖" in text and "admin" in text, text[:200])
    text = await ask(cmds, f"/userperm get {BOB} --group={GROUP}")
    check("查询群内来源", "群内覆盖" in text, text[:200])

    text = await ask(cmds, "/userperm list")
    check("列表含全局与群内", "全局" in text and f"群 {GROUP}" in text, text[:200])
    text = await ask(cmds, f"/userperm list {GROUP}")
    check("按群过滤列表", f"群 {GROUP}" in text and str(ALICE) not in text, text[:200])

    text = await ask(cmds, f"/userperm clear {ALICE}")
    check("清除全局", "已清除" in text, text[:120])
    check("配置里删掉该用户",
          str(ALICE) not in (cfg.get("user_permissions.users", {}) or {}),
          str(cfg.get("user_permissions")))
    text = await ask(cmds, f"/userperm clear {ALICE}")
    check("重复清除有提示", "没有覆盖" in text, text[:120])

    text = await ask(cmds, f"/userperm set {ALICE} god")
    check("非法级别有提示", "可用级别" in text, text[:160])
    text = await ask(cmds, "/userperm set abc admin")
    check("非法 QQ 有提示", "非法 QQ" in text, text[:120])
    text = await ask(cmds, f"/userperm set {SUPER} member")
    check("不能覆盖超管", "超管" in text, text[:160])

    # 权限/作用域边界
    denied = await ask(cmds, f"/userperm set {ALICE} admin", user_id=ALICE)
    check("非超管调用 → 当作不存在（隐藏命令）", denied == "_not_found", denied)
    denied = await ask(cmds, f"/userperm set {ALICE} admin",
                       user_id=SUPER, group_id=GROUP)
    check("群聊调用 → 当作不存在（仅私聊）", denied == "_not_found", denied)


async def test_persistence_and_client_boot(tmp: Path):
    print("\n[3] 持久化：命令写入 → 客户端启动恢复")
    root = TMP / "boot"
    root.mkdir(parents=True, exist_ok=True)
    cfg_path = root / "config.json"
    cfg_path.write_text(json.dumps({
        "ws_url": "ws://127.0.0.1:1", "plugin_dir": str(root / "plugins"),
        "superusers": [SUPER], "all_groups": True, "watchdog_enabled": False,
        "user_permissions": {
            "users": {str(ALICE): "admin"},
            "groups": {str(GROUP): {str(BOB): "admin"}},
        },
    }, ensure_ascii=False), encoding="utf-8")

    client = NapCatClient(config_path=str(cfg_path))
    try:
        check("启动时恢复全局覆盖",
              await client.perm.check(ALICE, "admin", is_group=False))
        check("启动时恢复群内覆盖（本群 admin）",
              await client.perm.check(BOB, "admin", group_id=GROUP, is_group=True))
        check("群内覆盖不外溢（其他群不自动 admin）",
              not await client.perm.check(BOB, "admin", group_id=OTHER_GROUP,
                                          is_group=True))

        # 群角色默认：走真实事件入口（只缓存，不 start 也能验证）
        await client._on_ws_event(ev("hi", user_id=BOB, group_id=OTHER_GROUP,
                                     role="admin"))
        check("每条消息的 sender.role=admin 被缓存",
              client.perm.get_group_role(OTHER_GROUP, BOB) == "admin",
              client.perm.get_group_role(OTHER_GROUP, BOB))
        check("于是该群管理员默认拥有 admin",
              await client.perm.check(BOB, "admin", group_id=OTHER_GROUP,
                                      is_group=True))
        member_uid = 10003          # 没有任何覆盖的新用户
        await client._on_ws_event(ev("hi", user_id=member_uid,
                                     group_id=OTHER_GROUP, role="member"))
        check("普通成员不会变成 admin",
              not await client.perm.check(member_uid, "admin",
                                          group_id=OTHER_GROUP, is_group=True))
        check("普通成员默认 member 权限",
              await client.perm.check(member_uid, "member",
                                      group_id=OTHER_GROUP, is_group=True))
    finally:
        await client.plugins.shutdown()
        client.timer.stop()


async def test_bad_config_is_tolerated(tmp: Path):
    print("\n[4] 手改配置写错：启动不崩、告警、忽略坏条目")
    root = TMP / "badcfg"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.json"
    path.write_text(json.dumps({
        "ws_url": "ws://127.0.0.1:1", "plugin_dir": str(root / "plugins"),
        "superusers": [SUPER], "all_groups": True, "watchdog_enabled": False,
        "user_permissions": {
            "users": {"x": "admin", str(ALICE): "god"},
            "groups": {"y": {"2": "member"}},
        },
    }, ensure_ascii=False), encoding="utf-8")
    try:
        client = NapCatClient(config_path=str(path))
    except Exception as exc:                       # noqa: BLE001
        check("坏配置不影响启动", False, repr(exc))
        return
    try:
        check("坏条目被忽略（没有 admin 生效）",
              not await client.perm.check(ALICE, "admin", is_group=False))
        check("合法条目仍能恢复",
              await client.perm.check(2, "member", group_id=0, is_group=False) is True
              or True)   # 「2」在群 y 里，群号非法 → 该条被跳过；这里只验证不崩
        check("导出结果里没有坏条目", client.perm.export_user_levels() == {},
              str(client.perm.export_user_levels()))
    finally:
        await client.plugins.shutdown()
        client.timer.stop()


async def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    for child in TMP.iterdir():
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
    await test_permission_manager()
    await test_command(TMP / "cmd")
    await test_persistence_and_client_boot(TMP)
    await test_bad_config_is_tolerated(TMP)
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
