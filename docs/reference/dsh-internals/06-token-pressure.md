# 6. Token/context pressure estimation

> 文档索引：[docs/README.md](../../README.md) · 上级：[DSH 内部机制](README.md) · 上一章：[5. Command registration and dispatch](05-command-dispatch.md) · 下一章：[7. Checklist for a Python re-implementation](07-python-checklist.md)


The meter is a deterministic replay fold, not a tokenizer.

Constants (`dsh-token-meter/lib/types/estimate.js:8-13`):

```js
const CHARS_PER_TOKEN = 4;
const BLOCK_OVERHEAD = 4;
export const ROLE_OVERHEAD = 4;
```

Pricing (`estimate.js:21-92`, mirrored in `lib/index.js:26-84`):

* `text` / `reasoning`: `ceil(text.length / 4) + 4`
* `tool-call`: `ceil(name.length/4) + ceil(arguments.length/4) + 4` (raw JSON string)
* `tool-result`: recursive `estimateContent(content) + 4`
* unknown blocks **including `image`/`file`**: `4 + ceil(JSON.stringify(block).length / 4)`
* non-system message: `estimateContent(content) + 4`
* system message: `ceil(Σchars/4) + 4`, with non-text blocks counted as `JSON.stringify(block).length`;
  **0** when content is empty and **no** per-block overhead
* tools: 0 when absent/empty, else `4 + ceil(JSON.stringify(header.tools).length / 4)`

`measure(session, requestHeader?)` (`lib/index.js:643-682`) returns a frozen snapshot
`{logRevision, baseline, surfaceDeltaTokens, totalTokens, surfaceTokens, nodes[]}` where
`baseline.kind ∈ {none, estimated, usage}`, `totalTokens: Math.max(0, baseline.tokens +
surfaceDeltaTokens)`, and each node is `{seq, tokens, heuristicTokens}` (`tokens` route-priced,
`heuristicTokens` route-independent for shadow pricing). There is **no ratio/limit/remaining field**:
the consumer computes `thresholdTokens = Math.floor(contextWindow * thresholdRatio)`; `contextWindow`
comes from the adapter (`llm.resolveModelInfo(...).context.contextWindow`) and is relayed in the
`request/context` event. Provider usage is reused only when the latest successful call's canonical
envelope matches and its total is not below the full route-priced estimate of that same call
(`lib/index.js:652-664`); otherwise the whole envelope and surface are estimated. Compaction consumes
exactly this: `meter.measure(session)` (`dsh-compaction-basic/lib/index.js:552, 877`), gate on
`measurement.totalTokens < spec.thresholdTokens` (`:900`), re-measure after prune/compact
(`:903, 916`).

---
