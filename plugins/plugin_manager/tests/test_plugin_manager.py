#!/usr/bin/env python3
"""plugin_manager 验收套件：列表文件 → 下载 → 安装 → 更新 → 卸载。

**真联网**：起一个本地 HTTP 服务当 GitHub（`plugins.json` + 源码包 tarball），
插件侧只用 `manifest_url` / `codeload` 两个地址就能整体指过来，所以
「解析 → 下载 → 安全解压 → 原子替换 → 记账 → 热重载 → 卸载」全程都是真跑的，
不 mock 关键路径。

覆盖：
  1. 列表文件解析与校验（非法条目必须被拒）；
  2. 更新计划（用户手改 vs 上游改动 → 冲突）；
  3. 解压安全（拒绝 `..`、绝对路径、链接）；
  4. 真实安装：目录结构、data/ 保留、记账文件、提交号；
  5. 真实更新：上游改一个文件 → 只动它；用户改过的文件 → 拒绝（force 才动）；
  6. 真实卸载：进回收站、框架里卸载、data/ 保留、回收站清理；
  7. 权限：仅私聊 + 仅超管（非超管私聊、超管群聊都拿不到）；
  8. 后台任务与忙锁。

跑法（两条路都行）：
    ./venv-arm/bin/python -B plugins/plugin_manager/tests/test_plugin_manager.py
    ./venv-arm/bin/python -B -m pytest plugins/plugin_manager/tests/test_plugin_manager.py
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import shutil
import sys
import tarfile
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]          # 框架仓库根
sys.path.insert(0, str(ROOT))

from napcat.command import CommandManager
from napcat.errors import ErrorReporter
from napcat.events import EventBus, TimerManager
from napcat.file_manager import FileManager
from napcat.client import GroupBinding
from napcat.models import MessageEvent, Sender
from napcat.permission import PermissionManager
from napcat.plugin import PluginManager, PluginBase
from napcat.config import ConfigManager                             # noqa: E402

PLUGIN_SRC = Path(__file__).resolve().parents[1]   # plugins/plugin_manager
sys.path.insert(0, str(PLUGIN_SRC.parent))         # 让 `import plugin_manager.*` 成立

from plugin_manager import installer as inst                        # noqa: E402
from plugin_manager import registry                                 # noqa: E402
from plugin_manager import zips as zips_mod                         # noqa: E402
from plugin_manager.main import PluginManagerPlugin                 # noqa: E402

SUPERUSER = 10001
STRANGER = 20002
_manager_logger = logging.getLogger("test.plugin_manager")
_manager_logger.addHandler(logging.NullHandler())
_manager_logger.setLevel(logging.CRITICAL)

_passed = 0
_failed = 0
_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        _failures.append(name)
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


# ==================== 本地「GitHub」====================


PLUGIN_FILES_V1 = {
    "__init__.py": "from .main import DemoPlugin\n",
    "main.py": (
        "from napcat.plugin import PluginBase\n\n\n"
        "class DemoPlugin(PluginBase):\n"
        "    name = 'demo'\n"
        "    version = '1.0.0'\n"
        "    description = '演示插件'\n"
    ),
    "helper.py": "VALUE = 1\n",
    "README.md": "# demo\n",
    "tests/test_demo.py": "def test_nothing():\n    assert True\n",
    "data/user_state.json": '{"keep": true}\n',
}


def make_tarball(files: dict[str, str], top: str = "demo-repo-main") -> bytes:
    """按 GitHub 源码包的样子打包：顶层目录 `<repo>-<ref>/`。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeGithub:
    """替换 `GithubClient`：源码包从内存里现打包，HEAD 可编程。"""

    def __init__(self):
        self.files = dict(PLUGIN_FILES_V1)
        self.top = "demo-repo-main"
        self.head_sha = "a" * 40
        self.head_date = "2026-10-10T00:00:00Z"
        self.head_unknown = False
        self.manifest_raw: dict = {"version": 1, "name": "测试列表", "plugins": []}
        self.manifest_error: Exception | None = None
        self.downloads = 0
        self.closed = False

    async def fetch_manifest(self, url, *, cache_ttl=300.0):
        if self.manifest_error is not None:
            raise self.manifest_error
        return json.loads(json.dumps(self.manifest_raw))

    async def resolve_head(self, repo, ref="", *, cache_ttl=120.0):
        from plugin_manager.github import HeadInfo
        if self.head_unknown:
            return HeadInfo(repo=repo, ref=ref or "(default)", unknown=True,
                            note="假客户端：拿不到")
        return HeadInfo(repo=repo, ref=ref or "(default)", sha=self.head_sha,
                        date=self.head_date)

    async def fetch_archive(self, repo, ref, dest_dir, *, progress=None):
        from plugin_manager.github import ArchiveInfo
        self.downloads += 1
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{repo.replace('/', '__')}-{ref}.tar.gz"
        path.write_bytes(make_tarball(self.files, top=self.top))
        return ArchiveInfo(path=path, repo=repo, ref=ref, sha=self.head_sha)

    def clear_cache(self):
        self.cleared = True

    async def close(self):
        self.closed = True


class LocalGitHub:
    """本地 HTTP 服务：给出 `manifest_url` 与 `codeload` 模板。"""

    def __init__(self, files: dict[str, str]):
        self.files = files
        self.manifest_raw = {"version": 1, "name": "本地列表", "plugins": []}
        self.hits: list[str] = []
        self.head_sha = "f" * 40
        self.head_date = "2026-10-10T00:00:00Z"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):        # 静音
                pass

            def do_GET(self):                    # noqa: N802
                outer.hits.append(self.path)
                if self.path.startswith("/manifest.json"):
                    body = json.dumps(outer.manifest_raw,
                                      ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                elif self.path.startswith("/archive/"):
                    body = make_tarball(outer.files)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                elif "/commits" in self.path:
                    body = json.dumps({"sha": outer.head_sha, "commit": {
                        "message": "本地提交",
                        "committer": {"date": outer.head_date}}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                else:
                    body = b"not found"
                    self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def manifest_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/manifest.json"

    @property
    def codeload(self) -> str:
        return f"http://127.0.0.1:{self.port}/archive/{{repo}}/{{ref}}.tar.gz"


class FakeApi:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.messages: dict[int, dict] = {}     # get_msg：引用消息用

    async def send_private_msg(self, user_id, message, **kwargs):
        text = message if isinstance(message, str) else "".join(
            seg.get("data", {}).get("text", "") for seg in message.build()
            if seg.get("type") == "text")
        self.sent.append((str(user_id), text))
        return {"message_id": len(self.sent)}

    async def send_group_msg(self, group_id, message, **kwargs):
        self.sent.append((f"group:{group_id}", str(message)))
        return {"message_id": len(self.sent)}

    async def get_msg(self, message_id):
        return self.messages.get(int(message_id), {})

    def texts(self) -> str:
        return "\n".join(text for _to, text in self.sent)


class Harness:
    """一整套运行环境：临时 plugins/ + 加载好的 plugin_manager 插件。"""

    def __init__(self, root: Path):
        self.root = root
        self.plugin_dir = root / "plugins"
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        # 把 plugin_manager 自己也复制进临时插件目录：装卸动作**只**允许落在
        # `plugin_dir` 里，绝不能碰真实源码树（`plugin_dir` 配置项 + `__file__`
        # 兜底的组合曾经让测试把线上插件换掉，这里从根上隔离）。
        shutil.copytree(PLUGIN_SRC, self.plugin_dir / "plugin_manager",
                        ignore=shutil.ignore_patterns("__pycache__", "data"))
        self.cfg = ConfigManager(str(root / "config.json"))
        self.api = FakeApi()
        self.bus = EventBus()
        self.timer = TimerManager()
        self.timer.start()
        self.perm = PermissionManager(superusers=[SUPERUSER])
        self.cmds = CommandManager(prefixes=["/"], perm_manager=self.perm)
        self.fake = FakeGithub()
        self.mgr = PluginManager(
            str(self.plugin_dir), api=self.api, event_bus=self.bus, timer=self.timer,
            cmd_manager=self.cmds, perm_manager=self.perm,
            file_manager=FileManager(api=self.api), logger=_manager_logger,
            groups=GroupBinding(bound=set(), all_groups=True), cfg=self.cfg,
            client=SimpleNamespace(plugins=None, ws_connected=False),
            error_reporter=ErrorReporter())
        self.cmds._plugins_ref = self.mgr
        self.plugin: PluginManagerPlugin | None = None
        self.reply_text = ""

    async def start(self, *, config: dict | None = None):
        plugin = PluginManagerPlugin(
            api=self.api, event_bus=self.bus, timer=self.timer,
            cmd_manager=self.cmds, perm_manager=self.perm,
            file_manager=FileManager(api=self.api), logger=_manager_logger,
            groups=GroupBinding(bound=set(), all_groups=True), cfg=self.cfg,
            client=SimpleNamespace(plugins=self.mgr, ws_connected=False))
        await plugin.on_load()
        await plugin.on_enable()
        plugin._client_obj = self.fake            # 走假 GitHub
        plugin.config.set("plugin_dir", str(self.plugin_dir))
        for key, value in (config or {}).items():
            plugin.config.set(key, value)
        self.plugin = plugin
        self.mgr._client.plugins = self.mgr
        return plugin

    async def stop(self):
        if self.plugin is not None:
            await self.plugin.on_unload()

    def use_fake_github(self):
        """让插件走假 GitHub（插件热重载后 `_client_obj` 会变回 None，要重新接）。"""
        self.plugin._new_client = lambda: self.fake
        self.plugin._client_obj = self.fake
        return self.fake

    def set_manifest(self, *entries: dict):
        self.fake.manifest_raw = {"version": 1, "name": "测试列表",
                                  "plugins": list(entries)}

    async def run(self, text: str, *, user_id: int = SUPERUSER,
                  message_type: str = "private", group_id: int | None = None):
        """真跑一遍命令（解析 → 权限 → 执行），返回**命令行内**的回复内容。

        后台任务的回执走 `api.send_private_msg`（用 `drain()` 收），
        命令行本身的回复走返回值（框架的投递通道在客户端里，这里直接看返回值）。
        """
        event = MessageEvent(
            message_type=message_type, user_id=user_id, group_id=group_id,
            self_id=1, sender=Sender(user_id=user_id, nickname="tester"),
            message=[{"type": "text", "data": {"text": text}}],
            raw_message=text)
        result = await self.cmds.handle(event)
        if isinstance(result, tuple):
            return None if result[0] == "_not_found" else "__unknown__"
        if result is None:
            return None
        items = result if isinstance(result, list) else [result]
        self.reply_text = "\n".join(str(i) for i in items)
        return self.reply_text

    def file_event(self, path: Path, *, text: str = "", user_id: int = SUPERUSER,
                   reply_to: int | None = None) -> MessageEvent:
        """一条「带文件段」的私聊消息事件（`file` 段指向本机路径）。"""
        segments = []
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        if reply_to is not None:
            segments.append({"type": "reply", "data": {"id": str(reply_to)}})
        segments.append({"type": "file", "data": {
            "name": path.name, "file_size": path.stat().st_size,
            "url": str(path)}})
        return MessageEvent(
            message_type="private", user_id=user_id, group_id=None,
            self_id=1, sender=Sender(user_id=user_id, nickname="tester"),
            message=segments, raw_message=text)

    async def run_file(self, path: Path, *, text: str = "",
                       user_id: int = SUPERUSER, reply_to: int | None = None):
        """把「带文件段的消息」丢给命令系统（等同于用户发文件顺带发命令）。"""
        event = self.file_event(path, text=text, user_id=user_id,
                                reply_to=reply_to)
        result = await self.cmds.handle(event)
        if isinstance(result, tuple):
            return None if result[0] == "_not_found" else "__unknown__"
        if result is None:
            return None
        items = result if isinstance(result, list) else [result]
        self.reply_text = "\n".join(str(i) for i in items)
        return self.reply_text

    async def drain(self, *, clear: bool = True):
        """等后台任务跑完，并把**这一批**的后台回执收进 `api.sent`。

        `clear=False` 用于「要先看命令当场回执、再看后台回执」的用例：
        安装任务可能在命令返回前就跑完，先清空会把两条都清掉。
        """
        plugin = self.plugin
        if clear:
            self.api.sent.clear()
        for _ in range(200):
            pending = [t for t in plugin._pending if not t.done()]
            if not pending:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.05)


# ==================== 1. 列表文件 ====================


def test_manifest():
    print("\n[1] 列表文件解析与校验")
    entry = registry.PluginEntry.parse({
        "name": "demo", "repo": "https://github.com/Green1423/ncqqpy-plugin-demo.git",
        "ref": "main", "description": "演示", "tags": "工具", "data": True}, 0)
    check("repo 链接会被抠成 owner/repo", entry.repo == "Green1423/ncqqpy-plugin-demo",
          entry.repo)
    check("字符串 tags 归一成列表", entry.tags == ["工具"], str(entry.tags))
    check("data 标记被读到", entry.keep_data is True)

    scp = registry.PluginEntry.parse(
        {"name": "x", "repo": "git@github.com:owner/repo.git"}, 0)
    check("scp 风格地址也认", scp.repo == "owner/repo", scp.repo)

    for bad, why in (
        ({"repo": "a/b"}, "缺 name"),
        ({"name": "ok"}, "缺 repo"),
        ({"name": "../evil", "repo": "a/b"}, "name 带路径穿越"),
        ({"name": "ok", "repo": "not-a-repo"}, "repo 不是 owner/repo"),
    ):
        try:
            registry.PluginEntry.parse(bad, 0)
            check(f"拒绝非法条目（{why}）", False, "居然通过了")
        except registry.ManifestError:
            check(f"拒绝非法条目（{why}）", True)

    manifest = registry.Manifest.parse({
        "version": 1, "plugins": [
            {"name": "a", "repo": "o/a"}, {"name": "b", "repo": "o/b"}]})
    check("正常列表解析出 2 条", manifest.names() == ["a", "b"], str(manifest.names()))

    try:
        registry.Manifest.parse({"version": 1, "plugins": [
            {"name": "a", "repo": "o/a"}, {"name": "a", "repo": "o/b"}]})
        check("重名条目被拒", False)
    except registry.ManifestError as exc:
        check("重名条目被拒", "重复" in str(exc), str(exc))

    try:
        registry.Manifest.parse({"version": 9, "plugins": [{"name": "a", "repo": "o/a"}]})
        check("更高版本号的列表被拒（提示先升级）", False)
    except registry.ManifestError as exc:
        check("更高版本号的列表被拒（提示先升级）", "版本" in str(exc), str(exc))

    check("valid_name 挡住穿越与分隔符",
          registry.valid_name("demo_1") and not registry.valid_name("../x")
          and not registry.valid_name("a/b"))


# ==================== 2. 更新计划 ====================


def test_plan():
    print("\n[2] 更新计划（用户手改 vs 上游改动）")
    old = {"main.py": "h1", "helper.py": "h2", "gone.py": "h3"}
    new = {"main.py": "h1", "helper.py": "hNEW", "fresh.py": "h9"}
    local = {"main.py": "h1", "helper.py": "hLOCAL", "gone.py": "h3",
             "mine.py": "hMINE"}
    plan = inst.plan_update(new, old, local)
    check("上游新增 → add", plan.add == ["fresh.py"], str(plan.add))
    check("本地改过 + 上游改过 → 冲突", plan.local_modified == ["helper.py"],
          str(plan.local_modified))
    check("用户自己的文件 → local_extra（永不删）",
          plan.local_extra == ["mine.py"], str(plan.local_extra))
    check("上游删掉、本地没动 → remove", plan.remove == ["gone.py"], str(plan.remove))
    check("内容一致 → keep", plan.keep == ["main.py"], str(plan.keep))
    check("冲突可以被 force 越过（conflicts 非空）", plan.conflicts == ["helper.py"])

    plan2 = inst.plan_update({"a.py": "h2"}, {"a.py": "h1"}, {"a.py": "h1"})
    check("本地没动过 → 直接更新", plan2.update == ["a.py"] and not plan2.conflicts,
          str(plan2.update))

    plan3 = inst.plan_update({"a.py": "h2", "b.py": "h5"}, {"a.py": "h1", "b.py": "h4"},
                             {"b.py": "h4"})
    check("用户删过 + 上游改了 → 冲突（不偷偷恢复）",
          plan3.local_deleted == ["a.py"], str(plan3.local_deleted))

    check("开发目录不参与同步（tests/docs/__pycache__）",
          inst.is_synced("main.py") and not inst.is_synced("__pycache__/x.pyc")
          and not inst.is_synced("tests/test_x.py")
          and not inst.is_synced("docs/a.md") and not inst.is_synced("README.md"))
    check("被跟踪文件数不含 data/ 与开发目录",
          inst.count_plugin_files({"main.py": "h", "data/x.json": "h",
                                   "tests/t.py": "h", "a/b.py": "h"}) == 2)


# ==================== 3. 解压安全 ====================


def test_extract(tmp: Path):
    print("\n[3] 源码包解压安全")
    tmp.mkdir(parents=True, exist_ok=True)

    good = tmp / "good.tar.gz"
    good.write_bytes(make_tarball({"__init__.py": "x = 1\n"}))
    root = inst.safe_extract(good, tmp / "out-good")
    check("正常包解出顶层目录", (root / "__init__.py").is_file(), str(root))

    def evil(name: str) -> Path:
        path = tmp / f"evil-{abs(hash(name))}.tar.gz"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"pwned"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        path.write_bytes(buf.getvalue())
        return path

    for name, why in (("../escape.txt", "路径穿越"),
                      ("/tmp/abs.txt", "绝对路径"),
                      ("top/../../escape2.txt", "穿出顶层目录")):
        try:
            inst.safe_extract(evil(name), tmp / "out-evil")
            check(f"拒绝{why}", False, "居然解出来了")
        except inst.InstallError:
            check(f"拒绝{why}", True)

    link = tmp / "link.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("top/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    link.write_bytes(buf.getvalue())
    try:
        inst.safe_extract(link, tmp / "out-link")
        check("拒绝包内符号链接", False)
    except inst.InstallError:
        check("拒绝包内符号链接", True)


# ==================== 4. 真实安装 / 更新 / 卸载 ====================


async def test_install_flow(work: Path):
    print("\n[4] 真实安装（本地 HTTP 服务当 GitHub）")
    server = LocalGitHub(dict(PLUGIN_FILES_V1)).start()
    try:
        root = work / "flow"
        harness = Harness(root)
        # codeload 指向本地服务：走真 HTTP 下载
        plugin = await harness.start(config={"manifest_url": server.manifest_url,
                                             "manifest_cache_seconds": 0})
        plugin._client_obj = None                      # 用真客户端（地址已改）
        server.manifest_raw = {"version": 1, "name": "本地列表", "plugins": [
            {"name": "demo", "repo": "Green1423/demo-repo", "ref": "main",
             "description": "演示插件", "version": "1.0.0"}]}
        plugin._client_obj = None
        from plugin_manager.github import GithubClient
        plugin._new_client = lambda: GithubClient(
            api_base=f"http://127.0.0.1:{server.port}/api",
            codeload=server.codeload, timeout=10)

        out = await harness.run("/plugin list")
        check("list 列出列表文件里的插件", "demo" in (out or ""), str(out))

        out = await harness.run("/plugin install demo")
        check("install 立刻回执（不阻塞命令）", "开始安装" in (out or ""), str(out))
        await harness.drain()
        texts = harness.api.texts()
        check("回执里有安装完成", "安装完成" in texts, texts)

        target = harness.plugin_dir / "demo"
        check("插件目录已建立", (target / "main.py").is_file())
        check("tests/ 不同步进插件目录", not (target / "tests").is_dir())
        check("README 不同步", not (target / "README.md").exists())
        check("记账文件落在插件目录里", (target / inst.META_FILE).is_file())
        check("data/ 保留（仓库里带的运行时数据）",
              (target / "data" / "user_state.json").is_file())
        record = harness.plugin._store.get("demo")
        check("全局记账写了来源仓库与 ref",
              record is not None and record.repo == "Green1423/demo-repo"
              and record.ref == "main", str(record))
        check("记账里有文件哈希", bool(record and record.files.get("main.py")))
        check("记账里记下了提交号（源码包没给，就从 API 补）",
              bool(record and record.sha and record.sha == server.head_sha),
              str(record.sha if record else None))
        check("框架里已加载 demo 插件", harness.mgr.get("demo") is not None)

        # ---- 上游改一个文件 → 更新 ----
        print("\n[5] 真实更新")
        v2 = dict(PLUGIN_FILES_V1)
        v2["helper.py"] = "VALUE = 2\n"
        v2["fresh.py"] = "NEW = True\n"
        server.files = v2
        out = await harness.run("/plugin update demo")
        check("update 有回执", "开始更新" in (out or ""), str(out))
        await harness.drain()
        texts = harness.api.texts()
        check("更新报告里说明改了哪些文件", "更新" in texts or "⬆️" in texts, texts)
        check("上游改动落地", (target / "helper.py").read_text()
              == "VALUE = 2\n")
        check("上游新增落地", (target / "fresh.py").is_file())
        check("data/ 在更新后仍在", (target / "data" / "user_state.json").is_file())

        # check：装了 + 提交号一致 → 已是最新
        out = await harness.run("/plugin check")
        check("check 报到「已是最新」", "已是最新" in (out or ""), str(out))

        # 再更新一次：内容没变 → noop
        await harness.run("/plugin update demo")
        await harness.drain()
        check("内容没变时报告「已是最新」", "已是最新" in harness.api.texts(),
              harness.api.texts())

        # ---- 用户手改 + 上游也改 → 冲突 ----
        print("\n[6] 冲突保护")
        (target / "helper.py").write_text("VALUE = 999  # 我手改的\n")
        v3 = dict(server.files)
        v3["helper.py"] = "VALUE = 3\n"
        server.files = v3
        await harness.run("/plugin update demo")
        await harness.drain()
        texts = harness.api.texts()
        check("冲突时拒绝覆盖并说明原因", "冲突" in texts, texts)
        check("用户手改的内容还在",
              "我手改的" in (target / "helper.py").read_text())
        check("本地多余文件被保留",
              True)                                  # 下面 force 时一起验
        (target / "mine.py").write_text("MY_PATCH = 1\n")

        await harness.run("/plugin update demo --force=true")
        await harness.drain()
        check("force 之后上游版本落地",
              (target / "helper.py").read_text() == "VALUE = 3\n",
              (target / "helper.py").read_text())
        check("force 也不能删掉用户自己加的文件", (target / "mine.py").is_file())
        check("被覆盖的旧版本进了回收站",
              any("demo-" in p.name for p in
                  (harness.plugin_dir / ".trash").iterdir()))

        # ---- 卸载 ----
        print("\n[7] 真实卸载")
        out = await harness.run("/plugin uninstall demo")
        check("卸载有回执", "已卸载" in (out or ""), str(out))
        check("默认只留 data/，代码都移走",
              target.is_dir() and not (target / "main.py").exists()
              and (target / "data" / "user_state.json").is_file(),
              str(sorted(p.name for p in target.iterdir())) if target.is_dir() else "目录也没了")
        check("框架里也卸载了", harness.mgr.get("demo") is None)
        check("安装记录已清", harness.plugin._store.get("demo") is None)
        trash = list((harness.plugin_dir / ".trash").glob("demo-*"))
        check("回收站里能找到它",
              any((p / "main.py").is_file() for p in trash), str(trash))

        out = await harness.run("/plugin trash")
        check("trash 能列出备份", "回收站" in (out or ""), str(out))
        out = await harness.run("/plugin trash --purge=true")
        check("trash purge 能清空", "已清理" in (out or ""), str(out))
        check("回收站真的空了",
              not any((harness.plugin_dir / ".trash").iterdir())
              if (harness.plugin_dir / ".trash").is_dir() else True)

        # ---- --purge=true：连 data/ 一起进回收站，目录彻底消失 ----
        server.files = dict(PLUGIN_FILES_V1)
        await harness.run("/plugin install demo")
        await harness.drain()
        await harness.run("/plugin uninstall demo --purge=true")
        check("--purge=true 时整个目录都移走", not target.exists())
        trashed = list((harness.plugin_dir / ".trash").glob("demo-*"))
        check("data/ 也在回收站里（没被删，能找回）",
              any((p / "data" / "user_state.json").is_file() for p in trashed),
              str(trashed))

        # ---- --purge=false：data/ 原地保留，重装即续上 ----
        await harness.run("/plugin trash --purge=true")
        server.files = dict(PLUGIN_FILES_V1)
        await harness.run("/plugin install demo")
        await harness.drain()
        (harness.plugin_dir / "demo" / "data" / "user_state.json").write_text(
            '{"keep": "yes"}\n')
        await harness.run("/plugin uninstall demo --purge=false")
        check("--purge=false 时 data/ 留在原地",
              (harness.plugin_dir / "demo" / "data" / "user_state.json").is_file())

        await harness.stop()
    finally:
        server.stop()


# ==================== 8. 权限 ====================


async def test_permissions(work: Path):
    print("\n[8] 权限：仅私聊 · 仅超管")
    harness = Harness(work / "perm")
    plugin = await harness.start()
    harness.set_manifest({"name": "demo", "repo": "o/demo"})
    for cmd in ("/plugin list", "/plugin install demo", "/plugin uninstall demo"):
        result = await harness.run(cmd, user_id=STRANGER)
        check(f"非超管私聊拿不到 {cmd}", result is None, str(result))
    result = await harness.run("/plugin list", user_id=SUPERUSER,
                               message_type="group", group_id=123456)
    check("超管在群里也拿不到（仅私聊）", result is None, str(result))
    result = await harness.run("/plugin info demo", user_id=SUPERUSER)
    check("超管私聊可用", result is not None and "demo" in result, str(result))

    parent = harness.cmds._commands.get("plugin")
    subs = getattr(parent, "subcommands", {}) or {}
    check("命令都注册上了", all(
        name in subs for name in ("list", "info", "check", "install", "update",
                                  "uninstall", "add", "upload", "refresh",
                                  "trash")),
        str(sorted(subs)))
    for name in ("list", "install", "update", "uninstall", "add", "upload",
                 "trash"):
        handler = subs.get(name)
        check(f"{name} 声明了 superuser + private",
              handler is not None and handler.permission == "superuser"
              and handler.scope == "private",
              f"{getattr(handler, 'permission', None)}/{getattr(handler, 'scope', None)}")
    await harness.stop()


# ==================== 9. 忙锁 / 离线兜底 ====================


async def test_resilience(work: Path):
    print("\n[9] 忙锁与离线兜底")
    harness = Harness(work / "res")
    plugin = await harness.start()
    harness.set_manifest({"name": "demo", "repo": "o/demo"})
    plugin._busy = True
    out = await harness.run("/plugin install demo")
    check("忙时 install 被拦下并如实回话",
          out is not None and "还没跑完" in out, str(out))
    plugin._busy = False

    # 列表文件拉不到 → 用磁盘缓存
    harness.use_fake_github()
    await harness.run("/plugin refresh")
    from plugin_manager.github import GithubError
    plugin._client_obj.clear_cache()      # 假客户端没有缓存，这里只是走一遍真实路径
    harness.fake.manifest_error = GithubError("假客户端：断网了")
    out = await harness.run("/plugin list")
    check("断网时用本地缓存列表并标注",
          out is not None and "离线" in out, str(out))
    # 连磁盘缓存与客户端内部缓存都拿掉 → 必须如实报错，而不是装作「列表是空的」
    plugin.cached_manifest_path().unlink(missing_ok=True)
    plugin._client_obj.clear_cache()
    # 框架把命令里的异常渲染成「一句带编号的回话」而不是抛给调用方 ——
    # 断言这句回话里确实有失败原因（而不是拿旧列表糊弄过去）
    out = await harness.run("/plugin refresh")
    check("断网且无缓存时如实报错",
          out is not None and "断网" in out and "已重新拉取" not in out, str(out))
    harness.fake.manifest_error = None

    # /plugin add：列表外的仓库
    harness.use_fake_github()
    out = await harness.run("/plugin add o/other-repo")
    check("add 接受 owner/repo 并排后台任务", "开始安装" in (out or ""), str(out))
    await harness.drain()
    check("仓库里声明的 name 与插件名不一致时拒绝安装（避免装错目录）",
          not (harness.plugin_dir / "other_repo").exists()
          and "不一致" in harness.api.texts(), harness.api.texts())

    out = await harness.run("/plugin add o/other-repo --force=true")
    await harness.drain()
    check("force 之后按列表外的名字装上了",
          (harness.plugin_dir / "other_repo" / "main.py").is_file()
          and "安装完成" in harness.api.texts(), harness.api.texts())
    await harness.stop()


# ==================== 10. 依赖 ====================


def dep_files(name: str, requires=()) -> dict[str, str]:
    """一个声明了依赖的最小插件包（用于 dependency 相关用例）。"""
    extra = "".join(f"    requires = {list(requires)!r}\n" if requires else "")
    return {
        "__init__.py": "from .main import P\n",
        "main.py": ("from napcat.plugin import PluginBase\n\n\nclass P(PluginBase):\n"
                    f"    name = {name!r}\n    version = '1.0.0'\n{extra}"),
    }


async def test_dependencies(work: Path):
    print("\n[10] 依赖：装使用者 → 提示缺谁 → 装依赖后自动加载")
    harness = Harness(work / "deps")
    plugin = await harness.start()
    harness.set_manifest(
        {"name": "app", "repo": "local/app", "ref": "main", "description": "使用者"},
        {"name": "store", "repo": "local/store", "ref": "main", "description": "被依赖"})

    harness.fake.files = dep_files("app", requires=["store"])
    await harness.run("/plugin install app")
    await harness.drain()
    texts = harness.api.texts()
    check("回执说明缺哪个必需依赖", "还缺必需依赖" in texts and "store" in texts, texts)
    check("回执给出补齐命令", "/plugin install store" in texts, texts)
    check("依赖缺失时框架确实没加载它", harness.mgr.get("app") is None)
    check("框架记下了阻塞原因",
          "store" in harness.mgr.blocked_reason("app"),
          harness.mgr.blocked_reason("app"))

    out = await harness.run("/plugin list")
    check("合并后的总览标出被依赖挡住的插件",
          "⛔" in (out or "") and "缺少 store" in (out or ""), str(out))
    out = await harness.run("/plugin info app")
    check("info 展示依赖", "依赖：store" in (out or ""), str(out))

    harness.fake.files = dep_files("store")
    await harness.run("/plugin install store")
    await harness.drain()
    check("依赖装上后使用者自动加载", harness.mgr.get("app") is not None)
    check("并补了一条回执", "自动加载" in harness.api.texts(), harness.api.texts())
    app = harness.mgr.get("app")
    check("运行期能拿到依赖实例",
          app is not None and app.deps.get("store") is harness.mgr.get("store"))

    out = await harness.run("/plugin check")
    check("check 不因依赖问题出错", out is not None, str(out))
    await harness.stop()


# ==================== 11. 上传 zip 安装 ====================


def plugin_zip(name: str, *, version: str = "1.0.0", extra: dict | None = None,
               wrapper: str | None = None, requirements: str = "") -> bytes:
    """打一个「合法插件包」的 zip。

    约定的打包方式：zip 里只有一个顶层文件夹，**文件夹名就是插件名**；
    `wrapper=""` 用于构造"根目录直接放文件"的坏包。
    """
    files = {
        "__init__.py": "from .main import P\n",
        "main.py": ("from napcat import PluginBase\n\n\nclass P(PluginBase):\n"
                    f"    name = {name!r}\n    version = {version!r}\n"),
    }
    if requirements:
        files["requirements.txt"] = requirements
    files.update(extra or {})
    folder = name if wrapper is None else wrapper
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, text in files.items():
            zf.writestr(f"{folder}/{rel}" if folder else rel, text)
    return buf.getvalue()


def bad_zip(kind: str) -> bytes:
    """构造各种「不该被接受」的 zip。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if kind == "traversal":
            zf.writestr("../escape.py", "x = 1\n")
        elif kind == "absolute":
            zf.writestr("/tmp/escape.py", "x = 1\n")
        elif kind == "backslash":
            zf.writestr("..\\escape.py", "x = 1\n")
        elif kind == "symlink":
            info = zipfile.ZipInfo("demo/link")
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
        elif kind == "no-entry":
            zf.writestr("demo/readme.md", "no code here\n")
        elif kind == "root-files":
            zf.writestr("__init__.py", "x = 1\n")
            zf.writestr("main.py", "y = 1\n")
        elif kind == "two-folders":
            zf.writestr("a/__init__.py", "x = 1\n")
            zf.writestr("b/__init__.py", "y = 1\n")
        elif kind == "single-file":
            zf.writestr("demo.py", "x = 1\n")
        elif kind == "empty":
            pass
    return buf.getvalue()


def write_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_zip_limits():
    """zip 检查的纯逻辑部分：上限与同名条目（不用起插件）。"""
    print("\n[11.0] zip 检查（纯逻辑：上限 / 同名条目）")
    limits = zips_mod.ZipLimits()
    members, _names, _warn, folder = zips_mod.inspect_zip(plugin_zip("demo"),
                                                          limits=limits)
    check("正常包通过检查（2 个文件）", len(members) == 2)
    check("顶层文件夹名被识别（= 插件名）", folder == "demo", folder)

    def rejected(data: bytes, *, limits) -> str:
        try:
            zips_mod.inspect_zip(data, limits=limits)
            return ""
        except zips_mod.ZipError as exc:
            return str(exc)

    reason = rejected(plugin_zip("demo"),
                      limits=zips_mod.ZipLimits(max_uncompressed_bytes=10))
    check("解压总量超限被拒（zip 炸弹防护）", "zip 炸弹" in reason or "总量" in reason,
          reason)
    reason = rejected(plugin_zip("demo"),
                      limits=zips_mod.ZipLimits(max_file_bytes=10))
    check("单文件超限被拒", "太大" in reason, reason)
    reason = rejected(plugin_zip("demo"),
                      limits=zips_mod.ZipLimits(max_files=1))
    check("文件数超限被拒", "文件太多" in reason, reason)
    reason = rejected(plugin_zip("demo"),
                      limits=zips_mod.ZipLimits(max_zip_bytes=10))
    check("压缩包本身超限被拒", "压缩包太大" in reason, reason)

    dup = io.BytesIO()
    with zipfile.ZipFile(dup, "w") as zf:
        zf.writestr("demo/main.py", "A = 1\n")
        zf.writestr("demo/main.py", "A = 2\n")
    reason = rejected(dup.getvalue(), limits=limits)
    check("同名文件被拒（后写会覆盖先写）", "同名文件" in reason, reason)

    for kind, label in (("root-files", "根目录直接放文件"),
                        ("two-folders", "两个顶层文件夹"),
                        ("single-file", "顶层只有单个文件")):
        reason = rejected(bad_zip(kind), limits=limits)
        check(f"结构不对被拒：{label}", "只能有一个" in reason, reason)


async def test_upload(work: Path):
    print("\n[11] 上传安装：/oj-submit 式流程（校验→等待→安装）/ 包原样")
    harness = Harness(work / "upload")
    plugin = await harness.start(config={"upload.local_roots": [str(work)]})
    harness.set_manifest({"name": "demo", "repo": "local/demo", "ref": "main"})
    harness.use_fake_github()
    good = write_file(work / "demo.zip",
                      plugin_zip("demo", extra={"README.md": "# demo\n"}))

    # ---- 11.1 同一条消息带 zip：校验通过才落后台 ----
    out = await harness.run_file(good, text="/plugin upload")
    check("同消息 zip：命令当场回「校验通过」", "校验通过" in (out or ""), str(out))
    check("同消息 zip：命令回执里没有「安装完成」（装是后台的）",
          "安装完成" not in (out or ""), str(out))
    await harness.drain()
    texts = harness.api.texts()
    check("后台回执有安装完成", "安装完成" in texts, texts)
    check("插件目录已建立", (harness.plugin_dir / "demo" / "main.py").is_file())
    check("README 按包原样保留（不再过滤）",
          (harness.plugin_dir / "demo" / "README.md").is_file())
    record = plugin._store.get("demo")
    check("记账来源标成 upload",
          record is not None and record.source == "upload", str(record))
    check("记账里是 zip 指纹（sha256）",
          record is not None and len(record.sha) == 64, str(record.sha if record else ""))
    check("框架里已加载", harness.mgr.get("demo") is not None)

    # ---- 11.2 pending：先发命令（/oj submit 式提示），再发文件 ----
    await harness.run("/plugin uninstall demo --purge=true")
    out = await harness.run("/plugin upload")
    check("upload 无附件时给出等待提示", "zip 发上来" in (out or ""), str(out))
    check("提示里有取消指令", "/plugin cancel" in (out or ""), str(out))
    check("提示里写明打包方式（只有一个文件夹、文件夹名即插件名）",
          "只有一个文件夹" in (out or "") and "文件夹名就是插件名" in (out or ""),
          str(out))
    await harness.bus.dispatch(harness.file_event(good))
    await harness.drain()
    check("pending 自动接住下一条文件", harness.mgr.get("demo") is not None,
          harness.api.texts())
    check("pending 被消费（不会重复触发）",
          plugin._upload_pending == {}, str(plugin._upload_pending))

    # ---- 11.3 引用一条文件消息 ----
    await harness.run("/plugin uninstall demo --purge=true")
    harness.api.messages[4242] = {"message": [{"type": "file", "data": {
        "name": "demo.zip", "file_size": good.stat().st_size, "url": str(good)}}]}
    event = harness.file_event(good, text="/plugin upload", reply_to=4242)
    event.message = [seg for seg in event.message if seg.get("type") != "file"]
    result = await harness.cmds.handle(event)
    check("引用消息：命令当场回「校验通过」", "校验通过" in str(result), str(result))
    await harness.drain()
    check("引用消息：装上了", harness.mgr.get("demo") is not None,
          harness.api.texts())

    # ---- 11.4 sample_pkg 式包：类写在 __init__.py、无 main.py、带 README ----
    await harness.run("/plugin uninstall demo --purge=true")
    pkg_buf = io.BytesIO()
    with zipfile.ZipFile(pkg_buf, "w") as zf:
        zf.writestr("sample_pkg/__init__.py",
                    "from napcat import PluginBase\n\n\n"
                    "class SamplePkgPlugin(PluginBase):\n"
                    "    name = 'sample_pkg'\n    version = '1.2.0'\n")
        zf.writestr("sample_pkg/sample_api.py", "VALUE = 1\n")
        zf.writestr("sample_pkg/README.md", "# sample_pkg\n")
    pkg_zip = write_file(work / "sample_pkg.zip", pkg_buf.getvalue())
    out = await harness.run_file(pkg_zip, text="/plugin upload")
    check("「类写在 __init__.py」式包（无 main.py）通过校验", "校验通过" in (out or ""), str(out))
    await harness.drain()
    target = harness.plugin_dir / "sample_pkg"
    check("「类写在 __init__.py」式包按原样安装",
          (target / "__init__.py").is_file() and (target / "sample_api.py").is_file()
          and (target / "README.md").is_file(),
          str(sorted(p.name for p in target.iterdir())) if target.is_dir() else "无目录")
    check("识别包内 name/version", plugin._store.get("sample_pkg").version == "1.2.0",
          str(plugin._store.get("sample_pkg")))
    check("框架能加载它", harness.mgr.get("sample_pkg") is not None)
    await harness.run("/plugin uninstall sample_pkg --purge=true")

    # ---- 11.5 各种坏包：命令当场拒绝，一个都不落盘 ----
    for kind, label, needle in (
            ("traversal", "路径穿越", "路径穿越"),
            ("absolute", "绝对路径", "绝对路径"),
            ("backslash", "反斜杠穿越", "路径穿越"),
            ("symlink", "符号链接", "符号链接"),
            ("empty", "空包", "空"),
            ("root-files", "根目录直接放文件", "根目录只能有一个文件夹"),
            ("two-folders", "两个顶层文件夹", "根目录只能有一个文件夹"),
            ("single-file", "顶层只有单个文件", "文件夹")):
        bad = write_file(work / f"bad-{kind}.zip", bad_zip(kind))
        out = await harness.run_file(bad, text="/plugin upload")
        check(f"坏包当场被拒：{label}",
              (out or "").startswith("❌") and needle in (out or ""), str(out))
    await harness.run("/plugin uninstall demo --purge=true")
    out = await harness.run_file(write_file(work / "notzip.zip", b"hello world"),
                                 text="/plugin upload")
    check("不是 zip 的文件当场被拒", "不是 zip" in (out or ""), str(out))
    missing = write_file(work / "no-entry.zip", bad_zip("no-entry"))
    out = await harness.run_file(missing, text="/plugin upload")
    check("没有入口文件的包当场被拒",
          "既没有 __init__.py" in (out or ""), str(out))
    check("坏包都没有落盘", not (harness.plugin_dir / "demo").exists())

    # ---- 11.6 大小闸（消息段声明大小先挡，不下载） ----
    plugin.config.set("upload.max_zip_bytes", 200)
    out = await harness.run_file(good, text="/plugin upload")
    check("超过大小上限当场被拒", "太大" in (out or ""), str(out))
    plugin.config.set("upload.max_zip_bytes", 20 * 1024 * 1024)

    # ---- 11.7 插件名 = 文件夹名（zip 文件名/代码声明都不算数） ----
    renamed = write_file(work / "something-else.zip", plugin_zip("demo"))
    out = await harness.run_file(renamed, text="/plugin upload")
    check("zip 文件名不影响插件名", "校验通过" in (out or ""), str(out))
    await harness.drain()
    check("按文件夹名装上", (harness.plugin_dir / "demo" / "main.py").is_file())
    await harness.run("/plugin uninstall demo --purge=true")

    declared_other = io.BytesIO()
    with zipfile.ZipFile(declared_other, "w") as zf:
        zf.writestr("demo/__init__.py",
                    "from napcat import PluginBase\n\n\nclass P(PluginBase):\n"
                    "    name = 'other'\n    version = '1.0.0'\n")
    mismatch = write_file(work / "declared-other.zip", declared_other.getvalue())
    out = await harness.run_file(mismatch, text="/plugin upload")
    check("包内 name 与文件夹名不一致当场被拒", "不一致" in (out or ""), str(out))
    check("不一致时没有装", not (harness.plugin_dir / "demo").exists())
    out = await harness.run_file(mismatch, text="/plugin upload --force=true")
    check("force 后校验通过", "校验通过" in (out or ""), str(out))
    await harness.drain()
    check("force 也只按文件夹名装（不会装成 other）",
          (harness.plugin_dir / "demo" / "__init__.py").is_file()
          and not (harness.plugin_dir / "other").exists(), harness.api.texts())
    await harness.run("/plugin uninstall demo --purge=true")

    illegal = io.BytesIO()
    with zipfile.ZipFile(illegal, "w") as zf:
        zf.writestr("中文插件/__init__.py", "x = 1\n")
    out = await harness.run_file(
        write_file(work / "illegal-name.zip", illegal.getvalue()),
        text="/plugin upload")
    check("文件夹名不能当插件名时当场被拒", "不能当插件名" in (out or ""), str(out))

    # ---- 11.8 requirements：成功 / 失败回滚（后台部分） ----
    harness.fake.files = {
        "__init__.py": "from .main import P\n",
        "main.py": ("from napcat import PluginBase\n\n\nclass P(PluginBase):\n"
                    "    name = 'demo'\n    version = '1.0.0'\n"),
    }
    await harness.run("/plugin install demo")
    await harness.drain()
    check("仓库装出 v1", (harness.plugin_dir / "demo" / "main.py").is_file())

    v2 = plugin_zip("demo", version="2.0.0", requirements="demo-dep>=1.0\n",
                    extra={"helper.py": "V = 2\n"})
    pip_calls: list[list[str]] = []

    async def fake_pip_ok(cmd, cwd, timeout):
        pip_calls.append(list(cmd))
        check("pip 用的是当前解释器", cmd[0] == sys.executable, str(cmd[:3]))
        check("pip 命令带 -r 与 --no-input",
              "-r" in cmd and "--no-input" in cmd, str(cmd))
        check("pip 在暂存目录里跑（相对路径可解析）",
              Path(cwd).is_dir(), str(cwd))
        return 0, "Successfully installed demo-dep-1.0"

    plugin._pip_runner = fake_pip_ok
    await harness.run_file(write_file(work / "v2.zip", v2), text="/plugin upload")
    await harness.drain()
    texts = harness.api.texts()
    check("requirements 被安装", pip_calls and "requirements.txt 已安装" in texts,
          texts)
    check("新版本落地", (harness.plugin_dir / "demo" / "helper.py").is_file())

    async def fake_pip_fail(cmd, cwd, timeout):
        return 1, "ERROR: No matching distribution found for demo-dep"

    plugin._pip_runner = fake_pip_fail
    v3 = plugin_zip("demo", version="3.0.0", requirements="demo-dep>=9.9\n",
                    extra={"helper.py": "V = 3\n"})
    await harness.run_file(write_file(work / "v3.zip", v3), text="/plugin upload")
    await harness.drain()
    texts = harness.api.texts()
    check("pip 失败：回执说明放弃安装", "放弃本次安装" in texts, texts)
    check("pip 失败：附了 pip 输出尾部",
          "No matching distribution" in texts, texts)
    check("pip 失败：旧版本原样可用（v2 的 helper 还在）",
          "V = 2" in (harness.plugin_dir / "demo" / "helper.py").read_text(),
          (harness.plugin_dir / "demo" / "helper.py").read_text())
    check("pip 失败：记账仍是 v2",
          plugin._store.get("demo").version == "2.0.0",
          str(plugin._store.get("demo")))

    # ---- 11.9 requirements 文件本身不合法（后台解析失败，旧版本不动） ----
    plugin._pip_runner = fake_pip_ok
    pip_calls.clear()
    bad_req = plugin_zip("demo", requirements="-r other.txt\n")
    await harness.run_file(write_file(work / "bad-req.zip", bad_req),
                           text="/plugin upload --force=true")
    await harness.drain()
    check("requirements 里 -r 被拒且不跑 pip",
          "requirements.txt 第 1 行" in harness.api.texts() and not pip_calls,
          harness.api.texts())

    # ---- 11.10 同名目录不是本管理器装的 ----
    await harness.run("/plugin uninstall demo --purge=true")
    (harness.plugin_dir / "foreign").mkdir(parents=True, exist_ok=True)
    (harness.plugin_dir / "foreign" / "main.py").write_text("x = 1\n")
    foreign = write_file(work / "foreign.zip", plugin_zip("foreign"))
    out = await harness.run_file(foreign, text="/plugin upload")
    check("同名但非本管理器装的：当场拒绝",
          "不是本管理器装的" in (out or ""), str(out))
    check("默认拒绝时目录没被动",
          (harness.plugin_dir / "foreign" / "main.py").read_text() == "x = 1\n")
    out = await harness.run_file(foreign, text="/plugin upload --force=true")
    await harness.drain()
    check("--force 才接管同名目录",
          (harness.plugin_dir / "foreign" / "__init__.py").is_file(),
          harness.api.texts())

    # ---- 11.11 上传自己被拒 ----
    self_zip = write_file(work / "self.zip", plugin_zip("plugin_manager"))
    out = await harness.run_file(self_zip, text="/plugin upload")
    check("不允许上传覆盖 plugin_manager 自己",
          "不能在运行时装/卸自己" in (out or "")
          or "plugin_manager 自己" in (out or ""), str(out))

    # ---- 11.12 pending 语义（照 /oj submit） ----
    await harness.run("/plugin cancel")
    out = await harness.run("/plugin upload")
    check("重新开始等待", "zip 发上来" in (out or ""), str(out))
    # 坏 zip：错误回执，窗口保留
    await harness.bus.dispatch(harness.file_event(
        write_file(work / "bad-pending.zip", b"not a zip")))
    # 这一步是**同步**校验（照 submit），回执在 dispatch 返回时已经发出；
    # 先断言再 drain（drain 会清空 api.sent，只留后台任务的回执）。
    check("pending 文件校验失败会回执「命令不用重发」",
          "命令不用重发" in harness.api.texts(), harness.api.texts())
    check("pending 校验失败后窗口保留", plugin._upload_pending != {},
          str(plugin._upload_pending))
    # 普通命令消息不吞窗口（/oj submit 的「命令前缀不吞」）
    await harness.run("/plugin list")
    check("发别的命令不会消费窗口", plugin._upload_pending != {})
    # 发正确 zip 即可继续
    await harness.bus.dispatch(harness.file_event(good))
    await harness.drain()
    check("保留窗口后直接重发正确 zip 即可", harness.mgr.get("demo") is not None,
          harness.api.texts())
    # cancel
    out = await harness.run("/plugin upload")
    check("再次进入等待", plugin._upload_pending != {})
    out = await harness.run("/plugin cancel")
    check("/plugin cancel 取消成功", "已取消" in (out or ""), str(out))
    check("取消后窗口清空", plugin._upload_pending == {})
    out = await harness.run("/plugin cancel")
    check("没有等待时如实说明", "没有等待" in (out or ""), str(out))

    await harness.stop()


# ==================== 12. /plugin list 与 /plugins 合并 ====================


async def test_merged_list(work: Path):
    print("\n[12] /plugin list 与 /plugins 合并 + 权限")
    harness = Harness(work / "merged")
    plugin = await harness.start()
    harness.set_manifest(
        {"name": "installed_one", "repo": "local/one", "ref": "main",
         "description": "已装的一个"},
        {"name": "available_one", "repo": "local/two", "ref": "main",
         "description": "还没装的一个"})
    # 装一个（用 GitHub 路径，产生加载 + 记账）
    harness.fake.files = dep_files("installed_one")
    await harness.run("/plugin install installed_one")
    await harness.drain()
    check("前置：装好一个", harness.mgr.get("installed_one") is not None)

    # 本地手工放一个（不在列表文件里）—— 合并视图应能看到
    manual = harness.plugin_dir / "manual"
    manual.mkdir(parents=True, exist_ok=True)
    for rel, text in dep_files("manual").items():
        (manual / rel).write_text(text, encoding="utf-8")
    await harness.mgr.load("manual")

    merged = await harness.run("/plugin list")
    check("总览含「已安装」段", "已安装：" in (merged or ""), str(merged))
    check("总览含「可安装」段", "可安装：" in (merged or ""), str(merged))
    check("已装插件带运行状态", "[✓] installed_one" in (merged or ""), str(merged))
    check("未装插件在可安装段", "available_one" in (merged or ""), str(merged))
    check("手工插件也在已安装段里（标「手工安装」）",
          "manual" in (merged or "") and "手工安装" in (merged or ""), str(merged))

    alias = await harness.run("/plugins")
    check("/plugins 与 /plugin list 输出一致", alias == merged,
          f"list={merged!r} / plugins={alias!r}")
    alias_only = await harness.run("/plugins installed")
    check("/plugins 支持 installed 过滤",
          "available_one" not in (alias_only or "")
          and "installed_one" in (alias_only or ""), str(alias_only))
    alias_kw = await harness.run("/plugins available")
    check("/plugins 支持关键字过滤",
          "available_one" in (alias_kw or "") and "installed_one" not in (alias_kw or ""),
          str(alias_kw))

    # ---- 权限：非超管 / 群聊都拿不到（含旧 /plugins）----
    for command in ("/plugin list", "/plugins", "/plugin upload", "/plugin trash"):
        result = await harness.run(command, user_id=STRANGER)
        check(f"陌生人私聊拿不到 {command}", result is None, str(result))
    for command in ("/plugins", "/plugin list"):
        result = await harness.run(command, user_id=SUPERUSER,
                                   message_type="group", group_id=123)
        check(f"超管在群里也拿不到 {command}", result is None, str(result))

    subs = harness.cmds._commands["plugin"].subcommands
    bad = [name for name, handler in subs.items()
           if handler.permission != "superuser" or handler.scope != "private"]
    check("plugin 的**所有**子命令都是 superuser + private", not bad, str(bad))
    alias_handler = harness.cmds._commands.get("plugins")
    check("/plugins 别名也是 superuser + private",
          alias_handler is not None and alias_handler.permission == "superuser"
          and alias_handler.scope == "private",
          f"{getattr(alias_handler, 'permission', None)}/"
          f"{getattr(alias_handler, 'scope', None)}")
    check("内置 /plugins 已被移除（只剩管理器注册的别名）",
          alias_handler is not None
          and getattr(alias_handler, "plugin", "") == "plugin_manager",
          str(getattr(alias_handler, "plugin", "")))
    await harness.stop()


async def main_async(tmp: Path):
    test_manifest()
    test_plan()
    test_extract(tmp / "extract")
    await test_install_flow(tmp)
    await test_permissions(tmp)
    await test_resilience(tmp)
    await test_dependencies(tmp)
    test_zip_limits()
    await test_upload(tmp)
    await test_merged_list(tmp)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ncqqpy-plugin-manager-"))
    print(f"临时目录：{tmp}")
    try:
        asyncio.run(main_async(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 60}\n通过 {_passed} · 失败 {_failed}")
    if _failures:
        print("失败项：")
        for name in _failures:
            print(f"  - {name}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
