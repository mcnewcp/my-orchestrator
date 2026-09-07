# Prototype v0 review

Reviewed the implementation against `90f26c2830b71c54ab4ed1461c88879ec89b7793`,
using separate standards and specification reviewers. The initial reviewed
commits were `56d5dd2` and `40189a2`. The originating specification is
`.scratch/design/prototype-v0.md`, with the owner's subscription-only override.

## Standards

No violations of `AGENTS.md` or its repository-guidance documents were found.
Three maintenance heuristics remain; none is a required correctness repair:

- **Possible Primitive Obsession:** `Run = dict[str, Any]` and nested frozen
  inputs/check/review dictionaries make persisted record shapes implicit.
  Typed evidence records could make these contracts easier to follow.
- **Possible Duplicated Code:** Git and GitHub adapters each choose between
  `ProcessRunner` and direct subprocess execution. A common default execution
  path could reduce repeated timeout/error handling.
- **Possible Mysterious Name:** `finish_attempt` also records unfinished progress
  with `status="running"`. A name such as `update_attempt` would describe that
  responsibility more clearly.

These are deferred maintenance suggestions for the explicit v0 controller.

## Specification

Two material findings were reproduced and fixed:

- A final CI failure after GitHub readiness conversion left the PR ready while
  Postgres recorded a blocked run. Publication now restores the run's PR to
  draft and records refreshed facts. Restoration failure is reported explicitly;
  worker cancellation prevents further writes and leaves reconciliation to resume.
- A PR changed back to draft during the final CI wait could be recorded as ready
  using stale PR facts. Publication now validates and stores its final PR response,
  including draft status.

Both workflow regressions failed before their fixes and passed afterward.
GitHub transport tests cover draft restoration and run identity. The packaged
GitHub CLI supports the required `pr ready --undo` command. No additional
confirmed material specification defects or scope creep were found.

Standards: 0 hard violations, 3 maintenance heuristics. Specification: 2 material
findings, both repaired. See the validation record for separate live trial evidence.
