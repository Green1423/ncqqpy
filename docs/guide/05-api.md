# 五、API 调用

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[事件系统](04-events.md) · 下一章：[命令系统](06-commands.md)

> 完整方法清单（官方 178 个 action 全覆盖）见 [NapCat API 覆盖表](../reference/napcat-api.md)。


`client.api` 提供 100+ 方法，**自动提取响应 `data` 字段**，失败抛出 `ApiError`。

## 5.1 消息发送

```python
from napcat.message import MessageChain
# 发送群消息
result = await client.api.send_group_msg(
    group_id="123456",
    message=MessageChain().text("你好").at("987654"),
)
print(result.message_id)  # 123456

# 发送私聊消息
await client.api.send_private_msg(
    user_id="987654",
    message="你好呀",
)

# 通用发送（自动判断类型）
await client.api.send_msg(
    message_type="group", group_id="123456",
    message="Hello",
)

# 撤回消息
await client.api.delete_msg(message_id=123456)

# 标记已读
await client.api.mark_msg_as_read(group_id="123456")
```

## 5.2 群组管理

```python
# 获取群列表
groups = await client.api.get_group_list()
for g in groups:
    print(g.group_id, g.group_name)

# 获取群信息
info = await client.api.get_group_info("123456")
# Group(group_id=123456, group_name="测试群", member_count=100, ...)

# 获取成员列表
members = await client.api.get_group_member_list("123456")
# [GroupMember(user_id=987654, nickname="小明", role="admin", ...), ...]

# 获取单个成员信息
m = await client.api.get_group_member_info("123456", "987654")

# 群管理操作
await client.api.set_group_ban("123456", "987654", 600)           # 禁言600秒
await client.api.set_group_ban("123456", "987654", 0)             # 解禁
await client.api.set_group_whole_ban("123456", True)              # 全员禁言
await client.api.set_group_admin("123456", "987654", True)        # 设管理员
await client.api.set_group_kick("123456", "987654")               # 踢出
await client.api.set_group_name("123456", "新群名")               # 改群名
await client.api.set_group_card("123456", "987654", "新名片")     # 改名片
await client.api.set_group_special_title("123456", "987654", "头衔") # 设头衔
await client.api.set_group_leave("123456")                        # 退群
```

## 5.3 好友与用户

```python
# 好友列表
friends = await client.api.get_friend_list()
# [User(user_id=..., nickname=..., remark=..., ...), ...]

# 陌生人信息
await client.api.get_stranger_info("987654")

# 处理好友请求
await client.api.set_friend_add_request(flag="xxx", approve=True, remark="备注")
await client.api.delete_friend("987654")
await client.api.send_like("987654", times=10)

# 最近联系人
await client.api.get_recent_contact()
```

## 5.4 文件操作

```python
# 获取图片信息
info = await client.api.get_image("file_id")
# FileInfo(file="/tmp/xxx.jpg", url="https://...", size=12345, base64="...")

# 上传群文件
await client.api.upload_group_file(
    group_id="123456",
    file="/path/to/file.pdf",
    name="file.pdf",
)

# 下载文件
info = await client.api.download_file(url="https://example.com/doc.pdf")
print(info.file)  # 本地临时路径
```

## 5.5 系统与扩展

```python
# 登录信息
login = await client.api.get_login_info()
# LoginInfo(user_id=123, nickname="机器人", ...)

# 状态
status = await client.api.get_status()
print(status.online, status.good)

# 在线状态
await client.api.set_online_status(status=11)

# 个人资料
await client.api.set_qq_profile(
    nickname="新昵称", personal_note="签名", sex="1")

# OCR / 翻译
text = await client.api.ocr_image("image_file_id")
tr = await client.api.translate_en2zh("hello world")

# 戳一戳
await client.api.send_poke(user_id="987654", group_id="123456")

# QQ空间
result = await client.api.send_qzone_msg(
    content="今天天气不错",
    images=["https://..."],
    ugc_right=1,  # 1=所有人可见
)
await client.api.delete_qzone_msg(tid=result["tid"])
```

## 5.6 错误处理

```python
from napcat.api import ApiError
try:
    await client.api.set_group_ban("123", "456", 600)
except ApiError as e:
    print(f"API 错误 [{e.retcode}]: {e.message}")
    # 常见 retcode:
    #   1400 - 参数错误   1403 - Token 失败
    #   1404 - API 不存在  1200 - 服务器内部错误
```

---
