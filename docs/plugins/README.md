# 内置插件手册

| 插件 | 一句话 | 文档 |
|------|--------|------|
| `ai_chat` | 多会话 AI 对话：DSH 式消息包装、上下文压缩、内置工具、Markdown / 图片输出 | [ai_chat 手册](ai-chat/README.md) |
| `bupt_news` | 北邮校内通知同步、免打扰早报、短 ID 检索、DeepSeek 摘要 | [bupt_news 手册](bupt-news/README.md) |
| `byrdocs` | BYR Docs 资料检索与下载：搜索编号 / 翻页 / 取文件 / 白名单 | [byrdocs 手册](byrdocs/README.md) |
| `oj_bank` | 本地题库与判题：洛谷抓题 / AI 搜题入库 / 出图 / 卡人测试点 / 题组 | [oj_bank 手册](oj-bank/README.md) |

## 不在本目录的插件

- `plugin_manager`：源码与说明都在插件仓库里，见
  [plugins/plugin_manager/README.md](../../plugins/plugin_manager/README.md)。
- `essential`：没有独立手册，命令行为是插件开发文档里的示例来源，见
  [插件开发](../plugin-dev/README.md)。

## 新写一个内置插件文档

1. 在 `docs/plugins/<插件名>/` 下按章节建文件，`README.md` 作为索引；
2. 首页写清「一句话 + 命令一览 + 配置段 + 自检命令」；
3. 在 `docs/README.md` 的「内置插件」表里加一行；
4. `tests/test_docs.py` 会检查索引可达与相对链接。
