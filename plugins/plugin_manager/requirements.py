"""插件 `requirements.txt` 的校验与自动安装。

上传的 zip 里可以带一份 `requirements.txt`；安装插件前会用它跑
`python -m pip install -r ...`（用**当前解释器** `sys.executable`，也就是机器人
自己的 venv —— 插件 import 的包必须落在同一个环境里）。

为什么这里要自己写解析而不是直接把文件丢给 pip：

- **拒绝递归/可编辑安装**：`-r other.txt`、`-e path` 会把「这个文件里写了什么」
  变成不可审计的东西（`-e` 还会直接执行构建脚本），而上传包本身已经是代码了；
- **只允许「包规格」行**：`name`、`name==1.2`、`name[extra]>=1,<2`、
  带环境标记 `; python_version<'3.12'`。要私有源/额外参数请写在
  `config.json` 的 `plugins.plugin_manager.requirements.pip_args`（运维可控）。

失败语义（已与用户确认）：pip 装失败 → **取消本次替换**，旧版本插件原样可用。
所以调用方必须在 swap **之前** await 安装，见 `main._run_upload` 的 `before_swap`。

安全边界：pip 安装的包在安装期就可以执行任意代码，所以这条链路只对
**私聊超管**开放（与 `/plugin upload` 一致），并且可用
`requirements.enabled=false` 整体关掉。
"""
from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .installer import InstallError

#: 一条允许的依赖规格：名字 + 可选 extras + 可选比较符链 + 可选环境标记。
#: 不放行 URL / 本地路径 / VCS 引用 —— 这些会让「文件里写了什么」难以审计。
_SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[(?P<extras>[A-Za-z0-9._,-]+)\])?"
    r"(?P<rest>\s*(?:[<>=!~]=?\s*[A-Za-z0-9.*+_-]+\s*,?\s*)*"
    r"(?:;\s*[^#]+)?)$")
#: 回执里 pip 输出的尾部（行数 / 字符数双限制）
_TAIL_LINES = 12
_TAIL_CHARS = 1600


class RequirementsError(InstallError):
    """requirements.txt 不合法，或 pip 安装失败。"""

    def __init__(self, message: str = "", *, output: str = ""):
        super().__init__(message)
        #: pip 的输出尾部（失败时给用户看）
        self.output = output


@dataclass
class RequirementsResult:
    """一次 requirements 安装的结果。"""

    ok: bool
    path: Path | None = None
    #: requirements.txt 里合法规格的行数
    count: int = 0
    #: 是否因为 `requirements.enabled=false` 而跳过
    skipped: bool = False
    #: pip 输出（已截断到尾部）
    output: str = ""
    #: 装了哪些包（规格行，给回执用）
    specs: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped:
            return "requirements.txt 存在，但自动安装已关闭（requirements.enabled=false）"
        if not self.ok:
            return "requirements.txt 安装失败"
        return f"requirements.txt 已安装（{self.count} 项）"


def tail(text: str, *, lines: int = _TAIL_LINES, chars: int = _TAIL_CHARS) -> str:
    """取输出的尾部（回执里只放尾部：pip 的进度条会把整条消息撑爆）。"""
    rows = [row for row in str(text or "").strip().splitlines() if row.strip()]
    kept = rows[-lines:]
    joined = "\n".join(kept)
    if len(joined) > chars:
        joined = "…" + joined[-chars:]
    return joined


def parse_requirements(path: Path) -> list[str]:
    """读并校验 `requirements.txt`，返回合法规格行（保持原顺序）。

    Raises:
        RequirementsError: 任意一行不合法（**整份拒绝**，不装一半）。
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RequirementsError(f"读不了 requirements.txt：{exc}") from exc
    specs: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-"):
            raise RequirementsError(
                f"requirements.txt 第 {lineno} 行以 `-` 开头：{line}\n"
                "不支持 -r / -e / --index-url 这类选项；额外参数请写进 "
                "config.json → plugins.plugin_manager.requirements.pip_args")
        found = _SPEC_RE.match(line)
        if not found:
            raise RequirementsError(
                f"requirements.txt 第 {lineno} 行格式不对：{line}\n"
                "只允许「包名[可选 extras][比较符版本][; 环境标记]」")
        specs.append(line)
    if not specs:
        raise RequirementsError("requirements.txt 里没有任何依赖")
    return specs


async def _default_runner(cmd: list[str], cwd: Path, timeout: float
                          ) -> tuple[int, str]:
    """真跑 pip（子进程 + 超时 kill），返回 (退出码, 合并输出)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError as exc:
        raise RequirementsError(f"启动 pip 失败：{exc}") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        raise RequirementsError(f"pip 安装超时（{timeout:.0f} 秒）")
    return int(proc.returncode or 0), (out or b"").decode("utf-8", "replace")


#: 测试注入点：`(cmd, cwd, timeout) -> (returncode, output)`
PipRunner = Callable[[list[str], Path, float], Awaitable[tuple[int, str]]]


def pip_command(specs_path: Path, *, extra_args: Optional[list[str]] = None) -> list[str]:
    """拼 pip 命令：当前解释器 + `-r` + 运维给的额外参数。

    `--no-input` 必加：超管不在现场，pip 一旦停下来等交互就是「卡死」。
    """
    cmd = [sys.executable, "-m", "pip", "install",
           "--disable-pip-version-check", "--no-input",
           "-r", str(specs_path)]
    for arg in (extra_args or []):
        text = str(arg).strip()
        if text:
            cmd.append(text)
    return cmd


async def install(root: Path, *, timeout: float = 300.0,
                  extra_args: Optional[list[str]] = None,
                  runner: Optional[PipRunner] = None,
                  logger=None) -> RequirementsResult:
    """安装插件包根下的 `requirements.txt`（没有就返回 None 由调用方跳过）。

    `runner` 可注入（测试用假 pip），签名见 `PipRunner`；返回的
    `RequirementsResult.ok` 为 False 时调用方**不能**替换插件目录。
    """
    path = Path(root) / "requirements.txt"
    if not path.is_file():
        return RequirementsResult(ok=True, skipped=True)
    specs = parse_requirements(path)
    cmd = pip_command(path, extra_args=extra_args)
    if logger:
        logger.info("[plugin_manager] 安装依赖：%s（%d 项）",
                    path, len(specs))
    try:
        code, output = await (runner or _default_runner)(
            cmd, Path(root), max(10.0, float(timeout or 300)))
    except RequirementsError:
        raise
    except Exception as exc:  # noqa: BLE001 - 子进程/平台差异，统一成人话
        raise RequirementsError(
            f"运行 pip 失败：{type(exc).__name__}: {exc}") from exc
    result = RequirementsResult(
        ok=code == 0, path=path, count=len(specs), specs=specs,
        output=tail(output))
    if logger:
        logger.info("[plugin_manager] pip 退出码 %s%s", code,
                    "" if code == 0 else f"，输出尾部：\n{result.output}")
    return result


__all__ = ["PipRunner", "RequirementsError", "RequirementsResult", "install",
           "parse_requirements", "pip_command", "tail"]
