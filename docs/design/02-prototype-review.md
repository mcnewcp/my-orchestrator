# orch prototype review

**2026-09-04**

The prototype is sound in shape and unsound in enforcement. The state machine, the artifact-derived
state model, the runner harness and the six-role decomposition all work: three live runs against
`mcnewcp/orch-sandbox` produced a merged PR, a clarification exit and a second merged PR, and the
83-test suite is honest about the paths it covers. What does not hold is design principle 7,
"Nothing generative decides 'done'": every gate the design calls mechanical is in fact a value an
LLM typed into a JSON file that the CLI reads back without recomputing. Fix three things first, in
order: recompute the Auditor's `pass` from its own `checks` and validate `ledger.json`'s
`disposition`/`resolved` fields (R1-01, R1-05, R1-07); check the open-blocking ledger before
FINALIZING rather than after the APPROVE shortcut (R1-04); and verify that the audited commit is
the commit on GitHub before `gh pr ready` (R1-03). Checked and found solid: the derivation table
matches the design row for row, stuck detection catches a role that writes nothing, the round and
audit-failure caps do terminate, `orch status` genuinely does not check out the issue branch, and
the escalation exit carries the role's own final message.

Counts: 40 confirmed (1 critical, 21 major, 18 minor), 1 plausible, 17 refuted, 16 unverified
critic-round leads. Method and verification procedure are in the appendix.

---

## 1. Major and obvious flaws

Every confirmed critical and major finding, ranked. Repro tests live under
`/private/tmp/claude-501/-Users-coymcnew-code-my-orchestrator/87b580ef-b514-4055-a367-40dc0eede9dd/scratchpad/repro/`
(abbreviated below as `repro/`); each runs with
`uv run --project /Users/coymcnew/code/my-orchestrator pytest -q repro/<file>`.

### 1. R1-07 The Remediator's ledger update is unchecked freehand text editing

**CONFIRMED · critical · simplification · `.agents/skills/orch-remediate/SKILL.md:71`,
`orch/machine.py:188`**

The skill says the Remediator sets `resolved`/`resolved_commit` on **one** finding; nothing
mechanical enforces it. `orch/machine.py:188` takes `count = len(open_blocking_findings(scratch))`
at entry and invokes the remediator that many times without re-reading which finding closed, and
the CLI never compares the ledger before and after an invocation (`grep -rn resolved_commit orch/`
returns zero hits in production code).

**What goes wrong.** Round 1 yields two blocking findings. The Remediator fixes F-1-1 and marks
both resolved. The second invocation reports "no open blocking finding" and stops, `derive_state`
sees no open blockers, the delta-scoped round-2 review sees only the F-1-1 fix and approves, and
`gh pr ready` runs on a PR whose summary table asserts F-1-2 was fixed when nothing touched it.

**Evidence.** The live Remediator did exactly the dangerous thing:
`/Users/coymcnew/code/orch-sandbox/.scratch/1/logs/006-remediator.jsonl` line 49 is
`sed -i '' -e 's/^      "resolved": false,$/      "resolved": true,/' -e 's/^      "resolved_commit": null$/.../' .scratch/1/ledger.json`,
a line-anchored global substitution. Replayed against a two-finding ledger in the shape the live
Judge produced, both entries flip at the same sha. `repro/R1-07_test.py` (3 passed) drives the full
`machine.run`: roles `['remediator','remediator','auditor','reviewer']`, one commit
(`fix: F-1-1 (#17)`), no F-1-2 commit, result `READY`, no `escalation.md`, `gh pr ready 7` issued,
and `summary.md` printing `| F-1-2 | correctness | blocking | yes |, |`. It did not fire live only
because that ledger had one finding.

**Fix.** Have the CLI own the write: snapshot the ledger before each remediator invocation, require
that exactly one previously-open blocking finding changed, escalate otherwise. Change the skill to
patch the ledger with a JSON load/modify/dump keyed on the finding id.

### 2. R1-05 A non-boolean `resolved` or a mis-spelled `disposition` silently erases a blocker

**CONFIRMED · major · bug · `orch/state.py:100-104`, `orch/artifacts.py:340-352`**

`read_ledger` validates only that `rounds_completed` is an int and `findings` is a list of dicts.
`open_blocking_findings` then uses truthiness: `f.get("disposition") == "blocking" and not
f.get("resolved", False)`. `not "false"` is `False`, and any disposition other than the exact
lowercase string is non-blocking.

**What goes wrong.** The Judge writes `"resolved": "false"` (a string) or `"disposition":
"Blocking"`. The blocker vanishes from the filter, `derive_state` returns FINALIZING instead of
REMEDIATING, and the run marks the PR ready. For the string variant it is worse than silent:
`orch/finalize.py:30` is `res="yes" if f.get("resolved") else "no"`, so the published PR body
affirmatively claims the blocker was fixed.

**Evidence.** `repro/R1-05_test.py` (11 passed): six malformed shapes (`"false"`, `"no"`,
`"Blocking"`, `"BLOCKING"`, missing key, `"blocker"`) all read clean and all derive FINALIZING,
against a control that derives REMEDIATING. The same predicate also disarms the round-cap
escalation at `orch/machine.py:178`. Every other reader in `artifacts.py` validates the field the
state machine keys on (`read_audit` rejects `"pass": "yes"`, `read_review_front` rejects a verdict
outside the two allowed); `ledger.json` is the one artifact whose decision fields are read raw.

**Fix.** In `read_ledger`, require `disposition` in `{blocking, deferred, dropped}` and `resolved`
to be a real bool; raise `ArtifactError` rather than letting the entry fall out of the filter.

### 3. R1-04 An APPROVE review finalizes the PR even with an open blocking finding

**CONFIRMED · major · design-flaw · `orch/state.py:65`**

`if front["verdict"] == "APPROVE": return State.FINALIZING` (line 65) sits above
`return State.REMEDIATING if open_blocking_findings(scratch_dir) else State.FINALIZING` (line 71),
so the ledger is never read on the APPROVE path, and `orch/finalize.py:71-86` has no ledger check
of its own. `open_blocking_findings` is consulted at `state.py:59`, `machine.py:80`, `:178` and
`:188`, never on the path that ships the PR.

**What goes wrong.** The Remediator commits and pushes its fix but its session ends before it
writes `resolved: true`. HEAD moved, so the next state is AUDITING; the audit passes; the round-2
reviewer is delta-scoped by design (`.agents/skills/orch-review/SKILL.md:47`) so it sees only the
fix and approves. `derive_state` jumps to FINALIZING and the run exits `READY` with a blocking
correctness defect on record as unresolved.

**Evidence.** `repro/R1-04_test.py` (6 passed): identical ledgers derive REMEDIATING under
REQUEST_CHANGES and FINALIZING under APPROVE, proving the open-blocking check is reachable only
through the REQUEST_CHANGES branch; the full `machine.run` ends `state=READY reason=terminal`,
`open blocking findings at exit: ['F-1-1']`, no `escalation.md`, `gh pr ready 7` issued. Note this
is faithful to the design: `docs/design/01-initial-prototype.md:202` puts
`| latest review APPROVE | FINALIZING |` above the two ledger rows, so the hole is in the skeleton,
not in the implementation of it.

**Fix.** Move the open-blocking check above the verdict check in `derive_state`, and gate
`finalize.finalize` on `not open_blocking_findings(scratch)`. Add the missing case to
`tests/test_state.py`'s 18 `CASES`, which never pairs APPROVE with an open blocker.

### 4. R1-01 The Auditor gate is not mechanical: the CLI trusts a `pass` boolean an LLM typed

**CONFIRMED · major · simplification · `orch/artifacts.py:312-319`, `orch/state.py:58`**

`read_audit` is four lines: `pass` must be a bool, `commit` a non-empty string, return. `checks` is
not even a required key and is never read anywhere (`grep -rn '"checks"' orch/` returns nothing).
`orch/state.py:58` `if not audit[1]["pass"]:` is the entire gate. The contract's `scope_paths`,
`test_budget` and `commands` reach only the `orch status` inventory string;
`Contract.commands` (`orch/artifacts.py:246`) has no caller at all.

**What goes wrong.** An audit recording `commands.test: "fail"`, `AC-1 covered: false`,
`scope.pass: false` and `test_budget {budget: 12, added: 412}` alongside `"pass": true` is accepted,
derives REVIEWING and then FINALIZING, and finalize marks the PR ready. Three of the Auditor's four
checks are pure LLM judgement anyway: criterion coverage (`orch-audit/SKILL.md:51`, "its body
**exercises that criterion**"), scope-glob matching (`:63`, matched by eye) and test counting
(`:75`, "count the lines that genuinely declare a test").

**Evidence.** `repro/R1-01_test.py` (5 passed), including a control that flips only the boolean to
`false` with byte-identical `checks` and gets IMPLEMENTING. The docs claim the opposite in three
places: `docs/design/01-initial-prototype.md:53` "**Nothing generative decides 'done.'** The
Auditor gate and the CLI's finalize step are mechanical", `README.md:60` "mechanical pass/fail
against the contract", `docs/conventions.md:170` "`pass` is true iff every command passed, every AC
is covered, scope passes, and test budget passes". Not listed among the §9 intentional deviations.

**Fix.** Recompute `pass` in `read_audit` from `checks` and raise when the recorded value disagrees.
Move the two genuinely mechanical checks into the CLI (fnmatch each path of
`gh pr diff <pr> --name-only` against `scope_paths`, re-derive `test_budget.added`). Or drop the
word "mechanical" from the design, the README and the skill.

### 5. R2-02 Any file named `summary.md` / `escalation.md` / `clarification.md` is an unauthenticated verdict

**CONFIRMED · major · design-flaw · `orch/state.py:42-47`**

The first three checks in `derive_state` are bare `.exists()` tests on three names, first match
wins. The CLI never records that it wrote the file, and re-evaluates the precedence on every call,
so a file appearing after a run finished overrides the finished state.

**What goes wrong.** A `.scratch/2/` containing only a hand-written `summary.md` makes `orch run 2`
print `READY: .../summary.md` and exit 0 with no harness invoked, no contract, no branch and no PR.
Mid-run, a stub Judge that also writes `summary.md` ends the run `READY` with `ledger.json` still
holding `{'id': 'F-1-1', 'disposition': 'blocking', 'resolved': False}` and the gh call log empty,
so the PR is still a draft while the CLI reports the PR URL as if it were live. There is a
non-LLM path too: `orch/finalize.py:79` writes `summary.md` before the `try`, and the rollback at
`:83` is `except Exception`, so a Ctrl-C during the two gh calls leaves a permanent false READY
(this is R1-24's mechanism, same root).

**Evidence.** All three behaviours reproduced in-session against the real package with the repo's
own fake runner. `README.md:64` states the rule outright: "its existence means `READY`". The only
defence anywhere is skill prose, and the Judge's write-scope line
(`orch-judge/SKILL.md:11`, "Every file of code in the repo stays exactly as you found it") does not
cover `.scratch/` artifacts, unlike the other five roles.

**Fix.** Do not fix by making the verdict sticky (`docs/design/01-initial-prototype.md:190` says
state is never stored, and `README.md:45`'s retry depends on it). Close the two cheap holes instead:
catch `BaseException` (or write `summary.md` only after `gh pr ready` returns) at
`orch/finalize.py:83`, and tighten the Judge's write-scope line to match the other five.

### 6. R1-03 Nothing verifies the audited commit reached GitHub

**CONFIRMED · major · design-flaw · `orch/finalize.py:81-82`**

Every state decision compares artifacts to the local HEAD, but the artifact a human reviews is the
PR on GitHub. `grep -rn "push\|origin\|remote\|fetch\|upstream\|headRefOid" orch/` returns no hits:
the whole git/gh surface is `rev-parse`, `checkout`, `gh pr edit` and `gh pr ready`. The push
invariant is prose (`docs/conventions.md:300-301`) checked only by two skills' own "Done when"
self-assessment.

**What goes wrong.** The Remediator commits and `git push -u origin HEAD` is rejected (expired
credential, a push rule, an "Update branch" on GitHub). The session still exits 0 and marks the
finding resolved. HEAD moved, so the audit re-runs locally and passes, the round-2 review diffs
locally and approves, and finalize marks ready a PR whose branch on GitHub still carries the
defect. The gates also mix sources: `orch-audit/SKILL.md:26` stamps local `git rev-parse HEAD`
while `:60`/`:72` take scope and budget from the remote `gh pr diff`.

**Evidence.** `repro/R1-03_test.py` (3 passed) uses a real checkout with a real bare remote: after
the run, `git rev-parse origin/issue-17/fix` is still the pre-fix sha and
`git show origin/issue-17/fix:src/x.py` still contains `BUG: off-by-one`, while `audit-2.json` and
`review-2.md` both carry the unpushed local sha and the only gh calls are `pr edit` and `pr ready`.
All 21 git commands orch itself ran are local reads.

**Fix.** Before FINALIZING, compare `git rev-parse HEAD` with
`gh pr view <pr> --json headRefOid,state,isDraft` (after fetching the branch) and refuse on a
mismatch or a non-draft/closed PR.

### 7. R2-01 `pr_number` is an unverified LLM-written identity that finalize acts on destructively

**CONFIRMED · major · bug · `orch/finalize.py:81`**

`run.json`'s `pr_number` is written by the Implementer and never checked against the branch, the
issue, the repo or GitHub. `read_run` (`orch/artifacts.py:187-190`) validates nothing beyond "is a
JSON object"; finalize's only check is `if pr is None`. `finalize.py` is the sole caller of
`shell.gh` in the package, and `grep -rn "pr view" orch/` returns nothing.

**What goes wrong.** A number copied from an abandoned attempt or a sibling PR sends
`gh pr edit <n> --body-file summary.md` and `gh pr ready <n>` at an unrelated PR: its human-written
body is replaced and it is flipped out of draft, firing reviewer requests and CI. Meanwhile
`orch/cli.py:44` prints the separately-stored, equally unvalidated `pr_url`, so the operator opens
a normal-looking draft and concludes the run is fine. Upstream, the same wrong number makes the
Auditor's scope check and the Reviewer's diff read a different PR's changes while both stamp this
branch's HEAD.

**Evidence.** `repro/R2-01_test.py`: with `"pr_number": 5, "pr_url": .../pull/6`, finalize emits
`gh pr edit 5` / `gh pr ready 5` while `_terminal_report` returns `.../pull/6`.
`tests/test_finalize.py:52` pins the passthrough as intended behaviour. The stale-number precondition
is real: `gh pr list -R mcnewcp/orch-sandbox --state all` shows PR #5 CLOSED on the same head branch
as PR #6. Note that most wrong values fail loudly (a nonexistent number errors out of `gh pr edit`);
the silent case needs a number naming a different open PR.

**Fix.** In finalize, `gh pr view <pr> --json url,headRefName,state,isDraft` and refuse unless
`headRefName == run.json["branch"]`, the PR is open and still a draft, and `url == run.json["pr_url"]`.
Print the PR finalize actually edited.

### 8. R1-20 The contract's acceptance criteria are never validated

**CONFIRMED · major · simplification · `orch/artifacts.py:266-285`**

`read_contract` validates front matter only. It never requires an `## Acceptance Criteria` section,
never requires a `Verified by:` line per AC, and `split_sections` (`:167`) keys on the exact
`## <text>` string. `Contract.acceptance_criteria` (`:256-258`) is
`self.sections.get("Acceptance Criteria", "")`, and `orch/finalize.py:57` substitutes
`"_No acceptance criteria in the contract._"` rather than failing.

**What goes wrong.** A contract with no AC section (or an empty one) is accepted, the Auditor's
`criteria_coverage` is `[]` so "every AC is covered" is vacuously true, the run reaches READY, and
the PR the operator receives carries a placeholder where its definition of done belongs. A
lowercase `## Acceptance criteria` heading has the narrower effect of emptying the PR body's
criteria while the roles, which read the raw file, still see them.

**Evidence.** `repro/R1-20_test.py` (10 passed): a contract titled "Make the API faster" with only
`## Summary`, `scope_paths: ["**"]` and `commands: {test: "true"}` passes `read_contract` and renders
a complete PR body; the end-to-end run reaches `State.READY` and the body pushed to GitHub reads
`## Acceptance Criteria` / `_No acceptance criteria in the contract._`. The repo's own house style
already drifts: `orch-implement/SKILL.md:108` writes `## Acceptance criteria` (lowercase) while
`orch-contract/SKILL.md:100` and `conventions.md:135` use the capitalized form the parser demands.
This is design constraint 1 (`design:32`, "termination anchors to a contract of verifiable
acceptance criteria"), enforced nowhere in code.

**Fix.** Validate the contract at the end of the CONTRACTING transition (`machine.py:144-146`), not
at FINALIZING: require the AC section, a non-empty body, and a `Verified by:` line per `AC-<n>`;
escalate on failure. Separately, look the section up case-insensitively in `Contract`.

### 9. R1-12 The delta-only / no-re-raise ledger constraint has zero mechanical enforcement

**CONFIRMED · major · simplification · `orch/artifacts.py:322-329`**

`docs/conventions.md:180` defines `round` and `base` in the review front matter, and
`design:34` calls delta-only round-2 reviews the load-bearing fix for non-convergence. But
`read_review_front` returns only `verdict` and `commit`; the machine takes the round number from the
filename (`_REVIEW_RE`, `artifacts.py:21`); and `read_ledger` performs no cross-round checks.
`grep -rn '\bbase\b' orch/` returns exactly one unrelated hit (`runners.py:125  name = "base"`).

**What goes wrong.** A `review-2.md` declaring `round: 1` and `base: pr` parses clean, so a whole-PR
round-2 review that re-raises a dropped finding is accepted: the Judge blocks it, the Remediator
edits already-adjudicated code, and the run burns a full remediate/audit/review/judge cycle before
escalating. The mirror case loses history: a ledger that drops every round-1 entry is accepted,
`resolved: true` may regress to `false`, and `summary.md` (the operator's only permanent record)
lists only the surviving findings.

**Evidence.** `repro/R1-12_test.py` (6 passed), including the full non-convergence cycle
(`fake.calls == ["remediator","auditor","reviewer","judge"]`, ledger locations
`["src/x.py:4", "src/x.py:4"]`, ESCALATED). The repo's own fixture violates the invariant:
`tests/conftest.py:104` writes `base: pr` for every round, and four tests consume that malformed
round-2 review without complaint.

**Fix.** Validate `round` against the filename and `base` against the previous review's `commit` in
`read_review_front`; in `read_ledger`, assert every prior finding id is still present and no
`resolved: true` regressed.

### 10. R1-15 The state machine consults no invocation-health signal

**CONFIRMED · major · design-flaw · `orch/runners.py:223`, `orch/machine.py:132-140`**

`ClaudeRunner.on_line` warns when the role skill is absent and prints `is_error` without branching;
`CodexRunner.on_line` (`:262-268`) handles only `item.completed` and ignores `turn.failed`; the exit
code reaches `machine.py:139` only as the display string `f"ran {role} (exit {code})"`. Nothing in
`step`, `run` or `derive_state` reads any of it.

**What goes wrong.** A codex reviewer session fails (`turn.failed` in the parsed stream, non-zero
exit) but leaves a schema-valid `review-1.md` with `verdict: APPROVE`. `read_review_front` accepts
it, `derive_state` returns FINALIZING, and FINALIZING is unconditional `gh pr edit` plus
`gh pr ready`. Stuck detection cannot fire because a new artifact appeared. The documented
compensating control (`conventions.md:60-62`, "the CLI re-derives state from the scratch directory
after every step") is a file-existence-and-schema check, so it does not compensate.

**Evidence.** `repro/R1-15_test.py` (3 passed) plus a tie-break rerun with the harness exiting 1 as
the project's own measurements say it does: `actions: ['ran reviewer (exit 1)']`,
`state before: REVIEWING`, `state after: FINALIZING`, `escalation.md exists: False`. A new
observation from the live logs sharpens the skill half: every claude invocation's init line lists
the same 21 skills including all six `orch-*` regardless of which was dispatched, so
`if skill not in skills` verifies availability, not load. On the codex side there is no in-stream
skill signal at all (`thread.started` carries only `{type, thread_id}` across all four live codex
sessions), and reviewer and judge are exactly the two roles whose artifacts gate `gh pr ready`.

**Fix.** Carry a health flag on `StepResult` and treat `is_error: true` / `turn.failed` / a non-zero
exit as a failed invocation that escalates rather than a printed field. For codex, have each skill's
first action write a `logs/<seq>-<role>.loaded` marker the runner checks in `on_finish`.

### 11. R2-05 Nothing binds a run to a repo

**CONFIRMED · major · design-flaw · `orch/runners.py:77`**

`target_repo()` (`orch/cli.py:19-25`) accepts any git checkout the cwd sits in; its only rejection is
"not inside a git repository". `link_skills` computes `if target == orchestrator:` at
`runners.py:77` and then `return []` at `:78`, so the one place the code detects "this is the
orchestrator" uses the fact to skip linking rather than to refuse.

**What goes wrong.** An operator whose shell is still in the orchestrator checkout types
`orch run 8`. The Contractor's `gh issue view 8` resolves to `mcnewcp/my-orchestrator`, the
Implementer branches off that repo's default branch, pushes, opens a real PR, and finalize marks it
ready. Nothing warns before or during. With an uncommitted working tree (this repo has one right
now) the branch carries unrelated in-progress work into that PR.

**Evidence.** `scratchpad/selfrun.py` drives the full pipeline against a copy of this repo with
stub harnesses: no error, contractor through reviewer all run, `git log` shows
`feat: thing (#8)` on `issue-8/thing`, gh log shows `pr edit 42 --body-file .../summary.md` and
`pr ready 42`, exit 0. `tests/test_runners.py:190-191` asserts the permissive behaviour is correct.
`docs/demo.md:543-546` documents it as a known trap with no code guard, and `conventions.md:99-100`
specifies the skip, so the fix touches the spec too. Cost of one wrong-repo run, from the live
logs: five claude sessions at $0.406 + $0.556 + $0.242 + $0.298 + $0.246 = $1.75, plus three codex
sessions.

**Fix.** Raise `SetupError` at `runners.py:77` instead of returning `[]` (the comparison is already
computed), or gate it behind an explicit `--allow-self`.

### 12. R1-06 The Contractor reads whatever branch is checked out; the Implementer branches from origin/default

**CONFIRMED · major · design-flaw · `orch/machine.py:68-75`**

`ensure_branch` returns early while `run.json.branch` is unset, and `artifacts.ensure_run` writes
`"branch": null`, so the CONTRACTING step always inspects the tree the operator left behind.
`.agents/skills/orch-implement/SKILL.md:50` then cuts the branch with
`git checkout -b "issue-<issue>/$slug" "origin/$default"`. `machine.py:75` is the only `git checkout`
in the whole CLI and there is no `git fetch` anywhere.

**What goes wrong.** The contract is written against one tree and implemented against another. In
the abandoned issue-4 run, the Contractor sat on the unmerged `issue-1` branch and wrote acceptance
criteria referencing `test_percent_change_zero_old_raises` and
`test_percent_change_exported_alongside_add_and_mean`, neither of which exists on `origin/main`
(`git show 5a3a79e:tests/test_calc.py` has 3 tests, the issue-1 branch has 9). Six claude sessions
over roughly 8 minutes (~$2.5) ended in a stuck escalation, PR #5 abandoned, `.scratch/4` wiped and
rerun.

**Evidence.** `repro/R1-06_test.py` (3 passed) shows the contractor invoked with
`current_branch == "issue-1/add-percent-change"` while `git show origin/main:src/calc.py` lacks the
symbol, and the implementer branching from `origin/main` afterwards. Correction to the original
report, worth keeping straight: the run-4 checkout was moved by the old `orch status` checkout bug
(since fixed at `orch/cli.py:56-58`), not left behind by run 1, and issue #4's body itself named the
missing symbol. What the stale tree actually defeated was the Contractor's only guard: its
clarification rule fires on "a fact that is neither in the repo nor in the issue", and on the stale
tree the fact was in the repo. The by-omission path is still live because every finished run leaves
the checkout on the issue branch and the CLI never fetches or returns to the default branch. Only
`docs/demo.md:65` and `:536-538` mention it; README and conventions do not.

**Fix.** Fetch and pin the checkout to the ref the Implementer will branch from before CONTRACTING,
or pass that sha to the Contractor and have it verify every symbol it names against that ref.

### 13. R1-18 No clean-tree precondition and no lock

**CONFIRMED · major · design-flaw · `orch/machine.py:68`, `:125`, `:196`**

`ensure_branch` switches the operator's shared working tree at the start and end of every step with
no cleanliness check, no stash and no restore, and there is no lock file anywhere in the package
(`grep -rn "porcelain\|stash\|flock\|O_EXCL" orch/` finds only an unrelated `--quiet`).

**What goes wrong.** (a) Uncommitted work in a file the issue branch does not touch rides across the
checkout; the Auditor then runs the contract's commands against a tree that is not the commit it
stamps, and records `pass: true` for that commit. (b) A second `orch` command for another issue,
run from the same clone, switches the tree under a running role: the still-running auditor reads
issue 4's source and writes `.scratch/7/audit-1.json` with `pass: true` and the pre-switch sha.

**Evidence.** `repro/R1-18_test.py` (3 passed): in (a), inside the auditor
`current_branch == "issue-17/x"`, `src/wip.py` holds the operator's WIP, `git show
issue-17/x:src/wip.py` does not, the audit passes and `next_state is REVIEWING`; in (b) the auditor
observes `issue-4/b` mid-invocation and issue 7's gate opens on it. Note `ensure_branch` runs at
`machine.py:125` before the terminal check at `:142`, so even `orch step` on a finished run yanks
the tree. The project already half-fixed this once: `conventions.md:334-336` records that checking
out during `status` switched the tree under a running pipeline in the second live run; `status` was
fixed, `run` and `step` were not. `docs/demo.md:522-524` tells the operator the dirty-tree case is a
loud git error, which is true only when the file conflicts.

**Fix.** Refuse to start a step when `git status --porcelain` is non-empty outside `.scratch/`; take
an exclusive per-checkout lock naming the issue and pid for the duration of run/step; or run each
issue in its own `git worktree`.

### 14. R1-08 No per-state invocation cap, and a stuck predicate any new artifact defeats

**CONFIRMED · major · design-flaw · `orch/machine.py:225-229`, `orch/state.py:129-133`**

Only two loops are capped. AUDITING exits when the audit's `commit` string-equals HEAD and
IMPLEMENTING when `pr_number` is non-null, with no counter on either, and
`consecutive_audit_failures` returns 0 as soon as the latest audit passes so the failure cap cannot
fire for a passing-but-mismatched audit. The stuck test requires `files_after <= files_before` over
a set of file *names*, so writing `audit-2.json` after `audit-1.json` defeats it.

**What goes wrong.** An auditor that records an abbreviated sha in an otherwise passing audit (or a
`gh pr create` that failed once so `pr_number` stays null, or an implementer that commits a stray
hunk it cannot clear) loops until `max_steps`. `orch/machine.py:254-256` then returns
`reason="max_steps"` and `orch/cli.py:97` prints one stderr line: `_escalate` is never called, so
the operator gets no file, contradicting the design's "exactly two unattended-to-human exits".

**Evidence.** `repro/R1-08_test.py` (6 passed): the passing-but-mismatched audit gives 40 auditor
invocations, 40 audit files, `consecutive_audit_failures = 0`, no `escalation.md`; the null-`pr_number`
case gives 40 implementer invocations and 41 commits; a contractor writing `notes-<n>.md` keeps a
dead CONTRACTING loop alive for all 8 steps while the control (a role that writes nothing) escalates
in 1 step. At the measured $0.242/27s per auditor session that is roughly $10 and 20 minutes.
`read_audit` accepts `"4333ea7"`, `"A"*40` and `"deadbeef (HEAD -> main)"` alike. The skills do
instruct a full 40-char sha, and all live artifacts carry one, so this is latent rather than
observed.

**Fix.** Add an unconditional per-state invocation counter that escalates after N entries regardless
of files or commits; validate `commit` as 40 lowercase hex in `read_audit`/`read_review_front`; and
call `_escalate` on the `max_steps` path. Do not switch the stuck test to content comparison, which
would make it weaker.

### 15. R1-25 A closed or merged PR makes finalize overwrite the body and then hard-error forever

**CONFIRMED · major · bug · `orch/finalize.py:80-86`**

finalize never checks the PR's state. `gh pr edit` succeeds on a closed PR (the body is really
replaced), `gh pr ready` then fails, the rollback deletes the local `summary.md`, and the ShellError
escapes `machine.run` (which has no try/except) to `orch/cli.py:130`.

**What goes wrong.** The operator closes an abandoned draft PR, or merges one by hand between steps.
The next `orch run` re-derives FINALIZING, replaces the closed PR's body with this issue's summary,
dies with a raw gh message, and writes no `escalation.md`. Every retry repeats the same destructive
attempt; the run can never reach a terminal state.

**Evidence.** `repro/R1-25_test.py` (4 passed): three consecutive runs each raise, each leave
FINALIZING with no `escalation.md`, and the `pr edit` count rises 1, 2, 3. gh 2.97.0 carries
`Pull request %s#%d is closed. Only draft pull requests can be marked as "ready for review"` and
has no state guard for `pr edit`. Live precedent: PR #5 on `mcnewcp/orch-sandbox` was closed at
2026-09-04T03:05:27Z while its scratch run was on disk.

**Fix.** Read `gh pr view <pr> --json state,isDraft` first: skip `pr ready` when already ready, and
raise `FinalizeError` (or escalate) when the PR is CLOSED or MERGED, before touching the body. Same
call as R2-01's and R1-03's fix.

### 16. R1-11 `contract.md` is parsed for the first time at FINALIZING

**CONFIRMED · major · design-flaw · `orch/finalize.py:77`**

`derive_state` only checks that `contract.md` exists (`orch/state.py:48`). `read_contract` is called
from `finalize()` and the `orch status` inventory, nowhere else, and `machine.py:144-146` invokes
the contractor and returns without reading what it wrote.

**What goes wrong.** A front matter the CLI's own parser rejects passes CONTRACTING, IMPLEMENTING,
AUDITING, REVIEWING and JUDGING unnoticed, kills the run at the last step with no `escalation.md`,
and repeats on every retry because the Contractor refuses to touch an existing contract
(`orch-contract/SKILL.md:21`).

**Evidence.** `repro/R1-11_test.py` (13 passed): four plausible Contractor outputs are rejected
(`title: "Add "clamp" helper"`, a wrapped inline list, a block-style `scope_paths`, a title ending in
a backslash); each walks the pipeline through four roles and then raises out of `finalize.py:77`
with `gh_calls == []`, no `summary.md`, no `escalation.md`, and `derive_state` still FINALIZING; the
second run invokes zero roles and raises identically. Cost before the crash, from the live per-role
figures, is roughly $1.1 to $1.8. The skill itself names the hazard at `orch-contract/SKILL.md:89`
("Escape any `\"` inside it as `\\\"`, or the parser rejects the file") and checks it nowhere.

**Fix.** Call `read_contract` at the end of the CONTRACTING transition and write `escalation.md`
naming the file and the parse error on `ArtifactError`. Same insertion point as R1-20's fix.

### 17. R1-09 One malformed artifact wedges run, step and status, printing nothing on stdout

**CONFIRMED · major · bug · `orch/cli.py:59`**

`derive_state` calls the validating readers directly, and `orch/cli.py:59`
`state = derive_state(scratch, head)` runs before the first `print` and before `:66`'s
`inventory(scratch)`. So the per-artifact tolerance at `orch/state.py:136-141`, which exists for
exactly this case and is covered by `tests/test_state.py:171`, is unreachable from the CLI.

**What goes wrong.** The Auditor's session is killed mid-write, or it writes `"pass": "true"`.
`orch run` and `orch step` raise `ArtifactError` and write no `escalation.md` (the escalation path
itself raises on the same file at `machine.py:80`/`:89`), and `orch status`, the command that exists
to explain the wreck, exits 1 with **empty stdout**: no state, no HEAD, no branch, no inventory, no
log path.

**Evidence.** `repro/R1-09_test.py` (11 passed) asserts `stdout == ""` literally, for truncated and
mistyped audits and for a malformed ledger, while `inventory(scratch)` called directly returns the
usable row `audit-1.json -> 'unreadable (...)'`. The live logs show the auditor assembling these
files with a non-atomic `cat > .scratch/N/audit-1.json <<'EOF'`, so a killed session leaves exactly
the truncated file. `README.md:44-46`'s recovery advice does not apply because no `escalation.md`
exists.

**Fix.** Catch `ArtifactError` around the per-step derivation in `machine.run` and write
`escalation.md` naming the file and the parse error; in `cmd_status`, print
`state  UNDERIVABLE (<error>)` and fall through to head/branch/inventory, which already degrade.

### 18. R1-10 The CLI's own `.gitignore` write wedges `ensure_branch`'s checkout

**CONFIRMED · major · bug · `orch/runners.py:38`, `orch/machine.py:75`**

`ensure_scratch_ignored` dirties the tracked `.gitignore` on every command (`orch/cli.py:32` runs
`ensure_target_setup` for run, step and status) before `ensure_branch` runs `git checkout <branch>`
with `check=True`.

**What goes wrong.** The operator stands on `main` (the natural way to get a clean tree after a run)
and types `orch run 4`. Setup appends `.scratch/`, git refuses the checkout because the issue
branch's committed `.gitignore` differs, `ShellError` becomes exit 1, no role runs and no
`escalation.md` is written. Git refuses even when the working content is byte-identical to the
target branch's version, because it compares index against tree.

**Evidence.** `repro/R1-10_test.py` (8 passed), including two controls proving the write is the
trigger, and a test showing the obvious operator fix (`git checkout -- .gitignore`) does not help
because the next command re-appends the line. The precondition exists in the sandbox today:
`git show main:.gitignore` has no `.scratch/` (removed by the merged 578b423) while
`git show issue-4/...:.gitignore` and `issue-7/...` both have it. `tests/test_machine.py:266` is the
only `ensure_branch` test and creates the branch at the same commit, so its checkout cannot fail.

**Fix.** Move the ignore line into `.git/info/exclude` (see R1-02). Failing that, run
`ensure_scratch_ignored` only for run/step and only after `ensure_branch`, skip it when
`git check-ignore -q .scratch/` already passes, and route a checkout `ShellError` into `_escalate`.

### 19. R1-02 The CLI mutates the target repo's tracked `.gitignore`, and only skill prose stops the roles from fixing it

**CONFIRMED · major · simplification · `orch/runners.py:31-39`**

`ensure_scratch_ignored` appends `.scratch/` to a tracked file, the Implementer is told to commit it
(`orch-implement/SKILL.md:89`), and the exemption that keeps that hunk out of the Auditor's scope
check and the Reviewer's findings exists only as English prose repeated in four `SKILL.md` files.
The Remediator, the one role that edits code in response to a blocking finding, has no carve-out at
all (`grep -n gitignore .agents/skills/orch-remediate/SKILL.md` returns nothing).

**What goes wrong.** The loop already closed wrongly once: `.scratch/1/review-1.md` raised F-1-1
`contract_violation` at `.gitignore:6`, the ledger marked it `blocking`, and commit 578b423 "fix:
drop out-of-scope .scratch/ entry from .gitignore (F-1-1) (#1)" deleted the orchestrator's own setup
line and merged as 9a80696. Four extra harness sessions (judge, remediator, auditor, reviewer) were
spent on that hunk. Because main lost the line, every later branch re-adds it: `4564008` (#4) and
`d7d1fdc` (#7) each carry `.gitignore | 1 +`.

**Evidence.** `repro/R1-02_test.py` (4 passed): `orch status 42` on a clean repo leaves exactly
` M .gitignore`, while the twelve skill symlinks are hidden with `.git/info/exclude` by the same
function, which is the untracked mechanism that would work here too. Post-carve-out run 4 held
cleanly, so this is a prose-guarded hazard rather than an ongoing wrong result.
`docs/conventions.md:240` also says "`orch status` never touches the checkout", which is false as
written even though §9's narrower "never checks out the issue branch" is true.

**Fix.** Add `.scratch/` to the `rel_paths` list handed to `_ensure_excluded` (`orch/runners.py:97`)
instead of writing the tracked `.gitignore`. That deletes the need for all four carve-outs and fixes
R1-10 at the same time. Caveat: `_ensure_excluded` degrades to a warning when `.git` is a file
(R1-36), where `.gitignore` still works.

### 20. R1-13 No timeout or supervision on the harness subprocess

**CONFIRMED · major · bug · `orch/runners.py:168`**

`BaseRunner.run` launches the harness with `Popen`, reads stdout with a blocking `for` loop and
calls `proc.wait()`: no wall-clock timeout, no idle timeout, no `try/finally`, no `kill()`.
`grep -rn "timeout\|SIGTERM\|SIGINT\|signal\|kill" orch/ tests/ orch.toml` returns nothing, and
`Config` exposes no time budget to configure.

**What goes wrong.** A session that stops producing output, or one that exits cleanly while leaving
its stdout pipe held open by a background process, never yields EOF. `run()` never returns, so
`machine.run`'s stuck-detection escalation at `:225-249` is unreachable and `max_steps` is never
consulted. `orch run` sits silent with no deadline and no exit.

**Evidence.** A tie-break repro (`scratchpad/tie/R1-13_tiebreak.py`) demonstrates the clean-exit,
pipe-held case: `run() returned within 12s? False`. Healthy sessions are 30 to 91 seconds and $0.24
to $0.56, so a stall is trivially distinguishable by wall clock. Two things in the original report
are refuted and should not be repeated: the child shares orch's process group (measured
`parent pgid == child pgid`), so a terminal Ctrl-C does reach it, and a node child streaming JSON
dies on `write EPIPE` the moment the parent disappears. The remediator fan-out half of the original
finding is also refuted: `machine.py:187-191` is the documented contract
(`conventions.md:254`), the count is snapshotted at entry, and a per-step cap would interleave an
auditor session between every remediation and cost more.

**Fix.** Read with a deadline; on expiry terminate and reap the child (SIGTERM for claude, SIGINT
for codex per the harness learnings), then let the step land in the existing escalation path. Wrap
the loop in `try/finally`. Do not add `start_new_session=True`: it would remove the Ctrl-C
propagation that currently works.

### 21. R1-14 No preflight, and a missing harness binary escapes as a traceback

**CONFIRMED · major · design-flaw · `orch/cli.py:28-37`, `orch/runners.py:158`**

`_context` calls only `load_config` and `runners.ensure_target_setup`. It never checks that `gh` is
authenticated or that the issue exists, never checks that the configured harness binaries are on
PATH, and never inspects which credential the inherited environment will bill. `Popen` raises
`FileNotFoundError` for a missing binary, which is not in `orch/cli.py:130`'s caught tuple.

**What goes wrong.** `codex` is the default reviewer **and** judge. With it not installed, the
Contractor, Implementer and Auditor all run successfully (roughly $1.10 of measured spend and three
minutes), and the run then dies at REVIEWING with a raw Python traceback, leaving a draft PR, the
checkout on the issue branch, an empty `004-reviewer.jsonl` and no `escalation.md`. Separately, a
mistyped issue number costs two contractor sessions before escalating (the first step writes
`run.json`, defeating stuck detection).

**Evidence.** `repro/R1-14_test.py` (6 passed), asserting the exception is outside the caught tuple,
that `escalation.md` does not exist, and that `_context` runs no `gh` and no `shutil.which`. The
cost-ceiling half of the original finding is refuted: `design:234` and `:281` explicitly defer token
budgets, so reading the `total_cost_usd` the stream already reports is an improvement, not a gap.

**Fix.** Wrap `Popen` so `OSError` becomes a `SetupError` naming the harness, and `shutil.which` the
distinct configured binaries in `_context` for run/step. Optionally run
`gh issue view <issue> --json number,state` and fail fast.

### 22. R1-21 Config is merged without validation and never exercised against a real TOML file

**CONFIRMED · major · quality · `orch/config.py:94-97`**

`load_config` does `dict.update` over the defaults with no key or type checking, and `Config`'s
properties coerce with bare `int()`. There is no `tests/test_config.py`;
`grep -rn "load_config\|ConfigError\|ORCH_CONFIG" tests/` returns nothing, and coverage confirms
`orch/config.py 54 9 83%` with the error branches and both path overrides never executed, plus
`orch/runners.py:288-289`'s unknown-runner `ConfigError`.

**What goes wrong.** `reviewer = "cldude"` is accepted silently; the Contractor, Implementer and
Auditor all run and commit, and only then does `get_runner` raise, aborting mid-flight with no
`escalation.md`. A misspelled role key (`implementor`) produces **no** error at any point: the whole
unattended pipeline runs on a harness the operator did not choose. `extra_args = "--debug"` (a
string) is splatted per character into the command line. `review_round_cap = "two"` escapes as a
bare `ValueError` traceback.

**Evidence.** `repro/R1-21_test.py` (8 passed), including the printed argv ending
`'--model', 'opus', '-', '-', 'd', 'e', 'b', 'u', 'g'` and the three-role burn before the
`ConfigError`. Mitigating: state is file-derived, so after fixing the config `orch run` resumes at
REVIEWING and the earlier spend is not lost.

**Fix.** Validate after merging: reject role keys outside `ROLES` and runner names outside `RUNNERS`,
require policy values to be positive ints and `extra_args` to be a list of strings, and raise
`ConfigError` at load time. Add `tests/test_config.py`.

---

## 2. Quality issues

Confirmed minor findings plus one plausible finding, grouped by area.

### State machine

- **R1-31 · minor · bug · `orch/machine.py:145`.** The CONTRACTING transition writes `run.json`
  inside the same step that invokes the contractor, so `files_after > files_before` on the first
  pass and stuck detection cannot fire until a second, identical invocation. With a broken harness
  a fresh issue always pays for two contractor sessions before escalating.
  `tests/test_machine.py:260` encodes it as expected (`fake.calls == ["contractor", "contractor"]`).
  Verified idempotent on retry: once `run.json` exists the stuck exit fires after one invocation.
  Fix: call `ensure_run` before the run loop, or capture `files_before` after it.

- **R1-32 · PLAUSIBLE · minor · bug · `orch/machine.py:188`.** The REMEDIATING loop takes the
  open-blocking count once at entry and invokes the remediator that many times without re-reading
  the ledger, so if one invocation resolves more than one finding the remaining invocations are full
  `claude --model opus` sessions that print "no open blocking finding" and exit. Not refuted and not
  reproduced beyond a probe: the trigger requires the Remediator to break the "one finding per
  invocation" rule its skill states three times, which is exactly R1-07's mechanism, and the live
  evidence has only one single-finding ledger. What would settle it: a run whose Judge marks two
  blocking findings in one round. Note the documented contract (`conventions.md:254`) specifies the
  count-at-entry behaviour, so this is a design nit rather than a deviation, and a per-step cap
  would raise cost, not lower it.

### Runners

- **R1-35 · minor · bug · `orch/runners.py:34-38`.** `ensure_scratch_ignored` splits with
  `splitlines()` and rejoins with `\n`, so a CRLF `.gitignore` is rewritten whole
  (measured: `1 file changed, 3 insertions(+), 2 deletions(-)`), and the Implementer commits that
  churn into the PR under the never-a-finding carve-out. It also matches only the exact string
  `.scratch/`, so a repo that already ignores `.scratch` gains a duplicate line. The trigger needs
  literal CRLF bytes in the index; under `core.autocrlf` or a text attribute git normalizes it away.
  The sibling `_ensure_excluded` at `:57-70` already implements the append-don't-rewrite pattern.

- **R1-36 · minor · bug · `orch/runners.py:58-62`.** `_ensure_excluded` tests
  `(target / ".git").is_dir()` and only warns otherwise, so in a git worktree or submodule the twelve
  skill symlinks are never excluded and show as untracked in the repo the roles are working in,
  dropping `conventions.md:101`'s invariant and making the Judge's "`git status --porcelain` is
  clean" done-condition unsatisfiable. Even `--git-dir` would be wrong: `info/exclude` is read from
  the common dir. Fix: resolve with `git rev-parse --git-common-dir`. No test covers the
  warn-and-return arm.

### Artifacts and parsing

- **R1-17 · minor · simplification · `orch/state.py:100`.** "Only four defect classes may block" is
  enforced nowhere: neither `read_ledger` nor `open_blocking_findings` looks at `class`, though the
  CLI already parses it for display at `machine.py:83` and `finalize.py:28`. A `class: style` finding
  marked `blocking` drives a real remediation, a re-audit and a re-review, and at the cap produces an
  escalation. Calibrated down from major: the design names the Judge as the enforcer in the same
  sentence (`design:229-230`), so this is defence in depth, not a docs-vs-code lie. A warning at
  REMEDIATING entry is the proportionate fix; raising in `read_ledger` would break `orch status`.

- **R1-23 · minor · quality · `orch/artifacts.py:324`.** `read_review_front` parses the review body
  and discards it (`front, _ = ...`), so `conventions.md:190`'s "an APPROVE review has an empty
  `## Findings` section" is unchecked while `derive_state` routes APPROVE straight to FINALIZING. An
  APPROVE carrying two deferred-class findings skips JUDGING entirely: no ledger row, no
  `gh issue create` follow-ups, and the PR body prints `_No review findings were recorded._`. The
  file survives on disk and `orch status` shows it, so nothing is destroyed but the follow-ups and
  the ledger row. Fix: route APPROVE-with-findings to JUDGING rather than raising, which would make
  `orch status` exit 1.

- **R1-38 · minor · bug · `orch/artifacts.py:74-79`.** The escape scan looks at one preceding
  character and only un-escapes `\"`, so any value ending in a backslash is read as an escaped quote
  and reported "unterminated". Per R1-11 that surfaces only at FINALIZING. Single-quoted values are
  never un-escaped, so `'it\'s mine'` parses with the backslash retained while the error message
  advises an escape that cannot work. Fix: count the run of consecutive backslashes (odd escaped,
  even terminator) and un-escape `\\` too.

- **R1-39 · minor · bug · `orch/machine.py:90`.** `failures = "\n".join(f"- {line}" for line in
  (audit[1].get("failures") or []))` iterates whatever it finds, and `read_audit` never validates the
  field, so a JSON string renders one bullet per character in the escalation report. Worse sibling
  from the same gap: a numeric `failures` raises `TypeError` out of `_escalate`, which
  `orch/cli.py:130` does not catch, so `orch run` aborts with a traceback at exactly the cap, writes
  no `escalation.md`, and every re-run re-invokes the Auditor into the same crash. Validating
  `failures` as a list of strings in `read_audit` fixes both.

- **R1-40 · minor · bug · `orch/artifacts.py:167`.** `split_sections` is line-prefix based with no
  fence tracking, so a `## ` line inside a fenced block in the contract's Summary ends the section
  and invents a spurious one. The rendered PR body is then truncated at an unclosed code fence, so
  everything below Summary (criteria, ledger table, follow-ups) renders inside a code block on
  GitHub. Not observed live; the realistic trigger is a docs-flavoured issue quoting markdown.

### Skills and GitHub

- **R1-19 · minor · design-flaw · `.agents/skills/orch-judge/SKILL.md:46-63`.** Step 3 files one
  `gh issue create` per deferred finding (the default disposition) with no cap, no dedupe and no
  idempotency key, and it fires *before* step 4 writes the ledger that records the URLs. A Judge that
  dies in between leaves N issues recorded nowhere; the README's own "delete `escalation.md` and run
  again" then files them a second time, and `orch step` a third. Calibrated down from major: under
  plain `orch run` the reachable states escalate after one filing, no live run has filed any
  follow-up yet, and the blast radius is extra open issues in a system the operator can see. Fix:
  write the ledger entries with `followup_issue: null` first and file only for entries that still
  lack one.

- **R1-24 · minor · bug · `orch/finalize.py:79-86`.** `summary.md` is written before the gh calls and
  rolled back only in `except Exception`; `KeyboardInterrupt` is a `BaseException`, so a Ctrl-C
  during `gh pr edit`/`gh pr ready` leaves a permanent false READY. `repro/R1-24_test.py` (6 passed)
  confirms with a real SIGINT through a PATH-shimmed `gh`: later `orch run` returns
  `state=READY reason=terminal steps=0` with zero gh calls, and `orch status` prints `state READY`
  while the PR is a draft carrying the Implementer's body. No document mentions deleting
  `summary.md`. Fix: render to a temp path and move it into place only after both gh calls succeed;
  that also closes the process-killed and power-loss variants. Same root as R2-02.

- **R1-26 · minor · quality · `orch/finalize.py:45`.** `render_summary` builds the body from contract
  and ledger only, and `gh pr edit --body-file` replaces it outright, so the `## Notes` section the
  Implementer is explicitly told to write (`orch-implement/SKILL.md:112-113`, and `:71` routes
  out-of-scope caveats there) is destroyed immediately before the operator's review. Already happened
  twice: PR #2's Notes explained that AC-5 never had a failing state and is kept as a regression
  guard, PR #6's recorded budget and command results. Both are gone from the current bodies
  (GitHub's edit history retains them, unsurfaced). The cleanest fix is to retire the `## Notes`
  instruction and route caveats to an artifact the pipeline reads, or save the old body to
  `.scratch/<issue>/` before overwriting.

### Tests

- **R1-28 · minor · test-gap · `tests/test_machine.py:253`.** The 83 tests cover none of the failure
  modes that actually break a run: stuck detection is tested only against a runner that writes
  nothing; malformed-artifact tolerance is tested only on `inventory()`, which the CLI never reaches;
  every fixture writes well-typed artifacts with a 40-char sha and exact headings; and no test drives
  review to judge to remediate to re-audit to re-review through `run()`. The only `run()` callers are
  a 4-role happy path that never reaches JUDGING or REMEDIATING, a pause, and a max_steps test that
  never asserts `escalation.md` is absent.

- **R1-30 · minor · test-gap · `tests/test_runners.py:32`.** The doubles are more permissive than the
  interfaces they stand for. `FakePopen.wait()` returns 0 and `__init__` drops `stderr=`, so both
  the non-zero warn path and the stderr redirection are asserted by construction; both `gh` doubles
  declare `(*args, cwd, check=True)` while the real `shell.gh` takes no `check`, which the suite
  cannot catch because `shell.gh` is never executed. Mutation-tested: `stderr=err -> None`, deleting
  the non-zero warn, and adding `check=False` to `finalize.py:82` all leave 83 passing, and the last
  raises `TypeError` at the final step of a successful pipeline after the body is replaced.

- **R1-46 · minor · test-gap · `orch/cli.py:73`.** `cmd_step`'s body and `cmd_run`'s paused and
  aborted branches are never executed; coverage reproduces exactly
  `orch/cli.py 98 16 84% 43-44, 46, 49, 76-82, 94-98`. The one test naming `step` fails inside
  `_context` before `machine.step` is reached. A refactor that returned 0 on `max_steps` would make
  an unattended wrapper treat a 40-session burn as success.

- **R1-47 · minor · test-gap · `tests/test_runners.py:91`.** The codex event test feeds
  `{"item_type": "command_execution"}`, a shape codex never emits: across all three live runs the
  item key census is `('type',)` only, with 62 `command_execution`, 12 `agent_message`, 6
  `file_change`, and zero `item_type`. Mutation-tested: narrowing `orch/runners.py:267` to
  `item.get("item_type")` keeps 83 passing while every real codex run goes silent. `turn.failed` is
  neither handled nor tested.

- **R1-48 · minor · test-gap · `tests/test_state.py:122`.** `test_derive_state_is_pure_over_the_directory`
  monkeypatches `subprocess.run` and then derives from an *empty* scratch dir, returning at the
  fourth `exists()` without opening a file. Proved by fault injection: rebinding `latest_audit` and
  `open_blocking_findings` to wrappers that shell out leaves 83 passing. The load-bearing
  checkout property is pinned elsewhere, so the unpinned property is the weaker "no subprocess at
  all". Fix: parametrize the guard across the existing 18 `CASES`.

- **R1-49 · minor · test-gap · `orch/machine.py:196`.** No test exercises a role that switches
  branches: the happy path's fake Implementer writes `branch: "main"`, so both `ensure_branch` calls
  are no-ops. Mutation-tested: replacing either call with `pass` leaves 83 passing; only removing
  both fails one test. The suite pins "at least one of the two calls exists somewhere", not that
  `head_after` is read on the right branch.

### Docs

- **R1-52 · minor · docs · `README.md:75`.** The by-hand block's comment reads
  `# contractor | implement | audit | remediate`, but the skill is `orch-contract`; `contractor` is
  the role key. `docs/demo.md:413` already has the corrected list. One-word fix. The same finding's
  second half (the missing `--sandbox` flag on the codex line) is refuted: that block is
  deliberately the short interactive form, and `demo.md:410` and `:417-429` print the CLI's actual
  invocation separately.

---

## 3. Unverified candidates from the critic round

A fourth pass ran four adversarial lenses over the merged findings. It produced 19 new candidates.
The author capped verification at three of them to limit spend (R2-01, R2-02 and R2-05, all
confirmed and reported in section 1). **The 16 below are finder claims that no second agent
checked.** Treat them as leads, not findings: several carry executed-command evidence in their
write-ups, but none has been through the skeptic-plus-reproducer procedure that produced everything
in sections 1 and 2, and past experience in this review is that roughly one in four claims does not
survive it. Do not act on any of these without reproducing it first.

| id | sev | category | location | claim |
|---|---|---|---|---|
| R2-03 | critical | design-flaw | `orch/state.py:56` | Gate freshness is HEAD-only, so replacing or amending `contract.md` after the gates pass still finalizes; demo §3's `rm contract.md` recipe is the trigger |
| R2-04 | critical | bug | `orch/state.py:69` | "Already judged" is keyed on the review's filename number, so a rewritten `review-1.md` makes a live REQUEST_CHANGES look adjudicated and the PR is marked ready |
| R2-06 | critical | bug | `orch/state.py:71` | A remediation that resolves the last blocker without committing finalizes while the REQUEST_CHANGES review still stands at the same HEAD |
| R2-07 | major | design-flaw | `orch/state.py:107` | Both caps are counted from role-controlled files, so an auditor that reuses `audit-1.json` resets them and the run burns to `max_steps` with no escalation |
| R2-08 | major | bug | `orch/machine.py:75` | `git checkout <branch>` with no ref check, no `--` and no post-condition: a pathspec value reverts uncommitted work and the step continues on the wrong branch |
| R2-09 | major | simplification | `orch/artifacts.py:226` | Nothing compares the `issue` in `run.json`/`contract.md` with the scratch dir or the CLI argument; `Contract.issue` has no production caller |
| R2-10 | major | design-flaw | `docs/conventions.md:307` | `gh` resolves the repo from remotes and prefers `upstream` while every push targets `origin`; no orch or skill gh call passes `--repo` |
| R2-11 | major | docs | `docs/demo.md:370` | None of demo §5's escalation recoveries can clear a review-round-cap escalation, and each attempt pays for another role |
| R2-12 | minor | bug | `orch/shell.py:43` | `orch status` reports a wrong state and "(no commits)" whenever the issue branch is not a local ref |
| R2-13 | minor | quality | `orch/machine.py:241` | A dead-harness escalation quotes "(none)" and points only at the empty `.jsonl`, never at the `.err` holding the error |
| R2-14 | minor | bug | `orch/runners.py:168` | One non-UTF-8 byte in a harness's stdout aborts `orch run` with a traceback and no escalation |
| R2-15 | minor | bug | `orch/runners.py:53` | `_ensure_symlink` does exists-then-create with no exception handling, so two concurrent orch commands can crash with an unhandled `FileExistsError` |
| R2-16 | minor | quality | `orch/cli.py:89` | `orch run` prints nothing for the FINALIZING step: the PR body replacement and `gh pr ready` leave no operator-visible line |
| R2-17 | minor | test-gap | `tests/test_cli.py:101` | The test named for "status never touches the checkout" asserts only the branch, not the tree |
| R2-18 | minor | test-gap | `tests/test_runners.py:190` | No test asserts any identity binding, and one test locks in the missing wrong-repo guard |
| R2-19 | minor | docs | `orch/cli.py:61` | `orch status` prints a local filesystem path as `repo` and never the GitHub repo `gh` will act on |

**Docs check.** Eight of these cite `docs/demo.md`. I checked the current file: every quoted line is
still present verbatim (`:216` "not happy: the contract is **frozen**", `:222` "Safe to run in the
same clone while a run is in flight (none of these touch the checkout)", `:374` "or clear the
blocker by hand first", `:376` "delete the stale `audit-<n>.json` / `review-<n>.md`", `:546` "The
`repo` line in `orch status <n>` is the check"). None is fixed. One is partly addressed:
`demo.md:136-138` now discloses that the `.gitignore` write and the twelve symlinks run on
`status` as well as `run` and `step`, which softens the docs half of R2-12 and R2-17 without
touching the code or test claims.

---

## 4. Major simplifications

Six design-level themes, drawn from the finding evidence. Each states what the design claims, what
the prototype does, and whether it matters now.

### 4.1 "Nothing generative decides done" is false for every gate

`docs/design/01-initial-prototype.md:53` says "The Auditor gate and the CLI's finalize step are
mechanical", `README.md:60` says "mechanical pass/fail against the contract", and
`conventions.md:170` defines `pass` as an invariant over four checks. In the code, `pass` is a
boolean an LLM typed (R1-01); the ledger's `disposition` and `resolved` are unvalidated strings
(R1-05); the four-blocking-classes rule reads nothing (R1-17); the contract's acceptance criteria
are never required to exist (R1-20); the delta-only and no-re-raise rules are prose in four
`SKILL.md` files (R1-12); and the Remediator's one-finding-per-invocation rule is enforced by
nothing at all (R1-07). Three of the Auditor's four checks are eyeball judgement even when it
behaves.

**Matters now.** This is the one theme that produces a wrong shipped result rather than a broken
run. Two of the fixes are small and independent: recompute `pass` from `checks` in `read_audit`, and
type-check the ledger's two decision fields. The scope and budget checks are mechanically
recomputable in the CLI (fnmatch over `gh pr diff --name-only`), which would make the word
"mechanical" true for half the gate.

### 4.2 Artifacts are trusted by name, with no provenance and no identity binding

`design:44` makes local artifacts the source of truth, which is a sound choice. What is missing is
any binding between an artifact and the thing it describes. `derive_state`'s first three lines make
a file *name* a terminal verdict with no record that the CLI wrote it (R2-02). `pr_number` is an
LLM-written integer that finalize hands to two destructive gh calls with no check that it belongs to
this branch or this repo (R2-01). Nothing binds a run to a repo at all: the one place the code
detects "this is the orchestrator" uses the fact to skip linking rather than to refuse (R2-05). The
unverified critic-round leads extend the same theme to the issue number (R2-09), the branch value
(R2-08), the contract a gate was computed against (R2-03) and the review a ledger adjudicated
(R2-04).

**Matters now** for R2-05 (one line: raise instead of return) and for the `gh pr view` check that
closes R2-01, R1-03 and R1-25 together. The deeper provenance work can wait.

### 4.3 Local state is never reconciled with GitHub, but GitHub is what a human reviews

`design:15` promises "a PR on a pushed branch that verifiably satisfies the issue". The CLI has no
concept of a remote: no push, no fetch, no `gh pr view`, no comparison of HEAD to `@{upstream}`
(R1-03). It will happily finalize an unpushed commit, edit a closed PR's body and then loop forever
on the rejected `gh pr ready` (R1-25), and mix local and remote diff sources inside a single gate
(the Auditor stamps local HEAD while checking scope against `gh pr diff`).

**Matters now.** One `gh pr view <pr> --json headRefOid,state,isDraft` before FINALIZING closes
three majors.

### 4.4 "Exactly two unattended-to-human exits" is not what the code does

`design:55` names clarification and escalation as the only exits. The prototype has at least four
more that produce no artifact: the `max_steps` abort prints one stderr line and never calls
`_escalate` (R1-08); a malformed artifact raises out of `derive_state` and wedges even `orch status`
(R1-09); a contract the CLI's own parser rejects raises out of finalize after the whole pipeline is
paid for (R1-11); a missing harness binary escapes as a raw `FileNotFoundError` (R1-14); and a
failed checkout becomes a bare exit 1 (R1-10). Each leaves the operator with a message on stderr and
nothing on disk.

**Matters now, cheaply.** Route all of them through `_escalate`. The escalation writer itself needs
hardening first, since it can raise on the same malformed artifact.

### 4.5 The CLI mutates the target repo, then relies on prose to hide the mutation

`ensure_scratch_ignored` writes a tracked file on every command including `status`, the Implementer
is told to commit it, and four `SKILL.md` files carry an English carve-out so no role objects. It
already closed wrongly once, deleting the orchestrator's own setup line into main (R1-02), and the
same write wedges the checkout when resuming from `main` (R1-10). Separately, `ensure_branch`
switches the operator's shared tree with no cleanliness check and no lock (R1-18), which the project
already half-fixed for `status` after it bit the second live run.

**Matters now,** and the fix is one list entry: put `.scratch/` in `.git/info/exclude` alongside the
twelve skill links, which the same function already does correctly.

### 4.6 The caps bound steps, not invocations, spend or time

`design:234` says "the caps bound total invocations". They do not. The audit-failure cap cannot fire
for a passing-but-mismatched audit, IMPLEMENTING has no counter, and the stuck predicate is defeated
by any new numbered artifact (R1-08). There is no timeout of any kind on a harness session, so a
stalled child stops the run forever without reaching any cap (R1-13). `total_cost_usd` is parsed out
of every claude result line and thrown away (R1-14), and `max_steps` is documented as transitions,
not invocations, while one REMEDIATING step can launch N sessions.

**Can wait,** except for the timeout, which is the difference between a run that escalates and a run
that hangs unattended overnight.

---

## 5. What was checked and held up

Seventeen candidate findings were refuted on verification. Do not spend time on these.

- **R1-16, six roles run with permissions and sandbox disabled.** The grants are real and
  deliberate; the design never asks for containment (its three load-bearing constraints are
  contract, test budget and ledger), `conventions.md:69-91` specifies the exact flags, and a full
  scan of every `command_execution` across three runs found eight outside-repo events, all of them
  a role reading its own `SKILL.md` at the path orch itself symlinks. The documented rationale for
  `danger-full-access` is broader than the need, which is a docs nit.
- **R1-22, the review round cap counts files rather than judged rounds.** True, and deliberately so:
  a counterfactual run of the suggested fix on the finding's own scenario gave 40 reviewer sessions
  instead of 2, because a mis-stamped review never increments `rounds_completed`. The file count is
  what makes the backstop terminate.
- **R1-27, `pr_number` is the only guard against a second PR.** GitHub refuses a duplicate against an
  open PR, and the read-back the finding calls improvisation is written into the skill twice
  (`orch-implement/SKILL.md:119`, `:129-133`); the live log shows the model performing it.
- **R1-29, `orch step` dies on a finished run whose branch is gone.** Merging deletes the remote
  branch, not the local one; `git checkout` DWIMs a missing local ref from `origin/`; and a finished
  run returns READY before `head` is ever compared. Needs a deliberate `git branch -D` plus a pruned
  remote ref.
- **R1-33, the retry resumes past a cap it already hit.** The cap still holds: the REVIEWING backstop
  escalates before spending a reviewer session, and `docs/demo.md:370-374` predicts the extra round
  verbatim.
- **R1-34, `run()` derives state twice with a possibly wrong HEAD.** The terminal check returns
  before `head` is read at all, and the pre-loop derive is the only binding of `current` on the
  `max_steps <= 0` path, so deleting it raises `NameError`. Only the stale type annotation at
  `machine.py:130` is real.
- **R1-37, the `.err` stream is never surfaced.** It is printed on any non-zero exit
  (`runners.py:183`) and has a dedicated troubleshooting entry at `docs/demo.md:510-516`.
- **R1-41, the Implementer's re-entry brief contradicts step 4.** Step 2's fix list covers the
  lint-only case explicitly, and step 4's own budget rule forbids the ballooning the scenario
  requires.
- **R1-42, `escalation.md` omits the filed follow-up issues.** `conventions.md:222-225` assigns the
  follow-up list to `summary.md` by design, and `docs/demo.md:456` gives the operator the `jq`
  recipe.
- **R1-43, `orch status` reports AUDITING when the branch is missing locally.** `.scratch/` is
  git-ignored so a second clone has no scratch dir, `--prune` does not delete local branches, and a
  merged run returns READY before `head` matters.
- **R1-44, `pr_number` reaches gh unvalidated.** True of `read_run`, but the failure named does not
  occur: gh resolves `"#6"`, `"6"` and a PR URL alike. The real risk is a well-typed wrong integer,
  which type validation would not catch (that is R2-01).
- **R1-45, no test covers finalize's partial-publish path.** Two existing tests pin the gh call
  order; a reorder mutant fails 2 tests. The suggested BaseException test would fail against current
  code, which is R1-24, not a test gap.
- **R1-50, a frozen contract has no amendment path.** `docs/demo.md:216-218` and `:375-379` document
  two, and deleting the stale audits restores the failure budget (verified: 0 after deletion).
- **R1-51, the README's escalation row lists one of three causes.** `escalation.md` states its own
  cause on line 3, the CLI prints it, `README.md:43-46` documents the stuck case, and
  `docs/demo.md:355-361` tabulates all four.
- **R1-53, the design's four-GitHub-capabilities claim contradicts the skills.** The same design doc
  mandates the `gh pr checks` advisory at `:84-86` and lists a diff as a role input at `:69-70`.
- **R1-54, `RunResult.reason` documents an unreachable `stuck` value.** True of one word in an
  internal comment; the CLI docstring, `README.md:46` and `conventions.md §9` all state the actual
  behaviour.
- **R1-55, codex's `.last.md` appears in no artifact table.** Its content is inlined into
  `escalation.md` immediately under the `Log:` line, and `conventions.md:85` defines the file.

Beyond the refutations, verifiers checked and found solid: `derive_state` matches the design's §7
table row for row; stuck detection genuinely catches a role that writes nothing or rewrites an
existing artifact; both caps terminate; `orch status` really does read the branch tip with
`git rev-parse` instead of checking out; `_ensure_excluded` correctly hides all twelve skill links;
the escalation exit carries the role's own final message and its log path; `finalize` correctly rolls
back `summary.md` on an ordinary `ShellError`; and the runner's stream parsing handles both harnesses'
real event shapes (even though the codex test feeds a fictional one).

---

## 6. Recommended next steps

Ordered smallest effort and highest value first.

1. **Validate the two decision artifacts on read** (R1-05, R1-39, plus half of R1-08): in
   `read_ledger`, require `disposition` in `{blocking, deferred, dropped}` and `resolved` to be a
   real bool; in `read_audit`, require `failures` to be a list of strings and `commit` to be 40
   lowercase hex. Roughly 15 lines, four new tests.
2. **Move the open-blocking check above the verdict check in `derive_state`** and gate
   `finalize.finalize` on it (R1-04). Two lines plus the missing `CASES` row.
3. **Move `.scratch/` from the tracked `.gitignore` into `.git/info/exclude`** (R1-02, R1-10,
   R1-35): one entry in the list `_ensure_excluded` already receives, and four skill carve-outs
   become deletable.
4. **Raise `SetupError` when the target is the orchestrator** (R2-05), and `shutil.which` the
   configured harness binaries in `_context` while wrapping `Popen`'s `OSError` (R1-14). Update
   `tests/test_runners.py:190`, which currently pins the permissive behaviour.
5. **Add one `gh pr view <pr> --json headRefOid,state,isDraft` before FINALIZING** and refuse on a
   mismatch, a non-draft, or a closed/merged PR (R1-03, R1-25, R2-01). Have `_terminal_report` print
   the PR finalize actually edited.
6. **Recompute the Auditor's `pass` from its own `checks`** and raise when the recorded value
   disagrees (R1-01); move the scope and budget checks into the CLI, or delete the word "mechanical"
   from the design, the README and the skill.
7. **Make every unattended stop leave one file**: call `_escalate` on the `max_steps` path, catch
   `ArtifactError` around the per-step derivation, validate `contract.md` at the end of CONTRACTING,
   and make `cmd_status` degrade instead of dying (R1-08, R1-09, R1-11, R1-20). Harden `_escalate`
   itself first, since it can raise on the same malformed artifact.
8. **Give the harness a deadline** (R1-13): read with a timeout, terminate and reap the child on
   expiry, and let the step land in the existing escalation path. Then verify or discard the 16
   unverified critic-round leads in section 3, starting with R2-03, R2-04 and R2-06.

---

## Appendix: method

**Nine lenses, first pass.** Each read the prototype independently and filed findings against a
single facet: `state-machine`, `artifacts-parsing`, `runners-harness`, `skills-contract`,
`github-finalize`, `tests-quality`, `design-integrity`, `docs-consistency`, `live-evidence`. Inputs
were `README.md`, `docs/conventions.md` (which wins over the design skeleton, and whose §9
intentional deviations were excluded from reporting unless the deviation itself was flawed),
`docs/design/01-initial-prototype.md`, `docs/design/headless-harness-learnings.md`, the nine modules
of `orch/`, `bin/orch`, `orch.toml`, `pyproject.toml`, the six `SKILL.md` files, the nine test
modules, and the three live runs in `/Users/coymcnew/code/orch-sandbox/.scratch/{1,3,4}` plus that
repo's git history and the 18 harness session logs under
`~/.claude/projects/-Users-coymcnew-code-orch-sandbox/`.

**Merge.** Overlapping reports were merged by root cause into 55 candidates (R1-01 to R1-55), each
carrying its source lenses. A finding reported by several lenses was merged, not counted twice; the
`sources` count on each retained record shows how many lenses reached it independently.

**Verification.** Every candidate went to two independent verifiers with opposed briefs. The
*skeptic* tried to refute the finding: check every quoted line, find the guard the finder missed,
narrow or reject the severity. The *reproducer* tried to make the failure happen: write an
executable test against the real package (`machine.run`, `derive_state`, `finalize`, the real
runners) with only the harness faked, and record the command output. When the two disagreed on the
verdict, or disagreed on severity by more than one step, a third *tie-break* verifier read both
notes plus the source and ruled; nine findings needed one. A finding was marked CONFIRMED only when
at least one verifier reproduced the mechanism and no verifier refuted it; REFUTED when the
mechanism held but the failure could not occur, or the claim's premise was contradicted by the
docs or the live evidence; PLAUSIBLE when the mechanism is real but the trigger depends on
unobserved LLM behaviour that neither verifier could settle. Severity was recalibrated by the
verifiers against the rubric, not taken from the finder: eight findings were downgraded (five
critical to major), one was upgraded.

**Critic round.** A fourth pass ran four adversarial lenses over the merged and verified set,
looking for classes the nine original lenses had missed:
`artifact-provenance-and-role-blast-radius`, `identity-binding`, `demo-docs-and-operator-recovery`,
and `end-to-end-execution-of-the-real-cli` (which drove the actual `orch` binary against stub
harnesses rather than reading code). It produced 19 candidates. **The author capped verification at
three of them to limit spend.** Those three (R2-01, R2-02, R2-05) were confirmed and appear in
section 1. The other 16 are listed unverified in section 3 and should be treated as leads.

**Counts.**

| Status | Count | Of which critical / major / minor |
|---|---|---|
| CONFIRMED | 40 | 1 / 21 / 18 |
| PLAUSIBLE | 1 | 0 / 0 / 1 |
| REFUTED | 17 | claims not reproducible |
| UNVERIFIED | 16 | 3 / 5 / 8 (critic round, unchecked) |
| **Total candidates** | **74** | |

Repro tests are at
`/private/tmp/claude-501/-Users-coymcnew-code-my-orchestrator/87b580ef-b514-4055-a367-40dc0eede9dd/scratchpad/repro/`,
one file per finding, runnable with
`uv run --project /Users/coymcnew/code/my-orchestrator pytest -q <file>`. They are throwaway and
outside both repos. Nothing in `orch-sandbox` was modified; nothing in this repo was modified except
this file. `uv run pytest -q` reports 83 passed before and after.
