Review the proposed changes, following AGENTS.md and the supplied review policy.
Use only permitted read-only inspection; do not modify files, commit, push, or access GitHub. Return only
the JSON object required by the supplied output schema. Review bugs, security, and
compliance with the specification and plan. Apply the policy's nit cap and skip list.
Check whether examples in the spec and plan agree with the implemented behavior;
classify harmless artifact inaccuracies as nits with precise evidence.

For every open finding in the ledger, return exactly one update: resolved or
unresolved, with concrete evidence from the current code and diff. A fixer's claim
is not proof. Do not update findings that are already resolved or dismissed.
Raise new findings only when you can identify a specific file, a concrete issue,
its consequence, and evidence. Use a line number where available, otherwise null.
Do not re-raise resolved or dismissed findings. If a previously resolved defect has
actually regressed, explain the new evidence and retain its original pass, file,
and title so the factory can associate it with the existing finding.

Specification:
{spec}

Plan:
{plan}

Diff (factory-supplied content or file path; read the file if a path is given):
{diff}

Latest check output:
{checks}

Review policy:
{review_policy}

Finding ledger:
{ledger}
