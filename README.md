# orch — issue → PR orchestrator

`orch` turns a GitHub issue into a pushed branch with a PR that verifiably satisfies it, or stops
cleanly with a clarification request or an escalation. It is a thin state-machine runner: every
role is a skill run in a fresh `claude`/`codex` session that receives only the issue number, and
all state lives in the target repo's `.scratch/<issue>/`.

Design: [`docs/design/01-initial-prototype.md`](docs/design/01-initial-prototype.md).
CLI ↔ skill contract: [`docs/conventions.md`](docs/conventions.md).

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (0.7+) and Python 3.11+
- `gh`, already authenticated (`gh auth status`) for the repo you are working in
- `claude` and `codex` on `PATH`, both signed in
- the target repo is a git checkout with a remote

## Install

```sh
uv sync                                   # in this repo, once
ln -s "$PWD/bin/orch" ~/.local/bin/orch   # or any directory on your PATH
```

`bin/orch` runs this repo's environment while keeping your current directory, so run it from the
**target repo**. Without the symlink:
`uv run --project /path/to/my-orchestrator orch <args>`.

## Use

From a checkout of the repo the issue belongs to:

```sh
orch status 17                     # derived state, HEAD, branch, artifact inventory
orch run 17                        # step until a terminal state
orch run 17 --pause-after-contract # stop after the contract; `orch run 17` resumes
orch step 17                       # exactly one transition (debugging)
orch run 17 --config ./other.toml  # or $ORCH_CONFIG
```

Step-by-step demo walkthrough, with real output and the gotchas: [`docs/demo.md`](docs/demo.md).

A step that changes nothing (a role ran but made no commit, wrote no artifact, and left the state
where it was) ends the run through the escalation exit: `escalation.md` records the role, its log
path, and its final message. That also catches harness crashes, so if the cause was transient,
delete `escalation.md` and run again. `run` exits non-zero only when it hits `max_steps` or errors;
`step` exits non-zero only when the command itself errors. Terminal states exit zero and print the
file that explains them.

## Where things land

Everything is under the target repo's `.scratch/<issue>/` (git-ignored; `orch` adds the entry on
first run):

| File | Written by | Meaning |
|---|---|---|
| `run.json` | CLI, then Implementer | issue, branch, PR number/URL — identifiers only |
| `contract.md` | Contractor | acceptance criteria, scope, test budget, verification commands |
| `clarification.md` | Contractor | **operator exit (clarification)** — the issue cannot be made verifiable |
| `audit-<n>.json` | Auditor | mechanical pass/fail against the contract |
| `review-<n>.md` | Reviewer | `APPROVE` / `REQUEST_CHANGES` plus findings |
| `ledger.json` | Judge, Remediator | every adjudicated finding, cumulative |
| `escalation.md` | CLI | **operator exit (escalation)** — a cap was hit with blockers open |
| `summary.md` | CLI finalize | the PR body (opens with `Closes #<issue>`); its existence means `READY` |
| `logs/<seq>-<role>.jsonl` / `.err` | CLI | raw harness stdout/stderr per invocation |

The PR stays a draft until finalize marks it ready.

## Running a role by hand

Each role is an ordinary skill and takes the issue number as its only argument, so you can run any
step yourself from the target repo and inspect its output file:

```sh
claude "/orch-audit 17"      # contractor | implement | audit | remediate
codex  "\$orch-review 17"     # review | judge
```

`orch` symlinks the six `orch-*` skills into the target repo's `.agents/skills/` and
`.claude/skills/` (excluded via `.git/info/exclude`) on every command.

## Config (`orch.toml`)

Resolved as `--config <path>` → `$ORCH_CONFIG` → this repo's `orch.toml`.

| Key | Default | Meaning |
|---|---|---|
| `roles.<role>` | claude ×4, codex ×2 | which harness runs each role |
| `policy.review_round_cap` | 2 | judged review rounds before the CLI escalates |
| `policy.audit_failure_cap` | 3 | consecutive failed audits before escalation |
| `policy.max_steps` | 40 | transitions per `orch run` |
| `runners.claude.model` | `opus` | `""` lets the harness choose |
| `runners.codex.model` | `""` | `""` uses `~/.codex/config.toml` |
| `runners.codex.sandbox` | `danger-full-access` | `gh` needs network |
| `runners.*.extra_args` | `[]` | appended verbatim to the command line |

## Development

```sh
uv run pytest -q
```
