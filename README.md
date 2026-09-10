# Software factory v0

A Python CLI that turns a bounded GitHub issue into a draft PR, runs spec → plan → build → review/fix, and marks it ready for human review. Supports Claude Code and Codex; never merges.

## Install and verify

Requires Linux, Python 3.12+, Git, `gh`, and a logged-in `claude` or `codex`.

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

Inspect `.factory/worktrees/42/work/42/` for the intent, spec, plan, check logs, prompts, state, and findings ledger; the same files are committed on `factory/42`. Transcripts are under `.factory/transcripts/`.

Exit `0` means complete, `1` means failure (correct it and retry), and `2` means human input is needed. An unchanged parked run makes no model calls.

```sh
# Resolve a spec gate:
$EDITOR .factory/worktrees/42/work/42/spec.md
factory accept 42
factory run 42

# Adjudicate a finding or restart a stage after editing its inputs:
factory dismiss 42 F3 "Reason this finding does not apply"
factory build 42 --force

# Remove intake label, close PR, and delete the factory branch/worktree:
factory abandon 42
```

Hand code fixes must be committed inside the issue worktree. `--force` on `spec`, `plan`, or `build` rewinds the branch and pushes with an explicit lease. Repository instructions, checks, CI, and configured protected paths cannot be changed by agents; fix sessions also cannot change tests.

For unattended API-key runs, see [deployment](docs/deployment.md). See [validation notes](docs/validation.md) for the tested scope and live-run evidence.
