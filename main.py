"""
ncqqpy 启动示例 — 默认从 config.json 读取配置，构造参数可覆盖
"""
import asyncio
import os

from napcat.client import NapCatClient


async def main():
    # 不传任何参数 → 全部从 config.json 读取
    # 传参则覆盖配置文件值，例如: NapCatClient(superusers=[123])
    client = NapCatClient()

    try:
        await client.start()
    except KeyboardInterrupt:
        print("\n收到中断信号，正在退出...")
    finally:
        await client.stop()


if __name__ == "__main__":
    asyncio.run(main())
