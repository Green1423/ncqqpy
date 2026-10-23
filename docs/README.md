# ncqqpy 文档

本目录是 ncqqpy 的**全部说明文档**，按职责分成六个区：用框架、写插件、内置插件手册、
参考、归档、贡献规范。每篇文档开头都有「上级 / 上一章 / 下一章」导航。

```
docs/
├── README.md              # 本页：总索引与阅读路径
├── guide/                 # 用框架：配置 → 消息 → 事件 → API → 命令 → 权限 …
├── plugin-dev/            # 写插件：生命周期 → 资源 → 命令/Schema → 存储 → 错误 …
├── plugins/               # 内置插件手册：ai_chat / bupt_news / byrdocs
├── reference/             # 参考：sqlite ORM、看门狗、DSH 内部机制
├── archive/               # 历史设计/实施计划（只读，不再维护）
└── contributing/          # 贡献规范：docstring / 测试与发布
```

## 阅读路径

| 你想… | 从这里开始 |
|------|-----------|
| 把机器人跑起来 | [快速开始](guide/01-quickstart.md) → [客户端配置](guide/02-client.md) |
| 发消息 / 收事件 | [消息构造](guide/03-messages.md) → [事件系统](guide/04-events.md) |
| 写命令、帮助、权限 | [命令系统](guide/06-commands.md) → [权限管理](guide/07-permissions.md) |
| 写插件 | [插件开发](plugin-dev/README.md)（先看 [插件概述](plugin-dev/01-overview.md)） |
| 存结构化数据 | [数据持久化](plugin-dev/09-store.md) + [store 参考](reference/store.md) |
| 用内置插件 | [ai_chat](plugins/ai-chat/README.md) · [bupt_news](plugins/bupt-news/README.md) · [byrdocs](plugins/byrdocs/README.md) |
| 运维（断线重启） | [看门狗](reference/watchdog/README.md) |
| 改框架本身 | [docstring 规范](contributing/docstrings.md) + [DSH 内部机制](reference/dsh-internals/README.md) |

## 分区索引

- 使用框架：[guide/README.md](guide/README.md)
- 写插件：[plugin-dev/README.md](plugin-dev/README.md)
- 内置插件：[plugins/README.md](plugins/README.md)
- 参考：[reference/README.md](reference/README.md)
- 归档：archive/README.md
- 贡献规范：[contributing/README.md](contributing/README.md)

## 一、使用框架（`guide/`）

| 章节 | 小节 |
|------|------|
| [快速开始](guide/01-quickstart.md) | 1.1 安装 · 1.2 前提条件 · 1.3 最小运行示例 |
| [客户端配置](guide/02-client.md) | 2.1 构造参数 · 2.2 属性速览 · 2.3 启动与退出 · 2.4 断线重连 |
| [消息构造](guide/03-messages.md) | 3.1 文本与 @ · 3.2 多媒体 · 3.3 音乐 · 3.4 互动 · 3.5 富文本 · 3.6 合并转发 · 3.7 输出格式 |
| [事件系统](guide/04-events.md) | 4.1 事件类型 · 4.2 注册处理器 · 4.3 MessageEvent 属性 · 4.4 中间件 |
| [API 调用](guide/05-api.md) | 5.1 消息发送 · 5.2 群组管理 · 5.3 好友与用户 · 5.4 文件 · 5.5 系统与扩展 · 5.6 错误处理 |
| [命令系统](guide/06-commands.md) | 6.1 基本用法 · 6.2 解析规则 · 6.3 CommandPacket · 6.4 返回值 · 6.5 子命令 · 6.6 非法输入 · 6.7 手动解析 · 6.8 命令别名 |
| [权限管理](guide/07-permissions.md) | 7.1 角色层级 · 7.2 命令权限 · 7.3 手动检查 · 7.4 自定义节点 · 7.5 `/perm` · 7.6 `/userperm` |
| [插件开发（基础）](guide/08-plugin-dev.md) | 8.1 插件基类 · 8.2 事件处理 · 8.3 定时任务 · 8.4 配置读取 |
| [文件管理](guide/09-files.md) | 9.1 FileManager · 9.2 图片/文件上传 |
| [定时事件](guide/10-timers.md) | 10.1 定时器 · 10.2 事件中间件 |
| [高级用法](guide/11-advanced.md) | 11.1 自定义 API · 11.2 自定义事件 · 11.3 多客户端 |
| [API 参考速查](guide/12-api-reference.md) | 全部 API 方法速查表 |

## 二、写插件（`plugin-dev/`）

| 章节 | 小节 |
|------|------|
| [插件概述](plugin-dev/01-overview.md) | 1.1 最小插件 · 1.2 生命周期 · 1.3 声明依赖 · 1.4 导入分类 |
| [插件可用资源](plugin-dev/02-resources.md) | 2.1 快捷方法 · 2.2 读取连接状态 |
| [消息事件处理](plugin-dev/03-events.md) | 3.1 动态卸载 · 3.2 基础消息处理 · 3.3 指定事件类型 |
| [命令注册](plugin-dev/04-commands.md) | 4.1 基础命令 · 4.2 Schema · 4.3 参数 · 4.3.1 占位符 · 4.3.2 函数签名 … |
| [全部事件类型](plugin-dev/05-event-types.md) | 5.1 MessageEvent 常用属性 |
| [全部 API 方法](plugin-dev/06-api-methods.md) | 6.1 消息 · 6.2 好友/用户 · 6.3 群组 · 6.4 文件 … |
| [权限管理](plugin-dev/07-permissions.md) | 7.1 角色层级 · 7.2 命令绑定 · 7.3 手动检查 · 7.4 自定义节点 · 7.4.1 用户级覆盖 · 7.5 `/perm` |
| [消息构造](plugin-dev/08-messages.md) | 8.1 MessageChain 速查 |
| [数据持久化](plugin-dev/09-store.md) | 9.1 定义模型 · 9.2 增删改查 · 9.3 事务 |
| [定时任务](plugin-dev/10-timers.md) | 10.1 排程语义 · 10.2 取消/恢复 |
| [文件管理](plugin-dev/11-files.md) | 11.1 FileManager |
| [完整插件示例](plugin-dev/12-example.md) | 12.1 一个可运行插件 |
| [热重载](plugin-dev/13-hot-reload.md) | 13.1 自动检测 · 13.2 手动重载 · 13.3 失败回滚 |
| [插件包结构](plugin-dev/14-layout.md) | 14.1 `__init__.py` 只装配 · 14.2 逻辑拆分 · 14.3 声明式注册 |
| [配置管理](plugin-dev/15-config.md) | 15.1 首次生成 · 15.2 读写 · 15.3 契约测试 |
| [错误处理](plugin-dev/16-errors.md) | 16.1 错误类型 · 16.2 速查 · 16.3 用户反馈 |

## 三、内置插件（`plugins/`）

| 插件 | 一句话 | 文档 |
|------|--------|------|
| `ai_chat` | 多会话 AI 对话、消息包装、上下文压缩、内置工具 | [手册](plugins/ai-chat/README.md) |
| `bupt_news` | 北邮校内通知同步、免打扰早报、短 ID 检索、DeepSeek 摘要 | [手册](plugins/bupt-news/README.md) |
| `byrdocs` | BYR Docs 资料检索与下载（搜索编号 / 翻页 / 取文件 / 白名单） | [手册](plugins/byrdocs/README.md) |
| `plugin_manager` | 超管私聊装卸插件（GitHub 安装、回收站、校验） | [README](../plugins/plugin_manager/README.md) |
| `essential` | 群管理（`/ban` `/kick` `/groupinfo`）+ 问候 + 定时撤回 | 示例章节见 [插件开发](plugin-dev/README.md) |

## 四、参考（`reference/`）

| 文档 | 内容 |
|------|------|
| [sqlite ORM](reference/store.md) | `napcat.store`：模型、字段、查询、事务、迁移边界 |
| [看门狗](reference/watchdog/README.md) | 断线自动重启 + 扫码恢复的唯一状态机、配置与排障 |
| [DSH 内部机制](reference/dsh-internals/README.md) | DSH 的消息投影、会话日志、系统提示与上下文压缩（`ai_chat` 的语义依据） |

## 五、归档（`archive/`）

历史设计与实施计划，只读、不再维护：索引。

## 六、贡献规范（`contributing/`）

| 文档 | 内容 |
|------|------|
| [docstring 规范](contributing/docstrings.md) | Google 风格、`Args`/`Returns`/`Raises`、编辑器可解析 |
| [测试与发布](contributing/testing.md) | 测试套件、发布脚本、私有信息自查 |

## 目录速览

```
ncqqpy/
├── main.py            # 启动入口（从 config.json 读配置）
├── run.sh             # 启动脚本（venv-arm + .env）
├── config.json        # 全部运行配置
├── napcat/            # 框架本体
├── plugins/           # 插件目录
├── tests/             # 测试与诊断（见 tests/README.md）
└── docs/              # 本目录
```
