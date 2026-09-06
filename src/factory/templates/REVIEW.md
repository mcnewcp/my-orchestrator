# Review policy

The default policy installed by `factory init`. Edit it in your repository; the reviewer applies
whatever this file says.

Review the diff, not the repository. A defect the diff does not touch is out of scope.

## Passes

Run all three, in order, and attribute every finding to one of them.

1. **bugs** — Correctness. Wrong logic, wrong boundary, unhandled error or exception path, `None`
   or empty-collection cases, off-by-one, resource or file-handle leaks, concurrency and ordering
   assumptions, a test that cannot fail, a test that asserts the implementation instead of the
   behaviour.
2. **security** — Injection (shell, SQL, path traversal), unvalidated external input, secrets or
   tokens in code, logs or error messages, credentials or environment leaking into subprocesses,
   unsafe deserialization, permissions widened, a check or sandbox weakened.
3. **compliance** — Against `spec.md` and `plan.md`. An acceptance criterion not met, behaviour
   the spec did not ask for, a file changed that the plan does not list, a proof command the plan
   promised and the build did not run, a stale plan after a deviation.

## Important vs nit

**Important** — the change is wrong, unsafe, or does not do what the spec and plan say. You can
name the failing input or condition and its consequence. It blocks the PR and a fixer will change
code for it. When in doubt between important and nit, and you cannot name the failure, it is a nit.

**nit** — everything else worth saying: clarity, a missing test for a secondary case, a comment
that no longer matches the code, a simpler equivalent. Nits never block and are never auto-fixed.

**Nit cap: 10 per round.** Exceeding it fails the round. Keep the most useful ones.

## Evidence

Every finding, important or nit, carries: the file path, the line in the new version (or `null`
when it is not tied to one line), and a quoted snippet from the diff. `detail` says what goes
wrong, under what input, with what consequence. No evidence, no finding.

Every finding already in the ledger and marked NEEDS UPDATE gets an update this round: `resolved`
with a quoted snippet from the diff that shows the fix, or `unresolved` with what is still wrong.
Never re-raise a finding recorded as resolved or dismissed; only a genuine regression visible in
this diff may be raised again, with evidence of the regression.

## Skip list

Do not report: formatting, whitespace, line length, import order; naming preferences; anything the
repository's linter, formatter, or type checker already enforces; architecture or dependency
choices the plan settled; pre-existing issues the diff does not touch; requests for more tests
when the plan's proof is satisfied; praise.
