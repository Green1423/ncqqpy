# 二、客户端配置

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[快速开始](01-quickstart.md) · 下一章：[消息构造系统](03-messages.md)


## 2.1 NapCatClient 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `ws_url` | `str` | `"ws://127.0.0.1:3001"` | NapCat WebSocket 地址 |
| `token` | `str` | `""` | 鉴权 Token，留空不鉴权 |
| `plugin_dir` | `str` | `"./plugins"` | 插件目录路径 |
| `superusers` | `list[int]` | `None` | 超级用户 QQ 号列表 |
| `command_prefixes` | `list[str]` | `None` | 命令前缀，默认 `["/", "!", "。", "#"]` |
| `max_concurrency` | `int` | `100` | 事件处理器最大并发数 |
| `reconnect_interval` | `float` | `5.0` | 断线后首次重连等待（秒）；连续失败按 2 倍指数退避、封顶 60s |
| `api_delay` | `float \| list[float]` | `[0.5, 1.0]` | **所有 API 上报**发送前的延迟：数字=固定值，`[min, max]`=随机区间（秒）。`0` 关闭 |

> `api_delay` / `reconnect_interval` 也可以写在 (../../config.json) 里（构造参数优先）。
> `api_delay` 同时起**串行**作用：并发发起的 API 调用会排队，两次上报间隔不小于区间下限。

## 2.2 客户端属性速览

| 属性 | 类型 | 说明 |
|------|------|------|
| `client.api` | `ApiClient` | API 调用客户端（100+ 方法） |
| `client.event_bus` | `EventBus` | 事件总线（事件注册与分发） |
| `client.timer` | `TimerManager` | 定时管理器 |
| `client.perm` | `PermissionManager` | 权限管理器 |
| `client.cmds` | `CommandManager` | 命令管理器 |
| `client.files` | `FileManager` | 文件管理器 |
| `client.plugins` | `PluginManager` | 插件管理器 |
| `client.error_reporter` | `ErrorReporter` | 错误反馈器（文案 / 编号 / 受众 / 节流；见 (08-plugin-dev.md#86-完整生命周期)）|
| `client.self_id` | `int` | 机器人 QQ 号（启动后可用） |
| `client.logger` | `Logger` | Python 标准 Logger |

## 2.2.1 `config.json` 顶层：错误反馈策略

```json
"error_report": {
  "enabled": true,          // false = 出错只写日志，不回任何文案
  "throttle_seconds": 60,   // 同一错误编号的最小重复间隔（窗口内只回短提示）
  "show_reason": true,      // false = 只给编号，不附短原因
  "notify_superusers": true // 加载/启用失败、定时任务失败是否私聊超管
}
```

运行期失败（命令、事件处理器）会回给触发者 —— 群聊里 **@ 他**；
生命周期失败只私聊超管。插件可抛的错误类型见 (../plugin-dev/16-errors.md#十六错误处理与用户反馈)。

## 2.3 启动与退出

```python
# 方式一：async 上下文
async def main():
    client = NapCatClient(ws_url="ws://127.0.0.1:3001")
    try:
        await client.start()
    finally:
        await client.stop()

# 方式二：带 Ctrl+C 处理
await client.run()

# 方式三：同步入口（最简）
client.run_blocking()
```

## 2.4 断线重连与上报节流

**WebSocket 自动重连**（`WsHandler.run_forever()`）：连接由后台任务托管，
下列情况一律自动重连，不需要业务代码处理：

| 情况 | 行为 |
|------|------|
| 启动时 NapCat 还没起来（连接被拒 / 超时 / TLS / 鉴权失败…）| 后台重试，**不会**让机器人启动失败 |
| 连接被对端关闭（NapCat 重启、网络抖动）| 记录日志 → 等待退避 → 重连 → 刷新登录信息 |
| 接收/发送过程抛任何异常 | 一律当作断线处理（仅「脏 JSON」单条丢弃，不断线）|
| 重连成功 | 重新拉取 `get_login_info` 刷新 `client.self_id`（群聊 @bot 判定依赖它）|

- 退避：连续失败按 `reconnect_interval × 2^(n-1)` 递增，封顶 60s；连上后计数清零。
- 主动退出：`client.stop()` 会先停重连循环，再关闭连接（不会在退出时又连上）。

**掉线期间的 API 调用会保留**（`WsHandler._wait_ready()`）：

| 情况 | 行为 |
|------|------|
| 服务器没上线时调用 API | **不失败、排队等待**，服务器一上线按序补发（日志：`API 调用开始排队…` / `开始补发 N 个排队中的 API 调用`）|
| 等待上限 | `offline_hold_timeout` 秒，默认 `0` = **一直等**；设正数则超时抛 `ConnectionError` |
| 排队上限 | `offline_queue_max`，默认 `1000`；超出后新调用立即 `ConnectionError`（防止无限堆积）|
| 进程退出 | 排队中的调用被取消（抛 `ConnectionError`），不会永久挂住 |
| **已发出但没等到响应**的调用 | 仍然立即失败 —— 不自动重发，避免把消息重复发一遍 |

```python
client = NapCatClient(offline_hold_timeout=0)      # 默认：一直等服务器回来
client = NapCatClient(offline_hold_timeout=300)    # 最多等 5 分钟，超时算失败
```

> 副作用：掉线期间插件里的推送/发送会**挂住**而不是报错，等重连后补发
> （例如 (../../plugins/bupt_news) 的一轮同步会一直等到重连才结束，期间后续轮次自动跳过）。

**所有 API 上报节流**：`ApiClient` 的每个调用发送前都会等待 `api_delay`
（默认 0.5–1.0s 随机值）并串行化，用于规避 QQ/NapCat 的风控阈值：

```python
client = NapCatClient(api_delay=(0.5, 1.0))   # 默认；区间随机
client = NapCatClient(api_delay=1.0)          # 固定 1s
client = NapCatClient(api_delay=0)            # 关闭节流（压测/联调）
```

> 影响：命令回复、插件推送都会多等 0.5–1s；批量推送 N 条消息的总耗时约 `N × 0.75s`。
> 代价是慢一点，收益是长时间运行不容易被限流。

---
