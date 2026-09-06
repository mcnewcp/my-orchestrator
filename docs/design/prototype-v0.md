# Factory v0 — Design E

**Status:** proposed · **Owner:** Coy · **Scope:** prototype; one CLI, attended on a workstation (phase 1), then unattended on a dedicated Linux host (phase 2) · **Verification:** Claude Code headless auth behavior checked against the headless docs on September 5, 2026; every other harness flag is gated by `doctor` (§8, §19).

The factory turns one bounded GitHub issue into one PR ready for human review, using Claude Code or Codex as interchangeable headless harnesses. It is a CLI run from a checkout: local state lives on the branch, Python owns every write and every verdict, and the same command line runs attended on a workstation or unattended on a dedicated host, where it is packaged as a container image, started by a systemd timer, authenticated by API key, and scheduled by `poll`. There is no service, daemon, webhook, database, or queue. Two rules follow from that shape and recur throughout: the host is disposable, and subscription auth is the attended mode. The stage chain implements a bounded portion of Anthropic's AI-native SDLC playbook (see References).

## 1. What v0 does

One GitHub issue in, one PR ready for human review out, with the playbook's artifact chain committed on the branch:

```
issue #42 ─▶ intent.md ─▶ spec.md ─▶ plan.md ─▶ code + tests ─▶ review ⇄ fix ─▶ PR ready
             (snapshot)   (draft PR)              (checks)        (≤ 3 rounds)   (finalize)
```

Every arrow is one fresh, headless harness session (Claude Code or Codex, chosen per run) with one role prompt and one output schema. The factory is the plumbing between arrows: render the prompt, launch the harness, validate the output, run the checks, commit, push, write to GitHub.

Six rules hold everywhere:

1. **The committed artifact is the handoff.** Each stage reads the previous stage's files and commits its own. The branch is the audit trail.
2. **Local state is authoritative; GitHub is a write target.** The factory decides what happens next from `work/<issue>/state.json`, never from the issue, PR, labels, reviews, or checks (§7). `poll` reads one label to discover work and decides nothing else from it (§12).
3. **Python owns every write and every verdict.** Agents never commit, push, or touch GitHub. The factory runs the checks itself and does not accept an agent's claim that they passed.
4. **Verification loops; judgment is sampled.** Deterministic checks run as often as needed. Model review runs a fixed number of rounds, then stops.
5. **Burden of proof is on the finding.** Every finding carries evidence, every status change is recorded in the ledger, and an adjudicated finding cannot come back as new.
6. **The host is disposable.** Nothing needed to continue a run exists only on the machine that started it. Worktrees are rebuilt from `origin/factory/<issue>`; everything under `.factory/` is host-local and expendable.

## 2. The product owner's view

**Attended (phase 1), on the workstation.**

1. **Write the intent.** Open a GitHub issue from the intent template that `factory init` installs: problem, proposed outcome, affected users and systems, constraints, open questions. The issue body *is* `intent.md`. The template applies the `factory` label.
   **You:** required. The factory snapshots the issue once and never re-reads it.
2. **Start:** `factory run 42`.
3. **Spec.** The factory writes `work/42/spec.md` and opens a draft PR containing it. Open questions stop the run with exit 2.
   **You:** only if it stopped. Edit `spec.md` on the branch (or fix the issue and `factory spec 42 --force`), then `factory accept 42` and `factory run 42`.
4. **Plan → build → review ⇄ fix.** Autonomous. Deterministic checks between stages; at most three fix rounds.
5. **If it stops** (exit 2): baseline red, a fix round that resolved nothing, or rounds exhausted with Important findings open. `factory status 42` and a PR comment name the gate and what clears it.
   **You:** fix by hand and commit on the branch, `factory dismiss 42 F3 "reason"`, or edit `plan.md` and `factory build 42 --force`. Then `factory run 42`.
6. **Finalize.** The draft PR flips to ready with a summary comment: spec, plan, check output, ledger, harness and versions.
7. **Review and merge** on GitHub. Your CI runs as usual; branch protection requires your approval. The factory never merges.
8. **Deploy.** Merge fires your existing pipeline. Not factory scope.

**Unattended (phase 2), on the host.** Steps 2–6 happen on a timer for every open issue carrying the label. You write issues; a PR comment tells you when something stopped; you SSH in and run the same commands through the container wrapper (§13). Minimum touch is the same in both modes: write the issue, review the PR, merge.

## 3. Non-goals (v0)

- No service, daemon, webhook, GitHub App, reconciler, database, or queue. Unattended mode is the CLI under a systemd timer; `poll` is one-shot.
- No GitHub-side gating. Review states, check runs, CI status, and PR comments are neither consumed nor required. Exactly one label is consumed, by `poll` only, to select issues; it never affects which stage runs.
- No ADO, Jira, or GitLab. GitHub only, through `gh`.
- No harness hooks, skill discovery, MCP servers, subagents, Agent SDK, or app-server. Roles are prompt files the factory renders.
- No context-threshold handoff. Every stage is a fresh session; the committed artifacts are the handoff.
- No evals, security scans, or maintain loop.
- No isolation claim. The container is packaging and blast-radius reduction, not a security boundary (§16).
- No auto-merge, ever.

## 4. Assumptions

- Python 3.12+, `git`, `gh`, and at least one of `claude` / `codex` on PATH at versions `doctor` has smoke-tested. In phase 2 these come from the image.
- The target repo has one-command checks (default `make test`, `make lint`), each non-zero on failure, and an `AGENTS.md` (with `CLAUDE.md` containing `@AGENTS.md`).
- Issues are written by the product owner from the intent template.
- Branch protection on `main` requires a human approval. The factory pushes only to `factory/<issue>` branches.
- One harness session at a time per repo.
- Phase 2: a Linux host with Docker, an API key for the configured harness, and a fine-grained GitHub token scoped to the target repo.

## 5. Layout

The factory is its own package, installed once (or baked into an image) and run inside any target repo.

```
software-factory/                      # the tool — stdlib only at runtime
  pyproject.toml                       # console script: factory
  src/factory/
    cli.py        # argparse, exit codes
    state.py      # state.json, findings.json, transient files; atomic writes; ledger merge
    stages.py     # spec plan build review fix finalize run
    poll.py       # preflight, discovery, eligibility, sequential run (§12)
    harness.py    # Harness protocol; ClaudeCode; Codex
    checks.py     # configured checks, protected paths, allowed-edit rules, env filtering
    repo.py       # worktree, branch, commit, diff, reset, force-with-lease push
    gh.py         # issue snapshot and list; pr create/comment/ready/close; label create/remove (idempotent)
    roles/        # spec.md plan.md build.md review.md fix.md
    schemas/      # spec.json plan.json build.json review.json fix.json
  deploy/
    Dockerfile                         # factory base image (§13)
    factory-host                       # wrapper: run any factory command in the target image
    factory-poll@.service  factory-poll@.timer
  .github/workflows/image.yml          # build + push ghcr.io/<owner>/factory:<tag> on tag
  tests/          # fake claude / codex / gh / git binaries; small fixture repo
```

```
target-repo/
  AGENTS.md  CLAUDE.md  REVIEW.md  Makefile  factory.toml
  .devcontainer/Dockerfile             # FROM ghcr.io/<owner>/factory:<tag> + repo toolchain (phase 2)
  .github/ISSUE_TEMPLATE/intent.md     # written by `factory init`; applies the `factory` label
  work/<issue>/                        # committed on the branch
    intent.md        # issue snapshot: number, url, title, labels, sha256, timestamp
    spec.md  plan.md
    state.json       # §7
    findings.json    # §7, the ledger
    review-<n>.json  fix-<n>.json      # raw harness output per round
    prompts/<stage>-<n>.md             # the exact prompt that produced each artifact
    checks/<stage>-<n>.log             # the exact check output
  .factory/                            # gitignored, host-local: worktrees/ transcripts/ tmp/ run/ poll.json doctor.json
```

## 6. CLI

```
factory init                  # factory.toml, REVIEW.md, issue template, label if absent; .factory/ into .gitignore
factory doctor                # binaries, versions vs pins, gh auth, per-harness auth + write-mode probe
factory spec     42           # snapshot issue → intent.md; spec.md; branch + worktree; draft PR
factory accept   42           # record that you resolved spec.md's open questions
factory plan     42
factory build    42
factory review   42           # --harness codex here = cross-model review
factory fix      42
factory finalize 42           # PR ready + summary comment
factory run      42           # all remaining stages; stops at gates with exit 2
factory poll                  # run every eligible labeled issue in turn; one-shot (§12)
factory status   42           # from local state only; no network
factory dismiss  42 F3 "why"  # adjudicate a finding; recorded in the ledger
factory abandon  42           # remove the label, close the draft PR, delete branch and worktree — in that order
```

Global flags: `--harness claude|codex`, `--auth subscription|api`, `--model`, `--force`. Defaults come from `factory.toml`. `poll` accepts none of them; it uses the config.

Exit codes: `0` done or continue; `1` failed (harness error, check failed, invariant violated; fix and re-run the same command); `2` needs human (a gate). `poll` exits `1` if any run it started exited `1`, else `0`.

Every command is idempotent and re-runnable. `review` always runs a new round. `fix` runs only while Important findings are open and HEAD is the reviewed commit. `run` resumes at the first incomplete stage; with Important findings open it runs `review` if HEAD moved since the last review, otherwise `fix`. `--force` on `spec`, `plan`, or `build` rewinds the branch to the last commit of the previous stage (for `spec`, to base), re-applies your edits under `work/42/`, re-runs the stage, and pushes with `--force-with-lease`; the PR is a draft, so rewriting it is acceptable.

## 7. Local state

Two files on the branch, owned by the factory, plus host-local transients.

**`work/<issue>/state.json`** — what has happened. Written atomically in the worktree and committed with each stage's artifact, so it always describes completed stages. Illustrative:

```json
{
  "issue":   {"number": 42, "snapshot_sha256": "…", "snapshot_at": "…"},
  "base":    {"branch": "main", "sha": "…"},
  "branch":  "factory/42",
  "stages":  {"spec":  {"commit": "…", "at": "…", "harness": "claude", "model": "…",
                        "cli_version": "…", "auth": "api"},
              "plan":  {}, "build": {}},
  "spec_open_questions": ["…"],
  "spec_accepted": {"by": "auto | operator", "at": "…"},
  "reviews": [{"round": 1, "sha": "…", "important_open": 2, "important_resolved": 0,
               "nits": 4, "reraised_dropped": 0}],
  "fix_rounds": 1,
  "pr": {"number": 117, "url": "…"},
  "outcome": "null | done | needs_human:<open_questions|baseline_failing|no_progress|rounds_exhausted>",
  "outcome_sha": "…"
}
```

`outcome_sha` is HEAD at the moment `outcome` was set. `accept` and `dismiss` commit, so they move HEAD; so does a hand fix. That movement is the only signal `run` and `poll` need to know you acted (§11, §12).

**`work/<issue>/findings.json`** — the ledger. One entry per finding for the life of the run.

```json
{"findings": [{
  "id": "F3", "key": "sha1(pass|file|normalized title)",
  "pass": "bugs", "severity": "important", "file": "svc/x.py", "line": 41,
  "title": "…", "detail": "…", "evidence": "…",
  "opened_round": 1, "status": "open | resolved | dismissed",
  "status_round": 2, "status_evidence": "…", "dismissed_reason": null
}]}
```

**Host-local transients**, all safe to lose:

- `.factory/run/<issue>.json` — in-flight stage, pid, started_at, worktree path. Its presence with a live pid is the per-issue lock. A dead pid means an interrupted stage: the factory resets the worktree to HEAD, discards the partial work (transcript kept), and re-runs the stage.
- `.factory/run/poll.lock` — the per-repo poll lock.
- `.factory/poll.json` — per-issue consecutive exit-1 count and the HEAD it occurred at (§12).
- `.factory/doctor.json` — last passing `doctor` result, keyed by factory version and harness CLI version (§13).

**Worktree recovery.** A command for an issue with no local branch but an existing `origin/factory/<issue>` creates the local branch and worktree from it and continues. This is rule 6 in practice: a rebuilt host resumes every run from the remote.

**The GitHub rule.** The factory reads GitHub for exactly three purposes: to snapshot the issue at `spec`; to make its own writes idempotent (does a PR for this branch exist); and, in `poll` only, to list open issues carrying the intake label. It never reads GitHub to decide which stage runs next, whether a run is done, or what a finding's status is. Consequences: edits to the issue after `spec` have no effect on the run; comments, reviews, and check runs are not consumed; no GitHub App or permission model is involved beyond the `gh` login. Your input channel is local: edit `spec.md` or `plan.md`, `accept`, `dismiss`, `--force`, or `abandon`. On the host, local means over SSH.

**Start-of-command validation.** Every command loads `state.json` and checks that each recorded stage commit is reachable from HEAD and that the worktree is clean. Uncommitted changes under `work/42/` are treated as your edits and committed first; anything else dirty is exit 1. After `git fetch`, a remote branch strictly ahead is fast-forwarded (you edited on GitHub or pushed from elsewhere); a diverged one is exit 1. The factory never guesses.

## 8. Harness adapter — the interchangeability boundary

One protocol, two implementations, both `subprocess.run`:

```python
class Harness(Protocol):
    def run(self, *, cwd: Path, prompt_file: Path, schema_file: Path,
            mode: Literal["read", "write"], model: str | None,
            auth: Literal["subscription", "api"], env: dict[str, str],
            timeout_s: int) -> HarnessResult
# HarnessResult: output: dict, transcript_path: Path, exit_code: int, cli_version: str
```

The command-line prompt is always the fixed sentence `Follow the instructions in <prompt_file> exactly.` Role, inputs, and policy live in the committed prompt file; the output shape lives in the schema file. The same files drive either harness. The factory re-checks required keys and enums after the CLI's own schema enforcement.

| | Claude Code | Codex |
|---|---|---|
| Invocation | `claude -p "<msg>" --output-format json --permission-mode dontAsk --permission-prompts none` | `codex exec --json -o <last.json> "<msg>"` |
| `auth=api` adds | `--bare --append-system-prompt-file AGENTS.md` | — |
| `mode=read` | `--allowedTools Read,Grep,Glob --disallowedTools Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch` | `--sandbox read-only` (the default) |
| `mode=write` | `--allowedTools "Read,Grep,Glob,Edit,Write,Bash(make *),Bash(pytest *),Bash(uv *),Bash(python *)"` | `--sandbox workspace-write` |
| Structured output | `--json-schema '<schema>'` → `structured_output` field | `--output-schema <file>` → final message is the JSON |
| Transcript | stdout JSON → `.factory/transcripts/` | stdout JSONL → same |
| Model | `--model` | `-m` |
| Repo instructions | `CLAUDE.md` → `@AGENTS.md` (subscription); appended `AGENTS.md` (api) | `AGENTS.md` |
| Runaway bound | factory `timeout_s` + `--max-turns` | factory `timeout_s` |

Read-mode stages never write files; the artifact is the schema output, which the factory writes to disk. After a read stage the worktree must be clean, or the stage fails. Write-mode stages edit code and tests but never commit. Neither harness ever pushes.

**Auth.** `--auth` is explicit and there is no fallback.

- `api` is the default and the only unattended mode. The factory requires `ANTHROPIC_API_KEY` or `CODEX_API_KEY` in its own environment and passes only the selected one; a missing key is a preflight failure, never a silent switch to a saved login. Claude Code runs `--bare`: it never reads OAuth credentials or the keychain, loads no `CLAUDE.md`, hooks, or MCP servers from the worktree, and is the mode Anthropic recommends for scripted calls and says will become the default for `-p`. `AGENTS.md` is supplied explicitly.
- `subscription` is the attended mode. The harness inherits no provider key and uses its own saved login (`claude` sign-in; `codex login`) on the machine you are typing at. `poll` refuses it. Claude Code runs without `--bare`, so the worktree's `CLAUDE.md`, hooks, and MCP config load; the protected-path rule (§9) is what makes that acceptable.
- The factory never reads, copies, or stores credentials. `doctor` runs each harness in read mode on a trivial prompt under the selected mode and reports what authenticated.

**Environment.** Harness subprocesses get an allowlisted environment: `PATH`, `HOME`, locale, temp dir, the harness's own config-dir variable, and the selected provider key. `GH_TOKEN`, `GITHUB_TOKEN`, and unselected provider keys are stripped. Check commands get the same environment minus all provider keys, so repository-controlled test code never sees them.

## 9. Stage contracts

Every stage: validate state → render prompt → run harness → validate output → deterministic gate → commit artifact + `state.json` → push. A failed gate resets the worktree to HEAD, keeps the transcript, and exits 1. Every exit 2 posts one PR comment naming the gate and what clears it, idempotent per gate and HEAD.

| Stage | Mode | Inputs | Commits | Deterministic gate | GitHub |
|---|---|---|---|---|---|
| spec | read | `intent.md`, repo | `intent.md`, `spec.md` | schema-valid; non-empty markdown | create `factory/42`, push, draft PR `Closes #42` |
| plan | read | `spec.md`, repo | `plan.md` | has `## Files that change` and `## Proof` | push |
| build | write | `plan.md`, `spec.md` | code + tests, `checks/build-<n>.log` | baseline checks green **before** the session (else exit 2); allowed-edit rules; every changed path listed in `plan.md`; checks green | push |
| review | read | `spec.md`, `plan.md`, diff, latest check log, `REVIEW.md`, ledger | `review-<n>.json`, `findings.json` | schema-valid; every open finding has an update; nits ≤ cap | push; PR comment (rendered ledger) |
| fix | write | open Important findings with evidence | code, `fix-<n>.json`, `checks/fix-<n>.log` | HEAD == reviewed sha; allowed-edit rules; checks green | push |
| finalize | — | state, ledger | — | open Important == 0; HEAD == last reviewed sha | `gh pr ready`; summary comment |

The diff given to review is `git diff <base sha>...HEAD -- . ':!work'`, written by the factory to `.factory/tmp/` inside the worktree, so the reviewer needs no shell.

**Allowed-edit rules**, checked on a write stage's diff before anything else runs:

- Protected paths never change: `Makefile`, `factory.toml`, `AGENTS.md`, `CLAUDE.md`, `REVIEW.md`, `.devcontainer/`, `.claude/`, `.mcp.json`, `.codex/`, `.github/`, plus `protected_paths` from config. Reason: the next session runs whatever hooks, MCP servers, and image the worktree defines, and the gate runs whatever the Makefile says.
- `build` may edit anything else; inside `work/42/` only `plan.md` (the playbook's "update the plan in the same commit").
- `fix` may edit anything else except `test_paths` and all of `work/`. An agent fixing code must not be able to weaken the check on that code.

A violation resets the worktree and exits 1 naming the paths. You can make those edits by hand, commit, and re-run.

## 10. Roles and schemas

Five short prompt templates under `roles/`, rendered with `{intent}`, `{spec}`, `{plan}`, `{diff}`, `{checks}`, `{review_policy}`, `{ledger}`, `{findings}`. Conventions come from `AGENTS.md`, not the role.

- **spec** — From the intent, write a requirements and design spec for *this* codebase: problem, proposed outcome, affected users and systems, constraints, acceptance criteria, flagged concerns. List as open questions only what blocks implementation; state other assumptions explicitly.
  Schema `{"markdown": str, "open_questions": [str]}`. The factory writes `spec.md` from `markdown` and appends `## Open questions`.
- **plan** — From the spec and the code: files that change, order of work, risks, proof (exact commands and what they demonstrate). Someone who never saw the conversation must be able to implement from it.
  Schema `{"markdown": str}`.
- **build** — Implement the plan. Write the failing test first where the plan names one. Run the proof commands and the checks until green. Do not commit. If you deviate, update `plan.md` in the same pass.
  Schema `{"summary": str, "deviations": [str]}`.
- **review** — Apply `REVIEW.md` (bugs; security; compliance against `spec.md` and `plan.md`) to the diff. For every open finding in the ledger, say whether it is resolved, with evidence from the diff. Raise a new finding only with evidence. Do not re-raise resolved or dismissed findings.
  Schema `{"summary": str, "updates": [{"id": str, "status": "resolved|unresolved", "evidence": str}], "new": [{"severity": "important|nit", "pass": "bugs|security|compliance", "file": str, "line": int|null, "title": str, "detail": str, "evidence": str}]}`.
- **fix** — Address only the listed Important findings. Do not modify tests or anything under `work/`. Run the checks. Do not commit.
  Schema `{"addressed": [{"id": str, "how": str}], "not_addressed": [{"id": str, "why": str}]}`. `how` is recorded as a claim; only the next review changes a status.

**Ledger merge**, deterministic, in `state.py`: apply `updates`; for each `new` finding compute `key`; a key matching an open finding merges (no duplicate); matching a dismissed finding is dropped and counted in `reraised_dropped`; matching a resolved finding reopens it as a regression; otherwise it is appended with a new id.

## 11. Termination rule

`run` proceeds spec → plan → build → review, then alternates fix → review while `open_important > 0`:

- Spec produced open questions and none has been accepted → exit 2 (`open_questions`).
- Baseline checks red before build → exit 2 (`baseline_failing`).
- A review following a fix resolved zero Important findings → exit 2 (`no_progress`). A fixer that isn't converging doesn't get the next round.
- `fix_rounds == max_fix_rounds` with Important findings open → PR comment listing them; exit 2 (`rounds_exhausted`).
- `open_important == 0` and checks green → `finalize`; exit 0.

**Parked stays parked.** If `outcome` is `needs_human:*` and HEAD == `outcome_sha`, `run` exits 2 with the same gate before any harness call. Only your action re-opens the loop, and every action you can take (`accept`, `dismiss`, a hand commit, `--force`) moves HEAD. Nits never block and are never auto-fixed. `REVIEW.md`'s nit cap and skip list leash the reviewer; the ledger stops re-raising; the round cap and the no-progress rule leash the loop.

## 12. `poll`

`poll` is the whole of unattended mode: one-shot, sequential, timer-driven. It adds no state to the branch and no model calls of its own.

0. **Preflight.** Config `auth` must be `api` and the selected key present; otherwise exit 1 before touching GitHub. If `.factory/doctor.json` holds no passing record for the current factory and harness CLI versions, run `doctor`; exit 1 if it fails.
1. **Lock.** Take `.factory/run/poll.lock`; if held, exit 0.
2. **Discover.** `gh issue list --state open --label <label>` (the third GitHub read, §7), ascending issue number. The label is your selection, applied by the template or by hand.
3. **Classify** each issue from local state after `git fetch`, creating the worktree from `origin/factory/<n>` where needed:
   - no branch → eligible (starts at `spec`);
   - `outcome` null → eligible (resumes);
   - `outcome` `needs_human:*` and HEAD ≠ `outcome_sha` → eligible (you acted);
   - `outcome` `needs_human:*` and HEAD == `outcome_sha` → skip;
   - `outcome` `done` → skip;
   - `max_consecutive_failures` exit-1s recorded in `poll.json` at the current HEAD → skip.
4. **Run** `run <n>` for each eligible issue in turn. Exit 0 or 2 clears the issue's entry in `poll.json`; exit 1 increments it.
5. **Exit** 1 if any run exited 1, else 0. The journal holds the log; PR comments hold the gates.

Close runs with `abandon`, not the GitHub UI: a PR closed by hand leaves an open, labeled issue whose `finalize` fails until the retry cap parks it. Cost per issue is bounded structurally by the stage timeout, the three fix rounds, and the retry cap; issues enter only through the label you apply.

## 13. Deployment (phase 2)

Two images, one host, one wrapper, one timer.

**Factory image** — `deploy/Dockerfile` in the factory repo: Debian slim, Python 3.12, `uv`, `git`, `gh`, Node LTS, `@anthropic-ai/claude-code` and `@openai/codex` at the pinned versions, the factory wheel, a non-root user, an entrypoint that runs `gh auth setup-git` (the credential helper reads `GH_TOKEN`, which harness subprocesses do not receive). `image.yml` builds and pushes `ghcr.io/<owner>/factory:<tag>` on every Git tag. This is what makes the pins real.

**Target image** — `.devcontainer/Dockerfile` in the target repo: `FROM ghcr.io/<owner>/factory:<tag>` plus whatever `make test` and `make lint` need. Built on the host with `docker build`; publishing it is optional. It is a protected path (§9).

**Host** — a dedicated `factory` user with no production access. `/srv/factory/<repo>/repo` is the clone (its `.factory/` inside); `/srv/factory/<repo>` is bind-mounted at `/work`. `/etc/factory/<repo>.env` (mode 600) holds `FACTORY_IMAGE`, `GH_TOKEN` (fine-grained PAT scoped to the target repo: contents, issues, pull requests; metadata read), and the selected provider key. No credential directories are mounted; `auth = api` is the only mode that runs here unattended.

**Wrapper** — `factory-host <repo> <args…>`: `docker run --rm --user <factory uid:gid> --env-file /etc/factory/<repo>.env -v /srv/factory/<repo>:/work -w /work/repo $FACTORY_IMAGE factory <args…>`, with `-it` when a TTY is present. It is the single place the Docker invocation lives; the timer and the operator both use it.

**Units** — `factory-poll@<repo>.service` is `Type=oneshot` with `EnvironmentFile=/etc/factory/%i.env` and `ExecStart=/usr/local/bin/factory-host %i poll`. `factory-poll@<repo>.timer` fires it every 15 minutes with `Persistent=true`. systemd does not start a oneshot that is still running, and `poll` locks anyway.

**Operator access** — `ssh host`, then `factory-host <repo> status 42`, `factory-host <repo> accept 42`, and so on. Nothing is installed natively on the host except Docker, the wrapper, the env file, and the two units.

**Harness sandboxes in Docker** — both harnesses' own sandboxes depend on kernel features that Docker's default seccomp and user-namespace settings can restrict. `doctor` therefore runs each harness in write mode on a probe prompt that creates one file in a scratch worktree; pass requires the harness to exit 0 and the file to exist. A sandbox that cannot start fails this. Fix the container (profile, namespaces), not the harness flags; running a harness unsandboxed inside the container is a config decision made in `factory.toml`, never a runtime fallback.

**Rebuild** — restore the env file, wrapper, and units; clone; pull the image; enable the timer. Every run resumes from its remote branch (§7). Transcripts from before the rebuild are gone; nothing else is.

## 14. Config — `factory.toml`

```toml
[factory]
harness = "claude"                 # claude | codex
auth = "api"                       # api | subscription (attended only; poll refuses it)
base_branch = "main"
max_fix_rounds = 3
stage_timeout_min = 45
checks = [["make", "test"], ["make", "lint"]]   # argv arrays, run without a shell
test_paths = ["tests/"]
protected_paths = []               # added to the built-in list in §9

[poll]
label = "factory"                  # the one label read from GitHub
max_consecutive_failures = 3

[harness.claude]
model = ""                         # empty = CLI default
pinned_version = ""                # doctor warns on mismatch; actual version recorded in state.json

[harness.codex]
model = ""
pinned_version = ""
```

Config is read once, from the checkout the command is run in, never from the worktree. That is the whole surface.

## 15. Failure handling

- Harness non-zero exit or timeout → exit 1; transcript path printed; worktree reset; re-run the same command. Under `poll`, retried next tick up to `max_consecutive_failures`.
- Gate or allowed-edit violation → same, with the reason and paths.
- `gh` failure after a commit → the commit is local; re-running pushes it. PR creation is idempotent: if a PR for the branch exists, its number is recorded instead.
- Interrupted stage, including a killed container → detected on the next command via the transient file; partial work discarded; the stage re-runs. No duplicate PR, branch, or commit can result.
- A rate limit is a failed stage with the harness's message; no backoff beyond the tick.

## 16. Safety boundary

Run only against repositories you trust and issues you selected. Native permission and sandbox controls stay on; nothing requires a bypass flag. Python owns commits, pushes, and PR writes; agents are told not to perform them and hold no token that could. Check commands and prompts come from the factory and the invoking checkout, never from candidate edits, and the protected-path rule keeps the branch from altering either, including the image it runs in.

A git worktree is not a security sandbox, and neither is a container. `Bash(python *)` is arbitrary code execution; the allowlists bound scope and prevent accidents, not a hostile agent. On the workstation, agents can reach user-level files. On the host, the container sees only the mounted clone, the two tokens in its environment, and the network: a smaller blast radius, not isolation. Keep the host account free of production access and the tokens scoped to the one repo.

## 17. Definition of done

**Phase 1 — attended.**

1. An issue becomes a ready PR with each harness, and all four harness × auth combinations pass `doctor` and one live run.
2. Tests with fake `claude`, `codex`, `gh`, and `git` prove: red checks cannot push; a protected-path edit fails the stage; a test-file edit in `fix` fails the stage; `fix` refuses when HEAD ≠ reviewed sha; `finalize` refuses with open Important findings; a re-raised dismissed finding is dropped; the no-progress rule and the round cap terminate; an interrupted stage re-runs without a duplicate PR; `api` mode with no key fails rather than using a saved login; a parked issue re-run unchanged makes no harness call.
3. Five real issues dogfooded end to end. `REVIEW.md` and the five roles tuned from what the ledger shows.

**Phase 2 — unattended.** Starts only after phase 1 converges.

4. `image.yml` publishes a tagged factory image; `doctor` passes inside the target image on the host, including the write-mode probe for both harnesses.
5. Under the timer, in `api` mode, one labeled issue goes from open to ready PR with no operator command; a second parks on a gate, is cleared over SSH with `accept` or `dismiss`, and completes on a later tick.
6. Fake-binary tests prove: `poll` skips parked, done, and capped issues; two `poll`s cannot overlap; `subscription` under `poll` is refused; a deleted `.factory/` (simulated rebuild) resumes every run from the remote branch.

Success is one useful, verifiable PR per issue with minimal human intervention, not maximum autonomy.

## 18. Build order (est. 1,200–1,600 lines of Python)

1. `harness.py` + `doctor`: both CLIs in read mode with a schema; all four auth paths; `--bare` in `api` mode; the write-mode probe.
2. `state.py` + `repo.py` + `gh.py` + `spec` + `accept`: issue → draft PR containing `intent.md`, `spec.md`, `state.json`; the open-questions gate; worktree recovery from remote.
3. `plan`, `build`, `checks.py`: baseline, allowed-edit rules, plan/diff sync, environment filtering.
4. `review`, `fix`, the ledger merge, `finalize`, `run`, `dismiss`, `abandon`, gate comments, parked-stays-parked.
5. The fake-binary suite for §17.2. Dogfood five issues. **Phase 1 ends here.**
6. `poll.py`, `deploy/`, `image.yml`. Dogfood two issues under the timer.

## 19. Known gotchas

- **`--bare` in `api` mode.** Anthropic recommends it for scripted calls and says it will become the default for `-p`; it never reads the subscription login, so it is exactly right for `api` mode and exactly wrong for `subscription`. If the default flips before the pin is bumped, `subscription` mode needs whatever flag restores the saved login or becomes unavailable headless. `doctor` catches the flip.
- **Subscription auth does not survive unattended.** A long-lived `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` exists, but whether `--bare` honors it is unverified, and stored OAuth tokens have been reported to expire without refresh in headless contexts. v0 builds on neither; `poll` is `api` only, and a personal plan's usage windows are sized for a human, not a timer.
- **Untrusted worktree.** Without `--bare`, `-p` runs the worktree's `.claude/` hooks and `.mcp.json` servers with no trust prompt. That is why the protected-path check runs before the next session launches, not only before publish, and why `subscription` mode is attended.
- `--permission-prompts none` requires Claude Code ≥ 2.1.259; `doctor` checks.
- Claude Code `dontAsk` denies anything not allowlisted; a denial shows in the transcript. Extend the allowlist in `harness.py` rather than reaching for `bypassPermissions`.
- Codex `workspace-write` blocks network by default. Pre-install dependencies in the image or worktree rather than opening the sandbox's network.
- Codex `exec` is non-interactive; `--sandbox` is its permission control. `--full-auto` is deprecated.
- Both CLIs change fast. §8 is the only place flags appear; `doctor` runs `--version` and a smoke prompt so a renamed flag surfaces before a real run, and the image pins the version it surfaced on.
- The target image couples the factory runtime to one repo's toolchain. Right for a monorepo; one image per repo is the unit if there are ever several.
- `work/` grows by one directory per issue and merges to `main` with the PR. That is the audit trail the playbook asks for; prune by policy later if it becomes noise.

## 20. Deferred, deliberately

Consuming PR review comments as ledger findings (the playbook's `@claude` fix loop) · CI status at `finalize` · a notification hook on exit 1 and 2 (journal and PR comments suffice) · retry/backoff on rate limits beyond the tick · a per-run cost cap from harness-reported usage · multi-issue concurrency and a second worker, the point at which a shared database or queue earns its place · publishing the target image · GitHub Actions as an alternative runner (possible only because state lives on the branch) · cross-run reporting over committed `state.json` files (a script or SQLite index) · evals on role and `REVIEW.md` changes · per-repo role overrides · cross-model review by default · context-threshold handoff inside long builds · closing the loop from monitoring to `intent.md` · ADO/GitLab adapters.

## References

- The AI-Native SDLC playbook — https://claude.com/blog/the-ai-native-sdlc-playbook
- Claude Code, run programmatically — https://code.claude.com/docs/en/headless
- Codex, non-interactive mode — https://developers.openai.com/codex/non-interactive-mode