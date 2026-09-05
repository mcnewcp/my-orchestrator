---
name: orch-contract
description: "Orchestrator role 1 (Contractor): turn a GitHub issue into a verifiable contract, or into a clarification request when it cannot be made verifiable."
disable-model-invocation: true
---

# Contractor

**The argument is the issue number.** Substitute it for `<issue>` in every path and command below. Your cwd is the target repo root; every path is relative to it.

You write **exactly one** of `.scratch/<issue>/contract.md` or `.scratch/<issue>/clarification.md` — never both. Leave every other file in the repo unchanged; the CLI has already written `run.json`.

The **contract** is the definition of done for every role after you: the implementer builds to it, the auditor checks against it, the reviewer renders a verdict against it. A criterion no test can settle is the failure mode this role exists to prevent.

## 1. Ensure the scratch directory

```sh
mkdir -p .scratch/<issue>
```

The contract is **frozen** once written: when `.scratch/<issue>/contract.md` already exists, report its path and stop the whole skill here, leaving it untouched.

Done when `.scratch/<issue>/` exists and holds no contract yet.

## 2. Read the issue

```sh
gh issue view <issue> --comments
```

Copy the issue title verbatim — the branch slug is derived from it downstream.

Done when you can state, in one sentence each, every behaviour change the issue and its comments ask for.

## 3. Discover the verification commands

Downstream roles never guess how this repo verifies itself; they run what you record. Read the repo's own configuration:

- `package.json` (`scripts`), `pyproject.toml`, `Makefile`, `justfile`, `Cargo.toml`, `go.mod`
- `.github/workflows/*.yml` and any other CI config — the best source, because it names the commands the maintainers actually run

Then **run each candidate once** from the repo root. Keep the ones that execute: a suite whose tests fail still executes, a missing script does not. `test` is required; `lint` and `typecheck` are recorded only when the repo has them. When the repo configures no test runner, name its ecosystem's default (`npm test`, `uv run pytest -q`, `cargo test`, `go test ./...`) and confirm that executes.

Done when every command you will record has been run once and executed.

## 4. Decide: contract or clarification

A criterion is **verifiable** when a test at a named seam observes it, pass or fail, with no human judgement. Check each behaviour from step 2 against that bar. These make a criterion unverifiable:

- an outcome with no observable threshold ("make it faster", "cleaner API")
- a choice the issue leaves open (which format, which default, which of two behaviours on an edge case)
- a dependency on a fact that is neither in the repo nor in the issue (an external contract, a credential, a decision the operator owns)

When every behaviour clears the bar, go to step 5. When any behaviour fails it, write `.scratch/<issue>/clarification.md` and stop:

```markdown
# Clarification needed — issue #<issue>: <title>

## Blocked: <the criterion, as the issue states it>
Why it cannot be verified as written: <one sentence>
Question: <one concrete question whose answer makes it verifiable>

## Blocked: ...
```

One section per blocked criterion, each ending in a question the operator can answer in a sentence. Answering every question must be enough to write the contract on the next run.

Done when `clarification.md` exists, `contract.md` does not, and every unverifiable behaviour has its own section.

## 5. Write the contract

Write `.scratch/<issue>/contract.md` with this front matter shape exactly:

```yaml
---
issue: 17
title: "Add percent_change helper"
test_budget: 12
scope_paths: ["src/**", "tests/**"]
commands:
  test: "uv run pytest -q"
  lint: "uv run ruff check ."
  typecheck: "uv run mypy ."
---
```

The CLI parses this without a YAML library, so stay inside the subset and write no comments: scalar `issue` (bare integer), `title` (double-quoted), `test_budget` (bare integer); `scope_paths` as one inline JSON-style list on one line; `commands` as a one-level mapping whose values are double-quoted strings. Omit the command keys the repo lacks.

- **`title`** — the issue title, verbatim; the branch slug is derived from it. Escape any `"` inside it as `\"`, or the parser rejects the file.
- **`test_budget`** — the maximum number of tests the PR may **add**. Rule of thumb: roughly 2 per acceptance criterion, floor 4, ceiling 20; adjust down for a one-line fix, up for a criterion that genuinely needs several cases. This budget is what stops test count from ballooning round after round, so set it to the number a competent implementation needs, not to a comfortable margin.
- **`scope_paths`** — the narrowest globs, relative to the repo root, that cover the files this change touches plus their tests. `.scratch/**` and `.gitignore` are always implicitly in scope; leave them out.
- **`commands`** — exactly the strings from step 3, each runnable from the repo root via `sh -c`.

Then these four sections, in this order:

```markdown
## Summary
<what changes and why, 2-4 sentences>

## Acceptance Criteria
- **AC-1** — <single verifiable statement>. Verified by: `test_name_a`, `test_name_b`
- **AC-2** — <single verifiable statement>. Verified by: `test_name_c`

## Test Plan
<one bullet per AC: the seam it is exercised at and the cases that cover it>

## Non-Goals
<what this PR leaves alone>
```

Every AC is one statement and carries its own `Verified by:` line. The names on that line are the test functions the implementer will write; the auditor looks for them literally (`git grep`), so spell them the way this repo names its tests. Non-Goals is what keeps `scope_paths` honest — name the adjacent work this PR declines.

Done when `.scratch/<issue>/contract.md` exists, its front matter matches the shape above, every AC has a `Verified by:` line naming at least one test, and `clarification.md` does not exist.
