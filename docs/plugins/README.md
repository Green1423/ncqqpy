# 内置插件索引

各插件的**完整手册在各自插件仓**（见下表「文档」列）；本页只做索引。

| 插件 | 一句话 | 文档 |
|------|--------|------|
| `ai_chat` | 多会话 AI 对话：DSH 式消息包装、上下文压缩、内置工具、Markdown / 图片输出 | [ai_chat 手册](https://github.com/Green1423/ncqqpy-plugin-ai-chat/blob/main/docs/ai-chat/README.md) |
| `bupt_news` | 北邮校内通知同步、免打扰早报、短 ID 检索、DeepSeek 摘要 | [bupt_news 手册](https://github.com/Green1423/ncqqpy-plugin-bupt-news/blob/main/docs/bupt-news/README.md) |
| `byrdocs` | BYR Docs 资料检索与下载：搜索编号 / 翻页 / 取文件 / 白名单 | [byrdocs 手册](https://github.com/Green1423/ncqqpy-plugin-byrdocs/blob/main/docs/byrdocs/README.md) |
| `oj_bank` | 本地题库与判题：洛谷抓题 / AI 搜题入库 / 出图 / 卡人测试点 / 题组 | [oj_bank 手册](https://github.com/Green1423/ncqqpy-oj-bank/blob/main/docs/oj-bank/README.md) |

## 不在本目录的插件

- `plugin_manager`：源码与说明都在插件仓库里，见
  [plugins/plugin_manager/README.md](../../plugins/plugin_manager/README.md)。
- `essential`：没有独立手册，命令行为是插件开发文档里的示例来源，见
  [插件开发](../plugin-dev/README.md)。

## 新写一个内置插件文档

1. 在**插件仓**的 `docs/<slug>/` 下按章节建文件，`README.md` 作为索引；框架仓的
   `docs/plugins/README.md` 只登记一行，链接指向插件仓；
2. 首页写清「一句话 + 命令一览 + 配置段 + 自检命令」；
3. 在 `docs/README.md` 的「内置插件」表里加一行（链接指向插件仓）；
4. `tests/test_docs.py` 会检查索引可达与相对链接。
