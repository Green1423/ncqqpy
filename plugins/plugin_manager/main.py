"""plugin_manager —— 基于 GitHub 的插件管理器（下载 / 更新 / 卸载）。

插件列表文件（manifest）放在 GitHub 仓库 `Green1423/ncqqpy-plugins` 的
`plugins.json`：一条记录 = 一个插件 + 它的 GitHub 仓库 + 可选的分支/tag。
本插件负责「把列表里的东西装到 plugins/ 下、按需更新、干净卸载」。

命令（**全部仅私聊 + 仅超管**，见 §权限）
------------------------------------------
    /plugin list [关键字] [installed]     可用插件 + 安装/更新状态
    /plugin info <插件名>                 单个插件的详情（来源、版本、文件数）
    /plugin check                         检查全部已装插件的上游更新
    /plugin install <插件名> [ref] [force] 安装（未装过）
    /plugin update [插件名|all ...] [force] 更新（未装过时等价于安装）
    /plugin uninstall <插件名> [purge]     卸载（进回收站，可找回）
    /plugin add <仓库> [name] [ref] [subdir] [force]
                                          装列表文件里没有的仓库（一步到位）
    /plugin refresh                       重新拉取列表文件（清缓存）
    /plugin trash [purge] [keep]          查看 / 清空回收站

机制（为什么这么设计）
----------------------
- **只信列表文件**：装什么、从哪装、装哪条 ref 全部来自 `plugins.json`；
  `/plugin add` 是唯一的例外（手工指定仓库，仍走同一条安装流水线）；
- **下载源码包而不是 git clone**：用 GitHub 的 `codeload` tarball，bot 机器
  不需要装 git，也不需要整个仓库历史；
- **原子替换 + 回收站**：更新先把老目录整体挪进 `plugins/.trash/<名>-<时间>/`，
  再放新的；`data/` 与用户自己加的文件不参与同步、永不删除；
- **文件级记账**：每个插件的每个文件哈希都记进
  `plugins/plugin_manager/data/installed.json`，据此分辨「上游改了」与
  「你手改了」—— 你动过的文件默认拒绝覆盖（`force` 才动）；
- **更新后热重载**：装完调框架的 `PluginManager.reload()`，新代码立即生效
  （自己除外：`plugin_manager` 不允许在运行中装卸自己）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from napcat.plugin import PluginBase, discover_plugins
from napcat.errors import PluginConfigError
from napcat.command import (
    BoolField, ChoiceField, CommandSchema, IntField, StrField, VariadicField,
    CommandPacket,
)
import napcat.deps as plugindeps

from . import registry
from .github import GithubClient, GithubError, env_token, guess_repo
from .installer import (
    INSTALLED_FILE, STAGING_DIR, InstallError, InstalledPlugin, InstalledStore,
    Installer, SelfOperationError, clear_staging, collect_files,
    count_plugin_files, write_json_atomic,
)
from .registry import DEFAULT_MANIFEST_URL, ManifestError, PluginEntry, valid_name

#: 命令名（命令组）：`/plugin ...`
ROOT = "plugin"


# ==================== 渲染小工具 ====================


def human_size(num: float) -> str:
    """字节数 → 人话（1.2 MB）。"""
    value = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def short_sha(sha: str, length: int = 7) -> str:
    return (sha or "")[:length]


def clip(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def chunk_messages(lines: list[str], *, per_message: int = 18,
                   max_chars: int = 900) -> list[str]:
    """把行列表切成多条消息（QQ 长文本容易被吞，分条更稳）。

    双阈值：条数（`per_message`）与字符数（`max_chars`）任一超出就切。
    单行本身超长不切 —— 那是内容问题（`clip()` 负责），不是分段问题。
    """
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        if buf and (len(buf) >= per_message or size + len(line) > max_chars):
            out.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        out.append("\n".join(buf))
    return out or [""]


# ==================== 参数 Schema ====================


class ListSchema(CommandSchema):
    keyword = StrField(default="", description="按名字/描述/标签过滤")
    only = ChoiceField(["all", "installed"], default="all", case_insensitive=True,
                       description="只看已安装的用 installed")


class InfoSchema(CommandSchema):
    name = StrField(required=True, description="插件名")


class InstallSchema(CommandSchema):
    name = StrField(required=True, description="插件名（见 /plugin list）")
    ref = StrField(default="", description="分支/tag/commit，缺省用列表文件里的")
    force = BoolField(default=False, description="本地有同名目录时强制接管")


class UpdateSchema(CommandSchema):
    names = VariadicField(description="插件名，可多个；all = 全部已安装")
    force = BoolField(default=False, description="丢弃本地修改")


class UninstallSchema(CommandSchema):
    name = StrField(required=True, description="插件名")
    purge = BoolField(default=False, description="连 data/ 一起清掉")


class AddSchema(CommandSchema):
    repo = StrField(required=True, description="GitHub 仓库或链接")
    name = StrField(default="", description="本地插件名（缺省取仓库名）")
    ref = StrField(default="", description="分支/tag/commit")
    subdir = StrField(default="", description="仓库里的子目录（monorepo 用）")
    force = BoolField(default=False, description="忽略冲突与改名")


class TrashSchema(CommandSchema):
    purge = BoolField(default=False, description="清空回收站")
    keep = IntField(default=0, min=0, description="清空时保留最近 N 份")


# ==================== 插件 ====================


class PluginManagerPlugin(PluginBase):
    name = "plugin_manager"
    version = "1.0.0"
    description = "基于 GitHub 的插件管理：下载 / 更新 / 卸载（仅私聊 · 仅超管）"
    author = "Green"

    def config_template(self) -> dict:
        """出厂默认配置（框架写进 `config.json` → `plugins.plugin_manager`）。"""
        return {
            #: 插件列表文件地址（换镜像/换仓库只改这一行）
            "manifest_url": DEFAULT_MANIFEST_URL,
            #: 装卸动作落在哪个目录（留空 = 跟随框架的 plugin_dir）
            "plugin_dir": "",
            #: GitHub token：留空 = 匿名访问（每小时 60 次 API）。
            #: ⚠️ 填了就是明文躺在 config.json 里，建议改用环境变量
            #: NCQQPY_GITHUB_TOKEN / GITHUB_TOKEN。
            "token": "",
            #: 网络超时（秒）与代理（如 http://127.0.0.1:7890）
            "timeout": 20,
            "proxy": "",
            #: 列表文件缓存秒数（0 = 每次都重新拉）
            "manifest_cache_seconds": 300,
            #: 更新时保留用户自己加进插件目录的文件（false = 以仓库为准）
            "keep_local_extra_files": True,
            #: 一条操作报告最多列几行（多了就截断）
            "max_report_lines": 12,
        }

    # ---- 生命周期 ----

    async def on_load(self):
        """准备记账（**不**在这里联网：加载要快、要能在离线时成功）。

        数据目录是**懒创建**的（`_mdir()`），因为它由 `plugins_root` 推导 ——
        而它可能来自配置或框架注入（见该 property）。
        """
        self._client_obj: GithubClient | None = None
        self._busy = False                   # 同一时间只跑一个装卸任务
        self._pending: set[asyncio.Task] = set()
        mdir = self._mdir()
        mdir.mkdir(parents=True, exist_ok=True)
        self._store = InstalledStore(mdir / INSTALLED_FILE)
        self.logger.info("[plugin_manager] 数据目录：%s", mdir)
        self._log_inventory()

    async def on_unload(self):
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()
        client = self._client_obj
        self._client_obj = None
        if client is not None:
            await client.close()

    def _log_inventory(self):
        """启动时把「装了什么」写进日志（排查问题时不用去问人）。"""
        installed = self._store.all()
        if not installed:
            self.logger.info("[plugin_manager] 还没有安装记录")
            return
        for name in sorted(installed):
            record = installed[name]
            missing = "" if self._plugin_path(name).exists() else "（目录已不在！）"
            self.logger.info("[plugin_manager] 已装 %s ← %s@%s %s%s",
                             name, record.repo or "?", record.ref or "默认分支",
                             short_sha(record.sha), missing)

    # ---- 基础件 ----

    @property
    def plugins_root(self) -> Path:
        """`plugins/` 根目录 —— 装卸动作实际落在哪里。

        解析顺序（**不要**只看 `__file__`）：

        1. 配置项 `plugin_dir`（显式指定，优先级最高）；
        2. 框架注入的主客户端上的 `PluginManager.plugin_dir` —— 线上真正的那个
           （`config.json` 的 `plugin_dir` 未必是 `./plugins`）；
        3. 兜底：本插件包目录的上一级。

        ⚠️ 只认 `__file__` 会在「框架的插件目录被重定向」时把插件装进**源码树**
        里（测试里踩过：临时目录的操作用了真实目录，把线上插件换了）。

        命名：**不要**叫 `plugin_dir` —— 基类的 `plugin_dir` 是「我自己的目录」
        （`plugins/<名>/`），同名但语义相反会让基类方法拿到错的路径。
        """
        configured = str(self.config.get("plugin_dir") or "").strip()
        if configured:
            return Path(configured).expanduser()
        manager = self.manager
        from_manager = getattr(manager, "plugin_dir", None)
        if from_manager:
            return Path(from_manager)
        return Path(__file__).resolve().parent.parent

    def _plugin_path(self, name: str) -> Path:
        return self.plugins_root / name

    def _mdir(self) -> Path:
        """本插件的运行时数据目录（`plugins/plugin_manager/data/`）。

        ⚠️ 从 `plugins_root` 推导，**不要**用 `__file__` 旁边：它可能来自配置或
        框架注入（测试、非默认目录），只认 `__file__` 会把记账与缓存写进源码树。
        放在插件目录下还有个好处：更新插件时 `data/` 跟着走（换代码不丢记账）。
        """
        return self.plugins_root / self.name / "data"

    @property
    def manifest_url(self) -> str:
        url = str(self.config.get("manifest_url") or "").strip()
        return url or DEFAULT_MANIFEST_URL

    def _new_client(self) -> GithubClient:
        """按当前配置新建 GitHub 客户端（配置改了下次操作就生效）。"""
        token = str(self.config.get("token") or "").strip() or env_token()
        try:
            timeout = float(self.config.get("timeout") or 20)
        except (TypeError, ValueError):
            self.logger.warning("[plugin_manager] timeout 配置不是数字，按 20 秒用")
            timeout = 20.0
        client = GithubClient(
            token=token,
            timeout=timeout,
            proxy=str(self.config.get("proxy") or ""),
            logger=self.logger,
        )
        if not token:
            self.logger.debug("[plugin_manager] 未配置 token，按匿名配额访问 GitHub")
        return client

    async def _client(self) -> GithubClient:
        if self._client_obj is None:
            self._client_obj = self._new_client()
        return self._client_obj

    def _installer(self) -> Installer:
        return Installer(plugin_dir=self.plugins_root, work_dir=self._mdir(),
                         client=None, store=self._store, self_name=self.name,
                         logger=self.logger)

    async def _installer_ready(self) -> Installer:
        inst = self._installer()
        inst.client = await self._client()
        return inst

    # ---- 列表文件 ----

    def cached_manifest_path(self) -> Path:
        return self._mdir() / "manifest.json"

    async def load_manifest(self, *, refresh: bool = False):
        """取插件列表文件：客户端缓存（TTL + ETag）→ 网络 → 磁盘兜底。

        **离线也要能用**：拉不到就用上一次成功拉到的磁盘副本，并把
        `self._manifest_stale` 立起来（`/plugin list` 会标「离线」）——
        「列表文件拿不到」不该让卸载、回收站这些完全本地的功能一起失效。

        缓存的**唯一 owner 是 `GithubClient`**：TTL 决定「要不要直接返回」，
        ETag 决定「能不能只拿 304」。这里以前还有一层自己的内存缓存（另一套
        时钟 + 一个 5 秒去重窗口），而且每次进网络分支前先 `client.clear_cache()`
        —— 顺带把 ETag 也清掉，条件请求永远命中不了。两层缓存互相拆台，
        只留客户端那层才对。
        """
        ttl = max(0.0, float(self.config.get("manifest_cache_seconds") or 0))
        client = await self._client()
        if refresh:
            client.clear_cache()          # 强制刷新：连客户端那层一起丢
        self._manifest_stale = False
        try:
            raw = await client.fetch_manifest(self.manifest_url, cache_ttl=ttl)
            manifest = registry.Manifest.parse(raw, source=self.manifest_url)
        except (GithubError, ManifestError) as exc:
            fallback = self._read_cached_manifest()
            if fallback is None:
                raise
            self._manifest_stale = True
            self.logger.warning("[plugin_manager] 列表文件拉取失败，改用本地缓存：%s", exc)
            return fallback
        try:
            self._write_cached_manifest(raw)
        except OSError as exc:
            self.logger.warning("[plugin_manager] 写本地缓存失败：%s", exc)
        return manifest

    def _read_cached_manifest(self):
        path = self.cached_manifest_path()
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return registry.Manifest.parse(raw, source=f"{self.manifest_url}（本地缓存）")
        except (OSError, ValueError, ManifestError):
            return None

    def _write_cached_manifest(self, raw) -> None:
        write_json_atomic(self.cached_manifest_path(), raw)

    async def _entry_for(self, name: str, *, manifest=None) -> PluginEntry:
        """找一个插件的来源：列表文件优先，其次是本地记账里的仓库。

        本地记账兜底的意义：列表文件里被删掉的插件，仍然可以用
        `/plugin update <名>` 从原来的仓库更新，而不是「查无此插件」。
        """
        manifest = manifest or await self.load_manifest()
        entry = manifest.get(name)
        if entry is not None:
            return entry
        record = self._store.get(name)
        if record is not None and record.repo:
            return PluginEntry(name=name, repo=record.repo, ref=record.ref,
                               version=record.version, author=record.author,
                               keep_data=record.keep_data)
        raise PluginConfigError(
            f"列表文件里没有 {name}，本地也没有它的来源记录"
            "（手工装的插件请用 /plugin add <仓库> 接管）")

    # ---- 状态计算 ----

    async def describe_local(self, name: str, *, with_files: bool = False) -> dict:
        """本地视角的插件状态（不联网）；`installed=False` 表示没装。

        `with_files=True` 才扫目录、算「哪些文件被本地改过」（`/plugin info`
        要展示它）。`/plugin check` 只看「有没有上游更新」，用不到哈希 ——
        以前它跟着一起算，等于每次检查都把所有插件的文件全哈希一遍。

        Args:
            name: 插件名。
            with_files: 是否统计文件数与本地改动（有 IO 成本）。

        Returns:
            含 `installed` / `record` / `files` / `files_changed` 的字典。
        """
        path = self._plugin_path(name)
        record = self._store.get(name)
        if not path.is_dir():
            return {"name": name, "installed": False,
                    "record": record, "known": record is not None}
        local_meta = None
        if record is None:
            raw = self._installer().local_meta(name)
            if raw:
                local_meta = InstalledPlugin.from_dict(raw)
        effective = record or local_meta
        local_files: dict[str, str] = {}
        files_changed: list[str] = []
        if with_files:
            local_files = collect_files(path)
            if effective is not None and effective.files:
                for rel, digest in effective.files.items():
                    if rel in local_files and local_files[rel] != digest:
                        files_changed.append(rel)
        return {
            "name": name,
            "installed": True,
            "known": effective is not None,
            "record": effective,
            "files": count_plugin_files(local_files),
            "files_changed": files_changed,
        }

    async def head_of(self, entry: PluginEntry, *, cache_ttl: float = 120.0):
        """上游 HEAD（带缓存；拿不到返回 unknown=True，不谎报「已是最新」）。"""
        client = await self._client()
        return await client.resolve_head(entry.repo, entry.ref, cache_ttl=cache_ttl)

    async def backfill_sha(self, name: str, entry: PluginEntry) -> str:
        """给缺提交号的记账补上当前 HEAD 的提交号，返回补到的值（可能为空）。

        为什么需要它：源码包（codeload tarball）的响应头**不一定**带提交号，
        而「有没有更新」全靠提交号比对 —— 不补的话新装的插件第一次
        `/plugin check` 只能说「无法比对」。补的时候取的是**当前** HEAD：
        刚下完源码包，两者一致（并发推送到同一分支这种极端情况最多让人
        多更新一次，不会误报「已是最新」）。
        """
        record = self._store.get(name)
        if record is None or not record.repo:
            return ""
        info = await self.head_of(entry, cache_ttl=0)
        if info.unknown or not info.sha:
            return ""
        if record.sha != info.sha:
            record.sha = info.sha
            self._store.put(record)
            target = self._plugin_path(name)
            if target.is_dir():
                self._installer()._write_local_meta(target, record)
        return info.sha

    async def describe_remote(self, entry: PluginEntry):
        """列表文件视角的插件状态：本地记录 + 上游 HEAD + 更新判定。"""
        local = await self.describe_local(entry.name)
        info = await self.head_of(entry)
        record = local.get("record")
        state = "not_installed" if not local["installed"] else "unknown"
        if local["installed"] and record is not None:
            if not record.repo:
                state = "local"
            elif info.unknown:
                state = "unknown"
            elif record.sha and record.sha == info.sha:
                state = "current"
            elif not record.sha:
                state = "unknown"
            else:
                state = "outdated"
        return {"entry": entry, "local": local, "head": info, "state": state}

    # ---- 报告 ----

    def _op_report(self, title: str, lines: list[str]) -> list[str]:
        """回执：标题 + 若干行（超出 `max_report_lines` 截断并说明）。"""
        try:
            limit = int(self.config.get("max_report_lines") or 12)
        except (TypeError, ValueError):
            limit = 12
        limit = max(1, limit)
        body = [line for line in lines if line]
        if len(body) > limit:
            more = len(body) - limit
            body = body[:limit] + [f"…还有 {more} 条"]
        return chunk_messages([title, *body])

    async def _notify(self, user_id: int, text: str) -> None:
        try:
            await self.api.send_private_msg(user_id=str(user_id), message=text)
        except Exception:  # noqa: BLE001 - 通知发不出去不该影响安装结果
            self.logger.warning("[plugin_manager] 私聊通知 %s 失败", user_id,
                                exc_info=True)

    async def _finish_operation(self, user_id: int, title: str, lines: list[str],
                                results: list[dict]) -> None:
        """后台装卸任务的收尾：回执 + 热重载受影响的插件 + 重试被依赖挡住的插件。"""
        ok = [r for r in results if r.get("ok")]
        failed = [r for r in results if not r.get("ok")]
        head = title if not failed else f"{title}（{len(ok)} 成功 / {len(failed)} 失败）"
        touched = [r["name"] for r in results if r.get("reload")]
        try:
            for message in self._op_report(head, lines):
                await self._notify(user_id, message)
        finally:
            self._busy = False
        await self._reload_affected(touched)
        # 依赖可能刚就位：把此前「因依赖缺失没加载」的插件拉起来，并补一条回执
        started = await self._retry_blocked(exclude=touched)
        if started:
            await self._notify(
                user_id,
                "🔗 依赖已就绪，这些插件已自动加载："
                + "、".join(started) + "（此前因为缺少依赖没起来）")

    async def _retry_blocked(self, exclude: list[str] | None = None) -> list[str]:
        """重试那些「因依赖不满足没加载」的插件，返回这次被拉起来的名。

        没有这一步，「先装使用者、再装依赖」的顺序会留下一个永远不加载的插件：
        框架的 watcher 虽然每 2 秒会重试，但用户当下看不到任何交代。

        ⚠️ 候选者是**目录里所有没加载的插件**，不是框架 `_dep_blocked` 里的记录：
        那个字典是判定缓存，`load()` 成功时会清（装 store 本身就会把它清空），
        依赖装好之后再读它就是空的 —— 现象是「功能永远不生效」。
        """
        manager = self.manager
        if manager is None:
            return []
        skip = set(exclude or [])
        started: list[str] = []
        for name in manager._plugin_names():
            if name in skip or name == self.name:
                continue
            if manager.get(name) is not None:
                continue
            try:
                verdict = manager.dependency_verdict(name)
            except Exception:  # noqa: BLE001 - 判定失败就当还不行
                verdict = None
            if verdict is None or not verdict.ok:
                continue
            loaded = await manager.load(name)
            if loaded is None:
                continue
            await manager.enable(name)
            started.append(name)
            self.logger.info("[plugin_manager] %s 的依赖已就绪，自动加载"
                             "（此前缺少：%s）", name,
                             "、".join(str(s) for s in verdict.missing) or "无")
        return started

    async def _reload_affected(self, names: list[str]) -> None:
        """让改动过的插件立刻生效（框架的热重载会重新走 on_load/on_enable）。"""
        if not names:
            return
        await asyncio.sleep(0.5)          # 先把回执发完，再动插件
        manager = self.manager
        if manager is None:
            self.logger.warning("[plugin_manager] 拿不到框架插件管理器，"
                                "请手动 /reload 或重启")
            return
        for name in names:
            try:
                if manager.get(name) is None:
                    loaded = await manager.load(name)
                    if loaded is not None:
                        await manager.enable(name)
                    self.logger.info("[plugin_manager] 已加载新插件 %s", name)
                else:
                    await manager.reload(name)
                    self.logger.info("[plugin_manager] 已热重载 %s", name)
            except Exception:  # noqa: BLE001 - 单个插件重载失败不该中断整批
                self.logger.error("[plugin_manager] 重载 %s 失败", name,
                                  exc_info=True)

    def _launch(self, coro, receipt: str) -> str:
        """后台跑一个装卸任务：置忙 + 落任务 + 回一句「已开始」。

        三条写命令（install / update / add）共用 —— 它们原来的收尾样板
        （`_busy = True` → `_spawn` → 返回 ⏳ 文案）逐字重复了三遍。
        """
        self._busy = True
        self._spawn(coro)
        return receipt

    def _spawn(self, coro) -> None:
        """把长任务丢到后台（命令本身立刻回执，避免装到一半才回话）。"""
        task = asyncio.create_task(coro)
        self._pending.add(task)

        def _done(t: asyncio.Task) -> None:
            self._pending.discard(t)
            exc = t.exception() if not t.cancelled() else None
            if exc is not None:
                self.logger.error("[plugin_manager] 后台任务失败", exc_info=exc)
                self._busy = False

        task.add_done_callback(_done)

    # ---- 依赖报告 ----

    def dependency_report(self, name: str) -> dict:
        """这个插件声明了哪些依赖、现在满足没有（不联网、不实例化插件）。

        未加载的插件读**类属性**（框架的 `PluginManager.plugin_class()`，带
        mtime 缓存），所以刚装完就能报告；模块导入失败时返回空报告 —— 那种
        问题由框架的加载错误路径报，不该在这里再喊一遍。
        """
        manager = self.manager
        if manager is None:
            return {"required": [], "optional": [], "missing": [], "optional_missing": []}
        plugin_class = manager.plugin_class(name)
        if plugin_class is None:
            return {"required": [], "optional": [], "missing": [], "optional_missing": []}
        try:
            required, optional = plugin_class.dependency_specs()
        except plugindeps.DependencyError as exc:
            return {"required": [], "optional": [], "missing": [], "optional_missing": [],
                    "error": str(exc)}
        verdict = plugindeps.check_dependencies(
            required, optional, resolve=manager.dependency_target,
            check_enabled=False, owner=name)
        return {
            "required": [str(s) for s in required],
            "optional": [str(s) for s in optional],
            "missing": [s.name for s in verdict.missing],
            "optional_missing": [s.name for s in verdict.optional_missing],
            "error": "",
        }

    def _dependency_lines(self, name: str) -> list[str]:
        """依赖情况渲染成回执里的几行（装完/更新后提示还缺什么）。"""
        report = self.dependency_report(name)
        if report.get("error"):
            return [f"⚠️ 依赖声明有问题：{report['error']}"]
        if not report["required"] and not report["optional"]:
            return []
        lines = []
        if report["required"]:
            lines.append("依赖：" + "、".join(report["required"]))
        if report["optional"]:
            lines.append("可选依赖：" + "、".join(report["optional"]))
        if report["missing"]:
            hint = "、".join(f"/plugin install {n}" for n in report["missing"])
            lines.append(f"⚠️ 还缺必需依赖（缺了它不会加载）：{'、'.join(report['missing'])}")
            lines.append(f"   装上就会自动加载：{hint}")
        if report["optional_missing"]:
            lines.append("（可选依赖暂缺，不影响运行："
                         + "、".join(report["optional_missing"]) + "）")
        return lines

    # ---- 命令注册 ----

    async def on_enable(self):
        """注册命令。**每个子命令都显式声明 `superuser` + `private`** ——
        父命令上的声明只是子命令的默认值，子命令自己写一遍才不会随父命令漂移。
        """
        op = dict(permission="superuser", scope="private")
        plugin = self

        @self.register_command(ROOT, description="插件管理：安装 / 更新 / 卸载（仅私聊 · 仅超管）")
        async def cmd_plugin(packet: CommandPacket):
            return "插件管理：用 /help plugin 看子命令"

        # ---------------- 只读 ----------------

        @self.register_command(f"{ROOT}.list", schema=ListSchema, **op,
                               description="列出可用插件与安装状态")
        async def cmd_list(params, event):
            only = (params.only or "all").lower()
            keyword = (params.keyword or "").strip().lower()
            manifest = await plugin.load_manifest()
            installed_names = {n for n in plugin._store.all()
                               if plugin._plugin_path(n).is_dir()}
            # 本地装了但列表文件里没有的，也要出现在列表里（否则「装了什么」看不见）
            extra_local = sorted(n for n in discover_plugins(plugin.plugins_root)
                                 if n not in manifest and n != plugin.name)

            lines: list[str] = []
            stale = "（离线：用的是本地缓存的列表文件）" if getattr(
                plugin, "_manifest_stale", False) else ""
            lines.append(f"📦 插件列表 {len(manifest.entries)} 个{stale}")
            if manifest.updated:
                lines.append(f"列表更新于 {manifest.updated}")

            shown = 0
            for entry in manifest.entries:
                if keyword and keyword not in (
                        entry.name + entry.description + " ".join(entry.tags)).lower():
                    continue
                if only == "installed" and entry.name not in installed_names:
                    continue
                shown += 1
                lines.extend(plugin._render_entry(entry, installed_names))
            if extra_local:
                shown += len(extra_local)
                lines.append("\n📁 本地有、列表文件里没有：")
                for name in extra_local:
                    record = plugin._store.get(name)
                    mark = "🔧" if record else "❔"
                    src = f"← {record.repo}" if record and record.repo else "（来源未知）"
                    lines.append(f"  {mark} {name} {src}")
            if not shown:
                lines.append("（没有匹配的插件）")
            lines.append("\n/plugin info <插件名> 看详情 · /plugin install <插件名> 安装")
            lines.append("🛡 仅私聊 · 仅超管可用")
            return chunk_messages(lines)

        @self.register_command(f"{ROOT}.info", schema=InfoSchema, **op,
                               description="查看单个插件的详情")
        async def cmd_info(params, event):
            name = params.name.strip()
            if not valid_name(name):
                raise PluginConfigError(f"插件名非法：{name!r}")
            local = await plugin.describe_local(name, with_files=True)
            entry = None
            try:
                manifest = await plugin.load_manifest()
                entry = manifest.get(name)
            except (GithubError, ManifestError) as exc:
                if not local["installed"]:
                    raise
                plugin.logger.warning("[plugin_manager] 列表文件不可用：%s", exc)
            if entry is None and not local["installed"]:
                raise PluginConfigError(
                    f"没有 {name} 这个插件（列表文件里没有，本地也没装）")

            record = local.get("record")
            lines = [f"🔎 {name}"]
            if entry is not None:
                lines.append(f"介绍：{entry.description or '（无）'}")
                if entry.tags:
                    lines.append("标签：" + " ".join(entry.tags))
                lines.append(f"仓库：https://github.com/{entry.repo}")
                lines.append(f"列表 ref：{entry.ref_display}")
                if entry.version:
                    lines.append(f"列表版本：{entry.version}")
            dep_lines = plugin._dependency_lines(name)
            if dep_lines:
                lines.append("")
                lines.extend(dep_lines)
            if local["installed"]:
                lines.append("")
                lines.append(f"已安装：是（{local['files']} 个文件）")
                if record is not None:
                    lines.append(f"来源：{record.repo or '未知'}@{record.ref or '默认分支'}")
                    lines.append(f"提交：{short_sha(record.sha) or '未记录'}")
                    if record.version:
                        lines.append(f"版本：{record.version}")
                    if record.installed_at:
                        lines.append(f"首次安装：{record.installed_at}")
                    if record.updated_at:
                        lines.append(f"最近更新：{record.updated_at}")
                else:
                    lines.append("来源：❔ 不是本管理器装的（可用 update 接管）")
                if local["files_changed"]:
                    lines.append("本地改动："
                                 + ", ".join(local["files_changed"][:5])
                                 + ("…" if len(local["files_changed"]) > 5 else "")
                                 + "（更新会被拦下，除非 force）")
                if entry is not None:
                    status = await plugin.describe_remote(entry)
                    lines.append(f"上游：{plugin._state_text(status)}")
            else:
                lines.append("已安装：否")
                if entry is not None:
                    lines.append(f"安装：/plugin install {name}")
            return chunk_messages(lines)

        @self.register_command(f"{ROOT}.check", **op,
                               description="检查全部已装插件的上游更新")
        async def cmd_check(event):
            manifest = await plugin.load_manifest()
            installed = plugin._store.all()
            lines = []
            if not installed:
                return "还没有装任何插件（/plugin list 看可装的，/plugin install <名> 安装）"
            outdated = unknown = current = 0
            for name in sorted(installed):
                record = installed[name]
                if not plugin._plugin_path(name).is_dir():
                    lines.append(f"❌ {name} —— 安装记录还在，但目录没了")
                    continue
                entry = manifest.get(name)
                if entry is None and record.repo:
                    entry = PluginEntry(name=name, repo=record.repo, ref=record.ref)
                if entry is None:
                    lines.append(f"❔ {name} —— 没有来源仓库，无法检查")
                    unknown += 1
                    continue
                if record.sha == "" and entry.repo:
                    # 老记账（或源码包没给提交号）→ 顺手补一次，下次就能比对了
                    await plugin.backfill_sha(name, entry)
                status = await plugin.describe_remote(entry)
                lines.append(f"{plugin._state_icon(status['state'])} {name} "
                             f"—— {plugin._state_text(status)}")
                outdated += status["state"] == "outdated"
                current += status["state"] == "current"
                unknown += status["state"] == "unknown"
            lines.append("")
            lines.append(f"合计：最新 {current} · 可更新 {outdated} · 未知 {unknown}")
            if outdated:
                lines.append("更新：/plugin update all")
            return chunk_messages(lines)

        # ---------------- 写操作 ----------------

        @self.register_command(f"{ROOT}.install", schema=InstallSchema, **op,
                               description="安装插件（未装过的用这个）")
        async def cmd_install(params, event):
            name = params.name.strip()
            if not valid_name(name):
                raise PluginConfigError(f"插件名非法：{name!r}")
            plugin._busy_guard()
            manifest = await plugin.load_manifest()
            entry = manifest.get(name)
            if entry is None:
                raise PluginConfigError(
                    f"列表文件里没有 {name}。想装列表外的仓库用 /plugin add <仓库>")
            if params.ref.strip():
                entry = PluginEntry(**{**entry.__dict__, "ref": params.ref.strip()})
            return plugin._launch(
                plugin._run_install(event.user_id, entry, force=params.force),
                f"⏳ 开始安装 {name}（{entry.repo}@{entry.ref_display}）…\n"
                "下载完成后会私聊回执。")

        @self.register_command(f"{ROOT}.update", schema=UpdateSchema, **op,
                               description="更新已装插件（all = 全部）")
        async def cmd_update(params, event):
            targets = [n.strip() for n in (params.names or []) if n.strip()]
            if not targets:
                raise PluginConfigError(
                    "要更新哪个？/plugin update <插件名> 或 /plugin update all")
            plugin._busy_guard()
            manifest = await plugin.load_manifest()
            if any(t.lower() == "all" for t in targets):
                targets = sorted(plugin._store.all())
                if not targets:
                    raise PluginConfigError("还没有装任何插件")
            entries = []
            for name in targets:
                if not valid_name(name):
                    raise PluginConfigError(f"插件名非法：{name!r}")
                entries.append(await plugin._entry_for(name, manifest=manifest))
            return plugin._launch(
                plugin._run_update(event.user_id, entries, force=params.force),
                f"⏳ 开始更新 {'、'.join(e.name for e in entries)}…\n"
                "完成后会私聊回执。")

        @self.register_command(f"{ROOT}.uninstall", schema=UninstallSchema, **op,
                               description="卸载插件（进回收站，可找回）")
        async def cmd_uninstall(params, event):
            name = params.name.strip()
            if not valid_name(name):
                raise PluginConfigError(f"插件名非法：{name!r}")
            if name == plugin.name:
                raise SelfOperationError()
            plugin._busy_guard()
            installer = await plugin._installer_ready()
            manager = plugin.manager
            if manager is not None and manager.get(name) is not None:
                await manager.disable(name)
                await manager.unload(name)
                disabled = True
            else:
                disabled = False
            keep_data = not params.purge
            report = installer.uninstall(name, keep_data=keep_data)
            plugin.logger.info("[plugin_manager] 卸载 %s（备份：%s）", name,
                               report["backup"])
            lines = [f"🗑 已卸载 {name}"]
            if disabled:
                lines.append("已从运行中的框架里卸载（命令/事件/定时器都收回了）")
            else:
                lines.append("它本来就没在运行（或框架里没加载）")
            if report["backup"]:
                lines.append(f"文件已进回收站：plugins/.trash/{report['backup'].name}")
                lines.append("确认没问题后 /plugin trash purge=true 清掉")
            else:
                lines.append("本地没有它的目录，只清了安装记录")
            if report["kept_data"]:
                lines.append(f"data/ 按你的要求留在了 plugins/{name}/data")
            elif report["had_data"]:
                lines.append("（data/ 也一起进回收站了，想保留下次加 purge=false）")
            return lines

        @self.register_command(f"{ROOT}.add", schema=AddSchema, **op,
                               description="安装列表文件里没有的 GitHub 仓库")
        async def cmd_add(params, event):
            repo = guess_repo(params.repo.strip())
            if not repo:
                raise PluginConfigError(
                    "认不出仓库地址，写成 owner/repo 或 https://github.com/owner/repo")
            name = (params.name or "").strip() or repo.split("/")[-1].replace("-", "_")
            name = name.removeprefix("ncqqpy-plugin-").removeprefix("napcat-plugin-")
            if not valid_name(name):
                raise PluginConfigError(f"插件名非法：{name!r}（用 name=<名字> 指定）")
            if name == plugin.name:
                raise SelfOperationError()
            plugin._busy_guard()
            entry = PluginEntry(name=name, repo=repo, ref=params.ref.strip(),
                                description=f"手工添加：{repo}")
            subdir = params.subdir.strip()
            return plugin._launch(
                plugin._run_install(event.user_id, entry, force=params.force,
                                    subdir=subdir),
                f"⏳ 开始安装 {name} ← github.com/{repo}"
                f"{'/' + subdir if subdir else ''}…\n完成后会私聊回执。")

        @self.register_command(f"{ROOT}.refresh", schema=None, **op,
                               description="重新拉取插件列表文件")
        async def cmd_refresh(event):
            # 重建客户端：在 config.json 里改了 token / proxy / timeout 之后，
            # `/plugin refresh` 立刻就按新配置走（否则要等插件热重载）
            old = plugin._client_obj
            plugin._client_obj = plugin._new_client()
            if old is not None and old is not plugin._client_obj:
                await old.close()
            manifest = await plugin.load_manifest(refresh=True)
            lines = [f"🔄 已重新拉取列表文件：{len(manifest.entries)} 个插件"]
            if manifest.name:
                lines.append(f"名称：{manifest.name}")
            if manifest.updated:
                lines.append(f"列表更新于 {manifest.updated}")
            lines.append(" ".join(manifest.names()))
            return chunk_messages(lines)

        @self.register_command(f"{ROOT}.trash", schema=TrashSchema, **op,
                               description="查看 / 清理回收站")
        async def cmd_trash(params, event):
            installer = await plugin._installer_ready()
            if params.purge:
                keep = params.keep
                removed, freed = installer.purge_trash(keep=keep)
                return (f"🧹 回收站已清理：删除 {removed} 份备份，"
                        f"释放 {human_size(freed)}"
                        + (f"（保留最近 {keep} 份）" if keep else ""))
            entries = installer.trash_entries()
            if not entries:
                return "回收站是空的（卸载/更新留下的备份会出现在这里）"
            lines = [f"🗄 回收站（{len(entries)} 份，新的在前）"]
            total = 0
            for item in entries:
                total += item["size"]
                lines.append(f"  {item['name']} —— {item['files']} 个文件 · "
                             f"{human_size(item['size'])}")
            lines.append("")
            lines.append(f"合计 {human_size(total)} · 清空：/plugin trash purge=true")
            lines.append("要找回某个文件：plugins/.trash/<名字>/ 里直接拷回去")
            return chunk_messages(lines)

    # ---- 命令内部用的小工具 ----

    def _busy_guard(self) -> None:
        if self._busy:
            raise PluginConfigError("上一个装卸任务还没跑完，稍等一下再试")

    def _render_entry(self, entry: PluginEntry, installed: set[str]) -> list[str]:
        """列表里的一行（依赖信息只在 `/plugin info` 里展开，这里只给标记）。"""
        try:
            report = self.dependency_report(entry.name)
        except Exception:  # noqa: BLE001 - 列表不该因为某个插件读不了就整页失败
            report = {}
        needs = report.get("required") or []
        missing = report.get("missing") or []
        if entry.name in installed:
            record = self._store.get(entry.name)
            if record is None:
                head = f"  🔧 {entry.name} —— 已装（非本管理器安装）"
            else:
                head = (f"  ✅ {entry.name} —— 已装 v{record.version or '?'}"
                        f"（{short_sha(record.sha) or '提交未记录'}）")
        else:
            head = f"  ⬜ {entry.name}"
        detail = clip(entry.description, 46) or "（无介绍）"
        line = f"{head}  {detail}"
        if needs:
            line += f"  🔗需 {'、'.join(needs)}"
            if missing:
                line += f"（缺 {'、'.join(missing)}）"
        if entry.tags and entry.name not in installed:
            line += f"  [{'/'.join(entry.tags[:3])}]"
        return [line]

    @staticmethod
    def _state_icon(state: str) -> str:
        return {"current": "✅", "outdated": "⬆️", "unknown": "❔",
                "not_installed": "⬜", "local": "🔧"}.get(state, "•")

    @staticmethod
    def _state_text(status: dict) -> str:
        state = status["state"]
        head = status["head"]
        record = status["local"].get("record")
        if state == "not_installed":
            return "未安装"
        if state == "local":
            return "本地安装（没有来源仓库，无法比对）"
        if state == "unknown":
            if head.unknown:
                return f"无法确认（{clip(head.note, 60)}）"
            if record is not None and not record.sha:
                return ("无法比对（安装时没记下提交号；"
                        "再更新一次即可记录）")
            return "无法确认"
        if state == "current":
            return f"已是最新（{short_sha(record.sha) if record else ''}）"
        when = f" · {head.date[:10]}" if head.date else ""
        return (f"可更新（本地 {short_sha(record.sha) if record else '?'}"
                f" → 上游 {short_sha(head.sha)}{when}）")

    # ---- 后台任务实体 ----

    async def _run_install(self, user_id: int, entry: PluginEntry, *,
                           force: bool = False, subdir: str = "") -> None:
        title = f"📥 安装 {entry.name}"
        lines: list[str] = []
        results: list[dict] = []
        try:
            installer = await self._installer_ready()
            result = await installer.install(entry, force=force, subdir=subdir,
                                             replace=not force)
            results.append({"name": entry.name, "ok": True,
                            "reload": result.changed})
            if result.action == "noop":
                lines.append("内容已经是最新的，没有改动")
            else:
                verb = "安装完成" if result.action == "install" else "更新完成"
                lines.append(f"{verb}：{entry.repo}@{result.ref}")
                lines.append(f"版本 {result.version or '?'}"
                             + (f" · 提交 {short_sha(result.new_sha)}"
                                if result.new_sha else ""))
                if result.plan is not None:
                    lines.append("文件：" + result.plan.summary())
                if result.backup is not None:
                    lines.append(f"旧版本备份：plugins/.trash/{result.backup.name}")
            if result.changed:
                await self.backfill_sha(entry.name, entry)
                lines.extend(self._dependency_lines(entry.name))
            lines.extend(f"· {note}" for note in result.notes)
            lines.append("")
            lines.append(self._reload_hint(entry.name))
        except SelfOperationError as exc:
            results.append({"name": entry.name, "ok": False})
            lines.append(str(exc))
        except (InstallError, GithubError, ManifestError) as exc:
            results.append({"name": entry.name, "ok": False})
            lines.append(f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001 - 兜底：后台任务不能静默死掉
            results.append({"name": entry.name, "ok": False})
            self.logger.error("[plugin_manager] 安装 %s 失败", entry.name,
                              exc_info=True)
            lines.append(f"❌ 未预期的失败：{type(exc).__name__}: {exc}")
        finally:
            clear_staging(self._mdir() / STAGING_DIR)
            await self._finish_operation(user_id, title, lines, results)

    async def _run_update(self, user_id: int, entries: list[PluginEntry], *,
                          force: bool = False) -> None:
        title = f"⬆️ 更新 {len(entries)} 个插件"
        lines: list[str] = []
        results: list[dict] = []
        changed: list[str] = []
        try:
            installer = await self._installer_ready()
            for entry in entries:
                try:
                    result = await installer.install(entry, force=force,
                                                     replace=True)
                except (InstallError, GithubError) as exc:
                    results.append({"name": entry.name, "ok": False})
                    lines.append(f"❌ {entry.name}：{exc}")
                    continue
                except Exception as exc:  # noqa: BLE001
                    results.append({"name": entry.name, "ok": False})
                    self.logger.error("[plugin_manager] 更新 %s 失败", entry.name,
                                      exc_info=True)
                    lines.append(f"❌ {entry.name}：{type(exc).__name__}: {exc}")
                    continue
                results.append({"name": entry.name, "ok": True,
                                "reload": result.changed})
                if result.action == "noop":
                    lines.append(f"✅ {entry.name} —— 已是最新")
                else:
                    await self.backfill_sha(entry.name, entry)
                    changed.append(entry.name)
                    plan = result.plan.summary() if result.plan else ""
                    lines.append(f"⬆️ {entry.name} —— {result.version or '?'}"
                                 f"（{plan}）")
                    lines.extend("   " + line
                                 for line in self._dependency_lines(entry.name))
            if changed:
                lines.append("")
                lines.append("受影响插件会热重载：" + "、".join(changed))
        finally:
            clear_staging(self._mdir() / STAGING_DIR)
            await self._finish_operation(user_id, title, lines, results)

    @staticmethod
    def _reload_hint(name: str) -> str:
        return (f"新代码会自动热重载；没生效就发 /reload {name}"
                "（插件自身的配置项写在 config.json → plugins."
                f"{name}）")
