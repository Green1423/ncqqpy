# NapCat API 覆盖说明

> 文档索引：[docs/README.md](../README.md) · 上级：[参考文档](README.md)

`napcat/api.py` 的公开方法覆盖 [NapCat 官方接口文档](https://napcat.apifox.cn/llms.txt) 的全部 **178 个 action**。字段名与官方一致（如 `set`、`longNick`、`fileset_id`、`file_path`、`character`）；`tests/test_api_coverage.py` 会检查覆盖率与易错字段，接口增删时同步更新该测试。

调用方式：`await self.api.<方法>(...)`；返回值是响应里的 `data` 字段（失败抛 `ApiError`）。

## AI

| 方法 | action | 说明 |
|------|--------|------|
| `get_ai_record` | `get_ai_record` | 获取 AI 语音 |
| `send_group_ai_record` | `send_group_ai_record` | 发送群 AI 语音 |

## Go-CQHTTP

| 方法 | action | 说明 |
|------|--------|------|
| `handle_quick_operation` | `.handle_quick_operation` | 处理快速操作 |
| `get_model_show` | `_get_model_show` | 获取机型显示 |
| `send_group_notice` | `_send_group_notice` | 发送群公告 |
| `set_model_show` | `_set_model_show` | 设置机型 |
| `check_url_safely` | `check_url_safely` | 检查URL安全性 |
| `create_group_file_folder` | `create_group_file_folder` | 创建群文件目录 |
| `delete_friend` | `delete_friend` | 删除好友 |
| `delete_group_file` | `delete_group_file` | 删除群文件 |
| `delete_group_folder` | `delete_group_folder` | 删除群文件目录 |
| `download_file` | `download_file` | 下载文件 |
| `get_forward_msg` | `get_forward_msg` | 获取合并转发消息 |
| `get_friend_msg_history` | `get_friend_msg_history` | 获取好友历史消息 |
| `get_group_at_all_remain` | `get_group_at_all_remain` | 获取群艾特全体剩余次数 |
| `get_group_file_system_info` | `get_group_file_system_info` | 获取群文件系统信息 |
| `get_group_files_by_folder` | `get_group_files_by_folder` | 获取群文件夹文件列表 |
| `get_group_honor_info` | `get_group_honor_info` | 获取群荣誉信息 |
| `get_group_msg_history` | `get_group_msg_history` | 获取群历史消息 |
| `get_group_root_files` | `get_group_root_files` | 获取群根目录文件列表 |
| `get_online_clients` | `get_online_clients` | 获取在线客户端 |
| `get_stranger_info` | `get_stranger_info` | 获取陌生人信息 |
| `send_forward_msg` | `send_forward_msg` | 发送合并转发消息 |
| `send_group_forward_msg` | `send_group_forward_msg` | 发送群合并转发消息 |
| `send_private_forward_msg` | `send_private_forward_msg` | 发送私聊合并转发消息 |
| `set_group_portrait` | `set_group_portrait` | 设置群头像 |
| `set_qq_profile` | `set_qq_profile` | 设置QQ资料 |
| `upload_group_file` | `upload_group_file` | 上传群文件 |
| `upload_private_file` | `upload_private_file` | 上传私聊文件 |

## 扩展接口

| 方法 | action | 说明 |
|------|--------|------|
| `ocr_image_internal` | `.ocr_image` | 图片 OCR 识别 (内部) |
| `create_collection` | `create_collection` | 创建收藏 |
| `delete_qzone_msg` | `delete_qzone_msg` | 删除QQ空间说说 |
| `get_ai_characters` | `get_ai_characters` | 获取AI角色列表 |
| `get_clientkey` | `get_clientkey` | 获取ClientKey |
| `ocr_image` | `ocr_image` | 图片 OCR 识别 |
| `send_qzone_msg` | `send_qzone_msg` | 发表QQ空间说说 |
| `set_group_kick_members` | `set_group_kick_members` | 批量踢出群成员 |
| `set_group_special_title` | `set_group_special_title` | 设置专属头衔 |
| `set_qq_avatar` | `set_qq_avatar` | 设置QQ头像 |
| `set_self_longnick` | `set_self_longnick` | 设置个性签名 |
| `translate_en2zh` | `translate_en2zh` | 英文单词翻译 |

## 文件扩展

| 方法 | action | 说明 |
|------|--------|------|
| `cancel_online_file` | `cancel_online_file` | 取消在线文件 |
| `create_flash_task` | `create_flash_task` | 创建闪传任务 |
| `download_fileset` | `download_fileset` | 下载文件集 |
| `get_fileset_id` | `get_fileset_id` | 获取文件集 ID |
| `get_fileset_info` | `get_fileset_info` | 获取文件集信息 |
| `get_flash_file_list` | `get_flash_file_list` | 获取闪传文件列表 |
| `get_flash_file_url` | `get_flash_file_url` | 获取闪传文件链接 |
| `get_online_file_msg` | `get_online_file_msg` | 获取在线文件消息 |
| `get_share_link` | `get_share_link` | 获取文件分享链接 |
| `move_group_file` | `move_group_file` | 移动群文件 |
| `receive_online_file` | `receive_online_file` | 接收在线文件 |
| `refuse_online_file` | `refuse_online_file` | 拒绝在线文件 |
| `rename_group_file` | `rename_group_file` | 重命名群文件 |
| `send_flash_msg` | `send_flash_msg` | 发送闪传消息 |
| `send_online_file` | `send_online_file` | 发送在线文件 |
| `send_online_folder` | `send_online_folder` | 发送在线文件夹 |
| `trans_group_file` | `trans_group_file` | 传输群文件 |

## 文件接口

| 方法 | action | 说明 |
|------|--------|------|
| `get_file` | `get_file` | 获取文件 |
| `get_group_file_url` | `get_group_file_url` | 获取群文件URL |
| `get_image` | `get_image` | 获取图片 |
| `get_private_file_url` | `get_private_file_url` | 获取私聊文件URL |
| `get_record` | `get_record` | 获取语音 |

## 核心接口

| 方法 | action | 说明 |
|------|--------|------|
| `cancel_group_todo` | `cancel_group_todo` | 取消群待办 |
| `complete_group_todo` | `complete_group_todo` | 完成群待办 |
| `friend_poke` | `friend_poke` | 发送戳一戳 |
| `group_poke` | `group_poke` | 发送戳一戳 |
| `send_poke` | `send_poke` | 发送戳一戳 |
| `set_group_todo` | `set_group_todo` | 设置群待办 |

## 流式传输扩展

| 方法 | action | 说明 |
|------|--------|------|
| `clean_stream_temp_file` | `clean_stream_temp_file` | 清理流式传输临时文件 |
| `download_file_image_stream` | `download_file_image_stream` | 下载图片文件流 |
| `download_file_record_stream` | `download_file_record_stream` | 下载语音文件流 |
| `test_download_stream` | `test_download_stream` | 测试下载流 |

## 流式接口

| 方法 | action | 说明 |
|------|--------|------|
| `download_file_stream` | `download_file_stream` | 下载文件流 |
| `upload_file_stream` | `upload_file_stream` | 上传文件流 |

## 消息扩展

| 方法 | action | 说明 |
|------|--------|------|
| `ark_share_group` | `ArkShareGroup` | 分享群 (Ark) |
| `ark_share_peer` | `ArkSharePeer` | 分享用户 (Ark) |
| `click_inline_keyboard_button` | `click_inline_keyboard_button` | 点击内联键盘按钮 |
| `fetch_emoji_like` | `fetch_emoji_like` | 获取表情点赞详情 |
| `fetch_ptt_text` | `fetch_ptt_text` | 获取语音转文字结果 |
| `get_emoji_likes` | `get_emoji_likes` | 获取消息表情点赞列表 |
| `send_ark_share` | `send_ark_share` | 分享用户 (Ark) |
| `send_group_ark_share` | `send_group_ark_share` | 分享群 (Ark) |
| `set_msg_emoji_like` | `set_msg_emoji_like` | 设置消息表情点赞 |

## 消息接口

| 方法 | action | 说明 |
|------|--------|------|
| `mark_all_as_read` | `_mark_all_as_read` | 标记所有消息已读 |
| `delete_msg` | `delete_msg` | 撤回消息 |
| `forward_friend_single_msg` | `forward_friend_single_msg` | 转发单条消息 |
| `forward_group_single_msg` | `forward_group_single_msg` | 转发单条消息 |
| `get_msg` | `get_msg` | 获取消息 |
| `mark_group_msg_as_read` | `mark_group_msg_as_read` | 标记群聊已读 |
| `mark_msg_as_read` | `mark_msg_as_read` | 标记消息已读 (Go-CQHTTP) |
| `mark_private_msg_as_read` | `mark_private_msg_as_read` | 标记私聊已读 |
| `send_msg` | `send_msg` | 发送消息 |
| `send_private_msg` | `send_private_msg` | 发送私聊消息 |

## 用户扩展

| 方法 | action | 说明 |
|------|--------|------|
| `get_friends_with_category` | `get_friends_with_category` | 获取带分组的好友列表 |
| `get_profile_like` | `get_profile_like` | 获取资料点赞 |
| `get_unidirectional_friend_list` | `get_unidirectional_friend_list` | 获取单向好友列表 |
| `set_diy_online_status` | `set_diy_online_status` | 设置自定义在线状态 |

## 用户接口

| 方法 | action | 说明 |
|------|--------|------|
| `get_cookies` | `get_cookies` | 获取 Cookies |
| `get_friend_list` | `get_friend_list` | 获取好友列表 |
| `get_recent_contact` | `get_recent_contact` | 获取最近会话 |
| `send_like` | `send_like` | 点赞 |
| `set_friend_add_request` | `set_friend_add_request` | 处理加好友请求 |
| `set_friend_remark` | `set_friend_remark` | 设置好友备注 |

## 系统扩展

| 方法 | action | 说明 |
|------|--------|------|
| `add_custom_face` | `add_custom_face` | 添加自定义表情 |
| `bot_exit` | `bot_exit` | 退出登录 |
| `delete_custom_face` | `delete_custom_face` | 删除自定义表情 |
| `fetch_custom_face` | `fetch_custom_face` | 获取自定义表情 |
| `fetch_custom_face_detail` | `fetch_custom_face_detail` | 获取自定义表情详情 |
| `get_collection_list` | `get_collection_list` | 获取收藏列表 |
| `get_mini_app_ark` | `get_mini_app_ark` | 获取小程序 Ark |
| `get_rkey` | `get_rkey` | 获取扩展 RKey |
| `get_rkey_server` | `get_rkey_server` | 获取 RKey 服务器 |
| `get_robot_uin_range` | `get_robot_uin_range` | 获取机器人 UIN 范围 |
| `nc_get_rkey` | `nc_get_rkey` | 获取 RKey |
| `nc_get_user_status` | `nc_get_user_status` | 获取用户在线状态 |
| `send_packet` | `send_packet` | 发送原始数据包 |
| `set_custom_face_desc` | `set_custom_face_desc` | 修改自定义表情描述 |
| `set_input_status` | `set_input_status` | 设置输入状态 |
| `set_online_status` | `set_online_status` | 设置在线状态 |

## 系统接口

| 方法 | action | 说明 |
|------|--------|------|
| `can_send_image` | `can_send_image` | 是否可以发送图片 |
| `can_send_record` | `can_send_record` | 是否可以发送语音 |
| `clean_cache` | `clean_cache` | 清理缓存 |
| `get_credentials` | `get_credentials` | 获取登录凭证 |
| `get_csrf_token` | `get_csrf_token` | 获取 CSRF Token |
| `get_doubt_friends_add_request` | `get_doubt_friends_add_request` | 获取可疑好友申请 |
| `get_group_system_msg` | `get_group_system_msg` | 获取群系统消息 |
| `get_login_info` | `get_login_info` | 获取登录号信息 |
| `get_status` | `get_status` | 获取运行状态 |
| `get_version_info` | `get_version_info` | 获取版本信息 |
| `nc_get_packet_status` | `nc_get_packet_status` | 获取Packet状态 |
| `set_doubt_friends_add_request` | `set_doubt_friends_add_request` | 处理可疑好友申请 |
| `set_restart` | `set_restart` | 重启服务 |

## 群组扩展

| 方法 | action | 说明 |
|------|--------|------|
| `cancel_group_album_media_like` | `cancel_group_album_media_like` | 取消点赞群相册媒体 |
| `del_group_album_media` | `del_group_album_media` | 删除群相册媒体 |
| `do_group_album_comment` | `do_group_album_comment` | 发表群相册评论 |
| `get_group_album_media_list` | `get_group_album_media_list` | 获取群相册媒体列表 |
| `get_group_info_ex` | `get_group_info_ex` | 获取群详细信息 (扩展) |
| `get_group_share_link` | `get_group_share_link` | 获取群分享链接 |
| `get_group_signed_list` | `get_group_signed_list` | 获取群组今日打卡列表 |
| `get_qun_album_list` | `get_qun_album_list` | 获取群相册列表 |
| `send_group_sign` | `send_group_sign` | 群打卡 |
| `set_group_add_option` | `set_group_add_option` | 设置群加群选项 |
| `set_group_album_media_like` | `set_group_album_media_like` | 点赞群相册媒体 |
| `set_group_remark` | `set_group_remark` | 设置群备注 |
| `set_group_robot_add_option` | `set_group_robot_add_option` | 设置群机器人加群选项 |
| `set_group_search` | `set_group_search` | 设置群搜索选项 |
| `set_group_sign` | `set_group_sign` | 群打卡 |
| `upload_image_to_qun_album` | `upload_image_to_qun_album` | 上传图片到群相册 |
| `upload_images_to_qun_album` | `upload_images_to_qun_album` | 上传多张图片到群相册 |
| `upload_video_to_qun_album` | `upload_video_to_qun_album` | 上传视频到群相册 |

## 群组接口

| 方法 | action | 说明 |
|------|--------|------|
| `del_group_notice` | `_del_group_notice` | 删除群公告 |
| `get_group_notice` | `_get_group_notice` | 获取群公告 |
| `delete_essence_msg` | `delete_essence_msg` | 移出精华消息 |
| `get_essence_msg_list` | `get_essence_msg_list` | 获取群精华消息 |
| `get_group_detail_info` | `get_group_detail_info` | 获取群详细信息 |
| `get_group_ignore_add_request` | `get_group_ignore_add_request` | 获取群被忽略的加群请求 |
| `get_group_ignored_notifies` | `get_group_ignored_notifies` | 获取群忽略通知 |
| `get_group_info` | `get_group_info` | 获取群信息 |
| `get_group_list` | `get_group_list` | 获取群列表 |
| `get_group_member_info` | `get_group_member_info` | 获取群成员信息 |
| `get_group_member_list` | `get_group_member_list` | 获取群成员列表 |
| `get_group_shut_list` | `get_group_shut_list` | 获取群禁言列表 |
| `send_group_msg` | `send_group_msg` | 发送群消息 |
| `set_essence_msg` | `set_essence_msg` | 设置精华消息 |
| `set_group_add_request` | `set_group_add_request` | 处理加群请求 |
| `set_group_admin` | `set_group_admin` | 设置群管理员 |
| `set_group_ban` | `set_group_ban` | 群组禁言 |
| `set_group_card` | `set_group_card` | 设置群名片 |
| `set_group_kick` | `set_group_kick` | 群组踢人 |
| `set_group_leave` | `set_group_leave` | 退出群组 |
| `set_group_member_invite_policy` | `set_group_member_invite_policy` | 设置群成员邀请策略 |
| `set_group_member_permissions` | `set_group_member_permissions` | 设置群成员功能权限 |
| `set_group_name` | `set_group_name` | 设置群名称 |
| `set_group_new_member_history_visibility` | `set_group_new_member_history_visibility` | 设置新成员历史消息可见性 |
| `set_group_whole_ban` | `set_group_whole_ban` | 全员禁言 |

## 频道接口

| 方法 | action | 说明 |
|------|--------|------|
| `get_guild_list` | `get_guild_list` | 获取频道列表 |
| `get_guild_service_profile` | `get_guild_service_profile` | 获取频道个人信息 |

