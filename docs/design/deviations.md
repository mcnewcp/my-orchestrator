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
