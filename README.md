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
# select the harness, and configure the repository's test/lint commands.
# Commit and push the generated files to the configured base_branch.
factory doctor --harness claude --auth subscription
factory doctor --harness codex --auth subscription

gh issue create --template intent.md
factory run 42 --harness claude --auth subscription
factory status 42
```

Use the new issue's number instead of `42`. Run `factory run 42 --harness codex --auth subscription` to use Codex. Each session uses the selected CLI's default model unless configured otherwise.

Inspect `.factory/issues/42/` in the target repo root for the intent, spec, plan, check logs, prompts, state, findings ledger, and review/fix outputs. These ignored files stay on this machine; `factory/42` contains only build and fix commits. Transcripts are under `.factory/transcripts/` and the code worktree is under `.factory/worktrees/42/`. Keep `.factory/issues/` to resume runs locally; cloning the branch does not restore run state.

The draft PR is opened after build produces code changes. Its body and the final summary comment include the spec and plan in collapsed sections. Before build, gates are reported locally by the CLI and `factory status`.

Exit `0` means complete, `1` means failure (correct it and retry), and `2` means human input is needed. An unchanged parked run makes no model calls.

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
harness's API key (`ANTHROPIC_API_KEY` or `CODEX_API_KEY`), and run `factory poll`
from the target repository. Each invocation makes one pass over eligible issues
with the configured intake label (`factory` by default). Poll uses `factory.toml`
and requires API authentication; saved subscription logins are for attended runs.
