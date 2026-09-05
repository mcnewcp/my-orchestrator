---
name: orch-judge
description: "Orchestrator role 5 (Judge): adjudicate a review round's findings into the ledger and file follow-up issues for the deferred ones."
disable-model-invocation: true
---

# Judge

**The argument is the issue number.** Substitute it for `<issue>` in every path and command below. Your cwd is the target repo root; every path is relative to it.

You write `.scratch/<issue>/ledger.json` and GitHub follow-up issues. Every file of code in the repo stays exactly as you found it. The round cap belongs to the CLI: it reads `rounds_completed` and escalates on its own, so never write `escalation.md`.

**Deferral is the default.** A finding blocks this PR only when it proves it must; everything else is recorded, filed as a follow-up, and gets out of the way. The **ledger** is permanent: once a finding is in it, no later round may raise it again.

## 1. Set up

```sh
find .scratch/<issue> -maxdepth 1 -name 'review-*.md' | sed -E 's/.*review-([0-9]+)\.md/\1/' | sort -n | tail -1
```

`n` = that number — the round you are adjudicating.

```sh
cat .scratch/<issue>/review-<n>.md
cat .scratch/<issue>/contract.md
cat .scratch/<issue>/run.json          # take pr_url
cat .scratch/<issue>/ledger.json       # may not exist yet
```

When review-`n` has no findings, skip to step 4 and record the round.

Done when you have `n`, every finding in review-`n`, the contract's acceptance criteria, `pr_url`, and the existing ledger entries.

## 2. Adjudicate each finding

Open the file at each finding's `location` and check its evidence against what the code actually does. Rate it only after that check.

- **`blocking`** — the class is `correctness`, `contract_violation`, `security`, or `data_loss`, **and** the evidence holds up as a real defect measured against the contract. Nothing else can be blocking; a `style` or `maintainability` finding, however sharp, is not.
- **`dropped`** — the evidence does not survive your check (the code does not do what the finding says), the same defect is already in the ledger, or the finding's only substance is a change to `.gitignore` or to a file under `.scratch/` (both are implicitly inside `scope_paths`, so neither is a defect). The rationale says which.
- **`deferred`** — everything else, including a genuine defect in a class that cannot block. This is the default: when you find yourself arguing that something *ought* to block, it defers.

Every disposition carries a one-sentence `rationale` that names what decided it — the acceptance criterion violated, the evidence that failed to reproduce, the class that cannot block.

Done when every finding in review-`n` has a disposition and a rationale.

## 3. File a follow-up for every deferred finding

```sh
gh issue create --title "<class>: <the finding's statement>" --body-file - <<'BODY'
Deferred from review round <n> of #<issue>.

PR: <pr_url>

- class: <class>
- location: <path:line>
- evidence: <evidence, verbatim from the review>
- statement: <statement>
BODY
```

The command prints the issue URL; that string is the finding's `followup_issue`. Blocking and dropped findings get `null`.

Done when every deferred finding has a real issue URL.

## 4. Write the ledger

Carry every existing entry through **verbatim**, append one entry per finding from review-`n`, keeping the review's ids, and set `rounds_completed` to `n`. Write `.scratch/<issue>/ledger.json` in exactly this shape:

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

`summary` is the finding's statement, compressed to a phrase; `round` is `n`; `id`, `class`, and `location` come across from the review unchanged. New entries start `resolved: false` and `resolved_commit: null` — the remediator sets those.

Done when `ledger.json` parses, `rounds_completed` equals `n`, every finding of every round appears exactly once, every deferred entry has a non-null `followup_issue`, and every file outside `.scratch/<issue>/` is unchanged (`git status --porcelain` is clean).
