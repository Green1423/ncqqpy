"""
配置管理器 — JSON 文件存取 + 点号分隔 key + 插件命名空间隔离
"""
import copy
import json
import os
import threading
from typing import Any, NamedTuple


class ConfigFill(NamedTuple):
    """一次「按模板补全配置」的结果。

    - `filled`：这次补上的键路径（点号形式）。整段缺失时记的是**该段路径**
      （如 `summarize`），不展开到叶子；段里只缺个别子键时记子键路径
      （如 `summarize.retry_batch_size`）。
    - `conflicts`：模板要 dict、而用户写成了别的类型 → **保留用户值**、只报告。

    `changed` 为真才意味着写盘了（幂等：没缺键就不动文件）。
    """

    filled: list[str]
    conflicts: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.filled)


def _fill_missing(defaults: dict, current: Any, *, prefix: str) -> tuple[dict, ConfigFill]:
    """把 `defaults` 里 `current` 缺的键补进去（**递归**），返回 (合并结果, 报告)。

    ⚠️ 递归是必须的：`setdefault("images", {...})` 在用户已经配了 `images` 段时
    **整体不生效**，于是新版本新增的子键一个都用不上、全部退化成代码里的硬编码
    兜底（bupt_news 的 summarize / binding 都踩过这个坑）。
    已有的值一个都不动；模板要 dict 而用户写了别的类型时保留用户值并记进
    `conflicts`（既不静默，也不擅自覆盖运维写的东西）。
    """
    out = dict(current) if isinstance(current, dict) else {}
    filled: list[str] = []
    conflicts: list[str] = []
    for key, want in defaults.items():
        path = f"{prefix}{key}"
        if key not in out:
            out[key] = copy.deepcopy(want)      # 深拷贝：写出去的不能与模板共享对象
            filled.append(path)
            continue
        got = out[key]
        if isinstance(want, dict):
            if isinstance(got, dict):
                out[key], sub = _fill_missing(want, got, prefix=f"{path}.")
                filled.extend(sub.filled)
                conflicts.extend(sub.conflicts)
            else:
                conflicts.append(path)
    return out, ConfigFill(filled, conflicts)


class ConfigManager:
    """全局配置管理器，支持点号路径和插件命名空间。

    用法:
        # 全局配置
        cfg = ConfigManager("config.json")
        cfg.get("ws_url")              # "ws://127.0.0.1:3001"
        cfg.set("bound_groups", [1,2])
        cfg.delete("token")

        # 插件配置（自动加 plugins.<name> 前缀）
        with cfg.plugin("admin") as pc:
            pc.set("welcome_text", "你好")
            pc.get("welcome_text")     # "你好"  → 实际 key: plugins.admin.welcome_text
    """

    def __init__(self, file_path: str = "config.json"):
        self._path = file_path
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    # ---- 读写 ----

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}
            self._save_locked()

    def _save_locked(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)

    def save(self):
        """手动保存到文件。"""
        with self._lock:
            self._save_locked()

    def reload(self) -> dict:
        """**重新从文件读取**配置，返回读到的数据。

        ⚠️ `ConfigManager` 默认只在构造时读一次文件：`cfg.get()` 拿到的始终是
        启动那一刻的快照。手工编辑 `config.json` 之后想让进程感知到，必须显式
        调这个方法 —— 改内存的 `cfg.set()` 反而会**覆盖**文件里的手工改动
        （只覆盖它写的那个键，但足以让人以为「改了配置没生效」）。

        解析失败时保留内存里的旧值并返回它（不要因为一次手滑把运行中的配置清空）。
        """
        with self._lock:
            backup = self._data
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                self._data = backup
                return dict(self._data)
            self._data = data if isinstance(data, dict) else {}
            return dict(self._data)

    # ---- 点号路径解析 ----

    def _resolve(self, key: str, create_parents: bool = False) -> tuple[dict, str]:
        """返回 (parent_dict, last_key)。"""
        parts = key.split(".")
        parent = self._data
        for p in parts[:-1]:
            if p not in parent or not isinstance(parent[p], dict):
                if create_parents:
                    parent[p] = {}
                else:
                    return {}, parts[-1]
            parent = parent[p]
        return parent, parts[-1]

    # ---- CRUD ----

    def get(self, key: str, default: Any = None) -> Any:
        """读取配置项（点号路径）。

        Args:
            key: 如 `plugins.ai_chat.model`。
            default: 路径不存在时的返回值。

        Returns:
            配置值；不存在返回 `default`。"""
        with self._lock:
            parent, last = self._resolve(key)
            return parent.get(last, default) if isinstance(parent, dict) else default

    def set(self, key: str, value: Any, save: bool = True):
        """写入配置项（点号路径，自动建父节点）。

        Args:
            key: 点号路径。
            value: 任意可 JSON 序列化的值。
            save: 是否立即落盘（默认 `True`）。"""
        with self._lock:
            parent, last = self._resolve(key, create_parents=True)
            parent[last] = value
            if save:
                self._save_locked()

    def delete(self, key: str, save: bool = True):
        with self._lock:
            parent, last = self._resolve(key)
            if isinstance(parent, dict) and last in parent:
                del parent[last]
                if save:
                    self._save_locked()

    def has(self, key: str) -> bool:
        with self._lock:
            parent, last = self._resolve(key)
            return isinstance(parent, dict) and last in parent

    def setdefault(self, key: str, default: Any):
        """不存在时写入默认值并保存。"""
        with self._lock:
            parent, last = self._resolve(key, create_parents=True)
            if last not in parent:
                parent[last] = default
                self._save_locked()

    def keys(self, prefix: str = "") -> list[str]:
        """列出指定前缀下的所有 key（仅自身，不递归）。"""
        with self._lock:
            if not prefix:
                parent = self._data
            else:
                parent, _ = self._resolve(prefix)
            if isinstance(parent, dict):
                return list(parent.keys())
            return []

    # ---- 插件命名空间 ----

    def plugin(self, name: str) -> "PluginConfig":
        """返回插件命名空间的配置代理。"""
        return PluginConfig(self, f"plugins.{name}")


class PluginConfig:
    """插件配置代理，全局 / 群聊 / 私聊三级隔离。"""

    def __init__(self, cfg: ConfigManager, prefix: str):
        self._cfg = cfg
        self._prefix = prefix

    def get(self, key: str, default: Any = None) -> Any:
        return self._cfg.get(f"{self._prefix}.{key}", default)

    def set(self, key: str, value: Any):
        self._cfg.set(f"{self._prefix}.{key}", value)

    def delete(self, key: str):
        self._cfg.delete(f"{self._prefix}.{key}")

    def has(self, key: str) -> bool:
        return self._cfg.has(f"{self._prefix}.{key}")

    def setdefault(self, key: str, default: Any):
        self._cfg.setdefault(f"{self._prefix}.{key}", default)

    def keys(self) -> list[str]:
        return self._cfg.keys(self._prefix)

    def set_all(self, data: dict) -> None:
        """整体写入本插件的配置段（**一次**保存，不是逐键写）。

        用于「补齐配置模板」这类批量写入：逐键 `set()` 会每键重写一次整个
        `config.json`，几十个键就是几十次全量写盘。
        """
        self._cfg.set(self._prefix, dict(data or {}))

    def fill_defaults(self, defaults: dict) -> ConfigFill:
        """按模板补全缺失的配置项：**只补缺、不覆盖、幂等**。

        这是「插件首次运行就把全部配置项写进 `config.json`」的实现
        （契约见 `PluginBase.config_template()`）：

        1. 递归补全 —— 父键已存在也照样补里面缺的子键；
        2. 用户改过的值一个都不动（模板要 dict 而用户写成别的类型时保留用户值，
           记进 `conflicts` 由调用方告警）；
        3. 没有缺键就**不写盘**，有缺键也**只写一次**。

        返回值里「补了哪些键」的记法见 `ConfigFill`。
        """
        if not isinstance(defaults, dict) or not defaults:
            return ConfigFill([], [])
        merged, report = _fill_missing(defaults, self.all(), prefix="")
        if report.filled:
            self.set_all(merged)
        return report

    def all(self) -> dict:
        """整个插件命名空间的配置（dict 副本）。

        ⚠️ 别用 `get("")` 代替：`ConfigManager._resolve("plugins.ai_chat.")` 拆出
        空段后会在 `plugins` 里找 `"ai_chat."`，返回 `{}`；`get("__all__")` 也只是
        在做"取一个恰好叫 __all__ 的键"。想拿整段配置就走这里。
        """
        value = self._cfg.get(self._prefix, None)
        return dict(value) if isinstance(value, dict) else {}

    def group(self, group_id: int) -> "ScopedConfig":
        return ScopedConfig(self._cfg, f"{self._prefix}.groups.{group_id}")

    def private(self, user_id: int) -> "ScopedConfig":
        return ScopedConfig(self._cfg, f"{self._prefix}.private.{user_id}")

    @property
    def global_(self) -> "ScopedConfig":
        return ScopedConfig(self._cfg, self._prefix)


class ScopedConfig:
    """作用域配置代理。"""
    def __init__(self, cfg, prefix):
        self._cfg = cfg
        self._prefix = prefix
    def get(self, k, d=None): return self._cfg.get(f"{self._prefix}.{k}", d)
    def set(self, k, v): self._cfg.set(f"{self._prefix}.{k}", v)
    def delete(self, k): self._cfg.delete(f"{self._prefix}.{k}")
    def has(self, k): return self._cfg.has(f"{self._prefix}.{k}")
    def setdefault(self, k, d): self._cfg.setdefault(f"{self._prefix}.{k}", d)
    def keys(self): return self._cfg.keys(self._prefix)
