# 一、快速开始

> 文档索引：[docs/README.md](../README.md) · 上级：[使用指南](README.md) · 下一章：[客户端配置](02-client.md)


## 1.1 安装

```bash
pip install ncqqpy
# 或从源码
git clone https://github.com/Green1423/ncqqpy && cd ncqqpy
pip install -e .
```

**依赖**: `websockets>=13.0` `pydantic>=2.0` `aiohttp>=3.9` `aiofiles>=24.0`

## 1.2 前提条件

确保 NapCatQQ 的 WebSocket 服务端已启动（默认 `ws://127.0.0.1:3001`）。

## 1.3 最小运行示例

```python
# main.py
from napcat.client import NapCatClient
from napcat.message import MessageChain
client = NapCatClient(
    ws_url="ws://127.0.0.1:3001",
    token="your_token",
    superusers=[123456789],
)

@client.on_message()
async def handle(event):
    if event.plain_text == "ping":
        await client.api.send_group_msg(
            group_id=str(event.group_id),
            message=MessageChain().text("pong!"),
        )

client.run_blocking()  # 等同于 asyncio.run(client.run())
```

```bash
python main.py
```

---
