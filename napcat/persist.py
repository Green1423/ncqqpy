"""本地磁盘杂事 —— JSON 原子读写 + 目录清理（插件共用）。

这几件事原先在四个插件里各写了一遍，且**写法已经在分叉**：

- JSON 落盘：`bupt_news` 的三份状态库、`byrdocs` 的结果库、`ai_chat` 的三个
  索引、`plugin_manager` 的记账 —— 都是「先写 `.tmp` 再 `os.replace`」，
  但其中一份没有 `os.makedirs`，还有一份不是原子写；
- 目录清理：`ai_chat`、`oj_bank`、`bupt_news` 各有一份「只留最近 N 个」和
  「删掉不再被引用的文件」，连 `OSError` 吞不吞都不一致。

这里只收**机制**：怎么写不会写坏、怎么删不会删错。文件名规则、保留多少、
哪些算「仍在引用」由调用方给。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

__all__ = ["read_json", "write_json", "prune_keep", "prune_unreferenced"]


def read_json(path: str | os.PathLike, default=None):
    """读 JSON；文件不存在、读不了、内容坏了都返回 `default`（绝不抛）。

    状态文件坏掉不该让插件起不来 —— 这是所有调用方的共同口径（它们各自写的
    `try/except` 就是把这句话抄了几遍）。

    Args:
        path: JSON 文件路径。
        default: 读不出来时的返回值。

    Returns:
        解析出来的对象，或 `default`。
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: str | os.PathLike, payload, *, indent: Optional[int] = None) -> None:
    """原子写 JSON：先写 `<名>.tmp` 再 `os.replace`，中途断掉不会留半份文件。

    父目录不存在会自动建。`indent=None` 用紧凑分隔符（状态文件动辄几十 KB，
    缩进只是让人肉眼看，落盘体积翻倍不值）。

    Args:
        path: 目标文件路径。
        payload: 可 JSON 序列化的对象。
        indent: 缩进宽度；`None` 表示紧凑输出。

    Raises:
        OSError: 落盘失败（磁盘满、权限）—— 由调用方决定是告警还是忽略。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    separators = None if indent is not None else (",", ":")
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=indent,
                  separators=separators)
    os.replace(tmp, path)


def prune_keep(directory: str | os.PathLike, *, keep: int = 60,
               skip_prefix: Iterable[str] = (),
               skip_suffix: Iterable[str] = ()) -> list[str]:
    """目录里按 mtime **只留最近 `keep` 个**条目，返回被删掉的路径。

    用于「出图/下载目录别把磁盘写满」。`skip_prefix` 用来放过正在用的临时目录
    （Chromium 的 `prof-` / `ojprof-` profile），`skip_suffix` 放过半成品文件。

    目录不存在或读不了时安静返回空列表 —— 清理失败不该影响主流程。

    Args:
        directory: 要清理的目录。
        keep: 保留多少个（至少 1）。
        skip_prefix: 文件名以此开头的跳过（正在使用的临时目录）。
        skip_suffix: 文件名以此结尾的跳过（下载中的半成品）。

    Returns:
        被删除的路径（按「新 → 旧」顺序被淘汰的那批）。
    """
    prefixes = tuple(skip_prefix)
    suffixes = tuple(skip_suffix)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    entries: list[tuple[float, str]] = []
    for name in names:
        if prefixes and name.startswith(prefixes):
            continue
        if suffixes and name.endswith(suffixes):
            continue
        path = os.path.join(os.fspath(directory), name)
        try:
            entries.append((os.path.getmtime(path), path))
        except OSError:
            continue
    limit = max(1, int(keep))
    if len(entries) <= limit:
        return []
    entries.sort(reverse=True)
    removed: list[str] = []
    for _mtime, path in entries[limit:]:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            removed.append(path)
        except OSError:
            continue
    return removed


def prune_unreferenced(directory: str | os.PathLike, *, keep: Iterable[str],
                       key: Optional[Callable[[str], str]] = None,
                       only_suffix: str = "", always_delete: str = "",
                       min_age: float = 0.0) -> list[str]:
    """删掉目录里**不在 `keep` 里**的文件，返回被删掉的文件名。

    用于「缓存目录只保留仍被引用的文件」（图片缓存、表格图）。与 `prune_keep`
    的区别：那个按数量和 mtime 淘汰，这个按「谁还被引用」判定，没有数量上限。

    Args:
        directory: 要清理的目录。
        keep: 仍在引用的键集合。
        key: 文件名 → 键（默认用整个文件名）；缓存按主名比对时传 `split(".")`。
        only_suffix: 只处理这种后缀（空 = 全部）。
        always_delete: 这种后缀无条件删（下载中的 `.tmp`）。
        min_age: 只删「至少这么久没动过」的文件（秒）；防止与正在发送的文件抢。

    Returns:
        被删除的文件名列表。
    """
    resolve = key or (lambda name: name)
    wanted = {str(k) for k in keep if k}
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    now = time.time()
    removed: list[str] = []
    for name in names:
        if not (always_delete and name.endswith(always_delete)):
            if only_suffix and not name.endswith(only_suffix):
                continue
            if resolve(name) in wanted:
                continue
        path = os.path.join(os.fspath(directory), name)
        try:
            if not os.path.isfile(path):
                continue
            if min_age > 0 and now - os.path.getmtime(path) < min_age:
                continue
            os.remove(path)
            removed.append(name)
        except OSError:
            continue
    return removed
