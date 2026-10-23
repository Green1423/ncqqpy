"""
ncqqpy 底层数据模型定义
包含所有事件、消息段、数据类型，与 NapCatQQ websocket-types 文档完全对齐
"""
from __future__ import annotations

from enum import Enum
from typing import Optional, Union, Any, Literal
from dataclasses import dataclass, field
from pydantic import BaseModel, Field, model_validator
import time as _time


# ==================== 枚举类型 ====================

class EventType(str, Enum):
    """事件大类枚举（meta_event / message / notice / request）。"""
    META = "meta_event"
    REQUEST = "request"
    NOTICE = "notice"
    MESSAGE = "message"
    MESSAGE_SENT = "message_sent"


class MessageType(str, Enum):
    """消息类型枚举（私聊 / 群聊）。"""
    PRIVATE = "private"
    GROUP = "group"


class MessageSubType(str, Enum):
    """消息子类型（好友、群临时会话、匿名等）。"""
    FRIEND = "friend"
    GROUP = "group"
    NORMAL = "normal"


class GroupMemberRole(str, Enum):
    """群成员角色（群主 / 管理员 / 成员）。"""
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class UserSex(str, Enum):
    """用户性别。"""
    MALE = "male"
    FEMALE = "female"
    UNKNOWN = "unknown"


class SegmentType(str, Enum):
    """消息段类型（text / image / file …）。"""
    TEXT = "text"
    IMAGE = "image"
    MUSIC = "music"
    VIDEO = "video"
    VOICE = "record"
    FILE = "file"
    AT = "at"
    REPLY = "reply"
    JSON = "json"
    FACE = "face"
    MFACE = "mface"
    MARKDOWN = "markdown"
    NODE = "node"
    FORWARD = "forward"
    XML = "xml"
    POKE = "poke"
    DICE = "dice"
    RPS = "rps"
    MINIAPP = "miniapp"
    CONTACT = "contact"
    LOCATION = "location"
    ONLINEFILE = "onlinefile"
    FLASHTRANSFER = "flashtransfer"


class LifeCycleSubType(str, Enum):
    """生命周期子类型（connect / enable / disable）。"""
    ENABLE = "enable"
    DISABLE = "disable"
    CONNECT = "connect"


# ==================== 发送者/用户/群信息 ====================

class Sender(BaseModel):
    """消息发送者信息（QQ、昵称、群名片、角色）。"""
    user_id: int
    nickname: str
    sex: Optional[UserSex] = None
    age: Optional[int] = None
    card: Optional[str] = None
    level: Optional[str] = None
    role: Optional[GroupMemberRole] = None


class User(BaseModel):
    """用户简要信息。"""
    user_id: int
    nickname: str
    remark: Optional[str] = None
    sex: Optional[UserSex] = None
    age: Optional[int] = None
    level: Optional[int] = None
    qid: Optional[str] = None
    birthday_year: Optional[int] = None
    birthday_month: Optional[int] = None
    birthday_day: Optional[int] = None
    phone_num: Optional[str] = None
    email: Optional[str] = None
    login_days: Optional[int] = None
    category_id: Optional[int] = None
    categoryName: Optional[str] = None
    categoryId: Optional[int] = None


class Group(BaseModel):
    """群简要信息。"""
    group_id: int
    group_name: str
    group_remark: str = ""
    group_all_shut: int = 0
    member_count: Optional[int] = None
    max_member_count: Optional[int] = None


class GroupMember(BaseModel):
    """群成员详细信息。"""
    group_id: int
    user_id: int
    nickname: str
    card: Optional[str] = None
    sex: Optional[UserSex] = None
    age: Optional[int] = None
    join_time: Optional[int] = None
    last_sent_time: Optional[int] = None
    level: Optional[str] = None
    qq_level: Optional[int] = None
    role: Optional[GroupMemberRole] = None
    title: Optional[str] = None
    area: Optional[str] = None
    unfriendly: Optional[bool] = None
    title_expire_time: Optional[int] = None
    card_changeable: Optional[bool] = None
    shut_up_timestamp: Optional[int] = None
    is_robot: Optional[bool] = None
    qage: Optional[int] = None


class GroupFile(BaseModel):
    """群文件。"""
    group_id: int
    file_id: str
    file_name: str
    busid: int
    size: int
    file_size: Optional[int] = None
    upload_time: int
    dead_time: int
    modify_time: int
    download_times: int
    uploader: int
    uploader_name: str


class GroupFileFolder(BaseModel):
    """群文件夹。"""
    group_id: int
    folder_id: str
    folder: str
    folder_name: str
    create_time: int
    creator: int
    creator_name: str
    total_file_count: int


# ==================== 消息段类型 ====================

class MessageSegment(BaseModel):
    """原始消息段（OneBot 数组格式的一条）。"""
    type: str
    data: dict = Field(default_factory=dict)


class EmojiLike(BaseModel):
    """表情表态记录。"""
    emoji_id: str
    emoji_type: str = ""
    likes_cnt: str = "0"


# ==================== 事件基类 ====================

class NapCatEvent(BaseModel):
    """所有推送事件的基类"""
    time: int = Field(default_factory=lambda: int(_time.time()))
    self_id: int = 0
    post_type: str = ""


# ---- 事件解析工厂 ----

_EVENT_CLASS_MAP: dict[tuple, type] = {}

def _build_event_map():
    """构建 (post_type, sub_key, sub_val) → EventClass 映射"""
    from .models import (  # noqa: reimport for lazy
        HeartbeatEvent, LifeCycleEvent,
        MessageEvent, PokeEvent, GroupNameEvent, GroupTitleEvent,
        InputStatusEvent, ProfileLikeEvent, GroupGrayTipEvent,
        GroupIncreaseEvent, GroupDecreaseEvent, GroupBanEvent,
        GroupAdminEvent, GroupRecallEvent, GroupUploadEvent,
        GroupCardEvent, FriendAddEvent, FriendRecallEvent,
        EssenceEvent, MsgEmojiLikeEvent, BotOfflineEvent,
        OnlineFileSendEvent, OnlineFileReceiveEvent,
        FriendRequestEvent, GroupRequestEvent,
    )
    m = _EVENT_CLASS_MAP
    # meta
    m[("meta_event", "meta_event_type", "heartbeat")] = HeartbeatEvent
    m[("meta_event", "meta_event_type", "lifecycle")] = LifeCycleEvent
    # message
    m[("message", None, None)] = MessageEvent
    m[("message_sent", None, None)] = MessageEvent
    # notice → notify 子类型
    notify_map = {
        "poke": PokeEvent, "group_name": GroupNameEvent, "title": GroupTitleEvent,
        "input_status": InputStatusEvent, "profile_like": ProfileLikeEvent,
        "gray_tip": GroupGrayTipEvent,
    }
    for sub, cls in notify_map.items():
        m[("notice", "sub_type", sub)] = cls
    # notice → 直接 notice_type
    direct_map = {
        "group_increase": GroupIncreaseEvent, "group_decrease": GroupDecreaseEvent,
        "group_ban": GroupBanEvent, "group_admin": GroupAdminEvent,
        "group_recall": GroupRecallEvent, "group_upload": GroupUploadEvent,
        "group_card": GroupCardEvent, "friend_add": FriendAddEvent,
        "friend_recall": FriendRecallEvent, "essence": EssenceEvent,
        "group_msg_emoji_like": MsgEmojiLikeEvent,
        "bot_offline": BotOfflineEvent, "online_file_send": OnlineFileSendEvent,
        "online_file_receive": OnlineFileReceiveEvent,
    }
    for nt, cls in direct_map.items():
        m[("notice", "notice_type", nt)] = cls
    # request
    m[("request", "request_type", "friend")] = FriendRequestEvent
    m[("request", "request_type", "group")] = GroupRequestEvent


def _get_event_class(data: dict) -> type[NapCatEvent]:
    if not _EVENT_CLASS_MAP:
        _build_event_map()
    pt = data.get("post_type", "")
    # 精确匹配
    for (post_type, sub_key, sub_val), cls in _EVENT_CLASS_MAP.items():
        if post_type != pt:
            continue
        if sub_key is None:
            return cls
        if data.get(sub_key) == sub_val:
            return cls
    # notify 兜底: 如果 notice_type == "notify" 没匹配到，回退到 NapCatEvent
    return NapCatEvent


def parse_event(data: dict) -> NapCatEvent:
    """从原始 JSON dict 解析为具体的事件类型。

    用法:
        event = parse_event({"post_type": "message", ...})
        # event 是 MessageEvent 实例
    """
    cls = _get_event_class(data)
    return cls.model_validate(data)


# ==================== 具体事件类型 ====================

class HeartbeatEvent(NapCatEvent):
    """心跳事件（meta_event / heartbeat）。"""
    post_type: str = "meta_event"
    meta_event_type: str = "heartbeat"
    status: dict = Field(default_factory=lambda: {"online": True, "good": True})
    interval: int = 30000


class LifeCycleEvent(NapCatEvent):
    """生命周期事件（连接、启用、禁用）。"""
    post_type: str = "meta_event"
    meta_event_type: str = "lifecycle"
    sub_type: str = "connect"


class MessageEvent(NapCatEvent):
    """消息事件（私聊或群聊收到消息）。"""
    post_type: str = "message"
    message_type: str = "group"
    sub_type: Optional[str] = None
    message_id: int = 0
    message_seq: int = 0
    real_id: int = 0
    user_id: int = 0
    group_id: Optional[int] = None
    group_name: Optional[str] = None
    sender: Sender = Field(default_factory=lambda: Sender(user_id=0, nickname=""))
    message: Union[list, str] = Field(default_factory=list)
    raw_message: str = ""
    message_format: str = "array"
    font: int = 0
    real_seq: Optional[str] = None
    temp_source: Optional[int] = None
    target_id: Optional[int] = None
    emoji_likes_list: list[EmojiLike] = Field(default_factory=list)

    @property
    def is_temp_session(self) -> bool:
        """是否为「群聊临时会话」（群成员小窗）。

        这类消息在 OneBot 里是 `message_type="private"`，但会带来源 `group_id`
        （NapCat 还会给 `sub_type="group"`）。回复时**必须把 group_id 带回**
        `send_private_msg`，否则服务端会当普通好友私聊处理（非好友直接失败）。
        """
        if self.message_type != "private":
            return False
        return bool(self.group_id) or self.sub_type == "group"

    @property
    def plain_text(self) -> str:
        if isinstance(self.message, str):
            return self.message
        parts = []
        for seg in self.message:
            if isinstance(seg, dict):
                if seg.get("type") == "text":
                    parts.append(seg.get("data", {}).get("text", ""))
                elif seg.get("type") == "image":
                    parts.append("[图片]")
                elif seg.get("type") == "at":
                    qq = seg.get("data", {}).get("qq", "")
                    parts.append(f"[@{qq}]")
        return "".join(parts)

    def get_segments(self, seg_type: str) -> list[dict]:
        """获取指定类型的消息段"""
        if isinstance(self.message, str):
            return []
        return [s for s in self.message if isinstance(s, dict) and s.get("type") == seg_type]


class GroupIncreaseEvent(NapCatEvent):
    """群成员增加事件。"""
    post_type: str = "notice"
    notice_type: str = "group_increase"
    group_id: int = 0
    user_id: int = 0
    operator_id: int = 0
    sub_type: str = "approve"


class GroupDecreaseEvent(NapCatEvent):
    """群成员减少（退群/被踢）事件。"""
    post_type: str = "notice"
    notice_type: str = "group_decrease"
    group_id: int = 0
    user_id: int = 0
    operator_id: int = 0
    sub_type: str = "leave"


class GroupBanEvent(NapCatEvent):
    """群禁言 / 解除禁言事件。"""
    post_type: str = "notice"
    notice_type: str = "group_ban"
    group_id: int = 0
    user_id: int = 0
    operator_id: int = 0
    duration: int = 0
    sub_type: str = "ban"


class GroupAdminEvent(NapCatEvent):
    """群管理员设置 / 取消事件。"""
    post_type: str = "notice"
    notice_type: str = "group_admin"
    group_id: int = 0
    user_id: int = 0
    sub_type: str = "set"


class GroupRecallEvent(NapCatEvent):
    """群消息撤回事件。"""
    post_type: str = "notice"
    notice_type: str = "group_recall"
    group_id: int = 0
    user_id: int = 0
    operator_id: int = 0
    message_id: int = 0


class GroupUploadEvent(NapCatEvent):
    """群文件上传事件。"""
    post_type: str = "notice"
    notice_type: str = "group_upload"
    group_id: int = 0
    user_id: int = 0
    file: dict = Field(default_factory=dict)


class GroupCardEvent(NapCatEvent):
    """群成员名片变更事件。"""
    post_type: str = "notice"
    notice_type: str = "group_card"
    group_id: int = 0
    user_id: int = 0
    card_new: str = ""
    card_old: str = ""


class GroupNameEvent(NapCatEvent):
    """群名变更事件。"""
    post_type: str = "notice"
    notice_type: str = "notify"
    sub_type: str = "group_name"
    group_id: int = 0
    user_id: int = 0
    name_new: str = ""


class GroupTitleEvent(NapCatEvent):
    """群头衔变更事件。"""
    post_type: str = "notice"
    notice_type: str = "notify"
    sub_type: str = "title"
    group_id: int = 0
    user_id: int = 0
    title: str = ""


class PokeEvent(NapCatEvent):
    """戳一戳事件。"""
    post_type: str = "notice"
    notice_type: str = "notify"
    sub_type: str = "poke"
    target_id: int = 0
    user_id: int = 0
    sender_id: int = 0
    group_id: Optional[int] = None
    raw_info: Any = None


class InputStatusEvent(NapCatEvent):
    """输入状态（对方正在输入）事件。"""
    post_type: str = "notice"
    notice_type: str = "notify"
    sub_type: str = "input_status"
    status_text: str = "对方正在输入..."
    event_type: int = 1
    user_id: int = 0
    group_id: int = 0


class ProfileLikeEvent(NapCatEvent):
    """名片点赞事件。"""
    post_type: str = "notice"
    notice_type: str = "notify"
    sub_type: str = "profile_like"
    operator_id: int = 0
    operator_nick: str = ""
    times: int = 0


class GroupGrayTipEvent(NapCatEvent):
    """群灰条提示事件。"""
    post_type: str = "notice"
    notice_type: str = "notify"
    sub_type: str = "gray_tip"
    group_id: int = 0
    user_id: int = 0
    message_id: int = 0
    busi_id: str = ""
    content: str = ""
    raw_info: Any = None


class FriendAddEvent(NapCatEvent):
    """好友添加事件。"""
    post_type: str = "notice"
    notice_type: str = "friend_add"
    user_id: int = 0


class FriendRecallEvent(NapCatEvent):
    """好友消息撤回事件。"""
    post_type: str = "notice"
    notice_type: str = "friend_recall"
    user_id: int = 0
    message_id: int = 0


class EssenceEvent(NapCatEvent):
    """群精华消息变更事件。"""
    post_type: str = "notice"
    notice_type: str = "essence"
    group_id: int = 0
    message_id: int = 0
    sender_id: int = 0
    operator_id: int = 0
    sub_type: str = "add"


class MsgEmojiLikeEvent(NapCatEvent):
    """消息表情表态事件。"""
    post_type: str = "notice"
    notice_type: str = "group_msg_emoji_like"
    group_id: int = 0
    user_id: int = 0
    message_id: int = 0
    likes: list[dict] = Field(default_factory=list)
    is_add: bool = True


class BotOfflineEvent(NapCatEvent):
    """机器人离线事件。"""
    post_type: str = "notice"
    notice_type: str = "bot_offline"
    user_id: int = 0
    tag: str = "BotOfflineEvent"
    message: str = "BotOfflineEvent"


class OnlineFileSendEvent(NapCatEvent):
    """在线文件发送事件。"""
    post_type: str = "notice"
    notice_type: str = "online_file_send"
    peer_id: int = 0
    sub_type: str = "receive"


class OnlineFileReceiveEvent(NapCatEvent):
    """在线文件接收事件。"""
    post_type: str = "notice"
    notice_type: str = "online_file_receive"
    peer_id: int = 0
    sub_type: str = "cancel"


class FriendRequestEvent(NapCatEvent):
    """好友请求事件。"""
    post_type: str = "request"
    request_type: str = "friend"
    user_id: int = 0
    comment: str = ""
    flag: str = ""


class GroupRequestEvent(NapCatEvent):
    """入群请求事件。"""
    post_type: str = "request"
    request_type: str = "group"
    group_id: int = 0
    user_id: int = 0
    sub_type: str = "add"
    comment: str = ""
    flag: str = ""


# ==================== API 响应类型 ====================

class ApiResponse(BaseModel):
    """OneBot API 响应包装（status / retcode / data）。"""
    status: str
    retcode: int
    data: Any = None
    message: str = ""
    wording: str = ""
    echo: Any = None

    @property
    def ok(self) -> bool:
        return self.retcode == 0


class LoginInfo(BaseModel):
    """登录账号信息。"""
    user_id: int
    nickname: str
    sex: Optional[str] = None
    age: Optional[int] = None
    level: Optional[int] = None
    qid: Optional[str] = None
    login_days: Optional[int] = None


class StatusInfo(BaseModel):
    """运行状态信息。"""
    online: bool
    good: bool
    stat: dict = Field(default_factory=dict)


class VersionInfo(BaseModel):
    """版本信息。"""
    version: str
    version_code: str
    codename: str = ""
    protocol_version: str = ""


class SendMsgResult(BaseModel):
    """发送消息的返回结果。"""
    message_id: int
    res_id: Optional[str] = None
    forward_id: Optional[str] = None


class FileInfo(BaseModel):
    """文件信息（名称、大小、路径等）。"""
    file: str
    url: Optional[str] = None
    size: Optional[int] = None
    name: Optional[str] = None
    base64: Optional[str] = None
