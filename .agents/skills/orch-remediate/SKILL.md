---
name: orch-remediate
description: "Orchestrator role 6 (Remediator): fix the first open blocking finding in the ledger with a minimal diff, then mark it resolved."
disable-model-invocation: true
---

# Remediator

**The argument is the issue number.** Substitute it for `<issue>` in every path and command below. Your cwd is the target repo root; every path is relative to it.

You write a commit on the issue's branch and the `resolved` / `resolved_commit` fields of **one** finding in `.scratch/<issue>/ledger.json`. Every other file in `.scratch/` stays as you found it.

**One finding per invocation.** You fix the finding the ledger points you at, with the smallest diff that removes the defect, and then you stop — the CLI invokes you again for the next one.

## 1. Pick the finding

```sh
cat .scratch/<issue>/ledger.json
```

Take the **first** entry in `findings` order with `disposition == "blocking"` and `resolved == false`. That one, and no other, is your work; later blocking findings belong to later invocations.

When no entry matches, or `ledger.json` is missing, report "no open blocking finding" and stop without changing anything.

```sh
cat .scratch/<issue>/contract.md
cat .scratch/<issue>/run.json            # take branch and pr_number
git rev-parse --abbrev-ref HEAD          # git checkout <branch> when it differs
```

Done when you can state the finding's id, location, and the defect in one sentence, and you are on the issue's branch.

## 2. Fix it

Read the code at the finding's `location` and confirm the defect is there. Then change the smallest set of lines that removes it, inside the contract's `scope_paths`.

- The finding's statement is the whole brief. Adjacent code that looks improvable stays as it is; that is a follow-up, not this diff.
- Add a test only when it **demonstrates this defect**: it fails against the current code for the reason the finding names, and passes after your fix. Before adding one, count what the PR already added against the contract's `test_budget`:

  ```sh
  gh pr diff <pr_number> | grep -E '^[+]' | grep -Ev '^[+][+][+]' | grep -E '(def test_|func Test[A-Z]|@Test|\b(it|test)[[:space:]]*\()'
  ```

  With no room left in the budget, fix the defect without a new test.

Done when the defect is gone at its location and, when you added a test, that test failed before the fix and passes now.

## 3. Verify, commit, push

Run **every** command in the contract's `commands` from the repo root and get them all green:

```sh
sh -c "<commands.test>"
sh -c "<commands.lint>"
sh -c "<commands.typecheck>"
```

Stage **by path** so only your fix enters the commit:

```sh
git add <path> <path>
git commit -m "fix: <what changed> (<finding id>) (#<issue>)"
git push -u origin HEAD
git rev-parse HEAD
```

Done when every contract command passes at HEAD and HEAD matches the pushed branch tip.

## 4. Mark it resolved

In `.scratch/<issue>/ledger.json`, set `resolved: true` and `resolved_commit: "<the sha from step 3>"` on **that finding only**. Every other field, every other entry, and `rounds_completed` keep their existing values byte for byte.

Done when `ledger.json` parses, your finding carries `resolved: true` with a full 40-character sha, and no other entry changed.
