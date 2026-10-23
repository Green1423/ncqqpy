# 5. Command registration and dispatch

> 文档索引：[docs/README.md](../../README.md) · 上级：[DSH 内部机制](README.md) · 上一章：[4. Compaction](04-compaction.md) · 下一章：[6. Token/context pressure estimation](06-token-pressure.md)


## 5.1 Declaration shape

```ts
// dsh-commands/lib/types/index.d.ts:37-52
export interface CommandDefinition {
    readonly name: string;              // lowercase, no leading slash
    readonly description: string;       // human-readable summary used in discovery UI
    readonly input?: CommandInputDescriptor;   // { hint: string; attachments?: boolean }
    readonly recordInput?: boolean;
    /** Execute against the receiving agent without sending the command to the model. */
    readonly handler: (invocation: CommandInvocation) => CommandResult | Promise<CommandResult>;
}
// :18-35 — the invocation the handler receives
export interface CommandInvocation {
    readonly commandId: CommandId;   // already written to this invocation's `command/run`
    readonly agent: Agent;
    /** Exact text following the registered command name, including separator whitespace. */
    readonly rawInput: string;
    readonly attachments: readonly (ImageBlock | FileBlock)[];
    readonly signal: AbortSignal;
}
// types.d.ts:33-41 — the result union
export type CommandResult =
  | { readonly kind: 'success'; readonly text?: string; readonly sourceEventSeq?: SessionSeq; }
  | { readonly kind: 'error'; readonly text: string; };
```

The registration-time definition is normalized to **exactly** `{name, description, input?, recordInput?,
handler}` (`index.js:158-164`), so there is no `usage`, `aliases`, typed-argument schema, `scope`,
`permission`, or `availability` field — an extra key is silently dropped, not rejected. Name grammar
(`index.js:71`):

```js
const COMMAND_NAME = /^[a-z][a-z0-9_-]*$/u;
```

Validation (`index.js:142-157`) requires a matching name, non-empty trimmed description, a function
handler, and (when present) a non-empty `input.hint` plus a boolean `attachments`. Handler results are
validated at the registry boundary (`normalizeResult`, `index.js:176-194`): error `text` must be a
non-empty string; `sourceEventSeq`, when present, must be a non-negative safe integer naming an
**earlier** event (`dsh-commands/lib/invariant.js:25-27`).

**Scope is not a field and there is no permission layer.** Scope comes from *where* the plugin is
mounted: `register()` inserts into the calling context's layer, and a scoped layer shadows a global
command of the same name (`index.js:81-83, 257-259, 414-416`; nearest wins via `dsh-scope` merge).
The `/compact` handler implements usage itself by rejecting non-empty `rawInput`
(`dsh-command-compact/lib/index.js:9, 49-52`).

## 5.2 Parsing and dispatch

```js
// dsh-commands/lib/index.js:89-105
function parseCommand(line) {
	const match = /^\/([a-z][a-z0-9_-]*)(?=$|[\t\n\r ])/u.exec(line);
	if (match === null) return void 0;
	const name = match[1];
	if (name === void 0) return void 0;
	return Object.freeze({ name, rawInput: line.slice(match[0].length) });
}
```

Note: `rawInput` keeps everything after the name **including the separating whitespace**, and a
command must be terminated by end-of-line or whitespace (`/compactx` is not a command).

Execution (`index.js:318-386`): parse → resolve (`this.view(agent).get(name)`) → `undefined` for
unknown names (nothing logged) → mint `commandId` → append `command/run` → enforce attachment
admission → build the frozen invocation `{commandId, agent, rawInput, attachments, signal}` → run the
handler under `withAbort` → normalize → append `command/done`.

Lifecycle events (`types.d.ts:87-117`):

```ts
        'command/run': { commandId: CommandId; name: string; args?: string; source: CommandSource; };
        'command/done': { commandId: CommandId; kind: 'success' | 'error'; text?: string;
            sourceEventSeq?: SessionSeq; };
```

"Both are direct log-only appends — no turn wraps them" (`index.js:293-297`); `args` is omitted when
the definition sets `recordInput: false` (`index.js:328`); the pairing id is
`cmd-${instanceToken}-${commandSeq}` (`index.js:400-403`). Namespacing: names are flat; scope
shadowing plus within-layer duplicate rejection is the only collision policy. **Admission is silent**:
malformed syntax or an unknown name returns `undefined`, logs nothing, and produces no error object
(`index.js:319-322`); the *adapter* decides the user-facing string — the shipped Web adapter renders
`` `unknown or malformed command: ${line}` `` (`dsh-client-ui-commands/lib/client.js:795-808`). A
**thrown** handler is logged as a `command/done` error and then rethrown, not converted into an error
result (`index.js:377-398`).

---
