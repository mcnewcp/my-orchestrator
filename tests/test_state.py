"""Every row of the state-derivation table (skeleton section 7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (HEAD, OLD, blocking, write_audit, write_contract, write_ledger,
                      write_review, write_run)
from orch.state import (State, consecutive_audit_failures, derive_state, inventory,
                        next_audit_round, next_review_round, open_blocking_findings)


def implemented(scratch: Path) -> None:
    """Contract written and a draft PR open."""
    write_contract(scratch)
    write_run(scratch)


# Each case: (name, build the scratch dir, expected state).
CASES = [
    (
        "clarification wins over everything",
        lambda s: (implemented(s), (s / "clarification.md").write_text("q\n"),
                   (s / "escalation.md").write_text("e\n"), (s / "summary.md").write_text("s\n")),
        State.NEEDS_CLARIFICATION,
    ),
    (
        "escalation wins over summary",
        lambda s: (implemented(s), (s / "escalation.md").write_text("e\n"),
                   (s / "summary.md").write_text("s\n")),
        State.ESCALATED,
    ),
    ("summary means ready", lambda s: (s / "summary.md").write_text("s\n"), State.READY),
    ("empty scratch", lambda s: None, State.CONTRACTING),
    ("contract without run.json", lambda s: write_contract(s), State.IMPLEMENTING),
    (
        "run.json without a pr_number",
        lambda s: (write_contract(s), write_run(s, pr_number=None)),
        State.IMPLEMENTING,
    ),
    ("pr open, no audit yet", implemented, State.AUDITING),
    (
        "latest audit is older than head",
        lambda s: (implemented(s), write_audit(s, 1, passed=True, commit=OLD)),
        State.AUDITING,
    ),
    (
        "audit failed, no ledger",
        lambda s: (implemented(s), write_audit(s, 1, passed=False)),
        State.IMPLEMENTING,
    ),
    (
        "audit failed, ledger has no open blocker",
        lambda s: (implemented(s), write_audit(s, 1, passed=False),
                   write_ledger(s, findings=[blocking(resolved=True)])),
        State.IMPLEMENTING,
    ),
    (
        "audit failed with an open blocker",
        lambda s: (implemented(s), write_audit(s, 1, passed=False),
                   write_ledger(s, findings=[blocking()])),
        State.REMEDIATING,
    ),
    (
        "audit passed, no review",
        lambda s: (implemented(s), write_audit(s, 1, passed=True)),
        State.REVIEWING,
    ),
    (
        "audit passed, review is for an older commit",
        lambda s: (implemented(s), write_audit(s, 1, passed=True),
                   write_review(s, 1, verdict="APPROVE", commit=OLD)),
        State.REVIEWING,
    ),
    (
        "review approves this commit",
        lambda s: (implemented(s), write_audit(s, 1, passed=True),
                   write_review(s, 1, verdict="APPROVE")),
        State.FINALIZING,
    ),
    (
        "changes requested, no ledger yet",
        lambda s: (implemented(s), write_audit(s, 1, passed=True),
                   write_review(s, 1, verdict="REQUEST_CHANGES")),
        State.JUDGING,
    ),
    (
        "changes requested, round not yet judged",
        lambda s: (implemented(s), write_audit(s, 1, passed=True),
                   write_review(s, 2, verdict="REQUEST_CHANGES"),
                   write_ledger(s, rounds_completed=1, findings=[blocking(resolved=True)])),
        State.JUDGING,
    ),
    (
        "judged, blocker still open",
        lambda s: (implemented(s), write_audit(s, 1, passed=True),
                   write_review(s, 1, verdict="REQUEST_CHANGES"),
                   write_ledger(s, rounds_completed=1, findings=[blocking()])),
        State.REMEDIATING,
    ),
    (
        "judged, nothing blocking left",
        lambda s: (implemented(s), write_audit(s, 1, passed=True),
                   write_review(s, 1, verdict="REQUEST_CHANGES"),
                   write_ledger(s, rounds_completed=1,
                                findings=[blocking(resolved=True),
                                          {"id": "F-1-2", "class": "style",
                                           "disposition": "deferred", "resolved": False}])),
        State.FINALIZING,
    ),
]


@pytest.mark.parametrize("name,build,expected", CASES, ids=[c[0] for c in CASES])
def test_derive_state(scratch: Path, name: str, build, expected: State) -> None:
    build(scratch)
    assert derive_state(scratch, HEAD) is expected


def test_derive_state_is_pure_over_the_directory(scratch, monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("state ran a subprocess"))
    assert derive_state(scratch, HEAD) is State.CONTRACTING


def test_consecutive_audit_failures_counts_only_the_trailing_run(scratch):
    write_audit(scratch, 1, passed=False)
    write_audit(scratch, 2, passed=True)
    write_audit(scratch, 3, passed=False)
    write_audit(scratch, 4, passed=False)
    assert consecutive_audit_failures(scratch) == 2
    write_audit(scratch, 5, passed=True)
    assert consecutive_audit_failures(scratch) == 0


def test_next_round_numbers(scratch):
    assert (next_audit_round(scratch), next_review_round(scratch)) == (1, 1)
    write_audit(scratch, 1, passed=True)
    write_audit(scratch, 2, passed=True)
    write_review(scratch, 1, verdict="APPROVE")
    assert (next_audit_round(scratch), next_review_round(scratch)) == (3, 2)


def test_open_blocking_findings_ignores_resolved_and_non_blocking(scratch):
    write_ledger(scratch, findings=[
        blocking("F-1-1", resolved=True),
        blocking("F-1-2"),
        {"id": "F-1-3", "class": "style", "disposition": "deferred", "resolved": False},
    ])
    assert [f["id"] for f in open_blocking_findings(scratch)] == ["F-1-2"]


def test_inventory_lists_what_exists(scratch):
    implemented(scratch)
    write_audit(scratch, 1, passed=False)
    write_review(scratch, 1, verdict="REQUEST_CHANGES")
    write_ledger(scratch, findings=[blocking()])
    (scratch / "logs").mkdir()
    (scratch / "logs" / "001-contractor.jsonl").write_text("{}\n")
    rows = dict(inventory(scratch))
    assert set(rows) == {"run.json", "contract.md", "audit-1.json", "review-1.md",
                         "ledger.json", "logs/"}
    assert "FAIL" in rows["audit-1.json"]
    assert "REQUEST_CHANGES" in rows["review-1.md"]
    assert "open_blocking=1" in rows["ledger.json"]


def test_inventory_survives_a_malformed_artifact(scratch):
    write_contract(scratch)
    (scratch / "audit-1.json").write_text("{not json", encoding="utf-8")
    assert "unreadable" in dict(inventory(scratch))["audit-1.json"]
