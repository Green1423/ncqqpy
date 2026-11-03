# docstring 规范

本项目的 docstring 同时服务两类读者：**人**（文档站 / 源码）和**编辑器**
（PyCharm、VSCode、Jupyter 的参数提示）。因此统一采用 **PEP 257 + Google 风格**，
中文描述、英文段落标题，让 IDE 能按 `Args` / `Returns` / `Raises` 解析出参数表。

## 基本格式

```python
def send_group_msg(self, group_id: int, message: MessageChain,
                   *, auto_escape: bool = False) -> SendMsgResult:
    """向群聊发送消息。

    一句话摘要写在这里：**动词开头、以句号结尾**，不超过 80 字。

    Args:
        group_id: 目标群号。
        message: 要发送的消息链。
        auto_escape: 是否把消息当纯文本（不解析 CQ 码）。

    Returns:
        发送结果；`message_id` 可用于撤回。

    Raises:
        ApiError: 服务端拒绝或连接未就绪。
    """
```

规则：

1. **摘要行**：第一行是完整的一句话，空一行再写正文；摘要行不超过 80 字。
2. **参数段**：`Args:` 每行 `参数名: 说明。`，参数名与签名一致（不含 `self`/`cls`）；
   可选参数写明默认行为（「默认 …」「不给时 …」）。
3. **返回值段**：`Returns:` 写返回类型语义；无返回值写 `None`（或省略）。
4. **异常段**：`Raises:` 每行 `异常类型: 触发条件。`，只写**调用方能处理**的异常。
5. **属性段**：数据类 / 配置类用 `Attributes:` 描述字段，类级 docstring 里写。
6. 段落标题固定用英文：`Args` `Returns` `Raises` `Attributes` `Example` `Note`；
   顺序固定为摘要 → 正文 → Args → Returns → Raises → Attributes → Example/Note。
7. 不要把实现细节塞进公开 docstring（那是注释 / 设计文档的事）；公开 API 的
   docstring 只承诺**行为契约**。
8. 私有函数（`_name`）可以只写一行摘要，不必凑 Args。
9. 模块（文件）开头必须有模块 docstring：一行说明职责 + 可选的「边界/约定」。
10. 行宽 ≤ 88；中文标点用全角，段落间空一行。

## 编辑器能读到什么

- PyCharm / VSCode 的悬浮提示会把 `Args:` 渲染成参数表；`Returns:` 渲染成返回说明；
- `Ctrl+Q` / `Hover` 只显示**摘要 + 段落**，不会执行代码，所以摘要必须自解释；
- 类型以**签名注解**为准，`Args:` 里不重复写类型，避免漂移；
- 不要用 reStructuredText 的 `:param x:`（编辑器不识别，和 Google 风格混用会只显示一半）。

## 什么时候必须写全

| 场景 | 要求 |
|------|------|
| `napcat/` 公开类 / 方法 / 函数 | 摘要 + `Args`（有参数时）+ `Returns`（有返回值时） |
| 插件的 `on_load` / `on_enable` 等生命周期钩子 | 摘要即可（参数由框架注入） |
| 插件命令处理函数 | 摘要 + 说明 `packet` 约定（schema 见 `CommandSchema`） |
| 私有工具函数 | 一行摘要 |

## 检查

`tests/test_docstrings.py` 会检查：

- 每个模块有模块 docstring；
- 公开类/函数/方法有 docstring，且摘要行长度合规；
- docstring 里的段落标题白名单合法、顺序正确；
- 核心公开 API（清单见测试里的 `CORE_API`）带 `Args:` 且参数名与签名一一对应。

新增公开 API 时把它加进 `CORE_API`，否则只有基础检查。
