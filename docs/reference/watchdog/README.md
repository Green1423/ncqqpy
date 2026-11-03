# 看门狗（watchdog）

> 上级：[文档索引](../README.md)

> **它现在是框架能力，不是插件。** 代码在 [napcat/watchdog.py](../../../napcat/watchdog.py)（状态机）、
> [napcat/launcher.py](../../../napcat/launcher.py)（拉起+清 cache）、[napcat/ilink/](../../../napcat/ilink)（微信通道）、
> [napcat/qr_text.py](../../../napcat/qr_text.py) / [qr_image.py](../../../napcat/qr_image.py)（二维码编码与放大）。
> 配置在 config.json（config.json 只在本地，不入库） **顶层**（`watchdog_enabled` / `watchdog.*` / `restart_*`），
> 不再是 `plugins.ncqq_watchdog`。登录命令：`python -m napcat.ilink_login`。

## 章节

| 章节 | 小节 |
|------|------|
| [它做什么](01-overview.md) | — |
| [恢复流程（唯一状态机）](02-recovery.md) | 2.1 触发方式 · 2.2 二维码是怎么递的 · 2.3 主动健康检查 · 2.4 二维码发出后：重连固定 5 秒 … |
| [涉及的文件](03-files.md) | — |
| [启用步骤](04-setup.md) | 4.1 让运行机器人的用户能重启 ncqq（**需要 root 执行一次**） · 4.2 扫码登录微信 iLink（**只做一次**） · 4.3 让 bot 拿到 `context_token` · 4.4 重启 ncqqpy 让改动生效 |
| [配置](05-config.md) | 5.1 顶层（框架行为，`config.json`） · 5.2 看门狗（`config.json → watchdog`） |
| [命令](06-commands.md) | — |
| [测试](07-tests.md) | — |
| [已知边界](08-boundaries.md) | — |
