"""北邮「第二课堂」离线行为模拟器（依据 `afterclass/final_draft.md`）。

**它是什么**：一套纯内存的假后端 + 编码算法 + 场景脚本，用来在没有生产账号、
不碰真实服务器的前提下，把小程序的行为跑一遍（登录、活动、报名、签到、商城、
成绩单），并给本仓库的 `DektClient` 提供离线对打目标。

**它不是什么**：不是协议复刻。报告只给了算法形状（字段、时间基准、旋转、
长度约束），没有给逐位布局；本模拟器自行定义了位宽并用往返测试锁死自洽性。
所以它能证明"客户端在这套行为下怎么走"，**不能**证明"真实服务端一定这么返回"。
"""
from .codec import (BASE_EPOCH, BLE_DEVICE_NAME, CodeError, CheckinCode,
                    build_ble_packet, dec_code, dec_redeem_code, enc_code,
                    enc_redeem_code, new_trace, parse_ble_packet)
from .store import World, build_world
from .backend import Backend, Response
from .server import serve_in_thread

__all__ = [
    "BASE_EPOCH", "BLE_DEVICE_NAME", "CodeError", "CheckinCode",
    "build_ble_packet", "dec_code", "dec_redeem_code", "enc_code",
    "enc_redeem_code", "new_trace", "parse_ble_packet",
    "World", "build_world", "Backend", "Response", "serve_in_thread",
]
