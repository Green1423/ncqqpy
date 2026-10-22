# tests/ —— 框架回归套件

从**仓库根**执行（`python -B` 关掉 __pycache__）：

```bash
for t in test_watchdog.py test_command_parser.py test_help_structure.py test_command_permissions.py test_plugin_errors.py test_plugin_config.py test_plugin_deps.py test_timer.py test_message.py test_store.py test_plugin_layout.py test_plugin_registry.py test_plugin_ws_decoupling.py test_userperm.py test_public_api.py test_command_alias.py; do ./venv/bin/python -B tests/$t; done
```

| 套件 | 覆盖 |
|------|------|
| `test_watchdog.py` | 框架回归：WS/重连/服务器拉起/看门狗/插件热重载/AI 摘要重试 |
| `test_command_parser.py` | 命令解析 + 配置口径基线 |
| `test_help_structure.py` | /help 三级入口 + 权限/作用域 + 帮助重定向 |
| `test_command_permissions.py` | 运行时权限覆盖（/perm） |
| `test_plugin_errors.py` | 错误类型 / 统一错误反馈（@触发者 / 编号 / 节流 / 超管通道） |
| `test_plugin_config.py` | 插件配置模板契约（首次运行初始化全部配置项） |
| `test_plugin_deps.py` | 插件依赖：必需/可选声明、拓扑序、缺依赖不加载、卸载保护 |
| `test_timer.py` | 定时任务精度（不累积偏差 / 落后跳过 / 每日定时月末 / 暂停恢复） |
| `test_message.py` | 消息切分（原文链接不被切断 / 预算不浪费 / 媒体配额） |
| `test_store.py` | sqlite ORM（字段/查询/事务/补列/并发/插件集成） |
| `test_plugin_layout.py` | 插件目录布局（拆分 __init__.py / PLUGIN 声明 / 热重载） |
| `test_plugin_registry.py` | 声明式注册（注册处只装配 / 逻辑分模块 / 卸载回收） |
| `test_plugin_ws_decoupling.py` | 生命周期与 WebSocket 解耦（重连不重载插件） |
| `test_userperm.py` | 用户级权限（全局/群内覆盖、优先级、群角色默认 admin、持久化） |
| `test_public_api.py` | 公开 API 分类（子模块导入 / 懒加载 / 兼容层） |
| `test_command_alias.py` | 命令别名（增删改查 / 持久化 / 热重载重放 / 生命周期清理） |

## 依赖插件的用例会自动跳过

`plugins/` 里没有插件时（单独 clone 本仓库就是这种情况），需要真实插件的用例会
打印 `⏭️ 跳过` 而不是失败；把插件仓库 clone 到 `plugins/<插件名>/` 下即恢复全量：

```bash
git clone https://github.com/Green1423/ncqqpy-plugin-bupt-news plugins/bupt_news
```

插件的测试套件在**各自的插件仓库**里（例如 ai_chat 的历史/渲染/搜索套件）。

## 临时目录

所有套件在 `tests/.test-tmp/` 下建临时夹具（该目录不进仓库，可随时删）。
`tests/backups/` 是配置备份目录，同样不进仓库。
