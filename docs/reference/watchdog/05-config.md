# 五、配置

> 文档索引：[docs/README.md](../../README.md) · 上级：[看门狗（watchdog）](README.md) · 上一章：[启用步骤](04-setup.md) · 下一章：[命令](06-commands.md)


## 5.1 顶层（框架行为，`config.json`）

| 键 | 默认 | 说明 |
|----|------|------|
| `restart_enabled` | `true` | 是否在连不上时拉起服务端 |
| `restart_mode` | `"systemd"` | `systemd` = `systemctl restart <unit>`；`launcher` = 把 `restart_command` 当命令行 exec |
| `restart_command` | `"ncqq"` | systemd 模式下是**服务名**；launcher 模式下是完整命令行（如 `bash /opt/ncqq/launcher.sh`） |
| `restart_timeout` | `60.0` | 拉起命令超时（秒） |
| `restart_cooldown` | `180.0` | 两次真实拉起之间的最小间隔（**默认 3 分钟**）：拉起+QQ 启动本身要几十秒，间隔太短只会反复重启、每次还要重新扫码 |
| `restart_clear_cache_dir` | `/opt/ncqq/napcat/cache` | 拉起前清空的目录（登录二维码所在）。**需要 root**。安全防护：拒绝空值、相对路径、过浅路径（`/`、`/etc`、`/opt`、`/root`、家目录），命中则跳过并记日志 |
| `reconnect_interval` | `5.0` | 断线后首次重连等待 |
| `max_reconnect_interval` | `60.0` | 指数退避上限 |
| `short_connection_threshold` | `3.0` | 连接活不过这个秒数算「连上就断」，同样计入退避 |

> **为什么不直接用 `bash /opt/ncqq/launcher.sh`？** 那个脚本是个**阻塞式前台进程**
> （`LD_PRELOAD=... qq --no-sandbox`），而且已经在 `ncqq.service` 里被 systemd 托管。
> 从 ncqqpy 再 exec 一遍，会绕过 systemd 起第二个 QQ 实例、也没法干掉旧实例。
> 所以默认走 `systemctl restart ncqq` —— 等价于重新执行 launcher.sh，但不和 systemd 抢进程。
> 如果你的部署里没有 systemd 托管，把 `restart_mode` 设成 `launcher`、
> `restart_command` 设成 `bash /opt/ncqq/launcher.sh` 即可。

## 5.2 看门狗（`config.json → watchdog`）

| 键 | 默认 | 说明 |
|----|------|------|
| `enabled` | `true` | 总开关 |
| `qrcode_paths` | `["/opt/ncqq/napcat/cache/qrcode.png", "/opt/ncqq/cache/qrcode.png"]` | 二维码候选路径，取 mtime 最新的。**注意**：NapCat 实际写在 `cachePath`（`/opt/ncqq/napcat/cache/`）下，第二个路径只是兼容写法 |
| `qrcode_wait_seconds` | `180` | 重连后等新二维码出现的时长 |
| `qrcode_upscale` | `8` | 转发前放大倍数（微信会压图，放大更好扫） |
| `notify.on_offline` | `true` | 掉线通知 |
| `notify.on_reconnect` | `true` | 恢复通知 |
| `notify.on_qrcode` | `true` | 二维码通知 |
| `notify.on_launch_failed` | `true` | 拉起失败追加告警 |
| `notify.on_login_ok` | `true` | 登录成功后发反馈 |
| `offline_grace_seconds` | `25` | **抖动抑制观察期**：掉线不足这个时长就恢复 → 不发任何通知 |
| `qrcode_follow_launch` | `true` | 每次拉起服务端后跟进登录二维码 |
| `restart_on_startup` | `true` | 程序启动后执行一次重启 |
| `startup_restart_delay` | `10.0` | 启动后等这么久再重启（期间掉线则提前进入恢复流程） |
| `qrcode_after_launch_delay` | `20.0` | **拉起后等这么久再发二维码**（给 QQ 启动 + 出码留时间） |
| `health_check` | `true` | 是否每 5 秒主动 `get_status` |
| `health_interval` | `5.0` | 健康检查间隔（秒） |
| `health_max_failures` | `3` | 连续失败几次才触发恢复（防单次抖动误判） |
| `qrcode_poll_interval` | `1.0` | 轮询二维码文件、比对哈希的间隔 |
| `qrcode_scan_retry_interval` | `5.0` | **二维码发出后**（等用户扫码期间）WebSocket 固定重连间隔 |
| `ilink_retry_timeout` | `300.0` | iLink 发送失败后的重试总时限 |
| `ilink_retry_interval` | `5.0` | 重试间隔：**失败就每 5 秒重试一次，直到成功或超时** |
| `max_restart_cycles` | `0` | 每轮恢复流程的重启上限；`0` = 不限次数 |
| `retry_notify_every_rounds` | `10` | 不限次数时，每这么多轮才发一条微信提示 |
| `connect_timeout` | `60` | **每轮等登录成功的时限（1 分钟）**，超时回到「重启 ncqq」 |
| `qrcode_wait_seconds` | `180` | 等二维码出现的上限（NapCat 出码慢时不至于漏发） |
| `wechat.enabled` | `true` | 微信通道开关 |
| `wechat.target` | `""` | 收通知的微信用户 id |
| `wechat.auto_context` | `true` | 后台长轮询缓存 `context_token` |
| `wechat.credentials_file` | `data/ilink_credentials.json` | 凭据路径 |

---
