"""重定向通道：别的插件把能力交给 AI 使用（`napcat.redirect` + ai_chat 的桥）。

跑法：
    cd /ncqqpy && ./venv-arm/bin/python -B tests/test_redirects.py

卡的是这条通道的**语义**，不是实现细节：
谁注册、暴露成什么名字、过滤口径、失败怎么回到模型、卸载后会不会留死工具。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP_DIR = ROOT / "tests" / ".test-tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)   # 新 clone 里没有它（.gitignore 排除）

from napcat.redirect import Redirect, RedirectRegistry                     # noqa: E402
from plugins.ai_chat.tools import (ToolContext, ToolRegistry,      # noqa: E402
                                   command_tool_schema, register_redirect_tools)

PASSED = 0
FAILED = 0
FAILURES: list[str] = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  ❌ {name} — {detail}")


class _FieldBase:
    def __init__(self, description="", required=False, default=None, choices=None):
        self.description = description
        self.required = required
        self.default = default
        self.choices = choices or []


# ⚠️ 类名必须与框架一致（`StrField` / `IntField` / `ChoiceField`）：
# 桥是按**类名**翻译 JSON 类型的，替身起了别名就测不出真实映射了。
class StrField(_FieldBase):
    pass


class IntField(_FieldBase):
    pass


class ChoiceField(_FieldBase):
    pass


class _Schema:
    def __init__(self, fields):
        self._fields = fields

    def get_fields(self):
        return self._fields


class _Handler:
    """命令 handler 的最小替身（只实现重定向桥用到的字段）。"""

    def __init__(self, name, schema=None, description="", disabled=False,
                 permission="", scope="all", plugin=""):
        self.name = name
        self.plugin = plugin
        self.schema = schema
        self.description = description
        self.disabled = disabled
        self.permission = permission
        self.scope = scope


class _Cmds:
    def __init__(self, handlers=None):
        self.handlers = {h.name: h for h in (handlers or [])}

    def find_command(self, target):
        got = self.handlers.get(target)
        return (target, got) if got else None


def _ctx(**kw):
    base = dict(session_id="s1", user_id=20001, nickname="A",
                chat_kind="group", chat_id=100000002, group_id=100000002)
    base.update(kw)
    return ToolContext(**base)


# ==================== 1. 注册表：只收命令映射 ====================

def test_registry():
    print("\n[1] 注册表：重定向 = 命令映射")
    reg = RedirectRegistry()
    redirect = reg.register("byrdocs", "search", command="byrdocs.search")
    check("注册成功", reg.get("byrdocs.search") is redirect, "")
    check("工具名合法", redirect.tool_name == "byrdocs_search", redirect.tool_name)
    check("命令行还原成用户手敲的样子",
          redirect.command_line() == "/byrdocs search", redirect.command_line())
    check("按工具名反查", reg.by_tool_name("byrdocs_search") is redirect, "")

    try:
        reg.register("byrdocs", "empty")
        check("没有 command 要抛错", False, "居然注册成功了")
    except ValueError as e:
        check("没有 command 要抛错", "没有 command" in str(e), str(e))

    # 归属校验靠**命令自己的 plugin 字段**（不是命名空间前缀）：
    # `bupt_news` 插件的命令叫 `buptnews.*`，前缀不同也必须能注册
    own = _Cmds([_Handler("buptnews.list", plugin="bupt_news")])
    ok = reg.register("bupt_news", "recent", command="buptnews.list", cmds=own)
    check("命名空间与插件名不同也能注册", ok.command == "buptnews.list", ok.command)
    check("工具名是 bupt_news_recent", ok.tool_name == "bupt_news_recent",
          ok.tool_name)

    # prompt_hint（何时该用）随重定向走，进入系统提示
    hinted = reg.register("demo", "hinted", command="demo.search",
                          description="搜东西", prompt_hint="用户明确要搜东西时才用")
    check("重定向带 prompt_hint", hinted.prompt_hint == "用户明确要搜东西时才用",
          hinted.prompt_hint)
    aliased = reg.register("demo", "aliased2", command="demo.search",
                           when="只在用户要搜东西时用")
    check("when= 是别名", aliased.prompt_hint == "只在用户要搜东西时用", "")

    other = _Cmds([_Handler("byrdocs.search", plugin="byrdocs")])
    try:
        reg.register("bupt_news", "x", command="byrdocs.search", cmds=other)
        check("不能映射别的插件的命令", False, "居然注册成功了")
    except ValueError as e:
        check("不能映射别的插件的命令", "别的插件" in str(e), str(e))

    try:
        reg.register("byrdocs", "带 空格", command="byrdocs.search")
        check("非法名字要抛错", False, "居然注册成功了")
    except ValueError as e:
        check("非法名字要抛错", "不合法" in str(e), str(e))

    # 此时表里有 byrdocs.search 与 bupt_news.recent（上面刚注册的）
    check("可按提供方排除",
          "byrdocs.search" not in [s.name for s in reg.list(exclude="byrdocs")],
          f"{[s.name for s in reg.list(exclude='byrdocs')]}")
    check("整插件撤销", reg.unregister_plugin("byrdocs") == 1, "")
    check("撤销后查不到", reg.by_tool_name("byrdocs_search") is None, "")


# ==================== 2. Schema 复用 ====================

def test_schema_from_command():
    print("\n[2] 工具参数从命令的 CommandSchema 翻译（不另写一份）")
    handler = _Handler("byrdocs.search", schema=_Schema({
        "keyword": StrField("搜索关键词", required=True),
        "page": IntField("第几页", default=1),
        "type": ChoiceField("类型", default="all", choices=["all", "book"]),
    }), description="搜资料")
    schema = command_tool_schema(handler)
    props = schema["properties"]
    check("字符串字段", props["keyword"]["type"] == "string", props["keyword"])
    check("整数字段", props["page"]["type"] == "integer", props["page"])
    check("可选值字段给出 enum", props["type"].get("enum") == ["all", "book"],
          props["type"])
    check("必填项取自 schema", schema.get("required") == ["keyword"],
          schema.get("required"))
    check("描述也带过来", props["keyword"]["description"] == "搜索关键词",
          props["keyword"])

    no_schema = command_tool_schema(_Handler("x.y"))
    check("没有 schema 的命令 → 空参数对象",
          no_schema == {"type": "object", "properties": {}}, no_schema)


# ==================== 3. 桥：命令不存在就不暴露 ====================

def test_bridge():
    print("\n[3] 桥：解析命令、过滤、委派标记")
    reg = RedirectRegistry()
    reg.register("byrdocs", "search", command="byrdocs.search", description="搜资料")
    reg.register("gone", "lost", command="gone.lost")
    handler = _Handler("byrdocs.search", schema=_Schema({
        "keyword": StrField("关键词", required=True)}), description="搜资料")
    tools = ToolRegistry()
    register_redirect_tools(tools, redirects=reg, cfg={"enabled": True},
                         cmds=_Cmds([handler]))
    ctx = _ctx()
    names = [t.name for t in tools.visible(ctx)]
    check("命令存在的重定向才暴露", names == ["byrdocs_search"], f"{names}")
    tool = tools.visible(ctx)[0]
    check("描述里点明等价命令", "/byrdocs search" in tool.description,
          tool.description)
    check("标记成委派型（命令自己回消息）", tool.delegates is True, "")

    # 命令被禁用 → 不暴露
    handler.disabled = True
    check("命令禁用后不暴露", tools.visible(ctx) == [], "")

    # 过滤口径
    handler.disabled = False
    def names_with(cfg):
        t = ToolRegistry()
        register_redirect_tools(t, redirects=reg, cfg=cfg, cmds=_Cmds([handler]))
        return [x.name for x in t.visible(ctx)]

    check("enabled=false 全关", names_with({"enabled": False}) == [], "")
    check("allow 只接名单", names_with({"allow": ["byrdocs.search"]})
          == ["byrdocs_search"], "")
    check("deny 排除", names_with({"deny": ["byrdocs.search"]}) == [], "")


# ==================== 4. 委派执行：交给 run_redirect ====================

async def test_delegated_call():
    print("\n[4] 调用：参数交给 run_redirect，结果由命令发（AI 不接收）")
    reg = RedirectRegistry()
    reg.register("byrdocs", "search", command="byrdocs.search")
    handler = _Handler("byrdocs.search", schema=_Schema({
        "keyword": StrField("关键词", required=True)}))
    tools = ToolRegistry()
    register_redirect_tools(tools, redirects=reg, cfg={"enabled": True},
                         cmds=_Cmds([handler]))
    seen = {}

    async def run_redirect(redirect, args):
        seen.update(name=redirect.name, args=args)
        return "📚 结果已由命令自己发出"

    ctx = _ctx()
    ctx.extras["run_redirect"] = run_redirect
    text, failed = await tools.call("byrdocs_search", {"keyword": "线代"}, ctx)
    check("参数原样交给执行器",
          seen == {"name": "byrdocs.search", "args": {"keyword": "线代"}}, f"{seen}")
    check("返回命令的文本（桥不吞）", "命令自己发出" in text and not failed, text)

    async def boom(redirect, args):
        raise RuntimeError("命令炸了")

    ctx.extras["run_redirect"] = boom
    text, failed = await tools.call("byrdocs_search", {"keyword": "x"}, ctx)
    check("执行异常 → 标记失败并说明", failed and "触发失败" in text, text)

    ctx.extras.pop("run_redirect")
    text, failed = await tools.call("byrdocs_search", {"keyword": "x"}, ctx)
    check("没有执行器时给明确说明", "不支持" in text, text)


async def test_rich_result_not_swallowed():
    """委派型重定向：命令返回 MessageChain 时必须原样发出，不能丢成空串。

    线上真实事故：`/buptnews get` 返回富文本（正文图片/表格），
    `_run_redirect_command` 把它当成"命令自己会发"直接 `return ""`，
    而这条链上根本没有框架的分发器 —— 用户最后收到的是占位符
    「（工具返回空结果）」。
    """
    from plugins.ai_chat.main import AiChatPlugin
    from napcat.message import MessageChain

    print("\n[4.5] 富文本结果不能被吞")
    chain = (MessageChain().text("正文第一段")
             .text("\n正文第二段")
             .image("https://example.com/a.png"))

    class _Cmds2:
        async def handle(self, ev):
            return chain

    plugin = AiChatPlugin.__new__(AiChatPlugin)
    plugin.cmds = _Cmds2()
    plugin._redirect_event = lambda target, meta, text: object()
    sent: list[list] = []

    async def fake_send(target, payload):
        sent.append(payload.build())

    plugin._send = fake_send
    redirect = Redirect(name="bupt_news.notice", command="buptnews.get", plugin="bupt_news")
    out = await AiChatPlugin._run_redirect_command(plugin, redirect, {"id": "1"},
                                                target=None, meta={})
    check("★ 命令的 MessageChain 被原样发出去",
          bool(sent) and sent[0][0]["data"]["text"].startswith("正文第一段"),
          str(sent)[:100])
    check("★ 图片段也保留了（没被压成字符串）",
          any(seg.get("type") == "image" for seg in sent[0]), str(sent[0])[:100])
    check("★ 返回空串（避免重复发送）", out == "", repr(out))

    # 空结果：工具层返回空串，不塞占位符（占位符只给模型看）
    reg = ToolRegistry()

    @reg.tool("empty_tool", description="什么都不返回")
    async def empty_tool(ctx) -> None:
        return None

    ctx = _ctx()
    text, failed = await reg.call("empty_tool", {}, ctx)
    check("★ 空结果返回空串（用户不会收到「工具返回空结果」）",
          text == "" and not failed, f"{text!r} failed={failed}")


# ==================== 5. PluginBase 入口 ====================

async def test_plugin_base_api():
    print("\n[5] PluginBase 入口")
    from napcat.plugin import PluginBase
    check("有 register_redirect", hasattr(PluginBase, "register_redirect"), "")

    class Mini(PluginBase):
        name = "mini"
        async def on_load(self):
            pass

    reg = RedirectRegistry()
    plugin = Mini.__new__(Mini)
    plugin.redirects = reg
    plugin.name = "mini"
    redirect = plugin.register_redirect("calc", command="mini.calc")
    check("注册进表", "mini.calc" in reg and redirect.command == "mini.calc", "")
    check("撤销", plugin.unregister_redirect("calc") is True and len(reg) == 0, "")


# ==================== main ====================

async def main() -> int:
    test_registry()
    test_schema_from_command()
    test_bridge()
    await test_delegated_call()
    await test_rich_result_not_swallowed()
    await test_plugin_base_api()
    print(f"\n{'=' * 46}")
    print(f"通过 {PASSED} / 失败 {FAILED}")
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
