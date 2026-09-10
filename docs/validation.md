# Validation

Implementation follows `.scratch/design/prototype-v0.md` (Factory v0, Design E).

The runtime uses only Python's standard library. Automated tests use fake harness binaries, mocked GitHub responses, and temporary real Git repositories/remotes. They exercise credential isolation, schema enforcement, edit restrictions, red checks, ledger evidence, bounded review loops, parked gates, recovery, idempotent writes, push leases, and poll eligibility/locking.

All 123 tests passed on Python 3.12.14 and 3.14.7. `make lint` checks Python
syntax; an additional isolated Ruff correctness check (`F,E9`) passed. The wheel
and source distribution build, and the wheel installs with its console script
and all role/schema resources. Wrapper/entrypoint tests and shell syntax checks
pass; Docker image construction and deployment have not been validated.

The workflow assumes trusted repositories and selected issues. Environment
filtering and path gates enforce workflow rules; worktrees and containers are
not a security boundary against hostile code.

## Commit references

A committed state file cannot contain the hash of its own commit. State therefore records `checkpoint:<UUID>` references, resolved against an exact `Factory-Checkpoint: <UUID>` commit trailer. The resolver requires exactly one matching reachable commit. Reviews separately record `reviewed_input_sha`; the review checkpoint adds only factory artifacts. Any subsequent operator commit invalidates review freshness and clears an unchanged-head gate.

Finalization commits its successful checks and `outcome = "done"` after the idempotent GitHub writes. This makes completion recoverable from the remote branch. Forced stages preserve operator Markdown edits and the findings ledger, rewind generated downstream artifacts, and return the PR to draft.

Gate notification intent and its acknowledgement are committed, so poll can
retry an interrupted comment without another model call. Forced checkpoints
record the exact remote lease needed to retry a failed rewrite. Host-local
interruption markers retain the last safe factory commit across a rewind.

## Live verification

On September 7, 2026, Claude Code 2.1.263 and Codex 0.153.4 passed saved-subscription read and write probes on this workstation. The implemented doctor additionally verifies a random nonce and the actual contents of the created file.

The sandbox uses `prototype/v0-cc-cdx-fixture` as its base, with cached pytest and
Ruff dependencies and offline `make test` / `make lint` commands. Both harnesses
ran with saved subscription authentication and their configured CLI defaults.

| Issue | Harness | Ready PR | Target checks |
| --- | --- | --- | --- |
| [#20: sum_squares](https://github.com/mcnewcp/orch-sandbox/issues/20) | Claude Code | [#25](https://github.com/mcnewcp/orch-sandbox/pull/25) | 17 tests; lint green |
| [#21: data_span](https://github.com/mcnewcp/orch-sandbox/issues/21) | Codex | [#26](https://github.com/mcnewcp/orch-sandbox/pull/26) | 18 tests; lint green |
| [#22: pairwise_differences](https://github.com/mcnewcp/orch-sandbox/issues/22) | Claude Code | [#27](https://github.com/mcnewcp/orch-sandbox/pull/27) | 16 tests; lint green |
| [#23: degrees_to_turns](https://github.com/mcnewcp/orch-sandbox/issues/23) | Codex | [#28](https://github.com/mcnewcp/orch-sandbox/pull/28) | 19 tests; lint green |
| [#24: all_equal](https://github.com/mcnewcp/orch-sandbox/issues/24) | Claude Code | [#29](https://github.com/mcnewcp/orch-sandbox/pull/29) | 21 tests; lint green |

All five runs completed autonomously with one review, no open findings, and no
fix rounds. The automated suite exercises the review/fix and human-gate paths.
The PRs remain open for human review; none was merged.

The installed wheel also recovered completed issue #20 in a fresh clone with no
`.factory/` directory, reproduced the exact remote HEAD, and resumed without a
model call or new commit. `status` worked with network access disabled.

API credentials (`ANTHROPIC_API_KEY` and `CODEX_API_KEY`) were absent from the implementation session. API-auth doctor/live runs and unattended host/timer validation therefore require a later credentialed environment. Fake-binary tests cover the API invocation and fail-closed missing-key behavior; they are not a substitute for those live checks.

The first live run produced correct code with green checks, but its long plan
repeated an incorrect numeric example in an auxiliary table. This informed a
prompt revision: specs/plans now avoid full implementation listings and repeated
examples, review explicitly checks artifact examples, and harmless inaccuracies
remain nonblocking nits. Build and fix prompts also clarify plan-path permissions
and how to report a finding requiring operator-owned edits.

Official adapter references: [Claude Code headless mode](https://code.claude.com/docs/en/headless) and [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).
