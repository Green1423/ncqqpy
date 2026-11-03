"""布尔参数的简写旗标（`-y`、`-f`、`-v` …）。

规则：
- `BoolField` 默认用**字段名首字母**做简写（仅 ASCII 字母）；
- 显式 `short="v"` 覆盖自动值；`short=""` 关闭；
- 同字母被多个布尔字段抢用时**不猜**：`-y` 会报「有歧义」并提示用 `--字段名`；
- 简写等价于 `--字段名=true`，显式 `--字段名=false` 优先；
- 帮助里的「键值传参」显示 `[-y]`，参数表标注「简写：-y」。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from napcat.command import (                                  # noqa: E402
    BoolField, CommandManager, CommandSchema, StrField,
)
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


class PlainSchema(CommandSchema):
    name = StrField(required=True, description="名字")
    yes = BoolField(description="跳过确认")
    force = BoolField(description="强制执行", default=False)
    verbose = BoolField(short="v", description="详细输出")
    quiet = BoolField(short="", description="安静模式")


class AmbiguousSchema(CommandSchema):
    yes = BoolField(description="第一个 y")
    yolo = BoolField(description="第二个 y")


class RequiredSchema(CommandSchema):
    name = StrField(required=True)
    sure = BoolField(required=True, description="必须确认")


def expect_error(schema, args, kwargs, needle: str) -> None:
    from napcat.command import SchemaError
    try:
        schema.validate(args, kwargs)
    except SchemaError as exc:
        text = str(exc)
        check(f"报错含「{needle}」", needle in text, text[:160])
    else:
        check(f"报错含「{needle}」", False, "没有抛 SchemaError")


def test_field_and_map():
    print("\n[1] BoolField.short 与 short_flag_map")
    check("显式 short 保留", BoolField(short="v").short == "v")
    check("short='-V' 归一化为小写无横线", BoolField(short="-V").short == "v")
    check("short='' 表示关闭", BoolField(short="").short == "")
    check("未传 short 为 None（自动）", BoolField().short is None)
    for bad in ("yy", "1", "中", "-"):
        try:
            field = BoolField(short=bad)
        except ValueError:
            check(f"非法 short 被拒：{bad!r}", True)
        else:
            check(f"非法 short 被拒：{bad!r}", field.short == "",
                  f"得到 {field.short!r}")

    mapping, ambiguous = PlainSchema.short_flag_map()
    check("自动首字母：yes→y", mapping.get("y") == "yes", str(mapping))
    check("自动首字母：force→f", mapping.get("f") == "force", str(mapping))
    check("显式简写：verbose→v", mapping.get("v") == "verbose", str(mapping))
    check("short='' 不产生简写", "q" not in mapping, str(mapping))
    check("无歧义", ambiguous == {}, str(ambiguous))

    mapping2, ambiguous2 = AmbiguousSchema.short_flag_map()
    check("歧义字母不进映射", "y" not in mapping2, str(mapping2))
    check("歧义表列出两个字段", ambiguous2.get("y") == ["yes", "yolo"],
          str(ambiguous2))


def test_validation():
    print("\n[2] 校验：简写 = --字段=true，显式值优先")
    params = PlainSchema().validate(["a"], {"y": "true"})
    check("-y 置 yes=True", params["yes"] is True, repr(params["yes"]))
    params = PlainSchema().validate(["a"], {"f": "true"})
    check("-f 置 force=True", params["force"] is True, repr(params["force"]))
    params = PlainSchema().validate(["a"], {"v": "true"})
    check("显式 -v 置 verbose=True", params["verbose"] is True)
    params = PlainSchema().validate(["a"], {"Y": "true"})
    check("大写 -Y 也认", params["yes"] is True)
    params = PlainSchema().validate(["a"], {"y": "true", "force": "false"})
    check("简写与显式值共存", params["yes"] is True and params["force"] is False)
    params = PlainSchema().validate(["a"], {"quiet": "true"})
    check("关闭简写的字段仍可用 --quiet", params["quiet"] is True)
    params = PlainSchema().validate(["a"], {"q": "true"})
    check("关闭简写后 -q 被忽略（不报错）", params["quiet"] is None,
          repr(params["quiet"]))
    params = PlainSchema().validate(["a", "true"], {})
    check("位置布尔值仍可用", params["yes"] is True)
    params = RequiredSchema().validate(["a"], {"s": "true"})
    check("必填布尔可被 -s 满足", params["sure"] is True)

    expect_error(AmbiguousSchema(), [], {"y": "true"}, "有歧义")
    expect_error(AmbiguousSchema(), [], {"y": "true"}, "yes / yolo")
    params = AmbiguousSchema().validate([], {"yes": "true", "yolo": "false"})
    check("歧义时 --字段名 仍可用", params["yes"] is True and params["yolo"] is False)

    from napcat.command import SchemaError
    try:
        RequiredSchema().validate(["a"], {})
    except SchemaError as exc:
        check("必填布尔缺失时照常报必填", "必填" in str(exc), str(exc)[:120])
    else:
        check("必填布尔缺失时照常报必填", False)


async def test_end_to_end():
    print("\n[3] 端到端：命令 / 子命令 / 帮助显示")
    cmds = CommandManager(prefixes=["/"],
                          perm_manager=PermissionManager(superusers=[SUPER]))

    @cmds.register("demo", schema=PlainSchema, permission="", scope="all",
                   description="演示")
    async def _demo(params, packet):
        return (f"name={params.name} yes={params.yes} force={params.force} "
                f"verbose={params.verbose} quiet={params.quiet}")

    @cmds.register("grp.sub", schema=PlainSchema, permission="", scope="all",
                   description="子命令")
    async def _sub(params, packet):
        return f"sub yes={params.yes}"

    async def ask(text: str) -> str:
        result = await cmds.handle(MessageEvent(
            message_type="private", user_id=SUPER, message=text))
        if isinstance(result, tuple):
            return result[0]
        return str(result or "")

    text = await ask("/demo a -y")
    check("命令里 -y 生效", "yes=True" in text, text[:120])
    text = await ask("/demo a -f")
    check("命令里 -f 生效", "force=True" in text, text[:120])
    text = await ask("/demo a -v")
    check("命令里显式 -v 生效", "verbose=True" in text, text[:120])
    text = await ask("/demo a --force=false")
    check("--force=false 仍然可用", "force=False" in text, text[:120])
    text = await ask("/grp sub a -y")
    check("子命令的 Schema 同样支持简写", "sub yes=True" in text, text[:120])

    help_text = await ask("/help demo")
    check("帮助的键值传参同时给出简写与长写法",
          "[-f|--force=False]" in help_text, help_text[:400])
    check("帮助的键值传参显示 [-v|--verbose=<值>]",
          "[-v|--verbose=<值>]" in help_text, help_text[:400])
    check("帮助参数表标注简写", "简写：-y" in help_text, help_text[:400])
    check("关闭简写的字段不出现 [-q|…] / 简写标注",
          "[-q|" not in help_text and "简写：-q" not in help_text, help_text[:300])

    help_amb = await ask("/help grp sub")
    check("子命令帮助也显示简写", "简写：-f" in help_amb, help_amb[:300])


async def main() -> int:
    test_field_and_map()
    test_validation()
    await test_end_to_end()
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
