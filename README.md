# ncqqpy

基于 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) WebSocket 协议的 Python
机器人框架：**异步、插件化、自带命令/事件/权限/热重载/错误反馈**。

- **事件总线 + 命令系统**：事件处理器、子命令、Schema 参数校验、权限与作用域、
  帮助系统（`/help` 三级入口）。
- **插件系统**：`plugins/<名字>/` 放进目录即被加载；`PluginBase` 生命周期
  （`on_load → on_enable → on_disable → on_unload`）、文件变更热重载、加载失败回滚。
- **首次运行即初始化配置**：插件声明 `config_template()`，框架把它全部配置项
  （含嵌套段）写进 `config.json`，只补缺、不覆盖、幂等。
- **失败不静默**：命令/事件/定时任务出错都会记日志并回给触发者（群里 @ 他），
  带稳定错误编号；插件加载失败私聊超管。
- **连接自愈**：断线指数退避重连、掉线期间 API 排队补发、服务器拉不起来自动重启
  并取二维码通知（`napcat/watchdog.py`）。

## 快速开始

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.json config.json     # 填 ws_url / token / superusers
./run.sh                               # 或 python main.py
```

`config.json` 首次运行会自动补齐默认值；插件被加载时会把各自的配置段写进去。

## 插件

| 插件 | 说明 |
|------|------|
| [`ai_chat`](https://github.com/Green1423/ncqqpy-plugin-ai-chat) | 把 DSH 式的对话语义搬进 QQ：多会话、上下文压缩、内置工具、联网搜索、函数图像与 Markdown 出图。 |
| [`bupt_news`](https://github.com/Green1423/ncqqpy-plugin-bupt-news) | 定时同步北邮校内通知（门户登录 + AI 摘要 + 表格/图片处理），按配置推送到指定群与私聊。 |
| [`byrdocs`](https://github.com/Green1423/ncqqpy-plugin-byrdocs) | 检索 byrdocs.org 资料库、按编号翻页取详情、下载文件到群，带服务范围白名单。 |
| [`essential`](https://github.com/Green1423/ncqqpy-plugin-essential) | 禁言/踢人/解禁/群信息等群管理命令，以及按群配置的入群欢迎语（含定时撤回）。 |

插件是**独立的仓库**：clone 到本仓库的 `plugins/` 下即被加载（也可以放到别处再
`ln -s` 进来）。

```bash
git clone https://github.com/Green1423/ncqqpy-plugin-ai-chat plugins/ai_chat
```

写自己的插件见 [`docs/plugin-dev.md`](docs/plugin-dev.md)：生命周期、命令/Schema、
事件、权限、错误类型（`PluginTimeoutError` 等）、配置模板契约、热重载语义。

## 常用运维命令（QQ 内，超级用户 · 仅私聊）

| 命令 | 作用 |
|------|------|
| `/plugins` | 查看已加载插件（任何人可用）|
| `/reload [插件名]` | 热重载全部/指定插件 |
| `/commands` `/events` `/timers` | 查看已注册的命令/监听器/定时任务 |
| `/stop` `/pause` | 按 id 停用/暂停监听器、命令、定时任务 |
| `/perm` | 运行时权限覆盖（写进 `config.json`）|

## 测试

```bash
./venv/bin/python -B tests/test_watchdog.py       # 框架回归（含热重载/看门狗）
./venv/bin/python -B tests/test_plugin_config.py  # 配置模板契约
./venv/bin/python -B tests/test_plugin_errors.py  # 错误类型与反馈
```

其余套件（命令解析、帮助结构、权限覆盖）见 [`tests/README.md`](tests/README.md)。
依赖具体插件的用例在 `plugins/` 为空时会**自动跳过**，所以单独 clone 本仓库也能跑。

| 套件 | 覆盖 |
|------|------|
| `tests/test_watchdog.py` | 覆盖 |
| `tests/test_command_parser.py` | 覆盖 |
| `tests/test_help_structure.py` | 覆盖 |
| `tests/test_command_permissions.py` | 覆盖 |
| `tests/test_plugin_errors.py` | 覆盖 |
| `tests/test_plugin_config.py` | 覆盖 |

## 目录

```
napcat/            框架包（客户端、事件、命令、插件、权限、看门狗…）
docs/usage.md      使用文档（配置、消息、事件、API、命令、插件运维）
docs/plugin-dev.md 插件开发文档（含错误类型与配置模板契约）
tests/             框架回归套件
config.example.json 脱敏配置示例（真实 config.json 不要提交）
```

## License

MIT，见 [LICENSE](LICENSE)。
