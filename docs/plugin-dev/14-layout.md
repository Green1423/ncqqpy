# 十四、插件包结构：注册与逻辑分离

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[热重载](13-hot-reload.md) · 下一章：[配置管理](15-config.md)


一条原则：**注册处只写「装哪些」，不写「怎么干」**。`on_enable` 里不定义处理器、
不写业务分支 —— 它只做 `self.register_module(...)`；每条命令 / 事件 / 定时任务的
逻辑按**类别**放独立模块（`commands/`、`events/`、`jobs/`）。

## 14.1 目录

```text
plugins/demo/
├── __init__.py            # docstring + `from .plugin import DemoPlugin` + `PLUGIN`
├── plugin.py              # 只有插件类 + 生命周期 + register_module(...)
├── schemas.py             # 命令参数（CommandSchema）
├── store.py               # 数据模型（napcat.store.Model）
├── commands/
│   ├── __init__.py
│   ├── chat.py            # 对话类命令的逻辑
│   └── admin.py           # 管理类命令的逻辑
├── events/
│   ├── __init__.py
│   └── message.py         # 事件逻辑
├── jobs/
│   ├── __init__.py
│   └── cleanup.py         # 定时任务逻辑
└── data/                  # 运行时数据（config / sqlite / 缓存；不进仓库）
```

## 14.2 注册：`plugin.py`（没有逻辑）

```python
from napcat.plugin import PluginBase
from .commands import admin, chat
from .events import message
from .jobs import cleanup
from .store import Note


class DemoPlugin(PluginBase):
    name = "demo"
    version = "1.0.0"

    async def on_load(self):
        self.database(ensure=[Note])          # 建表 + 绑定，库在 data/demo.db

    async def on_enable(self):
        # 只有装配：命令 / 事件 / 定时任务各归各的模块
        self.register_module(chat, admin, message, cleanup)
```

`register_module()` 扫描模块里的 `@command` / `@event` / `@every` / `@daily` /
`@delay` 声明，按**定义顺序**注册。卸载时框架照旧回收（命令、事件、定时器都走
原有通道），不用写清理代码。

## 14.3 逻辑：`commands/chat.py`

```python
from napcat.registry import command
from ..schemas import SaySchema
from ..store import Note


@command("demo.say", schema=SaySchema, permission="", scope="all",
         description="记一句话")
async def say(plugin, params, event):
    Note.objects.create(title=params.text)
    return f"现在共 {Note.objects.count()} 条"
```

第一个参数**固定是插件实例**（`plugin`）—— 逻辑模块不需要 import 插件类，也不会
和 `plugin.py` 互相 import 成环；`plugin.cfg` / `plugin.database()` / `plugin.api`
都从它拿。

## 14.4 逻辑：`events/` 与 `jobs/`

```python
# events/message.py
from napcat.models import MessageEvent
from napcat.registry import event


@event(MessageEvent, description="记一条消息")
async def on_message(plugin, event):
    if not plugin.cfg["enabled"]:
        return
    ...
```

```python
# jobs/cleanup.py
from napcat.registry import daily, delay, every


@every(3600, name="demo.cleanup")
async def cleanup(plugin):
    ...          # 每小时


@daily("08:00")
async def morning(plugin):
    ...          # 每天 08:00（本机时区，月末/闰年框架处理）


@delay(5)
async def warmup(plugin):
    ...          # 启动后 5 秒跑一次
```

## 14.5 处理器签名（框架注入什么）

| 声明 | 签名 | 拿到 |
|------|------|------|
| `@command(..., schema=...)` | `async def h(plugin, params, event)` | 校验后的 `params`、事件 |
| `@command(...)`（无 schema） | `async def h(plugin, packet, event)` | 原始 `CommandPacket`、事件 |
| `@event(EventType)` | `async def h(plugin, event)` | 事件对象 |
| `@every` / `@daily` / `@delay` | `async def h(plugin)` | 无 |

- 处理器**可以是同步函数**（返回值照常回给用户）；事件/定时里建议 `async`；
- 用不到的参数可以不写（`async def h(plugin)` 也行），框架按参数名注入；
- 事件 `hid` 默认 `插件名.函数名`，用 `@event(..., hid="...")` 可覆盖；
- 同一模块里多个声明按**定义顺序**注册；`commands/__init__.py` 里的再导出不会
  重复注册（`collect()` 按对象去重）。

## 14.6 几条硬性要求

1. **`__init__.py` 只写入口**（3 行）：

   ```python
   """demo —— 一句话说明。"""
   from .plugin import DemoPlugin

   PLUGIN = DemoPlugin
   ```

   为什么必须写 `PLUGIN`：框架先看它；没写就退回旧行为 —— 按模块属性**字母序**
   扫第一个 `PluginBase` 子类。拆分后 `__init__.py` 里常常顺手
   `from .base import GenericPlugin`，于是**基类**被实例化（插件"加载成功"但跑的
   不是你的子类）。导出多个子类又没写 `PLUGIN` 时框架会告警；`PLUGIN` 写错则告警
   后回退扫描，不会让插件消失。

2. **包内一律相对导入**：`from .commands import chat` / `from ..store import Note`。
   框架加载插件时模块名是 `napcat_plugin_<名字>`，不是顶层包 —— `import main`
   会 `ImportError`。

3. **`__init__.py` 会被重新执行**：热重载清整棵模块树，所以状态别放模块全局
   （会被重置）。状态放 `self.xxx`，数据放 `self.database()` 或 `config`。

4. **`data/` 是运行时的**：框架/发布脚本跳过它；需要版本化的资源放 `assets/`。

5. **单文件插件仍支持**（`plugins/foo.py`），但它没有自己的目录
   （`self.plugin_dir` 是 `plugins/`），`self.database()` 的默认库落在
   `plugins/data/foo.db` —— 逻辑一多就该拆成包。

## 14.7 什么时候别拆

- 一百多行、只有两三条命令：**单文件更省事**，拆了只是多几个 import；
- 逻辑要共享给多个插件：做成**独立包**（放 `napcat` 里或插件目录外），别塞进
  某个插件的 `__init__.py` —— 那会变成隐式依赖；
- 注册和逻辑分离的成本是「多一个装饰器」，收益是「找逻辑不用翻注册代码」——
  命令超过几十条、或一个插件里有事件+定时任务时，收益明显。

## 14.8 把现有插件迁过去（3 步）

1. 把 `on_enable` 里的闭包逐个搬进 `commands/xxx.py`，函数体不动，签名改成
   `(plugin, params, event)`，`self.` 改成 `plugin.`；
2. 在函数上加 `@command(...)` / `@event(...)` / `@every(...)`，参数从原来的
   `register_command` 调用里照抄；
3. `on_enable` 换成 `self.register_module(...)`，`__init__.py` 补 `PLUGIN`。

迁移后跑一遍命令、事件、定时任务与热重载（框架会清模块树）。存量插件里
`ai_chat` 是「大包 + 按职责分模块」的例子（`__init__.py` 只有三行）。

---
