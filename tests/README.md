# tests/ —— 框架回归套件

从**仓库根**执行（`python -B` 关掉 __pycache__）：

```bash
for t in test_watchdog.py test_command_parser.py test_help_structure.py test_command_permissions.py test_plugin_errors.py test_plugin_config.py; do ./venv/bin/python -B tests/$t; done
```

| 套件 | 覆盖 |
|------|------|
| `test_watchdog.py` | 框架回归：WS/重连/服务器拉起/看门狗/插件热重载/AI 摘要重试 |
| `test_command_parser.py` | 命令解析 + 配置口径基线 |
| `test_help_structure.py` | /help 三级入口 + 权限/作用域 + 帮助重定向 |
| `test_command_permissions.py` | 运行时权限覆盖（/perm） |
| `test_plugin_errors.py` | 错误类型 / 统一错误反馈（@触发者 / 编号 / 节流 / 超管通道） |
| `test_plugin_config.py` | 插件配置模板契约（首次运行初始化全部配置项） |

## 依赖插件的用例会自动跳过

`plugins/` 里没有插件时（单独 clone 本仓库就是这种情况），需要真实插件的用例会
打印 `⏭️ 跳过` 而不是失败；把插件仓库 clone 到 `plugins/<插件名>/` 下即恢复全量：

```bash
git clone https://github.com/<you>/ncqqpy-plugin-bupt-news plugins/bupt_news
```

插件的测试套件在**各自的插件仓库**里（例如 ai_chat 的历史/渲染/搜索套件）。

## 临时目录

所有套件在 `tests/.test-tmp/` 下建临时夹具（该目录不进仓库，可随时删）。
`tests/backups/` 是配置备份目录，同样不进仓库。
