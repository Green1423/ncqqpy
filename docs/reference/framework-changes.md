# 框架变更记录（插件系统 / 本地状态 / 服务范围）

> 上级：[参考文档](README.md) · 插件作者请从 [插件开发](../plugin-dev/README.md) 开始

本页记录**对外可见**的框架变更：新增了哪些 API、哪些行为变了、升级要做什么。
只写结论与迁移步骤；用法看对应章节，设计动机看代码里的注释。

## 1. 新增模块

| 模块 | 内容 | 用法 |
|------|------|------|
| `napcat.subscribe` | `Subscription`（群/用户订阅判定）、`parse_ids`（名单解析） | [服务范围](../plugin-dev/20-subscribe.md) |
| `napcat.persist` | `read_json` / `write_json`（原子 JSON）、`prune_keep` / `prune_unreferenced`（目录清理） | [本地状态与清理](../plugin-dev/21-persist.md) |

两个模块都已登记进 `napcat._SUBMODULES`：`import napcat` 之后可用
`napcat.subscribe.Subscription` / `napcat.persist.write_json` 按需加载。

## 2. 新增与变更的公开 API

| API | 变更 | 说明 |
|------|------|------|
| `PluginBase.manager` | 新增（property） | 「管理本插件的 `PluginManager`」的唯一解析：注入的 `_manager` 优先，`client.plugins` 兜底。插件里读 `self.manager` 即可，不要再自己拼 |
| `PluginManager.plugin_class(name)` | 新增 | 插件入口类：已加载的返回实例类，未加载的读一次磁盘并按源码 mtime 缓存。`/plugin list` 这类诊断入口用它，不用每次重新导入模块 |
| `discover_plugins(plugin_dir)` | 新增（模块函数） | 「目录里有哪些可加载插件」的唯一判定：`<名>/__init__.py` 或 `<名>.py`，跳过 `_` / `.` 开头的条目 |
| `plugin_module_name(name)` | 新增（模块函数） | `sys.modules` 里的模块名（`napcat_plugin_<名>`），加载/清理/热重载共用 |
| `CommandManager.set_disabled(name, disabled=None)` | 新增 | 命令启停的唯一入口；`disabled=None` 表示取反。`/pause command` 与 `PluginBase.pause_command` 都走它 |
| `TimerManager.cancel(name)` | 返回值变更 | 原来恒返回 `None`，现在返回 `bool`（该任务原本是否存在） |
| `napcat.skills.NamedRegistry` | 新增（基类） | 技能表与工具表共用的骨架：`qualify` / `list` / `get` / `unregister` / `unregister_plugin`；`SkillRegistry` 与 `ToolRegistry` 都继承它 |
| `qualify_name(plugin, name)` / `tool_name_for(name)` | 新增（模块函数） | 命名空间归一化（`插件名.名字`）与「暴露给模型的函数名」的唯一实现 |

## 3. 行为变更（对外可见）

| 变更 | 影响 | 需要做什么 |
|------|------|-----------|
| 插件发现跳过 `.` 开头的目录 | `plugins/.trash`、安装中的 `plugins/.new-<名>` 不会再被当成插件加载 | 无（若你依赖过这种目录名做插件，改成不以 `.` 开头） |
| `restart_cooldown` 未配置时的默认值统一为 180s | 客户端曾在配置缺省时回落 60s，与 `ServerLauncher` 和文档的 180s 不一致 | 无；显式配置的值仍然优先 |
| `watchdog.enabled` 不再作为开关 | 看门狗总开关是顶层 `watchdog_enabled`；旧键会记 warning 并忽略 | 把配置里的 `watchdog.enabled` 改成顶层 `watchdog_enabled` |
| `PluginBase.stop_command(name)` 回收别名 | 删除命令时它的别名条目一起消失（走 `CommandManager.unregister`），不会再出现「命令没了、别名还能用」 | 无 |
| `napcat.scope` 改名为 `napcat.subscribe`：`Scope` → `Subscription` | 判定语义不变（群/小窗按来源群、私聊按用户、空名单=谁都不用、超管豁免）。老名字保留为**兼容别名**（`napcat/scope.py` 转发到新模块） | 插件把 `from napcat.scope import Scope` 改成 `from napcat.subscribe import Subscription`；别名会在所有插件改用新名后删除 |
| **定时任务 API 收敛**：删掉 `PluginBase.stop_timer` / `pause_timer` / `resume_timer` / `cancel_schedule` | 这 4 个方法只是 `self.timer` 的同名转发（原来还有两个名字做同一件事）。现在**注册**用 `self.schedule` / `self.schedule_at`（带归属，卸载自动取消），**管理**用 `self.timer.cancel/pause_timer/resume_timer` | 把 `self.stop_timer(x)` 改成 `self.timer.cancel(x)`、`self.pause_timer(x)` 改成 `self.timer.pause_timer(x)`、`self.cancel_schedule(x)` 改成 `self.timer.cancel(x)` |
| `self.schedule` / `self.schedule_at` 补 `description` 参数 | 插件注册的定时任务现在能在 `/timers` 里显示说明 | 无 |
| `self.schedule` 的排程文档修正 | 语义一直是**固定频率**（按绝对时刻、耗时不计入周期）；原文档写成「本轮执行结束再计时」，与实现不符 | 无（按固定频率使用即可） |
| 加载失败 / 启用失败 / 卸载共用同一套拆卸 | 三条路径都会撤销该插件注册的**命令、技能、工具、事件处理器、定时任务与连接回调**；失败后不再留残留 | 无（插件里不必再自己写清理） |
| 内置 `/plugins` 命令移除，并入 `plugin_manager` 的 `/plugin list` | `/plugins` 改为 `plugin_manager` 注册的**等价别名**（同一渲染器、`superuser + private`）；框架不再提供「任何人可用」的插件列表 | 需要普通用户看插件列表的场景没有了；受限用户请改用服务范围内的功能 |
| `plugin_manager` 的 `/plugin list` 合并原 `/plugins` | 一次列出「已安装（✓/✗/⛔ + 版本 + 依赖 + 来源）」与「可安装」；`/plugins` 输出与它完全一致 | 无（脚本里解析输出的话注意分段标题变了） |
| 热重载只清理一次模块子树 | 语义不变（`reload()` 不再重复摘 `sys.modules`） | 无 |
| 技能表与工具表共用 `NamedRegistry` | `list(exclude=...)` 统一为 `exclude: str = ""`（空串 = 不过滤）；技能同名覆盖、工具同名拒绝的语义**保持不变** | 只用公开方法则无影响 |
| `plugin_manager`：插件根目录改名 `plugins_root` | 基类 `plugin_dir` 恢复「插件自己的目录」语义（`plugins/<名>/`） | 只有读过该插件内部属性的人需要改 |
| `plugin_manager`：列表文件缓存只剩客户端一层 | 客户端 TTL + ETag 决定「要不要问、问得多便宜」，磁盘副本只做离线兜底 | 无 |
| `plugin_manager`：`/plugin install` 的「已经装过」改为后台回执 | 命令先回「⏳ 开始」，失败原因随后走私聊回执（唯一判定在 `Installer`） | 无 |

## 4. 服务范围口径统一

五个官方插件（`ai_chat` / `bupt_news` / `byrdocs` / `oj_bank` / `todo`）现在共用
[`napcat.subscribe`](../plugin-dev/20-subscribe.md) 的判定：

| 维度 | 统一后的行为 |
|------|-------------|
| 名单来源 | 只看插件自己的配置，**不回落**框架 `bound_groups` |
| 空名单 | 谁都不用（不再有 `allow` 开关） |
| 群聊 / 群小窗 | 按**来源群**判 |
| 直接私聊 | 按**用户**判 |
| 超级用户 | 豁免（防「名单配错 → 谁都改不了」） |

各插件的差异按业务保留：

- `ai_chat`：**按会话邀请**的例外保留 —— 受邀者在**私聊**可用，群里不放开；
- `bupt_news`：`targets` 的逐对象配置保留（`message` / `event` / `token_file` / `token_by`），
  判准入只看两张表的键；
- `oj_bank`：`/oj bind` 的管理命令语义不变；
- `todo`：同一口径，没有额外例外（名单为空时只有超管可用）。

**配置变更**：`binding.empty_means` 已移除。老配置里保留这个键不会报错，但不再生效。

## 5. 升级检查清单

1. **白名单必须显式配置**：`binding`（`bupt_news` 是 `targets`）为空 = 谁都不能用。
   升级后先确认每个插件的名单，或用超管命令各加一条；
2. **移除 `binding.empty_means`**（可选清理，不影响运行）；
3. **插件与框架同版本发布**：官方插件现在依赖 `napcat.subscribe` / `napcat.persist`
   （与 `napcat.render` 同一约定：插件仓库按框架版本发布）；
4. **确认超管豁免符合预期**：`ai_chat` / `oj_bank` 的超管现在能进入未开通的群；
5. `plugin_manager` 的「已装过」提示改走私聊回执，排障时看后台消息而不是命令回复。

## 6. 验证

改动涉及的框架仓套件（从仓库根跑）：

```bash
./venv-arm/bin/python -B tests/test_subscribe.py      # 订阅名单口径
./venv-arm/bin/python -B tests/test_persist.py        # JSON 原子写 / 目录清理
./venv-arm/bin/python -B tests/test_plugin_errors.py  # 生命周期缺陷回归
./venv-arm/bin/python -B tests/test_plugin_deps.py    # 依赖图与加载顺序
```

插件侧验收在各自插件仓的 `tests/` 下跑：[ai_chat](https://github.com/Green1423/ncqqpy-plugin-ai-chat)、
[bupt_news](https://github.com/Green1423/ncqqpy-plugin-bupt-news)、[byrdocs](https://github.com/Green1423/ncqqpy-plugin-byrdocs)、
[oj_bank](https://github.com/Green1423/ncqqpy-oj-bank)。
