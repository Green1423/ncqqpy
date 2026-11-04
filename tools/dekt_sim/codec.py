"""第二课堂小程序的三种编码算法（离线模拟用，**不连生产**）。

对应 `final_draft.md` §7：

* **动态签到二维码**（§7.1）：固定时间基准 `1630425600`，把签发时间、有效时长、
  用户 ID、活动 ID、操作类型、能力位、digest、trace 编码成二进制，再经
  字节旋转 + Base64URL + 字符串旋转；首字符记录旋转档位。
* **BLE 广播包**（§7.2）：16 字节 / 32 个十六进制字符。
* **商品核销码**（§7.3）：32 字符表（去易混淆字符）的自定义进制，左补齐 6 位。

⚠️ 这里是**行为模型**，不是协议复刻：报告只给出了算法形状（字段、基准、旋转、
长度约束），没有给出每一位的精确布局。所以本模块自定义了具体位宽并**用往返
测试锁死自洽性** —— 用它验证的是"客户端在这套行为下怎么走"，不能用来声称
"真实服务端一定这么编码"。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "BASE_EPOCH", "QR_TTL", "enc_code", "dec_code", "CodeError",
    "new_trace", "parse_trace",
    "build_ble_packet", "parse_ble_packet", "BLEError", "BLE_DEVICE_NAME",
    "enc_redeem_code", "dec_redeem_code", "REDEEM_ALPHABET",
    "sign_jwt", "verify_jwt", "JWT_SECRET",
]

#: 二维码时间基准：2021-09-01 00:00:00 UTC（小程序 `utils/code.js` 里的常量）
BASE_EPOCH = 1630425600

#: 用户签到码有效期（秒）。报告说"每 10 秒换一次载荷"，这里给 30 秒余量。
QR_TTL = 30

#: 能力位（报告 §6 / API.md「活动能力位 Demands」）
DEMAND_SIGNUP = 1
DEMAND_CHECKIN = 2
DEMAND_CHECKOUT = 4
DEMAND_CHANNEL = 8
DEMAND_MANAGE = 16
DEMAND_HIDDEN = 32
DEMAND_ROLE_FILTER = 64

#: 封禁类型（EBannedType）
BANNED_SIGNUP = 1
BANNED_CHECKIN = 2
BANNED_REDEEM = 4


class CodeError(ValueError):
    """编码/解码失败（长度不对、字符不合法、校验不通过）。"""


# ------------------------------------------------------------------ 动态签到码


def new_trace() -> str:
    """生成 base36 的 trace（用户码的唯一标识，客户端用它轮询是否被扫）。"""
    raw = struct.pack(">Q", int(time.time() * 1000) & 0xFFFFFFFFFFFF) + \
        hashlib.sha1(str(time.time_ns()).encode()).digest()[:3]
    value = int.from_bytes(raw, "big")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    while value:
        value, rem = divmod(value, 36)
        out.append(digits[rem])
    return ("".join(reversed(out)) or "0").rjust(7, "0")[:13]


def parse_trace(trace: str) -> dict:
    """trace 只做形状校验：base36、长度 4~16。"""
    trace = str(trace or "")
    if not 4 <= len(trace) <= 16 or any(c not in "0123456789abcdefghijklmnopqrstuvwxyz"
                                        for c in trace):
        raise CodeError(f"trace 不合法：{trace!r}")
    return {"trace": trace}


@dataclass
class CheckinCode:
    """签到码载荷（`dec_code()` 的解出结果，字段与客户端一致）。"""

    iat: int = 0            # 签发时间（Unix 秒）
    exp: int = 0            # 失效时间
    sub: int = 0            # 用户 id（JWT 的 sub）
    act_id: int = -1        # 活动 id；-1 表示用户码（还没绑定活动）
    opr_type: int = 0       # 0 签到 / 1 签退
    act_demand: int = 0     # 能力位
    digest: str = ""
    trace: str = ""

    def to_dict(self) -> dict:
        return {"iat": self.iat, "exp": self.exp, "sub": self.sub,
                "act_id": self.act_id, "opr_type": self.opr_type,
                "act_demand": self.act_demand, "digest": self.digest,
                "trace": self.trace}


#: 载荷的固定位宽（字节）。改这里必须同时改文档与测试。
_FIELDS = (("iat", 5), ("exp", 5), ("sub", 5), ("act_id", 3),
           ("opr_type", 1), ("act_demand", 1), ("digest4", 4), ("trace6", 6))
_PAYLOAD_LEN = sum(n for _name, n in _FIELDS)          # 30 字节


def _rot_left(data: bytes, n: int) -> bytes:
    if not data:
        return data
    n %= len(data)
    return data[n:] + data[:n]


def _rot_right(data: bytes, n: int) -> bytes:
    if not data:
        return data
    n %= len(data)
    return data[-n:] + data[:-n] if n else data


def _digest4(digest: str) -> bytes:
    return hashlib.sha1(str(digest).encode()).digest()[:4]


def enc_code(code: CheckinCode) -> str:
    """签到码对象 → 二维码字符串。

    布局：`iat|exp|sub|act_id|opr_type|act_demand|digest[0:4]|trace[0:6]`（30 字节）
    → 按第 `(act_id & 7)` 档做字节旋转 → Base64URL（去 padding）
    → 再按同一档做字符串旋转 → 首字符写档位（`0`~`7`）。
    """
    act = int(code.act_id)
    # act_id = -1（用户码）要能原样往返：用「+1 存 0」把 -1 映射到 0
    act_raw = act + 1
    if act_raw < 0 or act_raw >= 1 << 24:
        raise CodeError(f"act_id 超出范围：{act}")
    trace6 = (code.trace or "").encode()[:6].ljust(6, b"0")
    payload = b"".join([
        int(code.iat).to_bytes(5, "big"),
        int(code.exp).to_bytes(5, "big"),
        int(code.sub).to_bytes(5, "big"),
        act_raw.to_bytes(3, "big"),
        bytes([int(code.opr_type) & 0xFF]),
        bytes([int(code.act_demand) & 0xFF]),
        _digest4(code.digest),
        trace6,
    ])
    if len(payload) != _PAYLOAD_LEN:            # pragma: no cover - 防改坏
        raise CodeError("载荷长度不对")
    slot = act & 7
    body = _rot_left(payload, slot)
    text = base64.urlsafe_b64encode(body).decode().rstrip("=")
    text = text[slot:] + text[:slot]
    return f"{slot}{text}"


def dec_code(text: str) -> CheckinCode:
    """二维码字符串 → 签到码对象；任何不合法都抛 `CodeError`。"""
    text = str(text or "").strip()
    if len(text) < 2 or text[0] not in "01234567":
        raise CodeError("签到码首字符不是旋转档位")
    slot = int(text[0])
    body = text[1:]
    if slot:
        body = body[-slot:] + body[:-slot] if len(body) > slot else body
    pad = -len(body) % 4
    try:
        raw = base64.urlsafe_b64decode(body + "=" * pad)
    except Exception as e:                      # noqa: BLE001
        raise CodeError(f"签到码 Base64 解码失败：{e}") from e
    if len(raw) != _PAYLOAD_LEN:
        raise CodeError(f"签到码长度不对：{len(raw)} != {_PAYLOAD_LEN}")
    raw = _rot_right(raw, slot)

    pos = 0
    got: dict = {}
    for name, width in _FIELDS:
        chunk = raw[pos:pos + width]
        pos += width
        got[name] = chunk
    act_raw = int.from_bytes(got["act_id"], "big")
    code = CheckinCode(
        iat=int.from_bytes(got["iat"], "big"),
        exp=int.from_bytes(got["exp"], "big"),
        sub=int.from_bytes(got["sub"], "big"),
        act_id=act_raw - 1,
        opr_type=got["opr_type"][0],
        act_demand=got["act_demand"][0],
        digest="",                              # 只回传前 4 字节摘要，无法还原原串
        trace=got["trace6"].decode("utf-8", "replace").rstrip("0"),
    )
    if code.iat < BASE_EPOCH - 86400:           # 明显不是本协议的码
        raise CodeError("签到码的签发时间超出合理范围")
    return code


# ------------------------------------------------------------------ BLE 广播包

#: 设备名前缀（报告：只处理设备名为 `2NDC` 且状态位有效的广播）
BLE_DEVICE_NAME = "2NDC"
#: 广播包固定 16 字节 = 32 个十六进制字符
BLE_PACKET_BYTES = 16


class BLEError(ValueError):
    """BLE 包不合法（长度、状态位、保留位）。"""


def build_ble_packet(*, act_id: int, opr_type: int = 0, demands: int = DEMAND_CHECKIN,
                     totp: int = 0, ts: Optional[int] = None,
                     status: int = 1, dev: int = 0x2D) -> str:
    """构造一次广播包（32 个十六进制字符）。

    布局（16 字节）：`dev(1) status(1) opr(1) demands(1) act_id(3)
    totp(4) ts(5)`。`status` 必须非 0（报告：状态位有效的包才处理）。
    """
    ts = int(time.time()) if ts is None else int(ts)
    if not 0 <= int(act_id) < 1 << 24:
        raise BLEError(f"act_id 超出范围：{act_id}")
    return (
        bytes([dev & 0xFF, status & 0xFF, opr_type & 0xFF, demands & 0xFF])
        + int(act_id).to_bytes(3, "big")
        + int(totp).to_bytes(4, "big")
        + ts.to_bytes(5, "big")
    ).hex()


def parse_ble_packet(packet: str) -> dict:
    """解析广播包；长度/状态位/保留位不合法一律抛 `BLEError`。"""
    text = str(packet or "").strip().lower()
    if len(text) != BLE_PACKET_BYTES * 2:
        raise BLEError(f"广播包长度必须是 {BLE_PACKET_BYTES * 2} 个字符")
    try:
        raw = bytes.fromhex(text)
    except ValueError as e:
        raise BLEError(f"广播包不是十六进制：{e}") from e
    dev, status, opr, demands = raw[0], raw[1], raw[2], raw[3]
    if dev != 0x2D:                             # '2N' 的首字节，代表 2NDC
        raise BLEError("设备标识不是 2NDC")
    if status & 0x80:
        raise BLEError("状态位无效")            # 最高位为保留位，必须为 0
    if not status:
        raise BLEError("状态位为 0（设备未激活）")
    return {
        "dev": dev, "status": status, "opr_type": opr, "demands": demands,
        "act_id": int.from_bytes(raw[4:7], "big"),
        "totp": int.from_bytes(raw[7:11], "big"),
        "ts": int.from_bytes(raw[11:16], "big"),
    }


# ------------------------------------------------------------------ 核销码

#: 去掉了易混淆的 0/O/1/I/L 等字符，共 32 个（正好 5 bit 一位）
REDEEM_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZabcdef"


def enc_redeem_code(order_code: int) -> str:
    """订单号 → 6 位核销码（自定义 32 进制，左补 `2`）。"""
    n = int(order_code)
    if n < 0:
        raise CodeError("订单号不能为负")
    out = []
    base = len(REDEEM_ALPHABET)
    while n:
        n, rem = divmod(n, base)
        out.append(REDEEM_ALPHABET[rem])
    text = "".join(reversed(out)) or REDEEM_ALPHABET[0]
    return text.rjust(6, REDEEM_ALPHABET[0])


def dec_redeem_code(code: str) -> int:
    """6 位核销码 → 订单号（只接受 6 位、字符表内）。"""
    text = str(code or "")
    if len(text) != 6:
        raise CodeError("核销码必须是 6 位")
    value = 0
    for ch in text:
        idx = REDEEM_ALPHABET.find(ch)
        if idx < 0:
            raise CodeError(f"核销码含非法字符：{ch!r}")
        value = value * len(REDEEM_ALPHABET) + idx
    return value


# ------------------------------------------------------------------ 假 JWT

#: 离线模拟专用的固定密钥。**只在模拟器里用**，生产 token 由服务端签发。
JWT_SECRET = b"dekt-simulator-secret-not-for-production"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_jwt(claims: dict, *, secret: bytes = JWT_SECRET) -> str:
    """HS256 签发一份假 JWT（字段：sub/exp/banned_type/banned_before）。"""
    import json
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"},
                             separators=(",", ":")).encode())
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing = f"{header}.{payload}".encode()
    sig = _b64(hmac.new(secret, signing, hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def verify_jwt(token: str, *, secret: bytes = JWT_SECRET) -> dict:
    """校验并解出 claims；签名不符/过期都抛 `CodeError`。"""
    import json
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise CodeError("JWT 段数不对")
    header_b64, payload_b64, sig_b64 = parts
    expect = _b64(hmac.new(secret, f"{header_b64}.{payload_b64}".encode(),
                           hashlib.sha256).digest())
    if not hmac.compare_digest(expect, sig_b64):
        raise CodeError("JWT 签名不匹配")
    try:
        claims = json.loads(_unb64(payload_b64))
    except Exception as e:                      # noqa: BLE001
        raise CodeError(f"JWT 载荷不是 JSON：{e}") from e
    if float(claims.get("exp") or 0) <= time.time():
        raise CodeError("JWT 已过期")
    return claims
