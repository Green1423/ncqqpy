# 服务器看门狗（框架内置）

> **它现在是框架能力，不是插件。** 代码在 `napcat/watchdog.py`（状态机）、
> `napcat/launcher.py`（拉起+清 cache）、`napcat/ilink/`（微信通道）、
> `napcat/qr_text.py` / `qr_image.py`（二维码编码与放大）。
> 配置在 `config.json` **顶层**（`watchdog_enabled` / `watchdog.*` / `restart_*`），
> 不再是 `plugins.ncqq_watchdog`。登录命令：`python -m napcat.ilink_login`。

## 恢复流程（唯一状态机）

    websocket 断开
      → 重启 ncqq（先清 cache，再执行 restart_command）
      → 取新 qrcode（cache 已清空 ⇒ 必是本次生成，不判断新旧）
      → ilink 发送二维码
      → 等连接成功
          成功 → ilink 发送「登录成功」
          超时 → 回到「重启 ncqq」，**默认不限次数**直到连上

由 `Watchdog._run_episode()` 单任务顺序推进；框架回调只置位
（`_kick_episode()` 掉线 / `_on_reconnected()` 恢复），不再有多条并行路径抢二维码。

### 触发方式

| 触发 | 配置 | 说明 |
|------|------|------|
| **程序启动** | `restart_on_startup: true`（默认）、`startup_restart_delay: 10`（秒） | 服务起来后等 10s 执行**一次**恢复流程。期间若已掉线会提前进入，不用等满延迟 |
| **掉线** | 总是生效 | 连接失败 / 连接断开立即触发同一条流程 |
| **健康检查** | `health_check: true`、`health_interval: 5`、`health_max_failures: 3` | 每 5s 主动 `get_status`，要求 `online` 且 `good` **都为 true**；连续 3 次不满足就按「断联」处理 |

> 为什么还要主动查：**websocket 连着不等于账号正常**。QQ 掉线但 OneBot 连接仍在时，
> 连接层看不出问题（「假在线」），只有 `get_status` 的 `online`/`good` 能发现。

两者共用 `_run_episode()`，由 `_run_loop()` 串行调度，所以不会并发抢二维码。

## 一、它做什么

| 时机 | 动作 |
|------|------|
| WebSocket **连不上**（服务器没上线） | 执行 `restart_command` 拉起服务端（**每轮只拉一次**），并发微信通知 |
| WebSocket **连上后又断开** | 同上；断开说明 QQ/服务端出问题了 |
| **掉线持续超过观察期** | 发「掉线」通知（抖动不发，见下） |
| **恢复** | 报平安（含掉线时长、原因、拉起结果）——仅当确实发过掉线通知 |
| **每次拉起服务端之后** | 等新 `qrcode.png` 出现并**放大后发到微信** —— 重启 QQ 必然要重新扫码 |
| 恢复后出现**新的** `qrcode.png` | 同上（**但只在刚重启过的时间窗内**，见下） |
| **登录成功** | 发一条「QQ 已登录成功，二维码已失效」的反馈 |
| 拉起命令**执行失败** | 追加一条告警（含可操作建议），避免你干等 |

### 二维码是怎么递的

**拉起前清空 cache → 等 20 秒 → 直接发。** 不需要判断新旧。

二维码在 `/opt/ncqq/napcat/cache/qrcode.png`。框架在**执行拉起命令之前**
先 `rmtree` 整个 cache 目录（`restart_clear_cache_dir`，默认
`/opt/ncqq/napcat/cache`），NapCat 启动时会自己重建它。于是：

> cache 里能读到的 `qrcode.png` **必然是本次启动刚生成的** —— 不必再判断新旧。

这一步现在由 **ncqqpy 自己完成**（服务以 root 运行，`/opt/ncqq` 属 root）。
若权限不足，日志会警告 `清理 cache 失败 …`，拉起照常继续。

> 备选方案（不想让应用动系统目录时）：把清理放进 systemd 单元——
> 在 `ncqq.service` 的 `[Service]` 段加一行
> `ExecStartPre=-/bin/rm -rf /opt/ncqq/napcat/cache`，然后
> `sudo systemctl daemon-reload`。这样任何来源的重启都会先清 cache。

插件侧只剩三步：

| 步骤 | 说明 |
|------|------|
| 1. 等 `qrcode_after_launch_delay`（默认 **20s**） | 给 QQ 启动 + 出码留时间。**所有路径都遵守这个间隔** —— 恢复/巡检路径若不等，会在码刚写出来（几秒）就抢先发出去，20s 就形同虚设（实测 6s 就发了） |
| 2. 等文件出现并写完 | 文件存在且非空、连续两次一致（避免发半个文件） |
| 3. 直接发 | 内容指纹去重，同一张码不会因拉起重试刷两遍 |

恢复/定时巡检路径同样走这三步（覆盖「没重启但 QQ 掉登录」）。

> 兜底提醒：若某张码的 mtime **早于**本次拉起，日志会警告
> `二维码比本次拉起更早 … 检查 cache 是否被清空`，但**仍会发送** ——
> 宁可多发一张作废的码，也不冒漏发的风险。

### 主动健康检查

`_health_loop()` 每 `health_interval`（默认 5s）调一次 `get_status`：

| 检查结果 | 处理 |
|----------|------|
| `online=true` 且 `good=true` | 正常，清零失败计数 |
| 任一为 false，或调用失败 / websocket 未连接 | 记一次失败；连续到 `health_max_failures`（默认 3）次 → 走**与断联完全相同**的恢复链路 |

日志形如：

```
[watchdog] 健康检查未通过（连续 1/3）: online=False good=True
[watchdog] 账号状态异常，按「断联」处理：online=False good=True
```

### 二维码发出后：重连固定 5 秒

二维码递到用户手上后，`_set_scan_waiting(True)` 会让 WebSocket 期间**固定每
`qrcode_scan_retry_interval`（默认 5s）重连一次**，不做指数退避 ——
否则退避到 60s 时，用户扫完还要干等一分钟才恢复。登录成功（`_mark_online`）
后自动退出该模式，恢复常规退避。

### 触发去重（曾经在这里翻过车）

`_kick_episode()` 判断的是「**当前有没有恢复流程在跑**」，不是
`_mark_offline()` 的返回值。后者在掉线期间恒为 False（`_offline_since` 会一直
置位到真正登录成功），拿它当闸门会让**第一轮之后的所有触发被永久丢弃** ——
实测表现是健康检查每 15 秒报一次「账号状态异常」，但再也起不来恢复流程，
重启和二维码推送全部停摆。

另外不限次数时，一轮跑完（暂时放弃）会复位掉线标记，好让下一条告警能重新开始；
设置上限时则保持「已放弃」状态，真正停下来等人工介入。

### 重启次数默认不限

`max_restart_cycles: 0`（默认）= **不限次数**：每轮「重启 ncqq → 等二维码 → 发码 →
等登录（`connect_timeout`，默认 **1 分钟**）」失败后继续下一轮，直到连上为止。

- 轮与轮之间仍受 `restart_cooldown`（默认 3 分钟）约束，不会把服务端打爆；
- 不限制次数时**不会每轮都发微信**：每 `retry_notify_every_rounds`（默认 10）轮
  才提示一次「已连续重启 N 轮仍未登录成功，继续重试中…」；
- 设成正数（如 `2`）即恢复旧行为：跑满就停下并提示需要人工介入。

### iLink 发送失败会重试

二维码、掉线通知、登录成功反馈都走带重试的发送（`send_text_retry` /
`send_image_retry`）：

```
发送失败 → 等 ilink_retry_interval（默认 5s）→ 再试 → … → 直到成功或
          ilink_retry_timeout（默认 300s）超时放弃
```

- **只有传输类失败才重试**（网络错误、会话过期、服务端 5xx）；配置类问题
  （没凭据 / 没 target）立即返回，否则会白等满 timeout；
- 重试成功会记一条 `微信通知第 N 次尝试成功`；超时放弃记 `重试 N 次仍未成功，放弃`。

### 登录成功反馈

WebSocket 恢复意味着 `get_login_info` 成功 —— 也就是 QQ 登录态可用。此时如果
登录那一刻的二维码指纹没有再变过（没有刷新出新码），就说明确实登进去了，
发一条反馈：

```
✅ QQ 已登录成功
时间：2026-09-25 11:02:13
登录二维码已失效，无需再扫码；机器人已恢复收发消息。
```

由 `notify.on_login_ok` 控制，并有 120s 冷却，避免连接抖动时反复报「登录成功」。

### 抖动是怎么被压住的

实测日志里出现过这种形态（每 6 秒一轮，持续 30+ 轮）：

```
WebSocket 已连接 → 重连成功（第 31 次连接） → 连接已关闭: received 1005
→ 无法获取登录信息 → WebSocket 已断开 → 5.0s 后重连 → （循环）
```

**每次重连都成功、但一秒后就被对端关掉**。两层处理：

1. **框架层（退避）**：连接活得比 `short_connection_threshold`（默认 3s）短，
   就算一次失败并参与指数退避，所以节奏从
   `5s → 10s → 20s → 40s → 60s(封顶)` 逐级拉开，不再固定 5 秒空转。
   连接活过阈值时挂一个定时器，立刻计入 `stable_connect_count` 并清零 `flap_count`。
2. **插件层（通知抑制）**：掉线后**不立即通知**，先观察 `offline_grace_seconds`
   （默认 25s）。观察期内自行恢复 → 只写日志，**一条通知都不发**；
   观察期结束仍未恢复 → 发一条「掉线」，之后即使连接继续反复抖动也不再重复发。
   恢复时只有确实发过掉线通知才报平安（不会出现只有「已恢复」的困惑消息）。

> 这两个计数是**分开**的，不能合并：退避用的 `failures` 会把「连上就断」算进去，
> 而「首次连不上才拉起」必须用独立的标志位 —— 否则抖动抬高 `failures` 之后，
> 服务器**真的**下线时第一次连接失败就不会再触发拉起了（这个 bug 实际发生过）。

---

## 二、涉及的文件

| 文件 | 作用 |
|------|------|
| `napcat/launcher.py` | **框架层**：按配置拉起服务端（systemd / launcher 两种模式） |
| `napcat/client.py` | `WsHandler` 新增 `on_connect_failed` / `on_connected` / `on_disconnected` 回调；客户端新增 `on_ws_*` 注册接口、`ws_connected`、`launcher` |
| `napcat/plugin.py` | 插件现在能拿到 `self.client`（读取连接状态、拉起结果） |
| `plugins/ncqq_watchdog/` | 插件本体 |
| `plugins/ncqq_watchdog/ilink/` | 微信 iLink Bot 协议客户端（**纯标准库**，支持扫码登录/发文本/发图片） |
| `plugins/ncqq_watchdog/qr_text.py` | QR 编码器（把二维码链接画成 ASCII/PNG） |
| `plugins/ncqq_watchdog/qr_image.py` | PNG 解码 + 最近邻放大（把 NapCat 的 147×147 小图放大到能扫） |
| `plugins/ncqq_watchdog/notifier.py` | 微信通知通道（所有失败只记日志，不影响主流程） |
| `tests/test_watchdog.py` | 端到端测试（真起 WebSocket 服务端 + 真执行进程） |

---

## 三、启用步骤

### 1) 让运行机器人的用户能重启 ncqq（**需要 root 执行一次**）

> 下面的 `botuser` 是**占位符**，换成实际运行机器人的用户名（`whoami` 即是）。

> **前提**：`ncqqpy` 需要能以 root 重启 `ncqq`（见下）。
> 若 `ncqqpy.service` 已设 `User=root`，下面的 polkit/sudoers 授权可以跳过 ——
> 但仍建议保留规则，以便将来切回普通用户时不用再改一遍。

`ncqq.service` 是 root 的 systemd 服务。非 root 运行时直接
`systemctl restart ncqq` 会失败：

```
Failed to restart ncqq.service: Access denied as the requested operation
requires interactive authentication.
```

**方案 A（推荐）** — polkit 规则，只放行「该用户重启 ncqq」这一件事：

```bash
sudo tee /etc/polkit-1/rules.d/49-ncqq-watchdog.rules >/dev/null <<'EOF'
// 允许 botuser 重启/启动 ncqq 服务（ncqq_watchdog 需要）
polkit.addRule(function(action, subject) {
    var allowedUnits = ["ncqq.service", "ncqq"];
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        subject.user == "botuser" &&
        subject.local && subject.active &&
        allowedUnits.indexOf(action.lookup("unit")) >= 0 &&
        ["restart", "start"].indexOf(action.lookup("verb")) >= 0) {
        return polkit.Result.YES;
    }
});
EOF

# 生效验证（应输出 active）
systemctl restart ncqq && systemctl is-active ncqq
```

**方案 B** — sudoers（同理只放行这一条命令）：

```bash
echo 'botuser ALL=(root) NOPASSWD: /usr/bin/systemctl restart ncqq' \
  | sudo tee /etc/sudoers.d/ncqq-watchdog
sudo chmod 440 /etc/sudoers.d/ncqq-watchdog
```

然后把 `config.json` 改成：

```json
{ "restart_mode": "launcher", "restart_command": "sudo -n systemctl restart ncqq" }
```

> 本机 `/etc/sudo.conf` 的属主不是 root，sudo 当前直接拒绝运行
> （`sudo: no new privileges flag is set`），所以**方案 A 更稳**。

### 2) 扫码登录微信 iLink（**只做一次**）

```bash
cd /ncqqpy && ./venv-arm/bin/python -m plugins.ncqq_watchdog.login
```

终端会直接画出二维码（iLink 返回的是 H5 页面链接，不是图片，所以本地编码），
用微信扫码确认即可。凭据写到 `plugins/ncqq_watchdog/data/ilink_credentials.json`
（权限 600）。

### 3) 让 bot 拿到 `context_token`

iLink 协议要求主动推送必须带 `context_token`，而这个 token **只出现在入站消息里**，
所以：**用微信给这个 bot 发一条任意消息**。插件后台常驻长轮询，收到后会把它落盘缓存，
之后就能主动推送了。日志里会看到：

```
[watchdog] 收到微信消息（xxx@im.wechat），已缓存 context_token；如需收通知请在配置里把 target 设为 xxx@im.wechat
```

把日志里那个 id 填进配置即可（或用 `/watchdog target <id>`）：

```json
"plugins": { "ncqq_watchdog": { "wechat": { "target": "xxx@im.wechat" } } }
```

### 4) 重启 ncqqpy 让插件生效

> ⚠️ **框架改动（`napcat/`）必须重启进程才生效**。插件的热重载只重载 `plugins/` 下的
> 代码；正在运行的 `WsHandler` 还是旧代码，退避/稳定连接判定都不会更新。
> 判断依据：`journalctl -u ncqqpy` 里如果还是固定的「5.0s 后重连」，说明还没重启。

```bash
sudo systemctl restart ncqqpy     # 或 /reload（QQ 私聊里）
```

---

## 四、配置

### 顶层（框架行为，`config.json`）

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

### 插件（`plugins.ncqq_watchdog`）

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

## 五、命令

| 命令 | 说明 |
|------|------|
| `/watchdog status` | 连接状态、拉起次数与结果、微信通道状态、二维码情况 |
| `/watchdog test [内容]` | 发一条微信测试通知（验证通道） |
| `/watchdog qr [路径]` | 把二维码发到微信（手动补发） |
| `/watchdog target <id>` | 设置通知接收人 |

全部是 `scope="private"` + `permission="superuser"`：只能在私聊里、由超级用户触发。

---

## 六、测试

```bash
cd /ncqqpy && ./venv-arm/bin/python tests/test_watchdog.py
```

覆盖 128 项：连续失败只拉起一次、断开→重连的自愈与真下线拉起、从未连上不误报掉线、
拉起失败与冷却、二维码新旧判定、**抖动退避**（6 秒内重连次数被压住 + 恢复后清零）、
**抖动不发通知**（0.5s 抖动零通知 / 持续掉线只发一条 / 恢复报平安一条），
**每次拉起递一次二维码**（含「同一张码不重发」「换码后重发」「跨进程不重发」），
**重启后 20s 直接发**（延迟未到不发、到点即发、内容未变也发、同内容不重发、
换码后发新的、没有码时不误报）、**拉起前清空 cache**（含危险路径防护）、
**3 分钟内不二次拉起**、
**登录成功反馈**（含冷却）、**iLink 失败重试**（失败重试到成功/超时、配置缺失不空转），
以及两条端到端链路：插件级（掉线 → 恢复 → 二维码）与**框架级**
（真连接失败 → 真执行拉起脚本 → 脚本重写二维码 → 插件自动发到微信）。

> 测试用 `NCQQ_WATCHDOG_DATA_DIR` 把插件数据目录指到临时目录：
> 否则测试会把「已递指纹」写进 `plugins/ncqq_watchdog/data/`，
> 既污染生产状态，也会让后续测试因去重命中而假失败。

---

## 七、已知边界

- **微信通道依赖 iLink**：登录态过期（`errcode=-14`）后需要重新扫码；通知器会把这条错误写进日志与
  `/watchdog status`。
- **主动推送需要 `context_token`**：用户从没给 bot 发过消息时，掉线通知发不出去（日志会给出指引）。
  这是 iLink 协议的限制，不是实现问题。
- **发图片需要 `cryptography`**：venv 里没装时自动回退到系统的 `/usr/lib/python3/dist-packages`；
  都没有就只能发文字（会给出二维码文件路径）。
- **冷却期**：掉线 60 秒内的重复触发不会再次拉起，日志记「跳过拉起（冷却中）」。
