# 十二、API 参考速查表

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[高级用法](11-advanced.md)


## 12.1 ApiClient 方法清单

| 方法 | 参数 | 返回 |
|------|------|------|
| **系统** | | |
| `get_login_info()` | 无 | `LoginInfo` |
| `get_status()` | 无 | `StatusInfo` |
| `get_version_info()` | 无 | `VersionInfo` |
| `can_send_image()` | 无 | `bool` |
| `can_send_record()` | 无 | `bool` |
| `clean_cache()` | 无 | `None` |
| `set_restart()` | 无 | `None` |
| `bot_exit()` | 无 | `None` |
| `get_cookies(domain)` | domain | `Any` |
| `get_csrf_token()` | 无 | `Any` |
| `get_credentials()` | 无 | `Any` |
| `get_group_system_msg()` | 无 | `Any` |
| `get_online_clients()` | 无 | `Any` |
| **消息** | | |
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
| **好友/用户** | | |
| `get_friend_list(no_cache)` | | `list[User]` |
| `get_stranger_info(user_id)` | | `Any` |
| `get_friends_with_category()` | | `Any` |
| `set_friend_add_request(flag, approve, remark)` | | `None` |
| `set_friend_remark(user_id, remark)` | | `None` |
| `delete_friend(user_id)` | | `None` |
| `send_like(user_id, times)` | | `None` |
| `get_recent_contact()` | | `Any` |
| `get_profile_like()` | | `Any` |
| **群组** | | |
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
| `get_group_shut_list(group_id)` | | `Any` |
| `set_group_sign(group_id)` | | `None` |
| `send_group_sign(group_id)` | | `None` |
| **公告** | | |
| `get_group_notice(group_id)` | | `Any` |
| `send_group_notice(group_id, title, content)` | | `Any` |
| `del_group_notice(group_id, notice_id)` | | `None` |
| **精华** | | |
| `get_essence_msg_list(group_id)` | | `Any` |
| `set_essence_msg(message_id)` | | `None` |
| `delete_essence_msg(message_id)` | | `None` |
| **文件** | | |
| `get_image(file)` | | `FileInfo` |
| `get_record(file)` | | `FileInfo` |
| `get_file(file)` | | `FileInfo` |
| `get_group_file_url(group_id, file_id)` | | `Any` |
| `upload_group_file(group_id, file, name, folder, upload_file)` | | `Any` |
| `download_file(*, url, base64, name, headers)` | | `FileInfo` |
| `delete_group_file(group_id, file_id)` | | `None` |
| `get_group_root_files(group_id)` | | `Any` |
| `get_group_files_by_folder(group_id, folder_id)` | | `Any` |
| **扩展** | | |
| `set_online_status(status, ext_status, battery_status)` | | `None` |
| `set_qq_profile(nickname, personal_note, sex)` | | `None` |
| `set_qq_avatar(file)` | | `None` |
| `set_self_longnick(long_nick)` | | `None` |
| `ocr_image(image)` | | `Any` |
| `translate_en2zh(words)` | | `Any` |
| `send_poke(user_id, group_id, target_id)` | | `None` |
| `set_input_status(user_id, event_type)` | | `None` |
| `get_clientkey()` | | `Any` |
| `send_group_ark_share(group_id, ark_data)` | | `Any` |
| `get_mini_app_ark(app_id, app_path)` | | `Any` |
| `get_ai_record(group_id, message_id)` | | `Any` |
| `get_ai_characters()` | | `Any` |
| **频道** | | |
| `get_guild_list()` | | `Any` |
| `get_guild_service_profile(guild_id)` | | `Any` |
| **QQ 空间** | | |
| `send_qzone_msg(content, images, ugc_right, target_uins)` | | `Any` |
| `delete_qzone_msg(tid)` | | `None` |
| **闪传/在线文件** | | |
| `send_flash_msg(file_set_id, group_id, user_id)` | | `Any` |
| `send_online_file(peer_id, file_path, name)` | | `Any` |
