# 九、文件管理

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[插件开发](08-plugin-dev.md) · 下一章：[定时事件](10-timers.md)


## 9.1 基础操作

```python
# 图片/语音/文件信息
img = await client.files.get_image("file_id")
print(img.file, img.url, img.size, img.base64)

# 上传群文件
file_id = await client.files.upload_group_file(
    group_id="123456",
    file_path="./test.pdf",
    name="测试.pdf",
)

# 下载到本地
local_path = await client.files.download("https://example.com/file.pdf", save_path="./dl/")
```

## 9.2 流式下载

```python
# 分片迭代
async for chunk in client.files.download_stream("https://example.com/big.bin"):
    await outfile.write(chunk)

# 带进度回调
await client.files.download_to_file(
    url="https://example.com/big.zip",
    save_path="./downloads/big.zip",
    on_progress=lambda received, total: print(f"\r{received} bytes"),
)
```

## 9.3 文件 Hash

```python
sha = await FileManager.file_sha256("./local_file.zip")
```

---
