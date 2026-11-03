# 0. Executive summary of the semantics that matter

> 文档索引：[docs/README.md](../../README.md) · 上级：[DSH 内部机制](README.md) · 下一章：[1. Message shape, roles, blocks, and provenance](01-message-shape.md)


1. **There is no user-message envelope.** DSH does *not* wrap user text in `<user_message>` style
   delimiters or add role markers. The projection rule is an explicitly documented verbatim
   pass-through; the comment says so and names the removed feature (`dsh-session/lib/types/surface.js:80-87`).
2. **Wrapping exists only where a producer bakes it into its own content**, e.g. workspace
   instructions in `<system-reminder>…</system-reminder>` (`dsh-agent-instructions/lib/index.js:111-116`,
   `256-266`) and the skill catalog (`dsh-tool-skill/lib/index.js:238-261`).
3. **"Injected context" is distinguished from "the user is asking" by `source`, not by text.**
   `Message.source.kind` is one of `user | plugin | model | tool`, with plugin producers additionally
   declaring a `form` (`instructions | catalog | snapshot | notice | relay | recall`)
   (`dsh-llm/lib/types/message.d.ts:42-108`). Injected context is a **user-role message** with
   `source.kind: 'plugin'`.
4. **Only three roles exist**: `system | user | assistant` (`message.d.ts:124`). Tool results are
   **user-role** messages carrying one `tool-result` block (`message.d.ts:149-153`,
   `lib/types/message.js:85-95`).
5. **Sessions are append-only event logs with contiguous 0-based `seq`**; model history is *derived*
   by folding a "surface" of node seqs, where a surface event is `append` or
   `{op:'replace', startSeq, endSeq}` (`dsh-session/lib/types/types.d.ts:429-433`,
   `dsh-session/lib/index.js:1170-1188`, `1269-1284`).
6. **Compaction replaces an old surface range with one user-role checkpoint message**, does not delete
   the log, and frames the summary as `CHECKPOINT_PREAMBLE + "\n\n" + "<compacted-summary>"` …
   `"</compacted-summary>"` (`dsh-compaction-basic/lib/index.js:210-212, 256-257, 323-335`).
   Defaults: `thresholdRatio = 0.8`, `retainRatio = 0.16`, `maxTokens = 8192`,
   `compactionRetries = 1`, `maxOverflowRetries = 1`, `auto = true`
   (`dsh-compaction-basic/lib/index.js:14-17, 58-77`).

---
