# 六、全部 API 方法

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[全部事件类型](05-event-types.md) · 下一章：[权限管理](07-permissions.md)

> 下面只列常用方法；完整覆盖（178 个 action）与字段名对照见 [NapCat API 覆盖表](../reference/napcat-api.md)。


## 6.1 消息

| 方法 | 参数 | 返回 |
|------|------|------|
| `send_msg(*, message_type, user_id, group_id, message, auto_escape)` | | `SendMsgResult` |
| `send_private_msg(user_id, message, auto_escape)` | | `SendMsgResult` |
| `send_group_msg(group_id, message, auto_escape)` | | `SendMsgResult` |
| `delete_msg(message_id)` | | `None` |
| `get_msg(message_id)` | | `Any` |
| `get_forward_msg(*, message_id, id)` | | `Any` |
| `send_forward_msg(*, message_type, group_id, user_id, messages)` | | `Any` |
| `mark_msg_as_read(*, user_id, group_id)` | | `None` |
| `set_msg_emoji_like(message_id, emoji_id, is_add)` | | `None` |
| `fetch_ptt_text(message_id)` | | `str` |
| `get_friend_msg_history(user_id)` | | `Any` |
| `get_group_msg_history(group_id)` | | `Any` |

## 6.2 好友/用户

| 方法 | 参数 | 返回 |
|------|------|------|
| `get_friend_list(no_cache)` | | `list[User]` |
| `get_stranger_info(user_id)` | | `Any` |
| `get_friends_with_category()` | | `Any` |
| `set_friend_add_request(flag, approve, remark)` | | `None` |
| `set_friend_remark(user_id, remark)` | | `None` |
| `delete_friend(user_id)` | | `None` |
| `send_like(user_id, times)` | | `None` |
| `get_recent_contact()` | | `Any` |
| `get_profile_like()` | | `Any` |

## 6.3 群组

| 方法 | 参数 | 返回 |
|------|------|------|
| `get_group_list(no_cache)` | | `list[Group]` |
| `get_group_info(group_id)` | | `Group` |
| `get_group_member_list(group_id, no_cache)` | | `list[GroupMember]` |
| `get_group_member_info(group_id, user_id, no_cache)` | | `GroupMember` |
| `get_group_honor_info(group_id, type)` | | `Any` |
| `set_group_ban(group_id, user_id, duration)` | | `None` |
| `set_group_whole_ban(group_id, enable)` | | `None` |
| `set_group_admin(group_id, user_id, enable)` | | `None` |
| `set_group_kick(group_id, user_id, reject_add_request)` | | `None` |
| `set_group_leave(group_id, is_dismiss)` | | `None` |
| `set_group_name(group_id, group_name)` | | `None` |
| `set_group_card(group_id, user_id, card)` | | `None` |
| `set_group_special_title(group_id, user_id, special_title)` | | `None` |
| `set_group_add_request(flag, sub_type, approve, reason)` | | `None` |
| `get_group_at_all_remain(group_id)` | | `Any` |
| `set_group_sign(group_id)` | | `None` |
| `send_group_sign(group_id)` | | `None` |

## 6.4 文件

文件类 action **不在 `ApiClient` 的公开接口里**：统一由 `FileManager`（插件里的
`self.files` / 客户端上的 `client.files`）承载，避免插件直接拼文件 action 参数。
可用方法见 [文件管理](11-files.md)：`get_image` / `get_record` / `get_file` /
`download_*` / `upload_*` / 群文件 / 闪传 / 在线文件 / `clean_temp`。

## 6.5 系统/扩展

| 方法 | 参数 | 返回 |
|------|------|------|
| `get_login_info()` | | `LoginInfo` |
| `get_status()` | | `StatusInfo` |
| `get_version_info()` | | `VersionInfo` |
| `can_send_image()` | | `bool` |
| `can_send_record()` | | `bool` |
| `get_cookies(domain)` | | `Any` |
| `set_online_status(status, ext_status, battery_status)` | | `None` |
| `set_qq_profile(nickname, personal_note, sex)` | | `None` |
| `set_qq_avatar(file)` | | `None` |
| `ocr_image(image)` | | `Any` |
| `translate_en2zh(words)` | | `Any` |
| `send_poke(user_id, group_id, target_id)` | | `None` |
| `send_qzone_msg(content, images, ugc_right, target_uins)` | | `Any` |
| `delete_qzone_msg(tid)` | | `None` |

## 6.6 错误处理

```python
from napcat.api import ApiError
try:
    await self.api.set_group_ban("123", "456", 600)
except ApiError as e:
    self.logger.error(f"API 错误 [{e.retcode}]: {e.message}")
```

> **上报节流**：普通 API 调用发送前统一等待 `api_delay`（默认 0.5–1.0s）并串行化
> （流式分块不节流），插件不需要自己 sleep 限速；反过来说，**别在一个事件里
> 同步连发上百个 API** —— 它们会排队，每个仍要等 0.5–1s。
> **断线期间**调用 API **不会立即失败**：框架把调用排队留住，服务器一上线就按序补发
> （`offline_hold_timeout` 控制最长等待，默认 0 = 一直等；`offline_queue_max` 控制排队上限）。
> 只有**已经发出但没等到响应**的调用会立即抛 `ConnectionError`（不自动重发，避免重复消息），
> 这类失败按业务自行重试即可。

---
