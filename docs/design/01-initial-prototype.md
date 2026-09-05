# Issue → PR Orchestrator — Prototype Design Skeleton

> **Repo note:** verbatim copy of the operator's design skeleton. The built prototype deviates
> from it in a few places, all listed in `docs/conventions.md` §9; conventions.md wins where the
> two disagree.

> **Audience:** the prototyping agent building this from scratch in a fresh repo.
> Every design point in this document is decided. Values marked *(default)* are tunables
> exposed in config; implement the default.

---

## 1. Goal

**Input:** a GitHub issue number.
**Output:** a PR on a pushed branch that verifiably satisfies the issue, marked ready for
operator review — or a clean stop with a clarification request or an escalation written to the
scratch directory.

The operator is the product owner: writes issues, reviews the resulting PRs. Everything between
issue and PR is unattended.

---

## 2. Design rationale — failure modes this design prevents

A naive implement → review → fix loop does not converge. Three observed failure modes drive the
constraints below. **These constraints are load-bearing; do not simplify them away.**

| Failure mode | Constraint that prevents it |
|---|---|
| LLM reviewers never return "no findings," so a loop that ends when "the reviewer is satisfied" has no fixed point. | Termination anchors to a **contract** of verifiable acceptance criteria written *before* implementation. Reviewer renders a **verdict** against the contract, not a suggestion list. |
| Each round adds code and tests for the next reviewer to comment on; the implementer appeases the reviewer and test count balloons (~300 tests on simple issues). | **Test budget** in the contract, enforced by the Auditor. Findings block only if they name a defect class with evidence (**burden of proof on the finding**); everything else is deferred. |
| Fresh-context reviewers re-raise items already dropped in earlier rounds. | **Ledger** persists every adjudication; round ≥2 reviews are **delta-only** and forbidden from re-raising ledger items. |

---

## 3. Principles

1. **Asymmetry.** Deterministic verification (tests, lint, types) may loop repeatedly — it
   converges. Subjective judgment (review, adjudication) runs a fixed small number of times.
2. **The contract is the definition of done.** Quality is produced in implementation; review is
   defect detection, not quality maximization.
3. **Local artifacts are the source of truth.** `.scratch/<issue>/` holds all state. GitHub
   is a peer system used for exactly four things: read the issue, push the branch, create/update
   the PR, file follow-up issues. No GitHub Apps, no review states, no labels, no check runs.
4. **Every role is an applied skill, run in a fresh session, receiving only the issue number.**
   All communication between roles is via files in the scratch directory. This makes each
   role portable and manually verifiable: the operator can invoke any skill by hand in the
   target repo and inspect its output file.
5. **Deferral is the default.** A finding must prove it blocks.
6. **Scope monotonicity.** After round 1, review covers only the diff since the last review.
7. **Nothing generative decides "done."** The Auditor gate and the CLI's finalize step are
   mechanical.
8. **Exactly two unattended-to-human exits** (clarification, escalation) plus the terminal
   operator review of the PR.

---

## 4. Roles

Six roles. Roles 2 and 6 are one skill with two entrypoints. Each runs in a fresh session with
no conversation history; each reads and writes only within `.scratch/<issue>/` and the repo.

| # | Role | Reads | Writes | Runner *(default)* |
|---|---|---|---|---|
| 1 | **Contractor** | Issue (via `gh`), repo | `contract.md` **or** `clarification.md` | claude |
| 2 | **Implementer** | `contract.md`, repo | Commits on branch; draft PR; `run.json` (branch, pr fields) | claude |
| 3 | **Auditor** | `contract.md`, repo, diff | `audit-<n>.json` | claude |
| 4 | **Reviewer** | `contract.md`, diff (delta-only for n≥2), `ledger.json` | `review-<n>.md` | codex |
| 5 | **Judge** | `review-<n>.md`, `contract.md`, `ledger.json`, repo | `ledger.json` (updated); follow-up issues (via `gh`); `escalation.md` when cap hit | codex |
| 6 | **Remediator** | One blocking finding from `ledger.json`, `contract.md`, repo | Commits on branch; marks finding resolved in `ledger.json` | claude |

**Role notes**

- **Contractor** discovers the repo's verification commands (test, lint, typecheck) and records
  them in the contract so downstream roles never guess. Writes `clarification.md` instead of a
  contract when acceptance criteria cannot be made verifiable from the issue as written.
- **Implementer** is the existing TDD implement skill pointed at the contract. Loops freely
  against the contract's verification commands; does not commit until they pass. Creates the
  branch `issue-<n>/<slug>`, pushes, opens a **draft** PR.
- **Auditor** is a skill whose instructions make it mechanical: run the contract's commands,
  map each acceptance criterion to the test(s) that verify it, check the diff stays inside
  `scope_paths`, count tests against `test_budget`. It produces pass/fail with structured
  detail and no opinions. If the repo has CI, `gh pr checks` is consulted as an additional
  signal; local command results are authoritative.
- **Reviewer** produces a verdict, `APPROVE` or `REQUEST_CHANGES`. Each finding must carry a
  defect class from §8, a location, and evidence. It receives the ledger with an explicit
  instruction not to re-raise adjudicated items, and for round ≥2 receives only the diff since
  the previous review's commit.
- **Judge** assigns each finding a disposition — `blocking`, `deferred`, `dropped` — with
  rationale, defaulting to deferral. Deferred findings are filed as GitHub issues referencing the
  PR; the issue URL is recorded in the ledger. Enforces the review round cap.
- **Remediator** addresses exactly one blocking finding per invocation with a minimal diff; adds
  tests only if they demonstrate the defect. The CLI invokes it once per open blocking finding.

---

## 5. Scratch directory layout

```
.scratch/<issue>/
  run.json            # identifiers only: issue, branch, pr_number, pr_url, created_at
  contract.md         # Contractor — frozen once written
  clarification.md    # Contractor — terminal alternative to contract.md
  audit-<n>.json      # Auditor — one per audit pass, n increments
  review-<n>.md       # Reviewer — one per review round, n increments
  ledger.json         # Judge / Remediator — cumulative
  escalation.md       # terminal
  summary.md          # written by CLI finalize; becomes the PR body
```

`.scratch/` is listed in the repo's `.gitignore`. The CLI adds the entry on first run if it
is missing; with the whole directory ignored, orchestrator artifacts never appear in a PR diff.

---

## 6. Artifact schemas

**`contract.md`** — YAML front matter + fixed markdown sections.

```yaml
---
issue: 123
test_budget: 12                # max new tests this PR may add
scope_paths: ["src/auth/**", "tests/auth/**"]
commands:
  test: "npm test"
  lint: "npm run lint"
  typecheck: "npm run typecheck"   # omit keys the repo lacks
---
```

Sections, in order: `## Summary`, `## Acceptance Criteria` (items `AC-1`, `AC-2`, … — each a
single verifiable statement followed by a `Verified by:` line naming the intended test(s)),
`## Test Plan`, `## Non-Goals`.

**`audit-<n>.json`**

```json
{
  "pass": false,
  "commit": "abc123",
  "checks": {
    "commands": {"test": "pass", "lint": "pass", "typecheck": "fail"},
    "criteria_coverage": [{"id": "AC-1", "tests": ["test_login_ok"], "covered": true}],
    "scope": {"pass": true, "out_of_scope_files": []},
    "test_budget": {"budget": 12, "added": 9, "pass": true}
  },
  "failures": ["typecheck failed: src/auth/token.ts:42 ..."]
}
```

**`review-<n>.md`** — front matter `verdict: APPROVE | REQUEST_CHANGES`, `commit: <sha>`, then
`## Findings` with items `F-<n>-<k>` (round-scoped ids), each listing `class`, `location`,
`evidence`, and a one-sentence statement. An `APPROVE` review has an empty Findings section.

**`ledger.json`**

```json
{
  "rounds_completed": 1,
  "findings": [
    {
      "id": "F-1-2",
      "round": 1,
      "class": "correctness",
      "location": "src/auth/token.ts:42",
      "summary": "expiry compared in seconds vs ms",
      "disposition": "blocking",
      "rationale": "violates AC-3; reproducible via evidence in review-1",
      "followup_issue": null,
      "resolved": false,
      "resolved_commit": null
    }
  ]
}
```

**`run.json`** — `{issue, branch, pr_number, pr_url, created_at}`. Identifiers only; never state.

**`clarification.md`, `escalation.md`, `summary.md`** — prose. Escalation lists what converged,
what did not, open blockers, and a recommendation. Summary contains the contract summary, the
acceptance criteria with their verifying tests, the ledger table, and links to filed follow-ups.

---

## 7. State machine and derivation

State is never stored. The CLI derives it from the scratch directory on every step:

| Condition (evaluated top-down, first match wins) | State |
|---|---|
| `clarification.md` exists | `NEEDS_CLARIFICATION` (terminal) |
| `escalation.md` exists | `ESCALATED` (terminal) |
| `summary.md` exists | `READY` (terminal) |
| no `contract.md` | `CONTRACTING` |
| `contract.md` but no `run.json` with `pr_number` | `IMPLEMENTING` |
| latest `audit-<n>` missing or older than head commit | `AUDITING` |
| latest audit failed | `REMEDIATING` if `ledger.json` has open blocking findings, else `IMPLEMENTING` |
| latest audit passed, no review for this commit | `REVIEWING` |
| latest review `APPROVE` | `FINALIZING` |
| latest review `REQUEST_CHANGES`, not yet judged (ledger `rounds_completed` < n) | `JUDGING` |
| ledger has open blocking findings | `REMEDIATING` |
| ledger has no open blocking findings | `FINALIZING` |

Transitions:

```
CONTRACTING   → run Contractor        → NEEDS_CLARIFICATION | IMPLEMENTING
IMPLEMENTING  → run Implementer       → AUDITING
AUDITING      → run Auditor           → REVIEWING | IMPLEMENTING | REMEDIATING | ESCALATED*
REVIEWING     → run Reviewer          → FINALIZING | JUDGING
JUDGING       → run Judge             → REMEDIATING | FINALIZING | ESCALATED**
REMEDIATING   → run Remediator (×1 per open blocking finding) → AUDITING
FINALIZING    → CLI: write summary.md, set PR body, `gh pr ready` → READY
```

\* ESCALATED when consecutive audit failures reach the cap (§8); the CLI writes `escalation.md`.
\*\* ESCALATED when the review round cap is reached with blocking findings still open; the Judge
writes `escalation.md`. The PR remains a draft in both cases.

---

## 8. Loop policies

- **Review round cap:** 2 *(default)*.
- **Consecutive audit failure cap:** 3 *(default)*. Counted per state; reset on pass.
- **Defect classes eligible to block:** `correctness`, `contract_violation`, `security`,
  `data_loss`. Any other class is auto-deferred by the Judge.
- **Test budget:** set by the Contractor; enforced by the Auditor; Remediator may add tests only
  when they demonstrate a blocking defect.
- **Delta-only review from round 2**; ledger items may not be re-raised.
- **Cost control:** the caps bound total invocations. No separate token budget in the prototype.

---

## 9. CLI

Thin state-machine runner, stateless with respect to the scratch directory. Runs from a
checkout of the target repo.

**Commands**

- `orch run <issue>` — step until a terminal state.
- `orch step <issue>` — perform exactly one transition (for debugging and manual verification).
- `orch status <issue>` — print derived state and artifact inventory.
- `orch run <issue> --pause-after-contract` — stop after `CONTRACTING`; a subsequent `orch run`
  resumes. *(default: off)*

**Runner adapters.** Interface: `run(role, issue, cwd) -> exit_code`. Two implementations,
`claude` and `codex`, each launching a fresh session that invokes the role's skill with the issue
number as its sole argument. Role → runner mapping lives in the orchestrator's config file
(defaults in §4). Skills ship in the orchestrator repo; the adapter is responsible for making
them invocable in the target repo's session (e.g., linking into the runner's skill directory at
run start).

**GitHub access.** The operator's already-authenticated `gh` CLI. Required capabilities: read
issues, push branches, create/edit PRs, create issues. Nothing else.

**Finalize** is CLI code, not a role: assemble `summary.md`, set it as the PR body, mark the PR
ready for review.

**Branching.** `issue-<n>/<slug>`, slug derived from the issue title. PR is created as a draft
by the Implementer and flipped to ready only by finalize.

---

## 10. Scope

**The prototype:** single leaf issue, single repo, sequential. Six role skills, the
scratch artifacts in §5–6, the state machine in §7, the policies in §8, the CLI in §9.

**Deferred to later iterations:**

- Handoff protocol for fresh-context continuation within a role (context-rot defense).
- GitHub as system of record: labels, native review states, check runs, GitHub App auth.
- Integrator role / merge; the operator reviews and merges.
- Parent-spec decomposition across sub-issues; dependency ordering between sub-issues.
- Parallel issue execution; multi-repo; multi-machine.
- Token/cost budgets beyond the invocation caps.
- Learning loop mining ledgers and escalations to improve skills.