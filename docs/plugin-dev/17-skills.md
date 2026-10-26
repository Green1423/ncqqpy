# 十七、技能（把你的**命令**交给 AI 用）

> 上级：[插件开发](README.md) · 相关：[命令](04-commands.md) · [ai_chat 的工具](../plugins/ai-chat/08-tools.md)

**命令给「人」敲，技能让 AI 也能触发同一条命令。** 技能不是另写一套 handler ——
它只是**一行命令映射**：

```python
async def on_load(self):
    self.register_skill("search", command="byrdocs.search")

    # 想**自己发**结果（而不是让 ai_chat 转发）就再给一个 deliver：
    self.register_skill("notice", command="buptnews.get",
                        deliver=self._deliver_skill_output)
```

| 项 | 来源 |
|---|---|
| 参数 schema | 直接取 `byrdocs.search` 的 **`CommandSchema`**（不另写一份，永不漂移） |
| 说明 | 取命令的 `description` |
| 执行 | 走 **`CommandManager.handle()`**（合成一条真实消息事件） |
| 权限 / 作用域 / 白名单 | **完全按命令判定** —— AI 这条路不绕过任何检查 |
| 回复 | 命令的返回值；**给了 `deliver` 就由插件自己发**（推荐，见 17.2） |
| AI | 不说话、不接收命令输出（委派语义） |
| 边界 | **只用来取数据**。用户已给内容（引用/粘贴）并要求解读、总结、翻译、改写、分析时，AI 应当自己回答 —— 转交后这轮就结束了，没人解释 |

## 17.1 规矩

* **只能映射自己的命令**（`byrdocs.search` 的 owner 是 `byrdocs`）；
  顶层命令（`/ban` 那种）不校验归属 —— 名字上看不出主人；
* **命令不存在就不暴露**：消费方（`ai_chat`）每次按 `find_command()` 解析，
  解析不到或 `disabled` 就跳过，模型看不到死工具；
* **不需要写 handler**：`register_skill()` 返回 `Skill`，不接收函数；
* 卸载/禁用/热重载时框架按插件名整批撤销。

## 17.1a 由谁把结果发给用户

命令的产物可能是 `str`、`MessageChain`、**`list[MessageChain]`**（长通知分段）或
段列表 —— 分段、图片、@ 的规矩只有插件自己知道。所以**推荐插件自己发**：

```python
async def _deliver_skill_output(self, payload, ctx):
    # ctx.user_id / ctx.group_id / ctx.is_group 都在；用你自己的 api 发
    for chain in (payload if isinstance(payload, list) else [payload]):
        ...
        await self.api.send_private_msg(str(ctx.user_id), chain)
```

没给 `deliver` 时会退回 ai_chat 的默认发送（简单输出够用，但猜不出你的分段规则）。
**这个坑实测踩过两次**：chain 列表被当段列表 → 整条通知一个字都没发出去，
用户只收到一句「执行完了，但没有返回内容」。

## 17.2 消费方：ai_chat 侧

`ai_chat` 把每个技能包成一个**委派型工具**（`delegates=True`）：

1. 模型决定调用 → `ai_chat` 合成一条以**当前用户身份**发的消息
   （`/<命令> --键=值`）→ `CommandManager.handle()`；
2. 命令系统按自己的权限/作用域判定：不通过就是「未知命令」口径；
3. 通过 → 执行命令 → 把返回值发给用户；
4. `ai_chat` **结束本轮**，不接话、不把命令输出喂回模型。

配置（`config.json → plugins.ai_chat.skills`）：

```json
{"enabled": true, "allow": [], "deny": ["byrdocs.search"]}
```

## 17.3 调试

```python
self.skills.list()                       # 现在有哪些技能
self.skills.get("byrdocs.search")        # 单个
self.skills.unregister_plugin("byrdocs") # 手工撤（一般不用，框架会做）
```

最直接的端到端验证：在对话里说一句本该由那个插件处理的话（「帮我查查学校的通知」），
看回消息的是不是那个插件（AI 不该额外接话）。

> ⚠️ 技能通道是**框架代码**：换框架版本要**重启进程**才生效（热重载只换插件）。
> 旧进程里插件会打一行警告并跳过技能注册，命令不受影响。
