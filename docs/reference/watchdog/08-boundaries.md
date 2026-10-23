# 八、已知边界

> 文档索引：[docs/README.md](../../README.md) · 上级：[看门狗（watchdog）](README.md) · 上一章：[测试](07-tests.md)


- **微信通道依赖 iLink**：登录态过期（`errcode=-14`）后需要重新扫码；通知器会把这条错误写进日志与
  `/watchdog status`。
- **主动推送需要 `context_token`**：用户从没给 bot 发过消息时，掉线通知发不出去（日志会给出指引）。
  这是 iLink 协议的限制，不是实现问题。
- **发图片需要 `cryptography`**：venv 里没装时自动回退到系统的 `/usr/lib/python3/dist-packages`；
  都没有就只能发文字（会给出二维码文件路径）。
- **冷却期**：掉线 60 秒内的重复触发不会再次拉起，日志记「跳过拉起（冷却中）」。
