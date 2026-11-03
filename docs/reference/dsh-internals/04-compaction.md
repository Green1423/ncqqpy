# 4. Compaction

> 文档索引：[docs/README.md](../../README.md) · 上级：[DSH 内部机制](README.md) · 上一章：[3. System prompt composition](03-system-prompt.md) · 下一章：[5. Command registration and dispatch](05-command-dispatch.md)


Two packages cooperate: the seam (`dsh-compaction`, `dsh-compaction-basic`) and an optional
model-free pruner (`dsh-compaction-tool-result-pruner`). The `/compact` command is
`dsh-command-compact`.

## 4.1 Configuration and defaults

`dsh-compaction-basic/lib/index.js:14-17`:

```js
/** Default request-pressure fraction for every routed model. */
const DEFAULT_THRESHOLD_RATIO = .8;
/** Default verbatim-tail fraction for every routed model. */
const DEFAULT_RETAIN_RATIO = .16;
```

`resolveConfig` (`index.js:58-78`) validates keys, rejects `retainRatio` ≥ `thresholdRatio`
(`index.js:134-136`), rejects `retainRatio` + `retainTokens` together and non-integer budgets
(`index.js:160-172`), and produces:

```js
	return deepFreeze({
		thresholdRatio,
		...retention,
		summarizationProvider: config.summarizationProvider ?? "",
		summarizationModel: config.summarizationModel ?? "",
		maxTokens: config.maxTokens ?? 8192,
		compactionRetries: config.compactionRetries ?? 1,
		maxOverflowRetries: config.maxOverflowRetries ?? 1,
		modelPolicies,
		auto: config.auto ?? true
	});
```

Per-model overrides are exact `{provider, model, ...partialPolicy}` entries (`index.js:85-101`),
duplicates rejected (`index.js:144-145`). Budgets are resolved against the **adapter-owned** capacity
(`index.js:108-126`):

```js
	const thresholdTokens = Math.floor(contextWindow * policy.thresholdRatio);
	const retainTokens = policy.retainTokens === void 0 ? Math.floor(contextWindow * policy.retainRatio) : policy.retainTokens;
	if (retainTokens >= thresholdTokens) throw new TargetPressureConfigError(targetKey, `BasicCompactionConfig: ${policy.target.provider}/${policy.target.model} retainTokens (${retainTokens}) must be less than threshold tokens ${thresholdTokens}`);
```

Public config keys (`index.js:19-40`): `thresholdRatio`, `retainRatio`, `retainTokens`,
`summarizationProvider`, `summarizationModel`, `maxTokens`, `compactionRetries`,
`maxOverflowRetries`, plus top-level `modelPolicies` and `auto`.

The shipped base composition mounts `dsh-compaction-basic` with **no config**
(`dsh-base/cordis.patch.yml:317-324`), i.e. 0.8/0.16/8192/1/1/auto, and the pruner with
`thresholdChars: 8192, headChars: 4096, tailChars: 1024` (`:393-398`).

## 4.2 Trigger conditions

Automatic (`dsh-compaction-basic/lib/index.js:793-846`):

* `agent/pre-step` listener → `compactIfNeeded(agent, "pressure", signal)`; failures are logged and
  the turn continues.
* `agent/request-error` listener → only for `failure.code === CONTEXT_WINDOW_EXCEEDED`
  (`CONTEXT_WINDOW_EXCEEDED_CODE = 'CONTEXT_WINDOW_EXCEEDED'`, `dsh-llm/lib/types/error.js:22`);
  bounded by `maxOverflowRetries`; retries only after the surface replacement generation advances
  (`:842-845`).

Pressure gate (`index.js:895-905`):

```js
		const context = (await this.ctx.llm.resolveModelInfo(target.provider, target.model, signal)).context;
		assertNoActiveCompaction(agent.session, "automatic pressure compaction");
		const targetKey = `${target.provider}/${target.model}`;
		if (context === void 0) throw new TargetPressureConfigError(targetKey, `compaction-basic: no context capacity for ${targetKey}; configure contextWindow on that adapter model`);
		const spec = resolveCompactSpec(policy, context.contextWindow);
		if (measurement.totalTokens < spec.thresholdTokens) return null;
		if (prune !== void 0) {
			prune.pruneSession(agent.session);
			measurement = meter.measure(agent.session);
		}
		if (measurement.totalTokens < spec.thresholdTokens) return null;
```

Note the pruner runs **only after** the threshold qualifies, and if pruning alone drops pressure
below threshold, no summary is produced.

Convergence (`index.js:906-919`): a loop of `compactionRetries + 1` attempts, re-measuring after each,
throwing `compaction still above threshold after N compaction attempts` if it never converges.

Overflow path (`index.js:886-893`) bypasses the threshold and the retention budget:

```js
		if (trigger === "context-overflow") {
			if (prune !== void 0) { prune.pruneSession(agent.session); measurement = meter.measure(agent.session); }
			const range = selectCompactableRange(agent.session, measurement, 0);
			if (range === null) return null;
			return this.compactRegion(range.start, range.end, agent, signal);
		}
```

## 4.3 Range selection: exactly what stays verbatim

`dsh-compaction-basic/lib/index.js:379-416` (verbatim, with comments):

```js
function systemHead(session, headSeq) {
	const head = session.eventAt(headSeq);
	return head.type === "system/message" ? head : void 0;
}
function selectCompactableRange(session, measurement, retainTokens) {
	const pricedNodes = measurement.nodes;
	if (pricedNodes.length === 0) return null;
	const surfaceNodes = session.surface.nodes;
	if (surfaceNodes.length !== pricedNodes.length || surfaceNodes.some((seq, index) => seq !== pricedNodes[index]?.seq)) throw new Error("compaction: token-meter surface does not match the current session surface");
	const firstIdx = systemHead(session, surfaceNodes[0]) === void 0 ? 0 : 1;
	let accumulated = 0;
	let keepFromIdx = pricedNodes.length;
	for (let index = pricedNodes.length - 1; index >= 0; index -= 1) {
		accumulated += pricedNodes[index].tokens;
		keepFromIdx = index;
		if (accumulated >= retainTokens) break;
	}
	if (keepFromIdx <= firstIdx) return null;
	while (keepFromIdx > firstIdx) {
		if (toolPairingBalancedBefore(session, surfaceNodes[keepFromIdx])) break;
		keepFromIdx -= 1;
	}
	if (keepFromIdx <= firstIdx) return null;
	return { start: surfaceNodes[firstIdx], end: surfaceNodes[keepFromIdx - 1] };
}
```

Semantics to re-implement exactly:

1. Never include surface node 0 when it is the `system/message` holding the prompt.
2. Walk backwards accumulating route-priced node tokens until the accumulated sum is **≥ retainTokens**;
   that node index is the first kept node. So the retained tail is the smallest suffix whose priced
   size reaches the budget (at least one node whenever the surface is non-empty).
3. Move the cut earlier while the cut would split an assistant `tool-call` from its `tool/result`s
   (`toolPairingBalancedBefore`).
4. Return `null` when nothing is compactable (whole surface is the system head, or one indivisible unit).

Tool-pairing balance (`dsh-compaction/lib/types/tool-pairing.js:8-18, 82-95`):

```js
function eventDelta(event) {
    switch (event.type) {
        case 'assistant/message':
            return event.data.message.content.filter(block => block.type === 'tool-call').length;
        case 'tool/result':
            return -1;
        default:
            return 0;
    }
}
```
with `toolPairingBalancedBefore(session, seq)` = "no unanswered tool call crosses the cut" and
`toolPairingBalancedAfter` the same for the trailing cut. A `tool/result` with balance < 0 throws
("corrupt surface").

Explicit region validation for a caller-supplied span
(`dsh-compaction-basic/lib/index.js:533-549`) additionally requires a balanced boundary before
`start` and after `end`.

## 4.4 The summarization request

`dsh-compaction-basic/lib/index.js:269-317` — a direct one-shot `ctx.llm.stream()` whose prefix is the
conversation's own prefix:

```js
	const latest = agent.session.requestHeader()?.config;
	const target = configuredTarget ?? latest ?? agentTarget;   // summarization pair → latest routed → AgentOptions
	if (target === void 0) throw new Error("no provider/model available for summarization: set both BasicCompactionConfig summarization fields, route one request, or set both AgentOptions fields");
	const messages = [...input.messages, createUserMessage({
		content: [{ type: "text", text: COMPACTION_INSTRUCTION }],
		source: { kind: "plugin", plugin: "dsh-compaction-basic" }
	})];
	const options = { provider: target.provider, model: target.model, messages,
		...input.tools === void 0 ? {} : { tools: [...input.tools] },
		maxTokens: config.maxTokens, sessionId: agent.session.id, purpose: "compaction", signal };
```

Replayed prefix (`index.js:666-675`):

```js
function buildSummarizationInput(session, shadowedSeqs) {
	const header = session.requestHeader();
	const head = systemHead(session, session.surface.nodes[0]);
	const system = head === void 0 ? null : session.deriveEventMessage(head);
	const regionMessages = shadowedSeqs.map((seq) => session.deriveEventMessage(session.eventAt(seq))).filter((message) => message !== null);
	return {
		...header?.tools === void 0 ? {} : { tools: header.tools },
		messages: system === null ? regionMessages : [system, ...regionMessages]
	};
}
```

Only text output is kept; images are a hard failure (`index.js:353-357`):

```js
function summaryText(blocks) {
	if (contentHasImage(blocks)) throw new LlmError("compaction summary cannot contain image output", "UNSUPPORTED_CONTENT");
	return blocks.filter((block) => block.type === "text");
}
```

A `max-tokens` finish is an error, not a partial summary (`index.js:336-352`):
`"summarization truncated at the token cap (incomplete checkpoint)"` with `code = "MAX_TOKENS"`.

## 4.5 The compaction instruction (verbatim, final user message)

`dsh-compaction-basic/lib/index.js:210-255`:

```js
const SUMMARY_OPEN_TAG = "<compacted-summary>";
const SUMMARY_CLOSE_TAG = "</compacted-summary>";
const COMPACTION_INSTRUCTION = [
	"You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.",
	"",
	"Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write \"(none)\" for an empty section — never drop a section.",
	"",
	"## Primary Request and Intent",
	"- [the user's original and evolving goals; quote verbatim where the exact wording matters]",
	"",
	"## Key Technical Concepts",
	"- [technologies, frameworks, patterns, and conventions in play]",
	"",
	"## Files and Code",
	"- [exact path: why it matters, key changes or snippets]",
	"",
	"## Errors and Fixes",
	"- [error: how it was resolved, plus any related user feedback]",
	"",
	"## Pending Jobs",
	"- [explicitly requested work not yet completed]",
	"",
	"## Current Work",
	"- [precisely what was in progress at this checkpoint]",
	"",
	"## Next Step",
	"- [the single next action, directly in line with the most recent request, or \"(none)\"]",
	"",
	"## Critical Context",
	"- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]",
	"",
	"Rules:",
	"- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.",
	"- Capture user feedback and explicit instructions faithfully, especially corrections.",
	"- Do NOT mention this summarization request or that the context was compacted.",
	"- Output only the checkpoint text: do not call any tool or take any other action.",
	`- If the conversation already contains a ${SUMMARY_OPEN_TAG} block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure.`
].join("\n");
```

## 4.6 The checkpoint replacement message

`dsh-compaction-basic/lib/index.js:256-257`:

```js
const CHECKPOINT_PREAMBLE = "This is an automatically generated checkpoint condensing an earlier span of the conversation to free up context. Treat the captured context as established background and build on it without restating it. Continue the task directly from the messages that follow, without acknowledging this checkpoint.";
```

Framing (`index.js:323-335`):

```js
function frameSummary(summary) {
	return [
		{ type: "text", text: `${CHECKPOINT_PREAMBLE}\n\n${SUMMARY_OPEN_TAG}` },
		...summary,
		{ type: "text", text: SUMMARY_CLOSE_TAG }
	];
}
```

So the replacement content is a **three-block array**: preamble+open tag, the summary text block(s),
and the close tag. It is created as `createUserMessage({content: frameSummary(...), source: compactCheckpointSource(...)})`
(`index.js:567-570`) with source (`dsh-compaction/lib/types/checkpoint.js:14-27`):

```js
const COMPACT_CHECKPOINT_MARKER = Object.freeze({ kind: 'plugin', plugin: 'compact' });
export function compactCheckpointSource(compactionId, sourceCommandId) {
    return Object.freeze({ ...COMPACT_CHECKPOINT_MARKER, compactionId,
        ...sourceCommandId === undefined ? {} : { sourceCommandId } });
}
```

## 4.7 Shrink validation

`dsh-compaction-basic/lib/index.js:565-578`:

```js
	const checkpointMessage = createUserMessage({ content: frameSummary(summaryResult.summary), source: compactCheckpointSource(compactionId, sourceCommandId) });
	const framedSummaryTokenCount = dependencies.meter.estimateMessage(checkpointMessage);
	if (framedSummaryTokenCount >= prepared.shadowedRouteTokenCount) throw new Error(`summary is not smaller than the shadowed content (${framedSummaryTokenCount} estimated framed tokens >= ${prepared.shadowedRouteTokenCount})`);
```

with `shadowedRouteTokenCount = selectedNodes.reduce((total, node) => total + node.tokens, 0)` and
`shadowedTokenCount = ... node.heuristicTokens ...` (`index.js:559-560`). A summary that is not
strictly smaller aborts the transaction.

## 4.8 The durable transaction and records

One bracket-first transaction (`index.js:433-505`):

1. validate the span, inspect the durable lock, refuse when another compaction is unmatched or a
   manual call meets an open turn (`:435-445`);
2. `const compactionId = CompactionId(randomUUID())` and
   `const startEvent = session.append("compaction/start", lifecycle)` where
   `lifecycle = { compactionId, sourceCommandId?, turn: owner }` (`:446-452`);
3. await the summary (`:461`);
4. revalidate stability — `whole-surface` for automatic calls, `selected-span` for manual
   (`:453, :580-597`);
5. commit `compaction/summary` + the replacement `user/message` (`:465`, `:599-646`);
6. exactly one `compaction/end` attempt (`:467`); on failure the close carries `error: errorChain(error)`
   (`:478-481`) and a failed close deliberately leaves the unmatched start as a blocking lock.

Commit body (`index.js:599-632`, verbatim field list):

```js
	const summaryEvent = session.append("compaction/summary", {
		compactionId: startEvent.data.compactionId,
		...startEvent.data.sourceCommandId === void 0 ? {} : { sourceCommandId: startEvent.data.sourceCommandId },
		summary,
		...callProvenance,                       // { rawOutput, llmStreamCall: true } for the default path
		shadowedRange: { start, end },
		shadowedSeqs: [...shadowedSeqs],
		shadowedTokenCount,
		provider, model,
		...maxTokens === void 0 ? {} : { maxTokens },
		...usage === void 0 ? {} : { usage }
	});
	session.append("user/message", checkpointMessage, {
		surfaceOp: { op: "replace", startSeq: start, endSeq: end },
		sourceEventSeqs: [startEvent.seq, summaryEvent.seq, ...shadowedSeqs]
	});
```

Event type declarations (`dsh-compaction/lib/types/types.d.ts:14-98`) confirm all three
`compaction/*` events are **log-only** (no `surfaceOp`), and that `shadowedRange` is a *surface
position* span (`:115-127`): "after a prior replace lands a fresh high-seq summary node at an older
range's position, `start` can be GREATER than `end`. `shadowedSeqs` is the authoritative set."

Liveness/lock inspection (`dsh-compaction-basic/lib/index.js:677-705`) scans backwards for the newest
`turn/start` vs `turn/end`, the newest unmatched `compaction/start`, and the newest
`session/end-seed`; an unmatched start older than a later seed boundary is stale and does not block.

Result returned to callers (`checkpoint`/types): `{compactionId, sourceCommandId?, startSeq, summarySeq, endSeq, summary, shadowedRange, shadowedSeqs, shadowedTokenCount}` (`types.d.ts:101-131`).

## 4.9 Manual `/compact`

`dsh-compaction-basic/lib/index.js:944-970` — `compactNow(agent, signal, sourceCommandId)` runs inside
`agent.runMaintenance(...)` (idle admission), selects a range with `retainTokens = 0`
(i.e. compact everything compactable, keeping the trailing balanced unit), and uses
`owner: null, stability: "selected-span"` plus a durability `flush` of the session store. Any
maintenance failure is re-thrown as `ManualCompactionError("busy", ...)`.

`dsh-command-compact/lib/index.js`:

```js
// 9
const USAGE = "Usage: /compact (no arguments)";
// 48-63
async function executeCompact(ctx, invocation) {
	if (invocation.rawInput.trim().length > 0) return { kind: "error", text: USAGE };
	try {
		const result = await ctx.compaction.compactNow(invocation.agent, invocation.signal, invocation.commandId);
		if (result === null) return { kind: "success", text: "No compactable history yet." };
		return { kind: "success", text: `Compacted ${result.shadowedSeqs.length} history items (~${result.shadowedTokenCount} tokens).`, sourceEventSeq: result.summarySeq };
	} catch (error) { ... }
}
// 92-96
		yield ctx.commands.register({
			name: "compact",
			description: "Compact older conversation history",
			handler
		});
```

Human-visible failures (`index.js:17-46`) include
`"Compaction is unavailable because this process has an active compaction, or the agent is not idle."`
(`busy`), `"Compaction cancelled."`, and messages for `changed` / `summary` / `commit` / `persistence`.

## 4.10 The model-free tool-result pruner

`dsh-compaction-tool-result-pruner/lib/index.js:8-14`:

```js
const PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n";
const DEFAULTS = deepFreeze({ thresholdChars: 8192, headChars: 4096, tailChars: 1024 });
```

`pruneContent` (`:91-124`) measures **Unicode code points** across text blocks only
(`measureContent`, `:79-83`), removes the span `[headChars, totalChars - tailChars)`, inserts the
marker once, and validates that the result is smaller and within threshold. Non-text blocks keep
their order and are copied through. `pruneSession` (`:137-194`) walks a snapshot of current surface
nodes, rewrites every over-budget `tool/result` as a replacement `tool/result` event (same `data`
except `content`) preceded by a `compaction/prune` shadow-price event citing the single shadowed node.

## 4.11 Shadow-price protocol (needed for replay-consistent accounting)

`compaction/summary` (and `compaction/prune`) must be **synchronously adjacent** to the surface
`replace` they price (`dsh-compaction/lib/types/types.d.ts:79-98`, and
`dsh-token-meter/lib/types/surface-projection.js:304-336`): the metering event arms a claim
`{start, end, tokens: shadowedTokenCount}`; only the immediately following surface `replace` may
consume it, folding `tokens(new) − claim.tokens`; a range mismatch throws; a claimless replace folds 0.

## 4.12 What the model sees after compaction

The next request receives the checkpoint preamble, a blank line, `<compacted-summary>`, the summary,
`</compacted-summary>`, followed by the retained recent units; the log still holds every shadowed
event. KV-cache effect (`dsh-compaction-basic/README.md`): "Each checkpoint invalidates reuse from the
first replaced history token; the unchanged request prefix before that range remains reusable."

---
