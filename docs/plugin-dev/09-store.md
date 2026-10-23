# 九、数据持久化（`napcat.store`）

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[消息构造](08-messages.md) · 下一章：[定时任务](10-timers.md)


要存的结构化数据（有查询、并发或增长预期）别再手写 JSON 文件：

```python
from napcat.store import Model, CharField, IntegerField, JSONField
class Note(Model):
    title = CharField(max_length=200, index=True)
    body = CharField(default="")
    meta = JSONField(default=dict)

async def on_load(self):
    self.database(ensure=[Note])              # 建表 + 绑定，库在 data/<插件名>.db
    Note.objects.create(title="第一条")
```

Django 风格的 `objects.create/get/filter/order_by/count/exists/values`、
`field__gt/gte/lt/lte/in/range/contains/isnull` 查询后缀、`with db.atomic():`
事务（可嵌套）、`ensure_schema()` 幂等补列、`await db.run(fn)` 线程逃生口。
卸载/热重载时框架自动关闭连接。完整参考见 (../store.md)。
