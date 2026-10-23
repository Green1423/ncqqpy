"""文档结构检查：链接、锚点、索引可达、篇幅上限。

文档按章节拆分后，**索引与链接就是结构**。这套检查防止：

1. 改了标题却忘了改链接（锚点失效）；
2. 新增文档没进索引（从 `docs/README.md` 走不到）；
3. 大文档重新长回来（单文件行数上限）；
4. 链接写成了旧路径（`usage.md` 之类已被拆掉的文件）。

跳过代码块与行内代码里的示例链接（`[文档](url)` 是占位符，不是真链接）。
"""
from __future__ import annotations

import re
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

PASSED = 0
FAILED = 0
MAX_LINES = 500

FENCE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def body(text: str) -> str:
    """去掉代码块与行内代码，避免把示例当链接/标题。"""
    return INLINE_CODE.sub("", FENCE.sub("", text))


def anchors(path: Path) -> set[str]:
    out: set[str] = set()
    text = path.read_text(encoding="utf-8")
    # 标题里可能有行内代码（如 `--force=true`），不能在解析锚点前剥掉内容
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        m = HEADING.match(line)
        if not m:
            continue
        raw = m.group(2).strip().lower().replace("`", "")
        slug = "".join(ch for ch in raw
                       if ch.isalnum() or ch in "-_ " or "\u4e00" <= ch <= "\u9fff")
        out.add(slug.replace(" ", "-"))
    return out


def loose(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def md_files() -> list[Path]:
    return sorted(DOCS.rglob("*.md"))


def all_md() -> list[Path]:
    files = md_files()
    files += [p for p in sorted(ROOT.glob("plugins/*/README.md"))]
    files += [p for p in sorted(ROOT.glob("tests/*.md"))]
    return files


def iter_links(path: Path):
    text = body(path.read_text(encoding="utf-8"))
    for m in LINK.finditer(text):
        target = m.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        if target in ("url", "..."):
            continue
        yield target


def check_links():
    print("\n[1] 相对链接与锚点")
    broken: list[str] = []
    bad_anchor: list[str] = []
    for path in all_md():
        for target in iter_links(path):
            path_part, _, anchor = target.partition("#")
            dest = path.parent / path_part if path_part else path
            if target.startswith("#") and not path_part:
                dest = path
            try:
                dest = dest.resolve()
            except OSError:
                broken.append(f"{path.relative_to(ROOT)} → {target}")
                continue
            if not dest.exists():
                broken.append(f"{path.relative_to(ROOT)} → {target}")
                continue
            if anchor and dest.suffix == ".md":
                got = anchors(dest)
                want = anchor.lower()
                ok = want in got or loose(want) in {loose(a) for a in got}
                if not ok and re.search(r"-\d+$", want):
                    ok = loose(re.sub(r"-\d+$", "", want)) in {loose(a) for a in got}
                if not ok:
                    bad_anchor.append(f"{path.relative_to(ROOT)} → {target}")
    check(f"相对链接全部可解析（{len(broken)} 处失效）", not broken,
          "; ".join(broken[:6]))
    check(f"锚点都能找到标题（{len(bad_anchor)} 处失效）", not bad_anchor,
          "; ".join(bad_anchor[:6]))


def check_reachable():
    print("\n[2] 索引可达性（从 docs/README.md 出发）")
    start = DOCS / "README.md"
    seen = {start.resolve()}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        for target in iter_links(cur):
            path_part = target.partition("#")[0]
            if not path_part.endswith(".md"):
                continue
            dest = (cur.parent / path_part).resolve()
            if dest.is_file() and dest not in seen and DOCS in dest.parents:
                seen.add(dest)
                queue.append(dest)
    files = {p.resolve() for p in md_files()}
    orphan = sorted(str(p.relative_to(ROOT)) for p in files - seen)
    check(f"docs 下全部 {len(files)} 个 Markdown 都能从索引走到", not orphan,
          "; ".join(orphan[:8]))


BANNED_PHRASES = ("旧实现", "此前", "曾经有过", "当时", "最初", "实现上",
                  "源码里", "设计取舍", "历史遗留")
FENCE_OPEN = re.compile(r"^```([^`]*)$")


def check_style():
    print("\n[4] 用户文档的写法（特性/用法，不写实现与历史）")
    roots = [DOCS / "guide", DOCS / "plugin-dev", DOCS / "plugins"]
    hits: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.md")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for word in BANNED_PHRASES:
                    if word in line:
                        hits.append(f"{path.relative_to(DOCS)}:{lineno}（{word}）")
                        break
    check("没有实现细节 / 历史口吻", not hits, "; ".join(hits[:8]))

    untagged: list[str] = []
    for path in sorted(DOCS.rglob("*.md")):
        in_fence = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("```"):
                if in_fence:
                    in_fence = False
                else:
                    info = line.lstrip()[3:].strip()
                    if not info:
                        untagged.append(str(path.relative_to(DOCS)))
                    in_fence = True
    check("代码块都带语言标记（编辑器可高亮）", not untagged,
          "; ".join(sorted(set(untagged))[:8]))


def check_layout():
    print("\n[3] 目录结构")
    check("docs/README.md 存在", (DOCS / "README.md").exists())
    dirs = {p.parent for p in md_files()
            if "archive" not in p.relative_to(DOCS).parts}
    missing = [str(d.relative_to(ROOT)) for d in dirs
               if d != DOCS and not (d / "README.md").exists()]
    check("每个文档目录都有 README.md 索引", not missing, "; ".join(missing[:6]))
    check("旧的大文档路径已不存在",
          not any((DOCS / n).exists() for n in
                  ("usage.md", "plugin-dev.md", "ai-chat.md", "bupt-news.md",
                   "byrdocs.md", "ncqq-watchdog.md", "dsh-internals-report.md")))
    long_docs = [(p, len(p.read_text(encoding="utf-8").splitlines()))
                 for p in md_files()
                 if "archive" not in p.relative_to(DOCS).parts
                 and len(p.read_text(encoding="utf-8").splitlines()) > MAX_LINES]
    check(f"单个文档 ≤ {MAX_LINES} 行", not long_docs,
          "; ".join(f"{p.relative_to(ROOT)}({n})" for p, n in long_docs[:5]))
    bad_h1 = []
    for p in md_files():
        heads = [l for l in body(p.read_text(encoding="utf-8")).splitlines()
                 if HEADING.match(l) and HEADING.match(l).group(1) == "#"]
        if p.name == "README.md" and len(heads) != 1:
            bad_h1.append(f"{p.relative_to(ROOT)}: {len(heads)} 个 H1")
    check("索引页各有一个 H1 标题", not bad_h1, "; ".join(bad_h1[:5]))


def main() -> int:
    check_links()
    check_reachable()
    check_layout()
    check_style()
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
