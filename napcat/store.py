"""napcat.store —— 基于 sqlite3 的轻量 ORM（Django 风格）。

插件原来的持久化各写各的：JSON 文件 + `threading.Lock`，读改写要自己保证原子性，
查询靠内存过滤。SQLite 是标准库自带的、单文件、有事务、够快 —— 缺的只是一层
「模型 / 字段 / 查询集」的映射，这就是本模块。

## 为什么是同步 API

sqlite3 是同步的，而且每次读写都是毫秒级。把它包成 async 只会把线程池的复杂度
搬给调用方（`await` 到处传），换来的是「事件循环不被几毫秒的 IO 阻塞」这种
在 QQ 机器人场景里感知不到的收益。所以和 Django ORM 一样是**同步**的：

    db = self.database(ensure=[Note])      # 建表 + 绑定（见 PluginBase.database）
    Note.objects.create(title="hi", body="...")
    Note.objects.filter(title__contains="hi").order_by("-id").first()

真有批量任务要避免阻塞事件循环，用 `await db.run(fn, ...)`（内部 `asyncio.to_thread`）。

## 边界（v1 刻意不做的事）

- **没有 migration 框架**：`ensure_schema()` = `CREATE TABLE IF NOT EXISTS` +
  「缺失列 `ALTER TABLE ADD COLUMN`」（只加不删）。改类型/删列请自己处理数据。
- **没有关系（ForeignKey）/联表**：单表 + `JSONField` 覆盖插件的绝大多数需求；
  造半成品的关系层比不做更糟。
- **连接是线程本地的**：sqlite3 连接不能跨线程共享；每线程一条连接，
  WAL + busy_timeout，写并发交给 SQLite 自己的锁。`close()` 只关当前线程的。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


# ==================== 异常 ====================

class StoreError(Exception):
    """存储层所有错误的基类。"""


class DoesNotExist(StoreError):
    """`get()` 没找到。每个模型有自己的一层子类（`User.DoesNotExist`）。"""


class MultipleObjectsReturned(StoreError):
    """`get()` 找到多于一条。"""


class IntegrityError(StoreError):
    """违反约束（唯一键冲突 / NOT NULL 等）。原始异常在 `__cause__`。"""


class FieldError(StoreError):
    """字段名写错 / 字段值无法转换。"""


class _NotProvided:
    def __repr__(self) -> str:                     # pragma: no cover - 调试用
        return "NOT_PROVIDED"


NOT_PROVIDED = _NotProvided()


# ==================== 字段 ====================

class Field:
    """一个列。子类只需要声明 `sql_type` 和（按需）覆写 `to_db` / `to_python`。"""

    sql_type = "TEXT"

    def __init__(self, *, primary_key: bool = False, null: bool = False,
                 unique: bool = False, index: bool = False,
                 default: Any = NOT_PROVIDED, db_column: str = "",
                 verbose_name: str = "") -> None:
        self.primary_key = bool(primary_key)
        self.null = bool(null)
        self.unique = bool(unique)
        self.index = bool(index)
        self.default = default
        self.db_column = db_column
        self.verbose_name = verbose_name
        self.name = ""                      # 由元类回填

    # ---- 值转换 ----

    def to_db(self, value: Any) -> Any:
        return value

    def to_python(self, value: Any) -> Any:
        return value

    def get_default(self) -> Any:
        """默认值可能是可调用的（`default=dict` / `default=time.time`）。"""
        if self.default is NOT_PROVIDED:
            return NOT_PROVIDED
        return self.default() if callable(self.default) else self.default

    # ---- DDL ----

    @property
    def column(self) -> str:
        return self.db_column or self.name

    def column_sql(self, *, for_alter: bool = False) -> str:
        parts = [f'"{self.column}"', self.sql_type]
        if self.primary_key:
            parts.append("PRIMARY KEY")
        if isinstance(self, AutoField):
            parts.append("AUTOINCREMENT")
        # ALTER TABLE ADD COLUMN 不允许「NOT NULL 且无默认值」—— 那种列只可能是
        # 模型里后来才加的，老行没法补值，只能按可空加进去（否则整条 DDL 报错）。
        required = (not self.null and not self.primary_key
                    and not (for_alter and self.default is NOT_PROVIDED))
        if required:
            parts.append("NOT NULL")
        if self.unique and not self.primary_key:
            parts.append("UNIQUE")
        if for_alter and self.default is not NOT_PROVIDED and not callable(self.default):
            parts.append(f"DEFAULT {_sql_literal(self.default)}")
        return " ".join(parts)


class AutoField(Field):
    """自增主键字段（`INTEGER PRIMARY KEY AUTOINCREMENT`）。"""
    sql_type = "INTEGER"

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("primary_key", True)
        super().__init__(**kwargs)


class IntegerField(Field):
    """整数字段。"""
    sql_type = "INTEGER"

    def to_db(self, value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return None if value is None else int(value)
        try:
            return int(value)
        except (TypeError, ValueError):
            raise FieldError(f"字段 {self.name} 需要整数，收到 {value!r}") from None

    def to_python(self, value: Any) -> Any:
        return None if value is None else int(value)


class FloatField(Field):
    """浮点字段。"""
    sql_type = "REAL"

    def to_db(self, value: Any) -> Any:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            raise FieldError(f"字段 {self.name} 需要数字，收到 {value!r}") from None

    def to_python(self, value: Any) -> Any:
        return None if value is None else float(value)


class BooleanField(Field):
    """布尔字段（落库为 0/1）。"""
    sql_type = "INTEGER"

    def to_db(self, value: Any) -> Any:
        return None if value is None else int(bool(value))

    def to_python(self, value: Any) -> Any:
        return None if value is None else bool(value)


class CharField(Field):
    """变长字符串。`max_length` 只是**校验**（SQLite 不强制长度）。"""

    sql_type = "TEXT"

    def __init__(self, *, max_length: Optional[int] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.max_length = max_length

    def to_db(self, value: Any) -> Any:
        if value is None:
            return None
        text = str(value)
        if self.max_length and len(text) > self.max_length:
            raise FieldError(f"字段 {self.name} 超长（{len(text)} > {self.max_length}）")
        return text


class TextField(CharField):
    """长文本字段（无长度上限）。"""
    def __init__(self, **kwargs: Any) -> None:
        kwargs.pop("max_length", None)
        super().__init__(max_length=None, **kwargs)


class JSONField(Field):
    """任意 JSON 可序列化结构（dict / list / 标量）。默认存成 UTF-8 JSON 文本。

    ⚠️ 可比较性有限：`filter(meta__contains=...)` 只是**字符串**包含，不是结构
    匹配。要按 JSON 内部字段查询，请把它拍平成普通列。
    """

    sql_type = "TEXT"

    def to_db(self, value: Any) -> Any:
        if value is None:
            return None
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as e:
            raise FieldError(f"字段 {self.name} 不能序列化成 JSON: {e}") from None

    def to_python(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list, int, float, bool)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value


class DateTimeField(Field):
    """时间点。存 **ISO 8601 文本**：可读、可按字符串排序/比较（前提是时区一致）。

    接受 `datetime` / ISO 字符串 / `date`；读出时还原成 `datetime`。
    """

    sql_type = "TEXT"

    def to_db(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day).isoformat()
        if isinstance(value, str):
            return value
        raise FieldError(f"字段 {self.name} 需要 datetime，收到 {value!r}")

    def to_python(self, value: Any) -> Any:
        if value is None or isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return value


# ==================== SQL 小工具 ====================

def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _sql_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: 支持的查询后缀：`字段__后缀=值`（不写后缀 = exact）
_LOOKUPS = {
    "exact", "iexact", "gt", "gte", "lt", "lte", "in", "range",
    "contains", "icontains", "startswith", "istartswith",
    "endswith", "iendswith", "isnull",
}


# ==================== 数据库 ====================

class Database:
    """一个 sqlite 文件 + 线程本地连接 + 事务。

    用法：

        db = Database("plugins/demo/data/demo.db")
        db.ensure_schema(Note)
        Note.bind(db)
        with db.atomic():
            Note.objects.create(title="a")
            Note.objects.create(title="b")        # 任一条失败 → 整段回滚
    """

    def __init__(self, path: "str | Path", *, timeout: float = 5.0,
                 wal: bool = True) -> None:
        self.path = Path(path)
        self.timeout = float(timeout)
        self.wal = bool(wal)
        self._local = threading.local()
        self._lock = threading.Lock()       # 只保护计数/路径创建这类小状态
        self._schema_done: set[str] = set()

    # ---- 连接 ----

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=self.timeout,
                               isolation_level=None)   # 显式事务：见 atomic()
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        if self.wal and str(self.path) != ":memory:":
            # WAL：读不阻塞写，多进程/多线程读同一个文件时明显更顺。
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        self._local.conn = conn
        self._local.depth = 0
        return conn

    def close(self) -> None:
        """关闭**当前线程**的连接（别的线程的连接随线程结束回收）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None
                self._local.depth = 0
        with self._lock:
            self._schema_done.clear()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:                     # pragma: no cover - 调试用
        return f"<Database {self.path}>"

    # ---- 底层执行 ----

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """执行写语句。

        Args:
            sql: SQL 文本（`?` 占位）。
            params: 占位参数。

        Returns:
            `sqlite3.Cursor`；`lastrowid` / `rowcount` 可继续使用。"""
        try:
            return self.conn.execute(sql, tuple(params))
        except sqlite3.IntegrityError as e:
            raise IntegrityError(str(e)) from e
        except sqlite3.Error as e:
            raise StoreError(f"{e}（SQL: {sql[:120]}）") from e

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> sqlite3.Cursor:
        try:
            return self.conn.executemany(sql, [tuple(r) for r in rows])
        except sqlite3.IntegrityError as e:
            raise IntegrityError(str(e)) from e
        except sqlite3.Error as e:
            raise StoreError(f"{e}（SQL: {sql[:120]}）") from e

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """执行查询并返回全部行。

        Args:
            sql: SQL 文本（`?` 占位）。
            params: 占位参数。

        Returns:
            `list[sqlite3.Row]`（可按列名访问）。"""
        return list(self.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        return self.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        """查询单个标量值。

        Args:
            sql: SQL 文本。
            params: 占位参数。
            default: 无结果时的返回值。

        Returns:
            第一行第一列；无结果返回 `default`。"""
        row = self.query_one(sql, params)
        return default if row is None else row[0]

    # ---- 事务 ----

    @contextmanager
    def atomic(self) -> Iterator["Database"]:
        """事务（可嵌套：内层用 SAVEPOINT）。

        连接是 `isolation_level=None`（自动提交）模式，所以这里显式 BEGIN。
        用 `BEGIN IMMEDIATE` 而不是 `BEGIN`：立刻拿写锁，避免「读到一半升级成写」
        时和其他连接互相等（SQLite 的死锁只会以 `database is locked` 的形式出现）。
        """
        conn = self.conn
        depth = int(getattr(self._local, "depth", 0))
        name = f"sp_{depth}"
        if depth == 0:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute(f"SAVEPOINT {name}")
        self._local.depth = depth + 1
        try:
            yield self
        except BaseException:
            if depth == 0:
                conn.execute("ROLLBACK")
            else:
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
            raise
        else:
            if depth == 0:
                conn.execute("COMMIT")
            else:
                conn.execute(f"RELEASE {name}")
        finally:
            self._local.depth = depth

    # ---- 表结构 ----

    def table_exists(self, table: str) -> bool:
        return self.scalar(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,), default=0) == 1

    def columns(self, table: str) -> dict[str, str]:
        """`{列名: 类型}`（`PRAGMA table_info` 的结果）。"""
        return {str(r["name"]): str(r["type"]) for r in self.query(f"PRAGMA table_info({_quote(table)})")}

    def tables(self) -> list[str]:
        return [str(r["name"]) for r in self.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    def ensure_schema(self, *models: type) -> "list[str]":
        """建表 + 补缺失列 + 建索引。**幂等**，返回本次实际做过的动作（便于日志）。

        只加不删：模型里删掉/改类型的列不会被动过 —— 数据是用户的，静默 drop
        一个列是不可接受的副作用。
        """
        actions: list[str] = []
        for model in models:
            table = model._table
            self.execute(model._create_table_sql())
            existing = self.columns(table)
            for field in model._fields.values():
                if field.column not in existing:
                    self.execute(
                        f"ALTER TABLE {_quote(table)} ADD COLUMN {field.column_sql(for_alter=True)}")
                    actions.append(f"{table}.{field.column} 已新增")
            for field in model._fields.values():
                if field.index and not field.unique and not field.primary_key:
                    idx = f"idx_{table}_{field.column}"
                    self.execute(f"CREATE INDEX IF NOT EXISTS {_quote(idx)} "
                                 f"ON {_quote(table)} ({_quote(field.column)})")
        return actions

    def register(self, *models: type) -> "Database":
        """`ensure_schema` + 把模型绑定到本库 —— 之后 `Model.objects` 直接用。"""
        self.ensure_schema(*models)
        for model in models:
            model.bind(self)
        return self

    # ---- async 逃生口 ----

    async def run(self, fn, *args: Any, **kwargs: Any) -> Any:
        """在工作线程里跑同步函数（避免长任务阻塞事件循环）。

        ⚠️ 线程里会**新开**一条连接（线程本地），事务不跨线程 —— 需要原子性的
        读改写成对，请在 `fn` 内部自己用 `db.atomic()`。
        """
        return await asyncio.to_thread(fn, *args, **kwargs)


# ==================== 查询集 ====================

class QuerySet:
    """惰性查询集：链式方法都返回**新的**对象，不会改动原对象。"""

    def __init__(self, model: type, *, using: Optional[Database] = None,
                 filters: tuple = (), excludes: tuple = (),
                 ordering: tuple = (), limit: Optional[int] = None,
                 offset: int = 0) -> None:
        self.model = model
        self._using = using
        self._filters = tuple(filters)
        self._excludes = tuple(excludes)
        self._ordering = tuple(ordering)
        self._limit = limit
        self._offset = int(offset)

    # ---- 复制 ----

    def _clone(self, **changes: Any) -> "QuerySet":
        data = {
            "using": self._using, "filters": self._filters,
            "excludes": self._excludes, "ordering": self._ordering,
            "limit": self._limit, "offset": self._offset,
        }
        data.update(changes)
        return QuerySet(self.model, **data)

    # ---- 链式 ----

    def using(self, db: Database) -> "QuerySet":
        return self._clone(using=db)

    def filter(self, **kwargs: Any) -> "QuerySet":
        """按等值条件筛选（链式，不修改当前对象）。

        Args:
            **kwargs: 字段名=值；未提供的字段不参与过滤。

        Returns:
            新的 `QuerySet`。"""
        return self._clone(filters=self._filters + tuple(kwargs.items()))

    def exclude(self, **kwargs: Any) -> "QuerySet":
        return self._clone(excludes=self._excludes + tuple(kwargs.items()))

    def order_by(self, *fields: str) -> "QuerySet":
        return self._clone(ordering=tuple(fields))

    def limit(self, n: int) -> "QuerySet":
        return self._clone(limit=int(n))

    def offset(self, n: int) -> "QuerySet":
        return self._clone(offset=int(n))

    # ---- 取值 ----

    @property
    def db(self) -> Database:
        db = self._using or self.model._default_db
        if db is None:
            raise StoreError(
                f"模型 {self.model.__name__} 没有绑定数据库：先 "
                f"{self.model.__name__}.bind(db)，或用 .using(db)")
        return db

    def _field(self, name: str) -> Field:
        field = self.model._fields.get(name)
        if field is None:
            raise FieldError(f"{self.model.__name__} 没有字段 {name!r}"
                             f"（可用：{', '.join(self.model._fields)}）")
        return field

    def _where(self, filters: tuple = (), excludes: tuple = ()) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in filters + self._filters:
            sql, sql_params = self._clause(key, value)
            clauses.append(sql)
            params.extend(sql_params)
        for key, value in excludes + self._excludes:
            sql, sql_params = self._clause(key, value)
            clauses.append("NOT (" + sql + ")")
            params.extend(sql_params)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def _clause(self, key: str, value: Any) -> tuple[str, list[Any]]:
        parts = str(key).split("__")
        lookup = "exact"
        if len(parts) > 1 and parts[-1] in _LOOKUPS:
            lookup = parts.pop()
        name = "__".join(parts)
        if name == "pk":
            name = self.model._pk_field.name
        field = self._field(name)
        col = _quote(field.column)
        if lookup == "isnull":
            return (f"{col} IS NULL" if value else f"{col} IS NOT NULL"), []
        if lookup == "in":
            values = list(value or ())
            if not values:
                return "0 = 1", []
            placeholders = ", ".join("?" for _ in values)
            return f"{col} IN ({placeholders})", [field.to_db(v) for v in values]
        if lookup == "range":
            lo, hi = list(value)
            return f"{col} BETWEEN ? AND ?", [field.to_db(lo), field.to_db(hi)]
        if lookup in ("contains", "icontains", "startswith", "istartswith",
                      "endswith", "iendswith"):
            text = str(value)
            base = lookup[1:] if lookup[0] == "i" and lookup[1:] in (
                "contains", "startswith", "endswith") else lookup
            pattern = {
                "contains": f"%{_escape_like(text)}%",
                "startswith": f"{_escape_like(text)}%",
                "endswith": f"%{_escape_like(text)}",
            }[base]
            if lookup.startswith("i"):
                return f"LOWER({col}) LIKE LOWER(?) ESCAPE '\\'", [pattern]
            return f"{col} LIKE ? ESCAPE '\\'", [pattern]
        op = {"exact": "=", "iexact": "=", "gt": ">", "gte": ">=",
              "lt": "<", "lte": "<="}[lookup]
        db_value = field.to_db(value)
        if lookup == "iexact":
            return f"LOWER({col}) = LOWER(?)", [db_value]
        if db_value is None:
            return (f"{col} IS NULL" if lookup == "exact"
                    else f"{col} {op} NULL"), []
        return f"{col} {op} ?", [db_value]

    def _select_sql(self, *, columns: str = "*") -> tuple[str, list[Any]]:
        where, params = self._where()
        sql = f"SELECT {columns} FROM {_quote(self.model._table)}{where}"
        if self._ordering:
            order = []
            for item in self._ordering:
                if item == "?":
                    order.append("RANDOM()")
                    continue
                desc = str(item).startswith("-")
                name = str(item).lstrip("-+")
                if name in ("pk", "id") and name == "pk":
                    name = self.model._pk_field.name
                order.append(f"{_quote(self._field(name).column)} {'DESC' if desc else 'ASC'}")
            sql += " ORDER BY " + ", ".join(order)
        if self._limit is not None:
            sql += " LIMIT ?"
            params.append(int(self._limit))
        if self._offset:
            sql += " LIMIT -1 OFFSET ?" if self._limit is None else " OFFSET ?"
            params.append(int(self._offset))
        return sql, params

    def _row_to_obj(self, row: sqlite3.Row) -> Any:
        obj = self.model.__new__(self.model)
        obj._loaded = True
        for name, field in self.model._fields.items():
            setattr(obj, name, field.to_python(row[field.column]))
        return obj

    def iterator(self, *, batch: int = 500) -> Iterator[Any]:
        sql, params = self._select_sql()
        cur = self.db.execute(sql, params)
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                return
            for row in rows:
                yield self._row_to_obj(row)

    def __iter__(self) -> Iterator[Any]:
        return self.iterator()

    def __len__(self) -> int:
        return len(list(self.iterator()))

    def __bool__(self) -> bool:
        return self.exists()

    def __getitem__(self, item: Any) -> Any:
        if isinstance(item, slice):
            if item.step not in (None, 1):
                raise StoreError("不支持带步长的切片")
            start = item.start or 0
            if start < 0 or (item.stop is not None and item.stop < 0):
                raise StoreError("不支持负数索引（SQL 没有「倒数第几行」的概念）")
            limit = None if item.stop is None else max(0, item.stop - start)
            if limit == 0:
                return []
            return list(self._clone(offset=self._offset + start,
                                    limit=limit).iterator())
        idx = int(item)
        if idx < 0:
            raise StoreError("不支持负数索引")
        rows = list(self._clone(offset=self._offset + idx, limit=1).iterator())
        if not rows:
            raise IndexError(f"{self.model.__name__} 查询越界")
        return rows[0]

    def all(self) -> "QuerySet":
        return self._clone()

    def first(self) -> Any:
        rows = self._clone(ordering=self._ordering, limit=1)._rows()
        return rows[0] if rows else None

    def last(self) -> Any:
        ordering = tuple(self._ordering)
        if not ordering or "?" in ordering:
            ordering = ("-" + self.model._pk_field.name,)
        else:
            inverted = tuple(("-" + f.lstrip("-")) if not f.startswith("-") else f[1:]
                             for f in ordering)
            ordering = inverted
        rows = self._clone(ordering=ordering, limit=1)._rows()
        return rows[0] if rows else None

    def _rows(self) -> list[Any]:
        return list(self.iterator())

    def get(self, **kwargs: Any) -> Any:
        """取唯一一条记录。

        Args:
            **kwargs: 过滤条件。

        Returns:
            模型实例。

        Raises:
            DoesNotExist: 没有匹配记录。
            MultipleObjectsReturned: 多于一条。"""
        if kwargs:
            qs = self.filter(**kwargs)
        else:
            qs = self
        rows = qs._clone(limit=2)._rows()
        if not rows:
            raise self.model.DoesNotExist(
                f"{self.model.__name__} 没有匹配的记录")
        if len(rows) > 1:
            raise self.model.MultipleObjectsReturned(
                f"{self.model.__name__} 匹配到多条记录（get() 只接受一条）")
        return rows[0]

    def count(self) -> int:
        where, params = self._where()
        return int(self.db.scalar(
            f"SELECT COUNT(*) FROM {_quote(self.model._table)}{where}", params, default=0))

    def exists(self) -> bool:
        return bool(self.count())

    def values(self, *names: str) -> list[dict[str, Any]]:
        """`[{字段: 值}]`，按需只取几列（省内存/省转换）。"""
        fields = [self._field(n) for n in names] if names else list(self.model._fields.values())
        columns = ", ".join(_quote(f.column) for f in fields)
        sql, params = self._select_sql(columns=columns)
        out = []
        for row in self.db.query(sql, params):
            out.append({f.name: f.to_python(row[f.column]) for f in fields})
        return out

    # ---- 写 ----

    def update(self, **kwargs: Any) -> int:
        """批量 UPDATE，返回受影响行数（不触发 `save()` 钩子）。"""
        if not kwargs:
            return 0
        sets, params = [], []
        for key, value in kwargs.items():
            field = self._field(key)
            sets.append(f"{_quote(field.column)} = ?")
            params.append(field.to_db(value))
        where, where_params = self._where()
        cur = self.db.execute(
            f"UPDATE {_quote(self.model._table)} SET {', '.join(sets)}{where}",
            params + where_params)
        return int(cur.rowcount or 0)

    def delete(self) -> int:
        where, params = self._where()
        cur = self.db.execute(f"DELETE FROM {_quote(self.model._table)}{where}", params)
        return int(cur.rowcount or 0)

    def create(self, **kwargs: Any) -> Any:
        """创建并保存一条记录。

        Args:
            **kwargs: 字段初值。

        Returns:
            已保存的模型实例（含自增主键）。"""
        return self.model(**kwargs).save(using=self._using)

    def get_or_create(self, defaults: Optional[dict] = None, **kwargs: Any):
        """返回 `(对象, 是否新建)`。"""
        try:
            return self.get(**kwargs), False
        except self.model.DoesNotExist:
            data = dict(defaults or {})
            data.update(kwargs)
            return self.create(**data), True

    def update_or_create(self, defaults: Optional[dict] = None, **kwargs: Any):
        """返回 `(对象, 是否新建)`；已存在则用 `defaults` 更新。"""
        data = dict(defaults or {})
        try:
            obj = self.get(**kwargs)
        except self.model.DoesNotExist:
            merged = dict(kwargs)
            merged.update(data)
            return self.create(**merged), True
        for key, value in data.items():
            setattr(obj, key, value)
        obj.save(using=self._using)
        return obj, False

    def bulk_create(self, objs: Iterable[Any]) -> int:
        """批量 INSERT（一条 executemany），返回写入行数。

        不会回填自增主键 —— 需要对象的 pk 就逐条 `save()`。
        ⚠️ 所有对象的列必须一致（比如都给了主键、或都没给）：混着来的话
        executemany 会把值塞错列，所以这里直接拒绝并让你逐条 `save()`。
        """
        columns: Optional[list[str]] = None
        rows: list[list[Any]] = []
        for obj in objs:
            cols, values = self.model._insert_values(obj)
            if columns is None:
                columns = cols
            elif cols != columns:
                raise StoreError("bulk_create：对象的列不一致（有的带主键有的没带？）"
                                 "，请逐条 save()")
            rows.append(values)
        if not rows or not columns:
            return 0
        placeholders = ", ".join("?" for _ in columns)
        cols = ", ".join(_quote(c) for c in columns)
        self.db.executemany(
            f"INSERT INTO {_quote(self.model._table)} ({cols}) "
            f"VALUES ({placeholders})", rows)
        return len(rows)


class Manager:
    """`Model.objects` —— 查询集的入口（Django 风格）。"""

    def __set_name__(self, owner: type, name: str) -> None:
        self.model = owner
        self.name = name

    def __get__(self, instance: Any, owner: Optional[type] = None) -> "Manager":
        if owner is not None:
            self.model = owner
        return self

    @property
    def db(self) -> Database:
        return QuerySet(self.model).db

    def get_queryset(self) -> QuerySet:
        return QuerySet(self.model)

    def using(self, db: Database) -> QuerySet:
        return QuerySet(self.model, using=db)

    # 转发到查询集（保持 `Model.objects.xxx()` 的手感）
    def all(self) -> QuerySet:
        return self.get_queryset()

    def filter(self, **kwargs: Any) -> QuerySet:
        return self.get_queryset().filter(**kwargs)

    def exclude(self, **kwargs: Any) -> QuerySet:
        return self.get_queryset().exclude(**kwargs)

    def order_by(self, *fields: str) -> QuerySet:
        return self.get_queryset().order_by(*fields)

    def create(self, **kwargs: Any) -> Any:
        return self.get_queryset().create(**kwargs)

    def get(self, **kwargs: Any) -> Any:
        return self.get_queryset().get(**kwargs)

    def get_or_create(self, defaults: Optional[dict] = None, **kwargs: Any):
        return self.get_queryset().get_or_create(defaults=defaults, **kwargs)

    def update_or_create(self, defaults: Optional[dict] = None, **kwargs: Any):
        return self.get_queryset().update_or_create(defaults=defaults, **kwargs)

    def bulk_create(self, objs: Iterable[Any]) -> int:
        return self.get_queryset().bulk_create(objs)

    def first(self) -> Any:
        return self.get_queryset().first()

    def last(self) -> Any:
        return self.get_queryset().last()

    def count(self) -> int:
        return self.get_queryset().count()

    def exists(self) -> bool:
        return self.get_queryset().exists()

    def values(self, *names: str) -> list[dict[str, Any]]:
        return self.get_queryset().values(*names)


# ==================== 模型 ====================

class ModelMeta(type):
    """模型元类：收集字段、注入默认表名与管理器。"""
    def __new__(mcls, name: str, bases: tuple, ns: dict):
        cls = super().__new__(mcls, name, bases, ns)
        if name == "Model" and not any(hasattr(b, "_is_model") for b in bases):
            return cls

        fields: dict[str, Field] = {}
        for base in reversed(cls.__mro__):
            for key, value in vars(base).items():
                if isinstance(value, Field):
                    fields[key] = value
        # 没有声明主键 → 自动加一个自增 id（Django 也这么做）
        if not any(f.primary_key for f in fields.values()):
            auto = AutoField()
            auto.name = "id"
            fields = {"id": auto, **fields}
        for key, field in fields.items():
            if not field.name:
                field.name = key
        pk_fields = [f for f in fields.values() if f.primary_key]
        if len(pk_fields) != 1:
            raise StoreError(f"模型 {name} 必须恰好有一个主键（现在 {len(pk_fields)} 个）")

        meta = ns.get("Meta")
        table = getattr(meta, "table", "") or name.lower()
        cls._is_model = True
        cls._fields = fields
        cls._pk_field = pk_fields[0]
        cls._table = str(table)
        cls._default_db: Optional[Database] = ns.get("_default_db")
        cls.DoesNotExist = type(f"{name}.DoesNotExist", (DoesNotExist,), {})
        cls.MultipleObjectsReturned = type(
            f"{name}.MultipleObjectsReturned", (MultipleObjectsReturned,), {})
        if "objects" not in ns:
            cls.objects = Manager()
        return cls


class Model(metaclass=ModelMeta):
    """继承它 + 声明 `Field` 就是一个表。"""

    class Meta:
        table = ""

    objects: Manager

    def __init__(self, **kwargs: Any) -> None:
        self._loaded = False
        provided: set[str] = set()
        for key, value in kwargs.items():
            if key == "pk":
                key = self._pk_field.name
            if key not in self._fields:
                raise TypeError(f"{type(self).__name__} 没有字段 {key!r}")
            setattr(self, key, value)
            provided.add(key)
        # ⚠️ 必须**总是**写实例属性：`hasattr(self, name)` 会命中类上的 Field 对象，
        # 于是默认值永远不生效、`getattr` 拿到的是一个 `<IntegerField>`（实测踩过）。
        for name, field in self._fields.items():
            if name in provided:
                continue
            default = field.get_default()
            setattr(self, name, None if default is NOT_PROVIDED else default)

    # ---- 绑定 ----

    @classmethod
    def bind(cls, db: Database) -> type:
        """把模型绑定到某个库（`Model.objects` 默认走它）。返回类本身，便于链式。"""
        cls._default_db = db
        return cls

    # ---- 实例 ----

    @property
    def pk(self) -> Any:
        return getattr(self, self._pk_field.name, None)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name, None) for name in self._fields}

    def __repr__(self) -> str:                     # pragma: no cover - 调试用
        return f"<{type(self).__name__} pk={self.pk!r}>"

    def __eq__(self, other: Any) -> bool:
        return (type(self) is type(other) and self.pk is not None
                and self.pk == other.pk)

    def save(self, *, using: Optional[Database] = None) -> "Model":
        """保存当前实例（无主键 INSERT，否则 UPDATE）。

        Args:
            using: 指定数据库；默认用模型绑定 / 插件数据库。"""
        db = using or self._default_db
        if db is None:
            raise StoreError(f"{type(self).__name__} 没有绑定数据库：先 bind(db)")
        if self._loaded:
            return self._update(db)
        return self._insert(db)

    def _insert(self, db: Database) -> "Model":
        columns, values = self._insert_values(self)
        placeholders = ", ".join("?" for _ in columns)
        cols = ", ".join(_quote(c) for c in columns)
        cur = db.execute(
            f"INSERT INTO {_quote(self._table)} ({cols}) VALUES ({placeholders})",
            values)
        pk_field = self._pk_field
        if isinstance(pk_field, AutoField) and self.pk is None:
            setattr(self, pk_field.name, cur.lastrowid)
        self._loaded = True
        return self

    def _update(self, db: Database) -> "Model":
        pk_field = self._pk_field
        pk_value = self.pk
        if pk_value is None:
            raise StoreError(f"{type(self).__name__}.save()：没有主键，无法 UPDATE")
        sets, params = [], []
        for name, field in self._fields.items():
            if field.primary_key:
                continue
            sets.append(f"{_quote(field.column)} = ?")
            params.append(field.to_db(getattr(self, name, None)))
        cur = db.execute(
            f"UPDATE {_quote(self._table)} SET {', '.join(sets)} "
            f"WHERE {_quote(pk_field.column)} = ?", params + [pk_value])
        if not cur.rowcount:
            raise self.DoesNotExist(
                f"{type(self).__name__} pk={pk_value} 已不存在（UPDATE 影响 0 行）")
        return self

    @classmethod
    def _insert_values(cls, obj: "Model") -> tuple[list[str], list[Any]]:
        columns, values = [], []
        for name, field in cls._fields.items():
            if isinstance(field, AutoField) and getattr(obj, name, None) is None:
                continue
            value = getattr(obj, name, None)
            if value is None:
                default = field.get_default()
                if default is not NOT_PROVIDED:
                    value = default
            if value is None and not field.null and not field.primary_key:
                raise StoreError(
                    f"{cls.__name__}.{name} 不能为空（INSERT 前请给值或设 default）")
            columns.append(field.column)
            values.append(field.to_db(value))
        return columns, values

    def delete(self, *, using: Optional[Database] = None) -> int:
        """删除当前实例。

        Args:
            using: 指定数据库；默认用模型绑定 / 插件数据库。"""
        db = using or self._default_db
        if db is None:
            raise StoreError(f"{type(self).__name__} 没有绑定数据库：先 bind(db)")
        if self.pk is None:
            raise StoreError("delete()：没有主键")
        cur = db.execute(
            f"DELETE FROM {_quote(self._table)} "
            f"WHERE {_quote(self._pk_field.column)} = ?", [self.pk])
        self._loaded = False
        return int(cur.rowcount or 0)

    def refresh(self, *, using: Optional[Database] = None) -> "Model":
        db = using or self._default_db
        if db is None or self.pk is None:
            raise StoreError("refresh()：需要已绑定数据库且已有主键")
        obj = type(self).objects.using(db).get(**{self._pk_field.name: self.pk})
        for name in self._fields:
            setattr(self, name, getattr(obj, name))
        self._loaded = True
        return self

    # ---- 由元类/查询集使用的内部接口 ----

    @classmethod
    def _create_table_sql(cls) -> str:
        cols = ", ".join(f.column_sql() for f in cls._fields.values())
        return f"CREATE TABLE IF NOT EXISTS {_quote(cls._table)} ({cols})"


__all__ = [
    "Database", "QuerySet", "Manager", "Model", "Field",
    "AutoField", "IntegerField", "FloatField", "BooleanField",
    "CharField", "TextField", "JSONField", "DateTimeField",
    "StoreError", "DoesNotExist", "MultipleObjectsReturned",
    "IntegrityError", "FieldError", "NOT_PROVIDED",
]
