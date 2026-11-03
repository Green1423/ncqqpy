"""本地磁盘杂事：JSON 原子读写 + 目录清理（`napcat.persist`）。

这三件事原来在 bupt_news / byrdocs / ai_chat / oj_bank 里各写了一遍，且已经
开始分叉（一份不是原子写、一份没建父目录、`OSError` 吞不吞不一致）。这个套件
把四条契约钉住：

1. **写坏不可见**：先写 `.tmp` 再 `replace`，落盘后不留临时文件、父目录自动建；
2. **读坏不抛**：文件不存在 / 不是 JSON / 结构不对，一律回默认值；
3. **按数量淘汰**：`prune_keep` 只留最近 N 个，跳过正在用的 profile 目录与半成品；
4. **按引用淘汰**：`prune_unreferenced` 只删「不在 keep 里」的，且尊重后缀、
   主名比对、无条件删除、最小存活时间。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from napcat.persist import prune_keep, prune_unreferenced, read_json, write_json

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


def _touch(path: Path, mtime: float, text: str = "x") -> Path:
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_json(tmp: Path) -> None:
    print("\n[1] JSON 原子读写")
    target = tmp / "deep" / "dir" / "state.json"
    write_json(target, {"名字": "测试", "n": 1, "list": [1, 2]})
    check("父目录被自动创建", target.is_file())
    check("落盘后没有 .tmp 残留", not target.with_name("state.json.tmp").exists())
    check("读回来内容一致", read_json(target) == {"名字": "测试", "n": 1, "list": [1, 2]})
    check("紧凑输出（无多余空格）", ", " not in target.read_text(encoding="utf-8"))

    check("文件不存在 → 默认值", read_json(tmp / "nope.json", "D") == "D")
    (tmp / "bad.json").write_text("{not json", encoding="utf-8")
    check("内容坏了 → 默认值", read_json(tmp / "bad.json", None) is None)
    (tmp / "list.json").write_text("[1,2]", encoding="utf-8")
    check("顶层列表原样返回", read_json(tmp / "list.json") == [1, 2])
    check("默认值可以是任意对象", read_json(tmp / "nope.json", {}) == {})

    pretty = tmp / "pretty.json"
    write_json(pretty, {"a": 1}, indent=2)
    check("indent 生效", "\n" in pretty.read_text(encoding="utf-8"))


def test_prune_keep(tmp: Path) -> None:
    print("\n[2] 按数量淘汰（prune_keep）")
    d = tmp / "out"
    d.mkdir()
    for i in range(5):
        _touch(d / f"f{i}.png", 1000 + i)
    (d / "prof-abc").mkdir()
    (d / "prof-abc" / "Cache").write_text("x", encoding="utf-8")
    _touch(d / "half.part", 999)

    removed = prune_keep(d, keep=2, skip_prefix=("prof-",), skip_suffix=(".part",))
    left = sorted(p.name for p in d.iterdir())
    check("只留最近 2 个", left[:2] == ["f3.png", "f4.png"] or
          {"f3.png", "f4.png"} <= set(left), str(left))
    check("删掉了 3 个最旧的", len(removed) == 3, str(removed))
    check("profile 临时目录不被删", (d / "prof-abc" / "Cache").is_file())
    check("半成品不被删", (d / "half.part").is_file())

    check("目录不存在 → 空列表", prune_keep(tmp / "missing", keep=1) == [])
    check("条目不足时什么都不删", prune_keep(d, keep=99) == [])

    d2 = tmp / "out2"
    d2.mkdir()
    for i in range(3):
        _touch(d2 / f"f{i}", 100 + i)
    (d2 / "subdir").mkdir()
    _touch(d2 / "subdir" / "inner", 1)
    removed = prune_keep(d2, keep=1)
    check("目录条目按 mtime 一起参与淘汰", len(removed) == 3, str(removed))


def test_prune_unreferenced(tmp: Path) -> None:
    print("\n[3] 按引用淘汰（prune_unreferenced）")
    d = tmp / "cache"
    d.mkdir()
    for name in ("a.png", "b.png", "c.txt"):
        (d / name).write_text("x", encoding="utf-8")
    removed = prune_unreferenced(d, keep={"a.png"}, only_suffix=".png")
    check("只删 .png 且不在 keep 里", sorted(removed) == ["b.png"], str(removed))
    check("keep 里的留着", (d / "a.png").is_file())
    check("后缀不匹配的不动", (d / "c.txt").is_file())

    d2 = tmp / "cache2"
    d2.mkdir()
    (d2 / "img-1.png").write_text("x", encoding="utf-8")
    (d2 / "img-1.png.tmp").write_text("x", encoding="utf-8")
    (d2 / "other.png").write_text("x", encoding="utf-8")
    removed = prune_unreferenced(d2, keep={"other"}, only_suffix=".png",
                                 key=lambda n: n.split(".", 1)[0])
    check("key 按主名比对", sorted(removed) == ["img-1.png"], str(removed))
    check("别的文件不受影响", (d2 / "other.png").is_file())

    d3 = tmp / "cache3"
    d3.mkdir()
    (d3 / "x.png").write_text("x", encoding="utf-8")
    (d3 / "y.png.tmp").write_text("x", encoding="utf-8")
    removed = prune_unreferenced(d3, keep={"x"}, only_suffix=".png",
                                 key=lambda n: n.split(".", 1)[0],
                                 always_delete=".tmp")
    check("always_delete 无条件删（哪怕主名在 keep 里）",
          removed == ["y.png.tmp"], str(removed))
    check("keep 里的仍在", (d3 / "x.png").is_file())

    d4 = tmp / "cache4"
    d4.mkdir()
    (d4 / "fresh.png").write_text("x", encoding="utf-8")
    removed = prune_unreferenced(d4, keep=set(), only_suffix=".png", min_age=3600)
    check("比 min_age 新的不动", removed == [] and (d4 / "fresh.png").is_file())
    removed = prune_unreferenced(d4, keep=set(), only_suffix=".png", min_age=0)
    check("min_age=0 时照删", removed == ["fresh.png"], str(removed))
    check("目录不存在 → 空列表", prune_unreferenced(tmp / "missing", keep=set()) == [])


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_json(tmp)
        test_prune_keep(tmp)
        test_prune_unreferenced(tmp)
    print(f"\n{'=' * 44}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
