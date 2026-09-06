# Harness smoke results — 2026-09-06 (this workstation)

Verified by hand before implementation. Every flag in design §8 was exercised in a stripped
environment (`env -i PATH HOME LANG TERM TMPDIR`), i.e. exactly what `harness.build_env` produces
in `subscription` mode. No API keys were set.

| harness | version | probe | result |
|---|---|---|---|
| claude | 2.1.263 | read: `-p --output-format json --permission-mode dontAsk --permission-prompts none --allowedTools Read,Grep,Glob --disallowedTools Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch --json-schema … --max-turns 6` | exit 0, `structured_output` = schema-valid object, 14 s |
| claude | 2.1.263 | write: `--allowedTools "Read,Grep,Glob,Edit,Write,Bash(make *),Bash(pytest *),Bash(uv *),Bash(python *)"` creating `probe.txt` and running `python -c` | exit 0, file present, `permission_denials: []`, 11 s |
| claude | 2.1.263 | `--bare` with no `ANTHROPIC_API_KEY` | exit 1 in 0.6 s, `is_error: true`, `subtype: "success"`, result "Not logged in · Please run /login" |
| codex | 0.153.4 | read: `exec --json --sandbox read-only --output-schema schema.json -o last.json` | exit 0, `last.json` = schema-valid object, 24 s |
| codex | 0.153.4 | write: `exec --json --sandbox workspace-write --output-schema … -o … -C <cwd>` creating `probe.txt` | exit 0, file present, 26 s |

Facts the adapter depends on:

- **stdin must be `/dev/null`.** Codex printed "Reading additional input from stdin..." and blocked
  until the 170 s timeout when stdin was an inherited pipe. Claude stalls 3 s and warns. The adapter
  always passes `stdin=subprocess.DEVNULL`.
- **Claude: branch on `is_error` before `subtype`.** The auth failure above reported `subtype: "success"`.
- Claude reports the models actually used under `modelUsage` (keys), the session under `session_id`,
  and the structured artifact under `structured_output`; `result` holds the same JSON as a string.
- Codex `--json` emits JSONL events: `thread.started`, `turn.started`, `item.started`/`item.completed`
  (item types `agent_message`, `command_execution`, `file_change`), `turn.completed` (with `usage`).
  The final structured message is written to the `-o` file. Exit code reports harness health only.
- Codex printed the stdin notice on stderr even with `/dev/null`; it is noise, not an error.
- Neither harness was detected as "nested" when `CLAUDECODE`/`CLAUDE_CODE_*` were stripped.
- `claude auth status --json` → `authMethod: claude.ai`, `subscriptionType: max`; Codex `auth.json` present.
- `--permission-prompts none` exists (needs ≥ 2.1.259, design §19); `--restricted` also exists but is
  not used (the allowlist is the design's control).
- Docker is installed but this user is not in the `docker` group; phase-2 deploy files are written
  but not exercised here.
