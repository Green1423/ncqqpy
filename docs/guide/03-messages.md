# 三、消息构造系统

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[客户端配置](02-client.md) · 下一章：[事件系统](04-events.md)


使用 `MessageChain` 以**链式 API** 构造所有 24 种 OneBot 消息段。

## 3.1 基础文本与 @

```python
from napcat.message import MessageChain
# 纯文本
MessageChain().text("Hello World!")

# @某人
MessageChain().at("123456789")          # @指定QQ
MessageChain().at("all")                # @全体成员

# 回复消息
MessageChain().reply(seq=123456).text("收到")

# QQ 表情
MessageChain().face("123")
```

## 3.2 多媒体文件

```python
# 图片（支持本地路径、URL、base64）
MessageChain().image("https://example.com/photo.jpg")
MessageChain().image("file:///C:/photo.png", name="photo.png")

# 语音
MessageChain().record("https://example.com/voice.amr")

# 视频
MessageChain().video("https://example.com/video.mp4")

# 通用文件
MessageChain().file("https://example.com/doc.pdf", name="doc.pdf")
```

## 3.3 音乐

```python
from napcat.message import MusicPlatform
# 平台 ID 音乐
MessageChain().music_id(MusicPlatform.QQ, "12345")

# 自定义音乐
MessageChain().music_custom(
    url="https://music.example.com",
    image="https://music.example.com/cover.jpg",
    title="歌曲标题",
    content="简介",
)
```

## 3.4 互动类

```python
# 戳一戳
MessageChain().poke()

# 骰子
MessageChain().dice()       # 随机1-6
MessageChain().dice(6)      # 指定结果

# 猜拳
MessageChain().rps("石头")
```

## 3.5 富文本

```python
# Markdown
MessageChain().markdown("# 标题\n这是 **Markdown**")

# JSON 卡片
MessageChain().json({"app": "com.tencent.structmsg"})

# XML 卡片
MessageChain().xml("<msg>...</msg>")

# 小程序
MessageChain().miniapp({"app": "..."})

# 位置分享
MessageChain().location(39.9042, 116.4074, title="北京", content="朝阳区")

# 名片分享
from napcat.message import ContactType
MessageChain().contact(ContactType.QQ, "123456789")
```

## 3.6 合并转发

```python
# 构造转发节点
node1 = MessageChain().node(
    user_id=987654321,
    nickname="小明",
    content=MessageChain().text("这是转发的消息"),
)

# 转发消息
MessageChain().forward(forward_id="res_id")

# 在线文件 / 闪传
MessageChain().online_file(msg_id="xxx", element_id="yyy", file_name="doc.pdf", file_size="1024")
MessageChain().flash_transfer(file_set_id="fileset_xxx")
```

## 3.7 输出格式

```python
chain = MessageChain().text("你好").at("123456").text(" 欢迎!")

# 构建为消息段列表（传给 API）
segments = chain.build()
# [{"type":"text","data":{"text":"你好"}},
#  {"type":"at","data":{"qq":"123456"}},
#  {"type":"text","data":{"text":" 欢迎!"}}]

# 转为 CQ 码字符串
cq = chain.to_cq()
# "你好[CQ:at,qq=123456] 欢迎!"

# 从 CQ 码解析
chain = MessageChain.from_cq("你好[CQ:at,qq=123456] 欢迎!")

# 消息段数量
len(chain)  # 3

# 合并两个消息链
combined = chain_a + chain_b
```

---
