# 插件开发

> 上级：[文档索引](../README.md)

> 版本 1.4.0

## 章节

| 章节 | 小节 |
|------|------|
| [插件概述](01-overview.md) | 1.1 最小插件 · 1.2 插件生命周期 · 1.3 声明依赖别的插件（必需 / 可选） · 1.4 从哪导入（包结构分类） |
| [插件可用资源](02-resources.md) | 2.1 快捷方法 · 2.2 读取连接状态 / 拉起结果（`self.client`） |
| [消息事件处理](03-events.md) | 3.1 动态卸载（事件/命令/定时器） · 3.2 基础消息处理 · 3.3 指定事件类型 |
| [命令注册](04-commands.md) | 4.1 基础命令 · 4.2 带 Schema 验证的命令 · 4.3 `register_command` 参数 · 4.3.1 占位符记号（全框架统一） … |
| [全部事件类型](05-event-types.md) | 5.1 MessageEvent 常用属性 |
| [全部 API 方法](06-api-methods.md) | 6.1 消息 · 6.2 好友/用户 · 6.3 群组 · 6.4 文件 … |
| [权限管理](07-permissions.md) | 7.1 角色层级 · 7.2 命令权限绑定 · 7.3 手动权限检查 · 7.4 自定义权限节点 … |
| [消息构造](08-messages.md) | — |
| [数据持久化（`napcat.store`）](09-store.md) | — |
| [定时任务](10-timers.md) | — |
| [文件管理](11-files.md) | — |
| [完整插件示例](12-example.md) | — |
| [热重载](13-hot-reload.md) | 13.1 从 GitHub 装卸插件（`plugin_manager`） |
| [插件包结构：注册与逻辑分离](14-layout.md) | 14.1 目录 · 14.2 注册：`plugin.py`（没有逻辑） · 14.3 逻辑：`commands/chat.py` · 14.4 逻辑：`events/` 与 `jobs/` … |
| [配置管理](15-config.md) | 15.1 config.json · 15.2 bind / unbind 自动持久化 · 15.3 插件配置 · 15.4 ConfigManager API |
| [错误处理与用户反馈](16-errors.md) | 16.1 三种写法，按你掌握的信息量选 · 16.2 错误类型速查（`from napcat.errors import ...`） · 16.3 框架怎么反馈 · 16.4 配置（`config.json` 顶层） … |
