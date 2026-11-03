# DSH 内部机制

> 上级：[文档索引](../README.md)

Read-only source analysis of the installed bundle at
`/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/` (packages `0.1.5-rc.3`).
Paths below are relative to that directory unless stated. All line numbers were verified by reading
the files. Where the bundle does not settle a question, this report says so instead of guessing.
Deliverable framing: semantics to re-implement in Python inside a chat-bot framework.
Doc index: [docs/README.md](../../README.md)

## 章节

| 章节 | 小节 |
|------|------|
| [0. Executive summary of the semantics that matter](00-summary.md) | — |
| [1. Message shape, roles, blocks, and provenance](01-message-shape.md) | 1.1 The one shared message representation · 1.2 Content block vocabulary · 1.3 Message provenance: `source` and `form` · 1.4 The explicit "no envelope" rule … |
| [2. Sessions: log, `seq`, surface, identity](02-sessions.md) | 2.1 Event envelope · 2.2 Surface model — how history is *derived*, and what a replacement does · 2.3 Core event map (message-producing and lifecycle) · 2.4 Request envelope records … |
| [3. System prompt composition](03-system-prompt.md) | 3.1 Sections, orders, and rendering · 3.2 The prompt is a system-role *message*, delivered in history · 3.3 Runtime context: a separate user-role snapshot message · 3.4 Assembly interception |
| [4. Compaction](04-compaction.md) | 4.1 Configuration and defaults · 4.2 Trigger conditions · 4.3 Range selection: exactly what stays verbatim · 4.4 The summarization request … |
| [5. Command registration and dispatch](05-command-dispatch.md) | 5.1 Declaration shape · 5.2 Parsing and dispatch |
| [6. Token/context pressure estimation](06-token-pressure.md) | — |
| [7. Checklist for a Python re-implementation](07-python-checklist.md) | — |
| [8. Points the bundle does not settle (stated, not guessed)](08-open-questions.md) | — |
