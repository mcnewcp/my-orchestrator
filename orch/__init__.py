"""Issue -> PR orchestrator: a thin, stateless state-machine runner over role skills."""

__all__ = ["ROLES", "SKILL_NAMES", "repo_root"]

from pathlib import Path

#: Role keys, in the order the state machine can reach them.
ROLES = ("contractor", "implementer", "auditor", "reviewer", "judge", "remediator")

#: Role key -> skill directory / skill name (conventions.md section 2).
SKILL_NAMES = {
    "contractor": "orch-contract",
    "implementer": "orch-implement",
    "auditor": "orch-audit",
    "reviewer": "orch-review",
    "judge": "orch-judge",
    "remediator": "orch-remediate",
}


def repo_root() -> Path:
    """Absolute path of the orchestrator repo (the package's parent directory)."""
    return Path(__file__).resolve().parent.parent


def note(message: str) -> None:
    """Print one line of operator-facing progress to stderr."""
    print(f"[orch] {message}", file=__import__("sys").stderr, flush=True)


def warn(message: str) -> None:
    """Print one line of operator-facing warning to stderr."""
    note(f"WARNING: {message}")
