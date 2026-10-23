# 十一、文件管理

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[定时任务](10-timers.md) · 下一章：[完整插件示例](12-example.md)


```python
# 获取图片
info = await self.files.get_image("file_id")
print(info.file, info.url, info.size)

# 下载
path = await self.files.download("https://example.com/doc.pdf", save_path="./dl/")

# 上传群文件
await self.files.upload_group_file(
    group_id="123456", file_path="./test.pdf", name="文档.pdf")
```

---
