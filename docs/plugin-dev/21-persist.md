# 二十一、本地状态与清理

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[服务范围](20-scope.md)

插件经常需要在磁盘上留下两类东西：

1. **状态**：去重集合、游标、索引、缓存 —— 重启后还要接着用；
2. **文件**：出图、下载的附件、缩略图 —— 会越攒越多，得按规则清掉。

这两件事都在 **`napcat.persist`** 里：JSON 的原子读写，加两种目录清理策略。
用库表存的数据请走 [`napcat.store`](09-store.md)，两者边界见 21.5。

## 21.1 JSON 状态：`read_json` / `write_json`

```python
from napcat.persist import read_json, write_json

state = read_json(self.data_path, {"seen": [], "cursor": 0})
state["seen"].append(new_id)
write_json(self.data_path, state)
```

| 函数 | 行为 |
|------|------|
| `read_json(path, default=None)` | 文件不存在、读不了、内容坏了都返回 `default`，**不抛异常** |
| `write_json(path, payload, *, indent=None)` | 先写 `<名>.tmp` 再原子替换；父目录自动建；`indent=None` 用紧凑分隔符 |

几条契约：

- **原子替换**：中途断电/被杀不会留下半份文件（旧内容仍完整）；
- **紧凑输出**：状态文件动辄几十 KB，缩进只方便人眼，体积翻倍不值 —— 要人读就传 `indent=2`；
- **读坏不抛**：损坏的状态文件按「还没有状态」处理，插件照常启动；
- **落盘失败照抛 `OSError`**：磁盘满、权限不足这类问题必须让调用方知道（告警或降级），
  不要在库里静默吞掉。

## 21.2 按数量清理：`prune_keep`

适合「只保留最近的 N 个文件」的目录：出图、截图、下载。

```python
from napcat.persist import prune_keep

# 图片目录只留最近 60 个；Chromium 的 profile 目录不算
prune_keep(self.out_dir, keep=60, skip_prefix=("prof-",), skip_suffix=(".part",))
```

| 参数 | 说明 |
|------|------|
| `keep` | 保留多少个（按修改时间，至少 1） |
| `skip_prefix` | 跳过以这些前缀开头的条目（正在用的临时目录，如浏览器 profile） |
| `skip_suffix` | 跳过以这些后缀结尾的条目（下载中的半成品） |

返回**被删掉的路径列表**（按淘汰顺序），需要同步内存缓存时用它；目录不存在或
读不了时返回空列表。

## 21.3 按引用清理：`prune_unreferenced`

适合「缓存里只保留仍被引用的文件」：图片缓存、表格图、附件。

```python
from napcat.persist import prune_unreferenced

# 只留被当前通知引用的图；仍在下载的 .tmp 无条件删；1 小时内的不动
prune_unreferenced(
    self.cache_dir,
    keep={image_key(u) for u in urls},
    key=lambda name: name.split(".", 1)[0],   # 文件名主名与 URL 的键比对
    always_delete=".tmp",
    min_age=3600,
)
```

| 参数 | 说明 |
|------|------|
| `keep` | 仍在引用的键集合 |
| `key` | 文件名 → 键（默认整个文件名） |
| `only_suffix` | 只处理这种后缀（空 = 全部） |
| `always_delete` | 这种后缀无条件删（下载中的 `.tmp`），哪怕主名在 `keep` 里 |
| `min_age` | 只删至少这么久没动过的文件（秒），避免和正在发送的消息抢文件 |

返回被删掉的文件名列表。与 `prune_keep` 的分工：那个按**数量和年龄**淘汰，
这个按**是否仍被引用**淘汰，没有数量上限。

## 21.4 两种清理怎么选

| 场景 | 用哪个 | 理由 |
|------|--------|------|
| 出图 / 截图目录 | `prune_keep` | 图发出去就没用了，只留最近几张供重发 |
| 下载的附件 | `prune_keep` | 同上，按时间淘汰最简单 |
| 图片 / 表格图缓存 | `prune_unreferenced` | 被消息引用的不能删，其余才清 |
| 临时目录（下载中的文件） | 都不要 | 用 `skip_suffix` / `always_delete` 把它排除在外 |

清理调用放在**一次发送结束之后**（而不是发送之前）：先清可能把正要发的文件删掉。
`min_age` 是第二道保险。

## 21.5 什么时候用 sqlite，什么时候用 JSON

| 用 `napcat.store`（sqlite） | 用 `napcat.persist`（JSON） |
|------|------|
| 要按字段查询、分页、聚合 | 整份读出、整份写回 |
| 多进程/多连接并发写 | 单进程、低频写 |
| 数据量会持续增长（题库、会话历史） | 有明确上限的小状态（去重集合、游标、索引） |
| 需要事务 | 一次写就能表达完整状态 |

JSON 状态文件请**始终**配一个上限（条数或时间），并在写入前淘汰 —— 无上限的
状态文件是「某天突然变慢」的常见来源。

## 21.6 目录约定

| 目录 | 放什么 | 是否进版本库 |
|------|--------|------|
| `plugins/<名>/data/` | 状态、库文件、索引 | 否（框架与发布脚本都跳过） |
| `plugins/<名>/data/out/` 之类 | 出图、下载、缓存 | 否 |
| `plugins/<名>/assets/` | 需要版本化的静态资源 | 是 |

`self.database()` 默认把库放在 `data/` 下；`napcat.persist` 不会替你选目录，
路径由插件自己决定（通常也从 `data/` 派生）。
