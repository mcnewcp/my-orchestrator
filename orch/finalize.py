"""Finalize: assemble summary.md, set it as the PR body, flip the PR out of draft."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import artifacts, shell
from .artifacts import SUMMARY_MD, Contract


class FinalizeError(RuntimeError):
    """Finalize cannot run (missing PR number) or GitHub rejected the update."""


def _ledger_table(ledger: dict[str, Any] | None) -> str:
    findings = (ledger or {}).get("findings", [])
    if not findings:
        return "_No review findings were recorded._"
    rows = [
        "| Finding | Class | Disposition | Resolved | Follow-up |",
        "|---|---|---|---|---|",
    ]
    for f in findings:
        rows.append(
            "| {id} | {cls} | {disp} | {res} | {link} |".format(
                id=f.get("id", "?"),
                cls=f.get("class", "?"),
                disp=f.get("disposition", "?"),
                res="yes" if f.get("resolved") else "no",
                link=f.get("followup_issue") or "—",
            )
        )
    return "\n".join(rows)


def _followups(ledger: dict[str, Any] | None) -> str:
    urls = [f.get("followup_issue") for f in (ledger or {}).get("findings", [])]
    urls = [u for u in urls if u]
    if not urls:
        return "None."
    return "\n".join(f"- {u}" for u in urls)


def render_summary(issue: int, contract: Contract, ledger: dict[str, Any] | None) -> str:
    """Render the PR body from the contract and the ledger."""
    return "\n".join(
        [
            f"Closes #{issue}",
            "",
            f"# {contract.title} (#{issue})",
            "",
            "## Summary",
            contract.summary or "_No summary in the contract._",
            "",
            "## Acceptance Criteria",
            contract.acceptance_criteria or "_No acceptance criteria in the contract._",
            "",
            "## Review ledger",
            _ledger_table(ledger),
            "",
            "## Follow-ups filed",
            _followups(ledger),
            "",
            f"_Assembled by `orch` from `.scratch/{issue}/contract.md` and `ledger.json`._",
            "",
        ]
    )


def finalize(issue: int, repo: Path, scratch: Path) -> Path:
    """Write summary.md, set it as the PR body, and mark the PR ready for review."""
    run = artifacts.read_run(scratch) or {}
    pr = run.get("pr_number")
    if pr is None:
        raise FinalizeError(f"{artifacts.run_path(scratch)}: no pr_number to finalize")
    contract = artifacts.read_contract(scratch)
    ledger = artifacts.read_ledger(scratch)
    path = artifacts.write_text(scratch / SUMMARY_MD, render_summary(issue, contract, ledger))
    try:
        shell.gh("pr", "edit", str(pr), "--body-file", str(path), cwd=repo)
        shell.gh("pr", "ready", str(pr), cwd=repo)
    except Exception:
        # summary.md existing means READY; do not claim a terminal state GitHub never saw.
        path.unlink(missing_ok=True)
        raise
    return path
