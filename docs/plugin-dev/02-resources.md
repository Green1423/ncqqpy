# 二、插件可用资源

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[插件概述](01-overview.md) · 下一章：[消息事件处理](03-events.md)


`PluginBase` 实例在 `__init__` 时注入以下资源：

| 属性 | 类型 | 说明 |
|------|------|------|
| `self.api` | `ApiClient` | API 调用客户端（100+ 方法） |
| `self.event_bus` | `EventBus` | 事件总线 |
| `self.timer` | `TimerManager` | 定时任务管理器 |
| `self.cmds` | `CommandManager` | 命令管理器 |
| `self.perm` | `PermissionManager` | 权限管理器 |
| `self.files` | `FileManager` | 文件管理器 |
| `self.logger` | `Logger` | Python 标准 Logger |
| `self.errors` | `ErrorReporter` | **错误反馈器**：`render()` / `report()` / `log()`，见 (16-errors.md#十六错误处理与用户反馈) |
| `self.deps` | `_DepsProxy` | **依赖访问**：`get()` / `has()` / `enabled()` / `require()`，见 (01-overview.md#13-声明依赖别的插件必需--可选) |
| `self._groups` | `GroupBinding` | 群组绑定管理器 |
| `self.client` | `NapCatClient` | 主客户端（**只读**：连接状态、`launcher` 结果；注册回调请用 `self.on_ws_*`，见 (#22-读取连接状态--拉起结果selfclient)） |
| `self.manager` | `PluginManager` | 管着本插件的插件管理器（`get()` / `load()` / `reload()`）；手搓宿主时退回 `client.plugins` |

## 2.1 快捷方法

| 方法 | 说明 |
|------|------|
| `self.on_message(group_id=..., private=...)` | 注册消息事件处理器 |
| `self.on_event(EventType)` | 注册指定类型事件处理器 |
| `self.off_event(EventType, handler)` | 取消注册指定事件处理器 |
| `self.off_all_events()` | 取消注册本插件全部事件 |
| `self.register_command(name, **kwargs)` | 注册命令 |
| `self.unregister_command(name)` | 取消注册命令 |
| `self.schedule(seconds, name=...)` | 注册周期定时任务 |
| `self.schedule_at("HH:MM", name=...)` | 注册每日定时任务 |
| `self.cancel_schedule(name)` | 取消指定名称的定时任务 |
| `self.bind_group(gid)` | 绑定群 |
| `self.unbind_group(gid)` | 解绑群 |
| `self.is_group_bound(gid)` | 检查群是否已绑定 |
| `self.bound_groups` | 已绑定群列表（property） |
| `self.stop_event(hid)` | 移除指定事件监听器（按注册时的 `hid`）|
| `self.dependency_check()` | 现场自查依赖是否满足（见 (01-overview.md#13-声明依赖别的插件必需--可选)）|
| `self.pause_event(hid)` | 暂停指定事件监听器 |
| `self.resume_event(hid)` | 恢复指定事件监听器 |
| `self.stop_command(name)` | 删除指定命令（连它的别名一起，与 `/stop command` 同一条路径） |
| `self.pause_command(name)` | 启用/禁用命令（toggle，禁用后返回"命令不存在"） |
| `self.stop_timer(name)` | 取消指定定时任务（返回它原本是否存在） |
| `self.pause_timer(name)` | 暂停指定定时任务 |
| `self.resume_timer(name)` | 恢复指定定时任务 |

> **框架自动回收的范围**（不用自己写清理代码）：
> `unload()` 时会撤销本插件注册的**全部**事件处理器、**定时任务**与**命令**
> （按注册时打的归属标签，不看命名约定）。
> `on_disable()` 只撤销事件处理器 —— 停用但没卸载时，定时任务与命令仍归插件自己管
> （(03-events.md#31-动态卸载事件命令定时器) 的写法仍然有效，重复回收是空操作）。
> 加载失败或 `on_enable` 抛异常时，这一次已经注册的东西会被框架回滚干净。

## 2.2 读取连接状态 / 拉起结果（`self.client`）

机器人掉线时往往**不能靠 QQ 自己报告**，需要插件观察 WebSocket 状态。

> **生命周期与连接是解耦的**：插件在**任何连接之前**就完成 `load` / `enable`；
> 之后断线、重连、服务端重启都**不会**触发插件重载（不 `on_load` / `on_enable` /
> `on_disable` / `on_unload`，实例也不换）。所以：
>
> - `on_load` / `on_enable` 里读 `ws_connected` **必然是 False**，别用它做分支；
> - 也不要在 `on_load` / `on_enable` 里调 API（那时还没连上）；
> - 「连上之后要做什么」放连接回调里 —— 这是唯一的恢复入口。

三个注册接口都挂在 `PluginBase` 上（回调都是 `async`）：

```python
class MyPlugin(PluginBase):
    async def on_enable(self):
        @self.on_ws_connect_failed
        async def on_failed(attempt: int, exc: BaseException):
            """连接建立不起来（服务器没上线）。每轮连续失败只触发第 1 次。"""
            self.logger.warning(f"连不上（第 {attempt} 次）: {exc!r}")

        @self.on_ws_connected
        async def on_connected():
            """连接就绪（首次连上 + 每次重连）。此时 self.client.self_id 已刷新。"""

        @self.on_ws_disconnected
        async def on_disconnected():
            """连上之后又断开。启动时连不上走 on_ws_connect_failed。"""
```

这几个回调**归插件所有**：`on_disable()` / 卸载时框架自动注销；重新启用时在
`on_enable` 里重新注册即可。直接调 `self.client.on_ws_*` 也有效，但**不受生命周期
管理**（框架内部看门狗才那么用）—— 热重载后会留下旧实例的回调，别这么写。

| 属性 / 方法 | 说明 |
|------|------|
| `self.client.ws_connected` | 当前 WebSocket 是否可用 |
| `self.client.launcher` | `ServerLauncher`，`status()` 返回拉起次数/模式/最近结果 |
| `self.client.launcher.launch(reason)` | 手动拉起服务端（配置见 (../../config.json) 顶层的 `restart_*`） |

> 框架自身的拉起逻辑：连不上时按 `restart_command` 拉起服务端，**每轮连续失败只拉一次**，
> 并在后台 task 里执行（不阻塞重连循环）。该逻辑现在是框架内置的
> （(../../napcat/watchdog.py)，配置见 (../../config.json) 的
> `watchdog*`；完整说明见 (../reference/watchdog/README.md)）——
> 历史上的 `plugins/ncqq_watchdog` 插件已退役，只剩 (../../plugins/ncqq_watchdog/data) 目录。

---
