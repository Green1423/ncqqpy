# 框架变更记录（插件系统 / 本地状态 / 服务范围）

> 上级：[参考文档](README.md) · 插件作者请从 [插件开发](../plugin-dev/README.md) 开始

本页记录**对外可见**的框架变更：新增了哪些 API、哪些行为变了、升级要做什么。
只写结论与迁移步骤；用法看对应章节，设计动机看代码里的注释。

## 1. 新增模块

| 模块 | 内容 | 用法 |
|------|------|------|
| `napcat.scope` | `Scope`（群/用户白名单判定）、`parse_ids`（名单解析） | [服务范围](../plugin-dev/20-scope.md) |
| `napcat.persist` | `read_json` / `write_json`（原子 JSON）、`prune_keep` / `prune_unreferenced`（目录清理） | [本地状态与清理](../plugin-dev/21-persist.md) |

两个模块都已登记进 `napcat._SUBMODULES`：`import napcat` 之后可用
`napcat.scope.Scope` / `napcat.persist.write_json` 按需加载。

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
| `PluginBase.stop_command(name)` 回收别名 | 删除命令时它的别名条目一起消失（走 `CommandManager.unregister`），不会再出现「命令没了、别名还能用」 | 无 |
| `PluginBase.stop_timer(name)` 如实回报 | 任务不存在时返回 `False`（原来恒为 `True`）；同时清掉实例的任务记账 | 依赖返回值做提示的地方会变准 |
| `PluginBase.cancel_schedule(name)` 与 `stop_timer` 同义 | 两个名字一个实现，返回值也一致 | 无 |
| `pause_timer` / `resume_timer` 去掉 `hasattr` 兜底 | `TimerManager` 一定有这两个方法，防御分支不会命中 | 无 |
| 加载失败 / 启用失败 / 卸载共用同一套拆卸 | 三条路径都会撤销该插件注册的**命令、技能、工具、事件处理器、定时任务与连接回调**；失败后不再留残留 | 无（插件里不必再自己写清理） |
| 热重载只清理一次模块子树 | 语义不变（`reload()` 不再重复摘 `sys.modules`） | 无 |
| 技能表与工具表共用 `NamedRegistry` | `list(exclude=...)` 统一为 `exclude: str = ""`（空串 = 不过滤）；技能同名覆盖、工具同名拒绝的语义**保持不变** | 只用公开方法则无影响 |
| `plugin_manager`：插件根目录改名 `plugins_root` | 基类 `plugin_dir` 恢复「插件自己的目录」语义（`plugins/<名>/`） | 只有读过该插件内部属性的人需要改 |
| `plugin_manager`：列表文件缓存只剩客户端一层 | 客户端 TTL + ETag 决定「要不要问、问得多便宜」，磁盘副本只做离线兜底 | 无 |
| `plugin_manager`：`/plugin install` 的「已经装过」改为后台回执 | 命令先回「⏳ 开始」，失败原因随后走私聊回执（唯一判定在 `Installer`） | 无 |

## 4. 服务范围口径统一

四个内置插件（`ai_chat` / `bupt_news` / `byrdocs` / `oj_bank`）现在共用
[`napcat.scope`](../plugin-dev/20-scope.md) 的判定：

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
- `oj_bank`：`/oj bind` 的管理命令语义不变。

**配置变更**：`binding.empty_means` 已移除。老配置里保留这个键不会报错，但不再生效。

## 5. 升级检查清单

1. **白名单必须显式配置**：`binding`（`bupt_news` 是 `targets`）为空 = 谁都不能用。
   升级后先确认每个插件的名单，或用超管命令各加一条；
2. **移除 `binding.empty_means`**（可选清理，不影响运行）；
3. **插件与框架同版本发布**：内置插件现在依赖 `napcat.scope` / `napcat.persist`
   （与 `napcat.render` 同一约定：插件仓库按框架版本发布）；
4. **确认超管豁免符合预期**：`ai_chat` / `oj_bank` 的超管现在能进入未开通的群；
5. `plugin_manager` 的「已装过」提示改走私聊回执，排障时看后台消息而不是命令回复。

## 6. 验证

改动涉及的套件（全部应从仓库根跑）：

```bash
./venv-arm/bin/python -B tests/test_scope.py          # 服务范围口径
./venv-arm/bin/python -B tests/test_persist.py        # JSON 原子写 / 目录清理
./venv-arm/bin/python -B tests/test_plugin_errors.py  # 生命周期缺陷回归
./venv-arm/bin/python -B tests/test_plugin_deps.py    # 依赖图与加载顺序
./venv-arm/bin/python -B tests/test_byrdocs.py        # 四个插件的准入行为
./venv-arm/bin/python -B tests/test_bupt_news.py
./venv-arm/bin/python -B tests/test_oj_bank.py
./venv-arm/bin/python -B tests/test_ai_chat.py
```
