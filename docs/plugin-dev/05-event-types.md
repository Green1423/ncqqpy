# 五、全部事件类型

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[命令注册](04-commands.md) · 下一章：[全部 API 方法](06-api-methods.md)


| 事件类 | post_type | notice_type | 触发场景 |
|--------|-----------|-------------|----------|
| `HeartbeatEvent` | meta_event | — | 定时心跳 |
| `LifeCycleEvent` | meta_event | — | 连接/启用/禁用 |
| `MessageEvent` | message | — | 接收私聊/群聊消息 |
| `GroupIncreaseEvent` | notice | group_increase | 群成员增加 |
| `GroupDecreaseEvent` | notice | group_decrease | 群成员减少 |
| `GroupBanEvent` | notice | group_ban | 群禁言/解禁 |
| `GroupAdminEvent` | notice | group_admin | 管理员设置/取消 |
| `GroupRecallEvent` | notice | group_recall | 群消息撤回 |
| `GroupUploadEvent` | notice | group_upload | 群文件上传 |
| `GroupCardEvent` | notice | group_card | 群名片修改 |
| `GroupNameEvent` | notice | group_name | 群名修改 |
| `GroupTitleEvent` | notice | title | 群头衔变更 |
| `PokeEvent` | notice | poke | 戳一戳 |
| `InputStatusEvent` | notice | input_status | 对方正在输入 |
| `ProfileLikeEvent` | notice | profile_like | 资料卡点赞 |
| `GroupGrayTipEvent` | notice | gray_tip | 群灰条消息 |
| `FriendAddEvent` | notice | friend_add | 好友添加 |
| `FriendRecallEvent` | notice | friend_recall | 好友消息撤回 |
| `EssenceEvent` | notice | essence | 精华消息变更 |
| `MsgEmojiLikeEvent` | notice | group_msg_emoji_like | 消息表情点赞 |
| `BotOfflineEvent` | notice | bot_offline | Bot 掉线 |
| `OnlineFileSendEvent` | notice | online_file_send | 在线文件发送 |
| `OnlineFileReceiveEvent` | notice | online_file_receive | 在线文件接收 |
| `FriendRequestEvent` | request | — | 好友请求 |
| `GroupRequestEvent` | request | — | 群请求/邀请 |

## 5.1 MessageEvent 常用属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `message_id` | int | 消息 ID |
| `user_id` | int | 发送者 QQ |
| `group_id` | int? | 群号（群消息） |
| `message_type` | str | `"private"` / `"group"` |
| `sender.nickname` | str | 发送者昵称 |
| `sender.card` | str? | 群名片 |
| `sender.role` | str? | `"owner"` / `"admin"` / `"member"` |
| `plain_text` | str | 纯文本（自动提取；**首尾空白保留原样**，自行比较前先 `normalize_text()`）|
| `raw_message` | str | CQ 码原始字符串 |
| `message` | list/str | 原始消息段数组 |
| `get_segments(type)` | list[dict] | 提取指定类型消息段 |

---
