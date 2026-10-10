"""
二维码处理（纯标准库）。

QQ 登录二维码是 NapCat 生成的 **147×147 1-bit 灰度 PNG**（约 600 字节）。
直接发到微信会被压成小图、噪点明显，扫码成功率低；这里把它做整数倍
**最近邻放大**（默认 8×）再发，纯 Python 重编码 PNG，不依赖 Pillow。

只用标准库的原因和 ilink 客户端一致：机器人跑在只读根文件系统上，
装不了包。PNG 的解码/编码本身不复杂（1-bit 灰度 + zlib），够用即可。
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Optional

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class QrImageError(RuntimeError):
    pass


def _iter_chunks(data: bytes):
    if not data.startswith(PNG_SIGNATURE):
        raise QrImageError("不是 PNG 文件")
    pos = len(PNG_SIGNATURE)
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        yield ctype, body
        pos += 12 + length          # 4 长度 + 4 类型 + 数据 + 4 CRC


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, width: int, height: int, bpp: int, stride: int) -> list[bytes]:
    """逐行反 PNG 滤波，返回每行像素字节。"""
    rows: list[bytes] = []
    prev = bytearray(stride)
    pos = 0
    for _ in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if len(line) < stride:
            line.extend(b"\x00" * (stride - len(line)))
        if ftype == 1:                      # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:                    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:                    # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:                    # Paeth
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        elif ftype != 0:
            raise QrImageError(f"不支持的 PNG 滤波类型 {ftype}")
        rows.append(bytes(line))
        prev = line
    return rows


def decode_gray_png(path: str | Path) -> tuple[int, int, list[bytearray]]:
    """读出灰度 PNG 的像素（0=黑, 255=白）。支持 8-bit 灰度与 1-bit 灰度。"""
    data = Path(path).read_bytes()
    width = height = bitdepth = colortype = None
    idat = bytearray()
    palette: Optional[list[tuple[int, int, int]]] = None

    for ctype, body in _iter_chunks(data):
        if ctype == b"IHDR":
            width, height, bitdepth, colortype, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", body[:13])
            if interlace:
                raise QrImageError("不支持隔行（Adam7）PNG")
        elif ctype == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif ctype == b"IDAT":
            idat.extend(body)
        elif ctype == b"IEND":
            break

    if width is None:
        raise QrImageError("PNG 缺少 IHDR")
    raw = zlib.decompress(bytes(idat))

    # 统一成 1 字节/像素的灰度
    if colortype == 0:                              # 灰度
        if bitdepth == 8:
            bpp, stride = 1, width
            rows = _unfilter(raw, width, height, bpp, stride)
            return width, height, [bytearray(r[:width]) for r in rows]
        if bitdepth == 1:
            bpp, stride = 1, (width + 7) // 8
            rows = _unfilter(raw, width, height, bpp, stride)
            out = []
            for r in rows:
                line = bytearray(width)
                for x in range(width):
                    bit = (r[x >> 3] >> (7 - (x & 7))) & 1
                    line[x] = 255 if bit else 0     # 1 = 白, 0 = 黑
                out.append(line)
            return width, height, out
    elif colortype == 3 and palette:                # 调色板
        if bitdepth == 8:
            rows = _unfilter(raw, width, height, 1, width)
            out = []
            for r in rows:
                line = bytearray(width)
                for x in range(width):
                    pr, pg, pb = palette[r[x]] if r[x] < len(palette) else (255, 255, 255)
                    line[x] = (pr * 299 + pg * 587 + pb * 114) // 1000
                out.append(line)
            return width, height, out
        if bitdepth in (1, 2, 4):
            stride = (width * bitdepth + 7) // 8
            rows = _unfilter(raw, width, height, 1, stride)
            per_byte = 8 // bitdepth
            mask = (1 << bitdepth) - 1
            out = []
            for r in rows:
                line = bytearray(width)
                for x in range(width):
                    shift = 8 - bitdepth * ((x % per_byte) + 1)
                    idx = (r[x // per_byte] >> shift) & mask
                    pr, pg, pb = palette[idx] if idx < len(palette) else (255, 255, 255)
                    line[x] = (pr * 299 + pg * 587 + pb * 114) // 1000
                out.append(line)
            return width, height, out
    raise QrImageError(f"暂不支持的 PNG 格式（colortype={colortype}, bitdepth={bitdepth}）")


def _chunk(ctype: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(ctype + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", crc)


def encode_gray_png(width: int, height: int, rows: list[bytearray]) -> bytes:
    """把灰度像素编码成 8-bit 灰度 PNG。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = bytearray()
    for r in rows:
        raw.append(0)                   # 滤波类型 None
        raw.extend(bytes(r[:width]))
    return (PNG_SIGNATURE + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def upscale_image(src: str | Path, dst: str | Path, factor: int = 8,
                  white_border: int = 1) -> Path:
    """把二维码 PNG 整数倍放大后另存（最近邻，保持锐利便于扫码）。

    `white_border`：放大后额外的白边模块数（微信里图片被裁切时留点余量）。
    """
    factor = max(1, int(factor))
    width, height, rows = decode_gray_png(src)
    border = max(0, int(white_border)) * factor
    out_w = width * factor + border * 2
    out_h = height * factor + border * 2

    white = bytearray([255]) * out_w
    out: list[bytearray] = [bytearray(white) for _ in range(border)]
    for r in rows:
        line = bytearray(out_w)
        for x in range(width):
            value = r[x]
            base = border + x * factor
            for k in range(factor):
                line[base + k] = value
        # 左右白边
        for _ in range(factor):
            out.append(bytearray(line))
    out.extend(bytearray(white) for _ in range(border))

    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_bytes(encode_gray_png(out_w, out_h, out))
    return dst_path
