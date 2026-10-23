# 2. Sessions: log, `seq`, surface, identity

> 文档索引：[docs/README.md](../../README.md) · 上级：[DSH 内部机制](README.md) · 上一章：[1. Message shape, roles, blocks, and provenance](01-message-shape.md) · 下一章：[3. System prompt composition](03-system-prompt.md)


## 2.1 Event envelope

`dsh-session/lib/types/types.d.ts:448-483`:

```ts
export type SessionEvent<T extends SessionEventType = SessionEventType> = {
    [K in SessionEventType]: {
        type: K;
        /** Monotonic sequence number within the session. */
        seq: SessionSeq;
        /** Unix epoch milliseconds. */
        time: number;
        data: SessionEventMap[K];
        /** ... absent means required: a reader meeting an unrecognized type
         *  without this marker MUST refuse to reconstruct the session ... */
        ignorable?: true;
    } & (K extends SurfaceEventType ? SurfaceIntent<K> : {
        surfaceOp?: never;
        sourceEventSeqs?: never;
    });
}[T];
```

Allocation (`dsh-session/lib/index.js:1182-1188`):

```js
		const event = deepFreeze({
			type,
			seq: SessionSeq(this.log.length),
			time: Date.now(),
			data: dataSnapshot,
			...surfaceMetadataSnapshot
		});
```

So `seq` is **0-based, contiguous, never reused**, and doubles as the log offset. The replay fold
asserts contiguity (`index.js:386`): `session event seq ${event.seq} is not contiguous; expected ${expectedSeq}`.

`ignorable` is a hard compatibility rule: absent means *required*, so an unknown event type without
it must make a reconstruction refuse rather than silently drop (`types.d.ts:468-478`). The bundle
ships 56 known type strings in the generated `KNOWN_SESSION_EVENT_TYPES` set
(`dsh-session/lib/types/known-event-types.js:21-78`), of which only 13 are declared inside
`dsh-session` itself; the rest are `declare module` merges from other packages
(e.g. `compaction/*` from `dsh-compaction/lib/types/types.d.ts:14-99`).

## 2.2 Surface model — how history is *derived*, and what a replacement does

`SurfaceOp` (`types.d.ts:429-433`):

```ts
// dsh-session/lib/types/types.d.ts:429-433
export type SurfaceOp = 'append' | {
    op: 'replace';
    startSeq: SessionSeq;
    endSeq: SessionSeq;
};
```

The four surface event types are exactly
`'system/message' | 'user/message' | 'assistant/message' | 'tool/result'`
(`types.d.ts:413-415`); `surfaceOp` is required on them and forbidden elsewhere
(`dsh-session/lib/index.js:267-281`), `sourceEventSeqs` must be non-empty, duplicate-free, all
strictly earlier, and must cover every shadowed node (`index.js:283-301`), and an
`assistant/message` may never carry it (`index.js:285`).

Commit (`dsh-session/lib/index.js:410-423`):

```js
function applySurfacePlan(state, plan) {
	if (plan?.kind === "append") state.nodes.push(plan.seq);
	else if (plan?.kind === "replace") {
		state.nodes.splice(plan.startIdx, plan.endIdx - plan.startIdx + 1, plan.seq);
		state.replaceGeneration += 1;
	}
	...
}
```

Derivation is a cached fold over `surface.nodes` (`index.js:1269-1284`):

```js
	deriveMessages() {
		const surface = this.surface;
		const nodes = surface.nodes;
		const generation = surface.replaceGeneration;
		if (generation !== this.derivedGeneration) {
			this.derived = [];
			this.derivedNodes = 0;
			this.derivedGeneration = generation;
		}
		for (const seq of nodes.slice(this.derivedNodes)) {
			const msg = this.deriveEventMessage(this.log[seq]);
			if (msg) this.derived.push(msg);
		}
		this.derivedNodes = nodes.length;
		return [...this.derived];
	}
```

**Replaced history is not deleted.** The README states the contract
(`dsh-session/README.md`, "Derived message history"): "A `replace` surface operation removes the
shadowed entries from future inputs without deleting their raw log records." The raw log keeps every
event; only the derived `Message[]` shrinks.

Protections the fold enforces (`index.js:380-403`): the surface node 0 `system/message` may only be
rewritten by another `system/message` covering exactly that node
(`"surface replace: node 0 holds the system prompt and may be rewritten only by a system/message over exactly that node"`);
a replacement must cite every shadowed node via `sourceEventSeqs` (`assertProvenance`); tool-result
rewrites have their own rule (`assertToolResultRewrite`).

## 2.3 Core event map (message-producing and lifecycle)

`dsh-session/lib/types/types.d.ts:242-360` (selected, verbatim field lists):

| Event | Payload | Surface? |
|---|---|---|
| `turn/start` | `{ turn: number }` | no |
| `turn/end` | `{ turn, reason: TurnEndReason }` | no |
| `step/start` | `{ turn, step }` | no |
| `step/end` | `{ turn, step }` | no |
| `user/message` | `UserMessage` (i.e. the whole message: `id, role, content, source`) | yes |
| `system/message` | `{ turn, step, message: SystemMessage }` | yes |
| `assistant/message` | `{ turn, step, message: AssistantMessage, stream, usage?, interrupted? }` | yes |
| `assistant/attempt` | `{ turn, step, stream }` (no surface message) | no |
| `tool/call` | `{ turn, step, callId, name, arguments }` | no (log-only pairing record) |
| `tool/result` | `{ turn, step, message: ToolResultMessage, error?, meta? }` | yes |
| `agent/inbox/spliced` | `{ target, start, removedCount?, inserted, outcome? }` | no |
| `request/header` | `{ header: EpochHeader, reason, startsSeries? }` | no |
| `request/context` | `{ provider, model, contextWindow?, systemPromptUpdate? }` | no |
| `session/title` | `{ title, messageSeqs, source }` | no |

`user/message`'s doc comment is the key disambiguation rule
(`dsh-session/lib/types/types.d.ts:274-281`):

```ts
    /**
     * A user-role message on the model-visible surface: a direct human prompt
     * (the queued message claimed for this turn), a synthetic `agent.inject()`
     * context (file-change notices, subdir AGENTS.md, skill content, cron
     * notifications, …), or an entered goal continuation round. All three
     * project their `content` verbatim; `source` tells them apart.
     */
    'user/message': UserMessage;
```

`tool/call` keeps the raw arguments string (`types.d.ts:328-339`):

```ts
    'tool/call': { turn, step, callId: ToolCallId; name: string; arguments: string; };
```

Turn/step lifecycle as driven by the loop (`dsh-agent-loop/lib/index.js`): `turn/start` at `:926`,
`step/start` at `:951-954`, `step/end` at `:960-963`, `turn/end` at `:994-997`.

Tool pairing in the log (`dsh-agent-loop/lib/index.js:687-713`):

```js
function appendToolCall(session, turn, step, block) {
	return session.append("tool/call", { turn, step, callId: block.id, name: block.name, arguments: block.arguments }).seq;
}
function appendToolResult(session, turn, step, block, result, callSeq) {
	const message = createToolResultMessage({ callId: block.id, content: result.content, isError: result.isError });
	session.append("tool/result", { turn, step, message, ... }, {
		surfaceOp: "append",
		sourceEventSeqs: [callSeq]
	});
}
```

So the result **cites the call event's seq** in `sourceEventSeqs`, and the assistant message carries
the `tool-call` block with the same provider-issued `id`.

## 2.4 Request envelope records

`EpochHeader` (`dsh-session/lib/types/types.d.ts:208-215`):

```ts
export interface EpochHeader {
    config: LlmCallConfig;                 // provider, model, reasoning effort, sampling scalars
    adapterDefaults?: LlmCallConfigAdapterDefaults;
    tools?: ToolSchema[];                  // absent for a tool-less request
}
```

`request/header` reasons (`types.d.ts:227-235`): `'initial' | 'resume' | 'change' | 'series'`.
The loop logs headers only on change/series (`dsh-agent-loop/lib/index.js:1182-1196`) and a
`request/context` when provider/model/contextWindow/systemPromptUpdate change (`:1197-1202`).

**The system prompt is never in the header**: the fold rejects `header.system`
(`dsh-session/lib/types/surface.js:130-131`): `throw new Error(\`${subject} must omit header.system; use system/message\`)`.

## 2.5 Inbox: `followup` / `steer` / `inject`

`dsh-agent/lib/README.md` (Use this package): "`followup()` queues an ordinary next-turn prompt and
wakes the driver; `steer()` submits next-step input and wakes it; `inject()` adds model-facing
context without waking the driver, so it lands in the next admitted step."

Implementation (`dsh-agent-loop/lib/index.js:783-797`):

```js
	send(message, target, wakeup) {
		const wakingAfterAbort = wakeup && this.phase.kind !== "idle" && this.phase.abort.signal.aborted;
		const resolvedTarget = wakingAfterAbort ? "next-turn" : target;
		this.inbox.splice(resolvedTarget, Infinity, 0, [message]);
		if (wakeup) this.wakeDriver(wakingAfterAbort);
	}
	followup(input) { this.send(input, "next-turn", true); }
	steer(input) { this.send(input, "next-step", true); }
	inject(input) { this.send(input, "next-step", false); }
```

At the step boundary the claimed batch (plus the dynamic runtime-context snapshot) is committed
**before** the model call (`dsh-agent-loop/lib/index.js:1019-1029`):

```js
			for (const { message, intent } of commits) this.session.append("system/message", { turn, step, message }, intent);
			if (firstAttempt) for (const message of decision.messages) this.session.append("user/message", message, { surfaceOp: "append" });
```

Tool-provided `additionalContexts` are re-queued into `next-step` (`:578`, `:1118`), i.e. they become
regular user-role messages in the next model call, never part of the tool result itself.

The request itself is built from the derived surface (`dsh-agent-loop/lib/index.js:1204-1218`):

```js
		const boundaryMessages = session.deriveMessages();
		...
		return markAgentLoopRequest(Object.freeze({
			...header.config,
			messages: boundaryMessages,
			...header.tools !== void 0 ? { tools: header.tools } : {},
			sessionId: this.session.id,
			signal
		}));
```

`GenerateOptions` (`dsh-llm/lib/types/types.d.ts:404-444`) has `provider`, `model`,
`reasoningEffort?`, `messages`, `system?` (one-shot callers only; loop requests leave it undefined),
`tools?`, `temperature?`, `maxTokens?`, `stop?`, `signal?`, `sessionId?`,
`purpose?: 'compaction' | 'session-title'`.

## 2.6 Session identity and naming

* `SessionId` is a branded string (`dsh-session/lib/types/types.d.ts:4-11`). `SessionStore.prepare()`
  auto-mints `session-${++counter}` (first auto id `session-1`) when the caller omits one
  (`dsh-session/lib/index.js:1374-1380`); the API layer mints `session-${randomUUID()}`
  (`dsh-api-session-controller/lib/index.js:573, 692`), and subagent children share that shape.
* `SessionHeader` (`types.d.ts:56-99`): `version` (current `SESSION_FORMAT_VERSION = 3`, `types.d.ts:54`),
  `id`, `createdAt` (epoch ms), optional `cwd`, `parentSession`, `isSeeded`, `origin: 'subagent'`,
  `delegationDepth`, `agentPreset`. There is **no title field**.
* Titles are a separate log-driven projection: a latest-wins `session/title` event
  `{ title, messageSeqs: SessionSeq[], source: {kind:'fallback'} | {kind:'provider', provider, model?} | {kind:'user'} }`
  (`dsh-session-title/lib/types/types.d.ts:23-41`; user rename at `lib/index.js:298-310`), with
  configured byte/word bounds (`fallbackMaxWords: 5, fallbackMaxBytes: 40, maxTitleBytes: 80` in the
  shipped base composition, `dsh-base/cordis.patch.yml:60-64`).
* On disk (JSONL persistence): one file per session named `session.jsonl` for generation 0 and
  `session.v<N>.jsonl` afterwards; the canonical pattern is
  `/^session(?:\.v([1-9][0-9]*))?\.jsonl$/u`
  (`dsh-session-format/lib/index.js:462-486`). The file is **one JSON header line followed by one
  JSON event per line** (`dsh-session-persistence-jsonl/lib/index.js:3015-3023`):
  `const header = JSON.stringify(toHeaderLine(meta, ...)) + "\n";` then
  `const body = eventLines(events) + "\n";` with
  `eventLines = events.map(eventLine).join("\n")` and `eventLine = JSON.stringify(...encodeEvent(event))`
  (`:946, 954`). A reader rejects a first line that is not a session header
  (`"corrupt session log: first line is not a JSON object"`, `:977-989`).
* `dsh-session-projection` is a *registry of derived state units* (todos, goals, token
  usage/pressure/breakdown), each `{key, stateSchema, stateVersion, init, apply, wire?}` applied
  synchronously to committed events; `snapshot(session)` returns `{ asOfSeq, values }`
  (`dsh-session-projection/README.md`). It does not carry conversation messages.

---
