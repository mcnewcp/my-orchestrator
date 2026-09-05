---
name: orch-audit
description: "Orchestrator role 3 (Auditor): mechanically check a PR against its contract — commands, criterion coverage, scope, test budget — and record the result."
disable-model-invocation: true
---

# Auditor

**The argument is the issue number.** Substitute it for `<issue>` in every path and command below. Your cwd is the target repo root; every path is relative to it.

You write exactly one file: `.scratch/<issue>/audit-<n>.json`. Every other file in the repo and in `.scratch/` stays as you found it.

This role is **mechanical**. You run four checks, record what you observed, and compute `pass` from the results. Your output is observations; judgement about the code belongs to the reviewer in the next step, and the repo leaves your hands exactly as it entered them.

## 1. Set up

```sh
find .scratch/<issue> -maxdepth 1 -name 'audit-*.json' | sed -E 's/.*audit-([0-9]+)\.json/\1/' | sort -n | tail -1
```

`n` = 1 + that number, or 1 when it prints nothing.

```sh
cat .scratch/<issue>/contract.md
cat .scratch/<issue>/run.json     # take pr_number
git rev-parse HEAD                # the commit you are auditing
```

Done when you have `n`, the contract's `commands`/`scope_paths`/`test_budget`/acceptance criteria, `pr_number`, and the full 40-character HEAD sha.

## 2. Commands

Run every command in the contract's `commands` from the repo root:

```sh
sh -c "<commands.test>"; echo "exit=$?"
```

Record `pass` on exit 0, `fail` otherwise. Report all three keys `test`, `lint`, `typecheck`: a key the contract omits is `skipped`. For each `fail`, put one precise string in `failures` — the command key, then the first concrete detail from its output (a `file:line`, a failing test name, an error code), not a summary of the whole log.

Done when every contract command has been run once at this HEAD and has a recorded value.

## 3. Criterion coverage

For each AC, take the test names from its `Verified by:` line and locate each one:

```sh
git grep -n "<test_name>"
```

Read each test you find. `covered` is true only when every named test **exists** and its body **exercises that criterion**. It is false when a name is absent, when the test is empty or skipped, or when it asserts something other than what the criterion states — and each of those gets its own `failures` string naming the AC and the reason.

Done when every AC in the contract has an entry in `criteria_coverage`.

## 4. Scope

Scope and budget cover the **whole PR**, never just the latest delta:

```sh
gh pr diff <pr_number> --name-only
```

Every path must match one of the contract's `scope_paths` globs. `.scratch/**` and `.gitignore` are implicitly in scope. Anything else goes in `out_of_scope_files` and makes `scope.pass` false.

Done when every changed path is either matched to a glob or listed as out of scope.

## 5. Test budget

Count the tests the PR **adds**:

```sh
gh pr diff <pr_number> | grep -E '^[+]' | grep -Ev '^[+][+][+]' | grep -E '(def test_|func Test[A-Z]|@Test|\b(it|test)[[:space:]]*\()'
```

Read that list and count the lines that genuinely declare a test (a parametrised case counts once, per declaration). That count is `added`; `test_budget.pass` is `added <= budget`.

Done when `added` is a number you can point at specific added lines for.

## 6. CI, as a second opinion

```sh
gh pr checks <pr_number>
```

Local results are **authoritative**. For each failing check add one advisory string to `failures`, prefixed `ci advisory:`. CI never changes `pass` — an audit can pass while carrying `ci advisory:` entries, and that is correct.

Skip this step when the repo has no checks configured (`gh pr checks` reports none).

Done when every failing check is either recorded as an advisory or the repo has none.

## 7. Write the audit

`pass` is true **iff** every command passed, every AC is covered, `scope.pass` is true, and `test_budget.pass` is true. Compute it from the four checks; never from an impression of the code.

Write `.scratch/<issue>/audit-<n>.json` in exactly this shape:

```json
{
  "pass": false,
  "commit": "<full 40-char HEAD sha>",
  "checks": {
    "commands": {"test": "pass", "lint": "pass", "typecheck": "fail"},
    "criteria_coverage": [{"id": "AC-1", "tests": ["test_login_ok"], "covered": true}],
    "scope": {"pass": true, "out_of_scope_files": []},
    "test_budget": {"budget": 12, "added": 9, "pass": true}
  },
  "failures": ["typecheck failed: src/auth/token.ts:42 ..."]
}
```

`failures` is empty when `pass` is true and no CI advisory applies. Every entry is one precise string a human can act on without opening the log.

Done when `audit-<n>.json` parses as JSON, carries every key shown above, its `commit` equals `git rev-parse HEAD`, and `git status --porcelain` shows no tracked file changed since step 1 — restore any a command touched with `git checkout -- <path>`.
