# 三、涉及的文件

> 文档索引：[docs/README.md](../../README.md) · 上级：[看门狗（watchdog）](README.md) · 上一章：[恢复流程（唯一状态机）](02-recovery.md) · 下一章：[启用步骤](04-setup.md)


| 文件 | 作用 |
|------|------|
| (../../../napcat/watchdog.py) | **状态机**：`Watchdog._run_episode()` 单任务推进恢复流程、健康检查、二维码递送与去重 |
| (../../../napcat/launcher.py) | **框架层**：按配置拉起服务端（systemd / launcher 两种模式） |
| (../../../napcat/client.py) | `WsHandler` 新增 `on_connect_failed` / `on_connected` / `on_disconnected` 回调；客户端新增 `on_ws_*` 注册接口、`ws_connected`、`launcher` |
| (../../../napcat/plugin.py) | 插件现在能拿到 `self.client`（读取连接状态、拉起结果） |
| (../../../napcat/ilink) | 微信 iLink Bot 协议客户端（**纯标准库**，支持扫码登录/发文本/发图片） |
| (../../../napcat/ilink/notifier.py) | 微信通知通道（所有失败只记日志，不影响主流程） |
| (../../../napcat/qr_text.py) | QR 编码器（把二维码链接画成 ASCII/PNG） |
| (../../../napcat/qr_image.py) | PNG 解码 + 最近邻放大（把 NapCat 的 147×147 小图放大到能扫） |
| (../../../napcat/ilink_login.py) | 扫码登录入口（`python -m napcat.ilink_login`） |
| (../../../plugins/ncqq_watchdog) | 退役插件的残留目录（只剩 `data/`，不再是代码位置） |
| (../../../tests/test_watchdog.py) | 端到端测试（真起 WebSocket 服务端 + 真执行进程） |

---
