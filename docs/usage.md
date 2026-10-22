# ncqqpy 使用文档

> 版本 1.0.0 | NapCatQQ Python 中间件 SDK — 完整使用指南
>
> 文档索引：[docs/README.md](README.md)

---

## 目录

1. [快速开始](#一快速开始)
    - [1.1 安装](#11-安装)
    - [1.2 前提条件](#12-前提条件)
    - [1.3 最小运行示例](#13-最小运行示例)
2. [客户端配置](#二客户端配置)
    - [2.1 NapCatClient 构造参数](#21-napcatclient-构造参数)
    - [2.2 客户端属性速览](#22-客户端属性速览)
    - [2.2.1 `config.json` 顶层：错误反馈策略](#221-configjson-顶层错误反馈策略)
    - [2.3 启动与退出](#23-启动与退出)
    - [2.4 断线重连与上报节流](#24-断线重连与上报节流)
3. [消息构造系统](#三消息构造系统)
    - [3.1 基础文本与 @](#31-基础文本与-)
    - [3.2 多媒体文件](#32-多媒体文件)
    - [3.3 音乐](#33-音乐)
    - [3.4 互动类](#34-互动类)
    - [3.5 富文本](#35-富文本)
    - [3.6 合并转发](#36-合并转发)
    - [3.7 输出格式](#37-输出格式)
4. [事件系统](#四事件系统)
    - [4.1 事件类型总览](#41-事件类型总览)
    - [4.2 注册事件处理器](#42-注册事件处理器)
    - [4.3 MessageEvent 常用属性](#43-messageevent-常用属性)
    - [4.4 事件中间件](#44-事件中间件)
5. [API 调用](#五api-调用)
    - [5.1 消息发送](#51-消息发送)
    - [5.2 群组管理](#52-群组管理)
    - [5.3 好友与用户](#53-好友与用户)
    - [5.4 文件操作](#54-文件操作)
    - [5.5 系统与扩展](#55-系统与扩展)
    - [5.6 错误处理](#56-错误处理)
6. [命令系统](#六命令系统)
    - [6.1 基本用法](#61-基本用法)
    - [6.1.1 `/help` 三级入口（帮助文档的构成）](#611-help-三级入口帮助文档的构成)
    - [6.1.2 帮助重定向：裸调命令组 / 用法不正确](#612-帮助重定向裸调命令组--用法不正确)
    - [6.2 命令解析规则](#62-命令解析规则)
    - [6.2.1 群聊里必须 @机器人](#621-群聊里必须-机器人)
    - [6.2.2 群聊临时会话（小窗）](#622-群聊临时会话小窗)
    - [6.2.3 `/help` 只列「你能用的」命令](#623-help-只列你能用的命令)
    - [6.2.4 引号与转义（字符串参数）](#624-引号与转义字符串参数)
    - [6.2.5 占位符记号：`<>` 必填、`[]` 可选](#625-占位符记号-必填-可选)
    - [6.3 CommandPacket 字段](#63-commandpacket-字段)
    - [6.4 命令返回值处理](#64-命令返回值处理)
    - [6.5 子命令](#65-子命令)
    - [6.6 非法输入与权限拒绝回调](#66-非法输入与权限拒绝回调)
    - [6.7 手动解析命令](#67-手动解析命令)
    - [6.8 命令别名（`/alias`）](#68-命令别名alias)
7. [权限管理](#七权限管理)
    - [7.1 角色层级](#71-角色层级)
    - [7.2 命令权限绑定](#72-命令权限绑定)
    - [7.3 手动权限检查](#73-手动权限检查)
    - [7.4 自定义权限节点](#74-自定义权限节点)
    - [7.5 用命令改权限（`/perm`）](#75-用命令改权限perm)
    - [7.6 用户级权限（`/userperm`）](#76-用户级权限userperm)
8. [插件开发](#八插件开发)
    - [8.1 插件基类](#81-插件基类)
    - [8.2 插件内置便捷方法](#82-插件内置便捷方法)
    - [8.3 插件文件约定](#83-插件文件约定)
    - [8.4 插件管理 API](#84-插件管理-api)
    - [8.5 运行时热重载](#85-运行时热重载)
    - [8.6 完整生命周期](#86-完整生命周期)
    - [8.6.1 首次运行即初始化全部配置项](#861-首次运行即初始化全部配置项)
    - [8.7 完整插件示例](#87-完整插件示例)
    - [8.8 用命令装卸插件（`plugin_manager`）](#88-用命令装卸插件plugin_manager)
9. [文件管理](#九文件管理)
    - [9.1 基础操作](#91-基础操作)
    - [9.2 流式下载](#92-流式下载)
    - [9.3 文件 Hash](#93-文件-hash)
10. [定时事件](#十定时事件)
    - [10.1 周期任务](#101-周期任务)
    - [10.2 每日定时](#102-每日定时)
    - [10.3 延迟执行](#103-延迟执行)
    - [10.4 定时器管理](#104-定时器管理)
11. [高级用法](#十一高级用法)
    - [11.1 EventBus 直接使用](#111-eventbus-直接使用)
    - [11.2 低频 API 直接调用](#112-低频-api-直接调用)
    - [11.3 自定义日志](#113-自定义日志)
    - [11.4 事件中发送 API](#114-事件中发送-api)
12. [API 参考速查表](#十二api-参考速查表)
    - [12.1 ApiClient 方法清单](#121-apiclient-方法清单)

---

## 一、快速开始

### 1.1 安装

```bash
pip install ncqqpy
# 或从源码
git clone https://github.com/Green1423/ncqqpy && cd ncqqpy
pip install -e .
```

**依赖**: `websockets>=13.0` `pydantic>=2.0` `aiohttp>=3.9` `aiofiles>=24.0`

### 1.2 前提条件

确保 NapCatQQ 的 WebSocket 服务端已启动（默认 `ws://127.0.0.1:3001`）。

### 1.3 最小运行示例

```python
# main.py
from napcat.client import NapCatClient
from napcat.message import MessageChain
client = NapCatClient(
    ws_url="ws://127.0.0.1:3001",
    token="your_token",
    superusers=[123456789],
)

@client.on_message()
async def handle(event):
    if event.plain_text == "ping":
        await client.api.send_group_msg(
            group_id=str(event.group_id),
            message=MessageChain().text("pong!"),
        )

client.run_blocking()  # 等同于 asyncio.run(client.run())
```

```bash
python main.py
```

---

## 二、客户端配置

### 2.1 NapCatClient 构造参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `ws_url` | `str` | `"ws://127.0.0.1:3001"` | NapCat WebSocket 地址 |
| `token` | `str` | `""` | 鉴权 Token，留空不鉴权 |
| `plugin_dir` | `str` | `"./plugins"` | 插件目录路径 |
| `superusers` | `list[int]` | `None` | 超级用户 QQ 号列表 |
| `command_prefixes` | `list[str]` | `None` | 命令前缀，默认 `["/", "!", "。", "#"]` |
| `max_concurrency` | `int` | `100` | 事件处理器最大并发数 |
| `reconnect_interval` | `float` | `5.0` | 断线后首次重连等待（秒）；连续失败按 2 倍指数退避、封顶 60s |
| `api_delay` | `float \| list[float]` | `[0.5, 1.0]` | **所有 API 上报**发送前的延迟：数字=固定值，`[min, max]`=随机区间（秒）。`0` 关闭 |

> `api_delay` / `reconnect_interval` 也可以写在 [config.json](../config.json) 里（构造参数优先）。
> `api_delay` 同时起**串行**作用：并发发起的 API 调用会排队，两次上报间隔不小于区间下限。

### 2.2 客户端属性速览

| 属性 | 类型 | 说明 |
|------|------|------|
| `client.api` | `ApiClient` | API 调用客户端（100+ 方法） |
| `client.event_bus` | `EventBus` | 事件总线（事件注册与分发） |
| `client.timer` | `TimerManager` | 定时管理器 |
| `client.perm` | `PermissionManager` | 权限管理器 |
| `client.cmds` | `CommandManager` | 命令管理器 |
| `client.files` | `FileManager` | 文件管理器 |
| `client.plugins` | `PluginManager` | 插件管理器 |
| `client.error_reporter` | `ErrorReporter` | 错误反馈器（文案 / 编号 / 受众 / 节流；见 [§8.6](#86-完整生命周期)）|
| `client.self_id` | `int` | 机器人 QQ 号（启动后可用） |
| `client.logger` | `Logger` | Python 标准 Logger |

### 2.2.1 `config.json` 顶层：错误反馈策略

```json
"error_report": {
  "enabled": true,          // false = 出错只写日志，不回任何文案
  "throttle_seconds": 60,   // 同一错误编号的最小重复间隔（窗口内只回短提示）
  "show_reason": true,      // false = 只给编号，不附短原因
  "notify_superusers": true // 加载/启用失败、定时任务失败是否私聊超管
}
```

运行期失败（命令、事件处理器）会回给触发者 —— 群聊里 **@ 他**；
生命周期失败只私聊超管。插件可抛的错误类型见 [plugin-dev.md §十六](plugin-dev.md#十六错误处理与用户反馈)。

### 2.3 启动与退出

```python
# 方式一：async 上下文
async def main():
    client = NapCatClient(ws_url="ws://127.0.0.1:3001")
    try:
        await client.start()
    finally:
        await client.stop()

# 方式二：带 Ctrl+C 处理
await client.run()

# 方式三：同步入口（最简）
client.run_blocking()
```

### 2.4 断线重连与上报节流

**WebSocket 自动重连**（`WsHandler.run_forever()`）：连接由后台任务托管，
下列情况一律自动重连，不需要业务代码处理：

| 情况 | 行为 |
|------|------|
| 启动时 NapCat 还没起来（连接被拒 / 超时 / TLS / 鉴权失败…）| 后台重试，**不会**让机器人启动失败 |
| 连接被对端关闭（NapCat 重启、网络抖动）| 记录日志 → 等待退避 → 重连 → 刷新登录信息 |
| 接收/发送过程抛任何异常 | 一律当作断线处理（仅「脏 JSON」单条丢弃，不断线）|
| 重连成功 | 重新拉取 `get_login_info` 刷新 `client.self_id`（群聊 @bot 判定依赖它）|

- 退避：连续失败按 `reconnect_interval × 2^(n-1)` 递增，封顶 60s；连上后计数清零。
- 主动退出：`client.stop()` 会先停重连循环，再关闭连接（不会在退出时又连上）。

**掉线期间的 API 调用会保留**（`WsHandler._wait_ready()`）：

| 情况 | 行为 |
|------|------|
| 服务器没上线时调用 API | **不失败、排队等待**，服务器一上线按序补发（日志：`API 调用开始排队…` / `开始补发 N 个排队中的 API 调用`）|
| 等待上限 | `offline_hold_timeout` 秒，默认 `0` = **一直等**；设正数则超时抛 `ConnectionError` |
| 排队上限 | `offline_queue_max`，默认 `1000`；超出后新调用立即 `ConnectionError`（防止无限堆积）|
| 进程退出 | 排队中的调用被取消（抛 `ConnectionError`），不会永久挂住 |
| **已发出但没等到响应**的调用 | 仍然立即失败 —— 不自动重发，避免把消息重复发一遍 |

```python
client = NapCatClient(offline_hold_timeout=0)      # 默认：一直等服务器回来
client = NapCatClient(offline_hold_timeout=300)    # 最多等 5 分钟，超时算失败
```

> 副作用：掉线期间插件里的推送/发送会**挂住**而不是报错，等重连后补发
> （例如 [bupt_news](../plugins/bupt_news/) 的一轮同步会一直等到重连才结束，期间后续轮次自动跳过）。

**所有 API 上报节流**：`ApiClient` 的每个调用发送前都会等待 `api_delay`
（默认 0.5–1.0s 随机值）并串行化，用于规避 QQ/NapCat 的风控阈值：

```python
client = NapCatClient(api_delay=(0.5, 1.0))   # 默认；区间随机
client = NapCatClient(api_delay=1.0)          # 固定 1s
client = NapCatClient(api_delay=0)            # 关闭节流（压测/联调）
```

> 影响：命令回复、插件推送都会多等 0.5–1s；批量推送 N 条消息的总耗时约 `N × 0.75s`。
> 代价是慢一点，收益是长时间运行不容易被限流。

---

## 三、消息构造系统

使用 `MessageChain` 以**链式 API** 构造所有 24 种 OneBot 消息段。

### 3.1 基础文本与 @

```python
from napcat.message import MessageChain
# 纯文本
MessageChain().text("Hello World!")

# @某人
MessageChain().at("123456789")          # @指定QQ
MessageChain().at("all")                # @全体成员

# 回复消息
MessageChain().reply(seq=123456).text("收到")

# QQ 表情
MessageChain().face("123")
```

### 3.2 多媒体文件

```python
# 图片（支持本地路径、URL、base64）
MessageChain().image("https://example.com/photo.jpg")
MessageChain().image("file:///C:/photo.png", name="photo.png")

# 语音
MessageChain().record("https://example.com/voice.amr")

# 视频
MessageChain().video("https://example.com/video.mp4")

# 通用文件
MessageChain().file("https://example.com/doc.pdf", name="doc.pdf")
```

### 3.3 音乐

```python
from napcat.message import MusicPlatform
# 平台 ID 音乐
MessageChain().music_id(MusicPlatform.QQ, "12345")

# 自定义音乐
MessageChain().music_custom(
    url="https://music.example.com",
    image="https://music.example.com/cover.jpg",
    title="歌曲标题",
    content="简介",
)
```

### 3.4 互动类

```python
# 戳一戳
MessageChain().poke()

# 骰子
MessageChain().dice()       # 随机1-6
MessageChain().dice(6)      # 指定结果

# 猜拳
MessageChain().rps("石头")
```

### 3.5 富文本

```python
# Markdown
MessageChain().markdown("# 标题\n这是 **Markdown**")

# JSON 卡片
MessageChain().json({"app": "com.tencent.structmsg"})

# XML 卡片
MessageChain().xml("<msg>...</msg>")

# 小程序
MessageChain().miniapp({"app": "..."})

# 位置分享
MessageChain().location(39.9042, 116.4074, title="北京", content="朝阳区")

# 名片分享
from napcat.message import ContactType
MessageChain().contact(ContactType.QQ, "123456789")
```

### 3.6 合并转发

```python
# 构造转发节点
node1 = MessageChain().node(
    user_id=987654321,
    nickname="小明",
    content=MessageChain().text("这是转发的消息"),
)

# 转发消息
MessageChain().forward(forward_id="res_id")

# 在线文件 / 闪传
MessageChain().online_file(msg_id="xxx", element_id="yyy", file_name="doc.pdf", file_size="1024")
MessageChain().flash_transfer(file_set_id="fileset_xxx")
```

### 3.7 输出格式

```python
chain = MessageChain().text("你好").at("123456").text(" 欢迎!")

# 构建为消息段列表（传给 API）
segments = chain.build()
# [{"type":"text","data":{"text":"你好"}},
#  {"type":"at","data":{"qq":"123456"}},
#  {"type":"text","data":{"text":" 欢迎!"}}]

# 转为 CQ 码字符串
cq = chain.to_cq()
# "你好[CQ:at,qq=123456] 欢迎!"

# 从 CQ 码解析
chain = MessageChain.from_cq("你好[CQ:at,qq=123456] 欢迎!")

# 消息段数量
len(chain)  # 3

# 合并两个消息链
combined = chain_a + chain_b
```

---

## 四、事件系统

### 4.1 事件类型总览

所有事件继承自 `NapCatEvent`，通过 `post_type` 自动分发为具体类型：

| 事件类 | post_type | 说明 |
|--------|-----------|------|
| `HeartbeatEvent` | `meta_event` | 心跳 |
| `LifeCycleEvent` | `meta_event` | 生命周期 |
| `MessageEvent` | `message` | 接收消息 |
| `GroupIncreaseEvent` | `notice` | 群成员增加 |
| `GroupDecreaseEvent` | `notice` | 群成员减少 |
| `GroupBanEvent` | `notice` | 群禁言 |
| `GroupAdminEvent` | `notice` | 管理员变动 |
| `GroupRecallEvent` | `notice` | 群消息撤回 |
| `GroupUploadEvent` | `notice` | 群文件上传 |
| `GroupCardEvent` | `notice` | 群名片修改 |
| `GroupNameEvent` | `notice` | 群名修改 |
| `GroupTitleEvent` | `notice` | 群头衔变更 |
| `PokeEvent` | `notice` | 戳一戳 |
| `InputStatusEvent` | `notice` | 输入状态 |
| `ProfileLikeEvent` | `notice` | 资料卡点赞 |
| `GroupGrayTipEvent` | `notice` | 群灰条消息 |
| `FriendAddEvent` | `notice` | 好友添加 |
| `FriendRecallEvent` | `notice` | 好友消息撤回 |
| `EssenceEvent` | `notice` | 精华消息变更 |
| `MsgEmojiLikeEvent` | `notice` | 消息表情点赞 |
| `BotOfflineEvent` | `notice` | Bot 掉线 |
| `OnlineFileSendEvent` | `notice` | 在线文件发送 |
| `OnlineFileReceiveEvent` | `notice` | 在线文件接收 |
| `FriendRequestEvent` | `request` | 好友请求 |
| `GroupRequestEvent` | `request` | 群请求 |

### 4.2 注册事件处理器

```python
from napcat.client import NapCatClient
from napcat.models import (
    MessageEvent,
    GroupIncreaseEvent,
    PokeEvent,
    FriendRequestEvent,
)
client = NapCatClient(...)

# ---- 方式一：client.on_message() 快捷装饰器 ----

@client.on_message()                      # 所有消息
async def on_all(event: MessageEvent):
    print(f"[{event.group_id}] {event.sender.nickname}: {event.plain_text}")

@client.on_message(group_id=123456)       # 仅指定群
async def on_group(event: MessageEvent):
    ...

@client.on_message(private=True)          # 仅私聊
async def on_private(event: MessageEvent):
    ...

# ---- 方式二：client.on() 指定事件类型 ----

@client.on(GroupIncreaseEvent)
async def on_join(event: GroupIncreaseEvent):
    await client.api.send_group_msg(
        group_id=str(event.group_id),
        message=MessageChain().at(event.user_id).text(" 欢迎!"),
    )

@client.on(PokeEvent)
async def on_poke(event: PokeEvent):
    if event.group_id:
        await client.api.send_poke(
            user_id=str(event.user_id),
            group_id=str(event.group_id),
        )

@client.on(FriendRequestEvent)
async def on_friend_req(event: FriendRequestEvent):
    await client.api.set_friend_add_request(
        flag=event.flag, approve=True,
    )

# ---- 方式三：client.on_any() 通配 ----

@client.on_any()
async def on_everything(event):
    print(f"[事件] {event.post_type}")
```

### 4.3 MessageEvent 常用属性

```python
event.message_id      # int     消息ID
event.message_seq     # int     消息序列号
event.user_id         # int     发送者QQ号
event.group_id        # int?    群号（群消息时）
event.group_name      # str?    群名
event.message_type    # str     "private"|"group"
event.sender          # Sender  发送者（.nickname, .card, .role）
event.message         # list|str 原始消息段
event.raw_message     # str     CQ码原始字符串
event.plain_text      # str     纯文本（自动提取）
event.emoji_likes_list # list  表情点赞列表

# 提取特定消息段
images = event.get_segments("image")
at_list = event.get_segments("at")
```

### 4.4 事件中间件

```python
@client.event_bus.middleware()
async def log_middleware(event, handler):
    print(f"[中间件] {type(event).__name__}")
    # 可以在这里做日志、限流、修改 event 等
```

---

## 五、API 调用

`client.api` 提供 100+ 方法，**自动提取响应 `data` 字段**，失败抛出 `ApiError`。

### 5.1 消息发送

```python
from napcat.message import MessageChain
# 发送群消息
result = await client.api.send_group_msg(
    group_id="123456",
    message=MessageChain().text("你好").at("987654"),
)
print(result.message_id)  # 123456

# 发送私聊消息
await client.api.send_private_msg(
    user_id="987654",
    message="你好呀",
)

# 通用发送（自动判断类型）
await client.api.send_msg(
    message_type="group", group_id="123456",
    message="Hello",
)

# 撤回消息
await client.api.delete_msg(message_id=123456)

# 标记已读
await client.api.mark_msg_as_read(group_id="123456")
```

### 5.2 群组管理

```python
# 获取群列表
groups = await client.api.get_group_list()
for g in groups:
    print(g.group_id, g.group_name)

# 获取群信息
info = await client.api.get_group_info("123456")
# Group(group_id=123456, group_name="测试群", member_count=100, ...)

# 获取成员列表
members = await client.api.get_group_member_list("123456")
# [GroupMember(user_id=987654, nickname="小明", role="admin", ...), ...]

# 获取单个成员信息
m = await client.api.get_group_member_info("123456", "987654")

# 群管理操作
await client.api.set_group_ban("123456", "987654", 600)           # 禁言600秒
await client.api.set_group_ban("123456", "987654", 0)             # 解禁
await client.api.set_group_whole_ban("123456", True)              # 全员禁言
await client.api.set_group_admin("123456", "987654", True)        # 设管理员
await client.api.set_group_kick("123456", "987654")               # 踢出
await client.api.set_group_name("123456", "新群名")               # 改群名
await client.api.set_group_card("123456", "987654", "新名片")     # 改名片
await client.api.set_group_special_title("123456", "987654", "头衔") # 设头衔
await client.api.set_group_leave("123456")                        # 退群
```

### 5.3 好友与用户

```python
# 好友列表
friends = await client.api.get_friend_list()
# [User(user_id=..., nickname=..., remark=..., ...), ...]

# 陌生人信息
await client.api.get_stranger_info("987654")

# 处理好友请求
await client.api.set_friend_add_request(flag="xxx", approve=True, remark="备注")
await client.api.delete_friend("987654")
await client.api.send_like("987654", times=10)

# 最近联系人
await client.api.get_recent_contact()
```

### 5.4 文件操作

```python
# 获取图片信息
info = await client.api.get_image("file_id")
# FileInfo(file="/tmp/xxx.jpg", url="https://...", size=12345, base64="...")

# 上传群文件
await client.api.upload_group_file(
    group_id="123456",
    file="/path/to/file.pdf",
    name="file.pdf",
)

# 下载文件
info = await client.api.download_file(url="https://example.com/doc.pdf")
print(info.file)  # 本地临时路径
```

### 5.5 系统与扩展

```python
# 登录信息
login = await client.api.get_login_info()
# LoginInfo(user_id=123, nickname="机器人", ...)

# 状态
status = await client.api.get_status()
print(status.online, status.good)

# 在线状态
await client.api.set_online_status(status=11)

# 个人资料
await client.api.set_qq_profile(
    nickname="新昵称", personal_note="签名", sex="1")

# OCR / 翻译
text = await client.api.ocr_image("image_file_id")
tr = await client.api.translate_en2zh("hello world")

# 戳一戳
await client.api.send_poke(user_id="987654", group_id="123456")

# QQ空间
result = await client.api.send_qzone_msg(
    content="今天天气不错",
    images=["https://..."],
    ugc_right=1,  # 1=所有人可见
)
await client.api.delete_qzone_msg(tid=result["tid"])
```

### 5.6 错误处理

```python
from napcat.api import ApiError
try:
    await client.api.set_group_ban("123", "456", 600)
except ApiError as e:
    print(f"API 错误 [{e.retcode}]: {e.message}")
    # 常见 retcode:
    #   1400 - 参数错误   1403 - Token 失败
    #   1404 - API 不存在  1200 - 服务器内部错误
```

---

## 六、命令系统

### 6.1 基本用法

消息以 `/` `!` `。` `#` 开头时自动解析为命令：

```python
# 注册命令（装饰器方式，推荐）
@client.register_command("ping", description="Ping 测试")
async def cmd_ping(packet):
    return "pong!"

# 注册需要权限的命令
@client.register_command("kick", permission="admin", description="踢出成员")
async def cmd_kick(packet, event):
    target = packet[0]
    await client.api.set_group_kick(str(event.group_id), target)
    return f"已踢出 {target}"
```

### 6.1.1 `/help` 三级入口（帮助文档的构成）

命令的帮助文档由**三层**拼成，每层只多展开一档信息。**整体描述写在主命令里**
（`register_command(..., description=...)`），子命令清单由**各个子命令的描述**拼出来 ——
框架渲染，插件不手写（手写会与注册信息漂移）：

| 输入 | 展示什么 | 数据来源 |
|------|----------|----------|
| `/help` | 命令清单：`[📦 命令组]` + `[📖 命令]` 两段，每行 `/{名字} — {介绍}` | 各命令的 `description` |
| `/help <主命令>` | 是命令组 → 本体**无需权限 + 作用域**，子命令清单逐条：名称 + **权限/作用域** + 描述（不带参数/用法）；<br>没有子命令 → 该命令的介绍 + **权限/作用域** + 用法/参数表 | 命令组：本体 `description`/`scope` + 各子命令生效的 `permission`/`scope`/`description`；无子命令：自身注册信息 |
| `/help <主命令> <子命令>` | 该子命令的**介绍 + 权限/作用域 + 用法/参数表** | 子命令自身生效的声明；与 `/help <主命令>.<子命令>` **等价** |

> 后两（三）种入口**必须写明权限与作用域**（`权限：superuser · 作用域：仅私聊`；公开且
> 不限场景也要写成 `权限：公开 · 作用域：所有场景`）—— 只写权限不写作用域，「这条命令在
> 群里能不能用」在帮助里就没有答案了。两处用同一句话（`describe_requirements()`），
> 详情行与子命令清单的方括号标注不会各说各话。

> 分层原则：**每级只多展开一档信息**，且**同一层里的命令排成同一个样子**
> （`/{名字} — {介绍}`）。第一级只给介绍 → **子命令不进第一级**、**叶子命令的用法也不进
> 第一级**、**也不按权限分成 `[👑 superuser]` 之类的段**（用不了的命令根本不会列出来，
> 再标权限档只会把同一类命令拆成几段）；第二级的子命令清单只给「名称 + 权限/作用域 + 描述」，
> **参数与用法不进第二级**（那是第三级）。

以 `demo` 命令组（`demo` 本体 + `demo.alpha` / `demo.beta`）和两个叶子命令为例：

```
/help
  [📦 命令组]
    /demo — 演示命令组：整体描述写在主命令里          ← 第一级：两段同版式，
  [📖 命令]                                            每行只有名称 + 描述
    /leafy — 叶子命令：带参数                          （没有子命令行、没有用法，
    /plain — 叶子命令：无子命令                          也不按权限分段）

/help demo
  📖 /demo
    演示命令组：整体描述写在主命令里
    📦 命令组 · 本体无需权限 · 作用域：所有场景 · 各子命令的权限/作用域见下行标注
    子命令
      /demo alpha [权限：公开 · 作用域：所有场景] — 子命令甲：按名字办事  ← 第二级（命令组）：
      /demo beta [权限：公开 · 作用域：所有场景] — 子命令乙：无参数        名称 + 权限/作用域 + 介绍
                                                                        （参数/用法不在这里）

/help demo alpha          （= /help demo.alpha）
  📖 /demo alpha <name> [times=1]
    子命令甲：按名字办事                              ← 第三级：介绍
    权限：公开 · 作用域：所有场景                      ← 权限 + 作用域（公开也写全）
    位置传参：/demo alpha <name> [times=1]            ← 用法与参数表
    键值传参：/demo alpha --name=<值> [--times=1]
    参数
      <name> StrField 必填 — 对象名字
      [times] StrField 可选 [默认: 1] — 重复次数

/help leafy
  📖 /leafy <name> [times=1]
    叶子命令：带参数                                  ← 第二级（无子命令）：介绍 + 权限/作用域 + 用法
    权限：admin · 作用域：所有场景
    位置传参：/leafy <name> [times=1]
    键值传参：/leafy --name=<值> [--times=1]
```

（三级输出结尾都按需带占位符图例，原文见 [§6.2.5](#625-占位符记号-必填-可选)；示例里的整体缩进仅为排版，
框架输出的缩进是每层两空格。）

> 第二段不是已注册的子命令时**回落到主命令**（`/help perm set` 这类「用法里带取值」
> 的写法照旧显示 `/perm` 详情），不会判成未知命令。每一级都按权限/范围过滤，口径与
> 执行完全一致 —— 见 [§6.2.3](#623-help-只列你能用的命令)。裸调命令组与用法错误也会落到这些帮助页，见 [§6.1.2](#612-帮助重定向裸调命令组--用法不正确)。

### 6.1.2 帮助重定向：裸调命令组 / 用法不正确

有两类输入**不执行任何动作**，而是回**对应的帮助页**（框架在 `CommandManager.handle()`
里统一处理，插件不用自己写）：

| 输入 | 行为 |
|------|------|
| `/命令组`（裸调，如 `/aichat`）| 重定向到 `/help 命令组`：本体描述 + 你有权使用的子命令。**注册在父命令上的函数不会运行**（想自定义这一页就写父命令的 `description`）；范围不符或插件过滤器否决时静默不回复 |
| `/命令组 不存在的子命令` | 同上一行 —— 子命令名写错时，这一页正好列着可用的名字（组本身用不了时仍按「未知命令」处理，不泄露命名空间）|
| 参数写错（Schema 校验失败）| `⚠️ 用法不正确：<原因>` + **该（子）命令**的帮助页：用法行、参数表、默认值、`权限：…` |

> 口径是「说不清就指路」：只回一句「参数不对」用户仍然不知道该怎么写，而帮助页与真实校验
> **同源**（用法行、参数表都由 Schema 生成），改了参数帮助自动跟着变。
> 完全未知的顶层命令（`/nosuch`）仍是「未知命令: /nosuch」。

### 6.2 命令解析规则

```
输入: /ban 987654 600
解析: prefix="/" command="ban" args=["987654", "600"]

输入: /music play qq 12345
解析: prefix="/" command="music" subcommand="play" args=["qq", "12345"]

输入: /search --limit=10 关键词
解析: prefix="/" command="search" args=["关键词"] kwargs={"limit": "10"}

输入: /echo "hello world"
解析: prefix="/" command="echo" args=["hello world"]
```

> **`--键=值` 优先于位置位**：Schema 校验时，显式写了 `--名称=值` 的字段就用键值（照样过
> 字段校验、非法值回「⚠️ 用法不正确」+ 该命令的帮助页，见 [§6.1.2](#612-帮助重定向裸调命令组--用法不正确)），其余字段才按位置逐个对应。这样调用者能**只改后面
> 某个可选参数**而跳过中间的：
>
> ```
> 输入: /group_config greeting 欢迎新同学 true --recall_delay=2m
> 解析: content="欢迎新同学" enabled=true recall_delay=120
> ```
>
> 反之，位置参数里放了非该字段类型的值（例如把 `2m` 塞进 `enabled` 这个布尔位）会被拒绝，
> 不会静默错位。

### 6.2.1 群聊里必须 @机器人

群聊消息只有 **@了机器人** 才会进入命令分发（框架入口 `command_dispatcher` 的规则，
见 [napcat/client.py](../napcat/client.py)）：`@机器人 /help` ✅、直接发 `/help` ❌（连 `/help` 自己也不例外）。
私聊没有这个限制。

因此 **`/help` 在群聊里会额外打一行提示**：

```
📖 可用命令（9）
⚠️ 群里需先 @机器人再发命令（直接复制命令不触发）
```

`/help <命令名>` 详情同样会带上这句（仅当该命令在群里本来就能用时；`scope="private"`
的命令只显示「仅私聊」，不会误导）。

### 6.2.2 群聊临时会话（小窗）

群成员在群里点你头像开的**临时会话（小窗）**，在 OneBot 里长这样：

```json
{"message_type": "private", "sub_type": "group", "group_id": 123456, "user_id": 10001}
```

框架的处理：

| 环节 | 行为 |
|------|------|
| 触发条件 | 按**私聊**处理：不需要 @，消息直接进命令分发 |
| 回复路由 | 自动带 `group_id` 调 `send_private_msg`（`MessageEvent.is_temp_session`）——**不带的话 NapCat 会当好友私聊发送，非好友直接失败**（小窗收不到回复） |
| 权限/作用域 | 仍按**私聊**判定（`is_private` 语义）：`scope="private"` 的命令可用，`scope="group"` 的被拒（返回「未知命令」），群角色不参与判定 |
| 插件自己发消息 | 用 `event.is_temp_session` 判断，转发时把 `event.group_id` 传给 `send_private_msg(group_id=…)`；参数校验失败的回执（见 [§6.1.2](#612-帮助重定向裸调命令组--用法不正确)）同样按来源回：群里回群、小窗回小窗，不会把错误泄露到群里 |

```python
# 插件里想把消息发回「消息来的地方」（群 / 私聊 / 小窗）
if event.message_type == "group":
    await self.api.send_group_msg(str(event.group_id), msg)
else:
    await self.api.send_private_msg(
        str(event.user_id), msg,
        group_id=str(event.group_id) if event.is_temp_session else None)
```

### 6.2.3 `/help` 只列「你能用的」命令

`/help` 按**调用者当下能实际执行**的命令来列，口径与 `CommandManager.handle()` 完全一致。
列表的构成见 [§6.1.1](#611-help-三级入口帮助文档的构成)：只列**命令组**与**没有子命令的命令**（子命令在 `/help <命令组>` 里）：

| 规则 | 说明 |
|------|------|
| 范围过滤 | `scope` 与当前上下文不符的命令不列（私聊命令不出现在群里，反之亦然）|
| 权限过滤 | `check_permission` 不通过的命令不列；**第一级列表不按权限分段**（没有 `[👑 superuser]` 之类的标题）—— 列出来的就是你能用的 |
| 权限/作用域 | 单命令详情、命令组页、子命令详情**都写明** `权限：xxx · 作用域：…`：命令组页逐条挂在子命令名后（`/buptnews add [权限：superuser · 作用域：仅私聊] — 添加推送目标`），详情页单独一行；公开且不限场景写成 `权限：公开 · 作用域：所有场景` |
| 命令组 | 本体不设权限（父命令上的 `permission` 只是子命令默认值），所以命令组本身始终可见；子命令**不在第一级列表里**，由 `/help <命令组>` 按各自 scope/permission 过滤后逐条列出（**只给名称 + 描述**；权限/范围看各子命令的详情）|
| 详情 | `/help <命令名>` 对无权限的命令按「未知命令」处理（与执行时的隐藏口径一致）；`/help <命令组>` 只列你可用子命令 |
| 计数 | 首行 `📖 可用命令（N）` 的 N = 可见的命令组数 + 无子命令命令数（不含子命令）|

效果（同一个群里的三种身份；条数与内容随该部署的命令数变化）：

```
普通成员  → [📦 命令组] + [📖 命令]（公开命令，如 /groupinfo）
群管理员  → 上面 + [📖 命令] 里的 admin 命令（如 /ban /kick /unban）
超级用户  → 上面 + [📖 命令] 里的 superuser 专属命令（/bind /perm …）
            （私聊里还能看到仅私聊的命令；分段标题始终只有「命令组」与「命令」两个）
```

### 6.2.4 引号与转义（字符串参数）

参数按 shlex 规则分词，`"…"` 与 `'…'` 都能把空格包进同一个参数；**框架在分词后统一处理
转义序列**：`\n` `\t` `\r` `\\` `\"` `\'`。

| 输入 | 解析结果 | 说明 |
|------|----------|------|
| `/echo "hello world"` | `["hello world"]` | 双引号 |
| `/echo 'hello world'` | `["hello world"]` | 单引号 |
| `/echo 'don\'t'` | `["don't"]` | **单引号内转义单引号**（历史上会判成引号没配对）|
| `/echo don\'t` | `["don't"]` | 不加引号也能转义 |
| `/echo "say \"hi\""` | `["say "hi""]` | 双引号内转义双引号 |
| `/echo 'a\'b\\c'` | `["a'b\\c"]` | 转义单引号与反斜杠混用 |
| `/echo 'a\nb'` | `["a\nb"]`（含真实换行）| 单引号内 `\n` 仍按框架约定转义 |
| `/echo ‘a b’ “c d”` | `["a b", "c d"]` | 中文引号自动等价于 ASCII 引号 |
| `/echo We're BUPT` | `["We're", "BUPT"]` | 引号没配对时**宽松回退**：按空白切，不会整条报错 |

> 想把含空格的整段文本作为**一个**参数（例如欢迎语），请加引号：
> `/group_config greeting 'We\'re BUPT!'`。不加引号时多词会按空白拆成多个参数，
> 只有第一个会进入单值字段。

### 6.2.5 占位符记号：`<>` 必填、`[]` 可选

所有帮助文本（`/help` 首屏、`/help <命令名>` 详情、`/help <命令组>` 的子命令清单、参数表）**只使用两种记号**，
并且都会在末尾附一行图例，明确告诉用户这是占位符、括号本身不用输入：

| 记号 | 含义 | 例子 |
|------|------|------|
| `<名称>` | **必填** | `/buptnews get <id>` |
| `[名称]` | **可选**（带默认值时写成 `[名称=默认]`） | `/buptnews sync [pages=1] [preview=False]` |
| `[名称 ...]` | 可变参数，吃剩余所有位置参数 | `/xxx tags [标签 ...]` |

图例原文：

```
ℹ️ <名称> 必填，[名称] 可选；尖括号/方括号不用输入
```

实现口径（`napcat/command.py`）：

- 用法行由 Schema 生成（`build_usage()` / `format_field_usage()`），子命令同样生效 ——
  显式传 `usage=` 也可以，两者都以同一套记号输出；
- 参数表（`describe_fields()`）里的字段名同样标注 `<>` / `[]`，与用法行口径一致；
- 报错文案也用同一记号（如 `请指定群号: /bind <群号>`）；
- 图例只在**这段帮助真的渲染了占位符**时才追加（`/help ping` 这类无参命令不会多这一行），
  所以插件不要再自己追加一次，否则会连出两行（命令组页的渲染完全由框架负责，见 [§6.1.2](#612-帮助重定向裸调命令组--用法不正确)）。

### 6.3 CommandPacket 字段

```python
packet.raw           # str     原始文本（不含前缀）
packet.prefix        # str     命中的前缀（如 "/"）
packet.command       # str     命令名（已转小写）
packet.subcommand    # str?    子命令
packet.args          # [str]   位置参数列表
packet.kwargs        # {str:str} 关键字参数（--key=value）
packet.event         # MessageEvent  原始消息事件

# 快捷访问
packet[0]            # 等同于 packet.arg(0)
packet.arg(1, "默认") # 获取位置参数，不存在时返回默认
packet.opt("key")    # 获取 --key=value

# 非法输入标记
packet.illegal       # bool    是否非法
packet.illegal_reason # str    非法原因
```

### 6.4 命令返回值处理

命令函数可以返回 `str` / `MessageChain`，客户端会自动将返回值作为回复发送：

```python
@client.register_command("天气")
async def cmd_weather(packet, api):
    city = packet.arg(0, "北京")
    weather = await fetch_weather(city)
    return MessageChain().text(f"{city}天气: {weather}")

@client.register_command("status")
async def cmd_status(packet, event):
    # 返回 None 表示不自动回复
    return None
```

> **注意**：命令返回值会通过 `send_group_msg` 自动发送到命令来源群。私聊命令会通过 `send_private_msg` 发送。

**太长就分段**：返回 `list[MessageChain]`（或 `list[str]`）时按顺序逐条发送。
直接用 `MessageChain.split()` 切，它会保持排版与顺序：

```python
@client.register_command("长文")
async def cmd_long(packet):
    chain = MessageChain().text(packet.arg(0, ""))    # 很长的内容
    chunks = chain.split(max_chars=1200, max_images=4)  # 0 = 该维度不限
    return chunks[0] if len(chunks) == 1 else chunks
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `max_chars` | `1200` | 每条消息最多多少文字字符；文字优先在 `\n` 处断开，找不到换行才硬切 |
| `max_images` | `4` | 每条消息最多多少媒体段（图片/语音/视频/文件）；单个媒体段不会被拆开 |

分段只影响「一条消息变多条」，**顺序与内容与原来完全一致**；发送过程中断线则停止后续分段。

### 6.5 子命令

```python
# 点号分隔自动注册为父子命令
@client.register_command("music.play", description="播放音乐")
async def music_play(packet):
    song = packet.arg(0)
    return f"正在播放: {song}"

@client.register_command("music.stop", description="停止播放")
async def music_stop(packet):
    return "已停止播放"

# 用法:
# /music play 晴天
# /music stop
```

> **命令组（有子命令的命令）本体不设权限**：父命令上写的 `permission`/`scope` 只作为
> 子命令的默认值（子命令可单独覆盖），不会用来拦截 `/music` 本身 —— 父命令函数只负责
> 分发/打印用法，任何人都能触发（`scope` 仍然生效）。需要权限的逻辑必须放进子命令，
> 框架会在路由到子命令**之前**按该子命令自己的声明校验。
> 子命令不传 `permission` 表示继承父命令；传 `permission=""` 表示**显式无权限限制**。

### 6.6 非法输入与权限拒绝回调

```python
@client.cmds.on_illegal
async def on_illegal(packet, event):
    await client.api.send_group_msg(
        group_id=str(event.group_id),
        message=f"命令格式错误: {packet.illegal_reason}",
    )

@client.cmds.on_denied
async def on_denied(packet, event):
    await client.api.send_group_msg(
        group_id=str(event.group_id),
        message="你没有权限执行此命令",
    )
```

### 6.7 手动解析命令

```python
from napcat.command import CommandParser
parser = CommandParser(prefixes=["/", "!"])
packet = parser.parse("/ban 987654 600")

if packet and not packet.illegal:
    print(packet.command, packet.args)  # ban ['987654', '600']
```

---

### 6.8 命令别名（`/alias`）

别名不是「另一条命令」：它在命令表里指向**同一个 handler**，所以权限、schema、
作用域、归属标签与目标命令完全一致，插件卸载时也一起回收。仅私聊 + 仅超级用户
可用（与 `/perm`、`/userperm` 同档）。

```
/alias                          列出全部别名
/alias list demo                看某条命令的别名
/alias add d demo               给 /demo 加别名 /d（之后 /d 与 /demo 等价）
/alias del d                    删除别名
/alias set demo d1 d2           整体替换 /demo 的别名（也可以写成 d1,d2）
/alias set demo                 清空 /demo 的全部别名
/alias move d other             把 /d 改指向 /other
```

规则：

- 别名要**字母开头**，只能含字母/数字/下划线/连字符，长度不超过命令名上限；
- 不能与现有**命令名**或**别的别名**冲突；不能等于目标命令自己的名字；
- 目标只能是**顶层命令**：命令组本体（如 `/demo` 有子命令）不执行动作、子命令
  （`父.子`）暂不支持别名 —— 会直接说明原因；
- 改动落盘到 [config.json](../config.json)：

  ```json
  "command_aliases": { "demo": ["d1", "d2"] }
  ```

  配置里一旦出现某个命令，它就是该命令别名的**全集**：空列表表示清空，连插件
  `register_command(..., aliases=[...])` 里声明的别名也会被压掉；删掉这个键则恢复
  代码里的声明值；
- 插件热重载 / 重新注册后别名自动恢复（重放点在命令登记处）；命令被
  `/stop command` 或插件卸载移除时，它的别名条目一起清掉；命令暂时不在（插件没装）
  时配置会保留并在 `/alias` 列表里标「未注册」，重装后自动生效。

## 七、权限管理

### 7.1 角色层级

```
superuser (100) > owner (80) > admin (60) > member (20) > everyone (0)
```

- **superuser** 拥有所有权限，通过 `NapCatClient(superusers=[...])` 配置
- **owner/admin/member** 从**每条消息的 `sender.role`** 缓存（`GroupAdminEvent` 变更时也会更新）——
  所以群主 / 管理员**默认至少是 admin**，启动后不需要先发生一次管理员变更
- `"everyone"` 权限始终判定为通过

### 7.2 命令权限绑定

```python
@client.register_command("reload", permission="superuser")
async def cmd_reload(packet):
    name = packet[0]
    await client.plugins.reload(name)

@client.register_command("kick", permission="admin")
async def cmd_kick(packet, event):
    ...

@client.register_command("ping", permission="everyone")   # 等同于不设 permission
async def cmd_ping(packet):
    return "pong!"
```

### 7.3 手动权限检查

```python
# 检查权限
can = await client.perm.check(user_id=987654, permission="admin", group_id=123456)

# 检查角色级别
is_admin = await client.perm.check_role(user_id=987654, role="admin", group_id=123456)

# 管理超级用户
client.perm.add_superuser(111111)
client.perm.remove_superuser(111111)
client.perm.set_superusers([123, 456])

# 手动设置群角色
client.perm.set_group_role(group_id=123456, user_id=987654, role="admin")
```

### 7.4 自定义权限节点

```python
# 注册自定义权限检查器
@client.perm.register("vip")
async def check_vip(user_id: int, group_id: int = None) -> bool:
    # 异步查询数据库、API 等
    return user_id in some_vip_list

# 使用自定义节点
@client.register_command("vip_only", permission="vip")
async def cmd_vip(packet):
    return "你确实是 VIP"
```

### 7.5 用命令改权限（`/perm`）

超级用户私聊里可以直接改某条命令（或子命令）的权限，不用改代码、不用重启：

```
/perm                          列出全部覆盖
/perm get buptnews.get         查看某条命令的权限
/perm set buptnews.get admin   把「查通知详情」改成需群管理员
/perm set ping public          放开为所有人可用
/perm reset buptnews.get       恢复代码里声明的权限
/perm reset all                清空全部覆盖
```

规则：

- 目标写顶层命令名或 `父.子`（子命令）；**命令组本体**（如 `aichat`）不能设 ——
  本体不校验权限，请逐条设置子命令（`/perm set aichat.new ...`）。
- 可用取值：`superuser`、`owner`、`admin`、`member`、`public`（= 公开；`everyone`/`公开`/`-`
  等价）、以及插件已注册的自定义节点（如 `vip`）。拼错会当场拒绝并列出可用值。
- 改动落盘到 [config.json](../config.json)：

  ```json
  "command_permissions": { "buptnews.get": "admin", "ping": "superuser" }
  ```

  重启与插件热重载后仍生效；全部清空时这个键会被删掉。手写这个键也可以（不认识的值会记
  warning 并保留，但那时该命令只有超级用户能用）。
- 超级用户在任何情况下都不受权限限制（`check()` 对超级用户无条件通过），所以不存在
  「把自己锁在外面」：改坏了照样能用 `/perm` 改回来。
- `/perm` 本身是**仅私聊 + 仅超级用户**，与 `/stop`、`/pause`、`/reload` 同档。

### 7.6 用户级权限（`/userperm`）

`/perm` 改的是「**命令**要求什么权限」，`/userperm` 改的是「**这个人**算几级」。
仅私聊 + 仅超级用户可用（与 `/perm` 同档）。

```
/userperm                             列出全部用户级覆盖
/userperm list 123456                 只看某个群的覆盖
/userperm get 987654                  查看某人当前生效的级别与来源
/userperm set 987654 admin            设为**全局** admin（所有场景）
/userperm set 987654 member 123456    仅在该群内设为 member
/userperm set 987654 admin --group=123456   同上（键值写法）
/userperm clear 987654                清除全局覆盖
/userperm clear 987654 123456         清除该群内的覆盖
```

优先级（高 → 低）：

```
群内覆盖 > 全局覆盖 > 群角色（群主/管理员默认 admin）> 默认成员
```

- **可设置级别**：`owner` / `admin` / `member` / `everyone`。**不能设 `superuser`** ——
  超管只能在 `config.json` 的 `superusers` 里配（一条命令就能提到最高级 = 权限系统没有边界）；
  给超管设覆盖也会被拒绝并提示「超管不受用户级覆盖影响」；
- **给群号 = 仅该群**；不给 = 全局。群内覆盖只影响那一个群，别的群仍按全局/群角色算；
- 群主 / 管理员默认 admin，所以通常**不需要**给每个管理员单独配；只有「想在群里降级某个
  管理员」或「想让普通成员在某群能管理」时才用；
- 改动落盘到 [config.json](../config.json)：

  ```json
  "user_permissions": {
    "users":  { "987654": "admin" },
    "groups": { "123456": { "987654": "member" } }
  }
  ```

  启动时恢复；手写这个键也可以 —— 非法 QQ / 非法级别只记 warning 并跳过那一条，
  不会让机器人起不来；全部清空时键会被删掉；
- 插件的 `await self.perm.check(...)` / `check_role(...)` 会自动套用这些覆盖，插件不用改。

---

## 八、插件开发

### 8.1 插件基类

```python
from napcat.plugin import PluginBase
from napcat.models import MessageEvent
from napcat.message import MessageChain
class MyPlugin(PluginBase):
    name = "my_plugin"
    version = "1.0.0"
    description = "我的插件"
    author = "Me"

    async def on_load(self):
        """插件被加载（初始化，不注册处理器）"""
        self.logger.info(f"{self.name} 已加载")

    async def on_enable(self):
        """插件被启用（注册事件/命令/定时器）"""
        @self.on_message()
        async def handle(event: MessageEvent):
            if "关键词" in event.plain_text:
                await self.api.send_group_msg(
                    group_id=str(event.group_id),
                    message=MessageChain().text("检测到关键词"),
                )

        @self.register_command("hello")
        async def hello_cmd(packet, event):
            return f"你好 {event.sender.nickname}!"

        @self.schedule(300, name="5min_check")  # 每5分钟
        async def periodic_task():
            self.logger.info("定时任务执行")

    async def on_disable(self):
        """插件被禁用（清理处理器）"""
        # 注意：通过 self.on_message / self.on_event 注册的处理器
        # 会在 PluginManager.disable() 时自动取消

    async def on_unload(self):
        """插件被卸载（释放资源）"""
        pass
```

### 8.2 插件内置便捷方法

| 方法 | 说明 |
|------|------|
| `self.on_message(group_id=..., private=...)` | 注册消息事件 |
| `self.on_event(EventType)` | 注册指定类型事件 |
| `self.register_command(name, ...)` | 注册命令 |
| `self.schedule(seconds, name=...)` | 注册周期定时任务 |
| `self.schedule_at("HH:MM", name=...)` | 注册每日定时任务 |
| `self.api` | ApiClient |
| `self.event_bus` | EventBus |
| `self.timer` | TimerManager |
| `self.cmds` | CommandManager |
| `self.perm` | PermissionManager |
| `self.files` | FileManager |
| `self.logger` | Logger |

### 8.3 插件文件约定

1. 插件文件放在 `./plugins/` 目录下（或客户端初始化时指定的 `plugin_dir`）
2. 文件名 = 插件名，如 `my_plugin.py`
3. 文件中必须有一个（或多个）继承 `PluginBase` 的公开类
4. 以 `_` 开头的文件会被自动忽略（`_disabled.py`）

### 8.4 插件管理 API

```python
# 加载/启用/禁用/卸载
await client.plugins.load("my_plugin")
await client.plugins.enable("my_plugin")
await client.plugins.disable("my_plugin")
await client.plugins.unload("my_plugin")

# 全部操作
await client.plugins.load_all()
await client.plugins.enable_all()

# 获取插件
plugin = client.plugins.get("my_plugin")
print(plugin.version, plugin.enabled)

# 列出所有
for name, p in client.plugins.plugins.items():
    print(name, p.version, p.enabled)
```

### 8.5 运行时热重载

```python
# 手动热重载
await client.plugins.reload("my_plugin")

# 全部热重载
await client.plugins.reload_all()
```

**自动文件监控**：客户端启动时自动调用 `client.plugins.watch(interval=2.0)`，每 2 秒检测插件文件 mtime，发现变更自动热重载。开发时只需保存文件即可生效。

```python
# 可在开发完成后关闭文件监控
client.plugins.stop_watch()
```

### 8.6 完整生命周期

```
PluginManager.load()
  └→ __init__()
  └→ on_load()

PluginManager.enable()
  └→ on_enable()       # 在这里注册事件/命令/定时器

... 运行中 ...

PluginManager.disable()
  └→ on_disable()      # 自动取消注册本次启用注册的 _handlers
                       # （定时任务与命令仍由插件自己清理，见下）

PluginManager.unload()
  └→ on_unload()       # 释放资源；框架顺带回收本插件的
                       # 事件处理器 + 定时任务 + 命令，并从 sys.modules
                       # 摘掉整棵模块子树（含 .main / .store 等子模块）
```

> **失败不会静默**：加载/启用/卸载/重载失败会记完整日志并私聊超管；
> 运行期（命令、事件、定时任务）失败会记日志并回给触发者 ——
> 群里会 **@ 出错时提问的那个人**，文案里带错误编号（`E-xxxx`），
> 同一编号 60 秒内重复出现只回一句短提示（不静默、也不刷屏）。
> 策略可在 `config.json` 顶层调：`error_report.enabled / throttle_seconds /
> show_reason / notify_superusers`。插件能抛的错误类型见
> [plugin-dev.md §十六](plugin-dev.md#十六错误处理与用户反馈)。

### 8.6.1 首次运行即初始化全部配置项

插件被**构造**时（`on_load()` 之前）框架会按它的 `config_template()` 把**全部**
配置项补进 `config.json` 的 `plugins.<插件名>` 段 —— 任何构造路径（框架加载、
测试直接 new、脚本手动构造）都一样：

- **只补缺键**：运维改过的值一个都不动；插件升级新增的配置项会自动补进老部署；
- **嵌套段逐键补**（`weather` / `images` / `binding` 这类段里缺的子键也补）；
- **幂等**：没有缺键就完全不写盘，有缺键也只写一次。

所以「这个插件有哪些开关可改」永远能在 `config.json` 里看到，不用翻代码。
插件侧写法见 [plugin-dev.md §15.3.1](plugin-dev.md#1531-首次运行即初始化全部配置项config_template)。

### 8.7 完整插件示例

```python
# plugins/welcome.py
from napcat.plugin import PluginBase
from napcat.models import MessageEvent, GroupIncreaseEvent
from napcat.message import MessageChain
class WelcomePlugin(PluginBase):
    name = "welcome"
    version = "1.0.0"
    description = "入群欢迎 + 退群通知"

    def config_template(self) -> dict:
        """完整默认配置：框架在加载时写进 config.json → plugins.welcome。

        每个插件都要实现它 —— 「首次运行即把所有配置项写进文件」是框架契约，
        运维打开 config.json 就能看到有哪些开关可改。
        """
        return {
            "enabled": True,
            "greeting": "欢迎加入！请注意群规～",
        }

    async def on_enable(self):
        @self.on_event(GroupIncreaseEvent)
        async def welcome(event: GroupIncreaseEvent):
            await self.api.send_group_msg(
                group_id=str(event.group_id),
                message=MessageChain()
                    .at(event.user_id)
                    .text(" " + (self.config.get("greeting") or "欢迎！")),
            )

        @self.register_command("welcome_toggle", permission="admin")
        async def toggle(packet):
            # ⚠️ PluginBase **没有** `self.disable()`（旧文档这里写的
            # `await self.disable()` 会直接 AttributeError）。命令作用域内的
            # 「关掉这个功能」要自己实现：撤销本命令注册的处理器 + 记一个开关位。
            self.off_all_events()
            self.unregister_command("welcome_toggle")
            return "欢迎功能已关闭"
```

> 想让**超管停用整个插件**，运维路径是 `/reload <插件名>`（会重新走
> `on_disable → on_load → on_enable`），或者把插件目录改名成 `_name`
> 让框架不再加载它；插件内部没有「自禁用」接口。

### 8.8 用命令装卸插件（`plugin_manager`）

框架自带 [plugins/plugin_manager](../plugins/plugin_manager/)，超管可以在**私聊**里把插件从 GitHub
装进来、更新、卸载 —— 不用登服务器改文件。列表文件在
`Green1423/ncqqpy-plugins` 的 `plugins.json`：

```
/plugin list                    看有哪些插件、装没装
/plugin info ai_chat            看单个插件的来源与状态
/plugin check                   检查全部已装插件的上游更新
/plugin install ai_chat         安装
/plugin update all              全部更新
/plugin uninstall ai_chat       卸载（进回收站 plugins/.trash/，可找回）
/plugin add <仓库> [name]        装列表文件里没有的仓库
```

> 🔒 **仅私聊 · 仅超管**：这些子命令都声明 `permission="superuser"` +
> `scope="private"`（装插件等于在服务器上执行代码）。非超管在私聊里、
> 超管在群里，看到的都是「命令不存在」。

几条保险设计（细节见 [plugins/plugin_manager/README.md](../plugins/plugin_manager/README.md)）：

- **更新不覆盖你手改过的文件**：冲突时停下来列出文件名，`--force=true` 才覆盖，
  旧版本进回收站；
- **`data/` 与你自己新增的文件永不删除**：更新时跟着走，卸载时留在原地
  （`--purge=true` 才连 `data/` 一起进回收站）；
- **下载的源码包会做安全检查**：拒绝路径穿越、绝对路径、符号链接，
  且校验包里的插件名与要装的名字一致；
- **不装卸自己**：`plugin_manager` 拒绝在运行中动自己，避免把管理器删掉。

**插件之间的依赖**：插件可以声明必需依赖（`requires`）与可选依赖
（`optional_requires`）—— 装一个带依赖的插件时，装完的回执会直接告诉你
「还缺谁、怎么补」，缺的装上后它会自动加载；被别人依赖着的插件**不能直接
卸载/禁用**（会被拒绝，或按提示先处理使用者）。`/plugins` 按依赖顺序列出
插件，被依赖挡住的显示 `[⛔] 名字 —— 缺少 X`。插件侧写法见
[plugin-dev.md §1.3](plugin-dev.md#13-声明依赖别的插件必需--可选)。

配置（`config.json → plugins.plugin_manager`）：`manifest_url`（换列表来源）、
`token`（GitHub token，留空 = 匿名，每小时 60 次 API）、`timeout`、`proxy`、
`manifest_cache_seconds`。身份建议用环境变量 `NCQQPY_GITHUB_TOKEN` /
`GITHUB_TOKEN` 给，避免明文写进 `config.json`。

---

## 九、文件管理

### 9.1 基础操作

```python
# 图片/语音/文件信息
img = await client.files.get_image("file_id")
print(img.file, img.url, img.size, img.base64)

# 上传群文件
file_id = await client.files.upload_group_file(
    group_id="123456",
    file_path="./test.pdf",
    name="测试.pdf",
)

# 下载到本地
local_path = await client.files.download("https://example.com/file.pdf", save_path="./dl/")
```

### 9.2 流式下载

```python
# 分片迭代
async for chunk in client.files.download_stream("https://example.com/big.bin"):
    await outfile.write(chunk)

# 带进度回调
await client.files.download_to_file(
    url="https://example.com/big.zip",
    save_path="./downloads/big.zip",
    on_progress=lambda received, total: print(f"\r{received} bytes"),
)
```

### 9.3 文件 Hash

```python
sha = await FileManager.file_sha256("./local_file.zip")
```

---

## 十、定时事件

### 10.1 周期任务

```python
# 每 N 秒执行一次
@client.timer.every(60, name="save_cache")
async def save():
    print("每分钟保存缓存")

@client.timer.every(3600)  # 自动以函数名作为任务名
async def hourly_report():
    print("每小时报告")
```

### 10.2 每日定时

```python
# 每天指定时间
@client.timer.at("08:00", name="morning")
async def morning():
    print("早上好")

@client.timer.at("23:59")
async def daily_summary():
    print("每日总结")
```

### 10.3 延迟执行

```python
@client.timer.delay(5, name="delayed_start")
async def init():
    print("5秒后初始化")
```

### 10.4 定时器管理

```python
# 取消定时任务
client.timer.cancel("save_cache")

# 全局启停
client.timer.stop()
client.timer.start()
```

---

## 十一、高级用法

### 11.1 EventBus 直接使用

```python
bus = client.event_bus

# 注册
@bus.on(HeartbeatEvent)
async def on_heartbeat(event):
    print(f"心跳: {event.interval}ms")

# 通配
@bus.on_any()
async def on_all(event):
    print(type(event).__name__)

# 手动分发
await bus.dispatch(some_event)

# 取消注册
bus.off(MessageEvent, handler_func)
```

### 11.2 低频 API 直接调用

`ApiClient` 封装过的 API 列表见[第十二章](#十二api-参考速查表)。如需调用未封装的 API：

```python
from napcat.api import ApiClient

# 通过 _call 直接调用
data = await client.api._call("some_action", {"param": "value"})
```

### 11.3 自定义日志

```python
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("napcat")
logger.setLevel(logging.DEBUG)

# 插件内使用
class MyPlugin(PluginBase):
    async def on_enable(self):
        self.logger.info("插件启用")
```

### 11.4 事件中发送 API

```python
@client.on_message()
async def handle(event):
    # 使用 client.api 直接调用
    await client.api.send_group_msg(
        group_id=str(event.group_id),
        message=MessageChain().text("回复"),
    )
```

---

## 十二、API 参考速查表

### 12.1 ApiClient 方法清单

| 方法 | 参数 | 返回 |
|------|------|------|
| **系统** | | |
| `get_login_info()` | 无 | `LoginInfo` |
| `get_status()` | 无 | `StatusInfo` |
| `get_version_info()` | 无 | `VersionInfo` |
| `can_send_image()` | 无 | `bool` |
| `can_send_record()` | 无 | `bool` |
| `clean_cache()` | 无 | `None` |
| `set_restart()` | 无 | `None` |
| `bot_exit()` | 无 | `None` |
| `get_cookies(domain)` | domain | `Any` |
| `get_csrf_token()` | 无 | `Any` |
| `get_credentials()` | 无 | `Any` |
| `get_group_system_msg()` | 无 | `Any` |
| `get_online_clients()` | 无 | `Any` |
| **消息** | | |
| `send_msg(*, message_type, user_id, group_id, message, auto_escape)` | | `SendMsgResult` |
| `send_private_msg(user_id, message, auto_escape)` | | `SendMsgResult` |
| `send_group_msg(group_id, message, auto_escape)` | | `SendMsgResult` |
| `delete_msg(message_id)` | | `None` |
| `get_msg(message_id)` | | `Any` |
| `get_forward_msg(*, message_id, id)` | | `Any` |
| `send_forward_msg(*, message_type, group_id, user_id, messages)` | | `Any` |
| `mark_msg_as_read(*, user_id, group_id)` | | `None` |
| `set_msg_emoji_like(message_id, emoji_id, is_add)` | | `None` |
| `fetch_ptt_text(message_id)` | | `str` |
| `get_friend_msg_history(user_id)` | | `Any` |
| `get_group_msg_history(group_id)` | | `Any` |
| **好友/用户** | | |
| `get_friend_list(no_cache)` | | `list[User]` |
| `get_stranger_info(user_id)` | | `Any` |
| `get_friends_with_category()` | | `Any` |
| `set_friend_add_request(flag, approve, remark)` | | `None` |
| `set_friend_remark(user_id, remark)` | | `None` |
| `delete_friend(user_id)` | | `None` |
| `send_like(user_id, times)` | | `None` |
| `get_recent_contact()` | | `Any` |
| `get_profile_like()` | | `Any` |
| **群组** | | |
| `get_group_list(no_cache)` | | `list[Group]` |
| `get_group_info(group_id)` | | `Group` |
| `get_group_member_list(group_id, no_cache)` | | `list[GroupMember]` |
| `get_group_member_info(group_id, user_id, no_cache)` | | `GroupMember` |
| `get_group_honor_info(group_id, type)` | | `Any` |
| `set_group_ban(group_id, user_id, duration)` | | `None` |
| `set_group_whole_ban(group_id, enable)` | | `None` |
| `set_group_admin(group_id, user_id, enable)` | | `None` |
| `set_group_kick(group_id, user_id, reject_add_request)` | | `None` |
| `set_group_leave(group_id, is_dismiss)` | | `None` |
| `set_group_name(group_id, group_name)` | | `None` |
| `set_group_card(group_id, user_id, card)` | | `None` |
| `set_group_special_title(group_id, user_id, special_title)` | | `None` |
| `set_group_add_request(flag, sub_type, approve, reason)` | | `None` |
| `get_group_at_all_remain(group_id)` | | `Any` |
| `get_group_shut_list(group_id)` | | `Any` |
| `set_group_sign(group_id)` | | `None` |
| `send_group_sign(group_id)` | | `None` |
| **公告** | | |
| `get_group_notice(group_id)` | | `Any` |
| `send_group_notice(group_id, title, content)` | | `Any` |
| `del_group_notice(group_id, notice_id)` | | `None` |
| **精华** | | |
| `get_essence_msg_list(group_id)` | | `Any` |
| `set_essence_msg(message_id)` | | `None` |
| `delete_essence_msg(message_id)` | | `None` |
| **文件** | | |
| `get_image(file)` | | `FileInfo` |
| `get_record(file)` | | `FileInfo` |
| `get_file(file)` | | `FileInfo` |
| `get_group_file_url(group_id, file_id)` | | `Any` |
| `upload_group_file(group_id, file, name, folder, upload_file)` | | `Any` |
| `download_file(*, url, base64, name, headers)` | | `FileInfo` |
| `delete_group_file(group_id, file_id)` | | `None` |
| `get_group_root_files(group_id)` | | `Any` |
| `get_group_files_by_folder(group_id, folder_id)` | | `Any` |
| **扩展** | | |
| `set_online_status(status, ext_status, battery_status)` | | `None` |
| `set_qq_profile(nickname, personal_note, sex)` | | `None` |
| `set_qq_avatar(file)` | | `None` |
| `set_self_longnick(long_nick)` | | `None` |
| `ocr_image(image)` | | `Any` |
| `translate_en2zh(words)` | | `Any` |
| `send_poke(user_id, group_id, target_id)` | | `None` |
| `set_input_status(user_id, event_type)` | | `None` |
| `get_clientkey()` | | `Any` |
| `send_group_ark_share(group_id, ark_data)` | | `Any` |
| `get_mini_app_ark(app_id, app_path)` | | `Any` |
| `get_ai_record(group_id, message_id)` | | `Any` |
| `get_ai_characters()` | | `Any` |
| **频道** | | |
| `get_guild_list()` | | `Any` |
| `get_guild_service_profile(guild_id)` | | `Any` |
| **QQ 空间** | | |
| `send_qzone_msg(content, images, ugc_right, target_uins)` | | `Any` |
| `delete_qzone_msg(tid)` | | `None` |
| **闪传/在线文件** | | |
| `send_flash_msg(file_set_id, group_id, user_id)` | | `Any` |
| `send_online_file(peer_id, file_path, name)` | | `Any` |
