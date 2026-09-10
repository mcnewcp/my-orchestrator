Implement the accepted plan in this repository, following AGENTS.md. Return only the
JSON object required by the supplied output schema after editing code and tests.
Never commit, push, access GitHub, or change factory state or review artifacts.
Write a failing test first wherever the plan names one, implement the behavior, and
run the proof commands and configured checks until they pass. Report facts in the
summary; the factory independently runs all checks before accepting your changes.

Do not modify Makefile, factory.toml, AGENTS.md, CLAUDE.md, REVIEW.md, .devcontainer/,
.claude/, .mcp.json, .codex/, .github/, or any additional protected paths stated in
this prompt. Do not write anywhere under .factory/. Every code or test
file you change must appear as a backtick-quoted repository-relative file path under
"## Files that change" in plan.md. Report deviations in the structured output;
changes outside the approved paths require an operator to edit the plan first.
Keep the structured summary concise, and name the actual checks you ran.

Specification:
{spec}

Plan:
{plan}

Configured checks:
{checks}
