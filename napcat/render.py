"""Chromium 无头渲染基础设施 —— **所有插件共用一份**。

2026-10-29 之前，这套东西在三个插件里各写了一遍（`ai_chat/render.py`、
`oj_bank/render.py`、`bupt_news/table_image.py`）：候选可执行名、`--no-sandbox`
那串启动参数、超时/杀进程、`file://` URI 拼装。三份的差别只有几个 flag，
但任何一份改了（比如 Chromium 新版把 `--headless` 换成 `--headless=new`），
另外两份不会跟着改 —— 这类"复制出来的基础设施"最后一定会分叉。

这里只放**与业务无关**的部分：找浏览器、拼命令行、跑一次、超时回收。
页面 HTML、CSS、Markdown 解析都不在这里（那是调用方的事）。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Sequence

__all__ = [
    "CHROMIUM_CANDIDATES", "RenderError", "find_chromium", "file_uri",
    "run_chromium", "CHECK_TIMEOUT",
]

#: 候选可执行名（先 PATH，再常见绝对路径）。顺序即优先级。
CHROMIUM_CANDIDATES = (
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome", "/snap/bin/chromium",
)

#: 杀进程之后等它退出的上限（秒）—— 回收失败不该盖住「超时」这个真实原因。
CHECK_TIMEOUT = 5.0


class RenderError(RuntimeError):
    """渲染失败（找不到浏览器、启动失败、超时）。调用方据此降级成文本。"""


def find_chromium(hint: str = "") -> Optional[str]:
    """找可执行的 Chromium；`hint` 优先（配置里可显式指定绝对路径或命令名）。"""
    if hint:
        hint = str(hint).strip()
        if not hint:
            return None
        if os.path.sep in hint:
            return hint if os.path.exists(hint) else None
        return shutil.which(hint)
    for name in CHROMIUM_CANDIDATES:
        got = shutil.which(name) if os.path.sep not in name else (
            name if os.path.exists(name) else None)
        if got:
            return got
    return None


def file_uri(path: str) -> str:
    """本地路径 → `file://` URI（中文/空格要转义，否则 Chromium 打不开）。"""
    return Path(os.path.abspath(path)).as_uri()


async def run_chromium(chrome: str, args: Sequence[str], *, profile: str = "",
                       timeout: float = 30.0, headless: str = "--headless=new",
                       stderr_to_null: bool = False) -> tuple[int, str, str]:
    """跑一次 Chromium，返回 `(returncode, stdout, stderr)`。

    参数一律走 argv（不拼 shell）。超时会被杀掉并抛 `RenderError` ——
    调用方要么降级成文本，要么重试，但**不要**把一个还在跑的 Chromium 留在后台。

    `headless` 可换（老的 `--headless` 与新版的 `--headless=new` 输出略有差别；
    需要精确像素/尺寸的调用方用老的）。
    `stderr_to_null=True` 时不收集 stderr（截图类调用嫌它吵）。
    """
    cmd = [chrome, headless, "--no-sandbox", "--disable-gpu",
           "--disable-dev-shm-usage", "--no-first-run",
           "--no-default-browser-check", "--disable-extensions",
           "--disable-background-networking"]
    if profile:
        cmd.append("--user-data-dir=" + profile)
    cmd.extend(str(a) for a in args)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL if stderr_to_null
            else asyncio.subprocess.PIPE)
    except (FileNotFoundError, OSError) as e:
        raise RenderError(f"启动 Chromium 失败：{e}") from e

    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=float(timeout))
    except (asyncio.TimeoutError, TimeoutError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass                        # 已经自己退出了
        try:
            await asyncio.wait_for(proc.wait(), timeout=CHECK_TIMEOUT)
        except Exception:               # noqa: BLE001 - 回收失败不该盖住超时本身
            pass
        raise RenderError(f"渲染超时（超过 {timeout:.0f} 秒）")

    return (proc.returncode or 0,
            (out_b or b"").decode("utf-8", "replace"),
            (err_b or b"").decode("utf-8", "replace"))


def chromium_hint_from_env() -> str:
    """环境变量 `CHROMIUM` 指定的路径（`config.json` 里没配时的兜底）。"""
    return os.environ.get("CHROMIUM", "") or os.environ.get("CHROME_PATH", "")


if sys.platform.startswith("linux"):      # pragma: no cover - 仅提示
    pass
