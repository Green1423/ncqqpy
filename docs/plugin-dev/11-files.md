# 十一、文件管理

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[定时任务](10-timers.md) · 下一章：[完整插件示例](12-example.md)

插件通过 `self.files`（`FileManager`）使用 NapCat 的流式文件接口。下载分片经
WebSocket 传回、边收边写；上传分块发到 NapCat，返回服务器侧文件信息。
不需要框架和 NapCat 共享文件系统。

## 11.1 信息与下载

```python
# 文件信息
info = await self.files.get_image("file_id")

# 流式下载到文件（带进度）
await self.files.download_to_file(
    "https://example.com/doc.pdf",
    "./dl/doc.pdf",
    on_progress=lambda got, total: self.logger.info("%s/%s", got, total),
)

# 逐块处理 / 小文件进内存
async for chunk in self.files.download_stream("https://example.com/big.bin"):
    ...
data = await self.files.download_bytes("https://example.com/small.png")

# 图片 / 语音（语音可转码）
async for chunk in self.files.download_record_stream(file="file_id", out_format="mp3"):
    ...
```

保存路径由插件自己决定（建议放在 `self.plugin_dir / "data"` 下）。

## 11.2 上传

```python
# 流式上传到 NapCat：返回 file_path / file_name / file_size / sha256
uploaded = await self.files.upload_stream("./report.pdf", name="report.pdf",
                                          on_progress=...)

# 也可以直接发到群文件（读本地路径/URL）
await self.files.upload_group_file(
    group_id="100000002", file_path="./report.pdf", name="report.pdf")
```

- 默认 512KB 一块，`chunk_size=` 可改；
- `retention`（毫秒）控制 NapCat 临时文件保留时间；
- `expected_sha256=` 让服务端校验完整性；
- 上传完成后按需用返回的 `file_path` 发消息、或调用群文件接口。

## 11.3 清理

```python
await self.files.clean_temp()          # 清理 NapCat 侧流式临时文件
sha = await self.files.file_sha256("./report.pdf")
```

> 低层接口也可以直接用：`self.api.download_file_stream(...)` 是异步字节迭代器，
> `self.api.iter_file_stream(...)` 会产出 `file_info` / `file_chunk` /
> `file_complete` 事件（Base64 分片）。
