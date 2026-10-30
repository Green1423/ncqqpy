# ai_chat 实现参考

> 文档索引：[docs/README.md](../../README.md) · 上级：[参考文档](../README.md)

这里记录 ai_chat 的**实现细节与开发约定**，面向改代码的人；使用说明见
[ai_chat 手册](https://github.com/Green1423/ncqqpy-plugin-ai-chat/blob/main/docs/ai-chat/README.md)。

| 文档 | 内容 |
|------|------|
| [Markdown 渲染实现](12-markdown-internals.md) | 图片/公式/表格的渲染细节与历史坑 |
| [持久化与并发](17-persistence.md) | 数据目录、写入时机、并发约束 |
| [测试](18-tests.md) | 测试套件与回归基线 |
| [插件包内的 import 纪律](19-imports.md) | 包内导入、跨插件解耦、历史教训 |
| [为框架做的改动](20-framework-changes.md) | 本插件依赖的框架能力与变更记录 |
| [与 DSH 的对应关系](21-dsh-mapping.md) | 概念映射与差异 |
