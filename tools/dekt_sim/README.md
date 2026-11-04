# 第二课堂离线模拟器

依据 [`plugins/bupt_news/afterclass/final_draft.md`](../../plugins/bupt_news/afterclass/final_draft.md)
（小程序行为分析报告）实现：**纯内存假后端 + 三种编码算法 + 场景脚本**。

它解决一个很实际的问题 —— 第二课堂的登录要过人机校验、token 会过期、接口还会
按账号返回不同活动。想做「同步链路」的回归，不能每次都靠真账号。这套东西让
`tests/test_dekt_sim.py` 和真实 `DektClient` 都能**完全离线**地跑一遍。

> ⚠️ **边界**：这是**行为模型**，不是协议复刻。报告给了算法形状（字段、时间基准
> `1630425600`、字节/字符串旋转、长度约束），没有给逐位布局；位宽是本仓库自定义的，
> 并用往返测试锁死自洽性。它能证明「客户端在这套行为下怎么走」，
> **不能**证明「真实服务端一定这么返回」。真实服务端规则仍需授权环境 + 真机复核。

## 用法

```bash
# 1) 跑全部场景（不联网、不起端口）
./venv-arm/bin/python -B -m tools.dekt_sim scenarios

# 2) 起一个本地假后端（只监听 127.0.0.1，随机端口）
./venv-arm/bin/python -B -m tools.dekt_sim serve

# 3) 自检（算法往返 / 43 路由 / 18 场景 / 真实 DektClient 对打）
./venv-arm/bin/python -B tests/test_dekt_sim.py
```

让真实插件打模拟器：

```json
"afterclass": {
  "enabled": true,
  "base": "http://127.0.0.1:<serve 输出的端口>"
}
```

token 用模拟器 `POST /api/v1/auth/sessions` 返回的那个 JWT（`username=2026000001`、
`password=dekt`），写进绑定自己的 token 文件即可 —— `DektClient` 只验签名格式与
`sub`，不看谁签的。

## 目录

| 文件 | 职责 |
|---|---|
| `codec.py` | 动态签到码（`enc_code`/`dec_code`）、BLE 广播包、核销码、base36 trace、假 JWT |
| `store.py` | 内存世界：用户、活动、报名、订阅、商品、订单、积分、签到会话、成绩单任务、资讯 |
| `backend.py` | **43 条路由**（清单与参数来自 `afterclass/api-endpoints.json`）+ 鉴权 + digest 乐观锁 |
| `server.py` | 标准库 `http.server` 挂到 127.0.0.1 |
| `scenarios.py` | 18 个场景，逐条对照报告的 §4 业务流程 |

## 与报告的对齐关系

| 报告条目 | 这里落成 |
|---|---|
| §4.1 启动/认证（419/420 验证码、微信 404） | `auth.*` 路由；404 分支有场景覆盖 |
| §4.2 活动列表与报名（受众/窗口/名额/封禁/digest） | `activity.list` + `participation` 的六道校验 |
| §4.3 用户二维码签到（trace、每 2s 轮询、Success） | `checkin.qr.user` + `checkin.status` 状态翻转 |
| §4.4 管理员扫码（`X-Token` 载荷） | `checkin.passive` 解 Base64(JSON) 后校验报名 |
| §4.5 BLE 签到（16 字节包、状态位） | `codec.parse_ble_packet` + `checkin.ble` |
| §4.6 通知/商城/成绩单 | `news.*` / `store.*` / `transcript.*`，含积分规则 |
| §5.1 digest 乐观锁 | 写操作带旧 digest → 409 |
| §7.1/7.2/7.3 三种编码 | `codec.py`，含多档位与 `act_id=-1` 往返 |
| §10 验证结论 | `tests/test_dekt_sim.py`（79 项） |

## 已知取舍

- **只支持单用户 + 少量种子数据**：报告里的 20 个页面是客户端行为，服务端只需要
  数据够用；要更多用户/活动，改 `store.build_world()` 即可。
- **不做验证码 419/420**：报告说清那是客户端弹 H5 的流程，模拟器只保留「密码错
  → 401」与「微信未绑定 → 404」两条真实会走到的分支。
- **JWT 用固定密钥**（`codec.JWT_SECRET`），只用于本地 —— 任何真实 token 都别往
  这个模拟器里塞。
