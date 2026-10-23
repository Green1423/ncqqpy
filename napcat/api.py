"""
API 调用层 - 统一封装 NapCatQQ 全部 WebSocket API
自动提取响应 data 字段为 Python 对象，类型安全
"""
from __future__ import annotations

from typing import Optional, Union, Any
from .models import (
    LoginInfo, StatusInfo, VersionInfo, SendMsgResult, FileInfo,
    User, Group, GroupMember, GroupFile, GroupFileFolder,
)


class ApiClient:
    """NapCatQQ API 客户端。

    所有方法自动:
    1. 发送 WebSocket action 请求
    2. 等待 echo 匹配的响应
    3. 提取 .data 字段作为 Python 对象返回
    """

    def __init__(self, ws_handler: "WsHandler"):
        self._ws = ws_handler
        self._echo_counter = 0

    def _next_echo(self) -> str:
        self._echo_counter += 1
        return f"ncpy_{self._echo_counter}"

    async def _call(self, action: str, params: dict = None) -> Any:
        """发起 API 调用，返回 data 字段"""
        echo = self._next_echo()
        payload = {"action": action, "echo": echo}
        if params:
            payload["params"] = params
        result = await self._ws.request(payload)
        if not result.ok:
            raise ApiError(result.retcode, result.message or result.wording or "未知错误")
        return result.data

    # ==================== 系统接口 ====================

    async def get_login_info(self) -> LoginInfo:
        data = await self._call("get_login_info")
        return LoginInfo(**data) if isinstance(data, dict) else data

    async def get_status(self) -> StatusInfo:
        data = await self._call("get_status")
        return StatusInfo(**data) if isinstance(data, dict) else data

    async def get_version_info(self) -> VersionInfo:
        data = await self._call("get_version_info")
        return VersionInfo(**data) if isinstance(data, dict) else data

    async def can_send_image(self) -> bool:
        return await self._call("can_send_image")

    async def can_send_record(self) -> bool:
        return await self._call("can_send_record")

    async def clean_cache(self) -> None:
        await self._call("clean_cache")

    async def set_restart(self) -> None:
        await self._call("set_restart")

    async def bot_exit(self) -> None:
        await self._call("bot_exit")

    async def get_cookies(self, domain: str = "") -> Any:
        return await self._call("get_cookies", {"domain": domain})

    async def get_csrf_token(self) -> Any:
        return await self._call("get_csrf_token")

    async def get_credentials(self) -> Any:
        return await self._call("get_credentials")

    async def get_group_system_msg(self) -> Any:
        return await self._call("get_group_system_msg")

    async def get_online_clients(self) -> Any:
        return await self._call("get_online_clients")

    # ==================== 消息接口 ====================

    async def send_msg(self, *, message_type: str = None, user_id: str = None,
                       group_id: str = None, message, auto_escape: bool = False) -> SendMsgResult:
        params = {"message": message if isinstance(message, (str, list)) else message.build()}
        if message_type: params["message_type"] = message_type
        if user_id: params["user_id"] = user_id
        if group_id: params["group_id"] = group_id
        if auto_escape: params["auto_escape"] = True
        data = await self._call("send_msg", params)
        return SendMsgResult(**data) if isinstance(data, dict) else data

    async def send_private_msg(self, user_id: str, message, auto_escape: bool = False,
                               group_id: str = None) -> SendMsgResult:
        """发送私聊消息。

        `group_id`：**群聊临时会话（小窗）**回复时必须带上来源群号
        （事件里的 `group_id`），否则服务端按普通好友私聊处理，非好友会发送失败。
        """
        params = {"user_id": user_id, "message": message if isinstance(message, (str, list)) else message.build()}
        if auto_escape: params["auto_escape"] = True
        if group_id: params["group_id"] = group_id
        data = await self._call("send_private_msg", params)
        return SendMsgResult(**data) if isinstance(data, dict) else data

    async def send_group_msg(self, group_id: str, message, auto_escape: bool = False) -> SendMsgResult:
        params = {"group_id": group_id, "message": message if isinstance(message, (str, list)) else message.build()}
        if auto_escape: params["auto_escape"] = True
        data = await self._call("send_group_msg", params)
        return SendMsgResult(**data) if isinstance(data, dict) else data

    async def delete_msg(self, message_id: int) -> None:
        await self._call("delete_msg", {"message_id": message_id})

    async def get_msg(self, message_id: int) -> Any:
        return await self._call("get_msg", {"message_id": message_id})

    async def get_forward_msg(self, *, message_id: int = None, id: str = None) -> Any:
        params = {}
        if message_id: params["message_id"] = message_id
        if id: params["id"] = id
        return await self._call("get_forward_msg", params)

    async def send_forward_msg(self, *, message_type: str = None, group_id: str = None,
                               user_id: str = None, messages: list) -> Any:
        params = {"messages": messages}
        if message_type: params["message_type"] = message_type
        if group_id: params["group_id"] = group_id
        if user_id: params["user_id"] = user_id
        return await self._call("send_forward_msg", params)

    async def mark_msg_as_read(self, *, user_id: str = None, group_id: str = None) -> None:
        params = {}
        if user_id: params["user_id"] = user_id
        if group_id: params["group_id"] = group_id
        await self._call("mark_msg_as_read", params)

    async def set_msg_emoji_like(self, message_id: int, emoji_id: str, is_add: bool = True) -> None:
        await self._call("set_msg_emoji_like", {"message_id": message_id, "emoji_id": emoji_id, "is_add": is_add})

    async def fetch_ptt_text(self, message_id: int) -> str:
        return await self._call("fetch_ptt_text", {"message_id": message_id})

    async def get_friend_msg_history(self, user_id: str) -> Any:
        return await self._call("get_friend_msg_history", {"user_id": user_id})

    async def get_group_msg_history(self, group_id: str) -> Any:
        return await self._call("get_group_msg_history", {"group_id": group_id})

    # ==================== 好友/用户接口 ====================

    async def get_friend_list(self, no_cache: bool = False) -> list[User]:
        data = await self._call("get_friend_list", {"no_cache": no_cache})
        return [User(**u) for u in data] if isinstance(data, list) else data

    async def get_stranger_info(self, user_id: str) -> Any:
        return await self._call("get_stranger_info", {"user_id": user_id})

    async def get_friends_with_category(self) -> Any:
        return await self._call("get_friends_with_category")

    async def set_friend_add_request(self, flag: str, approve: bool, remark: str = "") -> None:
        await self._call("set_friend_add_request", {"flag": flag, "approve": approve, "remark": remark})

    async def set_friend_remark(self, user_id: str, remark: str) -> None:
        await self._call("set_friend_remark", {"user_id": user_id, "remark": remark})

    async def delete_friend(self, user_id: str) -> None:
        await self._call("delete_friend", {"user_id": user_id})

    async def send_like(self, user_id: str, times: int = 1) -> None:
        await self._call("send_like", {"user_id": user_id, "times": times})

    async def get_recent_contact(self) -> Any:
        return await self._call("get_recent_contact")

    async def get_profile_like(self) -> Any:
        return await self._call("get_profile_like")

    # ==================== 群组接口 ====================

    async def get_group_list(self, no_cache: bool = False) -> list[Group]:
        data = await self._call("get_group_list", {"no_cache": no_cache})
        return [Group(**g) for g in data] if isinstance(data, list) else data

    async def get_group_info(self, group_id: str) -> Group:
        data = await self._call("get_group_info", {"group_id": group_id})
        return Group(**data) if isinstance(data, dict) else data

    async def get_group_member_list(self, group_id: str, no_cache: bool = False) -> list[GroupMember]:
        data = await self._call("get_group_member_list", {"group_id": group_id, "no_cache": no_cache})
        return [GroupMember(**m) for m in data] if isinstance(data, list) else data

    async def get_group_member_info(self, group_id: str, user_id: str, no_cache: bool = False) -> GroupMember:
        data = await self._call("get_group_member_info", {"group_id": group_id, "user_id": user_id, "no_cache": no_cache})
        return GroupMember(**data) if isinstance(data, dict) else data

    async def get_group_honor_info(self, group_id: str, type: str = "all") -> Any:
        return await self._call("get_group_honor_info", {"group_id": group_id, "type": type})

    async def set_group_ban(self, group_id: str, user_id: str, duration: int) -> None:
        await self._call("set_group_ban", {"group_id": group_id, "user_id": user_id, "duration": duration})

    async def set_group_whole_ban(self, group_id: str, enable: bool) -> None:
        await self._call("set_group_whole_ban", {"group_id": group_id, "enable": enable})

    async def set_group_admin(self, group_id: str, user_id: str, enable: bool) -> None:
        await self._call("set_group_admin", {"group_id": group_id, "user_id": user_id, "enable": enable})

    async def set_group_kick(self, group_id: str, user_id: str, reject_add_request: bool = False) -> None:
        await self._call("set_group_kick", {"group_id": group_id, "user_id": user_id, "reject_add_request": reject_add_request})

    async def set_group_leave(self, group_id: str, is_dismiss: bool = False) -> None:
        await self._call("set_group_leave", {"group_id": group_id, "is_dismiss": is_dismiss})

    async def set_group_name(self, group_id: str, group_name: str) -> None:
        await self._call("set_group_name", {"group_id": group_id, "group_name": group_name})

    async def set_group_card(self, group_id: str, user_id: str, card: str) -> None:
        await self._call("set_group_card", {"group_id": group_id, "user_id": user_id, "card": card})

    async def set_group_special_title(self, group_id: str, user_id: str, special_title: str) -> None:
        await self._call("set_group_special_title", {"group_id": group_id, "user_id": user_id, "special_title": special_title})

    async def set_group_add_request(self, flag: str, sub_type: str, approve: bool, reason: str = "") -> None:
        await self._call("set_group_add_request", {"flag": flag, "sub_type": sub_type, "approve": approve, "reason": reason})

    async def get_group_at_all_remain(self, group_id: str) -> Any:
        return await self._call("get_group_at_all_remain", {"group_id": group_id})

    async def get_group_shut_list(self, group_id: str) -> Any:
        return await self._call("get_group_shut_list", {"group_id": group_id})

    async def set_group_sign(self, group_id: str) -> None:
        await self._call("set_group_sign", {"group_id": group_id})

    async def send_group_sign(self, group_id: str) -> None:
        await self._call("send_group_sign", {"group_id": group_id})

    # ==================== 群公告 ====================

    async def get_group_notice(self, group_id: str) -> Any:
        return await self._call("_get_group_notice", {"group_id": group_id})

    async def send_group_notice(self, group_id: str, title: str, content: str) -> Any:
        return await self._call("_send_group_notice", {"group_id": group_id, "title": title, "content": content})

    async def del_group_notice(self, group_id: str, notice_id: str) -> None:
        await self._call("_del_group_notice", {"group_id": group_id, "notice_id": notice_id})

    # ==================== 精华消息 ====================

    async def get_essence_msg_list(self, group_id: str) -> Any:
        return await self._call("get_essence_msg_list", {"group_id": group_id})

    async def set_essence_msg(self, message_id: int) -> None:
        await self._call("set_essence_msg", {"message_id": message_id})

    async def delete_essence_msg(self, message_id: int) -> None:
        await self._call("delete_essence_msg", {"message_id": message_id})

    # ==================== 文件接口 ====================

    async def get_image(self, file: str) -> FileInfo:
        data = await self._call("get_image", {"file": file})
        return FileInfo(**data) if isinstance(data, dict) else data

    async def get_record(self, file: str) -> FileInfo:
        data = await self._call("get_record", {"file": file})
        return FileInfo(**data) if isinstance(data, dict) else data

    async def get_file(self, file: str) -> FileInfo:
        data = await self._call("get_file", {"file": file})
        return FileInfo(**data) if isinstance(data, dict) else data

    async def get_group_file_url(self, group_id: str, file_id: str) -> Any:
        return await self._call("get_group_file_url", {"group_id": group_id, "file_id": file_id})

    async def upload_group_file(self, group_id: str, file: str, name: str,
                                folder: str = "", upload_file: bool = False) -> Any:
        return await self._call("upload_group_file", {
            "group_id": group_id, "file": file, "name": name,
            "folder": folder, "upload_file": upload_file,
        })

    async def download_file(self, *, url: str = None, base64: str = None,
                            name: str = "", headers: str = "{}") -> FileInfo:
        params = {"name": name, "headers": headers}
        if url: params["url"] = url
        if base64: params["base64"] = base64
        data = await self._call("download_file", params)
        return FileInfo(**data) if isinstance(data, dict) else data

    async def delete_group_file(self, group_id: str, file_id: str) -> None:
        await self._call("delete_group_file", {"group_id": group_id, "file_id": file_id})

    async def get_group_root_files(self, group_id: str) -> Any:
        return await self._call("get_group_root_files", {"group_id": group_id})

    async def get_group_files_by_folder(self, group_id: str, folder_id: str) -> Any:
        return await self._call("get_group_files_by_folder", {"group_id": group_id, "folder_id": folder_id})

    # ==================== 扩展接口 ====================

    async def set_online_status(self, status: int, ext_status: int = 0, battery_status: int = 0) -> None:
        await self._call("set_online_status", {"status": status, "ext_status": ext_status, "battery_status": battery_status})

    async def set_qq_profile(self, nickname: str, personal_note: str = "", sex: str = "0") -> None:
        await self._call("set_qq_profile", {"nickname": nickname, "personal_note": personal_note, "sex": sex})

    async def set_qq_avatar(self, file: str) -> None:
        await self._call("set_qq_avatar", {"file": file})

    async def set_self_longnick(self, long_nick: str) -> None:
        await self._call("set_self_longnick", {"long_nick": long_nick})

    async def ocr_image(self, image: str) -> Any:
        return await self._call("ocr_image", {"image": image})

    async def translate_en2zh(self, words: str) -> Any:
        return await self._call("translate_en2zh", {"words": words})

    async def send_poke(self, user_id: str, group_id: str = None, target_id: str = None) -> None:
        params = {"user_id": user_id}
        if group_id: params["group_id"] = group_id
        if target_id: params["target_id"] = target_id
        await self._call("send_poke", params)

    async def set_input_status(self, user_id: str, event_type: int) -> None:
        await self._call("set_input_status", {"user_id": user_id, "event_type": event_type})

    async def get_clientkey(self) -> Any:
        return await self._call("get_clientkey")

    async def send_group_ark_share(self, group_id: str, ark_data: dict) -> Any:
        return await self._call("send_group_ark_share", {"group_id": group_id, **ark_data})

    async def get_mini_app_ark(self, app_id: str, app_path: str) -> Any:
        return await self._call("get_mini_app_ark", {"app_id": app_id, "app_path": app_path})

    async def get_ai_record(self, group_id: str, message_id: int) -> Any:
        return await self._call("get_ai_record", {"group_id": group_id, "message_id": message_id})

    async def get_ai_characters(self) -> Any:
        return await self._call("get_ai_characters")

    # ==================== 频道 ====================

    async def get_guild_list(self) -> Any:
        return await self._call("get_guild_list")

    async def get_guild_service_profile(self, guild_id: str) -> Any:
        return await self._call("get_guild_service_profile", {"guild_id": guild_id})

    # ==================== QQ 空间 ====================

    async def send_qzone_msg(self, content: str, images: list[str] = None,
                             ugc_right: int = 1, target_uins: list = None) -> Any:
        params = {"content": content, "ugc_right": ugc_right}
        if images: params["images"] = images
        if target_uins: params["target_uins"] = target_uins
        return await self._call("send_qzone_msg", params)

    async def delete_qzone_msg(self, tid: str) -> None:
        await self._call("delete_qzone_msg", {"tid": tid})

    # ==================== 闪传 / 在线文件 ====================

    async def send_flash_msg(self, file_set_id: str, group_id: str = None, user_id: str = None) -> Any:
        params = {"fileSetId": file_set_id}
        if group_id: params["group_id"] = group_id
        if user_id: params["user_id"] = user_id
        return await self._call("send_flash_msg", params)

    async def send_online_file(self, peer_id: str, file_path: str, name: str) -> Any:
        return await self._call("send_online_file", {"peer_id": peer_id, "file": file_path, "name": name})


class ApiError(Exception):
    """OneBot API 调用失败（服务端返回错误或连接不可用）。"""
    def __init__(self, retcode: int, message: str):
        self.retcode = retcode
        self.message = message
        super().__init__(f"[{retcode}] {message}")
