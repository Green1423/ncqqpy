# 九、文件管理

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[插件开发](08-plugin-dev.md) · 下一章：[定时事件](10-timers.md)

文件相关接口都走 NapCat 的**流式文件 API**：下载分片经 WebSocket 传回、边收边写；
上传把本地文件分块发给 NapCat，不需要框架与 NapCat 共享文件系统（远程部署也能用）。

> 示例号码均为虚构。

## 9.1 文件信息

```python
# 图片 / 语音 / 普通文件的信息（路径、URL、大小、可选 base64）
img = await client.files.get_image("file_id")
record = await client.files.get_record("file_id")
file = await client.files.get_file("file_id")
print(img.file, img.url, img.size)
```

## 9.2 流式下载

```python
# 逐块处理（不落盘）
async for chunk in client.files.download_stream("https://example.com/big.bin"):
    await outfile.write(chunk)

# 下载到文件，带进度（total 未知时为 -1）
await client.files.download_to_file(
    "https://example.com/big.zip",
    "./downloads/big.zip",
    on_progress=lambda received, total: print(received, total),
)

# 小文件直接进内存
data = await client.files.download_bytes("https://example.com/small.png")

# 图片 / 语音（语音可转码）
async for chunk in client.files.download_image_stream(file="file_id"):
    ...
async for chunk in client.files.download_record_stream(file="file_id", out_format="mp3"):
    ...
```

`download()` 是 `download_to_file()` 的便捷写法；不传保存路径时写入系统临时目录。

## 9.3 流式上传

`upload_stream` 把本地文件分块上传到 NapCat，返回 NapCat 侧的文件信息
（`file_path` / `file_name` / `file_size` / `sha256` 等，具体字段以 NapCat 版本为准）：

```python
info = await client.files.upload_stream(
    "./big.zip",
    name="big.zip",
    on_progress=lambda sent, total: print(f"{sent}/{total}"),
)
print(info["file_path"])
```

- 默认 512KB 一块；可用 `chunk_size=` 调整；
- `retention` 是 NapCat 侧临时文件保留时间（毫秒，默认 5 分钟）；
- 提供 `expected_sha256=` 时服务端会校验（可先 `await FileManager.file_sha256(path)`）；
- 分块上传不走上报节流，但大文件仍受网络与 NapCat 处理速度限制。

上传到**群文件**用另一条接口（读本地路径或 URL，不经过流式通道）：

```python
file_id = await client.files.upload_group_file(
    group_id="100000002",
    file_path="./test.pdf",
    name="测试.pdf",
)
```

## 9.4 清理与校验

```python
# 清理 NapCat 侧流式传输临时文件
await client.files.clean_temp()

# 计算本地文件 SHA256
sha = await client.files.file_sha256("./local_file.zip")
```
