"""ncqqpy —— Python 中间件 SDK for NapCatQQ WebSocket 协议。

公开 API 按**职责分类**放在子模块里，**不要**再从顶层一次性导入：

    # 客户端（连接 / 收发）
    from napcat.client import NapCatClient, GroupBinding

    # 插件系统
    from napcat.plugin import PluginBase, PluginManager

    # 事件与数据模型（MessageEvent / Sender / 枚举 / parse_event…）
    from napcat.models import MessageEvent, Sender

    # 事件总线与定时任务
    from napcat.events import EventBus, TimerManager

    # 消息构造
    from napcat.message import MessageChain

    # 命令 / 参数 Schema / 参数校验字段
    from napcat.command import CommandSchema, StrField, CommandManager

    # 声明式注册（@command / @event / @every）
    from napcat.registry import command

    # 权限
    from napcat.permission import PermissionManager

    # 错误类型与统一反馈
    from napcat.errors import PluginError, ErrorReporter

    # sqlite ORM
    from napcat.store import Database, Model, CharField

    # 配置 / 文件 / 服务端拉起 / 看门狗
    from napcat.config import ConfigManager
    from napcat.file_manager import FileManager
    from napcat.launcher import ServerLauncher
    from napcat.watchdog import Watchdog

为什么要分类：顶层一次性 re-export 会让 `import napcat` 把 client / aiohttp /
websockets 全部拉起来 —— 只用一个 `Model` 也要加载整个客户端；而且名字一多，就
分不清某个符号属于哪一层。**子模块就是分类**，导入路径本身说明了职责。

⚠️ 兼容层：`from napcat import X` 仍然可用（老代码不至于直接崩），但会发
`DeprecationWarning` 并指向正确模块。新代码请用上面的分类导入。
"""

__version__ = "1.0.0"

__all__ = ["__version__"]

#: 兼容层用：旧名字 → (拥有它的子模块, 模块里的原名)
_LEGACY = {'ApiClient': ['api', 'ApiClient'],
 'ApiError': ['api', 'ApiError'],
 'ApiResponse': ['models', 'ApiResponse'],
 'AutoField': ['store', 'AutoField'],
 'BaseField': ['command', 'BaseField'],
 'BoolField': ['command', 'BoolField'],
 'BooleanField': ['store', 'BooleanField'],
 'BotOfflineEvent': ['models', 'BotOfflineEvent'],
 'CharField': ['store', 'CharField'],
 'ChoiceField': ['command', 'ChoiceField'],
 'CommandExecutionError': ['command', 'CommandExecutionError'],
 'CommandHandler': ['command', 'CommandHandler'],
 'CommandManager': ['command', 'CommandManager'],
 'CommandPacket': ['command', 'CommandPacket'],
 'CommandParser': ['command', 'CommandParser'],
 'CommandSchema': ['command', 'CommandSchema'],
 'ConfigManager': ['config', 'ConfigManager'],
 'ContactType': ['message', 'ContactType'],
 'Database': ['store', 'Database'],
 'DateTimeField': ['store', 'DateTimeField'],
 'DefaultPermission': ['permission', 'DefaultPermission'],
 'DiceResult': ['message', 'DiceResult'],
 'DoesNotExist': ['store', 'DoesNotExist'],
 'DurationField': ['command', 'DurationField'],
 'ErrorReporter': ['errors', 'ErrorReporter'],
 'EssenceEvent': ['models', 'EssenceEvent'],
 'EventBus': ['events', 'EventBus'],
 'EventType': ['models', 'EventType'],
 'Field': ['store', 'Field'],
 'FieldError': ['store', 'FieldError'],
 'FileInfo': ['models', 'FileInfo'],
 'FileManager': ['file_manager', 'FileManager'],
 'FloatField': ['store', 'FloatField'],
 'FriendAddEvent': ['models', 'FriendAddEvent'],
 'FriendRecallEvent': ['models', 'FriendRecallEvent'],
 'FriendRequestEvent': ['models', 'FriendRequestEvent'],
 'GROUP_AT_HINT': ['command', 'GROUP_AT_HINT'],
 'Group': ['models', 'Group'],
 'GroupAdminEvent': ['models', 'GroupAdminEvent'],
 'GroupBanEvent': ['models', 'GroupBanEvent'],
 'GroupBinding': ['client', 'GroupBinding'],
 'GroupCardEvent': ['models', 'GroupCardEvent'],
 'GroupDecreaseEvent': ['models', 'GroupDecreaseEvent'],
 'GroupFile': ['models', 'GroupFile'],
 'GroupFileFolder': ['models', 'GroupFileFolder'],
 'GroupGrayTipEvent': ['models', 'GroupGrayTipEvent'],
 'GroupIncreaseEvent': ['models', 'GroupIncreaseEvent'],
 'GroupMember': ['models', 'GroupMember'],
 'GroupMemberRole': ['models', 'GroupMemberRole'],
 'GroupNameEvent': ['models', 'GroupNameEvent'],
 'GroupRecallEvent': ['models', 'GroupRecallEvent'],
 'GroupRequestEvent': ['models', 'GroupRequestEvent'],
 'GroupTitleEvent': ['models', 'GroupTitleEvent'],
 'GroupUploadEvent': ['models', 'GroupUploadEvent'],
 'HeartbeatEvent': ['models', 'HeartbeatEvent'],
 'InputStatusEvent': ['models', 'InputStatusEvent'],
 'IntField': ['command', 'IntField'],
 'IntegerField': ['store', 'IntegerField'],
 'JSONField': ['store', 'JSONField'],
 'LaunchResult': ['launcher', 'LaunchResult'],
 'LifeCycleEvent': ['models', 'LifeCycleEvent'],
 'LifeCycleSubType': ['models', 'LifeCycleSubType'],
 'LoginInfo': ['models', 'LoginInfo'],
 'Manager': ['store', 'Manager'],
 'MessageChain': ['message', 'MessageChain'],
 'MessageEvent': ['models', 'MessageEvent'],
 'MessageSubType': ['models', 'MessageSubType'],
 'MessageType': ['models', 'MessageType'],
 'Model': ['store', 'Model'],
 'MsgEmojiLikeEvent': ['models', 'MsgEmojiLikeEvent'],
 'MultipleObjectsReturned': ['store', 'MultipleObjectsReturned'],
 'MusicPlatform': ['message', 'MusicPlatform'],
 'NOT_PROVIDED': ['store', 'NOT_PROVIDED'],
 'NapCatClient': ['client', 'NapCatClient'],
 'NapCatError': ['errors', 'NapCatError'],
 'NapCatEvent': ['models', 'NapCatEvent'],
 'OnlineFileReceiveEvent': ['models', 'OnlineFileReceiveEvent'],
 'OnlineFileSendEvent': ['models', 'OnlineFileSendEvent'],
 'PLACEHOLDER_LEGEND': ['command', 'PLACEHOLDER_LEGEND'],
 'Params': ['command', 'Params'],
 'PermissionManager': ['permission', 'PermissionManager'],
 'PluginAccessError': ['errors', 'PluginAccessError'],
 'PluginBase': ['plugin', 'PluginBase'],
 'PluginConfigError': ['errors', 'PluginConfigError'],
 'PluginDependencyError': ['errors', 'PluginDependencyError'],
 'PluginError': ['errors', 'PluginError'],
 'PluginExternalError': ['errors', 'PluginExternalError'],
 'PluginLifecycleError': ['errors', 'PluginLifecycleError'],
 'PluginLoadError': ['errors', 'PluginLoadError'],
 'PluginManager': ['plugin', 'PluginManager'],
 'PluginRateLimitError': ['errors', 'PluginRateLimitError'],
 'PluginRuntimeError': ['errors', 'PluginRuntimeError'],
 'PluginTimeoutError': ['errors', 'PluginTimeoutError'],
 'PokeEvent': ['models', 'PokeEvent'],
 'PokeType': ['message', 'PokeType'],
 'ProfileLikeEvent': ['models', 'ProfileLikeEvent'],
 'QQField': ['command', 'QQField'],
 'QuerySet': ['store', 'QuerySet'],
 'RPSResult': ['message', 'RPSResult'],
 'SchemaError': ['command', 'SchemaError'],
 'SegmentType': ['models', 'SegmentType'],
 'SendMsgResult': ['models', 'SendMsgResult'],
 'Sender': ['models', 'Sender'],
 'ServerLauncher': ['launcher', 'ServerLauncher'],
 'StatusInfo': ['models', 'StatusInfo'],
 'StoreError': ['store', 'StoreError'],
 'StoreIntegrityError': ['store', 'IntegrityError'],
 'StrField': ['command', 'StrField'],
 'TextField': ['store', 'TextField'],
 'TimerManager': ['events', 'TimerManager'],
 'User': ['models', 'User'],
 'UserSex': ['models', 'UserSex'],
 'ValidatedParams': ['command', 'ValidatedParams'],
 'ValidationError': ['command', 'ValidationError'],
 'VariadicField': ['command', 'VariadicField'],
 'VersionInfo': ['models', 'VersionInfo'],
 'Watchdog': ['watchdog', 'Watchdog'],
 'build_usage': ['command', 'build_usage'],
 'describe_fields': ['command', 'describe_fields'],
 'error_code': ['errors', 'error_code'],
 'format_field_usage': ['command', 'format_field_usage'],
 'normalize_text': ['command', 'normalize_text'],
 'optional_placeholder': ['command', 'optional_placeholder'],
 'parse_event': ['models', 'parse_event'],
 'required_placeholder': ['command', 'required_placeholder'],
 'short_reason': ['errors', 'short_reason']}

#: 允许 `napcat.<子模块>` 这种属性访问（`import napcat` 后直接取子模块）
_SUBMODULES = {
    "api", "client", "command", "config", "deps", "errors", "events",
    "file_manager", "launcher", "message", "models", "permission", "persist",
    "plugin", "qr_text", "registry", "scope", "store", "watchdog",
}


def __getattr__(name: str):
    """PEP 562：兼容旧的 `from napcat import X`，同时支持 `napcat.<子模块>`。

    - 旧名字：导入拥有它的子模块并返回，附 DeprecationWarning；
    - 子模块名：按需导入并返回模块对象（`import napcat` 本身不再拉重依赖）。
    """
    if name in _LEGACY:
        module_name, original = _LEGACY[name]
        import importlib
        import warnings

        warnings.warn(
            f"from napcat import {name} 已弃用：请改 "
            f"from napcat.{module_name} import {original}",
            DeprecationWarning,
            stacklevel=2,
        )
        module = importlib.import_module(f".{module_name}", __name__)
        return getattr(module, original)
    if name in _SUBMODULES:
        import importlib

        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module 'napcat' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(_LEGACY) | _SUBMODULES)
