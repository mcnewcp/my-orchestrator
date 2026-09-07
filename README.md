# software-factory (v0 prototype)

One bounded GitHub issue in, one pull request ready for human review out. A single Python CLI
(`factory`) drives Claude Code or Codex as interchangeable headless harnesses through
spec → plan → build → review ⇄ fix → finalize, committing every artifact on a `factory/<issue>`
branch. Python owns every commit, push, check run and verdict; the agents never touch git or GitHub.

- Design: [`docs/design/prototype-v0.md`](docs/design/prototype-v0.md) (the spec this implements)
- Decisions and deviations made while implementing: [`docs/design/deviations.md`](docs/design/deviations.md)
- Harness behaviour verified on this machine: [`docs/design/harness-smoke-2026-09-06.md`](docs/design/harness-smoke-2026-09-06.md)
- Test doubles: [`tests/FAKES.md`](tests/FAKES.md)

## Prerequisites (versions this was verified with)

Python ≥ 3.12 (3.14.7 used), `uv`, `git`, `gh` (2.100, logged in), `make`, and at least one of
`claude` (2.1.263) / `codex` (0.153.4), each logged in with its own subscription. No API key is
needed for attended runs. Runtime is stdlib only.

## 1. Install and run the test suite

```bash
cd ~/personal/factory-prototype-cc
uv sync --group dev
uv run pytest            # 769 tests: unit tests per module + end-to-end proofs with fake claude/codex/gh
uv run ruff check src tests
uv tool install --editable . --force   # puts `factory` on PATH (~/.local/bin/factory)
factory version
```

The end-to-end suite (`tests/test_e2e_*.py`) proves every item in design §17.2 against fake
binaries and a real local git origin: red checks cannot push, protected-path and test-file edits
fail the stage, `fix` refuses after a hand commit, `finalize` refuses with open Important findings,
dismissed findings cannot be re-raised, the no-progress and round-cap rules terminate, an
interrupted stage re-runs without a duplicate PR, `api` mode without a key never touches a saved
login, a parked issue makes no harness call, `poll` skips parked and done issues, and a deleted
`.factory/` resumes from the remote branch. The rest of §17.6 (a capped issue is skipped with one
PR comment, two `poll`s cannot overlap) is proven in `tests/test_poll.py`, which drives `poll`
against real git with a stub harness and an injected runner instead of the fake binaries.

## 2. Try it live against the fixture repo

`mcnewcp/my-team-fixtures` is a disposable target: a small `textkit` Python package with
`make test` / `make lint`, `AGENTS.md`, and `factory init` already applied on `main`
(`factory.toml` with `auth = "subscription"`, `REVIEW.md`, the intent issue template, the `factory` label).

```bash
cd ~/personal/my-team-fixtures && git pull
factory doctor                      # claude, subscription: binaries, gh auth, read + write probe in a real worktree
factory doctor --harness codex
```

Open issues left untouched for you to run: **#7** (`--format json` for `textkit stats`) and
**#9** (document the CLI in README). Then:

```bash
factory run 7                       # claude: spec → draft PR → plan → build → review → finalize; exit 0
factory status 7                    # local state only, no network
factory run 9 --harness codex       # the same pipeline on codex
factory review 7 --harness codex    # cross-model review: one more round on the finished branch
```

Write your own issue with the **Intent** template (it applies the `factory` label) and run
`factory run <n>`. To see the open-questions gate, write an issue whose questions the spec cannot
answer from the repo (issue **#13** is the worked example, see §3): `factory run <n>` stops with
exit 2 (`open_questions`) and a gate comment on the draft PR; re-running is a no-op until you act.
Answer the questions by editing `work/<n>/spec.md` in `.factory/worktrees/<n>/` (the next command
commits your edit as "operator edits"), then `factory accept <n> && factory run <n>`.

Operator commands: `factory status N` · `factory accept N` · `factory dismiss N F3 "reason"` ·
`factory {spec,plan,build} N --force` (rewind + redo) · `factory abandon N` (label, PR, branch,
worktree). Exit codes: 0 done/continue, 1 failed (fix and re-run the same command), 2 needs you.
Transcripts, worktrees and locks live under the target repo's `.factory/` (gitignored, disposable).

## 3. What was dogfooded (2026-09-06/07, attended, subscription auth)

| issue | harness | result |
|---|---|---|
| #5 `truncate()` | claude | spec, plan, build (41 tests green), review (0 findings), finalize → [PR #10](https://github.com/mcnewcp/my-team-fixtures/pull/10) ready, 6 min |
| #6 `word_count` punctuation | codex | spec, plan, build (builder recorded a plan deviation about uv's cache), review 1 returned an **empty ledger without reviewing** (see §5), finalize → [PR #11](https://github.com/mcnewcp/my-team-fixtures/pull/11) marked ready on an unreviewed diff, 8.5 min. After the fix, `factory review 6 --harness codex` ran round 2: the reviewer `cat`-read all 116 diff lines, ran the three passes, `complete: true`, 0 findings, comment on PR #11, 39 s |
| #8 configurable `slugify` (under-specified) | claude | the spec turned the open questions into stated assumptions (allowed by the role), so no gate fired; build, review (2 nits in the ledger, no Important), finalize → [PR #12](https://github.com/mcnewcp/my-team-fixtures/pull/12) ready, 12 min |
| #13 stopword filtering (questions marked blocking) | claude | spec asked 3 questions → draft [PR #14](https://github.com/mcnewcp/my-team-fixtures/pull/14), parked, exit 2 with the gate comment; `factory run 13` again → exit 2, no harness call; answers written into `work/13/spec.md`, `factory accept 13`, `factory run 13` → plan, build (52 turns, 7 `dontAsk` denials logged), review (2 nits), finalize → PR #14 ready, 7 min |

Each stage was one fresh headless session: 8–52 turns and $0.8–$2.2 of list price per stage on the
claude rows (Fable default model; codex reports no turn count). The ledger, gate and summary
comments landed on the PRs as designed.

## 4. Not exercised here

- `auth = "api"` (no `ANTHROPIC_API_KEY` / `CODEX_API_KEY` on this workstation). `doctor --auth api`
  correctly fails before any harness call; the `--bare` path is covered by tests with the real
  transcript of a `--bare`-without-key failure.
- Phase 2: `deploy/` (Dockerfile, `factory-host`, systemd units) and `.github/workflows/image.yml`
  are written per design §13 but not run — this user is not in the `docker` group. `poll` is
  proven only with fakes.
- Claude Code's `dontAsk` allowlist denies compound shell commands (pipes, `$VAR`); the builder
  adapts, and denials are logged per stage (`permission_denials` in `state.json`), never a gate.
- Subscription limits surface as an ordinary harness failure. When the claude.ai session limit was
  hit mid-evening, `factory spec 13` exited 1 in 3 s with the CLI's own message ("You've hit your
  session limit · resets 2am") and the transcript path; nothing was committed and re-running the
  same command after the reset picked up cleanly.

## 5. What dogfooding found (and what changed because of it)

- **A reviewer that could not read the diff was treated as a clean review.** The codex review
  session for #6 returned `updates: [], new: []` with a summary saying it was blocked: the review
  role said "Do not run shell commands. Read files with your file-reading tool only", which is
  right for Claude Code in read mode (Bash disallowed) and impossible for Codex, whose only reader
  is the read-only sandboxed shell. The empty ledger satisfied every gate and PR #11 was marked
  ready on an unreviewed diff. Fix (deviations 69–70): the spec, plan and review roles now give
  harness-neutral read-only guidance, the review schema has a required `complete` boolean, and
  `factory review` rejects a round with `complete: false` before anything is saved (exit 1, the
  summary in the error, the next attempt's stage note carries it). `factory review 6 --harness codex`
  re-run after the fix produced a real round 2 (see the #6 row); the round-1 ledger stays in the
  history as the record of what happened.
- **The open-questions gate depends on the spec role's judgement.** Issue #8 was written to be
  under-specified but the spec answered its own questions as stated assumptions, which the role
  allows; only an issue that says the maintainer has not decided (#13) fired the gate.
- **`dontAsk` denials are frequent but harmless.** The #13 build hit 7 denials (compound shell
  commands) and still finished green in 52 turns; they are recorded per stage, never a gate.

## Layout

```
src/factory/   cli config errors schema state repo gh harness checks prompts stages doctor initcmd poll
               roles/*.md  schemas/*.json  templates/{factory.toml,REVIEW.md,intent.md}
tests/         unit tests per module, fakes/{claude,codex,gh}, test_e2e_*.py proofs, fixtures/real-transcripts
deploy/        Dockerfile factory-host factory-poll@.service factory-poll@.timer README.md
```
