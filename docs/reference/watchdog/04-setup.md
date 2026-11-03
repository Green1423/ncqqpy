# 四、启用步骤

> 文档索引：[docs/README.md](../../README.md) · 上级：[看门狗（watchdog）](README.md) · 上一章：[涉及的文件](03-files.md) · 下一章：[配置](05-config.md)


## 4.1 让运行机器人的用户能重启 ncqq（**需要 root 执行一次**）

> 下面的 `botuser` 是**占位符**，换成实际运行机器人的用户名（`whoami` 即是）。

> **前提**：`ncqqpy` 需要能以 root 重启 `ncqq`（见下）。
> 若 `ncqqpy.service` 已设 `User=root`，下面的 polkit/sudoers 授权可以跳过 ——
> 但仍建议保留规则，以便将来切回普通用户时不用再改一遍。

`ncqq.service` 是 root 的 systemd 服务。非 root 运行时直接
`systemctl restart ncqq` 会失败：

```text
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

然后把 (../../../config.json) 改成：

```json
{ "restart_mode": "launcher", "restart_command": "sudo -n systemctl restart ncqq" }
```

> 本机 `/etc/sudo.conf` 的属主不是 root，sudo 当前直接拒绝运行
> （`sudo: no new privileges flag is set`），所以**方案 A 更稳**。

## 4.2 扫码登录微信 iLink（**只做一次**）

```bash
cd /ncqqpy && ./venv-arm/bin/python -m napcat.ilink_login
```

终端会直接画出二维码（iLink 返回的是 H5 页面链接，不是图片，所以本地编码），
用微信扫码确认即可。凭据写到 `napcat/data/ilink_credentials.json`（权限 600）。

## 4.3 让 bot 拿到 `context_token`

iLink 协议要求主动推送必须带 `context_token`，而这个 token **只出现在入站消息里**，
所以：**用微信给这个 bot 发一条任意消息**。看门狗后台常驻长轮询，收到后会把它落盘缓存，
之后就能主动推送了。日志里会看到：

```text
[watchdog] 收到微信消息（xxx@im.wechat），已缓存 context_token；如需收通知请在配置里把 target 设为 xxx@im.wechat
```

把日志里那个 id 填进配置即可（或用 `/watchdog target <id>`）：

```json
"watchdog": { "wechat": { "target": "xxx@im.wechat" } }
```

## 4.4 重启 ncqqpy 让改动生效

> ⚠️ **框架改动（`napcat/`）必须重启进程才生效**。插件的热重载只重载 `plugins/` 下的
> 代码；正在运行的 `WsHandler` 还是旧代码，退避/稳定连接判定都不会更新。
> 判断依据：`journalctl -u ncqqpy` 里如果还是固定的「5.0s 后重连」，说明还没重启。

```bash
sudo systemctl restart ncqqpy     # 或 /reload（QQ 私聊里）
```

---
