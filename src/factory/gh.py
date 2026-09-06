"""GitHub via the `gh` CLI (design §5 gh.py, §7 "The GitHub rule").

Reads happen for exactly three purposes: issue snapshot at spec, idempotency of the factory's own
writes (does a PR / comment already exist), and `poll`'s label query. `gh` runs with the parent
environment (it needs GH_TOKEN or its own login) — harness subprocesses never get that environment.

All calls: subprocess.run(["gh", ...], cwd=checkout_root, shell=False). Non-zero -> FactoryError with stderr tail.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import FactoryError

# Markers make every PR write idempotent (pr_comment scans existing comments for the marker — authoritative;
# nothing in state.json caches this). `sha` in GATE_MARKER is state.outcome_sha, which is stable across park()'s
# own state-only commit, so a re-park at the same HEAD finds its marker.
GATE_MARKER = "<!-- factory:gate:{gate}:{sha} -->"
REVIEW_MARKER = "<!-- factory:review:{round}:{sha} -->"
SUMMARY_MARKER = "<!-- factory:summary:{sha} -->"
CAPPED_MARKER = "<!-- factory:capped:{sha} -->"


@dataclass
class Issue:
    number: int
    title: str
    body: str
    url: str
    labels: list[str]
    state: str

    def to_dict(self) -> dict:
        raise NotImplementedError


@dataclass
class PullRequest:
    number: int
    url: str
    is_draft: bool
    state: str  # OPEN | CLOSED | MERGED
    head: str


class GitHub:
    def __init__(self, checkout_root: Path):
        self.root = checkout_root

    def _run(self, args: list[str], *, check: bool = True) -> str:
        """`gh <args>`; returns stdout."""
        raise NotImplementedError

    def _json(self, args: list[str]):
        raise NotImplementedError

    def repo_slug(self) -> str:
        """`gh repo view --json nameWithOwner`."""
        raise NotImplementedError

    def auth_ok(self) -> tuple[bool, str]:
        """`gh auth status`; (ok, one-line detail)."""
        raise NotImplementedError

    # --- issues
    def issue(self, number: int) -> Issue:
        """`gh issue view N --json number,title,body,url,labels,state`."""
        raise NotImplementedError

    def list_open_issues_with_label(self, label: str) -> list[int]:
        """Ascending issue numbers (design §12 step 2)."""
        raise NotImplementedError

    def remove_label(self, number: int, label: str) -> None:
        """Idempotent: absent label is not an error."""
        raise NotImplementedError

    def ensure_label(self, label: str, *, color: str = "5319e7", description: str = "") -> bool:
        """Create the label if absent. Returns True if created."""
        raise NotImplementedError

    # --- pull requests
    def find_pr_for_branch(self, branch: str) -> PullRequest | None:
        """`gh pr list --head <branch> --state all --json ...`; prefer OPEN, else most recent."""
        raise NotImplementedError

    def create_draft_pr(self, *, branch: str, base: str, title: str, body: str) -> PullRequest:
        """Idempotent: if an OPEN PR for branch exists, return it instead (design §15)."""
        raise NotImplementedError

    def pr_comments(self, number: int) -> list[str]:
        """Bodies of existing comments (for marker-based idempotency)."""
        raise NotImplementedError

    def pr_comment(self, number: int, body: str, *, marker: str | None = None) -> bool:
        """Post a comment unless one containing `marker` already exists. Returns True if posted."""
        raise NotImplementedError

    def pr_ready(self, number: int) -> None:
        """`gh pr ready N`; idempotent (already-ready is not an error)."""
        raise NotImplementedError

    def pr_close(self, number: int, comment: str | None = None) -> None:
        """Idempotent."""
        raise NotImplementedError


def render_gate_comment(gate: str, what_clears_it: str, sha: str, issue: int) -> str:
    """PR comment body for an exit-2 gate (design §9): names the gate and what clears it; includes GATE_MARKER."""
    raise NotImplementedError


def render_review_comment(round: int, sha: str, summary: str, ledger_md: str, stats: dict) -> str:
    """PR comment body after a review round: summary, rendered ledger, counts; includes REVIEW_MARKER."""
    raise NotImplementedError


def render_summary_comment(state: dict, ledger_md: str, checks_tail: str, sha: str) -> str:
    """Finalize summary (design §2 step 6): spec/plan pointers, check output tail, ledger, harness + versions;
    includes SUMMARY_MARKER."""
    raise NotImplementedError


def render_capped_comment(issue: int, sha: str, failures: int, last_error: str) -> str:
    """poll: posted once when an issue hits max_consecutive_failures at `sha` (design §12 has no comment for exit 1;
    without this an unattended issue goes dark). Says the count clears when HEAD moves. Includes CAPPED_MARKER."""
    raise NotImplementedError


_ = FactoryError
