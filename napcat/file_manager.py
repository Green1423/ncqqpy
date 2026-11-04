"""
文件管理系统 —— 插件访问文件能力的**唯一入口**。

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
import math
import uuid
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




    # ==================== 文件信息 ====================

    async def get_image(self, file: str = None, file_id: str = None) -> FileInfo:
        params = {}
        if file: params["file"] = file
        if file_id: params["file_id"] = file_id
        data = await self.api._call("get_image", params)
        return FileInfo(**data) if isinstance(data, dict) else data

    async def get_record(self, file: str = None, file_id: str = None,
                         out_format: str = "mp3") -> FileInfo:
        params = {"out_format": out_format}
        if file: params["file"] = file
        if file_id: params["file_id"] = file_id
        data = await self.api._call("get_record", params)
        return FileInfo(**data) if isinstance(data, dict) else data

    async def get_file(self, file: str = None, file_id: str = None) -> FileInfo:
        params = {}
        if file: params["file"] = file
        if file_id: params["file_id"] = file_id
        data = await self.api._call("get_file", params)
        return FileInfo(**data) if isinstance(data, dict) else data

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
        async for chunk in self.download_file_stream(
                file=url, file_id=file_id,
                chunk_size=chunk_size or 65536, timeout=timeout):
            yield chunk

    async def download_image_stream(self, *, file: str = None,
                                    file_id: str = None,
                                    chunk_size: int = None) -> AsyncIterator[bytes]:
        """流式下载图片。"""
        async for chunk in self.download_file_image_stream(
                file=file, file_id=file_id, chunk_size=chunk_size or 65536):
            yield chunk

    async def download_record_stream(self, *, file: str = None, file_id: str = None,
                                     out_format: str = "mp3",
                                     chunk_size: int = None) -> AsyncIterator[bytes]:
        """流式下载语音（`out_format` 可转码）。"""
        async for chunk in self.download_file_record_stream(
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
            async for event in self.iter_file_stream(
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
        return await self.upload_file_stream(
            file_path, name=name or Path(file_path).name,
            chunk_size=chunk_size or self.chunk_size,
            retention=retention, expected_sha256=expected_sha256,
            on_progress=on_progress)

    async def upload_group_file(self, group_id: str, file: str = None,
                                name: str = "", folder: str = "",
                                upload_notify: bool = True, *,
                                file_path: str = None) -> str:
        """上传群文件（使用 NapCat 的 `upload_group_file`，读本地路径/URL）。

        Args:
            group_id: 群号。
            file: 本地路径或 URL（官方字段名）。
            name: 群文件里的文件名。
            folder: 目标文件夹 ID（旧字段）。
            upload_notify: 是否执行上传（官方字段 `upload_file`，默认 True）。
            file_path: `file` 的旧别名（兼容 `upload_group_file(file_path=...)`）。

        Returns:
            群文件 `file_id`（服务端未返回时为空串）。
        """
        target = file or file_path
        if not target:
            raise ValueError("upload_group_file 需要 file（或 file_path）")
        path = Path(target)
        name = name or path.name
        params = {"group_id": group_id, "file": str(path.absolute()),
                  "name": name, "upload_file": upload_notify}
        if folder:
            params["folder"] = folder
        result = await self.api._call("upload_group_file", params)
        return result.get("file_id", "") if isinstance(result, dict) else ""

    def _download_params(self, file, file_id, chunk_size) -> dict:
        params: dict = {"chunk_size": int(chunk_size or 64 * 1024)}
        if file:
            params["file"] = file
        if file_id:
            params["file_id"] = file_id
        return params

    async def _download_bytes(self, action: str, *, file=None, file_id=None,
                              chunk_size: int = 65536, timeout: float = 60.0,
                              out_format: str = None):
        params = self._download_params(file, file_id, chunk_size)
        if out_format:
            params["out_format"] = out_format
        async for event in self.api._stream_packets(action, params, timeout):
            if event.get("data_type") == "file_chunk":
                yield base64.b64decode(event.get("data") or "")

    async def download_file_stream(self, *, file: str = None, file_id: str = None,
                                   chunk_size: int = 65536,
                                   timeout: float = 60.0) -> AsyncIterator[bytes]:
        """流式下载文件，逐块产出字节。

        Args:
            file: 文件路径、URL 或引用 ID。
            file_id: 文件 ID。
            chunk_size: 分块大小（字节）。
            timeout: 单条数据包超时（秒）。
        """
        async for chunk in self._download_bytes(
                "download_file_stream", file=file, file_id=file_id,
                chunk_size=chunk_size, timeout=timeout):
            yield chunk

    async def download_file_image_stream(self, *, file: str = None,
                                         file_id: str = None,
                                         chunk_size: int = 65536,
                                         timeout: float = 60.0) -> AsyncIterator[bytes]:
        """流式下载图片，逐块产出字节。"""
        async for chunk in self._download_bytes(
                "download_file_image_stream", file=file, file_id=file_id,
                chunk_size=chunk_size, timeout=timeout):
            yield chunk

    async def download_file_record_stream(self, *, file: str = None,
                                          file_id: str = None,
                                          chunk_size: int = 65536,
                                          out_format: str = "mp3",
                                          timeout: float = 60.0) -> AsyncIterator[bytes]:
        """流式下载语音（可按 `out_format` 转码），逐块产出字节。"""
        async for chunk in self._download_bytes(
                "download_file_record_stream", file=file, file_id=file_id,
                chunk_size=chunk_size, timeout=timeout, out_format=out_format):
            yield chunk

    async def iter_file_stream(self, *, file: str = None, file_id: str = None,
                               chunk_size: int = 65536,
                               timeout: float = 60.0) -> AsyncIterator[dict]:
        """按事件流下载文件（`file_info` / `file_chunk` / `file_complete`）。

        Args:
            file: 文件路径、URL 或带 FileNapCat UUID 的引用。
            file_id: 文件 ID（与 `file` 二选一）。
            chunk_size: 分块大小（字节）。
            timeout: 单条数据包超时（秒）。

        Yields:
            原始事件字典；`file_chunk` 事件的 `data` 是 Base64。
        """
        params = self._download_params(file, file_id, chunk_size)
        async for event in self.api._stream_packets("download_file_stream",
                                                params, timeout):
            yield event

    async def upload_file_stream(self, file_path: str, *,
                                 name: str = None,
                                 chunk_size: int = 512 * 1024,
                                 retention: int = 300000,
                                 expected_sha256: str = None,
                                 on_progress=None) -> dict:
        """把本地文件分块流式上传到 NapCat，返回服务器上的文件信息。

        Args:
            file_path: 本地文件路径。
            name: 上传后的文件名（默认取文件名）。
            chunk_size: 每块字节数（默认 512KB）。
            retention: 服务器保留时间（毫秒，默认 5 分钟）。
            expected_sha256: 期望的 SHA256；提供时服务端会校验。
            on_progress: 进度回调 `(已发送字节, 总字节)`。

        Returns:
            服务端最终状态字典（含 `file_path` / `file_name` / `file_size` /
            `sha256` 等字段，具体以 NapCat 返回为准）。
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        file_size = path.stat().st_size
        file_name = name or path.name
        chunk_size = max(1, int(chunk_size or 512 * 1024))
        total_chunks = max(1, math.ceil(file_size / chunk_size))
        stream_id = uuid.uuid4().hex
        sent = 0
        base = {
            "stream_id": stream_id,
            "total_chunks": total_chunks,
            "file_size": file_size,
            "filename": file_name,
            "file_retention": int(retention),
        }
        if expected_sha256:
            base["expected_sha256"] = expected_sha256

        index = 0
        async with aiofiles.open(path, "rb") as fh:
            while index < total_chunks:
                data = await fh.read(chunk_size)
                params = dict(base)
                params["chunk_data"] = base64.b64encode(data).decode()
                params["chunk_index"] = index
                result = await self.api._call("upload_file_stream", params,
                                          throttle=False)
                sent += len(data)
                if on_progress:
                    on_progress(sent, file_size)
                index += 1
                if isinstance(result, dict) and result.get("status") == "file_complete":
                    return result

        params = dict(base)
        params["is_complete"] = True
        return await self.api._call("upload_file_stream", params, throttle=False)

    async def test_download_stream(self, error: bool = False) -> Any:
        """测试下载流（排查流式通道是否可用；`error=True` 时触发测试错误）。"""
        params = {"error": True} if error else None
        return await self.api._call("test_download_stream", params)

    async def download_file(self, *, url: str = None, base64: str = None,
                            name: str = "", headers: str = "{}") -> FileInfo:
        params = {"name": name, "headers": headers}
        if url: params["url"] = url
        if base64: params["base64"] = base64
        data = await self.api._call("download_file", params)
        return FileInfo(**data) if isinstance(data, dict) else data

    async def upload_private_file(self, user_id: str, file: str, name: str,
                                  upload_file: bool = True) -> Any:
        """上传文件到私聊（好友 / 群临时会话均可）。

        Args:
            user_id: 目标用户 QQ。
            file: 本地文件路径 / URL。
            name: 文件名。
            upload_file: 是否执行上传（默认 True）。
        """
        return await self.api._call("upload_private_file", {
            "user_id": user_id, "file": file, "name": name,
            "upload_file": upload_file,
        })

    async def get_private_file_url(self, file_id: str) -> Any:
        """获取私聊文件URL（`get_private_file_url`）。

        Args:
            file_id: 文件 ID。
        """
        params: dict = {"file_id": file_id}
        return await self.api._call("get_private_file_url", params)

    async def delete_group_file(self, group_id: str, file_id: str,
                                bus_id: int = None, busid: int = None) -> None:
        params = {"group_id": group_id, "file_id": file_id}
        if bus_id is not None: params["bus_id"] = bus_id
        if busid is not None: params["busid"] = busid
        await self.api._call("delete_group_file", params)

    async def get_group_root_files(self, group_id: str,
                                   file_count: int = None) -> Any:
        params = {"group_id": group_id}
        if file_count is not None: params["file_count"] = file_count
        return await self.api._call("get_group_root_files", params)

    async def get_group_files_by_folder(self, group_id: str,
                                        folder_id: str = None, folder: str = None,
                                        file_count: int = None) -> Any:
        params = {"group_id": group_id}
        if folder_id: params["folder_id"] = folder_id
        if folder: params["folder"] = folder
        if file_count is not None: params["file_count"] = file_count
        return await self.api._call("get_group_files_by_folder", params)

    async def get_group_file_url(self, group_id: str, file_id: str) -> Any:
        return await self.api._call("get_group_file_url", {"group_id": group_id, "file_id": file_id})

    async def get_group_file_system_info(self, group_id: str) -> Any:
        """获取群文件系统信息（`get_group_file_system_info`）。

        Args:
            group_id: 群号。
        """
        params: dict = {"group_id": group_id}
        return await self.api._call("get_group_file_system_info", params)

    async def create_group_file_folder(self, group_id: str, **kwargs: Any) -> Any:
        """创建群文件目录（`create_group_file_folder`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（folder_name, name），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self.api._call("create_group_file_folder", params)

    async def delete_group_folder(self, group_id: str, **kwargs: Any) -> Any:
        """删除群文件目录（`delete_group_folder`）。

        Args:
            group_id: 群号。
            **kwargs: 可选参数（folder, folder_id），值为 None 时忽略。
        """
        params: dict = {"group_id": group_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self.api._call("delete_group_folder", params)

    async def move_group_file(self, current_parent_directory: str, file_id: str, group_id: str, target_parent_directory: str) -> Any:
        """移动群文件（`move_group_file`）。

        Args:
            current_parent_directory: current_parent_directory。
            file_id: 文件 ID。
            group_id: 群号。
            target_parent_directory: target_parent_directory。
        """
        params: dict = {"current_parent_directory": current_parent_directory, "file_id": file_id, "group_id": group_id, "target_parent_directory": target_parent_directory}
        return await self.api._call("move_group_file", params)

    async def rename_group_file(self, current_parent_directory: str, file_id: str, group_id: str, new_name: str) -> Any:
        """重命名群文件（`rename_group_file`）。

        Args:
            current_parent_directory: current_parent_directory。
            file_id: 文件 ID。
            group_id: 群号。
            new_name: new_name。
        """
        params: dict = {"current_parent_directory": current_parent_directory, "file_id": file_id, "group_id": group_id, "new_name": new_name}
        return await self.api._call("rename_group_file", params)

    async def trans_group_file(self, file_id: str, group_id: str) -> Any:
        """传输群文件（`trans_group_file`）。

        Args:
            file_id: 文件 ID。
            group_id: 群号。
        """
        params: dict = {"file_id": file_id, "group_id": group_id}
        return await self.api._call("trans_group_file", params)

    async def clean_stream_temp_file(self) -> Any:
        """清理 NapCat 侧的流式传输临时文件（上传/下载残留）。"""
        return await self.api._call("clean_stream_temp_file")

    async def create_flash_task(self, files: list, **kwargs: Any) -> Any:
        """创建闪传任务（`create_flash_task`）。

        Args:
            files: files。
            **kwargs: 可选参数（name, thumb_path, validitySeconds），值为 None 时忽略。
        """
        params: dict = {"files": files}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self.api._call("create_flash_task", params)

    async def get_flash_file_list(self, fileset_id: str) -> Any:
        """获取闪传文件列表（`get_flash_file_list`）。

        Args:
            fileset_id: 文件集 ID。
        """
        params: dict = {"fileset_id": fileset_id}
        return await self.api._call("get_flash_file_list", params)

    async def get_flash_file_url(self, fileset_id: str, **kwargs: Any) -> Any:
        """获取闪传文件链接（`get_flash_file_url`）。

        Args:
            fileset_id: 文件集 ID。
            **kwargs: 可选参数（file_index, file_name），值为 None 时忽略。
        """
        params: dict = {"fileset_id": fileset_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self.api._call("get_flash_file_url", params)

    async def get_share_link(self, fileset_id: str) -> Any:
        """获取文件分享链接（`get_share_link`）。

        Args:
            fileset_id: 文件集 ID。
        """
        params: dict = {"fileset_id": fileset_id}
        return await self.api._call("get_share_link", params)

    async def get_fileset_info(self, fileset_id: str) -> Any:
        """获取文件集信息（`get_fileset_info`）。

        Args:
            fileset_id: 文件集 ID。
        """
        params: dict = {"fileset_id": fileset_id}
        return await self.api._call("get_fileset_info", params)

    async def get_fileset_id(self, share_code: str) -> Any:
        """获取文件集 ID（`get_fileset_id`）。

        Args:
            share_code: share_code。
        """
        params: dict = {"share_code": share_code}
        return await self.api._call("get_fileset_id", params)

    async def get_online_file_msg(self, user_id: str) -> Any:
        """获取在线文件消息（`get_online_file_msg`）。

        Args:
            user_id: 用户 QQ。
        """
        params: dict = {"user_id": user_id}
        return await self.api._call("get_online_file_msg", params)

    async def send_online_file(self, user_id: str, file_path: str,
                               file_name: str = None) -> Any:
        params = {"user_id": user_id, "file_path": file_path}
        if file_name: params["file_name"] = file_name
        return await self.api._call("send_online_file", params)

    async def send_online_folder(self, folder_path: str, user_id: str, **kwargs: Any) -> Any:
        """发送在线文件夹（`send_online_folder`）。

        Args:
            folder_path: folder_path。
            user_id: 用户 QQ。
            **kwargs: 可选参数（folder_name），值为 None 时忽略。
        """
        params: dict = {"folder_path": folder_path, "user_id": user_id}
        params.update({k: v for k, v in kwargs.items() if v is not None})
        return await self.api._call("send_online_folder", params)

    async def receive_online_file(self, element_id: str, msg_id: str, user_id: str) -> Any:
        """接收在线文件（`receive_online_file`）。

        Args:
            element_id: element_id。
            msg_id: msg_id。
            user_id: 用户 QQ。
        """
        params: dict = {"element_id": element_id, "msg_id": msg_id, "user_id": user_id}
        return await self.api._call("receive_online_file", params)

    async def refuse_online_file(self, element_id: str, msg_id: str, user_id: str) -> Any:
        """拒绝在线文件（`refuse_online_file`）。

        Args:
            element_id: element_id。
            msg_id: msg_id。
            user_id: 用户 QQ。
        """
        params: dict = {"element_id": element_id, "msg_id": msg_id, "user_id": user_id}
        return await self.api._call("refuse_online_file", params)

    async def cancel_online_file(self, msg_id: str, user_id: str) -> Any:
        """取消在线文件（`cancel_online_file`）。

        Args:
            msg_id: msg_id。
            user_id: 用户 QQ。
        """
        params: dict = {"msg_id": msg_id, "user_id": user_id}
        return await self.api._call("cancel_online_file", params)

    async def download_fileset(self, fileset_id: str) -> Any:
        """下载文件集（`download_fileset`）。

        Args:
            fileset_id: 文件集 ID。
        """
        params: dict = {"fileset_id": fileset_id}
        return await self.api._call("download_fileset", params)

    async def send_flash_msg(self, fileset_id: str, group_id: str = None,
                             user_id: str = None) -> Any:
        params = {"fileset_id": fileset_id}
        if group_id: params["group_id"] = group_id
        if user_id: params["user_id"] = user_id
        return await self.api._call("send_flash_msg", params)

    # ==================== 清理 / 校验 ====================

    async def clean_temp(self) -> None:
        """清理 NapCat 侧流式传输临时文件（`clean_stream_temp_file` 的别名）。"""
        await self.clean_stream_temp_file()

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