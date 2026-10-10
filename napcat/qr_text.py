"""
QR 码编码器（纯标准库）—— 生成扫码用的 ASCII / PNG 二维码。

iLink 登录返回的 `qrcode_img_content` 是一个 **H5 页面链接**（页面在客户端渲染二维码），
不是图片，所以没法直接转发给用户扫。这里自己把那个链接编码成二维码：

- 终端里打印 ASCII 二维码（扫屏幕）；
- 需要时也能落成 PNG（走 qr_image 的编码器）。

算法是标准 QR（ISO/IEC 18004）最小实现：8-bit 字节模式 + Reed-Solomon 纠错 +
掩码择优。**不是自己发明的实现** —— 逐行对照 `qrcode` 8.2（BSD，Lincoln Loop /
Kazuhiko Arase 的 QRCode.js）改写，只保留「文本 → 模块矩阵」需要的最小部分
（去掉了图片嵌入、SVG、DataMatrix 等）。纠错表与生成多项式表原样内嵌。

License: BSD-3-Clause，见本文件末尾的版权声明。
"""
from __future__ import annotations

import math
from bisect import bisect_left

# ---- 纠错等级 ----
ERROR_CORRECT_L = 1
ERROR_CORRECT_M = 0
ERROR_CORRECT_Q = 3
ERROR_CORRECT_H = 2

# ---- 编码模式 ----
MODE_NUMBER = 1 << 0
MODE_ALPHA_NUM = 1 << 1
MODE_8BIT_BYTE = 1 << 2

MODE_SIZE_SMALL = {MODE_NUMBER: 10, MODE_ALPHA_NUM: 9, MODE_8BIT_BYTE: 8}
MODE_SIZE_MEDIUM = {MODE_NUMBER: 12, MODE_ALPHA_NUM: 11, MODE_8BIT_BYTE: 16}
MODE_SIZE_LARGE = {MODE_NUMBER: 14, MODE_ALPHA_NUM: 13, MODE_8BIT_BYTE: 16}

ALPHA_NUM = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
NUMBER_LENGTH = {3: 10, 2: 7, 1: 4}

G15 = (1 << 10) | (1 << 8) | (1 << 5) | (1 << 4) | (1 << 2) | (1 << 1) | (1 << 0)
G18 = ((1 << 12) | (1 << 11) | (1 << 10) | (1 << 9) | (1 << 8)
       | (1 << 5) | (1 << 2) | (1 << 0))
G15_MASK = (1 << 14) | (1 << 12) | (1 << 10) | (1 << 4) | (1 << 1)

PAD0 = 0xEC
PAD1 = 0x11

PATTERN_POSITION_TABLE = [
    [],
    [6, 18],
    [6, 22],
    [6, 26],
    [6, 30],
    [6, 34],
    [6, 22, 38],
    [6, 24, 42],
    [6, 26, 46],
    [6, 28, 50],
    [6, 30, 54],
    [6, 32, 58],
    [6, 34, 62],
    [6, 26, 46, 66],
    [6, 26, 48, 70],
    [6, 26, 50, 74],
    [6, 30, 54, 78],
    [6, 30, 56, 82],
    [6, 30, 58, 86],
    [6, 34, 62, 90],
    [6, 28, 50, 72, 94],
    [6, 26, 50, 74, 98],
    [6, 30, 54, 78, 102],
    [6, 28, 54, 80, 106],
    [6, 32, 58, 84, 110],
    [6, 30, 58, 86, 114],
    [6, 34, 62, 90, 118],
    [6, 26, 50, 74, 98, 122],
    [6, 30, 54, 78, 102, 126],
    [6, 26, 52, 78, 104, 130],
    [6, 30, 56, 82, 108, 134],
    [6, 34, 60, 86, 112, 138],
    [6, 30, 58, 86, 114, 142],
    [6, 34, 62, 90, 118, 146],
    [6, 30, 54, 78, 102, 126, 150],
    [6, 24, 50, 76, 102, 128, 154],
    [6, 28, 54, 80, 106, 132, 158],
    [6, 32, 58, 84, 110, 136, 162],
    [6, 26, 54, 82, 110, 138, 166],
    [6, 30, 58, 86, 114, 142, 170],
]

RS_BLOCK_OFFSET = {
    ERROR_CORRECT_L: 0,
    ERROR_CORRECT_M: 1,
    ERROR_CORRECT_Q: 2,
    ERROR_CORRECT_H: 3,
}


RS_BLOCK_TABLE = (
    # L
    # M
    # Q
    # H
    # 1
    (1, 26, 19),
    (1, 26, 16),
    (1, 26, 13),
    (1, 26, 9),
    # 2
    (1, 44, 34),
    (1, 44, 28),
    (1, 44, 22),
    (1, 44, 16),
    # 3
    (1, 70, 55),
    (1, 70, 44),
    (2, 35, 17),
    (2, 35, 13),
    # 4
    (1, 100, 80),
    (2, 50, 32),
    (2, 50, 24),
    (4, 25, 9),
    # 5
    (1, 134, 108),
    (2, 67, 43),
    (2, 33, 15, 2, 34, 16),
    (2, 33, 11, 2, 34, 12),
    # 6
    (2, 86, 68),
    (4, 43, 27),
    (4, 43, 19),
    (4, 43, 15),
    # 7
    (2, 98, 78),
    (4, 49, 31),
    (2, 32, 14, 4, 33, 15),
    (4, 39, 13, 1, 40, 14),
    # 8
    (2, 121, 97),
    (2, 60, 38, 2, 61, 39),
    (4, 40, 18, 2, 41, 19),
    (4, 40, 14, 2, 41, 15),
    # 9
    (2, 146, 116),
    (3, 58, 36, 2, 59, 37),
    (4, 36, 16, 4, 37, 17),
    (4, 36, 12, 4, 37, 13),
    # 10
    (2, 86, 68, 2, 87, 69),
    (4, 69, 43, 1, 70, 44),
    (6, 43, 19, 2, 44, 20),
    (6, 43, 15, 2, 44, 16),
    # 11
    (4, 101, 81),
    (1, 80, 50, 4, 81, 51),
    (4, 50, 22, 4, 51, 23),
    (3, 36, 12, 8, 37, 13),
    # 12
    (2, 116, 92, 2, 117, 93),
    (6, 58, 36, 2, 59, 37),
    (4, 46, 20, 6, 47, 21),
    (7, 42, 14, 4, 43, 15),
    # 13
    (4, 133, 107),
    (8, 59, 37, 1, 60, 38),
    (8, 44, 20, 4, 45, 21),
    (12, 33, 11, 4, 34, 12),
    # 14
    (3, 145, 115, 1, 146, 116),
    (4, 64, 40, 5, 65, 41),
    (11, 36, 16, 5, 37, 17),
    (11, 36, 12, 5, 37, 13),
    # 15
    (5, 109, 87, 1, 110, 88),
    (5, 65, 41, 5, 66, 42),
    (5, 54, 24, 7, 55, 25),
    (11, 36, 12, 7, 37, 13),
    # 16
    (5, 122, 98, 1, 123, 99),
    (7, 73, 45, 3, 74, 46),
    (15, 43, 19, 2, 44, 20),
    (3, 45, 15, 13, 46, 16),
    # 17
    (1, 135, 107, 5, 136, 108),
    (10, 74, 46, 1, 75, 47),
    (1, 50, 22, 15, 51, 23),
    (2, 42, 14, 17, 43, 15),
    # 18
    (5, 150, 120, 1, 151, 121),
    (9, 69, 43, 4, 70, 44),
    (17, 50, 22, 1, 51, 23),
    (2, 42, 14, 19, 43, 15),
    # 19
    (3, 141, 113, 4, 142, 114),
    (3, 70, 44, 11, 71, 45),
    (17, 47, 21, 4, 48, 22),
    (9, 39, 13, 16, 40, 14),
    # 20
    (3, 135, 107, 5, 136, 108),
    (3, 67, 41, 13, 68, 42),
    (15, 54, 24, 5, 55, 25),
    (15, 43, 15, 10, 44, 16),
    # 21
    (4, 144, 116, 4, 145, 117),
    (17, 68, 42),
    (17, 50, 22, 6, 51, 23),
    (19, 46, 16, 6, 47, 17),
    # 22
    (2, 139, 111, 7, 140, 112),
    (17, 74, 46),
    (7, 54, 24, 16, 55, 25),
    (34, 37, 13),
    # 23
    (4, 151, 121, 5, 152, 122),
    (4, 75, 47, 14, 76, 48),
    (11, 54, 24, 14, 55, 25),
    (16, 45, 15, 14, 46, 16),
    # 24
    (6, 147, 117, 4, 148, 118),
    (6, 73, 45, 14, 74, 46),
    (11, 54, 24, 16, 55, 25),
    (30, 46, 16, 2, 47, 17),
    # 25
    (8, 132, 106, 4, 133, 107),
    (8, 75, 47, 13, 76, 48),
    (7, 54, 24, 22, 55, 25),
    (22, 45, 15, 13, 46, 16),
    # 26
    (10, 142, 114, 2, 143, 115),
    (19, 74, 46, 4, 75, 47),
    (28, 50, 22, 6, 51, 23),
    (33, 46, 16, 4, 47, 17),
    # 27
    (8, 152, 122, 4, 153, 123),
    (22, 73, 45, 3, 74, 46),
    (8, 53, 23, 26, 54, 24),
    (12, 45, 15, 28, 46, 16),
    # 28
    (3, 147, 117, 10, 148, 118),
    (3, 73, 45, 23, 74, 46),
    (4, 54, 24, 31, 55, 25),
    (11, 45, 15, 31, 46, 16),
    # 29
    (7, 146, 116, 7, 147, 117),
    (21, 73, 45, 7, 74, 46),
    (1, 53, 23, 37, 54, 24),
    (19, 45, 15, 26, 46, 16),
    # 30
    (5, 145, 115, 10, 146, 116),
    (19, 75, 47, 10, 76, 48),
    (15, 54, 24, 25, 55, 25),
    (23, 45, 15, 25, 46, 16),
    # 31
    (13, 145, 115, 3, 146, 116),
    (2, 74, 46, 29, 75, 47),
    (42, 54, 24, 1, 55, 25),
    (23, 45, 15, 28, 46, 16),
    # 32
    (17, 145, 115),
    (10, 74, 46, 23, 75, 47),
    (10, 54, 24, 35, 55, 25),
    (19, 45, 15, 35, 46, 16),
    # 33
    (17, 145, 115, 1, 146, 116),
    (14, 74, 46, 21, 75, 47),
    (29, 54, 24, 19, 55, 25),
    (11, 45, 15, 46, 46, 16),
    # 34
    (13, 145, 115, 6, 146, 116),
    (14, 74, 46, 23, 75, 47),
    (44, 54, 24, 7, 55, 25),
    (59, 46, 16, 1, 47, 17),
    # 35
    (12, 151, 121, 7, 152, 122),
    (12, 75, 47, 26, 76, 48),
    (39, 54, 24, 14, 55, 25),
    (22, 45, 15, 41, 46, 16),
    # 36
    (6, 151, 121, 14, 152, 122),
    (6, 75, 47, 34, 76, 48),
    (46, 54, 24, 10, 55, 25),
    (2, 45, 15, 64, 46, 16),
    # 37
    (17, 152, 122, 4, 153, 123),
    (29, 74, 46, 14, 75, 47),
    (49, 54, 24, 10, 55, 25),
    (24, 45, 15, 46, 46, 16),
    # 38
    (4, 152, 122, 18, 153, 123),
    (13, 74, 46, 32, 75, 47),
    (48, 54, 24, 14, 55, 25),
    (42, 45, 15, 32, 46, 16),
    # 39
    (20, 147, 117, 4, 148, 118),
    (40, 75, 47, 7, 76, 48),
    (43, 54, 24, 22, 55, 25),
    (10, 45, 15, 67, 46, 16),
    # 40
    (19, 148, 118, 6, 149, 119),
    (18, 75, 47, 31, 76, 48),
    (34, 54, 24, 34, 55, 25),
    (20, 45, 15, 61, 46, 16),
)

# ===========================================================================
# 以下两张表与 Reed-Solomon 算法来自 qrcode 8.2（BSD-3-Clause）：
#   Copyright (c) 2011, Lincoln Loop
#   Ported from the Javascript library by Sam Curren (QRCode.js,
#   Copyright (c) 2009 Kazuhiko Arase, MIT)
# 见文件末尾 LICENSE 声明。
# ===========================================================================


RS_POLY_LUT = {
    13: [1, 137, 73, 227, 17, 177, 17, 52, 13, 46, 43, 83, 132, 120],
    15: [1, 29, 196, 111, 163, 112, 74, 10, 105, 105, 139, 132, 151, 32, 134, 26],
    16: [1, 59, 13, 104, 189, 68, 209, 30, 8, 163, 65, 41, 229, 98, 50, 36, 59],
    17: [1, 119, 66, 83, 120, 119, 22, 197, 83, 249, 41, 143, 134, 85, 53, 125, 99, 79],
    18: [
        1,
        239,
        251,
        183,
        113,
        149,
        175,
        199,
        215,
        240,
        220,
        73,
        82,
        173,
        75,
        32,
        67,
        217,
        146,
    ],
    20: [
        1,
        152,
        185,
        240,
        5,
        111,
        99,
        6,
        220,
        112,
        150,
        69,
        36,
        187,
        22,
        228,
        198,
        121,
        121,
        165,
        174,
    ],
    22: [
        1,
        89,
        179,
        131,
        176,
        182,
        244,
        19,
        189,
        69,
        40,
        28,
        137,
        29,
        123,
        67,
        253,
        86,
        218,
        230,
        26,
        145,
        245,
    ],
    24: [
        1,
        122,
        118,
        169,
        70,
        178,
        237,
        216,
        102,
        115,
        150,
        229,
        73,
        130,
        72,
        61,
        43,
        206,
        1,
        237,
        247,
        127,
        217,
        144,
        117,
    ],
    26: [
        1,
        246,
        51,
        183,
        4,
        136,
        98,
        199,
        152,
        77,
        56,
        206,
        24,
        145,
        40,
        209,
        117,
        233,
        42,
        135,
        68,
        70,
        144,
        146,
        77,
        43,
        94,
    ],
    28: [
        1,
        252,
        9,
        28,
        13,
        18,
        251,
        208,
        150,
        103,
        174,
        100,
        41,
        167,
        12,
        247,
        56,
        117,
        119,
        233,
        127,
        181,
        100,
        121,
        147,
        176,
        74,
        58,
        197,
    ],
    30: [
        1,
        212,
        246,
        77,
        73,
        195,
        192,
        75,
        98,
        5,
        70,
        103,
        177,
        22,
        217,
        138,
        51,
        181,
        246,
        72,
        25,
        18,
        46,
        228,
        74,
        216,
        195,
        11,
        106,
        130,
        150,
    ],
}


# ===========================================================================
# GF(256) 运算
# ===========================================================================
EXP_TABLE = list(range(256))
LOG_TABLE = list(range(256))
for _i in range(8):
    EXP_TABLE[_i] = 1 << _i
for _i in range(8, 256):
    EXP_TABLE[_i] = (EXP_TABLE[_i - 4] ^ EXP_TABLE[_i - 5]
                     ^ EXP_TABLE[_i - 6] ^ EXP_TABLE[_i - 8])
for _i in range(255):
    LOG_TABLE[EXP_TABLE[_i]] = _i


def glog(n: int) -> int:
    if n < 1:
        raise ValueError(f"glog({n})")
    return LOG_TABLE[n]


def gexp(n: int) -> int:
    return EXP_TABLE[n % 255]


class Polynomial:
    """GF(256) 上的多项式（纠错码用）。"""

    def __init__(self, num, shift):
        if not num:
            raise ValueError("Polynomial 需要非空系数")
        offset = 0
        for offset in range(len(num)):
            if num[offset] != 0:
                break
        self.num = list(num[offset:]) + [0] * shift

    def __getitem__(self, index):
        return self.num[index]

    def __iter__(self):
        return iter(self.num)

    def __len__(self):
        return len(self.num)

    def __mul__(self, other):
        num = [0] * (len(self) + len(other) - 1)
        for i, item in enumerate(self):
            for j, other_item in enumerate(other):
                num[i + j] ^= gexp(glog(item) + glog(other_item))
        return Polynomial(num, 0)

    def __mod__(self, other):
        difference = len(self) - len(other)
        if difference < 0:
            return self
        ratio = glog(self[0]) - glog(other[0])
        num = [item ^ gexp(glog(other_item) + ratio)
               for item, other_item in zip(self, other)]
        if difference:
            num.extend(self[-difference:])
        return Polynomial(num, 0) % other


class RSBlock:
    __slots__ = ("total_count", "data_count")

    def __init__(self, total_count, data_count):
        self.total_count = total_count
        self.data_count = data_count


def rs_blocks(version: int, error_correction: int) -> list:
    if error_correction not in RS_BLOCK_OFFSET:
        raise ValueError(f"错误的纠错等级 {error_correction}")
    offset = RS_BLOCK_OFFSET[error_correction]
    rs_block = RS_BLOCK_TABLE[(version - 1) * 4 + offset]
    blocks = []
    for i in range(0, len(rs_block), 3):
        count, total_count, data_count = rs_block[i:i + 3]
        for _ in range(count):
            blocks.append(RSBlock(total_count, data_count))
    return blocks


_BIT_LIMIT_TABLE = [
    [0] + [8 * sum(b.data_count for b in rs_blocks(v, ec)) for v in range(1, 41)]
    for ec in range(4)
]


# ===========================================================================
# 位缓冲 / 数据编码
# ===========================================================================
class BitBuffer:
    def __init__(self):
        self.buffer: list[int] = []
        self.length = 0

    def __len__(self):
        return self.length

    def put(self, num: int, length: int):
        for i in range(length):
            self.put_bit(((num >> (length - i - 1)) & 1) == 1)

    def put_bit(self, bit: bool):
        idx = self.length // 8
        if len(self.buffer) <= idx:
            self.buffer.append(0)
        if bit:
            self.buffer[idx] |= 0x80 >> (self.length % 8)
        self.length += 1


def _to_bytestring(data) -> bytes:
    return data if isinstance(data, bytes) else str(data).encode("utf-8")


def optimal_mode(data: bytes) -> int:
    if data.isdigit():
        return MODE_NUMBER
    try:
        if all(bytes([c]) in ALPHA_NUM for c in data):
            return MODE_ALPHA_NUM
    except Exception:
        pass
    return MODE_8BIT_BYTE


class QRData:
    def __init__(self, data, mode=None):
        data = _to_bytestring(data)
        self.data = data
        self.mode = optimal_mode(data) if mode is None else mode

    def __len__(self):
        return len(self.data)

    def write(self, buffer: BitBuffer):
        if self.mode == MODE_NUMBER:
            for i in range(0, len(self.data), 3):
                chars = self.data[i:i + 3]
                buffer.put(int(chars), NUMBER_LENGTH[len(chars)])
        elif self.mode == MODE_ALPHA_NUM:
            for i in range(0, len(self.data), 2):
                chars = self.data[i:i + 2]
                if len(chars) > 1:
                    buffer.put(ALPHA_NUM.find(chars[0]) * 45 + ALPHA_NUM.find(chars[1]), 11)
                else:
                    buffer.put(ALPHA_NUM.find(chars), 6)
        else:
            for c in self.data:
                buffer.put(c, 8)


def _mode_sizes(version: int) -> dict:
    if version < 10:
        return MODE_SIZE_SMALL
    if version < 27:
        return MODE_SIZE_MEDIUM
    return MODE_SIZE_LARGE


def _length_in_bits(mode: int, version: int) -> int:
    return _mode_sizes(version)[mode]


def create_bytes(buffer: BitBuffer, blocks: list) -> list:
    offset = 0
    max_dc = max_ec = 0
    dcdata, ecdata = [], []
    for block in blocks:
        dc_count = block.data_count
        ec_count = block.total_count - dc_count
        max_dc = max(max_dc, dc_count)
        max_ec = max(max_ec, ec_count)
        current_dc = [0xFF & buffer.buffer[i + offset] for i in range(dc_count)]
        offset += dc_count

        if ec_count in RS_POLY_LUT:
            rs_poly = Polynomial(RS_POLY_LUT[ec_count], 0)
        else:
            rs_poly = Polynomial([1], 0)
            for i in range(ec_count):
                rs_poly = rs_poly * Polynomial([1, gexp(i)], 0)

        mod_poly = Polynomial(current_dc, len(rs_poly) - 1) % rs_poly
        mod_offset = len(mod_poly) - ec_count
        ecdata.append([mod_poly[i + mod_offset] if (i + mod_offset) >= 0 else 0
                       for i in range(ec_count)])
        dcdata.append(current_dc)

    data = []
    for i in range(max_dc):
        for dc in dcdata:
            if i < len(dc):
                data.append(dc[i])
    for i in range(max_ec):
        for ec in ecdata:
            if i < len(ec):
                data.append(ec[i])
    return data


def create_data(version: int, error_correction: int, data_list: list) -> list:
    buffer = BitBuffer()
    for data in data_list:
        buffer.put(data.mode, 4)
        buffer.put(len(data), _length_in_bits(data.mode, version))
        data.write(buffer)

    blocks = rs_blocks(version, error_correction)
    bit_limit = sum(b.data_count * 8 for b in blocks)
    if len(buffer) > bit_limit:
        raise ValueError(f"数据过长：{len(buffer)} bit > 可用 {bit_limit} bit")

    for _ in range(min(bit_limit - len(buffer), 4)):
        buffer.put_bit(False)
    delimit = len(buffer) % 8
    if delimit:
        for _ in range(8 - delimit):
            buffer.put_bit(False)
    for i in range((bit_limit - len(buffer)) // 8):
        buffer.put(PAD0 if i % 2 == 0 else PAD1, 8)
    return create_bytes(buffer, blocks)


# ===========================================================================
# BCH / 掩码 / 掩码评分
# ===========================================================================
def _bch_digit(data: int) -> int:
    digit = 0
    while data != 0:
        digit += 1
        data >>= 1
    return digit


def _bch_type_info(data: int) -> int:
    d = data << 10
    while _bch_digit(d) - _bch_digit(G15) >= 0:
        d ^= G15 << (_bch_digit(d) - _bch_digit(G15))
    return ((data << 10) | d) ^ G15_MASK


def _bch_type_number(data: int) -> int:
    d = data << 12
    while _bch_digit(d) - _bch_digit(G18) >= 0:
        d ^= G18 << (_bch_digit(d) - _bch_digit(G18))
    return (data << 12) | d


def mask_func(pattern: int):
    if pattern == 0:
        return lambda i, j: (i + j) % 2 == 0
    if pattern == 1:
        return lambda i, j: i % 2 == 0
    if pattern == 2:
        return lambda i, j: j % 3 == 0
    if pattern == 3:
        return lambda i, j: (i + j) % 3 == 0
    if pattern == 4:
        return lambda i, j: (math.floor(i / 2) + math.floor(j / 3)) % 2 == 0
    if pattern == 5:
        return lambda i, j: (i * j) % 2 + (i * j) % 3 == 0
    if pattern == 6:
        return lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0
    if pattern == 7:
        return lambda i, j: ((i * j) % 3 + (i + j) % 2) % 2 == 0
    raise ValueError(f"错误的掩码 {pattern}")


def _lost_point_level1(modules, count) -> int:
    lost = 0
    container = [0] * (count + 1)
    rng = range(count)
    for row in rng:
        this_row = modules[row]
        previous = this_row[0]
        length = 0
        for col in rng:
            if this_row[col] == previous:
                length += 1
            else:
                if length >= 5:
                    container[length] += 1
                length = 1
                previous = this_row[col]
        if length >= 5:
            container[length] += 1
    for col in rng:
        previous = modules[0][col]
        length = 0
        for row in rng:
            if modules[row][col] == previous:
                length += 1
            else:
                if length >= 5:
                    container[length] += 1
                length = 1
                previous = modules[row][col]
        if length >= 5:
            container[length] += 1
    lost += sum(container[n] * (n - 2) for n in range(5, count + 1))
    return lost


def _lost_point_level2(modules, count) -> int:
    lost = 0
    rng = range(count - 1)
    for row in rng:
        this_row = modules[row]
        next_row = modules[row + 1]
        it = iter(rng)
        for col in it:
            top_right = this_row[col + 1]
            if top_right != next_row[col + 1]:
                next(it, None)
            elif top_right != this_row[col]:
                continue
            elif top_right != next_row[col]:
                continue
            else:
                lost += 3
    return lost


def _lost_point_level3(modules, count) -> int:
    rng = range(count)
    short = range(count - 10)
    lost = 0
    for row in rng:
        this_row = modules[row]
        it = iter(short)
        for col in it:
            if (not this_row[col + 1] and this_row[col + 4]
                    and not this_row[col + 5] and this_row[col + 6]
                    and not this_row[col + 9]
                    and ((this_row[col + 0] and this_row[col + 2] and this_row[col + 3]
                          and not this_row[col + 7] and not this_row[col + 8]
                          and not this_row[col + 10])
                         or (not this_row[col + 0] and not this_row[col + 2]
                             and not this_row[col + 3] and this_row[col + 7]
                             and this_row[col + 8] and this_row[col + 10]))):
                lost += 40
            if this_row[col + 10]:
                next(it, None)
    for col in rng:
        it = iter(short)
        for row in it:
            if (not modules[row + 1][col] and modules[row + 4][col]
                    and not modules[row + 5][col] and modules[row + 6][col]
                    and not modules[row + 9][col]
                    and ((modules[row + 0][col] and modules[row + 2][col]
                          and modules[row + 3][col] and not modules[row + 7][col]
                          and not modules[row + 8][col] and not modules[row + 10][col])
                         or (not modules[row + 0][col] and not modules[row + 2][col]
                             and not modules[row + 3][col] and modules[row + 7][col]
                             and modules[row + 8][col] and modules[row + 10][col]))):
                lost += 40
            if modules[row + 10][col]:
                next(it, None)
    return lost


def lost_point(modules) -> int:
    count = len(modules)
    dark = sum(map(sum, modules))
    rating = int(abs(float(dark) / (count ** 2) * 100 - 50) / 5)
    return (_lost_point_level1(modules, count) + _lost_point_level2(modules, count)
            + _lost_point_level3(modules, count) + rating * 10)


# ===========================================================================
# QRCode
# ===========================================================================
class QRCode:
    """最小 QR 编码器：`QRCode(text, error_correction=...)` → 模块矩阵。"""

    def __init__(self, error_correction: int = ERROR_CORRECT_M, border: int = 4,
                 mask_pattern: int = None):
        self.error_correction = int(error_correction)
        self.border = max(0, int(border))
        self.mask_pattern = mask_pattern
        self.version: int = 0
        self.modules: list[list] = []
        self.modules_count = 0
        self.data_cache = None
        self.data_list: list[QRData] = []

    def add_data(self, data):
        self.data_list.append(data if isinstance(data, QRData) else QRData(data))
        self.data_cache = None

    # ---- 版本选择 ----
    def best_fit(self, start: int = 1) -> int:
        mode_sizes = _mode_sizes(start)
        buffer = BitBuffer()
        for data in self.data_list:
            buffer.put(data.mode, 4)
            buffer.put(len(data), mode_sizes[data.mode])
            data.write(buffer)
        needed = len(buffer)
        version = bisect_left(_BIT_LIMIT_TABLE[self.error_correction], needed, start)
        if version == 41:
            raise ValueError("数据过长，超出 QR 版本 40 的容量")
        self.version = version
        if mode_sizes is not _mode_sizes(self.version):
            return self.best_fit(start=self.version)
        return self.version

    # ---- 矩阵构建 ----
    def make(self, fit: bool = True):
        if fit or not self.version:
            self.best_fit(start=self.version or 1)
        mask = self.mask_pattern
        if mask is None:
            mask = self.best_mask_pattern()
        self.make_impl(False, mask)

    def best_mask_pattern(self) -> int:
        min_lost = 0
        pattern = 0
        for i in range(8):
            self.make_impl(True, i)
            lost = lost_point(self.modules)
            if i == 0 or min_lost > lost:
                min_lost = lost
                pattern = i
        return pattern

    def make_impl(self, test: bool, mask_pattern: int):
        self.modules_count = self.version * 4 + 17
        self.modules = [[None] * self.modules_count for _ in range(self.modules_count)]
        self._setup_position_probe_pattern(0, 0)
        self._setup_position_probe_pattern(self.modules_count - 7, 0)
        self._setup_position_probe_pattern(0, self.modules_count - 7)
        self._setup_position_adjust_pattern()
        self._setup_timing_pattern()
        self._setup_type_info(test, mask_pattern)
        if self.version >= 7:
            self._setup_type_number(test)
        if self.data_cache is None:
            self.data_cache = create_data(self.version, self.error_correction, self.data_list)
        self._map_data(self.data_cache, mask_pattern)

    def _setup_position_probe_pattern(self, row: int, col: int):
        for r in range(-1, 8):
            if row + r <= -1 or self.modules_count <= row + r:
                continue
            for c in range(-1, 8):
                if col + c <= -1 or self.modules_count <= col + c:
                    continue
                if ((0 <= r <= 6 and c in {0, 6}) or (0 <= c <= 6 and r in {0, 6})
                        or (2 <= r <= 4 and 2 <= c <= 4)):
                    self.modules[row + r][col + c] = True
                else:
                    self.modules[row + r][col + c] = False

    def _setup_timing_pattern(self):
        for r in range(8, self.modules_count - 8):
            if self.modules[r][6] is not None:
                continue
            self.modules[r][6] = r % 2 == 0
        for c in range(8, self.modules_count - 8):
            if self.modules[6][c] is not None:
                continue
            self.modules[6][c] = c % 2 == 0

    def _setup_position_adjust_pattern(self):
        pos = PATTERN_POSITION_TABLE[self.version - 1]
        for row in pos:
            for col in pos:
                if self.modules[row][col] is not None:
                    continue
                for r in range(-2, 3):
                    for c in range(-2, 3):
                        self.modules[row + r][col + c] = (
                            r == -2 or r == 2 or c == -2 or c == 2 or (r == 0 and c == 0))

    def _setup_type_number(self, test: bool):
        bits = _bch_type_number(self.version)
        for i in range(18):
            mod = not test and ((bits >> i) & 1) == 1
            self.modules[i // 3][i % 3 + self.modules_count - 8 - 3] = mod
        for i in range(18):
            mod = not test and ((bits >> i) & 1) == 1
            self.modules[i % 3 + self.modules_count - 8 - 3][i // 3] = mod

    def _setup_type_info(self, test: bool, mask_pattern: int):
        bits = _bch_type_info((self.error_correction << 3) | mask_pattern)
        for i in range(15):
            mod = not test and ((bits >> i) & 1) == 1
            if i < 6:
                self.modules[i][8] = mod
            elif i < 8:
                self.modules[i + 1][8] = mod
            else:
                self.modules[self.modules_count - 15 + i][8] = mod
        for i in range(15):
            mod = not test and ((bits >> i) & 1) == 1
            if i < 8:
                self.modules[8][self.modules_count - i - 1] = mod
            elif i < 9:
                self.modules[8][15 - i - 1 + 1] = mod
            else:
                self.modules[8][15 - i - 1] = mod
        self.modules[self.modules_count - 8][8] = not test

    def _map_data(self, data, mask_pattern: int):
        inc = -1
        row = self.modules_count - 1
        bit_index = 7
        byte_index = 0
        mask = mask_func(mask_pattern)
        data_len = len(data)
        for col in range(self.modules_count - 1, 0, -2):
            if col <= 6:
                col -= 1
            col_range = (col, col - 1)
            while True:
                for c in col_range:
                    if self.modules[row][c] is None:
                        dark = False
                        if byte_index < data_len:
                            dark = ((data[byte_index] >> bit_index) & 1) == 1
                        if mask(row, c):
                            dark = not dark
                        self.modules[row][c] = dark
                        bit_index -= 1
                        if bit_index == -1:
                            byte_index += 1
                            bit_index = 7
                row += inc
                if row < 0 or self.modules_count <= row:
                    row -= inc
                    inc = -inc
                    break

    # ---- 输出 ----
    def get_matrix(self) -> list[list[bool]]:
        """含静默边（border）的模块矩阵，True = 黑。"""
        if self.data_cache is None:
            self.make()
        if not self.border:
            return self.modules
        width = self.modules_count + self.border * 2
        code = [[False] * width for _ in range(self.border)]
        for module in self.modules:
            code.append([False] * self.border + list(module) + [False] * self.border)
        code += [[False] * width for _ in range(self.border)]
        return code

    def to_ascii(self, invert: bool = True) -> str:
        """两行一字节的 ASCII 二维码，可直接 print（终端扫屏幕）。"""
        matrix = self.get_matrix()
        count = len(matrix)
        # 默认 invert=True：终端是深色背景白字，"白"= 空格更省眼
        dark, light = ("  ", "██") if invert else ("██", "  ")
        lines = []
        for r in range(0, count, 2):
            top = matrix[r]
            bottom = matrix[r + 1] if r + 1 < count else [False] * count
            line = []
            for c in range(count):
                if top[c] and bottom[c]:
                    line.append(dark)
                elif top[c] or bottom[c]:
                    line.append("▀" if top[c] else "▄")
                else:
                    line.append(light)
            lines.append("".join(line))
        return "\n".join(lines)

    def to_gray_rows(self) -> list[bytearray]:
        """给 qr_image.encode_gray_png 用的灰度行（0=黑, 255=白）。"""
        return [bytearray(0 if m else 255 for m in row) for row in self.get_matrix()]


def make_qr(text: str, *, error_correction: int = ERROR_CORRECT_M,
            border: int = 4) -> QRCode:
    """把文本编码成 QRCode（自动选版本 + 自动选掩码）。"""
    qr = QRCode(error_correction=error_correction, border=border)
    qr.add_data(text)
    qr.make()
    return qr


def text_to_ascii(text: str, *, border: int = 2,
                  error_correction: int = ERROR_CORRECT_M) -> str:
    return make_qr(text, error_correction=error_correction, border=border).to_ascii()


def text_to_png(text: str, dst, *, scale: int = 8, border: int = 4,
                error_correction: int = ERROR_CORRECT_M):
    """把文本编码成 PNG 二维码（PNG 编码复用 qr_image，避免第二份实现）。"""
    import struct
    import zlib
    from pathlib import Path

    from .qr_image import encode_gray_png

    qr = make_qr(text, error_correction=error_correction, border=border)
    rows = qr.to_gray_rows()
    count = len(rows)
    scale = max(1, int(scale))
    out_w = count * scale
    out = []
    for r in rows:
        scaled = bytearray(out_w)
        for x in range(count):
            value = r[x]
            base = x * scale
            for k in range(scale):
                scaled[base + k] = value
        for _ in range(scale):
            out.append(bytearray(scaled))
    path = Path(dst)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_gray_png(out_w, out_w, out))
    return path
