"""Deterministic checks and allowed-edit rules (design §9).

Checks run as argv arrays without a shell, cwd = worktree, env = harness.checks_env(...) (no provider keys), via
harness.run_streaming (process-group kill on timeout; output streamed to a file, then read back).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

PROTECTED_PATHS: tuple[str, ...] = (
    "Makefile", "factory.toml", "AGENTS.md", "CLAUDE.md", "REVIEW.md",
    ".devcontainer/", ".claude/", ".mcp.json", ".codex/", ".github/",
)
# Tooling droppings the factory itself creates by running the checks (and its own .factory/tmp inside the worktree).
# Never the operator's edits, never a stage's output. Written to the checkout's .git/info/exclude by
# Repo.ensure_excludes and ignored by every clean/dirty comparison via is_transient().
TRANSIENT_PATHS: tuple[str, ...] = (
    ".factory/", ".venv/", "venv/", "__pycache__/", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/", ".tox/",
    "node_modules/", ".gradle/", "target/", "*.pyc", "*.egg-info/",
)
STAGE_LOG_ORDER = ("build", "fix", "finalize")


@dataclass
class CheckRun:
    ok: bool
    log: str  # full combined output of every check, with a header line per command and its exit code
    failed: list[str] = field(default_factory=list)  # "make test" style names of failing commands


def run_checks(cwd: Path, config: Config, env: dict[str, str], timeout_s: int) -> CheckRun:
    """Run every config.checks entry in order; stop at the first failure (its output is still captured).
    A timeout or a missing binary counts as a failure. Log format per check:
        $ make test
        <stdout+stderr>
        [exit 0]
    """
    raise NotImplementedError


def check_log_path(worktree: Path, issue: int, stage: str, round: int, suffix: str = "") -> Path:
    return worktree / "work" / str(issue) / "checks" / f"{stage}-{round}{suffix}.log"


def write_check_log(worktree: Path, issue: int, stage: str, round: int, log: str, *, suffix: str = "") -> Path:
    """work/<issue>/checks/<stage>-<round><suffix>.log (committed with the stage). Round convention: build always 1
    (a --force rewind removes the previous attempt's log; a plain re-run overwrites it); fix uses state.fix_rounds + 1;
    review has no log; finalize uses 1. suffix is "-baseline" for build's pre-session run."""
    raise NotImplementedError


def latest_check_log(worktree: Path, issue: int) -> tuple[Path | None, str]:
    """The newest work/<issue>/checks/*.log by (file mtime desc, then STAGE_LOG_ORDER, then integer round desc);
    returns (path, text) or (None, "(none)"). The single accessor used by review's {checks}, build/fix's {checks},
    and finalize's summary comment."""
    raise NotImplementedError


def is_protected(path: str, config: Config) -> bool:
    """PROTECTED_PATHS + config.protected_paths. Entries ending in '/' match the directory prefix;
    others match the exact path or the path as a directory prefix (".github" matches ".github/x.yml")."""
    raise NotImplementedError


def is_transient(path: str, config: Config) -> bool:
    """TRANSIENT_PATHS + config.transient_paths: '/'-suffixed entries match any path component prefix
    (".venv/" matches ".venv/x" and "sub/.venv/x"); '*.ext' entries match by suffix."""
    raise NotImplementedError


def is_under(path: str, prefixes: list[str]) -> bool:
    raise NotImplementedError


def allowed_edit_violations(changed_paths: list[str], *, stage: str, issue: int, config: Config,
                            ignore: list[str] | None = None) -> list[str]:
    """Return offending paths for a write stage's changes (design §9). Scope: the write stage's OWN diff (the
    branch-level protected-path check before a session launches is stages.branch_protected_path_violations).
      - paths in `ignore` (the factory's own rendered prompt file) and is_transient() paths are never violations
      - any protected path -> violation (both stages)
      - build: inside work/<issue>/ only plan.md is allowed; any other work/ path is a violation
      - fix:   any path under config.test_paths or anything under work/ is a violation
    Paths are worktree-relative posix strings.
    """
    raise NotImplementedError


def paths_missing_from_plan(changed_paths: list[str], plan_md: str, *, issue: int) -> list[str]:
    """Design §9 build gate "every changed path listed in plan.md": a changed path (outside work/) is listed
    when the exact relative path appears verbatim anywhere in plan_md. Returns the unlisted ones."""
    raise NotImplementedError


def plan_has_required_sections(plan_md: str) -> list[str]:
    """Design §9 plan gate: returns the missing headings among "## Files that change" and "## Proof"
    (case-insensitive match on the heading text, any heading level >= 2)."""
    raise NotImplementedError


def tail(text: str, lines: int = 60) -> str:
    raise NotImplementedError
