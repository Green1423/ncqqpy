# 数据持久化：`napcat.store`（sqlite3 ORM）

插件要存点东西时，默认选择是 **`napcat.store`** —— 一个 Django 风格的极简 ORM，
底层是标准库 `sqlite3`，数据库文件就放在插件自己的目录里。

## 为什么不是 JSON 文件

框架早期插件都用手写 JSON + `threading.Lock`：读改写要自己保证原子性、查询靠
内存过滤、并发写一不小心就丢数据。SQLite 是单文件、自带事务、有索引，缺的只是
一层映射 —— 这就是 `napcat.store`。别为「就存几条配置」上它，**有查询/并发/增长
预期的数据**才值得。

## 最小例子

```python
# store.py —— 数据模型
from napcat.store import Model, CharField, IntegerField, JSONField
class Note(Model):
    title = CharField(max_length=200, index=True)
    body = CharField(default="")
    views = IntegerField(default=0)
    meta = JSONField(default=dict)


# plugin.py —— 注册处只有装配（逻辑放 commands/，见 plugin-dev §14）
class MyPlugin(PluginBase):
    name = "demo"

    async def on_load(self):
        self.database(ensure=[Note])      # 建表（幂等）+ 绑定

    async def on_enable(self):
        from .commands import notes
        self.register_module(notes)


# commands/notes.py —— 逻辑
from napcat.registry import command
from ..store import Note

@command("demo.count")
async def count(plugin, packet, event):
    Note.objects.create(title="第一条", meta={"tags": ["a"]})
    return f"共 {Note.objects.count()} 条"
```

数据库文件默认落在 **`plugins/<插件名>/data/<插件名>.db`**（`data/` 已被发布
脚本排除，不会进仓库）。想换位置/多开一个库：

```python
self.database(path=Path("/var/lib/demo.db"))      # 指定路径
self.database("cache", ensure=[Cache])            # plugins/<插件>/data/cache.db
```

框架在**卸载 / 热重载 / 加载失败**时会自动 `close()` 这些库，插件不用自己关。

## 字段

| 字段 | SQLite 类型 | 说明 |
|------|------------|------|
| `AutoField` | INTEGER | 主键自增。**没声明主键时框架自动加一个 `id`** |
| `IntegerField` | INTEGER | |
| `FloatField` | REAL | |
| `BooleanField` | INTEGER | 存 0/1，读出 `bool` |
| `CharField(max_length=…)` | TEXT | `max_length` 只在写入时校验（SQLite 不强制） |
| `TextField` | TEXT | 不限长的文本 |
| `JSONField` | TEXT | 任意 JSON 结构（dict/list/标量），默认 `dict` |
| `DateTimeField` | TEXT | ISO 8601 文本，可按字符串排序/比较（时区要一致） |

公共参数：`primary_key`、`null`、`unique`、`index`、`default`（可调用）、
`db_column`、`verbose_name`。

```python
class User(Model):
    class Meta:
        table = "users"        # 不写就默认是类名小写

    name = CharField(max_length=50, unique=True)
    age = IntegerField(default=0, index=True)
```

## 存取

```python
Note.objects.create(title="hi", views=1)       # INSERT，回填自增 pk
note = Note.objects.get(pk=1)                  # 没有 → Note.DoesNotExist
note.views += 1
note.save()                                    # 已存在 → UPDATE
note.delete()                                  # 按主键删

Note.objects.filter(views__gt=10).order_by("-views")[0:5]
Note.objects.exclude(pinned=True).count()
Note.objects.first() / .last() / .exists()
Note.objects.values("title", "views")          # [{"title":…, "views":…}]
Note.objects.get_or_create(name="a", defaults={"age": 1})
Note.objects.update_or_create(name="a", defaults={"age": 2})
Note.objects.bulk_create([Note(title=f"n{i}") for i in range(100)])
```

查询后缀：`exact`（默认）、`iexact`、`gt`/`gte`/`lt`/`lte`、`in`、`range`、
`contains`/`icontains`、`startswith`/`istartswith`、`endswith`/`iendswith`、
`isnull`。`pk` 是主键的别名。

查询集是**惰性**的：`filter(...)` 只记条件，`.count()` / 迭代 / 切片时才发 SQL。
字段名写错也在求值时抛 `FieldError`（不会静默返回空）。

## 事务

```python
with self.database().atomic():
    Note.objects.create(title="a")
    Note.objects.create(title="b")      # 任一句抛异常 → 整段回滚
```

可以嵌套（内层用 `SAVEPOINT`，内层失败不影响外层已成功的部分）。

## 建表与「迁移」

`ensure_schema()` 是**幂等**的：`CREATE TABLE IF NOT EXISTS` + 缺失列
`ALTER TABLE ADD COLUMN`，返回本次实际做的动作（适合写日志）。

```python
actions = self.database().ensure_schema(Note)   # ["note.meta 已新增"]
```

⚠️ **只加不删**：模型里删掉/改类型的列不会被自动处理（改数据是不可逆操作）。
真要改结构，自己写 SQL 处理数据，再改模型。

## 不阻塞事件循环

API 是同步的（和 Django ORM 一样）：单次读写是毫秒级，没必要为它把 `await`
传染到整个插件。整批任务（比如导入几千条）放到工作线程：

```python
def _import() -> int:
    for row in rows:
        Note.objects.create(**row)
    return Note.objects.count()

total = await self.database().run(_import)      # asyncio.to_thread
```

⚠️ 线程里有**新连接**，事务不跨线程 —— 需要原子性就在 `_import` 内部用
`with db.atomic():`。

## 底层逃生口

```python
db.execute("UPDATE note SET views = views + 1 WHERE id = ?", (1,))
db.query("SELECT COUNT(*) FROM note")           # list[sqlite3.Row]
db.scalar("SELECT MAX(id) FROM note", default=0)
db.tables() / db.columns("note") / db.table_exists("note")
```

## 边界（v1 刻意不做）

- **没有关系（ForeignKey）/联表**：单表 + `JSONField` 够用；需要树形结构就塞
  JSON。真需要关系再设计，别先造半成品。
- **没有 migration 版本号**：`ensure_schema` 只做加列，历史结构由你自己保证。
- **不做连接池/多线程共享连接**：sqlite3 连接不能跨线程，本模块按线程各开一条
  （WAL + busy_timeout）。
- **`JSONField` 的可比较性有限**：`contains` 是**字符串**包含，不是结构匹配；
  要按 JSON 内部字段查，请把它拍平成普通列。

## 测试

```bash
./venv/bin/python -B tests/test_store.py    # 83 项
```

覆盖：建表/补列幂等、字段类型往返（含 JSON/时间/emoji/大文本）、全部查询后缀、
`get()` 两种异常、唯一键与非空约束、事务提交/回滚/嵌套、多线程并发写、
`db.run()`、`bulk_create`、低层 SQL，以及 `PluginBase.database()` 的落点与卸载关闭。
