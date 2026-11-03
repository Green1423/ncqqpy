"""取「用户上传的 zip 文件字节」—— 照 `oj_bank/codefile.py` 的做法。

三条来源归一成同一份 `UploadedFile`：

1. **消息里的 `file` 段**（私聊发文件、`/plugin upload` 同一条消息带的附件）；
2. **引用的消息**（`/plugin upload` 引用一条文件消息）；
3. **下一条消息**（`/plugin upload` 之后 3 分钟内发的文件 → 由 `main.py`
   的 pending 状态接住，仍然走这里的 `from_segment`）。

和 oj_bank 一样，每一条路都可能拿不到 URL（平台差异大），所以按
**框架文件 API → http(s) 直连 → 本机路径（白名单内）** 的顺序降级；
不管哪条路都先过**大小上限**再读。

与 oj_bank 的差异只有两点，都是为了「装包」这个场景：

- 不解码成文本（zip 是二进制），返回原始字节；
- 本机路径白名单默认沿用 NapCat 的文件缓存目录，可由
  `plugins.plugin_manager.upload.local_roots` 覆盖 —— **空列表 = 用默认值，
  不是「不限制」**（不限制等于本机任意文件读取，比功能本身危险）。

⚠️ 这里只负责「把字节拿到手」，不判断它是不是 zip：格式检查在 `zips.py`，
这样「下载失败」和「包不对」在回执里是两句话，用户知道该重传还是该重打包。
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import aiohttp

from .installer import InstallError

#: 允许直接读取的本机目录（NapCat/QQ 的文件缓存）。**不是**任意路径都读：
#: `get_file`/消息段里的 `url` 可能是用户可控的字符串，放开等于本机任意文件读取。
DEFAULT_LOCAL_ROOTS = (
    "~/.config/QQ",
    "~/.local/share/QQ",
    "/opt/ncqq",
    "/tmp",
    "/var/tmp",
)


class PackageFileError(InstallError):
    """上传文件不可用（太大 / 下不到 / 不是本机可读路径）。"""


@dataclass
class UploadedFile:
    """一份拿到的上传文件。"""

    #: 原始文件名（可能为空）
    name: str
    #: 字节数
    size: int
    #: 原始字节（zip）
    data: bytes

    def describe(self) -> str:
        return f"{self.name or '上传文件'}（{self.size} 字节）"


# --------------------------------------------------------------------------
# 消息段
# --------------------------------------------------------------------------
def file_segments(message: Any) -> list[dict]:
    """消息里的 `file` 段（兼容 list 与纯字符串两种 `message`）。"""
    if not isinstance(message, list):
        return []
    return [seg for seg in message
            if isinstance(seg, dict) and seg.get("type") == "file"]


def _seg_data(seg: dict) -> dict:
    return seg.get("data") if isinstance(seg.get("data"), dict) else {}


def _name_of(data: dict) -> str:
    """段/通知里的文件名（平台字段名不统一，挨个认）。"""
    for key in ("name", "file_name", "filename", "file"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            # 有些实现把完整路径塞在 `file` 里
            return os.path.basename(value.replace("\\", "/")) or value
    return ""


def _size_of(data: dict) -> int:
    for key in ("file_size", "size"):
        try:
            value = int(data.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def _id_of(data: dict) -> str:
    for key in ("file_id", "id", "file_uuid", "file"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def segment_name(segment: dict) -> str:
    return _name_of(_seg_data(segment))


def segment_size(segment: dict) -> int:
    return _size_of(_seg_data(segment))


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
def local_path_of(value: str) -> str:
    """`file://` URI 或普通路径 → 本机绝对路径（不校验存在性）。"""
    text = str(value or "").strip()
    if text.lower().startswith("file://"):
        parsed = urlparse(text)
        path = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            path = f"//{parsed.netloc}{path}"
        text = path
    return os.path.abspath(os.path.expanduser(text)) if text else ""


def within_roots(path: str, roots) -> bool:
    """`path` 是否在 `roots` 里（按 realpath 比，挡住软链接跳出去）。"""
    if not roots:
        return True
    real = os.path.realpath(path)
    for root in roots:
        base = os.path.realpath(os.path.expanduser(str(root)))
        try:
            if os.path.commonpath([real, base]) == base:
                return True
        except ValueError:            # 不同盘符 / 非法路径
            continue
    return False


async def download(url: str, *, limit: int, timeout: float = 60.0,
                   roots=None) -> bytes:
    """取一份文件：http(s) 走网络，**本机路径 / `file://` 直接读**。"""
    target = str(url or "").strip()
    if not target.lower().startswith(("http://", "https://")):
        return await _read_local(target, limit, roots=roots)
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(target) as resp:
                if resp.status != 200:
                    raise PackageFileError(f"下载文件失败：HTTP {resp.status}")
                declared = resp.content_length or 0
                if declared and declared > limit:
                    raise PackageFileError(
                        f"文件太大（{declared} 字节 > {limit} 字节）")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise PackageFileError(f"文件太大（超过 {limit} 字节）")
                    chunks.append(chunk)
                return b"".join(chunks)
    except (asyncio.TimeoutError, TimeoutError) as e:
        raise PackageFileError("下载文件超时") from e
    except (aiohttp.ClientError, OSError) as e:
        raise PackageFileError(f"下载文件失败：{type(e).__name__}") from e


async def _read_local(path: str, limit: int, roots=None) -> bytes:
    """读本机路径（机器人 & NapCat 同机时可用；只允许 `roots` 下的目录）。"""
    target = local_path_of(path)
    if not target:
        raise PackageFileError("拿不到文件内容（既没有下载地址，也不是本机文件）")
    if not within_roots(target, roots):
        raise PackageFileError(
            f"本机文件不在允许读取的目录里（{target}）；管理员可以在 config 的 "
            "plugins.plugin_manager.upload.local_roots 里加上这个目录")
    if not os.path.isfile(target):
        raise PackageFileError("拿不到文件内容（既没有下载地址，也不是本机文件）")
    try:
        size = os.path.getsize(target)
        if size > limit:
            raise PackageFileError(f"文件太大（{size} 字节 > {limit} 字节）")
        with open(target, "rb") as fh:
            return fh.read(limit + 1)
    except OSError as e:
        raise PackageFileError(f"读文件失败：{e}") from e


async def _fetch_bytes(file_manager, api, *, file=None, file_id=None,
                       limit: int, timeout: float = 60.0,
                       roots=None) -> bytes:
    """取文件字节：**优先走框架的文件 API**（`FileManager` / `ApiClient`）。

    平台的 `download_file_stream` 能直接读 NapCat 侧的文件（本机路径、URL、
    FileNapCat UUID 都认），比插件自己发 HTTP / 读磁盘可靠；两条 API 都不可用
    （旧版 NapCat / 单测）时才退回「http 直连 / 本机路径」。
    """
    def _bounded(chunks: list[bytes], chunk: bytes) -> bytes:
        chunks.append(chunk)
        if sum(len(x) for x in chunks) > limit:
            raise PackageFileError(f"文件太大（超过 {limit} 字节）")
        return chunk

    if file_manager is not None:
        try:
            chunks: list[bytes] = []
            async for chunk in file_manager.download_stream(
                    file or "", file_id=file_id, timeout=timeout):
                _bounded(chunks, chunk)
            if chunks:
                return b"".join(chunks)
        except PackageFileError:
            raise
        except Exception:             # noqa: BLE001 - 平台差异大，退下一档
            pass
    if api is not None:
        try:
            chunks = []
            async for chunk in api.download_file_stream(
                    file=file, file_id=file_id, timeout=timeout):
                _bounded(chunks, chunk)
            if chunks:
                return b"".join(chunks)
        except PackageFileError:
            raise
        except Exception:             # noqa: BLE001
            pass
    return await download(file, limit=limit, timeout=timeout, roots=roots)


def _info_url(info: Any) -> str:
    if isinstance(info, dict):
        return str(info.get("url") or "")
    return str(getattr(info, "url", "") or "")


def _info_path(info: Any) -> str:
    if isinstance(info, dict):
        return str(info.get("file") or "")
    return str(getattr(info, "file", "") or "")


def _info_name(info: Any) -> str:
    if isinstance(info, dict):
        return str(info.get("name") or "")
    return str(getattr(info, "name", "") or "")


async def from_segment(segment: dict, api: Any, *, limit: int,
                       timeout: float = 60.0, roots=None,
                       file_manager=None) -> UploadedFile:
    """消息 `file` 段 → UploadedFile（走框架文件 API，失败再退回直连/本机路径）。"""
    data = _seg_data(segment)
    name = _name_of(data)
    declared = _size_of(data)
    if declared and declared > limit:
        raise PackageFileError(
            f"文件太大（{declared} 字节 > {limit} 字节）")
    url = str(data.get("url") or "")
    file_id = _id_of(data)
    raw: Optional[bytes] = None
    last_error: Optional[PackageFileError] = None

    candidates: list[tuple[Optional[str], Optional[str]]] = []
    if url:
        candidates.append((url, None))
    if file_id:
        candidates.append((None, file_id))
    for file_arg, fid in candidates:
        try:
            raw = await _fetch_bytes(file_manager, api, file=file_arg, file_id=fid,
                                     limit=limit, timeout=timeout, roots=roots)
            break
        except PackageFileError as e:
            last_error = e

    if raw is None and file_id:
        # 退路：有些实现只有 `get_file` 里才有可用的 url / 落盘路径
        try:
            info = await api.get_file(file_id=file_id)
        except Exception as e:  # noqa: BLE001 - 平台差异大，统一转成人话
            info = None
            last_error = PackageFileError(f"读取文件信息失败：{type(e).__name__}")
        if info is not None:
            name = name or _info_name(info)
            source = _info_url(info) or _info_path(info)
            if source:
                try:
                    raw = await _fetch_bytes(file_manager, api, file=source,
                                             limit=limit, timeout=timeout,
                                             roots=roots)
                except PackageFileError as e:
                    last_error = e
    if raw is None:
        if last_error is not None:
            raise last_error
        raise PackageFileError("这条消息里没有可读取的文件")
    return UploadedFile(name=name, size=len(raw), data=raw)


__all__ = ["DEFAULT_LOCAL_ROOTS", "PackageFileError", "UploadedFile",
           "download", "file_segments", "from_segment", "local_path_of",
           "segment_name", "segment_size", "within_roots"]
