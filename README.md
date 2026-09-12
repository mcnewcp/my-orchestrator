# Software factory v0

A Python CLI that turns a bounded GitHub issue into a draft PR, runs spec → plan → build → review/fix, and marks it ready for human review. Supports Claude Code and Codex; never merges.

## Install and verify

Requires Linux, Python 3.12+, `uv`, Git, `gh`, and a logged-in `claude` or `codex`.

```sh
uv tool install .
make test
make lint
```

## Try it in a target repository

```sh
factory init
# Edit factory.toml: set auth = "subscription" for your saved login,
# select harnesses for the roles, and configure the repository's test/lint commands.
# Commit and push the generated files to the configured base_branch.
factory doctor --auth subscription

gh issue create --template intent.md
factory run 42 --harness claude --auth subscription
factory status 42          # readable summary; add --json for the raw state
```

Use the new issue's number instead of `42`. Run `factory run 42 --auth subscription` to use the configured harness for each role, or add `--harness codex` to use Codex throughout the invocation.

Set defaults in `[factory]` and override individual fields for any of `spec`, `plan`, `build`, `review`, or `fix`:

```toml
[factory]
harness = "claude"
model = ""
effort = ""
auth = "subscription"

[roles.review]
model = "opus"
effort = "high"

[roles.build]
harness = "codex"
effort = "medium"

[roles.fix]
harness = "codex"
```

Each field resolves independently: CLI flag → role setting → factory default → harness CLI default. An omitted role field inherits the factory value; an explicit empty `model` or `effort` uses the harness CLI default and passes no flag. Switching harnesses does not clear an inherited model or effort. Replace the old `[harness.claude]` and `[harness.codex]` tables with these defaults and role settings; the old tables are rejected.

Effort accepts `low`, `medium`, or `high`, plus `max` for Claude. Unsupported effort values, unknown roles, and unknown harnesses fail when loading the configuration. Model names are passed through to the selected harness. Claude receives `--model` and `--effort`; Codex receives `-m` and `-c model_reasoning_effort=<level>`.

`--harness`, `--model`, and `--effort` override their fields for the invocation, including all stages of `run`. For example, `factory run 42 --harness codex --model "" --effort high` uses Codex's default model at high effort. Stage, review-round, and fix-round records under `.factory/issues/42/state.json` retain the settings used; omitted model and effort flags are recorded as `CLI default`.

`factory doctor` probes every distinct harness referenced by the factory defaults or any role and reports each result. It uses factory settings for the default harness and the first role in stage order that uses each additional harness. Add `--harness` to probe one harness, or `--model` and `--effort` to override probe settings. Doctor records are cached by factory version, harness, CLI version, and auth mode.

Inspect `.factory/issues/42/` in the target repo root for the intent, spec, plan, check logs, prompts, state, findings ledger, and review/fix outputs. These ignored files stay on this machine; `factory/42` contains only build and fix commits. Transcripts are under `.factory/transcripts/` and the code worktree is under `.factory/worktrees/42/`. Keep `.factory/issues/` to resume runs locally; cloning the branch does not restore run state.

The draft PR is opened after build produces code changes. Its body and the final summary comment include the spec and plan in collapsed sections. Before build, gates are reported locally by the CLI and `factory status`.

## Reading the output

`factory run` prints one line when a stage starts and one when it finishes, and the same for each check run. Review and fix lines carry their round number:

```text
Issue #42: build start (codex, model default, effort medium, subscription auth)
Issue #42: build done in 3m07s (codex, model default, effort medium)
Issue #42: build checks start
Issue #42: build checks passed in 42.6s
Issue #42: review 2 start (claude, model opus, effort high, subscription auth)
Issue #42: review 2 done in 1m14s (claude, model opus, effort high)
Issue #42: run complete in 12m04s
  PR: https://github.com/you/repo/pull/7
  Artifacts: /home/you/repo/.factory/issues/42/
  Transcripts: /home/you/repo/.factory/transcripts/
```

`model default` and `effort default` mean the harness CLI default was used; `state.json` records the same fact as `CLI default`. A gate prints what stopped the run and the exact next command:

```text
Issue #42 needs human input: open_questions
  The spec asks questions only you can answer.
  Next: Resolve .factory/issues/42/spec.md, then run factory accept 42.
```

A failed check prints its log path, and a failed stage prints how long it ran before dying; its harness transcript and stderr paths follow the error on stderr, each on its own line.

`factory status 42` prints a readable summary: stage progress, the branch, worktree, artifact and transcript locations, the PR link, open findings (id, severity, title), and the next action. `factory status 42 --json` prints the raw local state document, unchanged, for scripts.

Exit `0` means complete, `1` means failure (correct it and retry), and `2` means human input is needed. A failure prints `factory: <what failed>` on stderr, followed by the harness transcript and stderr paths when a model session produced them. An unchanged parked run makes no model calls.

```sh
# Resolve a spec gate:
$EDITOR .factory/issues/42/spec.md
factory accept 42
factory run 42

# Adjudicate a finding or restart a stage after editing its inputs:
factory dismiss 42 F3 "Reason this finding does not apply"
factory build 42 --force

# Remove intake label, close PR, and delete the branch, worktree, and issue artifacts:
factory abandon 42
```

Hand code fixes must be committed inside the issue worktree. Edit spec and plan markdown directly in `.factory/issues/42/`; they need no commit. `--force` on `spec`, `plan`, or `build` rewinds the branch to the previous stage's SHA (the original base for spec) and pushes with an explicit lease. Edited markdown remains available as input to the rerun; spec and plan replace their own generated output. A failed forced stage or force push restores the previous run so you can retry with `--force`.

Repository instructions, checks, CI, configured protected paths, and `.factory/` cannot be changed by agents; fix sessions also cannot change tests. Build reports deviations in its JSON output; only the operator may edit the saved plan.

For label-based intake, set `auth = "api"` in `factory.toml`, supply the configured
harnesses' API keys (`ANTHROPIC_API_KEY` for Claude and `CODEX_API_KEY` for Codex), and run `factory poll`
from the target repository. Each invocation makes one pass over eligible issues
with the configured intake label (`factory` by default). Poll uses `factory.toml`
and requires API authentication; saved subscription logins are for attended runs.
Poll refuses CLI overrides and requires a passing doctor record for every configured
harness at its installed version and API auth mode. If any record is missing or
failed, it runs doctor before discovering issues and stops if a probe fails.
