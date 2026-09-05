---
name: orch-implement
description: "Orchestrator role 2 (Implementer): build the contract for an issue test-first on a fresh branch and open a draft PR."
disable-model-invocation: true
---

# Implementer

**The argument is the issue number.** Substitute it for `<issue>` in every path and command below. Your cwd is the target repo root; every path is relative to it.

You write commits on the issue's branch, a draft PR, and the `branch` / `pr_number` / `pr_url` fields of `.scratch/<issue>/run.json`. Leave every other file in `.scratch/` unchanged.

The **contract** is the definition of done. Build what it asks for and stop there: quality is produced here, and every line beyond the contract is a line the next reviewer comments on.

## 1. Read the contract

```sh
cat .scratch/<issue>/contract.md
```

From the front matter take `title`, `test_budget`, `scope_paths`, and `commands`; from the body take the acceptance criteria with their `Verified by:` test names.

Done when you can name, for each AC, the test you will write and the file it goes in.

## 2. Read the latest audit, if there is one

```sh
find .scratch/<issue> -maxdepth 1 -name 'audit-*.json' | sed -E 's/.*audit-([0-9]+)\.json/\1/' | sort -n | tail -1
cat .scratch/<issue>/audit-<n>.json     # the highest-numbered one
```

Nothing printed means this is the first invocation: go to step 3 and build from scratch.

When that audit's `pass` is **false**, you are here to clear it: its `failures` and its failing `checks` are the whole brief for this invocation, and the branch and the PR already exist (`run.json` carries them), so steps 3 and 6 are skipped — `git checkout` the branch named in `run.json` and go straight to the fixes.

- `scope.out_of_scope_files` — restore each one from the default branch (`git checkout "origin/$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name)" -- <path>`), or `git rm` it when the PR added it.
- `test_budget.pass` false — delete the surplus tests, keeping the ones the acceptance criteria name.
- `criteria_coverage` entries with `covered: false` — write or rename tests so every `Verified by:` name exists and exercises its criterion.
- `commands` values of `fail` — make that command pass, starting from the matching `failures` string.

Done when you can list the failures this invocation must clear, or you have established that there is no audit yet.

## 3. Create the branch

```sh
title=$(sed -n 's/^title: "\(.*\)".*$/\1/p' .scratch/<issue>/contract.md | sed 's/\\"/"/g')
default=$(gh repo view --json defaultBranchRef --jq .defaultBranchRef.name)
git fetch origin "$default"
slug=$(printf '%s' "$title" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' | cut -c1-40)
git checkout -b "issue-<issue>/$slug" "origin/$default"
```

Read the title out of the contract into `$title` like that rather than pasting it into the command line: a title carrying `$`, a backtick, or a quote otherwise corrupts the slug or runs as shell. Each command block below runs in its own shell, so later blocks name the branch as `HEAD` rather than reusing `$slug`.

When that branch already exists, `git checkout "issue-<issue>/$slug"` instead and continue the work already on it. When `run.json` already carries a `pr_number`, the PR exists too: keep using it rather than opening a second one.

Write the branch name into `run.json` now, so a crash later still leaves the branch discoverable (step 7 gives the file's shape).

Done when `git rev-parse --abbrev-ref HEAD` prints `issue-<issue>/<slug>` and `run.json` carries it.

## 4. Build it, one criterion at a time

The contract's `Verified by:` names are the **pre-agreed seams** — test there, under those names, and nowhere else. Work one AC at a time, top to bottom, in vertical slices:

1. Write the named failing test. Run the contract's `test` command narrowed to that file, and read the failure: it must fail for the reason the criterion describes, not on an import or a typo. That is **red**.
2. Write the minimum code that makes it pass. That is **green**. Add nothing the criterion did not ask for.
3. Move to the next criterion.

Rules that bind every slice:

- **Stay inside `scope_paths`.** A criterion that seems to need a file outside them is a signal the contract is wrong: implement what you can, and say so in the PR body rather than widening the change.
- **Stay under `test_budget`.** Count the tests you add as you go; the budget is the ceiling for the whole PR, and the auditor enforces it.
- **Test through the public interface**, never a private helper or a side channel.
- **Expected values come from an independent source** — the issue, a worked example, a known-good literal. A test that recomputes the answer the way the code computes it passes by construction and can never disagree with the code.
- Writing all the tests first (**horizontal slicing**) verifies imagined behaviour. One test, one implementation, repeat.

Done when every AC has its named test and that test passes.

## 5. Verify and commit

Run **every** command in the contract's `commands`, from the repo root, and get them all passing before the commit:

```sh
sh -c "<commands.test>"
sh -c "<commands.lint>"
sh -c "<commands.typecheck>"
```

Then stage **by path** — the files you wrote, plus `.gitignore` when it differs (it carries the `.scratch/` entry) — so nothing else can ride along:

```sh
git add <path> <path> ...
git commit -m "<what changed> (#<issue>)"
git push -u origin HEAD
```

Several commits are fine; every one of them keeps the commands green.

Done when `git status --porcelain` is empty of the files you touched, every contract command passes at HEAD, and `git rev-parse HEAD` matches `git rev-parse @{upstream}`.

## 6. Open the draft PR

```sh
title=$(sed -n 's/^title: "\(.*\)".*$/\1/p' .scratch/<issue>/contract.md | sed 's/\\"/"/g')
gh pr create --draft --title "$title (#<issue>)" --body-file - <<'BODY'
Closes #<issue>

## Acceptance criteria
- **AC-1** — <statement>. Verified by: `test_name_a`
- **AC-2** — <statement>. Verified by: `test_name_c`

## Notes
<anything the contract could not cover, or "none">
BODY
```

`gh pr create` takes the current branch as head and the repo's default branch as base. The PR stays a **draft**; the CLI flips it to ready at the end of the run.

Done when `gh pr view --json number,isDraft` shows the PR and `isDraft: true`.

## 7. Record the identifiers

Rewrite `.scratch/<issue>/run.json` with all five keys, keeping the existing `created_at`:

```json
{"issue": 17, "branch": "issue-17/add-percent-change-helper", "pr_number": 42, "pr_url": "https://github.com/owner/repo/pull/42", "created_at": "2026-09-03T20:15:00Z"}
```

`pr_number` is an integer with no quotes. Read the values back from GitHub rather than from memory:

```sh
gh pr view --json number,url
```

When `run.json` is missing entirely, write it with these five keys and `created_at` from `date -u +%Y-%m-%dT%H:%M:%SZ`.

Done when `run.json` parses as JSON and its `branch`, `pr_number`, and `pr_url` are all non-null.
