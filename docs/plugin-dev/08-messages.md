# 八、消息构造

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[权限管理](07-permissions.md) · 下一章：[数据持久化（`napcat.store`）](09-store.md)


```python
from napcat.message import MessageChain, MusicPlatform, ContactType
# 基础
MessageChain().text("你好").at("123456").face("123")

# 多媒体
MessageChain().image("https://example.com/photo.jpg")
MessageChain().record("https://example.com/voice.amr")

# 回复
MessageChain().reply(seq=123456).text("收到")

# 合并转发
MessageChain().node(user_id=123, nickname="小明",
    content=MessageChain().text("转发的消息"))

# 音乐
MessageChain().music_id(MusicPlatform.QQ, "12345")
MessageChain().music_custom(url="...", image="...", title="歌名")

# 交互
MessageChain().dice().rps("石头").poke()

# 输出
chain.build()    # → [{"type":"text","data":{...}}, ...]
chain.to_cq()    # → "你好[CQ:at,qq=123456]"
```

---
