# 7. Checklist for a Python re-implementation

> 文档索引：[docs/README.md](../../README.md) · 上级：[DSH 内部机制](README.md) · 上一章：[6. Token/context pressure estimation](06-token-pressure.md) · 下一章：[8. Points the bundle does not settle (stated, not guessed)](08-open-questions.md)


**Message shape** — (1) `Message = {id, role ∈ {system,user,assistant}, content: Block[], source}`,
frozen on creation, `id` a fresh UUID. (2) Blocks: `text`, `reasoning`, `image`, `file`,
`tool-call{id,name,arguments:string}`, `tool-result{toolCallId,content:Block[],isError?}`.
(3) Tool results are **user-role** messages with exactly one `tool-result` block, one result per
message so the wire mapping to `role:'tool'` is 1:1. (4) Do **not** add envelope text: distinguish
"the user is asking" from injected context with `source.kind` (+ `form` for plugin content), never with
delimiters. (5) If you do wrap injected content, use the exact producer frames `<system-reminder>` …
`</system-reminder>` (escape an inner close tag as `<\/system-reminder>`), and the
`Current runtime context. This snapshot supersedes earlier runtime-context snapshots.` preamble for
snapshot messages. (6) Text-only user content is joined with `""` and sent as a plain string; empty
tool output becomes `"(no output)"`.

**Sessions** — (7) Append-only log; `seq` 0-based contiguous; `time` = epoch ms; `ignorable?: true`
only for information-free records (unknown non-ignorable types must refuse reconstruction).
(8) Model history = fold over `surface.nodes` via `deriveEventMessage`; `replace` removes shadowed
nodes from the derivation but never from the log; `replaceGeneration` increments. (9) A replacement
must cite every shadowed node in `sourceEventSeqs`; node 0 may only be rewritten by a `system/message`
over exactly that node. (10) Pairing: assistant `tool-call` block id ↔ `tool/call` event ↔
`tool/result` event citing the call seq ↔ `ToolResultBlock.toolCallId`. (11) Keep lifecycle events
(`turn/start|end`, `step/start|end`) in the log but out of the prompt. (12) The prompt is a
`system/message` surface node, never a request-header field.

**System prompt** — (13) Prompt = sections sorted by `order` then code-unit name, non-empty, joined
with `\n\n`; harness identity `"You are an AI agent powered by DeepSeek Harness."` at order −1000;
persona prefix 0, persona suffix 10200; tool guidance 1000–2900; harness source 10000; web 10100.
(14) Dynamic context is a **separate user-role message** carrying the fixed preamble, re-published as
a whole snapshot only when its text changes; clearing publishes
`Current runtime context: none. Earlier runtime-context snapshots no longer apply.`
(15) `{{variable}}` references are strict (unknown/malformed → error); shipped variables `provider`,
`model`, `cwd`.

**Compaction** — (16) Threshold `floor(contextWindow * thresholdRatio)` (default 0.8); retained tail
`floor(contextWindow * retainRatio)` (default 0.16) or absolute `retainTokens`. (17) Select: start at
the first non-`system/message` node; keep the smallest priced suffix with tokens ≥ retainTokens; move
the start earlier to keep tool pairs whole. (18) Replay system prompt + header tools + shadowed-region
messages byte-identically, then append the fixed `COMPACTION_INSTRUCTION` as a final user message with
`purpose: 'compaction'` and `maxTokens: 8192`. (19) Keep only text output, reject images, treat
`max-tokens` as failure, require the framed summary to be strictly smaller than the shadowed
route-priced tokens. (20) Land `compaction/start` → `compaction/summary` → replacement `user/message`
(`{op:'replace',startSeq,endSeq}`, `sourceEventSeqs=[start,summary,...shadowed]`) → `compaction/end`,
with `source: {kind:'plugin', plugin:'compact', compactionId, sourceCommandId?}`. (21) Replacement
content = `preamble + "\n\n" + "<compacted-summary>"` + summary blocks + `"</compacted-summary>"`.
(22) Optional model-free pruning before summarizing, only after the threshold qualifies: > 8192 code
points → keep first 4096 and last 1024, insert `"\n\n[... tool result middle pruned ...]\n\n"`,
shadow-price via `compaction/prune` immediately before the replacement.

**Commands** — (23) Definition `{name, description, input?{hint,attachments?}, recordInput?, handler}`;
name regex `^[a-z][a-z0-9_-]*$`; parse `^\/([a-z][a-z0-9_-]*)(?=$|[\t\n\r ])` with `rawInput` = the
remainder including separator whitespace; log `command/run` / `command/done`; a handler result is
`{kind:'success', text?, sourceEventSeq?}` or `{kind:'error', text}`.

---
