# 十一、高级用法

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 上一章：[定时事件](10-timers.md) · 下一章：[API 参考速查表](12-api-reference.md)


## 11.1 EventBus 直接使用

```python
bus = client.event_bus

# 注册
@bus.on(HeartbeatEvent)
async def on_heartbeat(event):
    print(f"心跳: {event.interval}ms")

# 通配
@bus.on_any()
async def on_all(event):
    print(type(event).__name__)

# 手动分发
await bus.dispatch(some_event)

# 取消注册
bus.off(MessageEvent, handler_func)
```

## 11.2 低频 API 直接调用

`ApiClient` 封装过的 API 列表见(12-api-reference.md#十二api-参考速查表)。如需调用未封装的 API：

```python
from napcat.api import ApiClient

# 通过 _call 直接调用
data = await client.api._call("some_action", {"param": "value"})
```

## 11.3 自定义日志

```python
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("napcat")
logger.setLevel(logging.DEBUG)

# 插件内使用
class MyPlugin(PluginBase):
    async def on_enable(self):
        self.logger.info("插件启用")
```

## 11.4 事件中发送 API

```python
@client.on_message()
async def handle(event):
    # 使用 client.api 直接调用
    await client.api.send_group_msg(
        group_id=str(event.group_id),
        message=MessageChain().text("回复"),
    )
```

---
