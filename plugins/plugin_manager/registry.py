"""插件列表文件（manifest）的解析与校验。

列表文件放在 GitHub 仓库 `Green1423/ncqqpy-plugins` 的 `plugins.json`，
是插件管理器的**唯一可信来源**：哪些插件可用、从哪个仓库装、装缺省的哪条 ref。

格式（`version` 为格式版本，当前 1）::

    {
      "version": 1,
      "name": "ncqqpy 官方插件列表",
      "updated": "2026-10-10",
      "plugins": [
        {
          "name": "bupt_news",
          "repo": "Green1423/ncqqpy-plugin-bupt-news",
          "ref": "main",
          "description": "北邮校内通知推送",
          "version": "1.0.0",
          "tags": ["推送"],
          "data": true
        }
      ]
    }

字段语义：

- `name`   本地目录名兼插件标识（`plugins/<name>/`），**必填**，只允许字母数字与 `_-.`；
- `repo`   `owner/repo` 或任意 GitHub 链接（`git@github.com:...`、网页链接都认），**必填**；
- `ref`    分支 / tag / commit，缺省 = 仓库默认分支；
- 其余字段（`description` / `version` / `author` / `tags` / `data`）只是展示信息，
  缺了不影响安装；`data: true` 表示卸载时**默认保留**它的 `data/` 目录。

解析失败一律抛 `ManifestError`（人话说明第几条错在哪），绝不让半个列表生效。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from napcat.errors import PluginError

#: 合法插件名：目录名安全（不含 `/`、`..`、空格）
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
#: 合法 repo：`owner/name`，两段都只允许 GitHub 允许的字符
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

#: 本地默认的列表文件地址（config.json 里可改）
DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/Green1423/ncqqpy-plugins/main/plugins.json")


class ManifestError(PluginError):
    """插件列表文件不可用（拉不到、格式不对、条目非法）。"""

    kind = "插件列表不可用"


def valid_name(name: str) -> bool:
    """插件名是否合法（同时挡住路径穿越：`..` 与 `/` 都过不了正则）。"""
    return bool(NAME_RE.match(name or ""))


@dataclass
class PluginEntry:
    """列表文件里的一条插件记录。"""

    name: str
    repo: str
    ref: str = ""
    description: str = ""
    version: str = ""
    author: str = ""
    tags: list[str] = field(default_factory=list)
    keep_data: bool = False

    @property
    def ref_display(self) -> str:
        return self.ref or "默认分支"

    @classmethod
    def parse(cls, raw: Any, index: int) -> "PluginEntry":
        """解析一条记录；任何不合法都抛 `ManifestError`（带序号，便于改文件）。"""
        where = f"plugins[{index}]"
        if not isinstance(raw, dict):
            raise ManifestError(f"{where} 不是对象")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ManifestError(f"{where} 缺少 name")
        if not valid_name(name):
            raise ManifestError(
                f"{where} 的 name={name!r} 非法（只允许字母数字与 _ . -，首字符须为字母数字）")
        repo_raw = str(raw.get("repo") or raw.get("url") or "").strip()
        if not repo_raw:
            raise ManifestError(f"{where}（{name}）缺少 repo")
        # 允许写完整链接 / scp 风格，统一抠成 owner/repo
        from .github import guess_repo            # 局部导入：避免包内循环依赖
        repo = guess_repo(repo_raw)
        if not REPO_RE.match(repo):
            raise ManifestError(
                f"{where}（{name}）的 repo={repo_raw!r} 认不出 owner/repo")
        tags = raw.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        return cls(
            name=name,
            repo=repo,
            ref=str(raw.get("ref") or raw.get("branch") or "").strip(),
            description=str(raw.get("description") or "").strip(),
            version=str(raw.get("version") or "").strip(),
            author=str(raw.get("author") or "").strip(),
            tags=[str(t) for t in tags if str(t).strip()],
            keep_data=bool(raw.get("data", False)),
        )


@dataclass
class Manifest:
    """整份列表文件。"""

    entries: list[PluginEntry] = field(default_factory=list)
    name: str = ""
    updated: str = ""
    source: str = ""
    raw_version: int = 1

    def get(self, name: str) -> Optional[PluginEntry]:
        for entry in self.entries:
            if entry.name == name:
                return entry
        return None

    def names(self) -> list[str]:
        return [e.name for e in self.entries]

    @classmethod
    def parse(cls, raw: Any, *, source: str = "") -> "Manifest":
        if not isinstance(raw, dict):
            raise ManifestError(f"列表文件顶层必须是对象：{source}")
        version = raw.get("version", 1)
        try:
            version_int = int(version)
        except (TypeError, ValueError):
            raise ManifestError(f"version 必须是整数：{version!r}")
        if version_int > 1:
            raise ManifestError(
                f"列表文件版本 {version_int} 比本插件支持的版本 1 新，请先更新 plugin_manager")
        items = raw.get("plugins")
        if not isinstance(items, list):
            raise ManifestError("列表文件缺少 plugins 数组")
        entries: list[PluginEntry] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            entry = PluginEntry.parse(item, index)
            if entry.name in seen:
                raise ManifestError(f"plugins[{index}] 的 name={entry.name} 重复")
            seen.add(entry.name)
            entries.append(entry)
        if not entries:
            raise ManifestError("列表文件里一个插件都没有")
        return cls(entries=entries, name=str(raw.get("name") or ""),
                   updated=str(raw.get("updated") or ""), source=source,
                   raw_version=version_int)
