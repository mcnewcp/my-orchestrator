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
uv run pytest            # 676 tests: unit tests per module + end-to-end proofs with fake claude/codex/gh
uv run ruff check src tests
uv tool install --editable . --force   # puts `factory` on PATH (~/.local/bin/factory)
factory version
```

The end-to-end suite (`tests/test_e2e_*.py`) proves every item in design §17.2 and §17.6 against
fake binaries and a real local git origin: red checks cannot push, protected-path and test-file
edits fail the stage, `fix` refuses after a hand commit, `finalize` refuses with open findings,
dismissed findings cannot be re-raised, the no-progress and round-cap rules terminate, an
interrupted stage re-runs without a duplicate PR, `api` mode without a key never touches a saved
login, a parked issue makes no harness call, and `poll` skips parked/done/capped issues.

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
`factory run <n>`. Issue **#8** is deliberately under-specified: `factory run 8` stops with exit 2
(`open_questions`) and a PR comment; edit `work/8/spec.md` on the branch (or just decide), then
`factory accept 8 && factory run 8`.

Operator commands: `factory status N` · `factory accept N` · `factory dismiss N F3 "reason"` ·
`factory {spec,plan,build} N --force` (rewind + redo) · `factory abandon N` (label, PR, branch,
worktree). Exit codes: 0 done/continue, 1 failed (fix and re-run the same command), 2 needs you.
Transcripts, worktrees and locks live under the target repo's `.factory/` (gitignored, disposable).

## 3. What was dogfooded (2026-09-06/07, attended, subscription auth)

| issue | harness | result |
|---|---|---|
| #5 `truncate()` | claude | spec, plan, build (41 tests green), review (0 findings), finalize → [PR #10](https://github.com/mcnewcp/my-team-fixtures/pull/10) ready, 6 min |
| #6 `word_count` punctuation | codex | spec, plan, build (builder recorded a plan deviation about uv's cache), review (0 findings), finalize → [PR #11](https://github.com/mcnewcp/my-team-fixtures/pull/11) ready, 8.5 min |
| #8 configurable `slugify` (under-specified) | claude | the spec turned the open questions into stated assumptions (allowed by the role), so no gate fired; build, review (2 nits in the ledger, no Important), finalize → [PR #12](https://github.com/mcnewcp/my-team-fixtures/pull/12) ready, 12 min |

Each stage was one fresh headless session (12–18 turns, ≈$1 list price each on the Fable default
model); the ledger, gate and summary comments landed on the PR as designed.

## 4. Not exercised here

- `auth = "api"` (no `ANTHROPIC_API_KEY` / `CODEX_API_KEY` on this workstation). `doctor --auth api`
  correctly fails before any harness call; the `--bare` path is covered by tests with the real
  transcript of a `--bare`-without-key failure.
- Phase 2: `deploy/` (Dockerfile, `factory-host`, systemd units) and `.github/workflows/image.yml`
  are written per design §13 but not run — this user is not in the `docker` group. `poll` is
  proven only with fakes.
- Claude Code's `dontAsk` allowlist denies compound shell commands (pipes, `$VAR`); the builder
  adapts, and denials are logged per stage (`permission_denials` in `state.json`), never a gate.

## Layout

```
src/factory/   cli config errors schema state repo gh harness checks prompts stages doctor initcmd poll
               roles/*.md  schemas/*.json  templates/{factory.toml,REVIEW.md,intent.md}
tests/         unit tests per module, fakes/{claude,codex,gh}, test_e2e_*.py proofs, fixtures/real-transcripts
deploy/        Dockerfile factory-host factory-poll@.service factory-poll@.timer README.md
```
