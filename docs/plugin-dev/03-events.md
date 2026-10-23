# 三、消息事件处理

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[插件可用资源](02-resources.md) · 下一章：[命令注册](04-commands.md)


## 3.1 动态卸载（事件/命令/定时器）

```python
from napcat.command import normalize_text          # 归一化入口（框架统一提供）
class MyPlugin(PluginBase):
    async def on_enable(self):
        @self.on_message()
        async def temp_handler(event: MessageEvent):
            # ⚠️ plain_text 带首尾空白（用户可能发 "  /stop\n"），
            # 自行判断前必须 normalize_text()；命令系统内部已自动归一化
            if normalize_text(event.plain_text) == "/stop":
                self.off_event(MessageEvent, temp_handler)
                await self.api.send_group_msg(
                    group_id=str(event.group_id),
                    message=MessageChain().text("已停止监听"),
                )

        @self.register_command("disable_me", permission="admin")
        async def cmd_disable(packet):
            self.unregister_command("disable_me")
            self.cancel_schedule("hourly")
            return "命令和定时任务已卸载"

        @self.schedule(3600, name="hourly")
        async def hourly(): ...
```

## 3.2 基础消息处理

```python
from napcat.plugin import PluginBase
from napcat.models import MessageEvent
from napcat.message import MessageChain
class MyPlugin(PluginBase):
    name = "my"
    version = "1.0.0"

    async def on_enable(self):
        @self.on_message()
        async def on_all(event: MessageEvent):
            self.logger.info(f"[{event.group_id}] {event.sender.nickname}: {event.plain_text}")

        @self.on_message(group_id=123456)
        async def on_specific_group(event: MessageEvent):
            await self.api.send_group_msg(
                group_id=str(event.group_id),
                message=MessageChain().text("本群消息已记录"),
            )

        @self.on_message(private=True)
        async def on_private(event: MessageEvent):
            await self.api.send_private_msg(
                user_id=str(event.user_id),
                message=MessageChain().text("收到私聊"),
            )
```

## 3.3 指定事件类型

```python
from napcat.models import (
    GroupIncreaseEvent,
    GroupDecreaseEvent,
    GroupBanEvent,
    FriendRequestEvent,
    PokeEvent,
)
class MyPlugin(PluginBase):
    async def on_enable(self):
        @self.on_event(GroupIncreaseEvent)
        async def welcome(event: GroupIncreaseEvent):
            await self.api.send_group_msg(
                group_id=str(event.group_id),
                message=MessageChain().at(event.user_id).text(" 欢迎!"),
            )

        @self.on_event(FriendRequestEvent)
        async def auto_accept(event: FriendRequestEvent):
            await self.api.set_friend_add_request(flag=event.flag, approve=True)
```

---
