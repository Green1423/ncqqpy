"""
API 调用层 - 统一封装 NapCatQQ 全部 WebSocket API
自动提取响应 data 字段为 Python 对象，类型安全
"""
from __future__ import annotations

import base64
import hashlib
import math
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional, Union, Any

import aiofiles

from .models import (
    LoginInfo, StatusInfo, VersionInfo, SendMsgResult, FileInfo,
    User, Group, GroupMember, GroupFile, GroupFileFolder,
)


#: `SentLog` 里每条记录保留多久（秒）—— 引用一般发生在几分钟内
SENT_LOG_TTL = 6 * 3600
#: 同一次「连续发送」的判定窗口（秒）：插件把一条长消息切成几段发，间隔很小
SENT_LOG_GAP = 8.0


class SentLog:
    """机器人自己发出的消息：`message_id → {text, images, target, ts}`。

    **为什么框架层做**：`message_id` 由服务端分配，**不保证递增**（用户实测过），
    所以不能靠 `id±1` 找「同一条推送的下一段」。而发送方在发送那一刻就知道
    完整内容，顺手记下最可靠。

    分段还原靠**发送顺序 + 时间间隔**：同一目标、间隔 < `SENT_LOG_GAP` 的连续几条
    视为一次推送的若干段（`block()`）。
    """

    def __init__(self, path: str = "", *, logger=None, max_entries: int = 600) -> None:
        self.path = str(path or "")
        self.logger = logger
        self.max_entries = max(1, int(max_entries))
        self._data: dict[str, dict] = {}
        self._order: list[str] = []          # 发送顺序（id，保留重复则按插入）
        if self.path:
            self._load()

    # ---- 落盘 ----

    def _load(self) -> None:
        import json
        import os
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                got = json.load(f)
            if isinstance(got, dict):
                self._data = {str(k): v for k, v in got.items() if isinstance(v, dict)}
                self._order = [str(x) for x in (got.get("__order__") or []) if str(x) in self._data]
        except (OSError, ValueError):
            self._data, self._order = {}, []

    def save(self) -> None:
        import json
        import os
        if not self.path:
            return
        while len(self._data) > self.max_entries:
            old = self._order.pop(0) if self._order else next(iter(self._data))
            self._data.pop(old, None)
        payload = dict(self._data)
        payload["__order__"] = self._order[-self.max_entries:]
        tmp = self.path + ".tmp"
        try:
            import os as _os
            parent = _os.path.dirname(self.path)
            if parent:
                _os.makedirs(parent, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            if self.logger:
                self.logger.warning("[api] 发出消息日志落盘失败: %r", e)

    # ---- 记录 ----

    @staticmethod
    def _parse_message(message) -> tuple[str, list]:
        """把消息段解析成 `(纯文本, [图片地址])`。"""
        if isinstance(message, str):
            return message, []
        text: list[str] = []
        images: list[str] = []
        for seg in message or []:
            if not isinstance(seg, dict):
                continue
            stype = str(seg.get("type") or "")
            data = seg.get("data") or {}
            if stype == "text":
                text.append(str(data.get("text") or ""))
            elif stype == "image":
                url = str(data.get("url") or data.get("file") or "")
                if url:
                    images.append(url)
        return "".join(text), images

    def remember(self, action: str, params: dict, data) -> None:
        import time
        mid = 0
        if isinstance(data, dict):
            for key in ("message_id", "msg_id", "id"):
                try:
                    mid = int(data.get(key) or 0)
                except (TypeError, ValueError):
                    mid = 0
                if mid:
                    break
        if not mid:
            return
        text, images = self._parse_message(params.get("message"))
        target = str(params.get("user_id") or params.get("group_id") or "")
        self._data[str(mid)] = {"text": text, "images": images,
                                "target": target, "ts": time.time(),
                                "kind": action}
        self._order.append(str(mid))
        self.save()

    # ---- 查询 ----

    def get(self, message_id: int) -> dict:
        return dict(self._data.get(str(int(message_id or 0))) or {})

    def block(self, message_id: int, *, max_parts: int = 8,
              gap: float = SENT_LOG_GAP) -> dict:
        """把**同一次推送被切成的几段**拼起来：`{"text":…, "images":[…]}`。

        判定：同目标、间隔 < `gap`、连续出现在发送顺序里。与 `message_id`
        是否递增**无关**（服务端分配的 id 不保证递增，这一点用户实测确认过）。
        """
        key = str(int(message_id or 0))
        rec = self._data.get(key)
        if not rec:
            return {}
        try:
            idx = self._order.index(key)
        except ValueError:
            return {"text": rec.get("text", ""),
                    "images": list(rec.get("images") or [])}
        lo = hi = idx
        while lo - 1 >= 0 and idx - lo < max_parts - 1:
            prev = self._data.get(self._order[lo - 1]) or {}
            if not prev or prev.get("target") != rec.get("target") \
                    or abs(float(rec.get("ts") or 0) - float(prev.get("ts") or 0)) > gap:
                break
            lo -= 1
        while hi + 1 < len(self._order) and hi - idx < max_parts - 1:
            nxt = self._data.get(self._order[hi + 1]) or {}
            if not nxt or nxt.get("target") != rec.get("target") \
                    or abs(float(nxt.get("ts") or 0) - float(rec.get("ts") or 0)) > gap:
                break
            hi += 1
        texts: list[str] = []
        images: list[str] = []
        for i in range(lo, hi + 1):
            part = self._data.get(self._order[i]) or {}
            t = str(part.get("text") or "")
            if t:
                texts.append(t)
            for url in part.get("images") or []:
                if url not in images:
                    images.append(url)
        return {"text": "\n\n".join(texts), "images": images}


class _NullSentLog:
    """没有注入日志时的占位（测试、独立脚本）。"""

    def remember(self, action, params, data) -> None:  # noqa: D401 - 空实现
        return None


class ApiClient:
    """NapCatQQ API 客户端。

    所有方法自动:
    1. 发送 WebSocket action 请求
    2. 等待 echo 匹配的响应
    3. 提取 .data 字段作为 Python 对象返回
    """

    def __init__(self, ws_handler: "WsHandler", *, sent_log=None):
        self._ws = ws_handler
        self._echo_counter = 0
        #: 自己发出的消息日志（`SentLog`；框架启动时注入，测试里可为 None）
        self.sent_log = sent_log or _NullSentLog()

    def _next_echo(self) -> str:
        self._echo_counter += 1
        return f"ncpy_{self._echo_counter}"

    async def _call(self, action: str, params: dict = None, *,
                    throttle: bool = True) -> Any:
        """发起 API 调用，返回 data 字段。

        Args:
            action: OneBot action 名。
            params: 请求参数。
            throttle: 是否走 `api_delay` 上报节流（分块传输时关掉）。
        """
        echo = self._next_echo()
        payload = {"action": action, "echo": echo}
        if params:
            payload["params"] = params
        result = await self._ws.request(payload, throttle=throttle)
        if not result.ok:
            raise ApiError(result.retcode, result.message or result.wording or "未知错误")
        # **机器人自己发出去的消息**：记下 id → 内容（文本 + 图片）。
        # 为什么必须在框架层记：`message_id` 由服务端分配，**不保证递增**，
        # 也不能靠 id±1 找相邻段；而发送方（任何插件）这一层刚好知道完整内容。
        # 用户引用其中一段时，`ai_chat` 靠这份日志还原整条推送（见 docs §17）。
        if action in ("send_private_msg", "send_group_msg", "send_msg"):
            try:
                self.sent_log.remember(action, params or {}, result.data)
            except Exception:  # noqa: BLE001 - 记录失败绝不能影响发送
                pass
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

    async def get_credentials(self, domain: str = "") -> Any:
        """获取登录凭证（cookies + csrf token）。

        Args:
            domain: 目标域名（官方文档标为必填），如 `qun.qq.com`。
        """
        params = {"domain": domain} if domain else {}
        return await self._call("get_credentials", params)

    async def get_group_system_msg(self, count: int = None) -> Any:
        params = {} if count is None else {"count": count}
        return await self._call("get_group_system_msg", params)

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

    async def send_forward_msg(self, *, message: list = None, messages: list = None,
                               message_type: str = None, group_id: str = None,
                               user_id: str = None, auto_escape: bool = False,
                               source: str = None, news: list = None,
                               summary: str = None, prompt: str = None,
                               timeout: int = None) -> Any:
        """发送合并转发消息。

        Args:
            message: 节点数组（官方字段名）。
            messages: `message` 的旧别名。
            message_type: 目标类型（private/group）。
            group_id: 群号（群聊）。
            user_id: 用户 QQ（私聊）。
            auto_escape: 是否按纯文本发送。
            source: 合并转发来源。
            news: 合并转发新闻。
            summary: 合并转发摘要。
            prompt: 合并转发提示。
            timeout: 自定义发送超时（毫秒）。

        Raises:
            ValueError: 没有提供 `message` / `messages`。
        """
        payload = message if message is not None else messages
        if payload is None:
            raise ValueError("send_forward_msg 需要 message（节点数组）")
        params = {"message": payload}
        if message_type: params["message_type"] = message_type
        if group_id: params["group_id"] = group_id
        if user_id: params["user_id"] = user_id
        if auto_escape: params["auto_escape"] = True
        if source: params["source"] = source
        if news: params["news"] = news
        if summary: params["summary"] = summary
        if prompt: params["prompt"] = prompt
        if timeout is not None: params["timeout"] = timeout
        return await self._call("send_forward_msg", params)

    async def mark_msg_as_read(self, message_id: int = None, *,
                               user_id: str = None, group_id: str = None) -> None:
        params = {}
        if message_id is not None: params["message_id"] = message_id
        if user_id: params["user_id"] = user_id
        if group_id: params["group_id"] = group_id
        await self._call("mark_msg_as_read", params)

    async def set_msg_emoji_like(self, message_id: int, emoji_id: str,
                                 set: bool = True, *, is_add: bool = None) -> None:
        """给消息贴表情 / 取消。

        Args:
            message_id: 消息 ID。
            emoji_id: 表情 ID。
            set: `True` 贴表情，`False` 取消（官方字段名）。
            is_add: `set` 的旧别名（兼容）。
        """
        value = set if is_add is None else is_add
        await self._call("set_msg_emoji_like",
                         {"message_id": message_id, "emoji_id": emoji_id,
                          "set": value})

    async def fetch_ptt_text(self, message_id: int) -> str:
        return await self._call("fetch_ptt_text", {"message_id": message_id})

    async def get_friend_msg_history(self, user_id: str, *, count: int = None,
                                     message_seq: int = None,
                                     reverse_order: bool = False,
                                     parse_mult_msg: bool = False,
                                     quick_reply: bool = False,
                                     disable_get_url: bool = False) -> Any:
        params = {"user_id": user_id}
        if count is not None: params["count"] = count
        if message_seq is not None: params["message_seq"] = message_seq
        if reverse_order: params["reverseOrder"] = True
        if parse_mult_msg: params["parse_mult_msg"] = True
        if quick_reply: params["quick_reply"] = True
        if disable_get_url: params["disable_get_url"] = True
        return await self._call("get_friend_msg_history", params)

    async def get_group_msg_history(self, group_id: str, *, count: int = None,
                                    message_seq: int = None,
                                    reverse_order: bool = False,
                                    parse_mult_msg: bool = False,
                                    quick_reply: bool = False,
                                    disable_get_url: bool = False) -> Any:
        params = {"group_id": group_id}
        if count is not None: params["count"] = count
        if message_seq is not None: params["message_seq"] = message_seq
        if reverse_order: params["reverseOrder"] = True
        if parse_mult_msg: params["parse_mult_msg"] = True
        if quick_reply: params["quick_reply"] = True
        if disable_get_url: params["disable_get_url"] = True
        return await self._call("get_group_msg_history", params)

    # ==================== 好友/用户接口 ====================

    async def get_friend_list(self, no_cache: bool = False) -> list[User]:
        data = await self._call("get_friend_list", {"no_cache": no_cache})
        return [User(**u) for u in data] if isinstance(data, list) else data

    async def get_stranger_info(self, user_id: str, no_cache: bool = False) -> Any:
        return await self._call("get_stranger_info",
                                {"user_id": user_id, "no_cache": no_cache})

    async def get_friends_with_category(self) -> Any:
        return await self._call("get_friends_with_category")

    async def set_friend_add_request(self, flag: str, approve: bool, remark: str = "") -> None:
        await self._call("set_friend_add_request", {"flag": flag, "approve": approve, "remark": remark})

    async def set_friend_remark(self, user_id: str, remark: str) -> None:
        await self._call("set_friend_remark", {"user_id": user_id, "remark": remark})

    async def delete_friend(self, user_id: str, temp_block: bool = False,
                            temp_both_del: bool = False) -> None:
        params = {"user_id": user_id}
        if temp_block: params["temp_block"] = True
        if temp_both_del: params["temp_both_del"] = True
        await self._call("delete_friend", params)

    async def send_like(self, user_id: str, times: int = 1) -> None:
        await self._call("send_like", {"user_id": user_id, "times": times})

    async def get_recent_contact(self, count: int = None) -> Any:
        params = {} if count is None else {"count": count}
        return await self._call("get_recent_contact", params)

    async def get_profile_like(self, count: int = None, start: int = None,
                               user_id: str = None) -> Any:
        params = {}
        if count is not None: params["count"] = count
        if start is not None: params["start"] = start
        if user_id: params["user_id"] = user_id
        return await self._call("get_profile_like", params)

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

    async def set_group_add_request(self, flag: str, sub_type: str = None,
                                    approve: bool = False, reason: str = "",
                                    count: int = None) -> None:
        params = {"flag": flag, "approve": approve, "reason": reason}
        if sub_type: params["sub_type"] = sub_type
        if count is not None: params["count"] = count
        await self._call("set_group_add_request", params)

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

    async def send_group_notice(self, group_id: str, content: str, *,
                                image: str = "", pinned: int = 0, type: int = 1,
                                confirm_required: int = 1,
                                is_show_edit_card: int = 0,
                                tip_window_type: int = 0) -> Any:
        """发送群公告。

        Args:
            group_id: 群号。
            content: 公告正文。
            image: 公告图片路径或 URL。
            pinned: 是否置顶（0/1）。
            type: 公告类型（默认 1）。
            confirm_required: 是否需要确认（0/1）。
            is_show_edit_card: 是否显示修改群名片引导（0/1）。
            tip_window_type: 弹窗类型。
        """
        return await self._call("_send_group_notice", {
            "group_id": group_id, "content": content, "image": image,
            "pinned": pinned, "type": type,
            "confirm_required": confirm_required,
            "is_show_edit_card": is_show_edit_card,
            "tip_window_type": tip_window_type,
        })

    async def del_group_notice(self, group_id: str, notice_id: str) -> None:
        await self._call("_del_group_notice", {"group_id": group_id, "notice_id": notice_id})

    # ==================== 精华消息 ====================

    async def get_essence_msg_list(self, group_id: str) -> Any:
        return await self._call("get_essence_msg_list", {"group_id": group_id})

    async def set_essence_msg(self, message_id: int) -> None:
        await self._call("set_essence_msg", {"message_id": message_id})

    async def delete_essence_msg(self, message_id: int, group_id: str = "",
                                 msg_random: int = None,
                                 msg_seq: int = None) -> None:
        params = {"message_id": message_id}
        if group_id: params["group_id"] = group_id
        if msg_random is not None: params["msg_random"] = msg_random
        if msg_seq is not None: params["msg_seq"] = msg_seq
        await self._call("delete_essence_msg", params)

    # ==================== 文件接口 ====================

    async def get_image(self, file: str = None, file_id: str = None) -> FileInfo:
        params = {}
        if file: params["file"] = file
        if file_id: params["file_id"] = file_id
        data = await self._call("get_image", params)
        return FileInfo(**data) if isinstance(data, dict) else data

    async def get_record(self, file: str = None, file_id: str = None,
                         out_format: str = "mp3") -> FileInfo:
        params = {"out_format": out_format}
        if file: params["file"] = file
        if file_id: params["file_id"] = file_id
        data = await self._call("get_record", params)
        return FileInfo(**data) if isinstance(data, dict) else data

    async def get_file(self, file: str = None, file_id: str = None) -> FileInfo:
        params = {}
        if file: params["file"] = file
        if file_id: params["file_id"] = file_id
        data = await self._call("get_file", params)
        return FileInfo(**data) if isinstance(data, dict) else data

    async def get_group_file_url(self, group_id: str, file_id: str) -> Any:
        return await self._call("get_group_file_url", {"group_id": group_id, "file_id": file_id})

    async def upload_group_file(self, group_id: str, file: str, name: str,
                                folder: str = "", upload_file: bool = True,
                                folder_id: str = None) -> Any:
        params = {"group_id": group_id, "file": file, "name": name,
                  "upload_file": upload_file}
        if folder: params["folder"] = folder
        if folder_id: params["folder_id"] = folder_id
        return await self._call("upload_group_file", params)

    async def download_file(self, *, url: str = None, base64: str = None,
                            name: str = "", headers: str = "{}") -> FileInfo:
        params = {"name": name, "headers": headers}
        if url: params["url"] = url
        if base64: params["base64"] = base64
        data = await self._call("download_file", params)
        return FileInfo(**data) if isinstance(data, dict) else data

    async def delete_group_file(self, group_id: str, file_id: str,
                                bus_id: int = None, busid: int = None) -> None:
        params = {"group_id": group_id, "file_id": file_id}
        if bus_id is not None: params["bus_id"] = bus_id
        if busid is not None: params["busid"] = busid
        await self._call("delete_group_file", params)

    async def get_group_root_files(self, group_id: str,
                                   file_count: int = None) -> Any:
        params = {"group_id": group_id}
        if file_count is not None: params["file_count"] = file_count
        return await self._call("get_group_root_files", params)

    async def get_group_files_by_folder(self, group_id: str,
                                        folder_id: str = None, folder: str = None,
                                        file_count: int = None) -> Any:
        params = {"group_id": group_id}
        if folder_id: params["folder_id"] = folder_id
        if folder: params["folder"] = folder
        if file_count is not None: params["file_count"] = file_count
        return await self._call("get_group_files_by_folder", params)

    # ==================== 扩展接口 ====================

    async def set_online_status(self, status: int, ext_status: int = 0, battery_status: int = 0) -> None:
        await self._call("set_online_status", {"status": status, "ext_status": ext_status, "battery_status": battery_status})

    async def set_qq_profile(self, nickname: str, personal_note: str = "", sex: str = "0") -> None:
        await self._call("set_qq_profile", {"nickname": nickname, "personal_note": personal_note, "sex": sex})

    async def set_qq_avatar(self, file: str) -> None:
        await self._call("set_qq_avatar", {"file": file})

    async def set_self_longnick(self, long_nick: str) -> None:
        await self._call("set_self_longnick", {"longNick": long_nick})

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

    async def get_ai_record(self, character: str, group_id: str,
                            text: str) -> Any:
        """获取 AI 语音（返回语音 URL）。"""
        return await self._call("get_ai_record", {
            "character": character, "group_id": group_id, "text": text})

    async def get_ai_characters(self, group_id: str, chat_type: int = 1) -> Any:
        return await self._call("get_ai_characters",
                                {"group_id": group_id, "chat_type": chat_type})

    # ==================== 频道 ====================

    async def get_guild_list(self) -> Any:
        return await self._call("get_guild_list")

    async def get_guild_service_profile(self, guild_id: str = None) -> Any:
        params = {"guild_id": guild_id} if guild_id else {}
        return await self._call("get_guild_service_profile", params)

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

    async def send_flash_msg(self, fileset_id: str, group_id: str = None,
                             user_id: str = None) -> Any:
        params = {"fileset_id": fileset_id}
        if group_id: params["group_id"] = group_id
        if user_id: params["user_id"] = user_id
        return await self._call("send_flash_msg", params)

    async def send_online_file(self, user_id: str, file_path: str,
                               file_name: str = None) -> Any:
        params = {"user_id": user_id, "file_path": file_path}
        if file_name: params["file_name"] = file_name
        return await self._call("send_online_file", params)



    # ==================== 官方 API 全量覆盖（NapCat Apifox 文档） ====================
    #
    # 以下方法按官方文档的 action 补齐；必填参数显式列出，其余可选参数
    # 通过 `**kwargs` 透传（值为 None 的忽略）。字段名与官方一致。

    async def clean_stream_temp_file(self) -> Any:
        """清理 NapCat 侧的流式传输临时文件（上传/下载残留）。"""
        return await self._call("clean_stream_temp_file")

    # ---- 流式文件：下载 ----

    async def _stream_packets(self, action: str, params: dict,
                              timeout: float = 60.0) -> AsyncIterator[dict]:
        """发起流式 action，逐条产出 `data` 字典（内部方法）。

        Args:
            action: 流式 action 名。
            params: 请求参数。
            timeout: 单条数据包的超时（秒）。

        Yields:
            服务端推送的数据包（`data` 字段）。

        Raises:
            ApiError: 服务端返回 failed / error 包。
        """
        echo = self._next_echo()
        payload = {"action": action, "echo": echo, "params": params}
        async for packet in self._ws.stream_request(payload, timeout=timeout):
            status = packet.get("status")
            data = packet.get("data") or {}
            if status == "failed" or data.get("type") == "error":
                raise ApiError(
                    int(packet.get("retcode") or -1),
                    str(packet.get("message") or packet.get("wording") or "流式传输失败"))
            if isinstance(data, dict):
                yield data

    def _download_params(self, file, file_id, chunk_size) -> dict:
        params: dict = {"chunk_size": int(chunk_size or 64 * 1024)}
        if file:
            params["file"] = file
        if file_id:
            params["file_id"] = file_id
        return params

    async def iter_file_stream(self, *, file: str = None, file_id: str = None,
                               chunk_size: int = 65536,
                               timeout: float = 60.0) -> AsyncIterator[dict]:
        """按事件流下载文件（`file_info` / `file_chunk` / `file_complete`）。

        Args:
            file: 文件路径、URL 或带 FileNapCat UUID 的引用。
            file_id: 文件 ID（与 `file` 二选一）。
            chunk_size: 分块大小（字节）。
            timeout: 单条数据包超时（秒）。

        Yields:
            原始事件字典；`file_chunk` 事件的 `data` 是 Base64。
        """
        params = self._download_params(file, file_id, chunk_size)
        async for event in self._stream_packets("download_file_stream",
                                                params, timeout):
            yield event

    async def _download_bytes(self, action: str, *, file=None, file_id=None,
                              chunk_size: int = 65536, timeout: float = 60.0,
                              out_format: str = None) -> AsyncIterator[bytes]:
        params = self._download_params(file, file_id, chunk_size)
        if out_format:
            params["out_format"] = out_format
        async for event in self._stream_packets(action, params, timeout):
            if event.get("data_type") == "file_chunk":
                yield base64.b64decode(event.get("data") or "")

    async def download_file_stream(self, *, file: str = None, file_id: str = None,
                                   chunk_size: int = 65536,
                                   timeout: float = 60.0) -> AsyncIterator[bytes]:
        """流式下载文件，逐块产出字节。

        Args:
            file: 文件路径、URL 或引用 ID。
            file_id: 文件 ID。
            chunk_size: 分块大小（字节）。
            timeout: 单条数据包超时（秒）。
        """
        async for chunk in self._download_bytes(
                "download_file_stream", file=file, file_id=file_id,
                chunk_size=chunk_size, timeout=timeout):
            yield chunk

    async def download_file_image_stream(self, *, file: str = None,
                                         file_id: str = None,
                                         chunk_size: int = 65536,
                                         timeout: float = 60.0) -> AsyncIterator[bytes]:
        """流式下载图片，逐块产出字节。"""
        async for chunk in self._download_bytes(
                "download_file_image_stream", file=file, file_id=file_id,
                chunk_size=chunk_size, timeout=timeout):
            yield chunk

    async def download_file_record_stream(self, *, file: str = None,
                                          file_id: str = None,
                                          chunk_size: int = 65536,
                                          out_format: str = "mp3",
                                          timeout: float = 60.0) -> AsyncIterator[bytes]:
        """流式下载语音（可按 `out_format` 转码），逐块产出字节。"""
        async for chunk in self._download_bytes(
                "download_file_record_stream", file=file, file_id=file_id,
                chunk_size=chunk_size, timeout=timeout, out_format=out_format):
            yield chunk

    # ---- 流式文件：上传 ----

    async def upload_file_stream(self, file_path: str, *,
                                 name: str = None,
                                 chunk_size: int = 512 * 1024,
                                 retention: int = 300000,
                                 expected_sha256: str = None,
                                 on_progress=None) -> dict:
        """把本地文件分块流式上传到 NapCat，返回服务器上的文件信息。

        Args:
            file_path: 本地文件路径。
            name: 上传后的文件名（默认取文件名）。
            chunk_size: 每块字节数（默认 512KB）。
            retention: 服务器保留时间（毫秒，默认 5 分钟）。
            expected_sha256: 期望的 SHA256；提供时服务端会校验。
            on_progress: 进度回调 `(已发送字节, 总字节)`。

        Returns:
            服务端最终状态字典（含 `file_path` / `file_name` / `file_size` /
            `sha256` 等字段，具体以 NapCat 返回为准）。
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        file_size = path.stat().st_size
        file_name = name or path.name
        chunk_size = max(1, int(chunk_size or 512 * 1024))
        total_chunks = max(1, math.ceil(file_size / chunk_size))
        stream_id = uuid.uuid4().hex
        sent = 0
        base = {
            "stream_id": stream_id,
            "total_chunks": total_chunks,
            "file_size": file_size,
            "filename": file_name,
            "file_retention": int(retention),
        }
        if expected_sha256:
            base["expected_sha256"] = expected_sha256

        index = 0
        async with aiofiles.open(path, "rb") as fh:
            while index < total_chunks:
                data = await fh.read(chunk_size)
                params = dict(base)
                params["chunk_data"] = base64.b64encode(data).decode()
                params["chunk_index"] = index
                result = await self._call("upload_file_stream", params,
                                          throttle=False)
                sent += len(data)
                if on_progress:
                    on_progress(sent, file_size)
                index += 1
                if isinstance(result, dict) and result.get("status") == "file_complete":
                    return result

        params = dict(base)
        params["is_complete"] = True
        return await self._call("upload_file_stream", params, throttle=False)

    async def test_download_stream(self, error: bool = False) -> Any:
        """测试下载流（排查流式通道是否可用；`error=True` 时触发测试错误）。"""
        params = {"error": True} if error else None
        return await self._call("test_download_stream", params)

    async def del_group_album_media(self, album_id: str, group_id: str, lloc: str) -> Any:
        """删除群相册媒体（`del_group_album_media`）。

        Args:
            album_id: 相册 ID。
            group_id: 群号。
            lloc: lloc。
        """
        params: dict = {"album_id": album_id, "group_id": group_id, "lloc": lloc}
        return await self._call("del_group_album_media", params)

    async def set_group_album_media_like(self, album_id: str, batch_id: str, group_id: str, **kwargs: Any) -> Any:
        """点赞群相册媒体（`set_group_album_media_like`）。

        Args:
            album_id: 相册 ID。
            batch_id: batch_id。
            group_id: 群号。
            **kwargs: 可选参数（lloc），值为 None 时忽略。
        """
        params: dict = {"album_id": album_id, "batch_id": batch_id, "group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("set_group_album_media_like", params)

    async def cancel_group_album_media_like(self, album_id: str, batch_id: str, group_id: str, **kwargs: Any) -> Any:
        """取消点赞群相册媒体（`cancel_group_album_media_like`）。

        Args:
            album_id: 相册 ID。
            batch_id: batch_id。
            group_id: 群号。
            **kwargs: 可选参数（lloc），值为 None 时忽略。
        """
        params: dict = {"album_id": album_id, "batch_id": batch_id, "group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("cancel_group_album_media_like", params)

    async def do_group_album_comment(self, album_id: str, content: str, group_id: str, **kwargs: Any) -> Any:
        """发表群相册评论（`do_group_album_comment`）。

        Args:
            album_id: 相册 ID。
            content: 内容。
            group_id: 群号。
            **kwargs: 可选参数（batch_id, lloc），值为 None 时忽略。
        """
        params: dict = {"album_id": album_id, "content": content, "group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("do_group_album_comment", params)

    async def get_group_album_media_list(self, album_id: str, group_id: str, **kwargs: Any) -> Any:
        """获取群相册媒体列表（`get_group_album_media_list`）。

        Args:
            album_id: 相册 ID。
            group_id: 群号。
            **kwargs: 可选参数（attach_info），值为 None 时忽略。
        """
        params: dict = {"album_id": album_id, "group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("get_group_album_media_list", params)

    async def get_qun_album_list(self, group_id: str, **kwargs: Any) -> Any:
        """获取群相册列表（`get_qun_album_list`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（attach_info），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("get_qun_album_list", params)

    async def upload_image_to_qun_album(self, album_id: str, album_name: str, file: str, group_id: str) -> Any:
        """上传图片到群相册（`upload_image_to_qun_album`）。

        Args:
            album_id: 相册 ID。
            album_name: album_name。
            file: 资源路径 / URL / Base64。
            group_id: 群号。
        """
        params: dict = {"album_id": album_id, "album_name": album_name, "file": file, "group_id": group_id}
        return await self._call("upload_image_to_qun_album", params)

    async def set_group_add_option(self, add_type: str, group_id: str, **kwargs: Any) -> Any:
        """设置群加群选项（`set_group_add_option`）。

        Args:
            add_type: add_type。
            group_id: 群号。
            **kwargs: 可选参数（group_answer, group_question），值为 None 时忽略。
        """
        params: dict = {"add_type": add_type, "group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("set_group_add_option", params)

    async def set_group_robot_add_option(self, group_id: str, **kwargs: Any) -> Any:
        """设置群机器人加群选项（`set_group_robot_add_option`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（robot_member_examine, robot_member_switch），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("set_group_robot_add_option", params)

    async def set_group_search(self, group_id: str, **kwargs: Any) -> Any:
        """设置群搜索选项（`set_group_search`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（no_code_finger_open, no_finger_open），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("set_group_search", params)

    async def set_group_remark(self, group_id: str, remark: str) -> Any:
        """设置群备注（`set_group_remark`）。

        Args:
            group_id: 群号。
            remark: remark。
        """
        params: dict = {"group_id": group_id, "remark": remark}
        return await self._call("set_group_remark", params)

    async def get_group_info_ex(self, group_id: str) -> Any:
        """获取群详细信息 (扩展)（`get_group_info_ex`）。

        Args:
            group_id: 群号。
        """
        params: dict = {"group_id": group_id}
        return await self._call("get_group_info_ex", params)

    async def get_group_signed_list(self, group_id: str) -> Any:
        """获取群组今日打卡列表（`get_group_signed_list`）。

        Args:
            group_id: 群号。
        """
        params: dict = {"group_id": group_id}
        return await self._call("get_group_signed_list", params)

    async def get_group_share_link(self, group_id: str, **kwargs: Any) -> Any:
        """获取群分享链接（`get_group_share_link`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（additional_param, need_short_url, src_id），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("get_group_share_link", params)

    async def upload_images_to_qun_album(self, album_id: str, album_name: str, files: list, group_id: str) -> Any:
        """上传多张图片到群相册（`upload_images_to_qun_album`）。

        Args:
            album_id: 相册 ID。
            album_name: album_name。
            files: files。
            group_id: 群号。
        """
        params: dict = {"album_id": album_id, "album_name": album_name, "files": files, "group_id": group_id}
        return await self._call("upload_images_to_qun_album", params)

    async def upload_video_to_qun_album(self, album_id: str, album_name: str, file: str, group_id: str) -> Any:
        """上传视频到群相册（`upload_video_to_qun_album`）。

        Args:
            album_id: 相册 ID。
            album_name: album_name。
            file: 资源路径 / URL / Base64。
            group_id: 群号。
        """
        params: dict = {"album_id": album_id, "album_name": album_name, "file": file, "group_id": group_id}
        return await self._call("upload_video_to_qun_album", params)

    async def set_group_todo(self, group_id: str, **kwargs: Any) -> Any:
        """设置群待办（`set_group_todo`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（message_id, message_seq），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("set_group_todo", params)

    async def complete_group_todo(self, group_id: str, **kwargs: Any) -> Any:
        """完成群待办（`complete_group_todo`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（message_id, message_seq），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("complete_group_todo", params)

    async def cancel_group_todo(self, group_id: str, **kwargs: Any) -> Any:
        """取消群待办（`cancel_group_todo`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（message_id, message_seq），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("cancel_group_todo", params)

    async def group_poke(self, user_id: str, **kwargs: Any) -> Any:
        """发送戳一戳（`group_poke`）。

        Args:
            user_id: 用户 QQ。
            **kwargs: 可选参数（group_id, target_id），值为 None 时忽略。
        """
        params: dict = {"user_id": user_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("group_poke", params)

    async def friend_poke(self, user_id: str, **kwargs: Any) -> Any:
        """发送戳一戳（`friend_poke`）。

        Args:
            user_id: 用户 QQ。
            **kwargs: 可选参数（group_id, target_id），值为 None 时忽略。
        """
        params: dict = {"user_id": user_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("friend_poke", params)

    async def get_group_detail_info(self, group_id: str) -> Any:
        """获取群详细信息（`get_group_detail_info`）。

        Args:
            group_id: 群号。
        """
        params: dict = {"group_id": group_id}
        return await self._call("get_group_detail_info", params)

    async def get_group_ignored_notifies(self) -> Any:
        """获取群忽略通知（`get_group_ignored_notifies`）。
        """
        return await self._call("get_group_ignored_notifies")

    async def get_group_ignore_add_request(self) -> Any:
        """获取群被忽略的加群请求（`get_group_ignore_add_request`）。
        """
        return await self._call("get_group_ignore_add_request")

    async def set_group_member_invite_policy(self, group_id: str, policy: str) -> Any:
        """设置群成员邀请策略（`set_group_member_invite_policy`）。

        Args:
            group_id: 群号。
            policy: policy。
        """
        params: dict = {"group_id": group_id, "policy": policy}
        return await self._call("set_group_member_invite_policy", params)

    async def set_group_member_permissions(self, group_id: str, **kwargs: Any) -> Any:
        """设置群成员功能权限（`set_group_member_permissions`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（allow_member_create_group, allow_member_temporary_session, allow_member_upload_album），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("set_group_member_permissions", params)

    async def set_group_new_member_history_visibility(self, group_id: str, visible: str) -> Any:
        """设置新成员历史消息可见性（`set_group_new_member_history_visibility`）。

        Args:
            group_id: 群号。
            visible: visible。
        """
        params: dict = {"group_id": group_id, "visible": visible}
        return await self._call("set_group_new_member_history_visibility", params)

    async def set_group_kick_members(self, group_id: str, user_id: str, **kwargs: Any) -> Any:
        """批量踢出群成员（`set_group_kick_members`）。

        Args:
            group_id: 群号。
            user_id: 用户 QQ。
            **kwargs: 可选参数（reject_add_request），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id, "user_id": user_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("set_group_kick_members", params)

    async def create_collection(self, brief: str, rawData: str) -> Any:
        """创建收藏（`create_collection`）。

        Args:
            brief: brief。
            rawData: rawData。
        """
        params: dict = {"brief": brief, "rawData": rawData}
        return await self._call("create_collection", params)

    async def ocr_image_internal(self, image: str) -> Any:
        """图片 OCR 识别 (内部)（`.ocr_image`）。

        Args:
            image: 图片路径 / URL / Base64。
        """
        params: dict = {"image": image}
        return await self._call(".ocr_image", params)

    async def set_doubt_friends_add_request(self, approve: bool, flag: str) -> Any:
        """处理可疑好友申请（`set_doubt_friends_add_request`）。

        Args:
            approve: 是否同意。
            flag: 请求 flag。
        """
        params: dict = {"approve": approve, "flag": flag}
        return await self._call("set_doubt_friends_add_request", params)

    async def get_doubt_friends_add_request(self, count: int) -> Any:
        """获取可疑好友申请（`get_doubt_friends_add_request`）。

        Args:
            count: count。
        """
        params: dict = {"count": count}
        return await self._call("get_doubt_friends_add_request", params)

    async def nc_get_packet_status(self) -> Any:
        """获取Packet状态（`nc_get_packet_status`）。
        """
        return await self._call("nc_get_packet_status")

    async def get_rkey(self) -> Any:
        """获取扩展 RKey（`get_rkey`）。
        """
        return await self._call("get_rkey")

    async def get_rkey_server(self) -> Any:
        """获取 RKey 服务器（`get_rkey_server`）。
        """
        return await self._call("get_rkey_server")

    async def get_robot_uin_range(self) -> Any:
        """获取机器人 UIN 范围（`get_robot_uin_range`）。
        """
        return await self._call("get_robot_uin_range")

    async def fetch_custom_face(self, count: int) -> Any:
        """获取自定义表情（`fetch_custom_face`）。

        Args:
            count: count。
        """
        params: dict = {"count": count}
        return await self._call("fetch_custom_face", params)

    async def fetch_custom_face_detail(self, count: int) -> Any:
        """获取自定义表情详情（`fetch_custom_face_detail`）。

        Args:
            count: count。
        """
        params: dict = {"count": count}
        return await self._call("fetch_custom_face_detail", params)

    async def add_custom_face(self, file: str, **kwargs: Any) -> Any:
        """添加自定义表情（`add_custom_face`）。

        Args:
            file: 资源路径 / URL / Base64。
            **kwargs: 可选参数（emoji_id, file_name, file_size, is_mark_face, is_origin, md5 …），值为 None 时忽略。
        """
        params: dict = {"file": file}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("add_custom_face", params)

    async def delete_custom_face(self, **kwargs: Any) -> Any:
        """删除自定义表情（`delete_custom_face`）。

        Args:
            **kwargs: 可选参数（id, ids, md5, res_id），值为 None 时忽略。
        """
        return await self._call("delete_custom_face", {k: v for k, v in kwargs.items() if v is not None} or None)

    async def set_custom_face_desc(self, desc: str, emoji_id: str, md5: str, res_id: str) -> Any:
        """修改自定义表情描述（`set_custom_face_desc`）。

        Args:
            desc: desc。
            emoji_id: 表情 ID。
            md5: md5。
            res_id: res_id。
        """
        params: dict = {"desc": desc, "emoji_id": emoji_id, "md5": md5, "res_id": res_id}
        return await self._call("set_custom_face_desc", params)

    async def nc_get_user_status(self, user_id: str) -> Any:
        """获取用户在线状态（`nc_get_user_status`）。

        Args:
            user_id: 用户 QQ。
        """
        params: dict = {"user_id": user_id}
        return await self._call("nc_get_user_status", params)

    async def nc_get_rkey(self) -> Any:
        """获取 RKey（`nc_get_rkey`）。
        """
        return await self._call("nc_get_rkey")

    async def send_packet(self, cmd: str, data: Any, rsp: str) -> Any:
        """发送原始数据包（`send_packet`）。

        Args:
            cmd: cmd。
            data: data。
            rsp: rsp。
        """
        params: dict = {"cmd": cmd, "data": data, "rsp": rsp}
        return await self._call("send_packet", params)

    async def get_collection_list(self, category: str, count: int) -> Any:
        """获取收藏列表（`get_collection_list`）。

        Args:
            category: category。
            count: count。
        """
        params: dict = {"category": category, "count": count}
        return await self._call("get_collection_list", params)

    async def fetch_emoji_like(self, cookie: str, count: int, emojiId: str, emojiType: str, message_id: str) -> Any:
        """获取表情点赞详情（`fetch_emoji_like`）。

        Args:
            cookie: cookie。
            count: count。
            emojiId: emojiId。
            emojiType: emojiType。
            message_id: 消息 ID。
        """
        params: dict = {"cookie": cookie, "count": count, "emojiId": emojiId, "emojiType": emojiType, "message_id": message_id}
        return await self._call("fetch_emoji_like", params)

    async def get_emoji_likes(self, count: int, emoji_id: str, message_id: str, **kwargs: Any) -> Any:
        """获取消息表情点赞列表（`get_emoji_likes`）。

        Args:
            count: count。
            emoji_id: 表情 ID。
            message_id: 消息 ID。
            **kwargs: 可选参数（emoji_type, group_id），值为 None 时忽略。
        """
        params: dict = {"count": count, "emoji_id": emoji_id, "message_id": message_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("get_emoji_likes", params)

    async def ark_share_group(self, group_id: str) -> Any:
        """分享群 (Ark)（`ArkShareGroup`）。

        Args:
            group_id: 群号。
        """
        params: dict = {"group_id": group_id}
        return await self._call("ArkShareGroup", params)

    async def ark_share_peer(self, phone_number: str, **kwargs: Any) -> Any:
        """分享用户 (Ark)（`ArkSharePeer`）。

        Args:
            phone_number: phone_number。
            **kwargs: 可选参数（group_id, user_id），值为 None 时忽略。
        """
        params: dict = {"phone_number": phone_number}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("ArkSharePeer", params)

    async def send_ark_share(self, phone_number: str, **kwargs: Any) -> Any:
        """分享用户 (Ark)（`send_ark_share`）。

        Args:
            phone_number: phone_number。
            **kwargs: 可选参数（group_id, user_id），值为 None 时忽略。
        """
        params: dict = {"phone_number": phone_number}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("send_ark_share", params)

    async def click_inline_keyboard_button(self, bot_appid: str, button_id: str, callback_data: str, group_id: str, msg_seq: int) -> Any:
        """点击内联键盘按钮（`click_inline_keyboard_button`）。

        Args:
            bot_appid: bot_appid。
            button_id: button_id。
            callback_data: callback_data。
            group_id: 群号。
            msg_seq: msg_seq。
        """
        params: dict = {"bot_appid": bot_appid, "button_id": button_id, "callback_data": callback_data, "group_id": group_id, "msg_seq": msg_seq}
        return await self._call("click_inline_keyboard_button", params)

    async def get_private_file_url(self, file_id: str) -> Any:
        """获取私聊文件URL（`get_private_file_url`）。

        Args:
            file_id: 文件 ID。
        """
        params: dict = {"file_id": file_id}
        return await self._call("get_private_file_url", params)

    async def check_url_safely(self, url: str) -> Any:
        """检查URL安全性（`check_url_safely`）。

        Args:
            url: 链接。
        """
        params: dict = {"url": url}
        return await self._call("check_url_safely", params)

    async def send_group_forward_msg(self, message: str, **kwargs: Any) -> Any:
        """发送群合并转发消息（`send_group_forward_msg`）。

        Args:
            message: message。
            **kwargs: 可选参数（auto_escape, group_id, message_type, news, prompt, source …），值为 None 时忽略。
        """
        params: dict = {"message": message}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("send_group_forward_msg", params)

    async def send_private_forward_msg(self, message: str, **kwargs: Any) -> Any:
        """发送私聊合并转发消息（`send_private_forward_msg`）。

        Args:
            message: message。
            **kwargs: 可选参数（auto_escape, group_id, message_type, news, prompt, source …），值为 None 时忽略。
        """
        params: dict = {"message": message}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("send_private_forward_msg", params)

    async def handle_quick_operation(self, context: Any, operation: Any) -> Any:
        """处理快速操作（`.handle_quick_operation`）。

        Args:
            context: 事件上下文。
            operation: 要执行的操作。
        """
        params: dict = {"context": context, "operation": operation}
        return await self._call(".handle_quick_operation", params)

    async def set_group_portrait(self, file: str, group_id: str) -> Any:
        """设置群头像（`set_group_portrait`）。

        Args:
            file: 资源路径 / URL / Base64。
            group_id: 群号。
        """
        params: dict = {"file": file, "group_id": group_id}
        return await self._call("set_group_portrait", params)

    async def upload_private_file(self, user_id: str, file: str, name: str,
                                  upload_file: bool = True) -> Any:
        """上传文件到私聊（好友 / 群临时会话均可）。

        Args:
            user_id: 目标用户 QQ。
            file: 本地文件路径 / URL。
            name: 文件名。
            upload_file: 是否执行上传（默认 True）。
        """
        return await self._call("upload_private_file", {
            "user_id": user_id, "file": file, "name": name,
            "upload_file": upload_file,
        })

    async def get_model_show(self, **kwargs: Any) -> Any:
        """获取机型显示（`_get_model_show`）。

        Args:
            **kwargs: 可选参数（model），值为 None 时忽略。
        """
        return await self._call("_get_model_show", {k: v for k, v in kwargs.items() if v is not None} or None)

    async def set_model_show(self) -> Any:
        """设置机型（`_set_model_show`）。
        """
        return await self._call("_set_model_show")

    async def create_group_file_folder(self, group_id: str, **kwargs: Any) -> Any:
        """创建群文件目录（`create_group_file_folder`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（folder_name, name），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("create_group_file_folder", params)

    async def delete_group_folder(self, group_id: str, **kwargs: Any) -> Any:
        """删除群文件目录（`delete_group_folder`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（folder, folder_id），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("delete_group_folder", params)

    async def get_group_file_system_info(self, group_id: str) -> Any:
        """获取群文件系统信息（`get_group_file_system_info`）。

        Args:
            group_id: 群号。
        """
        params: dict = {"group_id": group_id}
        return await self._call("get_group_file_system_info", params)

    async def forward_friend_single_msg(self, message_id: str, **kwargs: Any) -> Any:
        """转发单条消息（`forward_friend_single_msg`）。

        Args:
            message_id: 消息 ID。
            **kwargs: 可选参数（group_id, user_id），值为 None 时忽略。
        """
        params: dict = {"message_id": message_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("forward_friend_single_msg", params)

    async def forward_group_single_msg(self, message_id: str, **kwargs: Any) -> Any:
        """转发单条消息（`forward_group_single_msg`）。

        Args:
            message_id: 消息 ID。
            **kwargs: 可选参数（group_id, user_id），值为 None 时忽略。
        """
        params: dict = {"message_id": message_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("forward_group_single_msg", params)

    async def mark_group_msg_as_read(self, **kwargs: Any) -> Any:
        """标记群聊已读（`mark_group_msg_as_read`）。

        Args:
            **kwargs: 可选参数（group_id, message_id, user_id），值为 None 时忽略。
        """
        return await self._call("mark_group_msg_as_read", {k: v for k, v in kwargs.items() if v is not None} or None)

    async def mark_private_msg_as_read(self, **kwargs: Any) -> Any:
        """标记私聊已读（`mark_private_msg_as_read`）。

        Args:
            **kwargs: 可选参数（group_id, message_id, user_id），值为 None 时忽略。
        """
        return await self._call("mark_private_msg_as_read", {k: v for k, v in kwargs.items() if v is not None} or None)

    async def mark_all_as_read(self) -> Any:
        """标记所有消息已读（`_mark_all_as_read`）。
        """
        return await self._call("_mark_all_as_read")

    async def move_group_file(self, current_parent_directory: str, file_id: str, group_id: str, target_parent_directory: str) -> Any:
        """移动群文件（`move_group_file`）。

        Args:
            current_parent_directory: current_parent_directory。
            file_id: 文件 ID。
            group_id: 群号。
            target_parent_directory: target_parent_directory。
        """
        params: dict = {"current_parent_directory": current_parent_directory, "file_id": file_id, "group_id": group_id, "target_parent_directory": target_parent_directory}
        return await self._call("move_group_file", params)

    async def rename_group_file(self, current_parent_directory: str, file_id: str, group_id: str, new_name: str) -> Any:
        """重命名群文件（`rename_group_file`）。

        Args:
            current_parent_directory: current_parent_directory。
            file_id: 文件 ID。
            group_id: 群号。
            new_name: new_name。
        """
        params: dict = {"current_parent_directory": current_parent_directory, "file_id": file_id, "group_id": group_id, "new_name": new_name}
        return await self._call("rename_group_file", params)

    async def trans_group_file(self, file_id: str, group_id: str) -> Any:
        """传输群文件（`trans_group_file`）。

        Args:
            file_id: 文件 ID。
            group_id: 群号。
        """
        params: dict = {"file_id": file_id, "group_id": group_id}
        return await self._call("trans_group_file", params)

    async def create_flash_task(self, files: list, **kwargs: Any) -> Any:
        """创建闪传任务（`create_flash_task`）。

        Args:
            files: files。
            **kwargs: 可选参数（name, thumb_path, validitySeconds），值为 None 时忽略。
        """
        params: dict = {"files": files}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("create_flash_task", params)

    async def get_flash_file_list(self, fileset_id: str) -> Any:
        """获取闪传文件列表（`get_flash_file_list`）。

        Args:
            fileset_id: 文件集 ID。
        """
        params: dict = {"fileset_id": fileset_id}
        return await self._call("get_flash_file_list", params)

    async def get_flash_file_url(self, fileset_id: str, **kwargs: Any) -> Any:
        """获取闪传文件链接（`get_flash_file_url`）。

        Args:
            fileset_id: 文件集 ID。
            **kwargs: 可选参数（file_index, file_name），值为 None 时忽略。
        """
        params: dict = {"fileset_id": fileset_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("get_flash_file_url", params)

    async def get_share_link(self, fileset_id: str) -> Any:
        """获取文件分享链接（`get_share_link`）。

        Args:
            fileset_id: 文件集 ID。
        """
        params: dict = {"fileset_id": fileset_id}
        return await self._call("get_share_link", params)

    async def get_fileset_info(self, fileset_id: str) -> Any:
        """获取文件集信息（`get_fileset_info`）。

        Args:
            fileset_id: 文件集 ID。
        """
        params: dict = {"fileset_id": fileset_id}
        return await self._call("get_fileset_info", params)

    async def get_online_file_msg(self, user_id: str) -> Any:
        """获取在线文件消息（`get_online_file_msg`）。

        Args:
            user_id: 用户 QQ。
        """
        params: dict = {"user_id": user_id}
        return await self._call("get_online_file_msg", params)

    async def send_online_folder(self, folder_path: str, user_id: str, **kwargs: Any) -> Any:
        """发送在线文件夹（`send_online_folder`）。

        Args:
            folder_path: folder_path。
            user_id: 用户 QQ。
            **kwargs: 可选参数（folder_name），值为 None 时忽略。
        """
        params: dict = {"folder_path": folder_path, "user_id": user_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self._call("send_online_folder", params)

    async def receive_online_file(self, element_id: str, msg_id: str, user_id: str) -> Any:
        """接收在线文件（`receive_online_file`）。

        Args:
            element_id: element_id。
            msg_id: msg_id。
            user_id: 用户 QQ。
        """
        params: dict = {"element_id": element_id, "msg_id": msg_id, "user_id": user_id}
        return await self._call("receive_online_file", params)

    async def refuse_online_file(self, element_id: str, msg_id: str, user_id: str) -> Any:
        """拒绝在线文件（`refuse_online_file`）。

        Args:
            element_id: element_id。
            msg_id: msg_id。
            user_id: 用户 QQ。
        """
        params: dict = {"element_id": element_id, "msg_id": msg_id, "user_id": user_id}
        return await self._call("refuse_online_file", params)

    async def cancel_online_file(self, msg_id: str, user_id: str) -> Any:
        """取消在线文件（`cancel_online_file`）。

        Args:
            msg_id: msg_id。
            user_id: 用户 QQ。
        """
        params: dict = {"msg_id": msg_id, "user_id": user_id}
        return await self._call("cancel_online_file", params)

    async def download_fileset(self, fileset_id: str) -> Any:
        """下载文件集（`download_fileset`）。

        Args:
            fileset_id: 文件集 ID。
        """
        params: dict = {"fileset_id": fileset_id}
        return await self._call("download_fileset", params)

    async def get_fileset_id(self, share_code: str) -> Any:
        """获取文件集 ID（`get_fileset_id`）。

        Args:
            share_code: share_code。
        """
        params: dict = {"share_code": share_code}
        return await self._call("get_fileset_id", params)

    async def set_diy_online_status(self, face_id: str, face_type: str, wording: str) -> Any:
        """设置自定义在线状态（`set_diy_online_status`）。

        Args:
            face_id: face_id。
            face_type: face_type。
            wording: wording。
        """
        params: dict = {"face_id": face_id, "face_type": face_type, "wording": wording}
        return await self._call("set_diy_online_status", params)

    async def get_unidirectional_friend_list(self) -> Any:
        """获取单向好友列表（`get_unidirectional_friend_list`）。
        """
        return await self._call("get_unidirectional_friend_list")

    async def send_group_ai_record(self, character: str, group_id: str, text: str) -> Any:
        """发送群 AI 语音（`send_group_ai_record`）。

        Args:
            character: character。
            group_id: 群号。
            text: 文本。
        """
        params: dict = {"character": character, "group_id": group_id, "text": text}
        return await self._call("send_group_ai_record", params)


class ApiError(Exception):
    """OneBot API 调用失败（服务端返回错误或连接不可用）。"""
    def __init__(self, retcode: int, message: str):
        self.retcode = retcode
        self.message = message
        super().__init__(f"[{retcode}] {message}")
