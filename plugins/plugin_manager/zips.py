"""上传 zip 的格式检查与安全解压。

用户上传的 zip 是**不可信输入**：它会被解压到 `plugins/` 下，而且里面的
`requirements.txt` 还会触发 pip 安装。所以这里的检查分两层：

1. **结构**（这个包是不是一个插件）——zip 里**只能有一个顶层文件夹**，
   **文件夹名就是插件名**，代码全在这个文件夹里（`__init__.py` + 其他模块 +
   可选 `README.md` / `requirements.txt`）；根目录不允许直接放文件、也不允许
   第二个顶层文件夹。`__MACOSX/`、`.DS_Store` 这类压缩工具垃圾会被忽略，
   不算「第二个文件夹」；
2. **安全**（这个包能不能落盘）——见下面的清单，每条都以人话拒绝：

| 检查 | 拒绝的东西 |
|------|-----------|
| 大小 | 压缩包本身 > `max_zip_bytes` |
| zip 有效性 | 不是 zip、CRC 对不上（`testzip`）、条目被加密 |
| 成员名 | 绝对路径、盘符（`C:`）、`..`、反斜杠绕过、NUL |
| 特殊文件 | 符号链接、设备/管道/套接字（按 `external_attr` 的高 16 位判 mode） |
| zip 炸弹 | 解压总量 > `max_uncompressed_bytes`、单文件 > `max_file_bytes`、文件数 > `max_files` |
| 噪声 | 跳过 `__MACOSX/` 与 `.DS_Store`（不算失败，只记提示） |

⚠️ Python 3.12+ 的 `tarfile` 有 `data` 过滤器，**`zipfile` 没有**（3.14 仍然
没有 `filter=` 参数），所以这里不能照抄 tarball 那条路，必须自己逐个成员校验；
落盘也**逐个成员自己写**，不用 `extractall` —— 校验过的东西，写入路径也要显式
且可控（同时也把实际写入字节再按上限卡一次）。

目录条目（`foo/`）只参与名字检查，不产生文件；`data/`、`tests/` 这些是否同步
由 installer 的 `copy_plugin_tree` 负责，这里不越权过滤。
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .installer import InstallError

#: 顶层压缩工具的噪声目录/文件：跳过（不算失败）
_IGNORED_TOP = ("__MACOSX",)
_IGNORED_NAMES = (".DS_Store", "Thumbs.db")

#: 盘符（`C:` / `d:/x`）
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
#: 文件权限位（`external_attr` 高 16 位），只放行普通文件/目录/未知(0)
_MODE_MASK = 0o170000
_MODE_FILE = 0o100000
_MODE_DIR = 0o040000
_MODE_SYMLINK = 0o120000


class ZipError(InstallError):
    """zip 包不可用（不是 zip / 结构不对 / 含危险条目 / 超过上限）。"""


@dataclass(frozen=True)
class ZipLimits:
    """解压上限（全部来自 `config.json`，缺省值见 `plugin_manager` 配置模板）。"""

    max_zip_bytes: int = 20 * 1024 * 1024
    max_uncompressed_bytes: int = 100 * 1024 * 1024
    max_file_bytes: int = 10 * 1024 * 1024
    max_files: int = 2000

    def clamp(self) -> "ZipLimits":
        """把配置里的非法值（0/负数）夹到「至少 1」，避免上限失效。"""
        return ZipLimits(
            max_zip_bytes=max(1, int(self.max_zip_bytes or 1)),
            max_uncompressed_bytes=max(1, int(self.max_uncompressed_bytes or 1)),
            max_file_bytes=max(1, int(self.max_file_bytes or 1)),
            max_files=max(1, int(self.max_files or 1)),
        )


@dataclass
class ZipPackage:
    """解压好的上传包。"""

    #: 插件包根（= 解压目录 / 顶层文件夹）
    package_root: Path
    #: 包内的文件（相对 `package_root`，已排掉噪声）
    files: list[str] = field(default_factory=list)
    #: 顶层文件夹名（**就是插件名**）
    folder: str = ""
    #: 跳过/处理过的提示（给回执用）
    warnings: list[str] = field(default_factory=list)
    #: 压缩包 sha256（记账用）
    sha256: str = ""


def safe_member_name(name: str) -> str:
    """把 zip 成员名归一成安全的相对路径；不安全就抛 `ZipError`。

    不安全的判据（每条都给得出人话）：
    - 空名字 / 含 NUL；
    - `\\` 被当成路径分隔符（Windows 造的包会把 `..\\x` 藏起来）；
    - 绝对路径（`/x`、`\\\\server\\share`）；
    - 盘符（`C:/x`）；
    - 路径里出现 `..`（哪怕在中间：`a/../b` 一样能跳出去）。
    """
    text = str(name or "")
    if not text:
        raise ZipError("压缩包里有一个空文件名")
    if "\x00" in text:
        raise ZipError("压缩包里有文件名含 NUL 字节")
    text = text.replace("\\", "/")
    if text.startswith("/"):
        raise ZipError(f"压缩包里有绝对路径：{name!r}")
    if _DRIVE_RE.match(text):
        raise ZipError(f"压缩包里有盘符路径：{name!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ZipError(f"压缩包里有路径穿越：{name!r}")
    return "/".join(parts)


def _member_mode(info: zipfile.ZipInfo) -> int:
    """成员的文件类型（高 16 位 mode）；Windows 造的包常为 0（未知，放行）。"""
    return (info.external_attr >> 16) & _MODE_MASK


def inspect_zip(raw: bytes, *, limits: ZipLimits
                ) -> tuple[list[zipfile.ZipInfo], list[str], list[str], str]:
    """检查 zip 并返回 (要解压的成员, 归一后的相对名, 提示, 顶层文件夹名)。

    `raw` 必须已经在调用方按 `max_zip_bytes` 卡过；这里再兜一次底。
    真正的解压前就把所有危险项挡下来（**一个文件都不落盘**）。
    """
    limits = limits.clamp()
    if not raw:
        raise ZipError("文件是空的")
    if len(raw) > limits.max_zip_bytes:
        raise ZipError(
            f"压缩包太大（{len(raw) / 1024 / 1024:.1f} MB，"
            f"上限 {limits.max_zip_bytes / 1024 / 1024:.0f} MB）")
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise ZipError("这不是 zip 压缩包（或文件已损坏）；请重新打包后再传")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ZipError(f"zip 打不开：{exc}") from exc

    warnings: list[str] = []
    members: list[zipfile.ZipInfo] = []
    names: list[str] = []
    seen_files: set[str] = set()
    total = 0
    count = 0
    try:
        infos = archive.infolist()
        if not infos:
            raise ZipError("压缩包是空的")
        for info in infos:
            clean = safe_member_name(info.filename)
            if not clean:
                continue
            head = clean.split("/", 1)[0]
            if head in _IGNORED_TOP:
                warnings.append(f"已跳过 {head}/（压缩工具附带的元数据）")
                continue
            if clean.rsplit("/", 1)[-1] in _IGNORED_NAMES:
                continue
            if info.flag_bits & 0x1:
                raise ZipError(f"压缩包里的 {clean} 被加密了，无法解压")
            mode = _member_mode(info)
            if mode == _MODE_SYMLINK:
                raise ZipError(f"压缩包里有符号链接：{clean}（不允许）")
            if mode not in (0, _MODE_FILE, _MODE_DIR):
                raise ZipError(f"压缩包里有特殊文件：{clean}（不允许）")
            if info.is_dir():
                members.append(info)          # 只为建目录，不算文件数
                names.append(clean)
                continue
            if info.file_size > limits.max_file_bytes:
                raise ZipError(
                    f"压缩包里的 {clean} 太大（{info.file_size / 1024 / 1024:.1f} MB，"
                    f"单文件上限 {limits.max_file_bytes / 1024 / 1024:.0f} MB）")
            if clean in seen_files:
                # 同名文件后写覆盖先写：检查看到的是前者、落盘的是后者，
                # 这种「内容与检查不一致」直接拒掉（也避免解压结果不确定）。
                # 只针对文件：重复的目录条目在 zip 里很常见，不构成歧义。
                raise ZipError(f"压缩包里有两个同名文件：{clean}")
            seen_files.add(clean)
            count += 1
            if count > limits.max_files:
                raise ZipError(f"压缩包里的文件太多（超过 {limits.max_files} 个）")
            total += info.file_size
            if total > limits.max_uncompressed_bytes:
                raise ZipError(
                    "解压后总量超过上限"
                    f"（{limits.max_uncompressed_bytes / 1024 / 1024:.0f} MB），"
                    "疑似 zip 炸弹")
            members.append(info)
            names.append(clean)

        if count == 0:
            raise ZipError("压缩包里没有文件")

        # ---- 结构：只能有一个顶层文件夹，文件夹名就是插件名 ----
        tops = {name.split("/", 1)[0] for name in names if name}
        if len(tops) != 1:
            shown = "、".join(sorted(tops)[:4])
            raise ZipError(
                "压缩包根目录只能有一个文件夹（zip 里放一个 `<插件名>/` 目录，"
                f"代码都放里面）；现在根目录有：{shown or '（空）'}")
        folder = tops.pop()
        root_files = sorted({name for name in names if "/" not in name
                             and name != folder})
        if root_files:
            raise ZipError(
                "压缩包根目录不允许直接放文件："
                + "、".join(root_files[:4])
                + "；请把它们放进 `<插件名>/` 文件夹里")
        if not any(name.startswith(folder + "/") for name in names):
            # 只剩顶层条目：根上放的是文件（或空文件夹）
            raise ZipError(
                "zip 根目录只能有一个 `<插件名>/` 文件夹，代码要放进这个文件夹里；"
                f"现在根上只有 {folder!r}（不是文件夹，或文件夹里没有文件）")
        # CRC 校验：testzip 会真解压一遍（大小已卡过，不会失控）
        bad = archive.testzip()
        if bad is not None:
            raise ZipError(f"压缩包损坏：{bad} 校验失败")
    finally:
        archive.close()
    return members, names, warnings, folder


def extract_zip(raw: bytes, dest: Path, *, limits: ZipLimits) -> ZipPackage:
    """检查 + 解压到 `dest`，返回插件包根（`dest/<顶层文件夹>`）。

    解压是**逐成员手写**的（见模块文档）：校验过的相对名 + 实际写入字节再
    按 `max_file_bytes` 卡一次，绝不依赖 `extractall` 的路径清洗。
    """
    limits = limits.clamp()
    members, names, warnings, folder = inspect_zip(raw, limits=limits)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for index, info in enumerate(members):
            clean = names[index]
            if not clean:
                continue
            target = dest / clean
            try:
                target.resolve().relative_to(dest.resolve())
            except (ValueError, OSError) as exc:  # 兜底：名字校验之外的意外
                raise ZipError(f"拒绝解压越界条目：{clean}") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            try:
                with archive.open(info) as src, open(target, "wb") as out:
                    while True:
                        chunk = src.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limits.max_file_bytes:
                            raise ZipError(
                                f"解压 {clean} 时超过单文件上限"
                                f"（{limits.max_file_bytes / 1024 / 1024:.0f} MB）")
                        out.write(chunk)
            except zipfile.BadZipFile as exc:
                raise ZipError(f"解压 {clean} 失败：{exc}") from exc

    package_root = dest / folder
    files = sorted(
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*") if path.is_file())
    if not files:
        raise ZipError("压缩包里没有文件")
    return ZipPackage(
        package_root=package_root,
        files=files,
        folder=folder,
        warnings=warnings,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


__all__ = ["ZipError", "ZipLimits", "ZipPackage", "extract_zip", "inspect_zip",
           "safe_member_name"]
