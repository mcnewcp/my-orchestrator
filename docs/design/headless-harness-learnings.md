# Running Claude Code and Codex headless — a field guide

## Where this came from

Two first-hand evidence sources:

1. **Contract research** — both CLIs' non-interactive surfaces read against their source and
   official docs. Every claim is either an observed command output or a cited primary doc.
2. **A live dual-harness experiment** — one Python driver took both harnesses through four
   milestones: measure context occupancy, stop at an arbitrary token count, have the interrupted
   session write a handoff document, and chain that document into a fresh session with no human in
   the loop. The workload was a fixed read-only payload repeated in *cycles* to grow context. Both
   harnesses ran on subscription auth, with API-key environment variables actively unset.

Everything below is measured unless flagged otherwise. Versions are at the bottom — both CLIs move
fast, so re-verify flags before you depend on them.

**The one thing to know first:** each harness has *two* headless surfaces, and they are not
interchangeable. One-shot is simple but blind; persistent is the only way to watch context and
interrupt. The experiment used the persistent surface on both sides.

| | one-shot | persistent (what the experiment used) |
| --- | --- | --- |
| Claude Code | `claude -p` | `claude -p --input-format stream-json`, or the Python `claude-agent-sdk` |
| Codex | `codex exec` | `codex app-server --stdio` (JSON-RPC over stdin/stdout) |

---

## 1. Invoking

**Claude Code**

```bash
claude -p "<prompt>" --output-format stream-json --verbose \
  --session-id "$SID" --model sonnet --permission-mode acceptEdits < /dev/null
```

- `--output-format json` is a strictly weaker channel — no per-message usage, no live signal. Use
  `stream-json`.
- Always redirect stdin (`< /dev/null`) or every call stalls 3s on a stdin warning. stdin caps at 10MB.
- `--session-id <uuid>` presets the ID *before* launch (Codex can't do this). Resume with
  `--resume <uuid>`, never `--continue` (directory-scoped, ambiguous under concurrency).
- Exit codes: `0` success, `1` error, `143` after SIGTERM. See §Gotchas — none of them mean the work got done.
- The experiment drove the SDK in-process instead: `async with ClaudeSDKClient(options=options)`,
  `await client.query(p)`, `async for m in client.receive_response()`. Note `get_context_usage()`,
  `interrupt()` and `set_permission_mode()` are **not** SDK-native — they're one-line
  `control_request` writes to the child's stdin, and were driven successfully against the bare CLI.

**Codex**

```bash
codex exec --json --sandbox workspace-write -o /tmp/final.md "<prompt>"
```

- Approvals are already off under `exec`; no approval flag needed. Hard-exits outside a git repo
  unless `--skip-git-repo-check`. `--full-auto` is **gone** from the source (docs lag).
- Exit is strictly binary, and reports *harness* health, not task outcome — interrupted and failed
  share code `1`.
- The persistent path is `codex app-server --stdio`, then `initialize` → `initialized` →
  `thread/start {cwd, approvalPolicy, sandbox, ephemeral}` → `turn/start {threadId, input}`.
  This is where live token usage and `turn/interrupt` live. It is documented as **experimental**.

**Auth (both):** the experiment was subscription-only, no API keys — it hard-fails if
`ANTHROPIC_API_KEY` or `CODEX_API_KEY` is set. Verify out-of-band with `claude auth status --json`
(expect `apiProvider: firstParty`, `apiKeySource: null`) and Codex `account/read` (expect
`type: "chatgpt"`). Note the Claude Agent SDK docs tell integrators to use an API key or cloud
credentials and do not promise the subscription path; it worked, but it isn't a guaranteed contract.

---

## 2. Invoking skills

**Hand the harness the skill's own invocation form as the prompt** — `/my-skill 17` on Claude Code,
`$my-skill 17` on Codex. The harness expands the skill into context before the model acts. Arguments
survive intact.

Do **not** ask in prose ("use the implement skill"). Measured over six dispatches on Claude Code
2.1.237: the slash-command-as-prompt loads the skill regardless of the skill's
`disable-model-invocation` setting; the prose form isn't even listed to the model when that flag is
on, and is merely *optional* when it's off. When we relied on model choice in real dispatches, both
runs were refused the skill and improvised the work instead.

Discovery paths:

| | root | notes |
| --- | --- | --- |
| Codex | `.agents/skills/<name>/SKILL.md` | literally Codex's own repo skill root; `skills/list` enumerates; a `turn/start` input can also carry a `{type:"skill", name, path}` item, which is the deterministic injection |
| Claude Code | `.claude/skills/<name>/SKILL.md` | entries may be symlinks → one tree can serve both harnesses |

- Pass `--setting-sources project` on Claude Code: a user's personal `~/.claude/skills/` silently
  shadows project skills of the same name. Skill names are not globally unique on either harness
  (Codex has the same collision via `$HOME/.agents/skills`), so **prefix your project's skills**.
- Codex reads `AGENTS.md` and never `CLAUDE.md`. Keeping `.agents/skills/` + `AGENTS.md` canonical,
  with `.claude/skills/` + `CLAUDE.md` as symlinks, means one tree serves both with no translation.
- `SKILL.md` frontmatter needs `name` and `description` on both. Codex ignores unknown keys silently
  rather than rejecting them, so Claude-only keys are harmless there.
- ⚠️ **This section is measured CLI behaviour, not a documented contract, and it is the least-proven
  part of this document.** We never dispatched a real project skill end to end — the experiment
  deliberately loaded *zero* skills (`setting_sources=[]` on Claude, `baseInstructions: ""` and
  `ephemeral: true` on Codex) so that protocol differences, not task differences, dominated the
  evidence. Verify skill dispatch first on any new project. If expansion ever regresses, the fallback
  is prose instruction with `disable-model-invocation: false` / `allow_implicit_invocation: true`
  already set on the skill — set them anyway, since a skill that composes another *by name* does
  consult them, and a wrong flag there fails silently.

---

## 3. Monitoring a running session

**Context occupancy — the load-bearing signal**

| | signal | cadence |
| --- | --- | --- |
| Claude Code | `get_context_usage()` → `totalTokens` (control request `{"subtype":"get_context_usage"}`) | on demand, **including mid-turn** (187ms round trip, measured) |
| Codex | notification `thread/tokenUsage/updated` → `params.tokenUsage.last.totalTokens` | completed-turn only |

Both crossed a 200,000-token trip point before any compaction (Codex 200,921 on cycle 21 in a
258,400 window; Claude 210,041 on cycle 15 in a 1,000,000 window).

- **Never use cumulative billing as occupancy.** Codex `total.totalTokens` crossed 200k on cycle 5
  and ended at 2,345,801 while real occupancy was 200,921. Same trap on the Claude side.
- Claude's zero-cost passive fallback: `input_tokens + cache_creation_input_tokens +
  cache_read_input_tokens` per assistant message *is* current context — but dedupe by `message.id`
  (the SDK emits two identical envelopes per API iteration; `ResultMessage.usage["iterations"]` is
  authoritative).
- Read `autoCompactThreshold` from the payload, don't hard-code it — trigger below it or the harness
  compacts first and you react to a summary. (Observed 934,000 on sonnet-5, 967,000 on opus-5[1m];
  the invariant is a 33,000-token gap below the window, not the number.)
- Codex has **no** compaction-threshold signal and no mid-turn query at all. Its persisted rollout
  JSONL is a fallback only — no schema promise makes its on-disk layout an API, and we never needed it.

**Progress and completion**

- Claude stream-json: `system/init` (enumerates model, tools, skills, and `capabilities` —
  feature-detect interrupt support there, not by version string), then `assistant` / `user` events,
  then exactly one terminal `result`. Missing `result` line = died partway. `system/thinking_tokens`
  is a decoy: it counts thinking-block tokens only, not context.
- Codex app-server: `item/completed` notifications per item. **Read tool activity from the live
  stream, not from `turn/completed`'s `turn.items` — that snapshot is lossy** (it dropped a
  successful `commandExecution` and produced a false "the write never happened").
- Correlate Codex notifications with `params.turnId or params.turn.id` — different notifications put
  it in different places. And drain your client-side notification backlog before reading the socket,
  or the terminal event is already buffered where you aren't looking.

**Interrupting**

| | how | terminal boundary |
| --- | --- | --- |
| Claude Code | `interrupt` control request / `await client.interrupt()` (persistent client only) | drain `receive_response()` through exactly one `ResultMessage`, `terminal_reason=aborted_streaming` |
| Codex | `turn/interrupt {threadId, turnId}` → returns `{}` | the matching `turn/completed` with `status: "interrupted"` |

The acknowledgement is **never** the terminal event on either side — keep draining. Claude's
interrupted result arrives as `is_error: true, subtype: error_during_execution`; that is the
representation of a *requested* cancellation, not a failure.

Signals differ: Claude handles **SIGTERM** (exit 143, transcript survives, `--resume` picks up
partial work). Codex registers **SIGINT only** — `Popen.terminate()` kills it without the graceful
path.

Overshoot is real and must not be designed away: at a 50,000-token stop target, Codex overshot by
8,775 (its trigger is completed-turn granular), Claude by 4,794.

**Chaining across a context limit.** Neither harness has a first-class handoff primitive. What works:
interrupt, drain to the terminal event, then ask the *same* session for one more turn that writes a
handoff document; validate that file; then start a genuinely fresh session (Codex `thread/start`,
Claude a new client with no `resume`) and embed the validated bytes into its first prompt. No resume
API is involved. Two costs to budget for: the handoff turn itself consumed 3,130 tokens of context on
Codex and 1,904 on Claude, so leave headroom below your stop point; and the handoff prompt must be a
single line (JSON-escape the content into it).

---

## Gotchas that bit us

1. **Exit code never proves work happened, on either harness.** Claude exits `0` with
   `is_error: false` and `permission_denials: []` when a needed tool was unavailable — the failure
   exists only as English prose in `result`. Codex exits `0` when the model tries and fails. Derive
   outcome from the event stream and from observing the repo.
2. **Branch on `is_error` before `subtype`.** An invalid model gave `subtype: "success"` *and*
   `is_error: true` with a 404.
3. `permission_denials` is contested in our own evidence: one design note calls it the only
   mechanical way to detect a hobbled dispatch, while the hands-on research could never populate it —
   it stayed `[]` even on a deliberately blocked-tool run. Re-verify before relying on it.
4. Claude allowlists decompose compound Bash commands and reject shell variables and command
   substitution outright; `rm` is path-guarded independently of the allowlist, which once left an
   agent unable to delete a directory it had just created outside the sandbox.
5. Every non-`--bare` Claude run pays a ~33k prompt-token floor before any work.
6. A `-p` session shows no workspace-trust dialog — it will run a project's `.claude/settings.json`
   hooks and connect its `.mcp.json` servers in a folder never trusted.
7. `total_cost_usd` is list price, not spend. Codex reports no cost at all — five token counters,
   no dollars — so any budget must be token-based.
8. Codex background-terminal cleanup (`thread/backgroundTerminals/list|clean|terminate`) is
   **experimental-only** and requires `experimentalApi: true` at initialize.
9. Codex `exec` uses snake_case item types (`agent_message`), the app-server camelCase
   (`agentMessage`). Codex is a fast-moving alpha and its generated schemas are specific to the CLI
   version that produced them — re-verify flags and re-digest schemas per version.

## What we never proved

- **Real project-skill dispatch, on either harness.** See the warning in §2.
- Claude background quiescence before a handoff — whether processes a turn launched are actually gone
  is unresolved. Codex can prove it, but only via the experimental API.
- Behaviour at either harness's automatic-compaction boundary; no compaction was ever observed, and
  the experiment inferred it heuristically (a drop ≥10,000 tokens *and* ≥20% of the prior reading).
- Occupancy cadence for a *tool-using* session — everything above was measured on a no-tool,
  one-request-per-turn read-only workload.
- Context-quality degradation. 200,000 tokens was a chosen trip point, not a calibrated one.

## Environment the evidence came from

Codex `codex-cli 0.149.1`, ChatGPT Plus, `gpt-5.6-sol` @ xhigh reasoning, 258,400-token window.
Claude `claude-agent-sdk 0.2.144` (bundles CLI 2.1.239; standalone CLI 2.1.243–2.1.246 also present),
`claude-opus-5[1m]`, 1,000,000-token window, auto-compact at 967,000. macOS arm64, Python 3.13.5.
Neither harness's persistent configuration was changed. Note the SDK wheel bundles its own Claude
Code binary (~90MB) and runs *that*, not the user's install, unless you override `cli_path`.
