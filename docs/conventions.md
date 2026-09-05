# Prototype conventions — the contract between the CLI and the role skills

This file pins every concrete decision the design skeleton (`docs/design/01-initial-prototype.md`)
left open, plus the exact invocation facts measured on this machine
(`docs/design/headless-harness-learnings.md`). The CLI and the six role skills are built against
this file. When it and the skeleton disagree, this file wins; when this file is silent, the
skeleton wins.

## 1. Repo layout (this repo, the orchestrator)

```
my-orchestrator/
  pyproject.toml            # package `orch`, python >= 3.11, zero runtime deps; dev dep: pytest
  orch.toml                 # default config (role → runner, caps, runner flags)
  bin/orch                  # `exec uv run --project "<this repo>" orch "$@"` — run from the TARGET repo
  orch/                     # flat package at repo root (repo root == Path(orch.__file__).parent.parent)
    __init__.py
    cli.py                  # argparse: run | step | status
    config.py               # load orch.toml (tomllib), --config / ORCH_CONFIG override
    state.py                # derive_state(scratch_dir, head_sha) -> State  (pure, no subprocess)
    artifacts.py            # readers/writers for run.json, contract.md front matter, audit-n.json, review-n.md front matter, ledger.json
    machine.py              # one transition: step(issue, ctx) ; run loop with caps + stuck detection
    runners.py              # Runner protocol run(role, issue, cwd) -> int ; ClaudeRunner, CodexRunner ; skill linking
    finalize.py             # summary.md assembly ; gh pr edit --body-file ; gh pr ready
    shell.py                # thin subprocess helpers: git(...), gh(...)
  tests/                    # pytest; fake runner; tmp scratch dirs; no network, no real harness
  .agents/skills/orch-*/SKILL.md   # canonical role skills (six directories)
  .claude/skills/orch-*  -> ../../.agents/skills/orch-*   # symlinks, same as the other skills here
  docs/conventions.md       # this file
  docs/design/              # the skeleton and the harness learnings
  README.md                 # operator usage
```

## 2. Role → skill name → invocation

| Role | Skill dir / name | Runner (default) | Claude prompt | Codex prompt |
|---|---|---|---|---|
| Contractor | `orch-contract` | claude | `/orch-contract <issue>` | `$orch-contract <issue>` |
| Implementer | `orch-implement` | claude | `/orch-implement <issue>` | `$orch-implement <issue>` |
| Auditor | `orch-audit` | claude | `/orch-audit <issue>` | `$orch-audit <issue>` |
| Reviewer | `orch-review` | codex | `/orch-review <issue>` | `$orch-review <issue>` |
| Judge | `orch-judge` | codex | `/orch-judge <issue>` | `$orch-judge <issue>` |
| Remediator | `orch-remediate` | claude | `/orch-remediate <issue>` | `$orch-remediate <issue>` |

Role keys used in config and code: `contractor`, `implementer`, `auditor`, `reviewer`, `judge`,
`remediator`. The skeleton's "roles 2 and 6 are one skill with two entrypoints" is realised as two
sibling skill directories; both are small and self-contained.

The **sole argument** to every skill is the issue number. Everything else is read from
`.scratch/<issue>/` and the repo. Every skill runs with cwd = target repo root.

Every skill's frontmatter: `name`, `description`, `disable-model-invocation: true`
(they are only ever dispatched by the CLI or by hand). Each skill dir may also carry
`agents/openai.yaml` mirroring the existing skills (`allow_implicit_invocation: false`).

## 3. Runner invocation (measured on this machine, 2026-09-03)

Environment is inherited unchanged. cwd = target repo root. stdin = `/dev/null`.
stdout is streamed to `.scratch/<issue>/logs/<seq>-<role>.jsonl`, stderr to `...<seq>-<role>.err`
(`seq` = zero-padded 3-digit counter over all invocations of this issue). The runner returns the
process exit code. Outcome is **never** inferred from the exit code alone: the CLI re-derives state
from the scratch directory after every step.

**Claude** (`claude 2.1.260`):

```
claude -p "/orch-<role> <issue>" \
  --output-format stream-json --verbose \
  --permission-mode bypassPermissions --dangerously-skip-permissions \
  --setting-sources project \
  [--model <cfg.model>]
```

- `--setting-sources project` keeps `~/.claude/skills` from shadowing project skills. It also
  drops the user's default model, so `model` is a config key (default `opus`).
- Parse the stream: the `system`/`init` line carries `session_id`, `model`, `skills`
  (assert the role skill is listed; warn loudly if not). Exactly one `result` line ends a healthy
  run: read `is_error` first, then `subtype`, and print `result` text (truncated) to the operator.
  A missing `result` line means the process died mid-run.

**Codex** (`codex-cli 0.152.1`):

```
codex exec --json --sandbox danger-full-access \
  -o .scratch/<issue>/logs/<seq>-<role>.last.md \
  [--model <cfg.model>] \
  '$orch-<role> <issue>'
```

- `danger-full-access` because `gh` (network) is needed by both codex roles; `workspace-write`
  blocks network by default. Config key `sandbox` (default `danger-full-access`).
- Skills resolve from `<target>/.agents/skills/<name>/SKILL.md`.
- Events are snake_case JSONL: `thread.started`, `item.completed` (`agent_message`,
  `command_execution`), `turn.completed` / `turn.failed`. Print the last agent message
  (from the `-o` file) to the operator.

**Skill linking** (runner setup, idempotent, before the first invocation of an `orch` command):
for each role skill, ensure `<target>/.agents/skills/<name>` and `<target>/.claude/skills/<name>`
are symlinks to `<orchestrator>/.agents/skills/<name>` (absolute). If the target *is* the
orchestrator repo, skip. If a path exists and is not a symlink to that target, fail with a clear
message. Append all twelve link paths to `<target>/.git/info/exclude` (so the Implementer never
commits them). Ensure `.scratch/` is a line in `<target>/.gitignore` (create/append; this edit
*is* committed by the Implementer, by design).

## 4. Scratch directory and artifact schemas

`<target>/.scratch/<issue>/` — `<issue>` is the bare integer.

| File | Writer | Notes |
|---|---|---|
| `run.json` | CLI creates; Implementer fills `branch`, `pr_number`, `pr_url` | `{"issue": 17, "branch": null, "pr_number": null, "pr_url": null, "created_at": "<ISO-8601 UTC>"}` |
| `contract.md` | Contractor | frozen once written |
| `clarification.md` | Contractor | terminal alternative to `contract.md` |
| `audit-<n>.json` | Auditor | `n` = 1 + highest existing `n` |
| `review-<n>.md` | Reviewer | `n` = 1 + highest existing `n` |
| `ledger.json` | Judge creates/updates; Remediator marks resolved | cumulative |
| `escalation.md` | CLI (review round cap or audit failure cap) | terminal |
| `summary.md` | CLI finalize | terminal; becomes the PR body |
| `logs/` | CLI | harness stdout/stderr per invocation |

### `contract.md`

```yaml
---
issue: 17
title: "Add percent_change helper"
test_budget: 12
scope_paths: ["src/**", "tests/**"]
commands:
  test: "uv run pytest -q"
  lint: "uv run ruff check ."
  typecheck: "uv run mypy ."
---
## Summary
## Acceptance Criteria
- **AC-1** — <single verifiable statement>. Verified by: `test_name_a`, `test_name_b`
- **AC-2** — ...
## Test Plan
## Non-Goals
```

- `title`: the issue title verbatim, with any embedded `"` escaped as `\"` (the branch slug and
  the PR title derive from it).
- `scope_paths`: glob patterns relative to the repo root; `.scratch/**` and `.gitignore` are always
  implicitly in scope for every role.
- `commands`: run from the repo root via `sh -c`; omit keys the repo lacks; `test` is required.

Front matter is a flat YAML subset the CLI parses without a YAML library: scalar keys
(`issue`, `title`, `test_budget`), one inline JSON-style list (`scope_paths`), and one
one-level mapping (`commands`) whose values are double-quoted strings, with no inline comments.
Skills must write exactly this shape (the parser tolerates trailing `# comments`, skills never
write them).

### `audit-<n>.json`

```json
{
  "pass": false,
  "commit": "<full 40-char HEAD sha>",
  "checks": {
    "commands": {"test": "pass", "lint": "pass", "typecheck": "fail"},
    "criteria_coverage": [{"id": "AC-1", "tests": ["test_login_ok"], "covered": true}],
    "scope": {"pass": true, "out_of_scope_files": []},
    "test_budget": {"budget": 12, "added": 9, "pass": true}
  },
  "failures": ["typecheck failed: src/auth/token.ts:42 ..."]
}
```

`pass` is true iff every command passed, every AC is covered, scope passes, and test budget
passes. Command values: `pass` | `fail` | `skipped` (key absent from contract).

### `review-<n>.md`

```markdown
---
verdict: REQUEST_CHANGES        # or APPROVE
commit: <full 40-char HEAD sha reviewed>
round: 1
base: <sha or "pr">              # what the diff was taken against: "pr" for round 1, previous review commit for n ≥ 2
---
## Findings
### F-1-1
- class: correctness
- location: src/auth/token.ts:42
- evidence: <what was observed / how to reproduce>
- statement: <one sentence>
```

An `APPROVE` review has an empty `## Findings` section. Defect classes: `correctness`,
`contract_violation`, `security`, `data_loss`, `style`, `performance`, `maintainability`,
`test_quality`, `docs`, `other`. Only the first four may block.

### `ledger.json`

```json
{
  "rounds_completed": 1,
  "findings": [
    {
      "id": "F-1-2", "round": 1, "class": "correctness", "location": "src/auth/token.ts:42",
      "summary": "expiry compared in seconds vs ms",
      "disposition": "blocking", "rationale": "violates AC-3; reproducible via evidence in review-1",
      "followup_issue": null, "resolved": false, "resolved_commit": null
    }
  ]
}
```

- `disposition` ∈ `blocking` | `deferred` | `dropped`. Only the four blocking-eligible classes may be
  `blocking`; anything else is `deferred` (or `dropped` with rationale).
- Deferred findings get a GitHub issue (`gh issue create`) whose URL is stored in `followup_issue`.
- **Open blocking finding** := `disposition == "blocking" and resolved == false`.
- The Remediator handles the **first** open blocking finding in array order, then sets
  `resolved: true` and `resolved_commit: <sha>`.
- The Judge sets `rounds_completed` to the round it just adjudicated; the CLI reads it back to
  enforce the review round cap (the Judge never escalates and never sees the config).

### `escalation.md`, `clarification.md`, `summary.md`

Prose. Escalation lists what converged, what did not, open blockers, and a recommendation.
Summary (assembled by the CLI) contains: `Closes #<issue>` as its first line (so merging the PR
closes the issue), then the contract's `## Summary`, the acceptance criteria with
their verifying tests, a ledger table (id, class, disposition, resolved, follow-up link), and a
link list of filed follow-ups. Clarification lists the questions the operator must answer, each
tied to the acceptance criterion it blocks.

## 5. State derivation (CLI, pure function)

Inputs: the scratch dir contents and `head` = `git rev-parse HEAD` of the target checkout.
Evaluate top-down, first match wins — exactly the skeleton's §7 table with these concretions:

- "latest audit older than head commit" := latest `audit-<n>.json` is missing **or** its `commit != head`.
- "no review for this commit" := no `review-<n>.md` exists **or** the latest review's `commit != head`.
- "not yet judged" := `ledger.json` missing **or** `rounds_completed < n` (n = latest review round).
- `IMPLEMENTING` := `contract.md` exists and (`run.json` missing or `pr_number` null).

Before deriving state (for every `run`/`step` transition after `run.json.branch` is set) the CLI
ensures the checkout is on that branch (`git checkout <branch>` if needed); otherwise `head` is
meaningless. `orch status` never touches the checkout: it reads the branch tip with
`git rev-parse <branch>` instead.

## 6. Transitions performed by the CLI

```
CONTRACTING   → ensure run.json; run contractor
IMPLEMENTING  → run implementer
AUDITING      → run auditor; then if the latest audit failed and the trailing run of consecutive
                failed audits ≥ audit_failure_cap → CLI writes escalation.md
REVIEWING     → if next round > review_round_cap → CLI writes escalation.md (backstop; the cap
                normally fires right after JUDGING); else run reviewer
JUDGING       → run judge; then if the ledger's rounds_completed ≥ review_round_cap and an open
                blocking finding remains → CLI writes escalation.md
REMEDIATING   → for each open blocking finding (count taken at entry): run remediator once
FINALIZING    → CLI: write summary.md; gh pr edit <n> --body-file summary.md; gh pr ready <n>
```

**Stuck detection.** After a step, re-derive state. If the state is unchanged *and* HEAD is
unchanged *and* no new artifact file appeared, the loop cannot converge: the CLI writes
`escalation.md` naming the role, its log path, and the role's final message (the harness's
`result` text or codex's last message), and `run` ends in `ESCALATED`. A role that legitimately
declines (a criterion it cannot satisfy) and a harness crash both land here; the operator reads
one file either way, and deletes it to retry after a transient failure.

`orch run` also stops after `max_steps` (config, default 40) transitions.

## 7. Config (`orch.toml`)

```toml
[roles]
contractor = "claude"
implementer = "claude"
auditor = "claude"
reviewer = "codex"
judge = "codex"
remediator = "claude"

[policy]
review_round_cap = 2
audit_failure_cap = 3
max_steps = 40

[runners.claude]
model = "opus"                       # "" → let the harness pick
extra_args = []                      # appended verbatim

[runners.codex]
model = ""                           # "" → user's ~/.codex/config.toml model
sandbox = "danger-full-access"
extra_args = []
```

Resolution order: `--config <path>` → `$ORCH_CONFIG` → `<orchestrator repo>/orch.toml`.

## 8. Git and GitHub rules for skills

- Branch name: `issue-<n>/<slug>`; slug = title lowercased, every run of non-alphanumerics → `-`,
  trimmed of leading/trailing `-`, truncated to 40 chars. Created by the Implementer from
  the contract's `title`, off the repo's default branch (`gh repo view --json defaultBranchRef`).
- Every role that commits (Implementer, Remediator) also pushes (`git push -u origin HEAD`),
  so `gh pr diff` and CI always see HEAD.
- Round-1 review diff = `gh pr diff <pr_number>`. Round-n (n ≥ 2) review diff =
  `git diff <commit of review-(n-1)>..HEAD`.
- Auditor scope and test-budget checks always cover the **whole PR**
  (`gh pr diff <pr_number> --name-only`, `gh pr diff <pr_number>`), never just the delta.
- Commit messages reference the issue (`(#<n>)` suffix). No `Co-Authored-By` trailers are required.
- Only these `gh` capabilities are used: `issue view`, `issue create`, `pr create --draft`,
  `pr view`, `pr diff`, `pr edit`, `pr ready`, `pr checks`, `repo view`.

## 9. Deviations from the skeleton

Each of these was chosen after the first live run or the review pass; the skeleton copy in
`docs/design/` is left verbatim.

- **The CLI, not the Judge, enforces the review round cap** (skeleton §4 row 5, §7 footnote **).
  A skill receives only the issue number and can never see `policy.review_round_cap`, so the
  Judge only adjudicates; after JUDGING the CLI writes `escalation.md` when
  `rounds_completed >= cap` and a blocking finding is still open. Both operator exits are now
  written by mechanical code.
- **The Implementer reads the latest failed audit** (skeleton §4 lists only `contract.md` and the
  repo). Without it, the `AUDITING → IMPLEMENTING` edge for scope/budget/coverage failures had
  no brief and the run stalled as "stuck".
- **Roles 2 and 6 are two sibling skill directories** (`orch-implement`, `orch-remediate`) rather
  than one skill with two entrypoints; both are self-contained.
- **`summary.md` opens with `Closes #<issue>`** so the finalized PR body still closes the issue
  on merge (finalize replaces the whole body the Implementer wrote).
- **`.gitignore` and `.scratch/**` are implicitly in scope for every role.** The CLI's own
  `.scratch/` ignore line, committed by the Implementer, was otherwise raised as a
  `contract_violation` and "fixed" by the Remediator in the first live run.
- **A stuck role escalates rather than aborting.** The skeleton allows exactly two unattended
  exits; a role that runs and changes nothing (second live run: an acceptance criterion that
  depended on an unmerged sibling PR) now ends in `escalation.md` carrying the role's own
  explanation, instead of a bare non-zero exit whose reason lived only in a log.
- **`orch status` never checks out the issue branch**; it reads the branch tip with
  `git rev-parse`. Checking out during `status` switched the working tree under a running
  pipeline in the second live run.
