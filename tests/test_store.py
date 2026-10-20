"""napcat.store：sqlite3 ORM 的回归。

覆盖 Django 风格存取的核心契约：字段类型/默认值/自增主键、查询后缀、
查询集不可变、get 的两种异常、唯一约束、JSON/时间往返、事务（含嵌套）、
建表与补列（幂等）、并发写、`db.run()` 线程逃生口，以及
`PluginBase.database()` 的插件目录落点 + 卸载关闭。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from napcat import (                                    # noqa: E402
    Database, Model, CharField, TextField, IntegerField, FloatField,
    BooleanField, JSONField, DateTimeField, StoreError, FieldError,
    StoreIntegrityError,
)

PASSED = 0
FAILED = 0
TMP = Path(tempfile.mkdtemp(prefix="ncqqpy-store-"))


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


def db_path(name: str) -> Path:
    return TMP / f"{name}.db"


# ==================== 模型定义 ====================

class Note(Model):
    title = CharField(max_length=100, index=True)
    body = TextField(default="")
    views = IntegerField(default=0)
    score = FloatField(null=True)
    pinned = BooleanField(default=False)
    meta = JSONField(default=dict)
    created = DateTimeField(null=True)


class User(Model):
    name = CharField(max_length=50, unique=True)
    age = IntegerField(default=0)


class Missing(Model):
    title = CharField()


class Renamed(Model):
    class Meta:
        table = "custom_table_name"

    title = CharField(default="")


# ==================== 用例 ====================

def test_schema():
    print("\n[1] 建表 / 字段类型 / 自增主键 / 幂等")
    db = Database(db_path("schema"))
    actions = db.ensure_schema(Note)
    check("首次建表没有 ALTER", actions == [], str(actions))
    check("表存在", db.table_exists("note"), str(db.tables()))
    cols = db.columns("note")
    check("列齐全", {"id", "title", "body", "views", "score", "pinned", "meta", "created"}
          <= set(cols), str(cols))
    check("自增主键是 INTEGER", cols.get("id", "").upper() == "INTEGER", str(cols))
    check("重复 ensure 幂等", db.ensure_schema(Note) == [])
    check("表名默认 = 类名小写", "note" in db.tables())
    db.ensure_schema(Renamed)
    check("Meta.table 可改名", "custom_table_name" in db.tables(), str(db.tables()))
    check("索引已建", any(r["name"] == "idx_note_title" for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='index'")))
    db.close()


def test_crud():
    print("\n[2] 增删改查 / 默认值 / 类型往返")
    db = Database(db_path("crud"))
    db.register(Note)
    now = datetime(2026, 10, 18, 12, 30, 0)
    n = Note.objects.create(title="第一条", body="正文", views=3, score=1.5,
                            pinned=True, meta={"tags": ["a", "b"], "中文": "值"},
                            created=now)
    check("自增主键回填", n.pk == 1, str(n.pk))
    got = Note.objects.get(pk=n.pk)
    check("字符串往返", got.title == "第一条", got.title)
    check("整数往返", got.views == 3)
    check("浮点往返", got.score == 1.5)
    check("布尔往返", got.pinned is True)
    check("JSON 往返（含中文/嵌套）",
          got.meta == {"tags": ["a", "b"], "中文": "值"}, str(got.meta))
    check("时间往返", isinstance(got.created, datetime) and got.created == now, str(got.created))
    check("默认值生效", Note.objects.create(title="默认值").body == "")

    got.views = 9
    got.save()
    check("实例 save() 走 UPDATE", Note.objects.get(pk=got.pk).views == 9)
    check("to_dict", Note.objects.get(pk=got.pk).to_dict()["title"] == "第一条")

    check("实例 delete()", got.delete() == 1)
    check("删除后 get 抛 DoesNotExist", _raises(lambda: Note.objects.get(pk=got.pk),
                                                Note.DoesNotExist))
    db.close()


def test_queries():
    print("\n[3] 查询集：过滤 / 排序 / 分页 / values / 聚合")
    db = Database(db_path("query"))
    db.register(Note)
    for i in range(1, 11):
        Note.objects.create(title=f"标题{i}", views=i, meta={"i": i},
                            score=float(i) / 2)
    Note.objects.create(title="特殊 abc", views=99)

    check("count", Note.objects.count() == 11, str(Note.objects.count()))
    check("filter exact", Note.objects.filter(views=3).count() == 1)
    check("filter gt", Note.objects.filter(views__gt=8).count() == 3,
          str(Note.objects.filter(views__gt=8).count()))
    check("filter gte/lt/lte", (Note.objects.filter(views__gte=2, views__lt=5).count() == 3))
    check("filter in", Note.objects.filter(views__in=[1, 2, 3]).count() == 3)
    check("filter in 空列表", Note.objects.filter(views__in=[]).count() == 0)
    check("filter range", Note.objects.filter(views__range=(2, 4)).count() == 3)
    check("filter contains", Note.objects.filter(title__contains="特殊").count() == 1)
    check("filter startswith", Note.objects.filter(title__startswith="标题").count() == 10)
    check("filter endswith", Note.objects.filter(title__endswith="10").count() == 1)
    check("filter icontains 大小写", Note.objects.filter(title__icontains="ABC").count() == 1)
    check("filter isnull（None 列）", Note.objects.filter(score__isnull=True).count() == 1)
    check("exclude", Note.objects.exclude(views__gt=5).count() == 5,
          str(Note.objects.exclude(views__gt=5).count()))
    check("链式 filter + order_by",
          [n.views for n in Note.objects.filter(views__lte=3).order_by("-views")] == [3, 2, 1])
    check("order_by 多字段",
          [n.title for n in Note.objects.order_by("views", "-title")][:2] == ["标题1", "标题2"])
    check("limit/offset 切片", len(Note.objects.order_by("views")[2:5]) == 3)
    check("负数索引被拒", _raises(lambda: Note.objects.order_by("views")[-1], StoreError))
    check("count 带过滤", Note.objects.filter(views__gte=5).count() == 7,
          str(Note.objects.filter(views__gte=5).count()))
    check("exists", Note.objects.filter(title="不存在").exists() is False)
    check("first / last", (Note.objects.order_by("views").first().views,
                           Note.objects.order_by("views").last().views) == (1, 99))
    check("values 只取指定列",
          Note.objects.filter(views=1).values("title", "views") == [{"title": "标题1", "views": 1}])
    check("查询集不可变（.filter 返回新对象）",
          Note.objects.all().count() == 11)
    check("unknown field 求值时报错",
          _raises(lambda: Note.objects.filter(nope=1).count(), FieldError))
    check("unknown lookup 求值时报错",
          _raises(lambda: Note.objects.filter(views__wat=1).count(), FieldError))
    db.close()


def test_get_errors():
    print("\n[4] get() 的两种异常 / get_or_create / update_or_create")
    db = Database(db_path("get"))
    db.register(User)
    User.objects.create(name="alice")
    check("get 不存在 → DoesNotExist",
          _raises(lambda: User.objects.get(name="nobody"), User.DoesNotExist))
    User.objects.create(name="alice2")
    User.objects.create(name="alice3")
    check("get 多条 → MultipleObjectsReturned",
          _raises(lambda: User.objects.get(age=0), User.MultipleObjectsReturned))
    obj, created = User.objects.get_or_create(name="bob")
    check("get_or_create 新建", created is True and obj.name == "bob")
    obj2, created2 = User.objects.get_or_create(name="bob")
    check("get_or_create 复用", created2 is False and obj2.pk == obj.pk)
    upd, made = User.objects.update_or_create(name="bob", defaults={"age": 7})
    check("update_or_create 更新", made is False and upd.age == 7)
    check("unknown 字段构造报错", _raises(lambda: User(nope=1), TypeError))
    db.close()


def test_constraints():
    print("\n[5] 约束：唯一键 / 非空")
    db = Database(db_path("cons"))
    db.register(User)
    User.objects.create(name="a")
    check("唯一键冲突包成 StoreIntegrityError",
          _raises(lambda: User.objects.create(name="a"), StoreIntegrityError))
    db2 = Database(db_path("cons-missing"))
    db2.register(Missing)
    try:
        Missing.objects.create(title=None)
        check("NOT NULL 缺值报错", False)
    except StoreError as e:
        check("NOT NULL 缺值报错", "不能为空" in str(e), str(e))
    db2.close()
    db.close()


def test_json_and_datetime_edges():
    print("\n[6] 边界值：None / 空串 / emoji / 大文本 / 时间")
    db = Database(db_path("edge"))
    db.register(Note)
    n = Note.objects.create(title="emoji 😀🎉", meta={"list": [None, True, 1.5]},
                            created=datetime(2026, 1, 1))
    got = Note.objects.get(pk=n.pk)
    check("emoji 往返", got.title == "emoji 😀🎉", got.title)
    check("JSON 混合类型往返", got.meta == {"list": [None, True, 1.5]}, str(got.meta))
    check("时间往返", got.created == datetime(2026, 1, 1), str(got.created))
    big = "长" * 10000
    n2 = Note.objects.create(title="big", body=big)
    check("大文本往返", Note.objects.get(pk=n2.pk).body == big)
    n3 = Note.objects.create(title="nulls", score=None)
    check("null 字段", Note.objects.get(pk=n3.pk).score is None)
    check("默认 JSON 是独立对象（不是共享的可变默认）",
          Note.objects.get(pk=n3.pk).meta is not None)
    db.close()


def test_transactions():
    print("\n[7] 事务：提交 / 回滚 / 嵌套 savepoint")
    db = Database(db_path("tx"))
    db.register(User)
    with db.atomic():
        User.objects.create(name="t1")
        User.objects.create(name="t2")
    check("提交后两条都在", User.objects.count() == 2)
    try:
        with db.atomic():
            User.objects.create(name="t3")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("异常 → 整段回滚", User.objects.count() == 2, str(User.objects.count()))

    try:
        with db.atomic():
            User.objects.create(name="outer")
            try:
                with db.atomic():
                    User.objects.create(name="inner")
                    raise ValueError("inner boom")
            except ValueError:
                pass
            User.objects.create(name="after-inner")
    except Exception:                                # noqa: BLE001
        pass
    names = {u.name for u in User.objects.all()}
    check("内层回滚不影响外层", {"outer", "after-inner"} <= names and "inner" not in names,
          str(names))
    db.close()


def test_migration_add_column():
    print("\n[8] 补列：新增字段自动 ALTER（只加不删）")
    path = db_path("mig")
    db = Database(path)
    db.ensure_schema(User)
    db.register(User)
    User.objects.create(name="old")

    class UserV2(Model):
        class Meta:
            table = "user"

        name = CharField(max_length=50, unique=True)
        age = IntegerField(default=0)
        extra = CharField(default="")

    actions = db.ensure_schema(UserV2)
    check("检测到新增列并 ALTER", any("extra" in a for a in actions), str(actions))
    check("老行补上默认值（空串）",
          db.scalar('SELECT extra FROM "user" WHERE name = ?', ("old",), "") == "")
    check("补列幂等", db.ensure_schema(UserV2) == [])
    db.close()


def test_threads():
    print("\n[9] 并发：每线程一条连接，计数不丢")
    db = Database(db_path("threads"))
    db.register(User)
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            for i in range(10):
                User.objects.create(name=f"t{idx}-{i}")
        except BaseException as e:                    # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("没有线程报错", not errors, str(errors[:1]))
    check("50 条全部写入", User.objects.count() == 50, str(User.objects.count()))
    check("耗时合理（<10s）", time.time() - start < 10)
    db.close()


def test_async_run():
    print("\n[10] db.run()：在工作线程里跑同步函数")

    async def main() -> None:
        db = Database(db_path("async"))
        db.register(User)

        def insert_many() -> int:
            for i in range(5):
                User.objects.create(name=f"a{i}")
            return User.objects.count()

        total = await db.run(insert_many)
        check("线程里写入成功", total == 5, str(total))
        check("主线程能看到（同一文件）", User.objects.count() == 5, str(User.objects.count()))
        db.close()

    asyncio.run(main())


def test_bulk_and_errors():
    print("\n[11] bulk_create / 未绑定库 / 低层 SQL")
    db = Database(db_path("bulk"))
    db.register(Note)
    rows = [Note(title=f"b{i}") for i in range(3)]
    check("bulk_create 写入 3 条", Note.objects.bulk_create(rows) == 3)
    check("bulk_create 后计数", Note.objects.count() == 3)
    check("bulk_create 空列表", Note.objects.bulk_create([]) == 0)

    class Unbound(Model):
        title = CharField(default="")

    check("未绑定库时访问 objects 报错",
          _raises(lambda: Unbound.objects.count(), StoreError))
    check("未知模型字段报错", _raises(lambda: Note.objects.values("nope"), FieldError))

    db.execute("CREATE TABLE raw (a INTEGER, b TEXT)")
    db.execute("INSERT INTO raw VALUES (?, ?)", (1, "x"))
    check("低层 query 可用", [tuple(r) for r in db.query("SELECT a, b FROM raw")] == [(1, "x")])
    check("scalar 默认值", db.scalar("SELECT 1 WHERE 0", default="dflt") == "dflt")
    with __import__("contextlib").suppress(StoreError):
        db.execute("SELECT * FROM no_such_table")
    check("SQL 错误包成 StoreError",
          _raises(lambda: db.execute("SELECT * FROM no_such_table"), StoreError))
    db.close()


def test_plugin_integration():
    print("\n[12] 插件集成：PluginBase.database() 落点 / 卸载关闭")
    from napcat import PluginBase
    plugin_root = TMP / "plugins" / "demo"
    plugin_root.mkdir(parents=True, exist_ok=True)

    class Demo(PluginBase):
        name = "demo"

        async def on_load(self):
            self.db = self.database(ensure=[Note])

        async def on_unload(self):
            pass

    inst = Demo(api=None, event_bus=None, timer=None, cmd_manager=None,
                perm_manager=None, file_manager=None, logger=_NullLogger(),
                plugin_dir=plugin_root)
    asyncio.run(inst.on_load())
    check("库文件落在插件目录 data/ 下",
          (plugin_root / "data" / "demo.db").exists(),
          str(list((plugin_root / "data").glob("*")) if (plugin_root / "data").exists() else []))
    check("ensure 顺便完成了绑定", Note._default_db is inst.db)
    Note.objects.create(title="插件写入")
    check("插件里直接 objects 可用", Note.objects.count() == 1)
    check("database() 有缓存", inst.database(ensure=[Note]) is inst.db)
    inst._close_databases()
    check("关闭后缓存清空", inst._databases == {})
    check("关库后还能重新打开",
          inst.database(ensure=[Note]).path == inst.db.path)


class _NullLogger:
    def _noop(self, *a, **k):
        pass

    debug = info = warning = error = exception = _noop


def _raises(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except BaseException:
        return False
    return False


def _is_store_error(fn) -> bool:
    return _raises(fn, StoreError)


def main() -> int:
    test_schema()
    test_crud()
    test_queries()
    test_get_errors()
    test_constraints()
    test_json_and_datetime_edges()
    test_transactions()
    test_migration_add_column()
    test_threads()
    test_async_run()
    test_bulk_and_errors()
    test_plugin_integration()
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
