# Implementation decisions and deviations from `prototype-v0.md`

Recorded by the driver after an adversarial review of the module contract (2026-09-06). Each item
either resolves an ambiguity two implementers could read differently, or deviates from a design
sentence on purpose. Section numbers refer to `prototype-v0.md`.

## Clarifications (the design was ambiguous or circular)

1. **Stage records anchor on `start_commit`, not the artifact commit** (§7). A commit cannot contain its
   own sha, so `state.stages[s].start_commit` is HEAD when stage `s` began — which is exactly what
   `--force` on `s` rewinds to (for `spec`, the base sha). The artifact commit is the next commit that
   touches `state.json`; nothing uses it for control flow.
2. **`reviews[-1].sha` is the commit that was reviewed** (HEAD before the review-record commit), and
   "HEAD is the reviewed commit" means *no diff outside `work/` since that sha*. `run`'s review-vs-fix
   switch tests the same predicate rather than raw HEAD movement, so an `accept`/`dismiss` commit
   (which touches only `work/`) leads to `fix` instead of burning a review round. Deliberate divergence
   from §6's literal "HEAD moved since the last review".
3. **`park()` commits only `state.json`**, after committing any evidence file (e.g. the red baseline log)
   in its own commit. `outcome_sha` is HEAD before park's state-only commit. The parked predicate is
   `HEAD == outcome_sha` or (`parent(HEAD) == outcome_sha` and HEAD touched only `state.json`). It lives
   in `state.is_parked()` as a pure function shared by `run` and `poll`.
4. **`no_progress` is decidable from state**: `ReviewRecord.fix_rounds_at` records `fix_rounds` when the
   review ran; `state.no_progress()` compares the last two reviews.
5. **The RunLock spans the whole command**, not just the harness call, so a kill during the gate/commit
   window is still detected as an interrupted stage (§15).
6. **Every failure after a harness launch resets the worktree** before exiting 1, so "re-run the same
   command" (§15) never meets "worktree dirty".
7. **`finalize` runs the checks** (§11 "checks green → finalize") and commits `checks/finalize-1.log`; §9's
   table omitted it, which would let an operator hand-fix reach a ready PR no check ran over.
8. **Round index** for `spec`/`plan`/`build` is always 1; `review`/`fix` use their round.
9. **`prepare()` is called once per process by `cli.py`**; stages never call it. `status` does not call it at
   all (no fetch, no commit, no gh), and `abandon` never commits operator edits.
10. **`ensure_pr()`** re-finds or creates the draft PR whenever `state.pr` is missing (an interrupted `spec`
    must not leave a run with no PR and therefore no gate comments).
11. **The review diff is a file**, `.factory/tmp/review-<n>.diff` inside the worktree (as §9 says), not inline
    in the prompt (the agent's Read tool caps a single read at ~2000 lines). The prompt states its path,
    size and line count and instructs the reviewer to read it completely. Diffs above `max_diff_bytes`
    are truncated largest-file-first with a banner; the review record notes `diff_truncated`.
12. **Transient paths** (`.factory/`, `.venv/`, caches, ...) are written to the checkout's
    `.git/info/exclude` and ignored by every clean/dirty comparison, so the factory's own `make test`
    cannot brick a worktree and the rule does not depend on the target repo's `.gitignore`.
13. **Protected paths are checked twice**: on a write stage's own diff (§9) and over the whole branch
    (`base..HEAD`) before *every* session launches (§19), so an operator or fast-forwarded commit that
    adds `.claude/hooks` or `.mcp.json` stops the next session.
14. **Env allowlist** adds `SHELL`, `XDG_*`, proxy and CA-bundle variables (both binaries read them) and a
    `[factory] env_passthrough` list for repo toolchains. Checks get the harness env minus every provider
    key, as §8 says. `build_env` is constructed from allowlists only; a test asserts no
    `CLAUDE*/ANTHROPIC*/CODEX*/OPENAI*/GH_*/GITHUB_*` key leaks except the one selected provider key.
15. **Harness subprocesses stream to disk** in their own process group; a timeout kills the group. The
    transcript exists on every path. `capture_output` is never used.
16. **`permission_denials`** are recorded and logged, never a gate — a builder that adapts to a denied
    `Bash(ls)` is not a failed build. Extend the allowlist in `harness.py` when denials recur.
17. **`fix` at the round cap parks** with `rounds_exhausted` (exit 2), the same gate `run` raises.
18. **poll's failure counter is per HEAD**: an exit-1 at a different HEAD replaces the entry; the first
    time an issue is skipped as capped, one idempotent PR comment says so (§12 had no channel for exit 1).
19. **`doctor.json` is a map** keyed `<harness>:<auth>`, and probes run in a throwaway *worktree* so
    `.git`-is-a-file topology and the harness's directory-trust check are exercised.

## Flag choices verified against the installed CLIs

- Claude Code (2.1.263) always gets `--setting-sources user --strict-mcp-config`: the worktree's `.claude/`
  settings, hooks and `.mcp.json` are repository-controlled and must not configure the session that reviews
  that repository. This closes §19's "untrusted worktree" hole in both auth modes; the protected-path rule
  becomes defence in depth. `--max-turns` (read 30 / write 120) and optional `--max-budget-usd`.
- Codex (0.153.4) always gets `--skip-git-repo-check` (fresh worktree paths fail its trust check unless an
  ancestor is trusted). `api` mode adds `--ignore-user-config --ignore-rules`, the analogue of `--bare`.
  `[harness.codex] writable_dirs` become `--add-dir` roots so a sandboxed builder can write uv's cache.
- Both: `stdin=/dev/null` always (Codex otherwise blocks on a non-tty stdin).

## Deviations from the design text

- **Fake git is not used** (§17.2): tests use real git against a local bare origin; only `claude`, `codex`
  and `gh` are faked (`tests/FAKES.md`).
- **Extra config keys**, all defaulted: `max_nits`, `max_diff_bytes`, `transient_paths`, `env_passthrough`,
  `[harness.claude] max_turns_read/max_turns_write/max_budget_usd`, `[harness.codex] writable_dirs`.
- **Phase 2 is written but not exercised here**: `deploy/` and `image.yml` exist; this workstation's user is
  not in the `docker` group and no API key is present, so `api` auth, the container and the timer were
  not run. `subscription` auth was used for every live run.

## Round-1 integration decisions (fixer, 2026-09-06)

Four behaviours settled while reconciling the module implementations against the proof tests. Each is recorded
in the docstring of the function that carries it.

20. **"Parked stays parked" is per command, not only per `run`** (§11 names `run`). `build`, `review`, `fix` and
    `finalize` call `check_parked()` before doing anything, so re-raising a gate costs no check run, no harness
    call and no worktree dirt. `--force` (§11 lists it among the operator actions that re-open the loop) is
    accepted only by `spec`/`plan`/`build`, so only `build` guards its call with `if not ctx.force`.
21. **`park()`'s no-op re-park resets the worktree** before raising. A stage that wrote evidence before hitting
    the gate (build's `checks/build-1-baseline.log`) would otherwise leave it uncommitted, and the next command's
    `prepare()` would commit it as an operator edit — moving HEAD and un-parking the issue with no human action.
22. **`doctor.json` records carry `"ok": true`** and `doctor_record_is_current` requires it alongside the two
    version comparisons, so `poll`'s preflight cannot skip itself on a rescued or hand-written record. `doctor`
    still writes a record only on a pass.
23. **The plan/diff-sync gate matches whole tokens** (§9 "every changed path listed in `plan.md`"). A plan naming
    `src/app.py.bak` or `docs/src/app.py` no longer licenses a change to `src/app.py`; markdown around a path
    (backticks, brackets, a trailing comma or stop) is not part of it.

## Round-2 decisions (post code review, 2026-09-07)

An adversarial review of the implemented prototype found the loop could strand an issue an operator had
already unblocked, and that several host-local mechanisms (the pid lock, the poll lock, the failure counter,
`git clean`) rested on assumptions the unattended container does not honour. Each change below is recorded in
the docstring of the function that carries it.

24. **`RunLock` records who wrote it, not just a pid** (§7, §13). It gains `boot_id`/`pid_start` (default `""`),
    a `create(issue, stage, worktree, last_error=None)` classmethod that stamps pid, `started_at` and that
    identity, and `is_mine()`; a lock file written before the fields existed still loads and behaves exactly as
    before, because an empty identity field never contradicts a known one.
25. **`pid_alive()` requires `os.kill(pid, 0)` AND a matching identity** where both sides are known. Every poll
    tick runs in a fresh container on a disposable host, so pid 7 repeats: a recycled pid now reads as a dead
    writer (an interrupted stage to be discarded) instead of a live lock that would wedge the issue for ever.
26. **`RunLock.clear_if_owned(factory_dir, issue) -> bool`** unlinks only when the file is absent or `is_mine()`
    and returns whether the file is gone; it never raises, because its caller is `cli.py`'s `finally`, where a
    raise would replace the command's own error. `clear()` is kept for `abandon` and the tests.
27. **A second error channel survives a clean exit 1**: `last_error_path`, `write_last_error` (a blank message
    clears), `read_last_error` (None when absent, unreadable or blank) and `clear_last_error`, at
    `.factory/run/<issue>.last-error`. The split is now in `RunLock`'s docstring: the lock's own `last_error` is
    the interrupted (dead-pid) case, the file is the ordinary exit-1 case the lock cannot carry.
28. **`ReviewRecord.gated`** (default False, serialized via `asdict`, parsed with a default so older `state.json`
    files load) records that a review raised a review-loop gate, and `State.mark_last_review_gated()` sets it.
    The module docstring documents the predicate.
29. **New module helpers** `current_boot_id()`, `pid_start_time(pid)` (field 22 of `/proc/<pid>/stat`, counted
    after the last `)` because `comm` may contain spaces and parentheses) and the private `_same_identity`.
    Either value is `""` where `/proc` is unreadable, which means "this host cannot tell", never "mismatch".
30. **`poll.classify` syncs before it reads HEAD**: `repo.sync_with_remote(worktree, branch)` after
    `ensure_worktree`, and its return is the HEAD the classification uses (§12 "from local state after git
    fetch"). A commit pushed from elsewhere therefore decides the classification instead of being called parked;
    a diverged branch is the same per-issue error as before.
31. **The poll lock is an `fcntl.flock(LOCK_EX | LOCK_NB)`, not a pid file**: `acquire_poll_lock(factory_dir)`
    returns the open file that holds the lock (or None), `release_poll_lock(handle)` closes it, and
    `_create_lock`/`_lock_pid`/`_pid_alive` are gone. The kernel drops the lock when a killed container dies, so
    there is no stale lock to detect and liveness no longer depends on pids that repeat; the pid and
    `started_at` written inside are for a human reading the file, which is deliberately never unlinked.
32. **An exit-1 journal entry is compared against the HEAD the run STARTED from** (captured before `run_issue`)
    while still recording HEAD after the run (deviation 18). A stage that commits before failing can no longer
    reset its own per-HEAD cap every tick, and an operator's commit between two ticks still resets it.
33. **`tests/test_state.py`** adds: `create()` stamps the identity and round-trips; an older lock without the
    fields loads and clears; a recycled pid and a changed boot id read as dead and not mine; `is_mine()` is
    False for another live process; `clear_if_owned` clears mine, reports an absent lock as gone and refuses a
    foreign lock, a pid-reuse lock and an unreadable file; the last-error file round-trips, blank-clears and is
    per issue; `gated` round-trips and defaults to False in an older state file; `mark_last_review_gated()`
    marks only the latest review and no-ops with none.
34. **`tests/test_poll.py`** rewrites the lock tests for the handle API — exclusive in-process, the file is
    never unlinked, a leftover file naming a live foreign pid does not block, a dead-process file is acquired, a
    real subprocess holding the flock blocks until it is killed, a held lock makes `poll` exit 0, and the lock is
    free after the tick — and adds tests for `classify` fast-forwarding a strictly-ahead remote before reading
    HEAD, a diverged branch as a per-issue error, a failing run that commits still reaching the cap, and an
    operator commit still resetting the counter.
35. **`PROTECTED_PATHS` covers every name GNU make looks for** — `GNUmakefile`, `makefile`, `Makefile`, searched
    in that order with the first hit winning, so all three are "the gate" — and the new `PROTECTED_BASENAMES`
    (`AGENTS.md`, `CLAUDE.md`) match at ANY depth, because both CLIs load the instruction file of every
    directory they read. `is_protected` casefolds both sides (built-in and `config.protected_paths` entries), so
    `MAKEFILE` or `.CLAUDE/` cannot slip past.
36. **`allowed_edit_violations` has no `is_transient()` escape at all** (§9): the protected-path rule, build's
    `work/` rule and fix's `test_paths`+`work/` rules are evaluated over the path itself, and the call is gone
    rather than merely reordered — after the rules it could never change an outcome. `ignore` still
    short-circuits every rule; the docstring records why `is_transient` (deviation 12) must not double as an
    amnesty.
37. **The plan gate matches the changed path LITERALLY with a boundary test** instead of scanning path-shaped
    tokens, so a path containing spaces, brackets or quotes is matchable at all — a token scan split
    `src/my report.py` and failed every build that touched it. `./<path>` and a backslash-normalised copy of the
    plan are also tried; deviation 23 is preserved (`src/app.py.bak` and `docs/src/app.py` still do not list
    `src/app.py`) and a trailing sentence stop still counts as a boundary.
38. **`reset_hard` is `git reset --hard <ref>` + `git clean -fdx -e .factory/`**: ignored files
    (`.venv/bin/pytest`, `node_modules/.bin/*`, a stale `__pycache__`) no longer survive a rejected stage, so
    "the failed stage left nothing" means nothing. The worktree's own `.factory/` — the review diff a gate
    failure is still reporting on — survives.
39. **New `stage_all(wt)`** (`git add -A`, never a commit) **and `clean_ignored(wt, keep=('.factory/',))`**,
    which removes only IGNORED untracked files and directories and spares everything under `keep`, plus the
    module constant `FACTORY_DIR_ENTRY`. `git clean -fdX -e .factory/` cannot express this: under `-X` an `-e`
    pattern names something to remove, so the ignored set is listed first and filtered in Python.
40. **`run_streaming` kills the process group on ANY exception out of `wait()`**, not only `TimeoutExpired`: an
    `except BaseException: _kill_process_group(proc); raise` clause after the timeout handler. A Ctrl-C would
    otherwise orphan `claude` and its `make`/`pytest` grandchildren in the worktree — the one thing the next
    command's interrupted-stage recovery cannot clean up (§15).
41. **`DEFAULT_TOML` is the packaged template, read at import** from `Path(__file__).parent/'templates'/
    'factory.toml'` (new public `TEMPLATE_PATH`); a missing or unreadable file raises `FactoryError` naming it.
    One copy of the default config, so `factory init` can no longer install a file that lacks keys the factory
    has since grown.
42. **`templates/factory.toml` now holds the full documented content**: `max_diff_bytes`, `transient_paths`,
    `env_passthrough`, `[harness.claude] max_turns_read/max_turns_write/max_budget_usd` and
    `[harness.codex] writable_dirs` with its comment. This is the live `codex --add-dir` bug from the smoke run:
    the installed file had no `writable_dirs` line to edit.
43. **The write-probe line says what it proves** (§13). A new `_seeds_a_makefile(config)` is shared by
    `_seed_probe_check` and the new `_probe_check_scope`, so a green `make` probe reads "ran against a seeded
    no-op Makefile — proves the sandbox can exec make, not the repo's checks" and any other command reads "ran
    in the probe worktree, which holds no repository source". The module docstring records that every probe goes
    through `harness.get_harness(...).run(...)`, i.e. the argv builders the stages use.
44. **`tests/test_checks.py`** updates the `is_protected` parametrisations (`docs/AGENTS.md` is now protected;
    `GNUmakefile`/`makefile`/`MAKEFILE`, `.CLAUDE/settings.json`, `docs/agents.md` and a nested `CLAUDE.md`
    added; `sub/Makefile`, `docs/AGENTS.md.bak` and `docs/notes/AGENTS.mdx` kept unprotected), adds
    case-insensitive config entries and a GNU-make-names test, proves that a transient location cannot launder a
    protected, test or work path while `ignore` still wins, and adds plan-gate tests for paths with spaces plus
    two parametrised sets of markdown spellings that do and do not list a path.
45. **`tests/test_repo.py`** renames and extends the `reset_hard` test (an ignored `.venv/bin/pytest` and
    `src/__pycache__` are removed, `.factory/tmp/keep.diff` survives) and adds tests for `stage_all` (stages
    edits, adds and deletes, never commits) and `clean_ignored` (removes only ignored files, honours `keep`, and
    is a no-op when nothing is ignored).
46. **`tests/test_harness.py`** adds `test_run_streaming_kills_the_process_group_when_the_wait_is_interrupted`:
    a real sleeping child with a sleeping grandchild and a `Popen` subclass whose first `wait()` raises
    `KeyboardInterrupt` after a second. It asserts the interrupt propagates, the transcript survives and the
    grandchild pid is gone.
47. **`tests/test_config.py`** asserts `DEFAULT_TOML == prompts.load_template('factory.toml') ==
    TEMPLATE_PATH.read_text()` and `parse_config(DEFAULT_TOML) == Config()` (including both `HarnessConfig`
    defaults). A `CONFIG_KEYS` map asserts that every `dataclasses.fields(Config)` name is documented in the
    template by its TOML key — and fails when a field is added without one — and that every `HarnessConfig`
    field name appears there.
48. **`tests/test_doctor.py`** asserts the exact new make-probe line, adds a test that a non-make check's line
    says what it proves and mentions no seeding, and adds
    `test_the_probes_go_through_the_real_harness_argv_builder`, which drives `doctor` with a `ClaudeCode`
    subclass that feeds the probe kwargs into the real argv builder and asserts `--bare`,
    `--append-system-prompt-file`, `CLAUDE_ALWAYS`, the read and write tool lists and `--max-turns 120`.
49. **`run()`'s review/fix loop tests staleness FIRST** (§11 "only your action re-opens the loop"). `_review_is_stale`
    — `reviews[-1].gated` OR `repo.code_changed_between(reviews[-1].sha, HEAD)` — reviews and continues, and only
    a fresh review reaches `no_progress` -> park, `fix_rounds >= max` -> park, else fix + review. An operator
    action therefore buys exactly one review instead of re-raising the gate it just answered.
50. **`run()` reviews code the last review never saw before finalizing**, in an outer loop around the review/fix
    loop (`_unreviewed_code`, code-changed only: a `gated` flag with no open findings is not a reason to review),
    and re-enters the loop if that reopened Important findings. This is what stops the "operator commit after the
    last review -> exit 1 for ever" trap.
51. **`park()` marks the review that raised the gate** with `state.mark_last_review_gated()` for the new module
    constant `REVIEW_LOOP_GATES` (`no_progress`, `rounds_exhausted`). It is set after the evidence commit, so the
    flag travels in park's state-only commit and the issue still reads as parked (deviation 3).
52. **"already finalized" is `outcome == "done"` AND no code change since `outcome_sha`**, not `outcome == "done"`
    alone. An operator commit after a finished run is re-checked and re-finalized rather than waved through by the
    idempotent `gh` half.
53. **`Context.holds_lock`** records that THIS process wrote `.factory/run/<issue>.json`; `_take_lock` and
    `_run_lock` set it, and both build locks with `RunLock.create` so the pid identity is stamped. A hand-built
    lock would silently degrade `is_mine()` and `pid_alive()` to the bare pid.
54. **`_take_lock()` decides ownership with `existing.is_mine()`**, not `pid == os.getpid()`. A live foreign lock
    raises with `holds_lock` still False, so no `finally` can delete it, and the dead-writer path resets and
    carries the lock's `last_error` as before — now only when it is non-empty, so an empty field cannot erase the
    file-borne message.
55. **The `finally`s clear only a lock this process owns, and never after an interrupt**: `run_issue()` and
    `cli._issue_command()` call `RunLock.clear_if_owned` when `ctx.holds_lock`, guarded by a flag set in an
    `except KeyboardInterrupt: raise`. `cli.main` prints "error: interrupted; the next command will discard the
    partial stage" and returns 1 — the dying pid IS the interrupted-stage signal (§7, §15).
56. **The last-error channel is wired through the stages**: `_remember_failure` writes both the `RunLock` field
    and `state.write_last_error`, and `_gate_failure` and `run_harness_stage`'s post-launch handler use it.
    `prepare()` reads `state.read_last_error` into `ctx.last_error` before taking the lock (an interrupted lock's
    message wins) then clears the file once the lock has captured it; `commit_and_push` clears both on a
    successful push, and `abandon`'s `_forget_issue` removes the file too.
57. **`_after_the_session(ctx)`** is a context manager wrapping every post-`run_harness_stage` block in `build`,
    `fix`, `review` and `finalize`: any `FactoryError` in that window — the ledger's `merged_copy` rejecting the
    reviewer's output, an unwritable artifact, a mis-spelled check command, a failed push — goes through
    `_gate_failure` (reset, record, exit 1). `GateViolation` passes through untouched, to avoid a second reset.
58. **`_run_checks()` stages and de-lints the tree first**: `repo.stage_all(wt)` then `repo.clean_ignored(wt)`
    before every check run (baseline, build, fix and finalize alike), so ignored tooling a session may have
    tampered with — `.venv/bin/pytest`, a stale `__pycache__` — is gone and the toolchain rebuilds it. `.factory/`
    is kept; the docstring records why.
59. **`_spec_base_sha(ctx, rewound=)`: only the branch-CREATING path reads `origin/<base>`.** An existing branch
    with no `state.json` (an interrupted first `spec`, re-run) bases on `origin/<base>` when it is still an
    ancestor of the tip and otherwise on the tip itself, so the run never anchors on a commit the branch never
    contained and `git diff base HEAD` cannot report the base branch's own commits as this run's.
60. **`rewind_for_force()` publishes the rewind immediately**, pushing with `force_with_lease=True` when the
    remote branch exists, and still leaves `ctx.force_push = True` for the stage's own final push. A `--force`
    stage that then fails a gate now leaves `origin` == the local rewound tip, so the next plain command is an
    ordinary fast-forward.
61. **`prepare()` gains `strict_clean` and `check_protected`**, skips `_push_if_ahead` until `work/<n>/state.json`
    exists (an empty `factory/<n>` is never published), and runs the whole-branch protected-path check at the end
    once state is loaded — the new shared helper `_refuse_protected_paths`, also used per launch by
    `run_harness_stage`. The commands that run no session (build's baseline checks, finalize's checks) would
    otherwise execute a `Makefile` a pushed or hand-written commit had rewritten (deviation 13).
62. **`_commit_operator_edits()` takes `strict_clean`**: with it False a dirty path outside `work/` is not an
    error, and the operator-edit commit is driven by the under-`work/` subset only, so a commit is never attempted
    with nothing under `work/` to commit.
63. **`_log_harness_result()` omits "(N turns" when `result.num_turns` is 0 or unknown** — codex reports none, and
    "0 turns" is a fact the transcript contradicts.
64. **`abandon` prepares for a teardown**: `need_state=False, commit_operator_edits=False, fetch=False,
    strict_clean=False, check_protected=False`. The command that exists to clean up cannot be the one that refuses
    to start, so a diverged remote, a dirty worktree and a protected-path change on the branch no longer block it.
65. **`tests/test_e2e_review_fixes.py`** is a new file of seven proofs driven entirely through `run_cli` and the
    fakes: `no_progress` + a hand commit is reviewed, not re-parked; `rounds_exhausted` + a `dismiss` (no code
    change) is reviewed, not re-parked; a hand commit after a finished run is reviewed and re-finalized rather than
    exiting 1; a `build --force` that fails a gate leaves `origin` == the rewound tip and the next plain build
    works; a lock held by a real live foreign process exits 1 and survives, then reads as interrupted once that
    process dies; a gate failure's message reaches the next attempt's build prompt and the file is cleared on
    success; and `abandon` tears down a genuinely diverged branch.
66. **`tests/test_stages_unit.py`** adds tests for lock ownership (`holds_lock` False and the lock untouched on a
    live foreign lock; the taken lock is mine), the last-error channel (carried into `_stage_note`, the file
    consumed but the lock carrying it, an interrupted lock outranking an older exit 1, `commit_and_push` retiring
    it), `_after_the_session` (resets and records, `GateViolation` not reset twice, a review whose ledger merge
    rejects a title-less finding), `_run_checks` on a pristine tree, `_spec_base_sha` (create vs existing branch
    after main moved), the parametrised turn-count progress line, prepare's protected-path refusal, abandon's
    policy starting where the others refuse, and the no-push of a stateless branch with its counterpart. `StubHarness`
    gained a `num_turns` knob and `prepared_for_harness` now takes the lock the way `prepare` does.
67. **`tests/test_stages_parked.py`** asserts that `park` marks `reviews[-1].gated` for `no_progress` and
    `rounds_exhausted` (parametrised over `stages.REVIEW_LOOP_GATES`) and leaves it alone for `open_questions` and
    `baseline_failing`; both check that the flag landed in the committed state and that the issue still reads as
    parked.
68. **`tests/test_cli.py`** updates `abandon`'s prepare policy and takes the lock the way `prepare` does in the
    lock-clearing test, and adds tests that a lock this process does not own is never cleared and that a
    `KeyboardInterrupt` leaves the lock in place and prints the new message.
