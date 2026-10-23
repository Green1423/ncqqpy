# 1. Message shape, roles, blocks, and provenance

> 文档索引：[docs/README.md](../../README.md) · 上级：[DSH 内部机制](README.md) · 上一章：[0. Executive summary of the semantics that matter](00-summary.md) · 下一章：[2. Sessions: log, `seq`, surface, identity](02-sessions.md)


## 1.1 The one shared message representation

`dsh-llm/lib/types/message.d.ts`

```ts
// 120-129
export interface Message {
    /** Stable identity preserved across every representation boundary. */
    readonly id: MessageId;
    /** Provider-neutral conversation role. */
    readonly role: 'system' | 'user' | 'assistant';
    /** Exact model-facing blocks. */
    readonly content: ContentBlock[];
    /** Required source fields supplied by the producer. */
    readonly source: MessageSource;
}
```

Specializations (`message.d.ts:131-153`):

```ts
// 135-137
export interface AssistantMessage extends Message {
    readonly role: 'assistant';
    readonly source: ModelMessageSource;
}
// 149-153
export interface ToolResultMessage extends Message {
    readonly role: 'user';
    readonly content: [ToolResultBlock];
    readonly source: ToolMessageSource;
}
```

Note the tuple type: a tool-result message carries **exactly one** `tool-result` block.

Identity: `id` is a branded UUID minted per message; user/assistant/system messages are deep-frozen at
creation. The system prompt is also a message (`SystemMessage`, `message.d.ts:144-147`), with
`source.kind: 'plugin'`; empty content means "no system prompt" and projects to no wire message.

Construction helpers (`dsh-llm/lib/types/message.js`):

```js
// 26-28
export function freezeMessage(message) {
    return deepFreeze(structuredClone(message));
}
// 45-50
export function createUserMessage(input) {
    return createMessage({
        ...input,
        role: 'user',
    });
}
// 73-79
export function createSystemMessage(text, plugin) {
    return createMessage({
        role: 'system',
        content: text.length === 0 ? [] : [{ type: 'text', text }],
        source: { kind: 'plugin', plugin },
    });
}
// 85-95
export function createToolResultMessage(input) {
    return createUserMessage({
        source: { kind: 'tool', callId: input.callId },
        content: [{
                type: 'tool-result',
                toolCallId: input.callId,
                content: input.content,
                isError: input.isError,
            }],
    });
}
```

`createMessage` stamps `id: brandString(randomUUID())` (`message.js:34-39`).

## 1.2 Content block vocabulary

`dsh-llm/lib/types/types.d.ts:38-102` — the block map is merge-extensible, discriminated by `type`:

```ts
// 39-42
export interface TextBlock { type: 'text'; text: string; }
// 44-47
export interface ReasoningBlock { type: 'reasoning'; text: string; }
// 54-58
export interface ImageBlock { type: 'image'; attachment: ImageAttachmentRef; }
// 66-70
export interface FileBlock { type: 'file'; attachment: FileAttachmentRef; }
// 72-79
export interface ToolCallBlock {
    type: 'tool-call';
    /** Provider-issued call id; correlates with the matching tool result. */
    id: ToolCallId;
    name: string;
    /** Raw JSON string as produced by the model. */
    arguments: string;
}
// 81-86
export interface ToolResultBlock {
    type: 'tool-result';
    toolCallId: ToolCallId;
    content: ContentBlock[];
    isError?: boolean;
}
```

Notes for a re-implementation:

* `tool-call.arguments` is the **raw JSON string** as produced by the model, not a parsed object
  (`types.d.ts:77-78`); the session log stores it unparsed (`dsh-session/lib/types/types.d.ts:333-339`).
* `tool-result.content` is *nested* blocks, so results may contain text and images.
* Files never reach a provider natively: request assembly projects every file occurrence to
  deterministic handle text (`dsh-llm/lib/types/content.d.ts`, `fileHandleText`, and the comment at
  `types.d.ts:59-65`). Images are role-neutral in the type but production adapters are text-only for
  assistant output (`types.d.ts:48-53`).

## 1.3 Message provenance: `source` and `form`

```ts
// message.d.ts:94-108
export interface MessageSourceMap {
    user: { kind: 'user'; };
    plugin: { kind: 'plugin'; plugin: string; } & ContextFormed;
    model: ModelMessageSource;
    tool: ToolMessageSource;
}
```

`ContextForm` (`message.d.ts:42-63`) is the *what kind of thing is it* axis, independent of the
*who produced it* axis:

```ts
export type ContextForm =
'instructions' | 'catalog' | 'snapshot' | 'notice' | 'relay' | 'recall';
```

with `snapshot` requiring `sections: ContextSnapshotSection[]` (`name` + `text`, assembly order) and
`notice` requiring a one-line `summary` bounded by `CONTEXT_SUMMARY_MAX_CHARS = 120`
(`message.d.ts:64-84, 110-117`).

Assistant provenance carries the provider route and adapter replay state
(`message.d.ts:7-19`): `AssistantProvenance { provider, model, replayState? }`.

## 1.4 The explicit "no envelope" rule

This is the single most important quote for a re-implementation
(`dsh-session/lib/types/surface.js:75-110`):

```js
export function deriveEventMessage(event) {
    switch (event.type) {
        // Ordinary prompts and injected context project in user role: the event's
        // model-facing content stays verbatim. Do NOT re-add per-type framing
        // (e.g. `<context>`) here: framing is caller-owned — a producer bakes it
        // into `content`, as agent-instructions does with `<system-reminder>` — or,
        // if reintroduced, must be driven by the event `meta` map and a dedicated
        // renderer, keeping this projection a verbatim pass-through. [...]
        case 'user/message': {
            return event.data;
        }
        case 'system/message':
        case 'assistant/message': {
            if (event.data.message.content.length === 0)
                return null;
            return event.data.message;
        }
        case 'tool/result': {
            return event.data.message;
        }
        default:
            return null;
    }
}
```

Consequences a re-implementation must honor:

* A "user turn" on the wire is exactly `{"role":"user","content": <text>}` (see (#16-wire-serialization-what-the-provider-actually-receives)) — no prefix,
  suffix, tag, or role marker added by the projection layer.
* The only framing that appears is embedded in the content string by the producer.
* An empty-content `system/message` or `assistant/message` projects to **no** message at all.

## 1.5 Concrete producer-owned framings (verbatim)

**(a) Workspace instructions** — `dsh-agent-instructions/lib/index.js`:

```js
// 111-116
const SYSTEM_REMINDER_OPEN = "<system-reminder>";
const SYSTEM_REMINDER_CLOSE = "</system-reminder>";
const WORKSPACE_CONTEXT_INTRO = "The following workspace instructions may be relevant to your work. Use them as guidance when applicable. More specific instructions take precedence over broader ones. They do not override system, developer, or direct user instructions.";
const REPLACEMENT_WORKSPACE_CONTEXT_INTRO = "This complete workspace instruction baseline replaces all earlier workspace instruction baselines. The following workspace instructions may be relevant to your work. ...";
const EMPTY_REPLACEMENT_WORKSPACE_CONTEXT_INTRO = "This complete workspace instruction baseline replaces all earlier workspace instruction baselines. No workspace instructions are currently active.";
const COMPACT_WORKSPACE_CONTEXT_INTRO = "Workspace instructions were omitted or truncated to fit the configured byte budget.";
```

```js
// 127-132 — one section's text, and the frame-body escape
function escapeInstructionFrameBody(body) {
	return body.replaceAll(SYSTEM_REMINDER_CLOSE, "<\\/system-reminder>");
}
function sectionText(file) {
	return `Instructions from: ${file.displayPath}\n\n${file.content}`;
}
// 256-266 — the exact message body: open tag \n (marker, intro, sections joined "\n\n") \n close tag
function buildInstructionText(files, maxBytes, omitted, truncated, style) {
	return [
		SYSTEM_REMINDER_OPEN,
		escapeInstructionFrameBody([
			markerText(maxBytes, omitted, truncated),
			style.intro,
			...files.map((file) => style.section(file))
		].filter((block) => block.length > 0).join("\n\n")),
		SYSTEM_REMINDER_CLOSE
	].join("\n");
}
```

Rendered body (an optional budget marker line precedes the intro when files were omitted or truncated,
`markerText`, `index.js:249-255`):

```text
<system-reminder>
<optional budget marker>
<intro>
Instructions from: <displayPath>

<file content>
</system-reminder>
```

Its `source` is either `{kind:'agent-instructions', form:'instructions', changes:[...]}` or
`{kind:'plugin', plugin:'agent-instructions'}` (`index.js:766-795`, `1199-1208`).

**(b) Skill catalog** — `dsh-tool-skill/lib/index.js:238-261`: same `<system-reminder>` frame with a
`<available_skills>` list inside, one entry per line as
`` - `${entry.name}`: ${escapeText(entry.description)} `` (`index.js:293-295`), followed by the
"call the `skill` tool with the exact skill name before taking task actions" paragraph and the
"its `<skill_content>` block then appears in this conversation" paragraph. A re-publish says
`"The available skill catalog changed. This complete catalog replaces every earlier available-skills
list in this session:"` (`index.js:262-286`).

**(c) Repeat-tool reminder** (`dsh-repeat-tool-reminder/lib/index.js:1385-1389`) — a loop-breaker
delivered as a plugin user message with `form:'notice'` and `summary: \`${name} × ${count}\``
(`:1483-1493`) through a tool result's `additionalContexts` (`:1495-1508`); the text begins
`"You are repeating the exact same tool call with identical arguments. …"`.

**(d) Dynamic runtime context** — see (03-system-prompt.md#33-runtime-context-a-separate-user-role-snapshot-message); the text is a *whole-snapshot* replacement, not a delta.

## 1.6 Wire serialization (what the provider actually receives)

`dsh-llm-deepseek/lib/index.js` is the production adapter. Roles are preserved; tool results become
separate `role: 'tool'` messages:

```js
// 126-162
/**
* Serialize the conversation. `tool-result` blocks become standalone
* `{role: 'tool'}` messages; the harness puts each tool result in its own
* user-role message, so a mixed user message contributes its text first and
* its tool results as separate wire messages after.
*/
function serializeMessages(messages) {
	const wire = [];
	for (const message of messages) {
		assertTextOnly(message.content);
		if (message.role === "system") {
			wire.push({ role: "system", content: flattenText(message.content) });
			continue;
		}
		if (message.role === "assistant") { wire.push(serializeAssistant(message)); continue; }
		const toolResults = message.content.filter((block) => block.type === "tool-result");
		const text = flattenText(message.content);
		if (text.length > 0 || toolResults.length === 0) wire.push({ role: "user", content: text });
		for (const result of toolResults) wire.push({
			role: "tool",
			tool_call_id: result.toolCallId,
			content: flattenText(result.content) || "(no output)"
		});
	}
	return wire;
}
```

Assistant serialization (`index.js:107-125`) returns
`{role:'assistant', content: text, reasoning_content?: ..., tool_calls?: [{id, type:'function', function:{name, arguments}}]}`.

Text-only user messages keep the **compact string form** by joining text blocks with `""`
(`index.js:98-106`):

```js
function userContent(parts) {
	const text = [];
	for (const part of parts) {
		if (part.type !== "text") return [...parts];
		text.push(part.text);
	}
	return text.join("");
}
```

Other constants a re-implementation should mirror: empty tool output becomes the literal
`"(no output)"` (`index.js:158, 218`); tool-result images are emitted as part of a **following user
message** whose first text part is `"Attached image(s) from tool result:"` (`index.js:24, 171-225`);
`purpose: 'compaction'` becomes the model-hidden header `x-deepseek-harness-compact: 1`
(`index.js:1667`) while `purpose: 'session-title'` disables thinking (`index.js:32`);
`flattenText(blocks) = blocks.filter(b => b.type==='text').map(b=>b.text).join("")` (`index.js:43-45`);
images in non-user roles are rejected (`index.js:50-53`).

---
