# 十、定时任务

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[数据持久化（`napcat.store`）](09-store.md) · 下一章：[文件管理](11-files.md)


```python
@self.schedule(60, name="save")          # 每60秒
async def periodic():
    self.logger.info("定时任务执行")

@self.schedule_at("08:00", name="morning")  # 每天8点
async def daily():
    await self.api.send_private_msg(
        user_id="123456",
        message=MessageChain().text("早安"),
    )

@self.schedule(3600, description="每小时巡检")  # 无 name 时用函数名
async def hourly_check(): ...
```

注册只有这一个入口：`self.schedule` / `self.schedule_at`。它们比
`self.timer.every()` 多做一件事 —— **记下任务归属**，`on_disable`、卸载、
加载失败时框架会自动取消（直接用 `self.timer.every()` 注册的任务框架不知道
属于谁，插件走了任务还在跑）。

排程语义（框架保证，插件不用自己补）：

- **周期不累积偏差**：按绝对时刻排程，任务自身的执行耗时**不计入**周期；
- **落后不补跑**：任务比间隔还久、或进程被挂起过，框架会跳过错过的轮次、
  对齐到下一个未来时刻，不会瞬间连发一堆；
- **间隔必须 > 0**：`schedule(0)` 在注册时直接 `ValueError`（否则就是忙等）；
- **每日定时**按本机本地时区，月末/闰年正确进位（`schedule_at("08:00")`
  在 1 月 31 日会排到 2 月 1 日，不会因为 `day=32` 崩）；
- **管理**（暂停 / 恢复 / 取消）用 `self.timer`：
  `self.timer.pause_timer("save")`、`self.timer.resume_timer("save")`、
  `self.timer.cancel("save")`；暂停后恢复从当前时刻重新起算。
  运维侧的 `/pause timer <名>`、`/stop timer <名>` 是同一份能力的命令入口。

---
