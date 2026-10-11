"""安装 / 更新 / 卸载的落地实现：下载 → 校验 → 原子替换 → 记账。

设计要点（每一条都是为了「出错时本地还能跑」）：

- **暂存区先行**：源码包解压到 `.staging/`，确认里头真有插件（`__init__.py` /
  `main.py`、且 `name` 声明与目录名一致）之后才动 `plugins/`；
- **原子替换**：先把老目录整体挪进 `.trash/<name>-<时间戳>/`，再把新目录挪到位。
  任何一步失败都能挪回来 —— 不做「就地覆盖」，那是半旧半新的来源；
- **自己不可动**：`plugin_manager` 把自己排除在装卸范围外，防止运行中把自己删掉；
- **记账**（本插件 `data/installed.json` + 每个插件目录里的 `.ncqqpy-plugin.json`）：
  记下来源仓库、提交、ref、**每个文件的哈希**。更新时据此分辨「用户手改」与
  「上游改了」，用户改过的文件默认拒绝覆盖（`force` 才动）；
- **`data/` 永不删除**：运行时数据、`__pycache__`、`.git`、用户自己加的文件都不
  参与同步；卸载也是整体进回收站（可找回）而不是 `rm -rf`。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from napcat import PluginError

from .github import GithubClient, now_iso

#: 每个插件目录里的记账文件（跟着插件走，手工复制插件时也带着来源信息）
META_FILE = ".ncqqpy-plugin.json"
#: 全局记账文件，放在本插件自己的 data/ 下
INSTALLED_FILE = "installed.json"
#: 备份区 / 暂存区（都在插件目录下，`_`/`.` 开头不会被框架当成插件）
TRASH_DIR = ".trash"
STAGING_DIR = ".staging"

#: 不参与同步的目录名（运行时数据 / 开发产物）
SKIP_DIRS = {".git", ".github", "__pycache__", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", ".venv", "venv", ".idea", ".vscode", "node_modules",
             "tests", "test", "docs", "doc"}
#: 不参与同步的文件名（开发/发布文件，与运行无关）
SKIP_FILES = {".gitignore", ".gitattributes", ".gitmodules", ".editorconfig",
              ".ncqqpy-plugin.json", "license", "licence", "notice", "readme.md"}

#: 插件名 / 版本声明（用于校验「装进来的确实是这个插件」）
_NAME_RE = re.compile(r"^\s*name\s*[:=]\s*['\"]([A-Za-z0-9_.-]+)['\"]", re.M)
_VERSION_RE = re.compile(r"^\s*version\s*[:=]\s*['\"]([^'\"]+)['\"]", re.M)
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class InstallError(PluginError):
    """安装 / 更新 / 卸载过程中的可预期失败（文案可给超管看）。"""

    kind = "插件操作失败"


class SelfOperationError(InstallError):
    """想动插件管理器自己 —— 明确拒绝。"""

    def __init__(self, message: str = ""):
        super().__init__(message or "不能对 plugin_manager 自己执行这个操作")


@dataclass
class FilePlan:
    """一次更新的文件级计划（干跑与真跑共用同一份计算）。"""

    add: list[str] = field(default_factory=list)
    """新文件（上游有、本地没有）。"""
    update: list[str] = field(default_factory=list)
    """上游与本地都变了（内容不同）。"""
    keep: list[str] = field(default_factory=list)
    """内容一致，原样保留。"""
    remove: list[str] = field(default_factory=list)
    """上次装过、这次上游删掉的文件（跟着删）。"""
    local_modified: list[str] = field(default_factory=list)
    """用户手改过、且上游这次也改了 —— 默认拒绝覆盖，`force` 才动。"""
    local_deleted: list[str] = field(default_factory=list)
    """用户手删过、且上游这次改了 —— 默认拒绝恢复，`force` 才动。"""
    local_extra: list[str] = field(default_factory=list)
    """本地多出来的文件（不是上次装的）—— 永不删除。"""

    @property
    def conflicts(self) -> list[str]:
        """需要 `force` 才能继续的冲突项。"""
        return sorted(set(self.local_modified) | set(self.local_deleted))

    @property
    def changed(self) -> bool:
        return bool(self.add or self.update or self.remove)

    def summary(self) -> str:
        parts = [f"新增 {len(self.add)}", f"更新 {len(self.update)}",
                 f"删除 {len(self.remove)}", f"保留 {len(self.keep)}"]
        if self.local_extra:
            parts.append(f"本地保留 {len(self.local_extra)}")
        if self.conflicts:
            parts.append(f"冲突 {len(self.conflicts)}")
        return " · ".join(parts)


# ==================== 记账 ====================


@dataclass
class InstalledPlugin:
    """一条安装记录（`plugins/plugin_manager/data/installed.json` 的一项）。"""

    name: str
    repo: str = ""
    ref: str = ""
    sha: str = ""
    version: str = ""
    author: str = ""
    installed_at: str = ""
    updated_at: str = ""
    keep_data: bool = False
    source: str = "manifest"
    files: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "repo": self.repo, "ref": self.ref, "sha": self.sha,
            "version": self.version, "author": self.author,
            "installed_at": self.installed_at, "updated_at": self.updated_at,
            "keep_data": self.keep_data, "source": self.source,
            "files": dict(self.files),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "InstalledPlugin":
        files = raw.get("files")
        return cls(
            name=str(raw.get("name") or ""),
            repo=str(raw.get("repo") or ""),
            ref=str(raw.get("ref") or ""),
            sha=str(raw.get("sha") or ""),
            version=str(raw.get("version") or ""),
            author=str(raw.get("author") or ""),
            installed_at=str(raw.get("installed_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            keep_data=bool(raw.get("keep_data", False)),
            source=str(raw.get("source") or "manifest"),
            files={str(k): str(v) for k, v in (files or {}).items()},
        )


class InstalledStore:
    """`installed.json` 的读写（原子写：临时文件 + replace）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._items: dict[str, InstalledPlugin] = {}
        self.load()

    def load(self) -> dict[str, InstalledPlugin]:
        self._items = {}
        if not self.path.exists():
            return self._items
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._items                   # 记账坏了不该让插件起不来
        for item in (raw.get("plugins") or []):
            if not isinstance(item, dict):
                continue
            entry = InstalledPlugin.from_dict(item)
            if entry.name:
                self._items[entry.name] = entry
        return self._items

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "saved_at": now_iso(),
                   "plugins": [self._items[k].to_dict()
                               for k in sorted(self._items)]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, name: str) -> Optional[InstalledPlugin]:
        return self._items.get(name)

    def put(self, entry: InstalledPlugin) -> None:
        self._items[entry.name] = entry
        self.save()

    def drop(self, name: str) -> None:
        if self._items.pop(name, None) is not None:
            self.save()

    def all(self) -> dict[str, InstalledPlugin]:
        return dict(self._items)


# ==================== 文件工具 ====================


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_synced(rel: str) -> bool:
    """这个相对路径要不要参与同步（`data/` 之类的运行时文件一律不动）。"""
    parts = Path(rel).parts
    if not parts:
        return False
    for part in parts[:-1]:
        if part in SKIP_DIRS:
            return False
    if parts[-1].lower() in SKIP_FILES:
        return False
    if parts[-1].endswith((".pyc", ".pyo", ".orig", ".rej")):
        return False
    return True


def collect_files(root: Path, *, only_synced: bool = True) -> dict[str, str]:
    """遍历目录 → {相对路径: sha256}（默认只收参与同步的文件）。

    `data/` **在同步范围内**（哈希照算，这样首次接管时不会把它当垃圾），
    但 `count_plugin_files()` 会把它从「插件有多少文件」里排掉 ——
    它是运行时数据，不是插件代码。
    """
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if only_synced and not is_synced(rel):
            continue
        try:
            out[rel] = sha256_of(path)
        except OSError:
            continue
    return out


def count_plugin_files(files: dict[str, str]) -> int:
    """「这个插件有多少文件」—— 排掉 data/ 与开发目录（用户看到的那个数）。"""
    return sum(1 for rel in files if not _is_runtime_file(rel))


def _is_runtime_file(rel: str) -> bool:
    parts = Path(rel).parts
    return any(part in ("data", "__pycache__", "tests", "test", "docs", "doc")
               for part in parts)


def copy_plugin_tree(src: Path, dst: Path) -> int:
    """把插件源码从 `src` 拷到 `dst`，**只拷参与同步的文件**（返回文件数）。

    为什么要过滤而不是整棵 `copytree`：
    - `tests/`、`docs/`、`.gitignore`、`LICENSE` 与运行无关，装进 `plugins/`
      只会让目录变脏（`plugins/<名>/tests` 还会被 `unittest`/pytest 顺带收集）；
    - 之前安装是整棵拷、更新是按过滤后的文件列表算的，于是「装进来的文件」与
      「更新会碰的文件」是两套口径 —— 第一次更新就会把 tests/ 留下、README 删掉，
      半新半旧。这里统一成一套：**只有参与同步的文件会进 `plugins/`**。
    - `data/` 不在过滤之列（`is_synced` 放行），仓库自带的运行期文件照样装上。
    """
    files = [(Path(rel), src / rel) for rel in sorted(collect_files(src))]
    for rel, source in files:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return len(files)


def plan_update(new_files: dict[str, str], old_files: dict[str, str],
                local_files: dict[str, str]) -> FilePlan:
    """算出「上游新版本」相对「本地现状」该做什么。

    三份输入都是 {相对路径: sha256}：

    - `new_files`   解压出来的新版本；
    - `old_files`   上次安装时记录的哈希（没有记录 = 空字典）；
    - `local_files` 现在磁盘上的实际哈希（只含参与同步的文件）。

    判定规则（**用户的手改优先于上游**）：

    | 情形 | 处理 |
    |------|------|
    | 上游有、本地没有（上次也没装过） | `add` |
    | 上游有、本地有、内容不同 | `update` |
    | 上游有、本地有、内容相同 | `keep` |
    | 上游没有、上次装过、本地没动 | `remove` |
    | 上游改了、本地也改过（与上次记录都不同） | `local_modified` → 冲突 |
    | 上游改了、本地被删 | `local_deleted` → 冲突 |
    | 本地有、上次没装过（用户自己加的） | `local_extra` → 永不删除 |
    """
    plan = FilePlan()
    for rel, digest in sorted(new_files.items()):
        if rel not in local_files:
            if rel in old_files:
                plan.local_deleted.append(rel)      # 用户删过，上游又改了
            else:
                plan.add.append(rel)
            continue
        if local_files[rel] == digest:
            plan.keep.append(rel)
            continue
        if rel not in old_files or local_files[rel] == old_files[rel]:
            plan.update.append(rel)                 # 本地没动过 → 安全覆盖
        else:
            plan.local_modified.append(rel)         # 本地手改 + 上游也改 → 冲突
    for rel in sorted(old_files):
        if rel in new_files:
            continue
        if rel in local_files and old_files[rel] == local_files[rel]:
            plan.remove.append(rel)                 # 上游删了、本地没动 → 跟着删
        elif rel in local_files:
            plan.local_extra.append(rel)            # 本地改过 → 留着
    for rel in sorted(local_files):
        if rel not in new_files and rel not in old_files:
            plan.local_extra.append(rel)            # 用户自己加的文件，永不删
    return plan


def sniff_plugin_identity(root: Path) -> tuple[str, str]:
    """从插件源码里读出 `name` / `version`（校验「装的是不是这个插件」）。

    只看 `__init__.py`、`main.py`，最多扫 64KB —— 插件入口就在这两个文件里，
    全目录正则扫描既慢又容易被示例代码里的 `name = "xxx"` 骗到。
    """
    name = version = ""
    for rel in ("__init__.py", "main.py"):
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:65536]
        except OSError:
            continue
        if not name:
            found = _NAME_RE.search(text)
            if found:
                name = found.group(1)
        if not version:
            found = _VERSION_RE.search(text)
            if found:
                version = found.group(1)
    return name, version


def safe_extract(archive: Path, dest: Path) -> Path:
    """解压 tarball 到 `dest`，返回剥掉顶层目录后的仓库根。

    安全约束（源码包来自网络，绝不能让它写到目录外）：

    - 解压前先整体检查成员名，含链接 / 绝对路径 / 混入顶层目录外的条目就拒绝；
    - 解压时再用 `tarfile` 的 `data` 过滤器兜一层（Python 3.12+ 的标准防线）。
    """
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if not members:
                raise InstallError("源码包是空的")
            tops = {m.name.split("/", 1)[0] for m in members if m.name.strip("/")}
            if len(tops) != 1:
                raise InstallError(
                    f"源码包结构异常：顶层目录不唯一（{sorted(tops)[:3]}）")
            top = tops.pop()
            if top in ("", ".", ".."):
                raise InstallError("源码包的顶层目录名非法")
            for member in members:
                if member.name != top and not member.name.startswith(top + "/"):
                    raise InstallError(
                        f"源码包里混入了顶层目录外的条目：{member.name}")
                if member.islnk() or member.issym():
                    raise InstallError(f"源码包里含链接，已拒绝：{member.name}")
                if member.isdev():
                    raise InstallError(f"源码包里含设备文件，已拒绝：{member.name}")
            tar.extractall(dest, filter="data")
    except tarfile.TarError as exc:
        raise InstallError(f"源码包解压失败：{exc}") from exc
    root = dest / top
    if not root.is_dir():
        raise InstallError("源码包里没有顶层目录")
    return root


# ==================== 安装器 ====================


@dataclass
class InstallResult:
    """一次安装/更新的结果（报告给超管看的都是这里的字段）。"""

    name: str
    action: str                 # install / update / noop
    repo: str = ""
    ref: str = ""
    old_sha: str = ""
    new_sha: str = ""
    version: str = ""
    plan: Optional[FilePlan] = None
    backup: Optional[Path] = None
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.action != "noop"


class Installer:
    """把「列表文件里的一条记录」变成磁盘上的插件目录。"""

    def __init__(self, *, plugin_dir: Path, work_dir: Path, client: GithubClient,
                 store: InstalledStore, self_name: str = "plugin_manager",
                 logger=None):
        self.plugin_dir = Path(plugin_dir)
        self.work_dir = Path(work_dir)          # 本插件的 data/：记账 + 暂存
        self.client = client
        self.store = store
        self.self_name = self_name
        self.logger = logger
        self.staging_root = self.work_dir / STAGING_DIR
        self.trash_root = self.plugin_dir / TRASH_DIR

    # ---- 基本位置与守卫 ----

    def guard(self, name: str) -> None:
        """挡住对自己 / 非法名字的操作。"""
        if name == self.self_name:
            raise SelfOperationError(
                f"{name} 是插件管理器自己，不能在运行时装/卸自己"
                "（要更新它请在 shell 里 git pull 或手工替换目录后重启）")
        if not _VALID_NAME_RE.match(name or ""):
            raise InstallError(f"插件名非法：{name!r}")

    def local_dir(self, name: str) -> Path:
        return self.plugin_dir / name

    def local_meta(self, name: str) -> Optional[dict]:
        """读插件目录里的记账文件（手工复制的插件也带着它）。"""
        path = self.local_dir(name) / META_FILE
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def effective_record(self, name: str) -> Optional[InstalledPlugin]:
        """这个插件「从哪来」：优先全局记账，其次插件目录里的记账文件。

        只有目录里的记账文件时（用户手工拷进来的），文件列表也一并采用 ——
        它同样能分辨「用户手改」与「上游改了」。
        """
        record = self.store.get(name)
        if record is not None:
            return record
        raw = self.local_meta(name)
        if not raw:
            return None
        merged = InstalledPlugin.from_dict(raw)
        if not merged.name:
            merged.name = name
        merged.source = merged.source or "local"
        return merged

    # ---- 下载与校验 ----

    async def stage_async(self, entry, subdir: str = "") -> tuple[Path, str, str]:
        """下载 → 解压 → 定位插件根，返回 (插件根目录, 下载用的 ref, 提交号)。

        `ref` 为空时按仓库默认分支下（codeload 的 `HEAD`），返回值里统一写回
        `"HEAD"`；`提交号` 只在响应头带了才有（codeload 有时不给），
        拿不到就留空，绝不猜一个填进记账。

        解压目录留在 `.staging/` 下，由调用方决定搬走还是丢弃。
        """
        ref = (entry.ref or "").strip() or "HEAD"
        info = await self.client.fetch_archive(entry.repo, ref,
                                               self.staging_root / "archives")
        extract_dir = self.staging_root / f"{entry.name}-{int(time.time() * 1000)}"
        root = safe_extract(info.path, extract_dir)
        try:
            info.path.unlink()
        except OSError:
            pass
        if subdir:
            candidate = root / subdir.strip("/")
            if not candidate.is_dir():
                raise InstallError(f"仓库 {entry.repo} 里没有目录 {subdir!r}")
            root = candidate
        return root, ref, (info.sha or "")

    def verify(self, entry, root: Path, *, allow_mismatch: bool = False) -> list[str]:
        """校验解压出来的确实是这个插件（入口文件 + `name` 声明）。

        返回告警列表；不一致时直接抛错 —— 装错目录（把 ai_chat 装进
        bupt_news）比装不上更难排查。
        """
        notes: list[str] = []
        if not root.is_dir():
            raise InstallError("插件根目录不存在")
        if not ((root / "__init__.py").is_file() or (root / "main.py").is_file()):
            raise InstallError(
                f"仓库 {entry.repo} 里既没有 __init__.py 也没有 main.py，"
                "看起来不是 ncqqpy 插件包")
        declared, version = sniff_plugin_identity(root)
        if declared and declared != entry.name:
            message = (f"仓库里声明的插件名是 {declared!r}，与列表文件里的 "
                       f"{entry.name!r} 不一致")
            if allow_mismatch:
                notes.append(message + "（已 force 继续）")
            else:
                raise InstallError(message + "，请检查列表文件（或用 force 强制）")
        elif not declared:
            notes.append("入口文件里没有 name 声明，按列表文件名安装")
        if version:
            notes.append(f"版本 {version}")
        return notes

    # ---- 安装 / 更新 ----

    async def install(self, entry, *, force: bool = False, subdir: str = "",
                      replace: bool = True, keep_local_extra: bool = True
                      ) -> InstallResult:
        """安装或更新一个插件。

        - `force=True`：忽略「用户手改过」的冲突，也忽略名字不一致；
        - `replace=False`：已存在就报错（`install` 子命令用，避免误覆盖）；
        - 已存在且 `force` 时按「以本地为基准的更新」走，本地多余文件仍保留。
        """
        self.guard(entry.name)
        target = self.local_dir(entry.name)
        previous = self.effective_record(entry.name)
        exists = target.is_dir()

        if exists and not replace:
            if previous is None:
                raise InstallError(
                    f"plugins/{entry.name} 已经存在，但不是本管理器装的 ——"
                    "要接管请用 /plugin update，确认覆盖再加 force")
            raise InstallError(
                f"{entry.name} 已经装过了（更新用 /plugin update {entry.name}）")

        root, ref, archive_sha = await self.stage_async(entry, subdir=subdir)
        notes = self.verify(entry, root, allow_mismatch=force)

        old_files = dict(previous.files) if previous else {}
        local_files = collect_files(target) if exists else {}
        new_files = collect_files(root)
        plan = plan_update(new_files, old_files, local_files)

        if exists and plan.conflicts and not force:
            raise InstallError(
                "本地修改与上游更新冲突，已停止：" + ", ".join(plan.conflicts[:6])
                + ("…" if len(plan.conflicts) > 6 else "")
                + "（确认要丢弃本地修改就加 force；先备份成 .bak 更稳）")

        same_source = bool(previous and previous.repo == entry.repo
                           and (previous.ref or "") == (ref if ref != "HEAD" else ""))
        if exists and not plan.changed and previous is not None and same_source \
                and not force:
            shutil.rmtree(root.parent, ignore_errors=True)
            return InstallResult(name=entry.name, action="noop", repo=entry.repo,
                                 ref=ref, old_sha=previous.sha,
                                 new_sha=previous.sha, version=previous.version,
                                 plan=plan, notes=notes)

        # ---- 组装新目录 ----
        # 以「本地现状」为基线（首次安装 = 空目录），再把仓库里参与同步的文件
        # 覆盖上去。这样**没被覆盖的东西一律留着**：`data/` 等运行时文件、
        # 用户自己加的补丁文件、用户手改过而本次不该动的文件 —— 不需要逐类
        # 特判，也就不会出现「更新一次把 tests/ 留下来、README 删掉」这种半新半旧。
        staged_target = self.plugin_dir / f".new-{entry.name}"
        shutil.rmtree(staged_target, ignore_errors=True)
        if exists:
            shutil.copytree(target, staged_target, symlinks=False)
        else:
            staged_target.mkdir(parents=True)
        # 上次装的记账文件不跟着走（新的会在替换后重写）
        (staged_target / META_FILE).unlink(missing_ok=True)
        remove_set = set(plan.remove) | (set(plan.local_modified) if force else set())
        for rel in sorted(remove_set):
            victim = staged_target / rel
            if victim.is_file():
                victim.unlink()
        # 仓库里不存在的本地文件：`keep_local_extra=False` 时按「以仓库为准」清掉
        # （默认 True，即永远不动它们）
        if not keep_local_extra:
            for rel in plan.local_extra:
                victim = staged_target / rel
                if victim.is_file():
                    victim.unlink()
        copy_plugin_tree(root, staged_target)

        backup: Optional[Path] = None
        try:
            if exists:
                backup = self._to_trash(entry.name)
            os.replace(staged_target, target)
        except OSError as exc:
            shutil.rmtree(staged_target, ignore_errors=True)
            if backup is not None and not target.exists():
                shutil.move(str(backup), str(target))   # 换不动就退回旧的
            raise InstallError(f"替换插件目录失败：{exc}") from exc

        keep_data = bool(previous.keep_data if previous else entry.keep_data)
        record = self._build_record(entry, target, ref=ref, previous=previous,
                                    keep_data=keep_data, sha_hint=archive_sha)
        try:
            self.store.put(record)
        except OSError as exc:
            raise InstallError(f"写入安装记录失败：{exc}") from exc
        self._write_local_meta(target, record)
        self._cleanup_staging()

        return InstallResult(name=entry.name,
                             action="update" if exists else "install",
                             repo=entry.repo, ref=ref,
                             old_sha=(previous.sha if previous else ""),
                             new_sha=record.sha, version=record.version,
                             plan=plan, backup=backup, notes=notes)

    def _build_record(self, entry, root: Path, *, ref: str,
                      previous: Optional[InstalledPlugin], keep_data: bool,
                      sha_hint: str = "") -> InstalledPlugin:
        """按「装好之后的插件目录」生成记账（含全部文件哈希）。"""
        files = collect_files(root)
        name, version = sniff_plugin_identity(root)
        now = now_iso()
        sha = sha_hint
        if not sha and previous is not None:
            # 内容与来源都没变 → 沿用上次记录的提交，不要因为一次重新下载就抹掉
            same_files = previous.files == files
            same_source = previous.repo == entry.repo and (previous.ref or "") == (
                "" if ref == "HEAD" else ref)
            if same_files and same_source:
                sha = previous.sha
        return InstalledPlugin(
            name=entry.name,
            repo=entry.repo,
            ref="" if ref == "HEAD" else ref,
            sha=sha,
            version=version or entry.version,
            author=entry.author or (previous.author if previous else ""),
            installed_at=((previous.installed_at if previous else "") or now),
            updated_at=now,
            keep_data=keep_data,
            source="manifest",
            files=files,
        )

    def adopt(self, name: str, record: InstalledPlugin) -> None:
        """把一份「本地重建出来」的记账写回去（接管手工安装的插件）。"""
        self.store.put(record)
        target = self.local_dir(name)
        if target.is_dir():
            self._write_local_meta(target, record)

    def _write_local_meta(self, target: Path, record: InstalledPlugin) -> None:
        try:
            (target / META_FILE).write_text(
                json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as exc:
            if self.logger:
                self.logger.warning("[plugin_manager] 写 %s 失败：%s", META_FILE, exc)

    def _to_trash(self, name: str) -> Path:
        """把插件目录整体挪进 `.trash/<name>-<时间戳>/`，返回备份路径。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.trash_root / f"{name}-{stamp}"
        counter = 1
        while dest.exists():
            dest = self.trash_root / f"{name}-{stamp}-{counter}"
            counter += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.local_dir(name)), str(dest))
        return dest

    def _cleanup_staging(self) -> None:
        """清暂存区（解压目录 + 已解压的源码包）。"""
        for path in list(self.staging_root.glob("*")):
            if path.name in ("installed.json",):
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    path.unlink()
                except OSError:
                    pass

    # ---- 回收站 ----

    def trash_entries(self, limit: int = 50) -> list[dict]:
        """回收站内容（新的在前），供 `/plugin trash` 查看。"""
        out: list[dict] = []
        if not self.trash_root.is_dir():
            return out
        for path in sorted(self.trash_root.iterdir(), reverse=True):
            if not path.is_dir():
                continue
            files = [f for f in path.rglob("*") if f.is_file()]
            out.append({"name": path.name, "files": len(files),
                        "size": sum(f.stat().st_size for f in files)})
            if len(out) >= limit:
                break
        return out

    def purge_trash(self, keep: int = 0) -> tuple[int, int]:
        """清理回收站，返回 (删掉的目录数, 释放的字节数)。

        `keep=0` = 全清。回收站是「更新出事后的人工退路」，留着占空间、
        清了就没退路，所以由超管显式决定。
        """
        entries = sorted([p for p in self.trash_root.iterdir() if p.is_dir()]
                         if self.trash_root.is_dir() else [], reverse=True)
        removed = freed = 0
        for path in entries[max(0, keep):]:
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
            freed += size
        return removed, freed

    # ---- 卸载 ----

    def uninstall(self, name: str, *, keep_data: bool = True) -> dict:
        """卸载：目录整体进回收站（**不是删除**），并清掉记账。

        `keep_data=True`（默认）时把 `data/` 目录留在原地，重装即可续上；
        `keep_data=False`（命令里的 `--purge=true`）则连 `data/` 一起搬进回收站。
        两种都不删除任何文件。
        """
        self.guard(name)
        target = self.local_dir(name)
        record = self.effective_record(name)
        if not target.exists() and record is None:
            raise InstallError(f"没装过 {name}")
        had_data = (target / "data").is_dir()
        # `keep_data=True` 时把 `data/` 留在原地（重装即可续上），其余文件照旧进
        # 回收站；`False`（`--purge=true`）则连 `data/` 一起搬进回收站 ——
        # **无论哪种都不删除**：误删插件比装不上严重得多，回收站里能人工找回。
        kept_data_path: Optional[Path] = None
        backup: Optional[Path] = None

        if target.exists() and keep_data and had_data:
            backup = self._to_trash(name)
            kept_data_path = target
            kept_data_path.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup / "data"), str(kept_data_path / "data"))
        elif target.exists():
            backup = self._to_trash(name)

        self.store.drop(name)
        return {
            "name": name, "backup": backup, "had_data": had_data,
            "was_managed": record is not None,
            "repo": (record.repo if record else ""),
            "kept_data": kept_data_path,
        }
