"""多行字符串：`\"\"\"…\"\"\"` / `'''…'''` 作为命令参数。

契约：

1. 三引号块整体作为**一个参数**，内部换行保留；
2. 内容是**原样**的：不解析 `\\n` / `\\t` / `\\\\` 等转义，引号与反斜杠不动
   （写代码、正则、模板时不会被改坏）；
3. 块必须单独占一个参数（前后是空白或首尾）；
4. 未闭合 / 前后紧贴文字 → 报「用法不正确」，不是静默丢弃；
5. 普通引号（`"…"` / `'…'`）的行为不变。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from napcat.command import (                                  # noqa: E402
    CommandManager, CommandParser, CommandSchema, StrField, VariadicField,
)
from napcat.models import MessageEvent                        # noqa: E402
from napcat.permission import PermissionManager               # noqa: E402

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


def parse(text: str):
    return CommandParser(prefixes=["/"]).parse(text)


def test_parser():
    print("\n[1] 解析：块 = 一个参数，内容原样")
    pkt = parse('/demo """line1\nline2"""')
    check("双引号块保留换行",
          pkt is not None and pkt.args == ["line1\nline2"], repr(pkt))

    pkt = parse("/demo '''line1\nline2'''")
    check("三个单引号同样支持",
          pkt is not None and pkt.args == ["line1\nline2"], repr(pkt))

    pkt = parse(r'/demo """a\nb"""')
    check("块内 \\n 不被转义（保持字面反斜杠）",
          pkt is not None and pkt.args == [r"a\nb"], repr(pkt))

    pkt = parse(r'/demo """a\tb\\c"""')
    check("块内 \\t 与 \\\\ 原样保留",
          pkt is not None and pkt.args == [r"a\tb\\c"], repr(pkt))

    pkt = parse('/demo """他说 "你好"。"""')
    check("块内双引号无需转义",
          pkt is not None and pkt.args == ['他说 "你好"。'], repr(pkt))

    pkt = parse('/demo """a \'\'\' b"""')
    check("双引号块内可出现三个单引号",
          pkt is not None and pkt.args == ["a ''' b"], repr(pkt))

    pkt = parse("/demo '''a \"\"\" b'''")
    check("单引号块内可出现三个双引号",
          pkt is not None and pkt.args == ['a """ b'], repr(pkt))

    pkt = parse('/demo a """line1\nline2""" b')
    # 第二个 token 是合法名字 → 先作为子命令候选（命令无子命令时执行阶段回落为参数）
    got_args = ([pkt.subcommand] if pkt.subcommand else []) + list(pkt.args)
    check("块与普通参数混用、顺序不变",
          pkt is not None and got_args == ["a", "line1\nline2", "b"],
          repr(pkt))

    pkt = parse('/demo """a""" """b"""')
    check("多个块各自成为一个参数",
          pkt is not None and pkt.args == ["a", "b"], repr(pkt))

    pkt = parse('/demo """  x  """')
    check("块内首尾空格原样保留",
          pkt is not None and pkt.args == ["  x  "], repr(pkt))

    pkt = parse('/demo """a\r\nb"""')
    check("CRLF 归一为 LF",
          pkt is not None and pkt.args == ["a\nb"], repr(pkt))


def test_errors():
    print("\n[2] 写错时明确报错")
    pkt = parse('/demo """unclosed')
    check("未闭合判为 parse 错误",
          pkt is not None and pkt.illegal and pkt.illegal_kind == "parse"
          and "没有闭合" in pkt.illegal_reason, repr(pkt))

    pkt = parse('/demo x"""a"""')
    check("块前紧贴文字被判错",
          pkt is not None and pkt.illegal and "单独" in pkt.illegal_reason,
          repr(pkt))

    pkt = parse('/demo """a"""x')
    check("块后紧贴文字被判错",
          pkt is not None and pkt.illegal and "单独" in pkt.illegal_reason,
          repr(pkt))

    pkt = parse('/demo """x')
    check("错误原因包含闭合提示", "结束" in (pkt.illegal_reason if pkt else ""),
          repr(pkt))


async def test_end_to_end():
    print("\n[3] 命令执行：Schema 收到多行原文")

    class CodeSchema(CommandSchema):
        code = VariadicField(description="代码")

    class SaySchema(CommandSchema):
        text = StrField(required=True, max_length=4000)

    cmds = CommandManager(prefixes=["/"],
                          perm_manager=PermissionManager(superusers=[100000009]))

    @cmds.register("code", schema=CodeSchema, permission="", scope="all")
    async def _code(params, packet):
        return repr(params.code)

    @cmds.register("say", schema=SaySchema, permission="", scope="all")
    async def _say(params, packet):
        return params.text

    async def ask(text):
        result = await cmds.handle(MessageEvent(
            message_type="private", user_id=100000009, message=text))
        if isinstance(result, tuple):
            return result[0]
        return str(result or "")

    text = await ask('/code """#include <iostream>\nint main() {\n  return 0;\n}"""')
    check("变参收到单个带换行的代码块",
          "line" not in text and "include" in text and "\\n" in text,
          text[:160])
    check("代码里的换行没有被压成空格",
          "\n" in text.replace("\\n", "\n"), text[:160])

    text = await ask("/say '''第一行\n第二行'''")
    check("单值字段保留换行", text == "第一行\n第二行", repr(text))

    text = await ask('/code """unclosed')
    check("未闭合：用户收到用法提示",
          "用法不正确" in text and "没有闭合" in text, text[:200])

    text = await ask('/say "普通\\n引号"')
    check("普通引号仍然解析转义", text == "普通\n引号", repr(text))


async def main() -> int:
    test_parser()
    test_errors()
    await test_end_to_end()
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
