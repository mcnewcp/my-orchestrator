"""Pure derivation of run state from the scratch directory (skeleton section 7)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from . import artifacts
from .artifacts import (
    CLARIFICATION_MD,
    CONTRACT_MD,
    ESCALATION_MD,
    LEDGER_JSON,
    LOGS_DIR,
    RUN_JSON,
    SUMMARY_MD,
    ArtifactError,
)


class State(StrEnum):
    """The states the CLI can derive; three of them are terminal."""

    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    ESCALATED = "ESCALATED"
    READY = "READY"
    CONTRACTING = "CONTRACTING"
    IMPLEMENTING = "IMPLEMENTING"
    AUDITING = "AUDITING"
    REVIEWING = "REVIEWING"
    JUDGING = "JUDGING"
    REMEDIATING = "REMEDIATING"
    FINALIZING = "FINALIZING"


TERMINAL = frozenset({State.NEEDS_CLARIFICATION, State.ESCALATED, State.READY})


def derive_state(scratch_dir: Path, head: str) -> State:
    """Derive the run state from the scratch directory and the checkout's HEAD sha."""
    if (scratch_dir / CLARIFICATION_MD).exists():
        return State.NEEDS_CLARIFICATION
    if (scratch_dir / ESCALATION_MD).exists():
        return State.ESCALATED
    if (scratch_dir / SUMMARY_MD).exists():
        return State.READY
    if not (scratch_dir / CONTRACT_MD).exists():
        return State.CONTRACTING

    run = artifacts.read_run(scratch_dir)
    if run is None or run.get("pr_number") is None:
        return State.IMPLEMENTING

    audit = latest_audit(scratch_dir)
    if audit is None or audit[1]["commit"] != head:
        return State.AUDITING
    if not audit[1]["pass"]:
        return State.REMEDIATING if open_blocking_findings(scratch_dir) else State.IMPLEMENTING

    review = latest_review(scratch_dir)
    if review is None or review[1]["commit"] != head:
        return State.REVIEWING
    round_n, front = review
    if front["verdict"] == "APPROVE":
        return State.FINALIZING

    ledger = artifacts.read_ledger(scratch_dir)
    if ledger is None or ledger["rounds_completed"] < round_n:
        return State.JUDGING
    return State.REMEDIATING if open_blocking_findings(scratch_dir) else State.FINALIZING


# ------------------------------------------------------------------------ helpers


def latest_audit(scratch_dir: Path) -> tuple[int, dict[str, Any]] | None:
    """Highest-numbered audit as (n, parsed json), or None when there is none."""
    files = artifacts.audit_files(scratch_dir)
    if not files:
        return None
    n, path = files[-1]
    return n, artifacts.read_audit(path)


def latest_review(scratch_dir: Path) -> tuple[int, dict[str, Any]] | None:
    """Highest-numbered review as (n, front matter), or None when there is none."""
    files = artifacts.review_files(scratch_dir)
    if not files:
        return None
    n, path = files[-1]
    return n, artifacts.read_review_front(path)


def open_blocking_findings(scratch_dir: Path) -> list[dict[str, Any]]:
    """Ledger findings with disposition 'blocking' that are not yet resolved, in array order."""
    ledger = artifacts.read_ledger(scratch_dir)
    if ledger is None:
        return []
    return [
        f
        for f in ledger["findings"]
        if f.get("disposition") == "blocking" and not f.get("resolved", False)
    ]


def consecutive_audit_failures(scratch_dir: Path) -> int:
    """Length of the trailing run of failed audits (0 when the latest audit passed)."""
    count = 0
    for _, path in reversed(artifacts.audit_files(scratch_dir)):
        if artifacts.read_audit(path)["pass"]:
            break
        count += 1
    return count


def next_audit_round(scratch_dir: Path) -> int:
    """Round number the next audit-<n>.json will carry."""
    files = artifacts.audit_files(scratch_dir)
    return files[-1][0] + 1 if files else 1


def next_review_round(scratch_dir: Path) -> int:
    """Round number the next review-<n>.md will carry."""
    files = artifacts.review_files(scratch_dir)
    return files[-1][0] + 1 if files else 1


def artifact_names(scratch_dir: Path) -> set[str]:
    """Names of the artifact files in the scratch dir (logs excluded) — used for stuck detection."""
    if not scratch_dir.is_dir():
        return set()
    return {p.name for p in scratch_dir.iterdir() if p.is_file()}


def _detail(fn) -> str:
    try:
        return fn()
    except ArtifactError as exc:
        return f"unreadable ({exc})"


def inventory(scratch_dir: Path) -> list[tuple[str, str]]:
    """Artifact inventory for `orch status`: (file name, one-line detail) pairs."""
    rows: list[tuple[str, str]] = []
    if not scratch_dir.is_dir():
        return rows

    run_file = scratch_dir / RUN_JSON
    if run_file.exists():
        rows.append(
            (
                RUN_JSON,
                _detail(
                    lambda: "branch={branch} pr={pr_number}".format(
                        **{"branch": None, "pr_number": None, **artifacts.read_run(scratch_dir)}
                    )
                ),
            )
        )
    if (scratch_dir / CONTRACT_MD).exists():
        rows.append(
            (
                CONTRACT_MD,
                _detail(
                    lambda: "test_budget={} scope={}".format(
                        artifacts.read_contract(scratch_dir).test_budget,
                        ",".join(artifacts.read_contract(scratch_dir).scope_paths),
                    )
                ),
            )
        )
    for _, path in artifacts.audit_files(scratch_dir):
        rows.append(
            (
                path.name,
                _detail(
                    lambda p=path: "{} commit={}".format(
                        "pass" if artifacts.read_audit(p)["pass"] else "FAIL",
                        artifacts.read_audit(p)["commit"][:7],
                    )
                ),
            )
        )
    for _, path in artifacts.review_files(scratch_dir):
        rows.append(
            (
                path.name,
                _detail(
                    lambda p=path: "{} commit={}".format(
                        artifacts.read_review_front(p)["verdict"],
                        artifacts.read_review_front(p)["commit"][:7],
                    )
                ),
            )
        )
    if (scratch_dir / LEDGER_JSON).exists():
        rows.append(
            (
                LEDGER_JSON,
                _detail(
                    lambda: "rounds_completed={} findings={} open_blocking={}".format(
                        artifacts.read_ledger(scratch_dir)["rounds_completed"],
                        len(artifacts.read_ledger(scratch_dir)["findings"]),
                        len(open_blocking_findings(scratch_dir)),
                    )
                ),
            )
        )
    for name in (CLARIFICATION_MD, ESCALATION_MD, SUMMARY_MD):
        if (scratch_dir / name).exists():
            rows.append((name, "present"))
    logs = scratch_dir / LOGS_DIR
    if logs.is_dir():
        rows.append((f"{LOGS_DIR}/", f"{len(list(logs.iterdir()))} files"))
    return rows
