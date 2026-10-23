# 八、插件开发

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[权限管理](07-permissions.md) · 下一章：[文件管理](09-files.md)


## 8.1 插件基类

```python
from napcat.plugin import PluginBase
from napcat.models import MessageEvent
from napcat.message import MessageChain
class MyPlugin(PluginBase):
    name = "my_plugin"
    version = "1.0.0"
    description = "我的插件"
    author = "Me"

    async def on_load(self):
        """插件被加载（初始化，不注册处理器）"""
        self.logger.info(f"{self.name} 已加载")

    async def on_enable(self):
        """插件被启用（注册事件/命令/定时器）"""
        @self.on_message()
        async def handle(event: MessageEvent):
            if "关键词" in event.plain_text:
                await self.api.send_group_msg(
                    group_id=str(event.group_id),
                    message=MessageChain().text("检测到关键词"),
                )

        @self.register_command("hello")
        async def hello_cmd(packet, event):
            return f"你好 {event.sender.nickname}!"

        @self.schedule(300, name="5min_check")  # 每5分钟
        async def periodic_task():
            self.logger.info("定时任务执行")

    async def on_disable(self):
        """插件被禁用（清理处理器）"""
        # 注意：通过 self.on_message / self.on_event 注册的处理器
        # 会在 PluginManager.disable() 时自动取消

    async def on_unload(self):
        """插件被卸载（释放资源）"""
        pass
```

## 8.2 插件内置便捷方法

| 方法 | 说明 |
|------|------|
| `self.on_message(group_id=..., private=...)` | 注册消息事件 |
| `self.on_event(EventType)` | 注册指定类型事件 |
| `self.register_command(name, ...)` | 注册命令 |
| `self.schedule(seconds, name=...)` | 注册周期定时任务 |
| `self.schedule_at("HH:MM", name=...)` | 注册每日定时任务 |
| `self.api` | ApiClient |
| `self.event_bus` | EventBus |
| `self.timer` | TimerManager |
| `self.cmds` | CommandManager |
| `self.perm` | PermissionManager |
| `self.files` | FileManager |
| `self.logger` | Logger |

## 8.3 插件文件约定

1. 插件文件放在 `./plugins/` 目录下（或客户端初始化时指定的 `plugin_dir`）
2. 文件名 = 插件名，如 `my_plugin.py`
3. 文件中必须有一个（或多个）继承 `PluginBase` 的公开类
4. 以 `_` 开头的文件会被自动忽略（`_disabled.py`）

## 8.4 插件管理 API

```python
# 加载/启用/禁用/卸载
await client.plugins.load("my_plugin")
await client.plugins.enable("my_plugin")
await client.plugins.disable("my_plugin")
await client.plugins.unload("my_plugin")

# 全部操作
await client.plugins.load_all()
await client.plugins.enable_all()

# 获取插件
plugin = client.plugins.get("my_plugin")
print(plugin.version, plugin.enabled)

# 列出所有
for name, p in client.plugins.plugins.items():
    print(name, p.version, p.enabled)
```

## 8.5 运行时热重载

```python
# 手动热重载
await client.plugins.reload("my_plugin")

# 全部热重载
await client.plugins.reload_all()
```

**自动文件监控**：客户端启动时自动调用 `client.plugins.watch(interval=2.0)`，每 2 秒检测插件文件 mtime，发现变更自动热重载。开发时只需保存文件即可生效。

```python
# 可在开发完成后关闭文件监控
client.plugins.stop_watch()
```

## 8.6 完整生命周期

```
PluginManager.load()
  └→ __init__()
  └→ on_load()

PluginManager.enable()
  └→ on_enable()       # 在这里注册事件/命令/定时器

... 运行中 ...

PluginManager.disable()
  └→ on_disable()      # 自动取消注册本次启用注册的 _handlers
                       # （定时任务与命令仍由插件自己清理，见下）

PluginManager.unload()
  └→ on_unload()       # 释放资源；框架顺带回收本插件的
                       # 事件处理器 + 定时任务 + 命令，并从 sys.modules
                       # 摘掉整棵模块子树（含 .main / .store 等子模块）
```

> **失败不会静默**：加载/启用/卸载/重载失败会记完整日志并私聊超管；
> 运行期（命令、事件、定时任务）失败会记日志并回给触发者 ——
> 群里会 **@ 出错时提问的那个人**，文案里带错误编号（`E-xxxx`），
> 同一编号 60 秒内重复出现只回一句短提示（不静默、也不刷屏）。
> 策略可在 `config.json` 顶层调：`error_report.enabled / throttle_seconds /
> show_reason / notify_superusers`。插件能抛的错误类型见
> (../plugin-dev/16-errors.md#十六错误处理与用户反馈)。

## 8.6.1 首次运行即初始化全部配置项

插件被**构造**时（`on_load()` 之前）框架会按它的 `config_template()` 把**全部**
配置项补进 `config.json` 的 `plugins.<插件名>` 段 —— 任何构造路径（框架加载、
测试直接 new、脚本手动构造）都一样：

- **只补缺键**：运维改过的值一个都不动；插件升级新增的配置项会自动补进老部署；
- **嵌套段逐键补**（`weather` / `images` / `binding` 这类段里缺的子键也补）；
- **幂等**：没有缺键就完全不写盘，有缺键也只写一次。

所以「这个插件有哪些开关可改」永远能在 `config.json` 里看到，不用翻代码。
插件侧写法见 (../plugin-dev/15-config.md#1531-首次运行即初始化全部配置项config_template)。

## 8.7 完整插件示例

```python
# plugins/welcome.py
from napcat.plugin import PluginBase
from napcat.models import MessageEvent, GroupIncreaseEvent
from napcat.message import MessageChain
class WelcomePlugin(PluginBase):
    name = "welcome"
    version = "1.0.0"
    description = "入群欢迎 + 退群通知"

    def config_template(self) -> dict:
        """完整默认配置：框架在加载时写进 config.json → plugins.welcome。

        每个插件都要实现它 —— 「首次运行即把所有配置项写进文件」是框架契约，
        运维打开 config.json 就能看到有哪些开关可改。
        """
        return {
            "enabled": True,
            "greeting": "欢迎加入！请注意群规～",
        }

    async def on_enable(self):
        @self.on_event(GroupIncreaseEvent)
        async def welcome(event: GroupIncreaseEvent):
            await self.api.send_group_msg(
                group_id=str(event.group_id),
                message=MessageChain()
                    .at(event.user_id)
                    .text(" " + (self.config.get("greeting") or "欢迎！")),
            )

        @self.register_command("welcome_toggle", permission="admin")
        async def toggle(packet):
            # ⚠️ PluginBase **没有** `self.disable()`（旧文档这里写的
            # `await self.disable()` 会直接 AttributeError）。命令作用域内的
            # 「关掉这个功能」要自己实现：撤销本命令注册的处理器 + 记一个开关位。
            self.off_all_events()
            self.unregister_command("welcome_toggle")
            return "欢迎功能已关闭"
```

> 想让**超管停用整个插件**，运维路径是 `/reload <插件名>`（会重新走
> `on_disable → on_load → on_enable`），或者把插件目录改名成 `_name`
> 让框架不再加载它；插件内部没有「自禁用」接口。

## 8.8 用命令装卸插件（`plugin_manager`）

框架自带 (../../plugins/plugin_manager)，超管可以在**私聊**里把插件从 GitHub
装进来、更新、卸载 —— 不用登服务器改文件。列表文件在
`Green1423/ncqqpy-plugins` 的 `plugins.json`：

```
/plugin list                    看有哪些插件、装没装
/plugin info ai_chat            看单个插件的来源与状态
/plugin check                   检查全部已装插件的上游更新
/plugin install ai_chat         安装
/plugin update all              全部更新
/plugin uninstall ai_chat       卸载（进回收站 plugins/.trash/，可找回）
/plugin add <仓库> [name]        装列表文件里没有的仓库
```

> 🔒 **仅私聊 · 仅超管**：这些子命令都声明 `permission="superuser"` +
> `scope="private"`（装插件等于在服务器上执行代码）。非超管在私聊里、
> 超管在群里，看到的都是「命令不存在」。

几条保险设计（细节见 (../../plugins/plugin_manager/README.md)）：

- **更新不覆盖你手改过的文件**：冲突时停下来列出文件名，`--force=true` 才覆盖，
  旧版本进回收站；
- **`data/` 与你自己新增的文件永不删除**：更新时跟着走，卸载时留在原地
  （`--purge=true` 才连 `data/` 一起进回收站）；
- **下载的源码包会做安全检查**：拒绝路径穿越、绝对路径、符号链接，
  且校验包里的插件名与要装的名字一致；
- **不装卸自己**：`plugin_manager` 拒绝在运行中动自己，避免把管理器删掉。

**插件之间的依赖**：插件可以声明必需依赖（`requires`）与可选依赖
（`optional_requires`）—— 装一个带依赖的插件时，装完的回执会直接告诉你
「还缺谁、怎么补」，缺的装上后它会自动加载；被别人依赖着的插件**不能直接
卸载/禁用**（会被拒绝，或按提示先处理使用者）。`/plugins` 按依赖顺序列出
插件，被依赖挡住的显示 `[⛔] 名字 —— 缺少 X`。插件侧写法见
(../plugin-dev/01-overview.md#13-声明依赖别的插件必需--可选)。

配置（`config.json → plugins.plugin_manager`）：`manifest_url`（换列表来源）、
`token`（GitHub token，留空 = 匿名，每小时 60 次 API）、`timeout`、`proxy`、
`manifest_cache_seconds`。身份建议用环境变量 `NCQQPY_GITHUB_TOKEN` /
`GITHUB_TOKEN` 给，避免明文写进 `config.json`。

---
