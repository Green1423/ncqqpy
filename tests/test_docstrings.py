"""docstring 规范检查（PEP 257 + Google 风格）。

约定见 `docs/contributing/docstrings.md`。检查分四层：

1. **存在性**：`napcat/` 的公开模块有模块 docstring，公开类 / 模块级函数有 docstring；
2. **摘要行**：非空、不超过 120 字、不以空行开头；
3. **段落格式**：只允许 `Args` / `Returns` / `Raises` / `Attributes` / `Example` /
   `Note` 等白名单标题，顺序固定；`Args:` 每行必须是 `名称: 说明`；
4. **核心 API**：`CORE_API` 里列出的公开方法必须带 `Args:`，且每个形参都在
   `Args:` 里出现（`self` / `cls` 除外）。

私有函数（`_name`）只要求摘要行与段落格式（若写了段落）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAPCAT = ROOT / "napcat"

PASSED = 0
FAILED = 0

#: 承诺完整 docstring 的模块（内部工具模块如 qr_text 只做格式检查）
PUBLIC_MODULES = {
    "api", "client", "command", "config", "errors", "events", "file_manager",
    "launcher", "message", "models", "permission", "plugin", "registry",
    "store", "watchdog", "deps",
}

#: 必须写全 Args 的核心公开 API（`模块:限定名`）
CORE_API = [
    "command:CommandPacket.arg",
    "command:CommandPacket.opt",
    "command:CommandSchema.validate",
    "command:CommandManager.register",
    "command:CommandManager.find_command",
    "command:CommandManager.handle",
    "plugin:PluginBase.register_command",
    "plugin:PluginBase.on_message",
    "plugin:PluginBase.schedule",
    "plugin:PluginBase.database",
    "permission:PermissionManager.check",
    "permission:PermissionManager.check_role",
    "events:EventBus.on",
    "events:TimerManager.every",
    "config:ConfigManager.get",
    "config:ConfigManager.set",
    "store:Database.execute",
    "store:Database.query",
    "store:Database.scalar",
    "store:QuerySet.filter",
    "store:QuerySet.get",
    "store:QuerySet.create",
    "store:Model.save",
    "store:Model.delete",
]

SECTION_ORDER = {"Args": 1, "Returns": 2, "Yields": 2, "Raises": 3,
                 "Attributes": 4, "Example": 5, "Examples": 5,
                 "Note": 6, "Notes": 6, "Warns": 7}
SECTION_RE = re.compile(r"^([A-Z][A-Za-z ]+):$")
ARG_RE = re.compile(r"^\s*(\*\*)?([A-Za-z_][\w]*|kwargs)\s*:\s*\S")


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def iter_nodes(tree):
    """遍历类 / 函数（含方法），产出 (限定名, 节点)。"""
    def walk(node, prefix=""):
        for child in getattr(node, "body", []):
            if isinstance(child, ast.ClassDef):
                yield prefix + child.name, child
                yield from walk(child, prefix + child.name + ".")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield prefix + child.name, child
    yield from walk(tree)


def section_lines(doc: str) -> list[tuple[str, list[str]]]:
    """把 docstring 拆成 (标题, 正文行)；摘要在 ("", [...])。"""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in doc.splitlines():
        m = SECTION_RE.match(line.strip())
        if m:
            out.append((m.group(1), []))
        else:
            out[-1][1].append(line)
    return out


def check_existence():
    print("\n[1] 模块 / 公开类 / 公开函数都有 docstring")
    missing = []
    for p in sorted(NAPCAT.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        if p.stem in PUBLIC_MODULES and not ast.get_docstring(tree):
            missing.append(f"{p.name}（模块）")
        if p.stem not in PUBLIC_MODULES:
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                if not ast.get_docstring(node):
                    missing.append(f"{p.name}:{node.name}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and not node.name.startswith("_"):
                if not ast.get_docstring(node):
                    missing.append(f"{p.name}:{node.name}()")
    check("公开 API 的 docstring 无缺失", not missing, "; ".join(missing[:8]))


def check_format():
    print("\n[2] 摘要与段落格式")
    bad_summary, bad_section, bad_args, bad_order = [], [], [], []
    for p in sorted(NAPCAT.glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for qualname, node in iter_nodes(tree):
            doc = ast.get_docstring(node)
            if not doc:
                continue
            lines = doc.splitlines()
            summary = lines[0].strip() if lines else ""
            where = f"{p.name}:{qualname}"
            if not summary or len(summary) > 120:
                bad_summary.append(f"{where}（{len(summary)} 字）")
            sections = section_lines(doc)
            seen = []
            for title, body in sections:
                if not title:
                    continue
                if title not in SECTION_ORDER:
                    bad_section.append(f"{where}: {title}")
                    continue
                seen.append(SECTION_ORDER[title])
                if title == "Args":
                    for line in body:
                        if not line.strip():
                            continue
                        if ARG_RE.match(line):
                            continue
                        # 续行（比条目再缩进一级）允许；其余都是格式错误
                        if line.startswith("        "):
                            continue
                        bad_args.append(f"{where}: {line.strip()[:40]}")
            if seen != sorted(seen):
                bad_order.append(where)
    check("摘要行非空且 ≤ 120 字", not bad_summary, "; ".join(bad_summary[:6]))
    check("段落标题在白名单内", not bad_section, "; ".join(bad_section[:6]))
    check("Args 每行是「名称: 说明」", not bad_args, "; ".join(bad_args[:6]))
    check("段落顺序（Args → Returns → Raises → …）", not bad_order,
          "; ".join(bad_order[:6]))


def check_core_api():
    print("\n[3] 核心 API 的 Args 覆盖")
    missing_doc, missing_args, missing_param = [], [], []
    for entry in CORE_API:
        mod, qualname = entry.split(":")
        path = NAPCAT / f"{mod}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = dict(iter_nodes(tree))
        node = nodes.get(qualname)
        if node is None:
            missing_doc.append(f"{entry}（找不到）")
            continue
        doc = ast.get_docstring(node) or ""
        if "Args:" not in doc:
            missing_args.append(entry)
            continue
        args_body = []
        for title, body in section_lines(doc):
            if title == "Args":
                args_body = body
                break
        text = "\n".join(args_body)
        params = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
        params += [a.arg for a in node.args.kwonlyargs]
        for name in params:
            if not re.search(rf"(^|\n)\s*(\*\*)?{re.escape(name)}\s*:", text):
                missing_param.append(f"{entry}（缺 {name}）")
    check("CORE_API 全部存在", not missing_doc, "; ".join(missing_doc[:6]))
    check("CORE_API 都有 Args:", not missing_args, "; ".join(missing_args[:6]))
    check("Args 覆盖全部形参", not missing_param, "; ".join(missing_param[:6]))


def main() -> int:
    check_existence()
    check_format()
    check_core_api()
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
