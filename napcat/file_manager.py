"""
文件管理系统 —— 对接 NapCat 的**流式文件接口**。

- 下载：`download_file_stream` / `download_file_image_stream` /
  `download_file_record_stream`，分片以 Base64 经 WebSocket 传回，边收边写；
- 上传：`upload_file_stream`，本地文件分块（Base64）上传到 NapCat，
  返回服务器侧路径（可用于发消息 / 群文件）；
- 清理：`clean_stream_temp_file`。

与旧的「先 `download_file` 落到 NapCat 临时目录、再读本地文件」相比，
流式接口不依赖框架与 NapCat 共享文件系统，适合远程部署。
"""
from __future__ import annotations

import base64
import hashlib
import tempfile
from pathlib import Path
from typing import AsyncIterator, Callable

import aiofiles

from .api import ApiClient
from .models import FileInfo


class FileManager:
    """文件管理器：信息查询、流式上传/下载、临时文件清理。

    用法：

        fm = FileManager(api_client)

        # 流式下载并写文件（带进度）
        await fm.download_to_file("https://example.com/large.bin", "/tmp/large.bin",
                                  on_progress=lambda got, total: print(got, total))

        # 流式上传（返回 NapCat 侧文件信息）
        info = await fm.upload_stream("./big.zip", name="big.zip")
    """

    def __init__(self, api: ApiClient, chunk_size: int = 512 * 1024,
                 temp_dir: str = None):
        self.api = api
        self.chunk_size = chunk_size          # 512KB（上传分块；下载由服务端决定）
        self.temp_dir = temp_dir or tempfile.gettempdir()

    # ==================== 文件信息 ====================

    async def get_image(self, file_id: str) -> FileInfo:
        """获取图片信息（路径 / URL / 尺寸等）。"""
        return await self.api.get_image(file_id)

    async def get_record(self, file_id: str) -> FileInfo:
        """获取语音信息（含下载 URL）。"""
        return await self.api.get_record(file_id)

    async def get_file(self, file_id: str) -> FileInfo:
        """获取文件信息（含下载 URL）。"""
        return await self.api.get_file(file_id)

    # ==================== 流式下载 ====================

    async def download_stream(self, url: str, chunk_size: int = None,
                              *, file_id: str = None,
                              timeout: float = 60.0) -> AsyncIterator[bytes]:
        """流式下载文件，逐块产出字节。

        Args:
            url: 文件 URL / NapCat 可访问的路径。
            chunk_size: 服务端分块大小（默认 64KB）。
            file_id: 文件 ID（与 `url` 二选一）。
            timeout: 单条数据包超时（秒）。
        """
        async for chunk in self.api.download_file_stream(
                file=url, file_id=file_id,
                chunk_size=chunk_size or 65536, timeout=timeout):
            yield chunk

    async def download_image_stream(self, *, file: str = None,
                                    file_id: str = None,
                                    chunk_size: int = None) -> AsyncIterator[bytes]:
        """流式下载图片。"""
        async for chunk in self.api.download_file_image_stream(
                file=file, file_id=file_id, chunk_size=chunk_size or 65536):
            yield chunk

    async def download_record_stream(self, *, file: str = None, file_id: str = None,
                                     out_format: str = "mp3",
                                     chunk_size: int = None) -> AsyncIterator[bytes]:
        """流式下载语音（`out_format` 可转码）。"""
        async for chunk in self.api.download_file_record_stream(
                file=file, file_id=file_id, out_format=out_format,
                chunk_size=chunk_size or 65536):
            yield chunk

    async def download_to_file(self, url: str, save_path: str,
                               on_progress: Callable[[int, int], None] = None,
                               *, file_id: str = None,
                               timeout: float = 60.0) -> str:
        """流式下载并写入本地文件。

        Args:
            url: 文件 URL / 路径。
            save_path: 本地保存路径（自动建父目录）。
            on_progress: 进度回调 `(已接收字节, 总字节)`；总大小未知时为 -1。
            file_id: 文件 ID（与 `url` 二选一）。
            timeout: 单条数据包超时（秒）。

        Returns:
            本地文件路径。
        """
        path = Path(save_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        received = 0
        async with aiofiles.open(path, "wb") as fh:
            async for event in self.api.iter_file_stream(
                    file=url, file_id=file_id, chunk_size=65536, timeout=timeout):
                data_type = event.get("data_type")
                if data_type == "file_info":
                    total = int(event.get("file_size") or 0)
                elif data_type == "file_chunk":
                    chunk = base64.b64decode(event.get("data") or "")
                    await fh.write(chunk)
                    received += len(chunk)
                    if on_progress:
                        on_progress(received, total or -1)
        return str(path)

    async def download(self, url: str, save_path: str = None,
                       progress_callback: Callable[[int, int], None] = None,
                       *, file_id: str = None) -> str:
        """下载文件到本地，返回路径（不指定 `save_path` 时放系统临时目录）。"""
        target = save_path or self.temp_path(
            suffix=Path(url.split("?")[0]).suffix if url else "")
        return await self.download_to_file(url, target, progress_callback,
                                           file_id=file_id)

    async def download_bytes(self, url: str, *, file_id: str = None) -> bytes:
        """下载到内存（小文件用；大文件请用 `download_to_file`）。"""
        return b"".join([chunk async for chunk in self.download_stream(
            url, file_id=file_id)])

    # ==================== 流式上传 ====================

    async def upload_stream(self, file_path: str, name: str = None, *,
                            on_progress: Callable[[int, int], None] = None,
                            chunk_size: int = None,
                            retention: int = 300000,
                            expected_sha256: str = None) -> dict:
        """把本地文件分块流式上传到 NapCat。

        Args:
            file_path: 本地文件路径。
            name: 上传后的文件名（默认取原文件名）。
            on_progress: 进度回调 `(已发送字节, 总字节)`。
            chunk_size: 分块大小（默认取 FileManager 的 `chunk_size`）。
            retention: 服务器保留时间（毫秒，默认 5 分钟）。
            expected_sha256: 期望的 SHA256（提供时服务端校验）。

        Returns:
            服务端返回的文件信息（含 `file_path` / `file_name` / `file_size` /
            `sha256` 等，具体字段以 NapCat 版本为准）。
        """
        return await self.api.upload_file_stream(
            file_path, name=name or Path(file_path).name,
            chunk_size=chunk_size or self.chunk_size,
            retention=retention, expected_sha256=expected_sha256,
            on_progress=on_progress)

    async def upload_group_file(self, group_id: str, file_path: str, name: str = "",
                                folder: str = "", upload_notify: bool = True) -> str:
        """上传群文件（使用 NapCat 的 `upload_group_file`，读本地路径/URL）。

        Args:
            group_id: 群号。
            file_path: 本地路径或 URL。
            name: 群文件里的文件名。
            folder: 目标文件夹 ID（旧字段）。
            upload_notify: 是否执行上传（官方字段 `upload_file`，默认 True）。

        Returns:
            群文件 `file_id`（服务端未返回时为空串）。
        """
        path = Path(file_path)
        name = name or path.name
        result = await self.api.upload_group_file(
            group_id=group_id, file=str(path.absolute()),
            name=name, folder=folder, upload_file=upload_notify)
        return result.get("file_id", "") if isinstance(result, dict) else ""

    # ==================== 清理 / 校验 ====================

    async def clean_temp(self) -> None:
        """清理 NapCat 侧流式传输临时文件。"""
        await self.api.clean_stream_temp_file()

    @staticmethod
    async def file_sha256(file_path: str) -> str:
        """计算文件 SHA256（用于 `expected_sha256` / 上传后校验）。"""
        sha = hashlib.sha256()
        async with aiofiles.open(file_path, "rb") as fh:
            while True:
                chunk = await fh.read(64 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest()

    @staticmethod
    def temp_path(suffix: str = "") -> str:
        """生成临时文件路径（不创建文件）。"""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as fh:
            return fh.name
