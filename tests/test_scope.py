"""服务范围白名单（`napcat.scope`）—— 四个插件共用的口径。

口径取自 byrdocs，四个插件一致；ai_chat 额外保留「受邀参与别人的会话」。
这个套件钉住语义本身（不看插件），插件层面的集成由各自套件覆盖：

1. **名单只看本插件配置**，空 = 谁都不用；
2. **群聊 / 群小窗按来源群判，直接私聊按用户判**，两条通道互不代替；
3. `parse_ids` 只收正整数：`bool`（`int(True)==1` 的笔误）、0、负数、非数字丢弃；
4. 缺失/非法的号（`None` / `"abc"`）一律不放行。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from napcat.scope import Scope, parse_ids

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


class Ev:
    """最小事件替身：只带 `event_ok` 会读的字段。"""

    def __init__(self, *, message_type="group", group_id=None, user_id=None,
                 is_temp_session=False):
        self.message_type = message_type
        self.group_id = group_id
        self.user_id = user_id
        self.is_temp_session = is_temp_session


def test_parse_ids() -> None:
    print("\n[1] parse_ids：只收正整数")
    check("字符串数字照收", parse_ids(["123", 456]) == {123, 456})
    check("bool 丢弃（True 不是 QQ 1）", parse_ids([True, False]) == set())
    check("0 / 负数丢弃", parse_ids([0, -5, 7]) == {7})
    check("非数字丢弃", parse_ids(["abc", None, {}, []]) == set())
    check("非列表 → 空", parse_ids("123") == set() and parse_ids(None) == set())
    check("集合/元组也认", parse_ids(("9",)) == {9} and parse_ids({11}) == {11})


def test_scope_policy() -> None:
    print("\n[2] Scope：群 / 私聊 / 小窗")
    empty = Scope.of({"groups": [], "users": []})
    check("空名单：群不放行", not empty.group_ok(100))
    check("空名单：私聊不放行", not empty.user_ok(200))
    check("空名单：小窗不放行",
          not empty.event_ok(Ev(message_type="private", group_id=100,
                                user_id=200, is_temp_session=True)))

    sc = Scope.of({"groups": [100], "users": [200]})
    check("群名单命中放行", sc.event_ok(Ev(group_id=100)) is True)
    check("群名单未命中拒绝", sc.event_ok(Ev(group_id=101)) is False)
    check("私聊按用户放行",
          sc.event_ok(Ev(message_type="private", user_id=200)) is True)
    check("私聊未命中拒绝",
          sc.event_ok(Ev(message_type="private", user_id=201)) is False)

    # 小窗按**来源群**判：人在 users 里但来源群没开 → 拒
    check("小窗按来源群判（群已开）",
          sc.event_ok(Ev(message_type="private", group_id=100, user_id=999,
                         is_temp_session=True)) is True)
    check("小窗按来源群判（群未开，即便人在 users 里）",
          sc.event_ok(Ev(message_type="private", group_id=101, user_id=200,
                         is_temp_session=True)) is False)

    check("缺失号不放行（None）",
          sc.event_ok(Ev(group_id=None)) is False
          and sc.event_ok(Ev(message_type="private", user_id=None)) is False)
    check("非法号不放行（'abc'）", sc.group_ok("abc") is False)
    check("bool 号不放行（True 不是 1）",
          sc.group_ok(True) is False and sc.group_ok(1) is False)
    check("无上下文（None 事件）放行", sc.event_ok(None) is True)
    check("非 dict 配置 → 空名单", Scope.of("nope").groups == frozenset())


def test_scope_of() -> None:
    print("\n[3] Scope.of：从 binding 结构建")
    sc = Scope.of({"groups": [1, "2", True, 0], "users": [3, -1]})
    check("群名单纯净化", sc.groups == frozenset({1, 2}), str(sc.groups))
    check("用户名单纯净化", sc.users == frozenset({3}), str(sc.users))
    check("缺键按空处理", Scope.of({"groups": [1]}).users == frozenset())


def main() -> int:
    test_parse_ids()
    test_scope_policy()
    test_scope_of()
    print(f"\n{'=' * 44}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
