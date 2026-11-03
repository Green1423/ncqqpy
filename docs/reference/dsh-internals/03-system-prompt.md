# 3. System prompt composition

> 文档索引：[docs/README.md](../../README.md) · 上级：[DSH 内部机制](README.md) · 上一章：[2. Sessions: log, `seq`, surface, identity](02-sessions.md) · 下一章：[4. Compaction](04-compaction.md)


## 3.1 Sections, orders, and rendering

`dsh-system-prompt/lib/index.js:10-42` defines the centrally owned ordering (excerpt):

```js
const SECTION_ORDERS = {
	HARNESS_IDENTITY: -1e3,          // -1000
	DEPLOYMENT_PERSONA_PREFIX: 0,
	PLAN_POLICY: 500,
	TEAM_POLICY: 600,
	PTC_ONLY: 800,
	FILE_REFERENCE: 900,
	TOOL_BASH: 1e3,  TOOL_PWSH: 1010,  TOOL_READ: 1100,  TOOL_WRITE: 1200,  TOOL_EDIT: 1300,
	TOOL_GLOB: 1400,  TOOL_GREP: 1500,  TOOL_JOBS: 1600,  TOOL_PTY: 1700,
	TOOL_WEB_SEARCH: 2e3,  TOOL_WEB_FETCH: 2100,  TOOL_LSP: 2200,  TOOL_SESSION_QUERY: 2300,
	TOOL_GOAL: 2400,  TOOL_CORDIS: 2500,  TOOL_WORKFLOW: 2600,  TOOL_RALPH: 2700,
	TOOL_SUBAGENT: 2800,  TOOL_REPORT: 2900,
	TOOLS_SDK: 5e3,
	DELIVERABLE_FILE_REFERENCES: 9e3,
	STRUCTURED_OUTPUT: 9900,
	HARNESS_SOURCE: 1e4,
	WEB_SURFACE: 10100,
	DEPLOYMENT_PERSONA_SUFFIX: 10200
};
const CONTEXT_ORDERS = { SANDBOX_POLICY: 110, APPROVAL_POLICY: 115, SUBAGENT_DELEGATION: 120 };
```

Rendering (`index.js:111-113`):

```js
function renderPrompt(assembly) {
	return assembly.sections.map((section) => interpolate(section, assembly.variables, "section"))
		.filter((text) => text.length > 0).join("\n\n");
}
```

Sections sort by `order`, ties broken by code-unit name comparison
(`comparePromptSections`, `index.js:96-98`); empty sections are dropped; `{{variable}}` interpolation
throws on malformed/unknown references (`index.js:151-175`). Registered variables in the shipped
loop are `provider`, `model`, `cwd` (`dsh-agent-loop/lib/index.js:1534-1536`).

Fixed identity section, registered by the registry itself unless `includeHarnessIdentity: false`
(`index.js:213-217`):

```js
		if (config.includeHarnessIdentity ?? true) this.section({
			name: "harness:identity",
			order: this.getSectionOrder("HARNESS_IDENTITY"),
			text: "You are an AI agent powered by DeepSeek Harness."
		});
```

Config surface (`index.js:199-205`): `includeHarnessIdentity` (default `true`),
`includeRuntimeContext` (default `true`), `personaPrefix` (default `''`), `personaSuffix` (default `''`),
`toolOrder` (default undefined).

A section may declare `complete: true`; more than one active complete section fails assembly, and the
single complete section then replaces the whole prompt (`index.js:332-357`). The persona row
(`dsh-persona/lib/index.js:23-47`) registers `PERSONA_PREFIX_SECTION` / `PERSONA_SUFFIX_SECTION` for a
scope, with `complete` and `includeRuntimeContext` options — that is the "persona vs instructions"
mechanism: persona is just sections at order 0 and 10200, and agent presets/subagents shadow them by
registering the *same section names* in an agent scope (`dsh-subagent/lib/index.js:549-553`).

## 3.2 The prompt is a system-role *message*, delivered in history

The assembled prompt is committed as a `system/message` surface node, not as a request field
(`dsh-session/README.md`, "Logged request header": "the system prompt is part of `deriveMessages()`
as surface node 0 … Header events add no message to history and hold no copy of the prompt").

Projection logic (`dsh-agent-loop/lib/index.js:241-298`): the first prompt — even empty — reserves
surface node 0; a route declaring `systemPromptUpdate: 'in-history'` may append changed prompts later
in the series; otherwise the loop normalizes by rewriting node 0 and emptying later active nodes
through logged `replace` operations.

## 3.3 Runtime context: a separate user-role snapshot message

Sections are joined into the system prompt; **contexts are joined into a separate user-role message**
(`dsh-system-prompt/lib/index.js:119-149`):

```js
function joinContextSections(sections) {
	const body = sections.map((section) => section.text).join("\n\n");
	if (body.length === 0) return "";
	return `Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n${body}`;
}
```

The loop projects it as an ordinary user message whose `source` records the named sections
(`dsh-agent-loop/lib/index.js:336-355`):

```js
	project(current, sections) {
		if (this.retained === void 0 && current.length === 0) return;
		const snapshot = current.length === 0 ? CLEARED : current;
		if (this.retained?.text === snapshot) return;
		return createUserMessage({
			content: [{ type: "text", text: snapshot }],
			source: sections.length === 0 ? { kind: "plugin", plugin: SOURCE } : {
				kind: "plugin", plugin: SOURCE, form: "snapshot", sections
			}
		});
	}
```

with (`dsh-agent-loop/lib/index.js:220-221`):

```js
const SOURCE = "@deepseek-ai/dsh-system-prompt";
const CLEARED = "Current runtime context: none. Earlier runtime-context snapshots no longer apply.";
```

Only the newest snapshot survives on the surface: `RuntimeContextProjection` tracks the last retained
owned `user/message`, and a replacement that shadows it clears the retained value
(`dsh-agent-loop/lib/index.js:299-329`).

**Is date/time injected?** Not by these packages. The shipped context contributors are sandbox policy
(`SANDBOX_POLICY`), approval policy (`APPROVAL_POLICY`), and subagent delegation
(`SUBAGENT_DELEGATION`) — `dsh-sandbox-policy/lib/index.js:124`, `dsh-user-approval/lib/index.js:82`,
`dsh-subagent/lib/index.js:546`. `dsh-time-context` / `dsh-tmux-context` packages exist in the tree, so
a time context is available as a composition choice, but it is not part of the core path analyzed
here. Example shipped context texts (`dsh-sandbox-policy/lib/index.js:72-83`,
`dsh-user-approval/lib/index.js:39-41`): `"Current DSH file policy: workspace-write. Any available
operation enforced by the DSH file sandbox may modify files under the session workspace: …"`,
`"Approval prompts are disabled in this session: actions that require approval are rejected
automatically — do not request sandbox escalation (do not set \`sandbox_permissions\`)."`, and
`"Approval policy: ask. Operations that require approval may ask through the configured answerers;
without an available answerer, the request fails closed."`

## 3.4 Assembly interception

`assemble()` (`dsh-system-prompt/lib/index.js:308-358`) merges scoped layers over globals, sorts
sections, orders tools, then runs the waterfall
`this.ctx.waterfall(scopeTarget(this, scope), "system-prompt/assemble", assembly, context, ...)`,
after which a declared complete section is enforced as the sole section and suppressed runtime
context is emptied.

---
