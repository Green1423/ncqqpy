# 六、命令

> 文档索引：[docs/README.md](../../README.md) · 上级：[看门狗（watchdog）](README.md) · 上一章：[配置](05-config.md) · 下一章：[测试](07-tests.md)


| 命令 | 说明 |
|------|------|
| `/watchdog status` | 连接状态、拉起次数与结果、微信通道状态、二维码情况 |
| `/watchdog test [内容]` | 发一条微信测试通知（验证通道） |
| `/watchdog qr [路径]` | 把二维码发到微信（手动补发） |
| `/watchdog target <id>` | 设置通知接收人 |

全部是 `scope="private"` + `permission="superuser"`：只能在私聊里、由超级用户触发。

---
