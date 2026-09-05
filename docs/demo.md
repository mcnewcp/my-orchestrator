# Demo: issue to PR on the sandbox repo

Take a ready-made issue in `mcnewcp/orch-sandbox`, run one command, and end with a pushed branch and
a non-draft PR whose body was assembled from the contract and the review ledger.

Budget: 4 to 9 harness invocations on the default caps (more when audits fail), 4 to 9 minutes of
wall clock, real tokens on every one of them.
Architecture lives in [`README.md`](../README.md) and [`docs/conventions.md`](conventions.md); this
file is the sequence and the gotchas.

Every command below is copy-pasteable from the `orch-sandbox` clone step 0 puts you in. All log
excerpts are real output from earlier runs, trimmed at `...`.

---

## 0. One-time setup and preflight

Install `orch` (skip if the first line prints a path):

```sh
command -v orch || (
  cd ~/code/my-orchestrator &&
  uv sync &&
  mkdir -p ~/.local/bin &&
  ln -sf "$PWD/bin/orch" ~/.local/bin/orch
)
```

If `command -v orch` still prints nothing afterwards, `~/.local/bin` is not on your `PATH`: see
step 8. `bin/orch` is a `uv run --project <orchestrator>` wrapper that keeps your current directory,
so it must be run from the **target** repo, never from `my-orchestrator`.

Clone the sandbox and stay in it for the rest of this guide. The repo is private, so `gh` auth is
what gets you in; every sample path below assumes this location:

```sh
[ -d ~/code/orch-sandbox ] || gh repo clone mcnewcp/orch-sandbox ~/code/orch-sandbox
cd ~/code/orch-sandbox
```

The guard matters: `gh repo clone` refuses a directory that already exists and is not empty
(`fatal: destination path '...' already exists and is not an empty directory`, exit 1). If you
already have this clone, make sure no `orch run` is in flight in it before you go on, because the
preflight below moves the checkout back to `main` and moving HEAD under a running role derails the
run (step 3).

Preflight, from the clone:

```sh
command -v orch claude codex uv gh jq   # six paths, one per line
gh auth status                          # logged in to github.com
git switch main && git pull --ff-only
git status --porcelain                  # prints nothing
```

`command -v` proves the binaries are on `PATH`, not that they are signed in. A signed-out `claude`
or `codex` still burns a real invocation, changes nothing, and the run ends in an escalation
(step 8), so confirm both harnesses are logged in before you start.

Two things worth knowing before you spend tokens:

- **Billing.** `orch` hands the environment to `claude` and `codex` unchanged. In your own terminal
  that means subscription auth. Inside a Claude Code session `ANTHROPIC_API_KEY` and
  `OPENAI_API_KEY` are set, so a run launched from there bills the API keys instead. Demo from a
  plain terminal, or `env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u CODEX_API_KEY orch run 8`.
- **Clean main.** `orch run` checks out the issue branch inside this checkout as soon as
  `run.json` carries one, so start from a clean `main`. The one file that will not stay clean is
  `.gitignore`: see step 2.

---

## 1. Choosing the issue

```sh
gh issue list --state all   # 8, 7, 4, 3, 1 in this sandbox
gh issue view 8             # the spec you are about to buy a contract for
```

**Ready to go: [orch-sandbox#8](https://github.com/mcnewcp/orch-sandbox/issues/8)**, "Add
weighted_mean(values, weights) to sandbox.calc". One helper in `src/sandbox/calc.py`, with a worked
example (`weighted_mean([1, 2, 3], [1, 1, 2]) == 2.25`), agreement with `mean` when the weights are
equal, and `ValueError` on a length mismatch or a zero total weight; it names the test file and the
two commands that must pass.

### Writing your own

The Contractor's bar (`.agents/skills/orch-contract/SKILL.md`, section 4): a criterion is
**verifiable** when a test at a named seam observes it, pass or fail, with no human judgement.
Three things fail that bar and produce a clarification instead of a contract:

- an outcome with no observable threshold ("make it faster", "a cleaner API")
- a choice the issue leaves open (which format, which default, which of two behaviours at an edge)
- a dependency on a fact that is in neither the repo nor the issue (an external contract, a
  credential, a decision the operator owns)

So write the exact signature, worked examples with literal expected values, the error type and the
condition that raises it, which files take the code and the tests, and the commands that must pass.
The Contractor also sets a `test_budget` (roughly 2 tests per acceptance criterion, floor 4, ceiling
20) that the Auditor enforces, so keep one issue to one behaviour.

### An issue that will come back as a clarification

[orch-sandbox#3](https://github.com/mcnewcp/orch-sandbox/issues/3), "Make the calc helpers faster
and nicer": no threshold for "faster", no chosen signature for "nicer". Run it if you want to see
the clarification exit (about 1 minute, 1 invocation) instead of a full run.

---

## 2. Look before you run: `orch status 8`

```sh
orch status 8
```

```
[orch] added .scratch/ to /Users/you/code/orch-sandbox/.gitignore
[orch] linked 12 role skill path(s) into /Users/you/code/orch-sandbox
issue    8
repo     /Users/you/code/orch-sandbox
scratch  /Users/you/code/orch-sandbox/.scratch/8
state    CONTRACTING
head     9a806966b9136e3cebb50093afb0fffc52856e93
branch   main
artifacts:
  (none)
```

`[orch] ...` lines go to stderr; the report goes to stdout. Exit code 0.

The two `[orch]` lines are the **side effects of the first `orch` command in any repo** (they run on
`run`, `step`, and `status` alike, and are idempotent afterwards):

- `.scratch/` is appended to the target's `.gitignore`. That leaves `git status` showing
  ` M .gitignore`, and the Implementer commits that line **by design** (`docs/conventions.md`
  sections 3 and 9): `.gitignore` and `.scratch/**` are implicitly in scope for every role, so the
  Judge drops any finding raised about it.
- the six role skills are symlinked into `.agents/skills/` and `.claude/skills/` (12 paths, both
  harnesses) pointing at `~/code/my-orchestrator/.agents/skills/orch-*`, and all 12 are appended to
  `.git/info/exclude` so the Implementer never commits them.

`orch status` **never touches the checkout**: once `run.json` names a branch it reads that branch's
tip with `git rev-parse` rather than checking it out, which is what makes it safe to run from a
second terminal while a run is in flight.

---

## 3. The run: `orch run 8`

Two things to decide before you press enter, both detailed below: `--pause-after-contract` stops
after the contract so you can read it before paying for the implementation, and this command blocks
for 4 to 8 minutes, so open a second terminal now if you want to watch it.

```sh
orch run 8
```

Role by role, from the real `orch run 7` demo run (trimmed):

```
[orch] claude: orch-contract 7 -> .scratch/7/logs/001-contractor.jsonl
[orch] session=de4ff984-738c-4469-92b7-89a591fbe1ce model=claude-opus-5
[orch] result is_error=False subtype=success :: Wrote `.scratch/7/contract.md`. No clarification needed ...
[orch] claude: orch-implement 7 -> .scratch/7/logs/002-implementer.jsonl
[orch] session=f6e36b74-2630-4990-9138-40f9c2ae4060 model=claude-opus-5
[orch] result is_error=False subtype=success :: Implementation complete for issue #7. **Draft PR:** https://github.com/mcnewcp/orch-sandbox/pull/9 on `issue-7/add-median-values-to-sandbox-calc` ...
[orch] claude: orch-audit 7 -> .scratch/7/logs/003-auditor.jsonl
[orch] session=65e958fb-114e-4d17-afac-ace4e5b3b85e model=claude-opus-5
[orch] result is_error=False subtype=success :: **Audit passed** ...
[orch] codex: orch-review 7 -> .scratch/7/logs/004-reviewer.jsonl
[orch] $ /bin/zsh -lc 'cat .scratch/7/contract.md'
[orch] $ /bin/zsh -lc 'git rev-parse HEAD'
[orch] $ /bin/zsh -lc 'gh pr diff 9'
[orch] final message :: Review complete: **APPROVE**, with no findings. Wrote [review-1.md](...). All 14 tests and Ruff checks passed.
READY: https://github.com/mcnewcp/orch-sandbox/pull/9
```

The two harnesses report differently, and that is the whole difference on screen: `claude` roles
print an `init` line (`session=`, `model=`) and one `result` line; `codex` roles print one `$` line
per command they execute and their final message at the end. The last stdout line is the terminal
state and the file or URL that explains it. When the run ends, your checkout is sitting on the issue
branch, not on `main`.

Measured per role (issues #1, #4, #7 in this sandbox):

| Step | Role | Harness | Writes | Typical |
|---|---|---|---|---|
| 1 | Contractor | claude | `contract.md` or `clarification.md` | 45 to 75 s |
| 2 | Implementer | claude | branch, commits, draft PR, `run.json` ids | 80 to 115 s |
| 3 | Auditor | claude | `audit-<n>.json` | 30 to 40 s |
| 4 | Reviewer | codex | `review-<n>.md` | 60 to 80 s |
| 5 | Judge | codex | `ledger.json`, follow-up issues | ~55 s |
| 6 | Remediator | claude | one commit per blocking finding, ledger update | 40 to 55 s (one measured run) |
| 7 | (CLI) | none | `summary.md`, PR body, PR out of draft | seconds |

An `APPROVE` in round 1 finishes in 4 invocations and about 4 to 5 minutes (issues #4 and #7). A
`REQUEST_CHANGES` adds judge, remediator, auditor, reviewer: 8 invocations and about 8 minutes
(issue #1).

### Sanity-check the contract before paying for the implementation

```sh
orch run 8 --pause-after-contract
```

```
paused after CONTRACTING in state IMPLEMENTING; run `orch run` again to resume
```

Exit code 0. The flag only pauses a run that writes the contract itself: once `contract.md` exists,
`orch run 8 --pause-after-contract` ignores it and runs the whole pipeline, so resume with plain
`orch run 8` rather than re-issuing the paused command. Read `.scratch/8/contract.md`: front matter
(`test_budget`, `scope_paths`, `commands`) plus Summary, Acceptance Criteria with their
`Verified by:` test names, Test Plan, Non-Goals. Then:

- happy with it: `orch run 8` resumes from `IMPLEMENTING`.
- not happy: the contract is **frozen** once written, so `rm .scratch/8/contract.md`, fix the issue
  text, and run again. Editing it by hand works too; keep the front-matter shape exactly (the CLI
  parses it without a YAML library).

### Watching from a second terminal

Safe to run in the same clone while a run is in flight (none of these touch the checkout):

```sh
orch status 8                                   # derived state and artifact inventory
ls .scratch/8/logs | tail -2                    # which role is running now
jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="text") | .text' \
  .scratch/8/logs/003-auditor.jsonl | tail -5   # a claude role, as it thinks
cat .scratch/8/logs/004-reviewer.last.md        # a codex role's final message
gh pr view "$(sed -n 's/.*"pr_number": \([0-9]*\).*/\1/p' .scratch/8/run.json)" \
  --json number,isDraft,url
```

Do **not** run `orch run`, `orch step`, `git checkout`, or anything else that moves HEAD in the
second terminal: state is derived from HEAD, and moving it under a running role sends the run back
to `AUDITING` at best.

---

## 4. Reading the result

Everything is in `.scratch/8/` (git-ignored):

| File | Written by | What it tells you |
|---|---|---|
| `run.json` | CLI, then Implementer | issue, branch, `pr_number`, `pr_url` |
| `contract.md` | Contractor | the definition of done every later role was measured against |
| `audit-<n>.json` | Auditor | mechanical result: commands, criterion coverage, scope, test budget, plus `failures` |
| `review-<n>.md` | Reviewer | `verdict: APPROVE` or `REQUEST_CHANGES`, the reviewed `commit`, and findings with class, location, evidence |
| `ledger.json` | Judge, Remediator | every finding ever raised, its disposition (`blocking`/`deferred`/`dropped`), its follow-up issue, whether it is resolved |
| `summary.md` | CLI finalize | the PR body; its existence is what makes the state `READY` |
| `logs/<seq>-<role>.jsonl` / `.err` / `.last.md` | CLI (`.last.md`: codex, via `-o`) | raw harness stdout, stderr, and (codex only) final message |

```sh
orch status 8
```

Real output from the issue #4 run (paths are your clone's):

```
issue    4
repo     /Users/you/code/orch-sandbox
scratch  /Users/you/code/orch-sandbox/.scratch/4
state    READY
head     456400815bf9b3f347ac699e1bc79445e0967377
branch   issue-4/add-clamp-value-low-high-to-sandbox-calc
artifacts:
  run.json           branch=issue-4/add-clamp-value-low-high-to-sandbox-calc pr=6
  contract.md        test_budget=8 scope=src/sandbox/calc.py,tests/test_calc.py
  audit-1.json       pass commit=4564008
  review-1.md        APPROVE commit=4564008
  summary.md         present
  logs/              9 files
```

The audit and review lines carry the commit they were taken at; when one of those does not match
`head`, that gate has to run again. That is the whole state machine in one screen.

On GitHub:

```sh
gh pr view --web        # you are on the issue branch, so this is the run's PR
```

The body was assembled by the CLI (`orch/finalize.py`), not by a model: `Closes #8` on the first
line, then the contract's title, Summary and Acceptance Criteria, a ledger table (finding, class,
disposition, resolved, follow-up), the list of follow-up issues, and a footer naming the two files
it came from. Then `gh pr edit --body-file` and `gh pr ready`: **the PR is a draft for the whole run
and only finalize flips it**, so a draft PR plus a terminal state that is not `READY` means the run
stopped early.

Deferred findings become real issues, filed by the Judge with `gh issue create`. `ledger.json`
exists only once a review round has been judged, so an `APPROVE` in round 1 leaves none, hence the
guard:

```sh
[ -f .scratch/8/ledger.json ] &&
  jq -r '.findings[] | select(.disposition=="deferred") | .followup_issue' .scratch/8/ledger.json
gh issue list --state all
```

---

## 5. The three endings

All three exit **0** and print the file or URL that explains them. `orch run` exits non-zero only on
`max_steps` (`aborted (max_steps) in state ...`) or a raised error (`orch: <message>`).

### READY

```
READY: https://github.com/mcnewcp/orch-sandbox/pull/9
```

Read `.scratch/8/summary.md` (the PR body) and the PR itself. Nothing is left to resume: review it
like any PR and merge or close it.

### NEEDS_CLARIFICATION

```
NEEDS_CLARIFICATION: /Users/you/code/orch-sandbox/.scratch/8/clarification.md
```

Written by the Contractor instead of a contract, one section per blocked criterion, each ending in a
question. From the real issue #3 run (trimmed):

```
## Blocked: "the API is a bit awkward" ... clean up the interface so it is nicer to use
Why it cannot be verified as written: "nicer" is a judgement with no observable behaviour attached,
and the issue leaves the actual design choice open ...
Question: Which exact signatures should `add` and `mean` have after this PR, and may those changes
break the current public `add(a, b)` / `mean(list)` API, or must the existing calls keep working?
```

To resume: answer every question **on the issue** (the Contractor re-reads it with
`gh issue view <n> --comments`), delete the file, run again.

```sh
gh issue comment 3 --body 'AC: keep add(a, b) and mean(list) as they are. ...'
rm .scratch/3/clarification.md
orch run 3
```

### ESCALATED

```
[orch] wrote /Users/you/code/orch-sandbox/.scratch/8/escalation.md
[orch] escalated: REVIEWING did not advance after running reviewer
ESCALATED: /Users/you/code/orch-sandbox/.scratch/8/escalation.md
```

`escalation.md` is written by the CLI, never by a role. It lists what converged (contract, branch,
PR), what did not, the open blocking findings, and a recommendation. Four ways to land here:

| Cause | Trigger |
|---|---|
| Audit failure cap | `audit_failure_cap` (3) consecutive failed audits |
| Review round cap | `review_round_cap` (2) rounds adjudicated with a blocking finding still open |
| Review round backstop | on entry to `REVIEWING`, the next round number would exceed `review_round_cap`, blockers or not |
| Stuck step | a role ran and changed nothing: no commit, no new artifact, no state change |

The stuck case is the common one, and it also catches harness crashes. It quotes the role's own
final message and its log path under `## What did not converge`, above the open blocking findings
and the recommendation, which is where you find out whether the role declined on purpose or died:

```sh
cat .scratch/8/escalation.md
```

- **Transient** (crash, network, a harness that died mid-run): `rm .scratch/8/escalation.md && orch run 8`.
  The run picks up from the derived state; nothing else needs undoing. That is a real retry only for
  the stuck case: after a **cap** escalation the counters live in the artifacts the recipe leaves
  behind (`audit-<n>.json`, `ledger.json`), so the run pays for one more round and escalates again.
  Raise the cap through `--config` (step 9) or clear the blocker by hand first.
- **Real** (a criterion nothing can satisfy, a blocker the Remediator cannot clear): fix it by hand
  on the branch, or amend the contract's acceptance criteria and delete the stale
  `audit-<n>.json` / `review-<n>.md` you want re-run, or close the PR and rewrite the issue. Either
  way, finish with `rm .scratch/8/escalation.md` before running again: `ESCALATED` is derived from
  that file's existence alone, so nothing resumes while it is there.

---

## 6. One transition at a time, and roles by hand

```sh
orch step 8
```

Exactly one transition, with the harness stream and the actions on stderr and the transition on
stdout (trimmed):

```
[orch] claude: orch-contract 8 -> .scratch/8/logs/001-contractor.jsonl
[orch] session=... model=claude-opus-5
[orch] result is_error=False subtype=success :: Wrote `.scratch/8/contract.md` ...
[orch] ran contractor (exit 0)
CONTRACTING -> IMPLEMENTING
```

A terminal step prints a second line (`READY: <pr_url>`). One `step` is one **transition**, not one
invocation: `REMEDIATING` runs the Remediator once per open blocking finding within the single step.
`step` exits non-zero only when the command itself errors. The caps still fire inside a step (they
are checked right after the auditor and the judge, plus a backstop on entry to `REVIEWING`), but
**stuck detection lives only in the `run` loop**, so a role that changes nothing under `step` just
leaves you in the same state with no `escalation.md`.

### Running a role by hand

Any role is an ordinary skill whose only argument is the issue number, so you can run one yourself
from the sandbox clone. `README.md` shows the short interactive form for eyeballing a role:

```sh
claude "/orch-audit 8"       # contract | implement | audit | remediate
codex  '$orch-review 8'      # review | judge
```

What the CLI actually runs (`orch/runners.py`, `command()` on each runner) is:

```sh
claude -p "/orch-audit 8" \
  --output-format stream-json --verbose \
  --permission-mode bypassPermissions --dangerously-skip-permissions \
  --setting-sources project \
  --model opus

codex exec --json --sandbox danger-full-access \
  -o .scratch/8/logs/005-reviewer.last.md \
  '$orch-review 8'
```

Reproduce that form when you are debugging what `orch` saw, and the short form when you just want to
watch a role work. Three flags carry real weight:

- `--setting-sources project` is what stops `~/.claude/skills` from shadowing the linked project
  skills. It also drops your default model, which is why `runners.claude.model` exists and defaults
  to `opus` (`--model` is passed only when the config value is non-empty, so codex gets none by
  default and uses `~/.codex/config.toml`).
- `--sandbox danger-full-access` because both codex roles need `gh`, and `workspace-write` blocks
  network.
- `-o` is where codex writes the final message the CLI prints; the CLI names it
  `.scratch/<issue>/logs/<seq>-<role>.last.md`, so pick an unused sequence number by hand.

Run these from the repo root, on the issue branch, and remember that state is derived from files: a
hand-run role that writes an artifact really does advance the machine.

---

## 7. Reset, and cleanup after a demo

To run the same issue again from scratch:

```sh
branch=$(sed -n 's/.*"branch": "\([^"]*\)".*/\1/p' .scratch/8/run.json)
pr=$(sed -n 's/.*"pr_number": \([0-9]*\).*/\1/p' .scratch/8/run.json)
[ -f .scratch/8/ledger.json ] &&
  jq -r '.findings[].followup_issue | select(.)' .scratch/8/ledger.json   # note these first

git switch main
[ -n "$pr" ] && gh pr close "$pr" --delete-branch        # closes the PR, drops the remote branch
[ -n "$branch" ] && git branch -D "$branch" || true      # gh may have already removed the local one
rm -rf .scratch/8
git restore .gitignore 2>/dev/null   # a no-op after a full run; see below
```

The guards matter: while `run.json` still says `"pr_number": null`, an unguarded `gh pr close ""`
acts on whatever PR matches the current branch.

Order matters: `git switch main` before deleting the branch, and read `ledger.json` before deleting
the scratch directory. The `git restore` is the one line that does nothing here: the Implementer
committed the `.scratch/` line on the issue branch, so `git switch main` is already what drops it,
and from that moment any scratch directory still on disk (issue #3's, if you tried the clarification
exit) shows up as untracked until you delete it. The next `orch` command puts the line back and says
so.

After a clarification exit, or any run that stopped before the Implementer committed, the
`.gitignore` edit is still uncommitted on `main` and there is no branch and no PR (`run.json` still
carries `"branch": null`). The whole reset is then `rm -rf .scratch/<n>` followed by
`git restore .gitignore` **last**, once every `.scratch/<n>` is gone.

Either way, finish on GitHub: close any follow-up issues the Judge filed (`gh issue close <url>`),
and re-open issue #8 if you closed it.

For a fully pristine clone, also `rm -rf .agents .claude` (only the `orch` symlinks live there in
this sandbox) and drop the 12 lines `orch` appended to `.git/info/exclude`. Leaving them costs
nothing: the next `orch` command recreates exactly the same links.

Do not merge the demo PRs unless you mean to: `main` is the base every later contract is written
against.

---

## 8. Troubleshooting

**`zsh: command not found: orch`** The symlink is missing or `~/.local/bin` is not on `PATH`. Check
`ls -l ~/.local/bin/orch` and `echo $PATH`. Always-works fallback:
`uv run --project ~/code/my-orchestrator orch status 8`. Do not swap in `--directory`; it would
chdir out of the target repo, which is exactly what `bin/orch` exists to avoid.

**`[orch] WARNING: skill 'orch-contract' is not in the session's skills (N listed)`** The claude
session started without the role skill, so it will do something other than the role and the step
will very likely escalate. Check that the links resolve
(`ls -l .agents/skills .claude/skills`); they are absolute paths into `~/code/my-orchestrator`, so
moving or renaming the orchestrator checkout breaks all 12. `orch` recreates links that are
**absent**, but never repairs one that is in the way: `run` and `step` fail with
`orch: <path> is a symlink but does not point at <source>` (a link left dangling by a moved
orchestrator) or `orch: <path> already exists and is not a symlink to ...` (something else at that
path). `status` downgrades both to `[orch] WARNING: ...` and carries on. Clear either by hand
(`rm -rf .agents/skills/orch-* .claude/skills/orch-*`), then re-run.

**`[orch] WARNING: no result line in the stream: the claude session died mid-run`** The process
ended without its terminal `result` event. Read `.scratch/8/logs/<seq>-<role>.err` and the tail of
the matching `.jsonl`. Usually transient; the step then changes nothing, so the run escalates and
`rm .scratch/8/escalation.md && orch run 8` retries it. The codex counterparts are
`[orch] WARNING: codex wrote no final message file (...)` and
`[orch] WARNING: codex exited <n> (see ...)`. `Reading additional input from stdin...` in a codex
`.err` file is normal (stdin is `/dev/null`) and not an error.

**A run that ends in `escalation.md`** See step 5. Read the quoted final message and the `Log:` path
under `## What did not converge` before deciding whether to retry or intervene: a role that declined
on purpose says so there, and re-running it will not change its mind.

**Dirty checkout or wrong branch** `run`/`step` check out `run.json`'s branch only when the checkout
is not already on it, printing `[orch] checking out <branch>` just then (a resumed run is usually
already there and prints nothing). Git refuses the checkout when uncommitted changes would be
overwritten, and the CLI surfaces it as `orch: git checkout ... failed (1) in <repo>: ...` with exit
1. The common case is `.gitignore` itself: every `orch` command re-appends `.scratch/` to it before
the checkout, so resuming from `main` (which lacks the line) into an issue branch (which commits it)
fails on every retry, and `git restore .gitignore` alone does not help because the next command
dirties it again. Restore and switch by hand, then run:

```sh
git restore .gitignore && git switch "$(sed -n 's/.*"branch": "\([^"]*\)".*/\1/p' .scratch/8/run.json)"
orch run 8
```

Start a **new** issue from a clean `main`, not a feature branch: the Contractor reads whatever is
checked out, while the Implementer branches from `origin/<default>`, so a contract written on a
feature branch can name code that is not on `main`.

**`orch: not inside a git repository: <path>`** Your cwd is not inside any git checkout. `cd` into
the target repo.

**A run against the wrong repo** Running `orch` from inside `my-orchestrator` does **not** error:
that is a git repo too, so it becomes the target. `orch` skips skill linking when the target is the
orchestrator, appends `.scratch/` to the orchestrator's own `.gitignore`, and then contracts and
implements against the wrong repo. The `repo` line in `orch status <n>` is the check.

**API key instead of subscription** See step 0. `[orch] session=... model=claude-opus-5` tells you
which model ran, not which credential paid for it, so check the environment before the run rather
than the log after it.

---

## 9. Knobs (`orch.toml`)

Resolution order: `--config <path>`, then `$ORCH_CONFIG`, then `~/code/my-orchestrator/orch.toml`.
Sections layer over the built-in defaults, so a partial file is fine. An explicitly named file that
does not exist is an error (`orch: config file not found: <path>`).

```sh
cp ~/code/my-orchestrator/orch.toml /tmp/demo.toml   # outside the clone, so it leaves no litter
orch run 8 --config /tmp/demo.toml
ORCH_CONFIG=/tmp/demo.toml orch run 8                # same effect
```

Worth touching for a demo:

| Key | Default | Why you would change it |
|---|---|---|
| `roles.<role>` | claude x4, codex x2 | flip `reviewer`/`judge` to `claude` for a single-harness demo, or `implementer` to `codex` to compare implementations |
| `runners.claude.model` | `"opus"` | a cheaper model for a dry run; `""` lets the harness choose |
| `runners.codex.model` | `""` | `""` uses your `~/.codex/config.toml` model |
| `policy.review_round_cap` | `2` | `1` to reach an escalation quickly on purpose |
| `policy.audit_failure_cap` | `3` | how many consecutive failed audits before escalation |
| `policy.max_steps` | `40` | the only cap that makes `orch run` exit non-zero rather than terminal |
| `runners.codex.sandbox` | `"danger-full-access"` | leave it: `gh` needs network |
| `runners.*.extra_args` | `[]` | appended verbatim to the command line |

Role keys are `contractor`, `implementer`, `auditor`, `reviewer`, `judge`, `remediator`. Config is
read fresh on every invocation, so you can change it between `orch step` calls in the same run.
