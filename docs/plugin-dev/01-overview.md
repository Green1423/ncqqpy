# 一、插件概述

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 下一章：[插件可用资源](02-resources.md)


插件是 ncqqpy 的核心扩展机制。每个插件是一个独立的 Python 包目录（`plugins/name/__init__.py`）或单文件（`plugins/name.py`），继承 `PluginBase` 即可被框架自动发现和加载。

## 1.1 最小插件

```python
# plugins/hello/__init__.py
from napcat.plugin import PluginBase
class HelloPlugin(PluginBase):
    name = "hello"
    version = "1.0.0"
    description = "我的第一个插件"
    author = "Me"

    async def on_enable(self):
        self.logger.info("Hello 插件已启用")
```

## 1.2 插件生命周期

```text
PluginManager.load()
  └→ __init__(api, event_bus, timer, ...)   # 注入依赖
  └→ on_load()                               # 初始化、不注册事件

PluginManager.enable()
  └→ on_enable()                             # ★ 注册事件/命令/定时器

...运行中...                                   # 处理事件、响应命令

PluginManager.disable()
  └→ on_disable()                            # 自动注销本次注册的处理器

PluginManager.unload()
  └→ on_unload()                             # 释放资源、清理
```

- **`on_load`**: 仅在加载时调用一次。可用于读取配置、初始化数据库连接等。**不应在此注册事件处理器**（插件可能被禁用而不卸载）。
- **`on_enable`**: 插件激活时调用。**在此注册命令、事件处理器、定时任务**。
- **`on_disable`**: 插件停用时调用。通过 `self.on_message` / `self.on_event` 注册的处理器**自动取消**。
- **`on_unload`**: 插件卸载时调用。释放文件句柄、关闭连接等。

## 1.3 声明依赖别的插件（必需 / 可选）

插件可以声明自己依赖哪些插件，框架负责**加载顺序**、**缺依赖时不加载**与
**反向保护**（别人在用的插件不能被悄悄卸掉）：

```python
class ReportPlugin(PluginBase):
    name = "report"
    version = "1.0.0"

    # 必需依赖：没装/没启用 → **本插件不加载**，原因私聊超管
    requires = ["store", "ai_chat>=1.2"]

    # 可选依赖：有就用、没有照常跑（`self.deps.get(...)` 拿到 None）
    optional_requires = ["weather"]

    async def on_load(self):
        store = self.deps.require("store")      # 必需依赖，加载期已保证在
        weather = self.deps.get("weather")      # 可选依赖：可能没有
        if weather is not None:
            ...                                 # 有天气插件就用它
```

| 概念 | 语义 |
|------|------|
| `requires` | 必需依赖。缺失/**未启用**/版本不符 → 本插件**不加载**（不构造实例），原因私聊超管，`/plugins` 里显示 `[⛔] 名字 —— 缺少 X` |
| `optional_requires` | 可选依赖。缺失不算失败，只是装/卸时的提示；都装了则保证依赖先加载 |
| `名字>=版本` | 版本要求（`>=` `>` `<=` `<` `==` `!=`，按数字段比较：`1.10 > 1.9`）。版本取对方声明的 `version`，可用 `provides_version` 覆盖 |
| `self.deps` | 依赖访问代理：`get(name)` / `has(name)` / `enabled(name)` / `require(name)` / `names()`；也支持 `name in self.deps` |
| `self.dependency_check()` | 现场自查（`DependencyCheck`：`ok` / `reason()` / `missing` / `disabled` / `version_mismatch`）|

框架侧保证的四件事（都有测试：(../../tests/test_plugin_deps.py)）：

1. **加载顺序 = 拓扑序**：`load_all()` / `enable_all()` 都按「依赖在前」执行，
   可选依赖也参与排序（有就先生效）；`shutdown()` 反过来（使用者先走）。
   同层按字典序，顺序稳定可测。**必需依赖成环会在加载期报错**，
   可选依赖成环则自动摘掉一条可选边（软约束不该让整批插件起不来）。
2. **缺依赖 = 不加载，而不是半死不活**：`on_load` 都不会跑（构造期也不构造，
   所以 `config_template()` 不会写盘）。依赖装上/启用后**会自动加载**
   （watcher 2 秒内重试，`/plugin` 装完依赖后立刻重试）。
3. **不能拆别人的台**：还有已启用插件依赖它时，`unload()` / `disable()` 默认
   拒绝（返回 False + 日志）；`force=True` 才按「使用者在前」级联。运维侧
   `/plugin uninstall` 会提示先卸载使用者。
4. **重载会级联**：重载 `store` 之后，持有旧实例的 `report` 会跟着重载
   （否则它 `self.deps.get("store")` 拿到的是已经解绑的死对象）。

几条约定（写错会在加载期被明确拒绝，不会拖到运行期）：

- 依赖名必须是合法插件名（字母/下划线开头，只含字母数字下划线）；
- **不能依赖自己**，也不能同一个插件**同时**写在 `requires` 和
  `optional_requires` 里（语义矛盾）；
- 同名依赖写两遍时**带版本要求的那条赢**（与书写顺序无关）。

---

## 1.4 从哪导入（包结构分类）

`napcat` 顶层**不再一次性导出所有名字**：每个符号从「拥有它」的子模块导入，
导入路径本身就是分类。

| 要用的东西 | 导入路径 |
|------|------|
| 客户端 / 群绑定 | `from napcat.client import NapCatClient, GroupBinding` |
| 插件基类 / 插件管理器 | `from napcat.plugin import PluginBase, PluginManager` |
| 事件与数据模型（`MessageEvent`、`Sender`、枚举、`parse_event`…） | `from napcat.models import MessageEvent, Sender` |
| 事件总线 / 定时任务 | `from napcat.events import EventBus, TimerManager` |
| 消息构造 | `from napcat.message import MessageChain` |
| 命令 / Schema / 参数字段 | `from napcat.command import CommandSchema, StrField, CommandManager` |
| 声明式注册（`@command` / `@event` / `@every`） | `from napcat.registry import command, event, every` |
| 权限 | `from napcat.permission import PermissionManager` |
| 错误类型 / 统一反馈 | `from napcat.errors import PluginError, ErrorReporter` |
| sqlite ORM | `from napcat.store import Model, CharField, Database` |
| 配置 / 文件 | `from napcat.config import ConfigManager` / `from napcat.file_manager import FileManager` |
| 服务端拉起 / 看门狗 | `from napcat.launcher import ServerLauncher` / `from napcat.watchdog import Watchdog` |

- 旧写法 `from napcat import X` 仍然可用（框架发 `DeprecationWarning`，并告诉你
  正确路径），**新代码请用上表**；
- 好处很直接：`import napcat` 不再连锁加载 client / aiohttp / websockets 这些重依赖，
  只用一个 `Model` 不会把整个客户端拉起来；
- 框架自己的模块之间一律相对导入（`from .models import …`），不依赖顶层 re-export。

---
