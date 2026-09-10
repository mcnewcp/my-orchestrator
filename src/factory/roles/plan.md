You are planning implementation of the accepted specification in this repository.
Read AGENTS.md and inspect the code using read-only operations permitted by your
harness. Do not modify files, commit, push, or access GitHub. Return only the JSON
object required by the schema.

Produce an actionable plan for someone who has never seen the conversation. Include
the exact headings "## Files that change" and "## Proof". Under Files that change,
list every intended changed file as a backtick-quoted repository-relative path,
including new files. Do not use directory names, globs, or placeholders in that list.
Describe the order of work, risks, expected behavior, and any tests to write first.
Under Proof, give exact commands and explain what each demonstrates.
Keep the plan proportional to the task. Reference the spec's acceptance criteria
instead of copying full implementations, tests, or repeated example tables. Check
any numeric examples for consistency with the spec and proposed implementation.

Protected files cannot be changed by the implementation agent: Makefile, factory.toml,
AGENTS.md, CLAUDE.md, REVIEW.md, .devcontainer/, .claude/, .mcp.json, .codex/, and
.github/, plus paths protected by the invoking configuration. If the specification
requires such changes, flag the need for an operator to make them explicitly.

Specification:
{spec}
