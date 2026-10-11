"""
ncqqpy - Python 中间件 SDK for NapCatQQ WebSocket 协议

Usage:
    from napcat import NapCatClient, MessageChain, normalize_text

    client = NapCatClient(ws_url="ws://127.0.0.1:3001", token="your_token")

    @client.on_message()
    async def handle(event):
        # ⚠️ plain_text 是原样的：用户可能发 "  /ping\n"，自己判断一定要归一化
        if normalize_text(event.plain_text) == "ping":
            await client.api.send_group_msg(
                group_id=str(event.group_id),
                message=MessageChain().text("pong!"),
            )

    client.run_blocking()
"""

from .client import NapCatClient, GroupBinding
from .config import ConfigManager
from .models import (
    NapCatEvent, MessageEvent, HeartbeatEvent, LifeCycleEvent,
    GroupIncreaseEvent, GroupDecreaseEvent, GroupBanEvent, GroupAdminEvent,
    GroupRecallEvent, GroupUploadEvent, GroupCardEvent, GroupNameEvent,
    GroupTitleEvent, PokeEvent, InputStatusEvent, ProfileLikeEvent,
    GroupGrayTipEvent, FriendAddEvent, FriendRecallEvent, EssenceEvent,
    MsgEmojiLikeEvent, BotOfflineEvent, OnlineFileSendEvent, OnlineFileReceiveEvent,
    FriendRequestEvent, GroupRequestEvent,
    Sender, User, Group, GroupMember, GroupFile, GroupFileFolder,
    ApiResponse, LoginInfo, StatusInfo, VersionInfo, SendMsgResult, FileInfo,
    EventType, MessageType, MessageSubType, GroupMemberRole, UserSex,
    SegmentType, LifeCycleSubType,
    parse_event,
)
from .message import (
    MessageChain, MusicPlatform, PokeType, DiceResult, RPSResult, ContactType,
)
from .api import ApiClient, ApiError
from .events import EventBus, TimerManager
from .command import (
    CommandManager, CommandPacket, CommandParser, CommandHandler,
    CommandSchema, ValidatedParams, Params, GROUP_AT_HINT,
    PLACEHOLDER_LEGEND, required_placeholder, optional_placeholder,
    normalize_text,
    format_field_usage, build_usage, describe_fields,
    SchemaError, ValidationError, CommandExecutionError,
    BaseField, StrField, IntField, FloatField, BoolField,
    QQField, DurationField, ChoiceField, VariadicField,
)
from .permission import PermissionManager, DefaultPermission
from .errors import (
    NapCatError, PluginError, PluginRuntimeError, PluginConfigError,
    PluginExternalError, PluginTimeoutError, PluginRateLimitError,
    PluginAccessError, PluginLoadError, PluginLifecycleError,
    PluginDependencyError,
    ErrorReporter, error_code, short_reason,
)
from .file_manager import FileManager
from .launcher import ServerLauncher, LaunchResult
from .watchdog import Watchdog
from .plugin import PluginBase, PluginManager

__version__ = "1.0.0"
__all__ = [
    # Client
    "NapCatClient", "GroupBinding",
    # Models — Events
    "NapCatEvent", "MessageEvent", "HeartbeatEvent", "LifeCycleEvent",
    "GroupIncreaseEvent", "GroupDecreaseEvent", "GroupBanEvent", "GroupAdminEvent",
    "GroupRecallEvent", "GroupUploadEvent", "GroupCardEvent", "GroupNameEvent",
    "GroupTitleEvent", "PokeEvent", "InputStatusEvent", "ProfileLikeEvent",
    "GroupGrayTipEvent", "FriendAddEvent", "FriendRecallEvent", "EssenceEvent",
    "MsgEmojiLikeEvent", "BotOfflineEvent", "OnlineFileSendEvent", "OnlineFileReceiveEvent",
    "FriendRequestEvent", "GroupRequestEvent",
    # Models — Data
    "Sender", "User", "Group", "GroupMember", "GroupFile", "GroupFileFolder",
    "ApiResponse", "LoginInfo", "StatusInfo", "VersionInfo", "SendMsgResult", "FileInfo",
    # Models — Enums & Utils
    "EventType", "MessageType", "MessageSubType", "GroupMemberRole", "UserSex",
    "SegmentType", "LifeCycleSubType", "parse_event",
    # Message
    "MessageChain", "MusicPlatform", "PokeType", "DiceResult", "RPSResult", "ContactType",
    # API
    "ApiClient", "ApiError",
    # Events
    "EventBus", "TimerManager",
    # Command
    "CommandManager", "CommandPacket", "CommandParser", "CommandHandler",
    "CommandSchema", "ValidatedParams", "Params", "GROUP_AT_HINT",
    "PLACEHOLDER_LEGEND", "required_placeholder", "optional_placeholder",
    "normalize_text",
    "format_field_usage", "build_usage", "describe_fields",
    "SchemaError", "ValidationError", "CommandExecutionError",
    "BaseField", "StrField", "IntField", "FloatField", "BoolField",
    "QQField", "DurationField", "ChoiceField", "VariadicField",
    # Permission
    "PermissionManager", "DefaultPermission",
    # Errors（插件用这些类型说明失败性质；反馈由框架统一投递）
    "NapCatError", "PluginError", "PluginRuntimeError", "PluginConfigError",
    "PluginExternalError", "PluginTimeoutError", "PluginRateLimitError",
    "PluginAccessError", "PluginLoadError", "PluginLifecycleError",
    "PluginDependencyError",
    "ErrorReporter", "error_code", "short_reason",
    # File
    "FileManager",
    # Launcher / Watchdog
    "ServerLauncher", "LaunchResult", "Watchdog",
    # Plugin
    "PluginBase", "PluginManager",
]
