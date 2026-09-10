Implement the accepted plan in this repository, following AGENTS.md. Return only the
JSON object required by the supplied output schema after editing code and tests.
Never commit, push, access GitHub, or change factory state or review artifacts.
Write a failing test first wherever the plan names one, implement the behavior, and
run the proof commands and configured checks until they pass. Report facts in the
summary; the factory independently runs all checks before accepting your changes.

Do not modify Makefile, factory.toml, AGENTS.md, CLAUDE.md, REVIEW.md, .devcontainer/,
.claude/, .mcp.json, .codex/, .github/, or any additional protected paths stated in
this prompt. Under work/, only this issue's plan.md may be edited. Every code or test
file you change must appear as a backtick-quoted repository-relative file path under
"## Files that change" in plan.md. If implementation deviates from the plan, update
that plan in the same pass and report the deviations. Preserve "## Proof".
For a changed plan, list its own work/<issue>/plan.md path in Files that change.
Keep the structured summary concise, and name the actual checks you ran.

Specification:
{spec}

Plan:
{plan}

Configured checks:
{checks}
