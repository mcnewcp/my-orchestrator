# Prototype v0 validation

Validation began on September 6, 2026 (America/Chicago), against the disposable
target `mcnewcp/orch-sandbox`. The implementation branch is `prototype/v0-cdx` in
`mcnewcp/my-orchestrator`. This record separates automated verification, native
agent trials, and acceptance work requiring a separate device or release exercise.

All model sessions used **Codex or Claude Code subscriptions**. The owner's
subscription-only instruction supersedes the design's four-way engine/auth
matrix. No model API trials or API credentials were used.

## Implementation verification

Source revision `6edb8bfd3f50a10a807fbf374140abc3de7eed43` includes the prototype,
the documented positional `status RUN_ID` CLI, preparation evidence improvements,
and publication reconciliation fixes. Its hosted
[Test workflow passed both the test and container jobs](https://github.com/mcnewcp/my-orchestrator/actions/runs/34076650164).

| Verification | Result |
| --- | --- |
| Complete local pytest suite with disposable Postgres 17.6 | 123 passed; database/workflow tests enabled |
| Ruff lint and formatting | Passed |
| GitHub workflow validation | Actionlint 1.7.12 passed; shellcheck and pyflakes integrations were unavailable and disabled |
| Compose, workflow YAML, shell, and embedded Python validation | Passed |
| AppArmor profile compilation and seccomp JSON validation | Passed |
| Offline native sandbox probe in the local image | Passed; no model requests |
| Packaged GitHub CLI pagination compatibility | Passed against an offline paginated HTTP fixture |
| Separate standards and specification review | 0 standards violations; 2 material specification findings repaired |

The [review record](prototype-v0-review.md) describes the fixes and three deferred
maintenance suggestions. The source suite uses real Git and Postgres with
simulated agent and GitHub adapters. In particular,
[workflow regressions](../tests/test_workflow.py) exercise failed checks, changed
approvals, stale reviews, missing CI, interruption, and attempt preservation;
[GitHub transport tests](../tests/test_github.py) exercise failed/missing CI,
candidate identity, pagination, and PR reconciliation. These tests supplement
the native trials below.

## Native runtime and deployment

The trial image pins Codex `0.153.4` and Claude Code `2.1.263`. Both native
subscription smokes read the `ORBIT` marker and returned schema-valid preparation
documents without modifying the fixture. Local evidence directories are:

- Codex: `.scratch/live/smoke/codex-subscription-a182ce89-8a59-4185-85f1-540abe0f8f32/`
- Claude Code: `.scratch/live/smoke/claude-subscription-755e46cb-807a-4870-8cdd-33492f82c849/`

The subsequent issue trials use immutable local image ID
`sha256:26dc01a84cbc11b62ff76c8cc692ceb262846e11da02f552369c6c8673d369c3`.
The final source formatting pass changed no image behavior. Compose runs a
single worker with a private Postgres service and persistent workspace and native
authentication mounts. Each run freezes its engine, subscription auth mode, image
identity, prompts, configuration, issue, and approval evidence.

The target base is `prototype/v0-cdx` at
`1c06523558aafca0f51d3c8bb948d30713902582`. Its local check is
`sh scripts/check-factory-v0.sh`; its required GitHub job is `factory-v0-tests`.
Every completed trial leaves its PR open for owner review. No trial PR was merged.

## Five-issue trial ledger

All five bounded issues reached ready PRs: three with Codex and two with Claude
Code. These runs consumed six implementation attempts, including one deliberately
interrupted attempt. Preparation and review sessions do not consume that budget.

| Issue | Engine | Run ID | Attempts | Outcome |
| --- | --- | --- | --- | --- |
| [#10: ASCII slugify](https://github.com/mcnewcp/orch-sandbox/issues/10) | Codex subscription | `691e25ed-4024-4e4c-b002-795e69c66e4a` | 1 | Ready: [PR #15](https://github.com/mcnewcp/orch-sandbox/pull/15), candidate `ccf2fa733162354b2ae17293b58da20089c09789`; local checks and required CI passed |
| [#11: Count whitespace-separated words](https://github.com/mcnewcp/orch-sandbox/issues/11) | Claude Code subscription | `f8c55f3f-7f88-4e34-b00a-b7199f68ae61` | 1 | Ready after publication interruption: [PR #16](https://github.com/mcnewcp/orch-sandbox/pull/16), unchanged candidate `91252fd5b0fa4e8253cbd57630d7fb6c6b8eb31e`; local checks and required CI passed |
| [#12: Normalize Unicode whitespace](https://github.com/mcnewcp/orch-sandbox/issues/12) | Codex subscription | `11ed9963-519c-4d2b-845a-3d0daed3cc8d` | 2: interrupted, then accepted | Ready after container replacement: [PR #17](https://github.com/mcnewcp/orch-sandbox/pull/17), candidate `627ded042baf834ca59f451fd7fd1729c5b88a34`; local checks and required CI passed |
| [#13: Truncate text to a character budget](https://github.com/mcnewcp/orch-sandbox/issues/13) | Claude Code subscription | `6df24a43-d8b3-48df-974a-acf34129fc76` | 1 | Ready after missing/failed CI exercises: [PR #18](https://github.com/mcnewcp/orch-sandbox/pull/18), unchanged candidate `ee7ced42854e2d88cd79796fd2ea1fc851de20ee`; local checks, actual required CI, and supplemental status passed |
| [#14: Summarize line statistics](https://github.com/mcnewcp/orch-sandbox/issues/14) | Codex subscription | `3904350a-0712-4d82-8739-23b9def3b2b2` | 1 | Ready: [PR #19](https://github.com/mcnewcp/orch-sandbox/pull/19), candidate `493bc9733583509ec277cd7634ceff2b58286807`; local checks and required CI passed |

The required GitHub check passed for the accepted candidate on
[PR #15](https://github.com/mcnewcp/orch-sandbox/actions/runs/34076807641/job/101604422523),
[PR #16](https://github.com/mcnewcp/orch-sandbox/actions/runs/34077159892/job/101605445573),
[PR #17](https://github.com/mcnewcp/orch-sandbox/actions/runs/34077672730/job/101606872191),
[PR #18](https://github.com/mcnewcp/orch-sandbox/actions/runs/34078028614/job/101607856204),
and [PR #19](https://github.com/mcnewcp/orch-sandbox/actions/runs/34078336872/job/101608719997).
Per-run artifacts remain under
`.scratch/live/workspace/runs/<run-id>/`: frozen inputs, local baseline and check
logs, native transcripts and structured responses, reviewed candidate evidence,
and CI observations. Postgres remains authoritative for run and attempt state;
these workspace files are evidence, not a replacement database.

An earlier issue #10 run, `e7c09a84-8513-4152-aa8b-f911722d8647`, used one
implementation attempt and produced a locally passing candidate. Its review
blocked because it required GitHub CI before publication, creating a circular
gate. Preparation and review prompts now state the controller's stage order
explicitly, and preparation receives the baseline results. That run was
superseded with its evidence retained; it did not create a PR and does not count
as a separate useful trial. The replacement used newly frozen inputs.

## Recovery and operator evidence

| Exercise | Evidence and current result |
| --- | --- |
| Duplicate preparation and approval submissions | Repeated accepted submissions returned the existing run; no additional run or implementation session was queued |
| Second worker | A live second worker exited with `Another factory worker already holds database lock` before launching a session |
| Terminal independence | Issue #11 was submitted from a detached tmux session that exited immediately; the independently managed Docker worker subsequently prepared, implemented, and reviewed it. Submission log: `.scratch/live/tmux-submit.log` |
| Interruption after PR creation | The worker received `SIGKILL` after draft PR #16 was recorded. Startup reconciled the run to `blocked:interrupted`; resume reached `ready` with the same candidate, one implementation attempt, and the existing PR. Independent GitHub inspection confirmed exactly one PR and passing required CI |
| Interruption during implementation | Issue #12 was killed during attempt 1 after source and test files were written. Replacement startup retained the approval and marked attempt 1 interrupted. Resume archived both partial files, used attempt 2, and reached ready PR #17; the three-attempt budget was preserved |
| Missing or failed required CI | Issue #13 blocked with draft PR #18 when the supplemental required status was missing, and blocked with the same draft when it failed. After success, resume reached ready with the same PR, candidate, and single implementation attempt. Actual `factory-v0-tests` CI passed throughout these observations |
| State and authentication after container replacement | The interrupted issue #12 container exited 137 and was replaced by a distinct container using the same immutable image. Durable state and workspaces survived; native subscription implementation/review resumed without another login |

Interruption evidence is saved as
`.scratch/live/interrupt-f8c55f3f-7f88-4e34-b00a-b7199f68ae61.json` (publication,
`2026-09-07T02:42:13.798649+00:00`) and
`.scratch/live/interrupt-11ed9963-519c-4d2b-845a-3d0daed3cc8d.json` (implementation,
`2026-09-07T02:48:50.201270+00:00`). The replacement changed container ID
`ca15cb64dad913e77a144e8c3ce4acc5627c45e876c81684e10d4febb7159e15` to
`cbbca26671e87f2571dab1932f97816587736f26dc7ce88294e40ab5bb8eb5e1`.
The blocked state after replacement is saved in
`.scratch/live/replaced-interrupted-11ed9963-519c-4d2b-845a-3d0daed3cc8d.json`.
Both partial implementation files survive under
`.scratch/live/workspace/runs/11ed9963-519c-4d2b-845a-3d0daed3cc8d/recovery/recovery-1500315e059a41b9b71ac956f4c9dec8/`.
Final state exports for completed runs are `.scratch/live/status-<run-id>.json`.
The event ledger export is `.scratch/live/events.json`; completed-run backups are
`.scratch/live/backups/completed-trials-postgres.sql` and
`.scratch/live/backups/completed-trials-workspace.tar.gz`. The worker remains
running with all five trial runs ready and the ordinary CI configuration restored.

Issue #13 freezes both the actual `factory-v0-tests` job and a supplemental
synthetic status, `factory-v0-validation-gate`, as required CI, with a 45-second
CI wait. The live harness passed all three observations:

| Supplemental status | Durable run state | Live PR state | Controller reason |
| --- | --- | --- | --- |
| Missing | Blocked | Draft | `Required CI timed out while fetching exact-revision evidence` |
| Failure | Blocked | Draft | `Required CI failed for ee7ced42854e2d88cd79796fd2ea1fc851de20ee: factory-v0-validation-gate` |
| Success | Ready | Ready for review | No blocked reason |

Each snapshot contains both database state and an independent GitHub observation:
`.scratch/live/ci-{missing,failed,success}-6df24a43-d8b3-48df-974a-acf34129fc76.json`.
The synthetic status responses are saved as
`.scratch/live/ci-status-{failure,success}-6df24a43-d8b3-48df-974a-acf34129fc76.json`.
The context is a controller validation fixture; the actual application check
remained required and passed on the unchanged candidate. After completion, the
owner configuration was restored to `factory-v0-tests` alone with a 300-second
CI timeout, and the worker restarted before issue #14 submission. Issue #13's
frozen configuration remains intact.

The implementation agent acted as the operator for these authorized disposable
trials: it read the generated specification and plan, made corrections, and
submitted approval. These were not separate owner-performed approval sessions.
The original slugify documents needed baseline wording corrected from saved
controller evidence; the successful replacement required no document edits.
The word-count specification needed one sentence corrected so behavior for
non-string input remained unspecified, matching the issue, rather than promising
it could not raise. These edits occurred before approval. The whitespace
normalization documents required no edits.

Before issue #13 approval, the operator restricted the ellipsis invariant to
positive budgets, clarified the Unicode escapes and the one-character budget
boundary, removed an unsupported claim that the out-of-scope pytest baseline
had failed (it had not been measured), and documented the supplemental CI gate.
Before issue #14 approval, the operator clarified that repairs repeat every
configured check and a fresh review, and that publication preserves the already
committed and reviewed candidate.

Forced container interruptions and any test CI statuses are validation fixtures;
they must be recorded separately from naturally occurring implementation defects.
Owner intervention time was not measured, and elapsed agent or CI time is not a
substitute for that metric.

## Remaining acceptance work

- Validate a real laptop-to-phone reconnect over Tailscale and inspect/approve in
  the same server-side tmux session. The local tmux exit test establishes worker
  independence only; it does not demonstrate the device handoff.
- Publish a versioned GHCR image and run both subscription smokes against its
  released digest. Release and manual smoke workflows exist, but no GHCR release
  or hosted subscription smoke run has been performed. The current trial used an
  immutable locally built image.
- Record owner intervention time in a future owner-operated trial. Current
  records identify interventions without inventing a duration.

Merge and target deployment are separate owner-controlled outcomes and are
outside factory readiness validation.
