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

@self.schedule(3600)  # 无 name 时用函数名
async def hourly_check(): ...
```

排程语义（框架保证，插件不用自己补）：

- **周期不累积偏差**：按绝对时刻排程，任务自身的执行耗时**不计入**周期
  （旧实现「跑完再睡」会每轮多等一个任务耗时：实测 40ms 任务 + 100ms 间隔
  → 141ms/轮，一天能偏出几十分钟）；
- **落后不补跑**：任务比间隔还久、或进程被挂起过，框架会跳过错过的轮次、
  对齐到下一个未来时刻，不会瞬间连发一堆；
- **间隔必须 > 0**：`schedule(0)` 在注册时直接 `ValueError`（否则就是忙等）；
- **每日定时**按本机本地时区，月末/闰年正确进位（`schedule_at("08:00")`
  在 1 月 31 日会排到 2 月 1 日，不会因为 `day=32` 崩）；
- `pause_timer` / `resume_timer`（以及运维的 `/pause timer <名>`）可暂停再恢复，
  恢复后从当前时刻重新起算。

---
