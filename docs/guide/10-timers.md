# 十、定时事件

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[文件管理](09-files.md) · 下一章：[高级用法](11-advanced.md)


## 10.1 周期任务

```python
# 每 N 秒执行一次
@client.timer.every(60, name="save_cache")
async def save():
    print("每分钟保存缓存")

@client.timer.every(3600)  # 自动以函数名作为任务名
async def hourly_report():
    print("每小时报告")
```

## 10.2 每日定时

```python
# 每天指定时间
@client.timer.at("08:00", name="morning")
async def morning():
    print("早上好")

@client.timer.at("23:59")
async def daily_summary():
    print("每日总结")
```

## 10.3 延迟执行

```python
@client.timer.delay(5, name="delayed_start")
async def init():
    print("5秒后初始化")
```

## 10.4 定时器管理

```python
# 取消定时任务
client.timer.cancel("save_cache")

# 全局启停
client.timer.stop()
client.timer.start()
```

---
