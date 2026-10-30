# 二十、服务范围：只服务名单里的人

> 文档索引：[docs/README.md](../README.md) · 上级：[插件开发](README.md) · 上一章：[渲染](19-render.md) · 下一章：[本地状态与清理](21-persist.md)

插件一旦对外提供服务（AI 对话、资料检索、题库判题、校内通知），第一个要回答的
问题不是「怎么用」，而是**「谁能用」**。答案是白名单：只服务配置里列出的群和
QQ 号，其余一律回绝。

这套判定收在框架的 **`napcat.scope`** 里，四个内置插件（`ai_chat`、`bupt_news`、
`byrdocs`、`oj_bank`）共用同一份实现 —— 自己另写一份的后果是四条边界逐渐不一致：
有的空名单放行、有的拒绝，有的把「机器人监听哪些群」当成服务范围。

## 20.1 四条规矩

| 规矩 | 含义 |
|------|------|
| **只看本插件名单** | 名单来自插件自己的配置（`binding.groups` / `binding.users`）。**不借用**框架的 `bound_groups` —— 那是「机器人监听哪些群」，空表示监听**所有**群，与白名单语义正好相反 |
| **空 = 谁都不用** | 没有「空名单就放开」的开关。要开哪一个，就显式加进来 |
| **分通道判** | 群聊与群小窗按**来源群**判；直接私聊按**用户**判。两条通道互不代替：群开通不等于这个人的私聊开通 |
| **超级用户豁免** | 管理白名单的命令本身只在私聊由超管执行；不豁免就会出现「名单配错 → 谁都改不了」的死锁 |

名单解析只认**正整数**：`bool` 是 `int` 的子类（配置里写成 `users: true` 会被
`int()` 收成「放行 QQ 1」）、0 与负数不是有效号码，一律丢弃。

## 20.2 接进插件

三步：配置模板里留两张名单、构造 `Scope`、在入口处判一次。

```python
from napcat.scope import Scope, parse_ids

class DemoPlugin(PluginBase):
    name = "demo"

    def config_template(self) -> dict:
        return {
            # 空 = 谁都不用；用命令或手改配置显式加
            "binding": {"groups": [], "users": []},
        }

    def _scope(self) -> Scope:
        return Scope.of(self.config.get("binding"))

    def check_access(self, event) -> bool:
        if event is None:
            return True                       # 帮助渲染 / 脚本直调：没有调用者
        uid = int(getattr(event, "user_id", 0) or 0)
        if self.perm.is_superuser(uid):
            return True                       # 防死锁：超管要能进来改名单
        return self._scope().event_ok(event)
```

然后**三处共用**这一个判定，不要各写一份：

| 入口 | 怎么用 |
|------|--------|
| 命令 | 处理函数开头 `if not self.check_access(event): return`，或注册一个可见性过滤器（范围外的人连 `/help` 都看不到这个命名空间） |
| 技能（`register_skill`） | 走的是命令通道，自动继承命令的判定 |
| 工具（`register_tool`） | 在消费侧调用前用同一 `check_access` 判（`ai_chat` 会把上下文交给你） |

## 20.3 判定 API

| 调用 | 返回 | 用途 |
|------|------|------|
| `Scope.of(binding)` | `Scope` | 从 `{"groups": [...], "users": [...]}` 建；不是 dict 时按空名单处理 |
| `scope.group_ok(group_id)` | `bool` | 这个群是否开通（群聊与群小窗都用它） |
| `scope.user_ok(user_id)` | `bool` | 这个人的私聊是否开通 |
| `scope.event_ok(event)` | `bool` | 按事件类型自动选上面两个之一（`None` 事件放行） |
| `parse_ids(raw)` | `set[int]` | 名单解析：只收正整数 |

`event_ok` **不含**超管豁免 —— 那要 `PermissionManager`，由调用方在更外层判。

## 20.4 三种上下文怎么判

| 上下文 | 判据 | 例子 |
|--------|------|------|
| 群聊 | 来源群 ∈ `groups` | 群 `100000003` 在名单里 → 全群可用（不按人授权） |
| 群小窗（临时会话） | **来源群** ∈ `groups`，不看 QQ 号 | 群开通了，该群成员的小窗就能用；换个没开通的群就不行 |
| 直接私聊 | QQ 号 ∈ `users` | 私聊按人开通，框架不管私聊 |

`event_ok` 会替你做这个分流；自己手写时注意：小窗的 `message_type` 也是
`private`，靠 `is_temp_session` 区分，**先判来源群，再判用户**就不会错。

## 20.5 白名单之外的授权：按会话例外

有些能力适合「按会话授权」而不是按人开通 —— 比如 `ai_chat` 的会话所有者可以
把某个会话 `invite` 给不在名单里的人，对方 `attach` 之后就能在**私聊**里用这个
会话。这是唯一允许在名单之外放行的形态，两条限制必须同时成立：

1. **只在私聊生效**：群是公共场所，群里的准入只能看群名单，否则任何人都能
   在群里把人拉进来用；
2. **必须绑定到具体会话**：判据是「此刻的焦点是不是那个被邀请的会话」，
   只是名单上的一个号码不构成任何权限。

落地方式就是 `check_access` 里多一个分支，仍走同一个入口（不要另开一条命令路径）：

```python
if self._scope().user_ok(uid):
    return True, ""
invited = self._invited_session_of(uid)      # 当前焦点是不是「他被邀请」的会话
if invited is not None:
    return True, ""
return False, "私聊没有开通服务（…binding.users…）"
```

## 20.6 名单怎么维护

| 方式 | 说明 |
|------|------|
| 手改 `config.json` | `plugins.<插件名>.binding`（`bupt_news` 用 `targets`，见下） |
| 插件自己的命令 | 如 `/byrdocs add group <群号>`、`/oj bind add user <QQ号>`、`/aichat add user`，写回配置并立即生效 |
| 权限 | 管理命令一律「仅私聊 + 仅超管」，并**豁免**服务范围判定 |

## 20.7 逐对象配置：`bupt_news` 的 `targets`

`bupt_news` 的目标不止「能不能用」，每个群/人还带独立开关：

```jsonc
"targets": {
  "groups": {
    "100000003": {"message": true, "event": true},   // 通知推送 + 活动订阅
    "100000004": {"message": false, "event": true}   // 只订阅活动，不推通知
  },
  "users": {
    "10000001": {"token_file": "tokens/u1.json", "token_by": 10000001}
  }
}
```

判准入时只看这两张表的**键**（群号 / QQ 号）是否在名单里，值的详细配置照旧由
推送、活动订阅、token 归属各自使用 —— 服务范围与业务配置是两层，互不覆盖。

## 20.8 常见坑

- **拿框架 `bound_groups` 兜底**：机器人进了多少群，这个插件就在多少群里可用，
  白名单形同虚设；
- **用 `set() <= 白名单` 判「来源群是否被允许」**：空集合永远满足子集关系，
  字段缺失时会被判成放行 —— 先确认集合非空再比，或直接用 `group_ok()`；
- **只判命令、忘了工具入口**：技能走命令通道没问题，工具是直接调用 handler，
  必须在调用前用同一 `check_access` 判；
- **回绝文案泄露命令内容**：范围外的人看到的应当是「没有开通服务」，
  不该看到命令名或参数。
