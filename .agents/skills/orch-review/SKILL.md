---
name: orch-review
description: "Orchestrator role 4 (Reviewer): render a verdict on a PR against its contract, with every finding carrying a defect class, a location, and evidence."
disable-model-invocation: true
---

# Reviewer

**The argument is the issue number.** Substitute it for `<issue>` in every path and command below. Your cwd is the target repo root; every path is relative to it.

You write exactly one file: `.scratch/<issue>/review-<n>.md`. Every other file in the repo stays as you found it.

You render a **verdict** against the contract, not a list of improvements. The **burden of proof is on the finding**: a defect you can demonstrate is written down; anything else is left out, and leaving it out is the correct outcome, not a miss.

## 1. Set up

```sh
find .scratch/<issue> -maxdepth 1 -name 'review-*.md' | sed -E 's/.*review-([0-9]+)\.md/\1/' | sort -n | tail -1
```

`n` = 1 + that number, or 1 when it prints nothing.

```sh
cat .scratch/<issue>/contract.md
cat .scratch/<issue>/run.json          # take pr_number
git rev-parse HEAD                     # the commit you are reviewing
cat .scratch/<issue>/ledger.json       # may not exist yet
```

Done when you have `n`, the contract's acceptance criteria, `pr_number`, the full 40-character HEAD sha, and the ledger's findings (or the knowledge that there are none).

## 2. Take the diff

**Round 1** — the whole PR, and `base` is the literal string `pr`:

```sh
gh pr diff <pr_number>
```

**Round n ≥ 2** — the **delta** since the previous review, and nothing else:

```sh
sed -n 's/^commit: *//p' .scratch/<issue>/review-<n-1>.md | head -1   # this is `base`
git diff <base>..HEAD
```

Code outside that delta was reviewed in an earlier round and is settled. Every finding you write in round n ≥ 2 points at a line inside this delta.

Done when you have read the diff for your round end to end.

## 3. Read the ledger

Every finding in `ledger.json` has already been adjudicated — blocking, deferred, or dropped, all three are settled. Raising one again restarts a conversation that already ended.

Match on **location and substance**, not on id: a ledger entry at `src/auth/token.ts:42` about seconds-vs-milliseconds covers the same defect reported at line 44 with different wording.

Done when you can name, for each ledger entry, the thing you will not report.

## 4. Find the defects

Read the diff for defects against the contract's acceptance criteria, using the classes below. Put every candidate through the evidence bar; the ones that clear it become findings.

### Defect classes

`correctness`, `contract_violation`, `security`, `data_loss`, `style`, `performance`, `maintainability`, `test_quality`, `docs`, `other`.

The first four are the ones that can block a PR, so spend your attention there:

- **correctness** — the code does the wrong thing for an input it will actually receive: a wrong comparison, an off-by-one, an unhandled `None`/`null`/error return, a unit mismatch.
- **contract_violation** — an acceptance criterion is unmet, or the change does something the contract's Non-Goals excluded. `.gitignore` and everything under `.scratch/` are implicitly inside `scope_paths` — the CLI writes them and the implementer commits the `.gitignore` line by design — so a change to either is never a finding, of this class or any other.
- **security** — untrusted input reaching a sink, a secret written where it persists, an authorisation check that can be bypassed.
- **data_loss** — a write, delete, or migration that can destroy data the caller expects to keep.

The other six classes are real but never block; write one only when the evidence bar is met, and expect it to be deferred.

### The evidence bar

Evidence is one of these three, quoted concretely in the finding:

1. **Observed behaviour** — you ran something and it produced the wrong result. Include the command and the output.
2. **A reproduction** — the exact input and the path through the code that reaches the defect, specific enough that someone else gets the same result.
3. **A quoted line** from the diff plus the reasoning that makes it wrong — the line, and the input for which it misbehaves.

These are not evidence, and a candidate resting on one of them is left out: a preference, a "consider whether", a hypothetical caller nothing in the repo calls, a style the repo already uses elsewhere, a refactor, or a request for a test the contract did not ask for. Nor is a hunk in `.gitignore` or under `.scratch/`: both are implicitly in scope, whatever `scope_paths` lists.

Done when every candidate defect has been either backed by one of the three forms of evidence or discarded.

## 5. Write the review

The verdict follows the findings: `APPROVE` when the Findings section is empty, `REQUEST_CHANGES` when it holds at least one.

Write `.scratch/<issue>/review-<n>.md` in exactly this shape:

```markdown
---
verdict: REQUEST_CHANGES
commit: <full 40-char HEAD sha reviewed>
round: 1
base: pr
---
## Findings
### F-1-1
- class: correctness
- location: src/auth/token.ts:42
- evidence: <what was observed / how to reproduce / the quoted line and why it is wrong>
- statement: <one sentence naming the defect>
```

- `verdict` is `APPROVE` or `REQUEST_CHANGES`; `round` is `n`; `base` is `pr` in round 1, the previous review's commit sha in round n ≥ 2.
- Finding ids are round-scoped: `F-<n>-1`, `F-<n>-2`, … in the order you found them.
- `class` is one of the ten above; `location` is `path:line`; `statement` is one sentence.
- An `APPROVE` review keeps the `## Findings` heading with nothing under it.

Done when `review-<n>.md` exists with the four front-matter keys, its `commit` equals `git rev-parse HEAD`, every finding carries all four fields, no finding repeats a ledger entry, and the verdict matches whether the Findings section is empty.
