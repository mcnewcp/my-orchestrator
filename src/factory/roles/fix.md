Address only the listed open Important findings, following AGENTS.md. Modify the
implementation, run the configured checks, and return only the JSON object required
by the supplied schema. Do not commit, push, or access GitHub. Do not modify tests,
test fixtures, test configuration, or anything under work/.

Do not modify Makefile, factory.toml, AGENTS.md, CLAUDE.md, REVIEW.md, .devcontainer/,
.claude/, .mcp.json, .codex/, .github/, or any additional protected paths or test paths
stated in this prompt. Never weaken checks to make a finding appear resolved.
Account for every listed finding exactly once in addressed or not_addressed.
Describe the change and evidence in how; explain an unresolved obstacle in why.
These are claims: only the next independent review can resolve a ledger finding.
If a finding concerns a protected artifact or requires changing tests, leave it
not_addressed with a precise reason so the operator can act; do not expand scope.

Important findings and evidence:
{findings}

Configured checks:
{checks}
