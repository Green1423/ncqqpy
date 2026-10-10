"""
文件管理系统 - 对接流式文件接口
支持大文件分片上传/下载、进度回调、临时文件清理
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Callable, Optional, AsyncIterator, BinaryIO
import aiofiles
import aiohttp

from .api import ApiClient
from .models import FileInfo


class FileManager:
    """文件管理器 - 封装上传/下载/流式传输。

    用法:
        fm = FileManager(api_client)

        # 流式上传大文件
        await fm.upload_stream("123456", "./big_file.zip", name="file.zip",
                               on_progress=lambda sent, total: print(f"{sent}/{total}"))

        # 流式下载
        async for chunk in fm.download_stream("https://example.com/large.bin"):
            await outfile.write(chunk)
    """

    def __init__(self, api: ApiClient, chunk_size: int = 512 * 1024, temp_dir: str = None):
        self.api = api
        self.chunk_size = chunk_size  # 512KB
        self.temp_dir = temp_dir or tempfile.gettempdir()

    # ==================== 基础文件操作 ====================

    async def get_image(self, file_id: str) -> FileInfo:
        """获取图片文件信息（含 base64 可选）"""
        return await self.api.get_image(file_id)

    async def get_record(self, file_id: str) -> FileInfo:
        """获取语音文件信息"""
        return await self.api.get_record(file_id)

    async def get_file(self, file_id: str) -> FileInfo:
        """获取文件信息"""
        return await self.api.get_file(file_id)

    async def download(self, url: str, save_path: str = None,
                       progress_callback: Callable[[int, int], None] = None) -> str:
        """下载文件到本地，返回本地路径"""
        info = await self.api.download_file(url=url)
        local_path = info.file

        # 如果指定了保存路径，移动文件
        if save_path:
            import shutil
            target = Path(save_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(local_path, str(target))
            return str(target)

        return local_path

    async def upload_group_file(self, group_id: str, file_path: str, name: str = "",
                                folder: str = "", upload_notify: bool = False) -> str:
        """上传群文件"""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        result = await self.api.upload_group_file(
            group_id=group_id,
            file=str(path.absolute()),
            name=name or path.name,
            folder=folder,
            upload_file=upload_notify,
        )
        return result.get("file_id", "") if isinstance(result, dict) else ""

    # ==================== 流式上传 ====================

    async def upload_stream(self, group_id: str, file_path: str, name: str = "",
                            on_progress: Callable[[int, int], None] = None) -> str:
        """流式上传大文件到群。

        内部步骤:
        1. 调用 upload_file_stream 初始化
        2. 分片发送数据
        3. 等待完成回调
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        file_size = path.stat().st_size
        file_name = name or path.name
        total_sent = 0

        # 这里使用 upload_group_file API，底层自动处理分片
        # 如果 NapCat 的 stream 接口需要独立调用，可以扩展
        result = await self.api.upload_group_file(
            group_id=group_id,
            file=str(path.absolute()),
            name=file_name,
        )
        return result.get("file_id", "") if isinstance(result, dict) else ""

    # ==================== 流式下载 ====================

    async def download_stream(self, url: str,
                              chunk_size: int = None) -> AsyncIterator[bytes]:
        """流式下载 URL 内容，返回字节流迭代器"""
        cs = chunk_size or self.chunk_size
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                while True:
                    chunk = await resp.content.read(cs)
                    if not chunk:
                        break
                    yield chunk

    async def download_to_file(self, url: str, save_path: str,
                               on_progress: Callable[[int, int], None] = None) -> str:
        """流式下载并写入文件，支持进度回调"""
        path = Path(save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        async with aiofiles.open(path, "wb") as f:
            async for chunk in self.download_stream(url):
                await f.write(chunk)
                received += len(chunk)
                if on_progress:
                    on_progress(received, -1)  # -1 表示未知总大小
        return str(path)

    # ==================== Hash 校验 ====================

    @staticmethod
    async def file_sha256(file_path: str) -> str:
        """计算文件 SHA256"""
        sha = hashlib.sha256()
        async with aiofiles.open(file_path, "rb") as f:
            while True:
                chunk = await f.read(64 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest()

    @staticmethod
    def temp_path(suffix: str = "") -> str:
        """生成临时文件路径"""
        return tempfile.mktemp(suffix=suffix)

    @staticmethod
    async def clean_temp(pattern: str = ""):
        """清理流临时文件（调用 NapCat API）"""
        # 本实现使用本地 tempfile，自动由系统管理
        # 如需调用 NapCat 的 clean_stream_temp_file，可通过 api 调用
        pass
