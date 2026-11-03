# 十二、完整插件示例

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[文件管理](11-files.md) · 下一章：[热重载](13-hot-reload.md)


```python
# plugins/welcome/__init__.py
from napcat.plugin import PluginBase
from napcat.models import MessageEvent, GroupIncreaseEvent
from napcat.message import MessageChain
from napcat.command import CommandSchema, StrField
class WelcomeSchema(CommandSchema):
    text = StrField(default="欢迎加入！", max_length=200)

class WelcomePlugin(PluginBase):
    name = "welcome"
    version = "1.0.0"
    description = "入群欢迎 + 自定义欢迎语"
    author = "ncqqpy"

    async def on_load(self):
        self._welcome_text = "欢迎加入！"

    async def on_enable(self):
        # 入群欢迎
        @self.on_event(GroupIncreaseEvent)
        async def say_hello(event: GroupIncreaseEvent):
            await self.api.send_group_msg(
                group_id=str(event.group_id),
                message=MessageChain()
                    .at(event.user_id)
                    .text(f" {self._welcome_text}"),
            )

        # 修改欢迎语
        @self.register_command("welcome", schema=WelcomeSchema,
                               permission="admin", scope="group")
        async def set_welcome(params):
            self._welcome_text = params.text
            return f"欢迎语已改为: {self._welcome_text}"

        # 定时清理
        @self.schedule(86400, name="daily_cleanup")
        async def cleanup():
            self.logger.info("每日清理完成")
```

---
