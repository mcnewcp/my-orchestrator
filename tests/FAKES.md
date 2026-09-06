# Test doubles contract

Design §17.2 asks for tests with fake `claude`, `codex`, `gh`, and `git`. We fake the first three and use
**real git** against a local bare "origin" — worktree, push, fast-forward and force-with-lease semantics are the
thing under test, and a fake git would only prove the fake.

## Layout

```
tests/
  FAKES.md            this file
  conftest.py         fixtures below
  fakes/claude        executable python3 script (no extension)
  fakes/codex         executable python3 script
  fakes/gh            executable python3 script
  test_*.py
```

`conftest.py` puts `tests/fakes` first on `PATH` for every test that needs binaries and sets
`FACTORY_FAKE_DIR` to a per-test temp directory. The fakes take ALL their instructions from files in that directory
so tests can script them without monkeypatching the factory.

## Environment contract

| variable | set by | read by |
|---|---|---|
| `FACTORY_FAKE_DIR` | conftest | all three fakes |
| `PATH` | conftest (fakes dir first) | factory (`shutil.which`) |
| `HOME` | conftest (temp home) | fakes ignore; real git uses it for identity |
| `GIT_AUTHOR_NAME/EMAIL`, `GIT_COMMITTER_NAME/EMAIL` | conftest | real git |

The factory passes harness subprocesses an allowlisted env (design §8) that does NOT include `FACTORY_FAKE_DIR`.
Therefore the fakes must locate the fake dir another way when the variable is absent: they read the path from
`<dirname of argv[0]>/../.fake_dir` (a one-line file conftest writes next to the fakes, i.e. `tests/.fake_dir`,
gitignored) — fall back to `FACTORY_FAKE_DIR` when the file is missing. This also lets tests assert that
`FACTORY_FAKE_DIR` and `GH_TOKEN` were *not* in the harness environment.

## Files in `FACTORY_FAKE_DIR`

### `harness_queue.jsonl` (consumed FIFO by `claude` and `codex`, shared queue)
One JSON object per line = one harness invocation, popped from the top (file rewritten without the first line):
```json
{"output": {"markdown": "# Spec…", "open_questions": []},
 "writes": {"src/x.py": "print(1)\n", "RED": ""},        // optional: files to create/overwrite relative to cwd before exiting
 "deletes": ["tests/test_a.py"],                          // optional
 "exit_code": 0,                                          // optional, default 0
 "is_error": false,                                       // optional (claude only): emit is_error=true with `result` text
 "sleep_s": 0,                                            // optional: sleep before exiting (timeout tests)
 "hang": false}                                           // optional: sleep forever (interrupted-stage tests kill the pid)
```
Empty queue -> the fake exits 3 and prints `fake harness: queue empty` (a bug in the test or in the factory's call count).

### `harness_calls.jsonl` (appended by `claude` and `codex`, one line per invocation)
```json
{"binary": "claude", "argv": [...], "cwd": "/abs/worktree", "env_keys": ["PATH","HOME",...],
 "env": {"ANTHROPIC_API_KEY": "present-or-absent", "GH_TOKEN": "present-or-absent", "CODEX_API_KEY": "…"},
 "prompt_file": "work/42/prompts/spec-1.md", "prompt_text": "<the file's content read from cwd>",
 "schema": {...}}   // parsed from --json-schema (claude) or --output-schema file (codex)
```
Tests assert on this file: number of calls (zero for a parked issue), `--bare` present iff api mode, allowed tools per mode,
no GH_TOKEN, prompt content contains the diff/ledger, etc.

### `gh_state.json` (read + rewritten by `gh`)
```json
{"repo": "owner/name",
 "auth_ok": true,
 "issues": {"42": {"number": 42, "title": "…", "body": "…", "url": "https://github.com/owner/name/issues/42",
                   "labels": ["factory"], "state": "OPEN"}},
 "labels": ["factory"],
 "prs": {"117": {"number": 117, "url": "https://github.com/owner/name/pull/117", "headRefName": "factory/42",
                 "baseRefName": "main", "isDraft": true, "state": "OPEN", "title": "…", "body": "…",
                 "comments": [{"body": "…"}]}},
 "next_pr_number": 118,
 "fail_next": []}          // optional: list of gh subcommand prefixes ("pr create", "pr comment") that fail once with exit 1
```
### `gh_calls.jsonl` (appended by `gh`): `{"argv": [...], "cwd": "…"}` per call.

## Fake `claude` behavior
- `--version` -> prints `9.9.9 (Claude Code)` and exits 0 (above CLAUDE_MIN_VERSION).
- Mirrors the real CLI where the factory depends on it: if `--bare` is present and `ANTHROPIC_API_KEY` is not in the
  environment, prints a result JSON with `is_error: true`, `subtype: "success"`, `result: "Not logged in · Please run /login"`
  and exits 1 WITHOUT consuming the queue (this is how "api mode with no key fails" is observable — though the factory must
  already have refused before launching; the test asserts zero calls).
- Otherwise pops the queue, applies `writes`/`deletes`, and prints ONE JSON object shaped like the real result:
  `{"type":"result","subtype":"success","is_error":false,"num_turns":3,"session_id":"fake-<n>","result":"<json string>",
    "structured_output": <output>, "modelUsage": {"claude-fake-1": {}}, "permission_denials": []}`, exit `exit_code`.
- Reads the prompt file named in the positional prompt sentence (`Follow the instructions in <path> exactly.`) relative to cwd
  and records its text in `harness_calls.jsonl`.

## Fake `codex` behavior
- `--version` -> `codex-cli 9.9.9`.
- Pops the queue, applies writes/deletes, writes `<output>` as JSON to the `-o` file, prints JSONL events
  (`thread.started`, `turn.started`, `item.completed` agent_message, `turn.completed`) to stdout, exits `exit_code`.
  Records the call like claude (schema parsed from the `--output-schema` file).

## Fake `gh` (subcommands the factory uses; anything else -> exit 2 with a message naming the argv)
- `repo view --json nameWithOwner` -> `{"nameWithOwner": repo}`
- `auth status` -> exit 0 if `auth_ok` else 1
- `issue view N --json <fields>` -> the fields of issues[N] (labels rendered as `[{"name": ...}]`); unknown -> exit 1
- `issue list --state open --label L --json number[,…]` (also `--limit`) -> JSON array of matching issues, ascending
- `issue edit N --remove-label L` / `--add-label L`
- `label list --json name` -> `[{"name": …}]`; `label create L [--color c] [--description d] [--force]`
- `pr list --head B --state all|open --json …` -> array of PR objects with the requested fields (`number,url,isDraft,state,headRefName`)
- `pr create --draft --head B --base M --title T (--body X | --body-file F)` -> creates PR (draft), prints its URL
- `pr view N --json comments` -> `{"comments": [{"body": …}]}`; also accept `pr view N --json number,url,isDraft,state`
- `pr comment N (--body X | --body-file F)` -> appends comment, prints URL
- `pr ready N` -> isDraft=false; `pr close N [--comment X]` -> state=CLOSED
- `api …` is not used by the factory in v0.
- Honors `fail_next`: if argv starts with a listed prefix, remove it from the list, print an error to stderr, exit 1.

## conftest fixtures (names are the contract; see conftest.py for details)
- `fake_dir` -> Path of FACTORY_FAKE_DIR (fresh per test), with helper methods on a small `Fakes` object:
  `queue(*outputs_or_dicts)`, `calls()` -> list[dict], `gh_calls()`, `gh_state()` / `set_gh_state(dict)`, `pr(number)`.
- `origin` -> Path of a bare git repo.
- `target` -> Path of a clone of `origin` on `main` seeded with: `checks.py` (exits 1 if a file named `RED` exists, else 0),
  `factory.toml` with `checks = [["python3","checks.py"]]`, `test_paths=["tests/"]`, `harness="claude"`, `auth="subscription"`,
  `stage_timeout_min=1`; `Makefile` (unused but present), `AGENTS.md`, `CLAUDE.md`, `REVIEW.md`, `.gitignore` with `.factory/`,
  `src/app.py`, `tests/test_app.py`; committed and pushed to origin. The fixture also registers issue 42 in `gh_state.json`.
- `run_cli(*args, env=None) -> (exit_code, stdout, stderr)` runs `factory.cli.main` in-process with cwd = target
  (monkeypatch chdir) and captured streams. Prefer this over subprocess for speed; one smoke test runs the console script for real.
- `worktree(issue=42)` -> Path `target/.factory/worktrees/42`.
