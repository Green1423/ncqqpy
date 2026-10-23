# 四、事件系统

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[消息构造系统](03-messages.md) · 下一章：[API 调用](05-api.md)


## 4.1 事件类型总览

所有事件继承自 `NapCatEvent`，通过 `post_type` 自动分发为具体类型：

| 事件类 | post_type | 说明 |
|--------|-----------|------|
| `HeartbeatEvent` | `meta_event` | 心跳 |
| `LifeCycleEvent` | `meta_event` | 生命周期 |
| `MessageEvent` | `message` | 接收消息 |
| `GroupIncreaseEvent` | `notice` | 群成员增加 |
| `GroupDecreaseEvent` | `notice` | 群成员减少 |
| `GroupBanEvent` | `notice` | 群禁言 |
| `GroupAdminEvent` | `notice` | 管理员变动 |
| `GroupRecallEvent` | `notice` | 群消息撤回 |
| `GroupUploadEvent` | `notice` | 群文件上传 |
| `GroupCardEvent` | `notice` | 群名片修改 |
| `GroupNameEvent` | `notice` | 群名修改 |
| `GroupTitleEvent` | `notice` | 群头衔变更 |
| `PokeEvent` | `notice` | 戳一戳 |
| `InputStatusEvent` | `notice` | 输入状态 |
| `ProfileLikeEvent` | `notice` | 资料卡点赞 |
| `GroupGrayTipEvent` | `notice` | 群灰条消息 |
| `FriendAddEvent` | `notice` | 好友添加 |
| `FriendRecallEvent` | `notice` | 好友消息撤回 |
| `EssenceEvent` | `notice` | 精华消息变更 |
| `MsgEmojiLikeEvent` | `notice` | 消息表情点赞 |
| `BotOfflineEvent` | `notice` | Bot 掉线 |
| `OnlineFileSendEvent` | `notice` | 在线文件发送 |
| `OnlineFileReceiveEvent` | `notice` | 在线文件接收 |
| `FriendRequestEvent` | `request` | 好友请求 |
| `GroupRequestEvent` | `request` | 群请求 |

## 4.2 注册事件处理器

```python
from napcat.client import NapCatClient
from napcat.models import (
    MessageEvent,
    GroupIncreaseEvent,
    PokeEvent,
    FriendRequestEvent,
)
client = NapCatClient(...)

# ---- 方式一：client.on_message() 快捷装饰器 ----

@client.on_message()                      # 所有消息
async def on_all(event: MessageEvent):
    print(f"[{event.group_id}] {event.sender.nickname}: {event.plain_text}")

@client.on_message(group_id=123456)       # 仅指定群
async def on_group(event: MessageEvent):
    ...

@client.on_message(private=True)          # 仅私聊
async def on_private(event: MessageEvent):
    ...

# ---- 方式二：client.on() 指定事件类型 ----

@client.on(GroupIncreaseEvent)
async def on_join(event: GroupIncreaseEvent):
    await client.api.send_group_msg(
        group_id=str(event.group_id),
        message=MessageChain().at(event.user_id).text(" 欢迎!"),
    )

@client.on(PokeEvent)
async def on_poke(event: PokeEvent):
    if event.group_id:
        await client.api.send_poke(
            user_id=str(event.user_id),
            group_id=str(event.group_id),
        )

@client.on(FriendRequestEvent)
async def on_friend_req(event: FriendRequestEvent):
    await client.api.set_friend_add_request(
        flag=event.flag, approve=True,
    )

# ---- 方式三：client.on_any() 通配 ----

@client.on_any()
async def on_everything(event):
    print(f"[事件] {event.post_type}")
```

## 4.3 MessageEvent 常用属性

```python
event.message_id      # int     消息ID
event.message_seq     # int     消息序列号
event.user_id         # int     发送者QQ号
event.group_id        # int?    群号（群消息时）
event.group_name      # str?    群名
event.message_type    # str     "private"|"group"
event.sender          # Sender  发送者（.nickname, .card, .role）
event.message         # list|str 原始消息段
event.raw_message     # str     CQ码原始字符串
event.plain_text      # str     纯文本（自动提取）
event.emoji_likes_list # list  表情点赞列表

# 提取特定消息段
images = event.get_segments("image")
at_list = event.get_segments("at")
```

## 4.4 事件中间件

```python
@client.event_bus.middleware()
async def log_middleware(event, handler):
    print(f"[中间件] {type(event).__name__}")
    # 可以在这里做日志、限流、修改 event 等
```

---
