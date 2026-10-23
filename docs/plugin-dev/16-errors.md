# 十六、错误处理与用户反馈

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[配置管理](15-config.md)


**一句话**：插件**抛什么异常**决定用户看到什么、谁收到；**怎么发**由框架统一负责，
插件不要自己拼「出错了」的文案。

## 16.1 三种写法，按你掌握的信息量选

```python
from napcat.errors import (
    PluginConfigError,
    PluginTimeoutError,
    PluginRateLimitError,
)
class MyPlugin(PluginBase):
    async def on_enable(self):
        @self.register_command("ask", schema=AskSchema)
        async def cmd_ask(params, event):
            if not self.config.get("api_key"):
                # ① 用户看得懂、也知道该找谁 → 直接给文案（原文回给用户）
                raise PluginConfigError("没配 api_key，找超管在 config.json 里补")

            try:
                return await self._call_upstream(params.q)
            except TimeoutError as e:
                # ② 分类明确 → 抛对应类型：文案自带「可以稍后再试」，
                #    日志与用户文案里都带同一个错误编号
                raise PluginTimeoutError(cause=e, plugin=self.name) from e
            except UpstreamQuota as e:
                raise PluginRateLimitError(cause=e, plugin=self.name) from e
            # ③ 其它异常：别 catch —— 框架包成 PluginRuntimeError 记录完整堆栈，
            #    用户收到「运行错误（E-xxxx）+ 短原因」，不会有「有问不答」
```

`self.errors`（`ErrorReporter`）是给**需要自己投递**的插件用的
（例如 ai_chat 要按会话分段/出图）：`render()` 取文案、`log()` 记日志（带编号）。

## 16.2 错误类型速查（`from napcat.errors import ...`）

| 类型 | 分类文案 | 可重试 | 受众 | 用在哪 |
|------|---------|--------|------|--------|
| `PluginConfigError` | 配置错误 | — | 触发者 | 配置缺失/非法，用户能自己改或找超管 |
| `PluginTimeoutError` | 上游超时 | ✅ | 触发者 | 上游/网络超时 |
| `PluginRateLimitError` | 被限流 | ✅ | 触发者 | 被限流、额度用尽 |
| `PluginExternalError` | 上游错误 | ✅ | 触发者 | 其它外部依赖失败（父类）|
| `PluginAccessError` | 无权访问 | — | 触发者 | 服务范围/白名单拒绝 |
| `PluginRuntimeError` | 运行错误 | — | 触发者 | 插件 bug（框架兜底用，也可主动抛）|
| `CommandExecutionError` | 命令失败 | — | 触发者 | 等价于「构造参数就是要回给用户的那句话」（历史写法，仍支持）|
| `PluginLoadError` / `PluginLifecycleError` | 装载/生命周期失败 | — | **超管** | 框架在加载、`on_*` 钩子失败时用 |

## 16.3 框架怎么反馈

| 出错位置 | 谁收到 | 说明 |
|---------|--------|------|
| 命令处理函数抛异常 | 触发命令的人 | 群聊里 **@ 他**，私聊直接回；带「分类 + 短原因 + 编号」|
| 事件处理器抛异常 | 触发事件的人 | 同上（定时器/通知类事件没有触发者时转超管）|
| 定时任务抛异常 | 超管私聊 | 没有「触发者」可回 |
| 插件加载 / 启用 / 卸载 / 重载失败 | 超管私聊 | 群里不暴露内部故障细节 |
| 回复发送失败（断线等）| 超管私聊 | 触发者此刻联系不上，只能让运维知道 |

文案形如：

```
⚠️ 执行 /aichat ask 时出错（E-3f21）：PluginTimeoutError: 上游 120s 没响应（可以稍后再试）
```

**编号**由「插件 + 分类 + 异常类型」稳定推导（`error_code()`）。用户报编号时，
`grep E-3f21 日志` 就能找到全部同类失败及其堆栈。

**节流**：同一编号在 `error_report.throttle_seconds`（默认 60s）内重复出现时，
只回一句 `⚠️ 又出错了（E-3f21），稍后再试` —— **仍然有回应**（静默才是 bug），
但不会把群刷屏；日志从不节流。

## 16.4 配置（`config.json` 顶层）

```json
"error_report": {
  "enabled": true,          // false = 只写日志，不回任何错误文案
  "throttle_seconds": 60,   // 同一编号的最小重复间隔
  "show_reason": true,      // false = 只给编号不附短原因
  "notify_superusers": true // false = 生命周期故障不私聊超管
}
```

## 16.5 插件作者要守的两条

1. **不要自己 `send_xxx` 拼错误文案** —— 你的插件不会 @ 人、没有编号、不管节流；
   同类失败在群里会以 N 种写法出现。要么抛类型，要么用 `self.errors`。
2. **不要用 `except Exception: pass` 把失败吞掉** —— 用户侧表现就是「有问不答」。
