"""`factory poll` (design §12): one-shot, sequential, timer-driven unattended mode.

0. Preflight: config.auth must be "api" and the selected key present (build_env raises) — else exit 1 before touching
   GitHub. doctor.json must hold a current record for (harness, auth, factory version, harness CLI version), else run
   doctor.doctor(); FAIL -> exit 1.
1. Lock .factory/run/poll.lock (O_EXCL, pid inside; a dead pid makes it stale and it is replaced); held -> exit 0.
2. Discover: gh.list_open_issues_with_label(config.poll_label), ascending.
3. Classify each from local state after repo.fetch() (worktree created from origin/factory/<n> where needed):
   no branch anywhere -> eligible ("new"); outcome None -> eligible ("resume"); needs_human and not
   state.is_parked(...) -> eligible ("operator acted"); parked -> skip; done -> skip;
   journal[n].sha == HEAD and journal[n].failures >= max_consecutive_failures -> skip ("capped").
   The first time an issue is skipped as capped, post one idempotent PR comment (gh.render_capped_comment, CAPPED_MARKER)
   and record capped_comment_sha.
4. Run run_issue(n) for each eligible issue in turn. Exit 0 or 2 -> delete journal[n]. Exit 1 at HEAD h: if
   journal[n].sha == h then failures += 1 else journal[n] = {failures: 1, sha: h}; last_error recorded. The counter is
   consecutive failures at ONE HEAD, so any HEAD movement (operator action) resets it.
5. Exit 1 if any run exited 1, else 0.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .gh import GitHub
from .repo import Repo


@dataclass
class PollResult:
    exit_code: int
    ran: list[int] = field(default_factory=list)
    skipped: dict[int, str] = field(default_factory=dict)  # issue -> reason
    outcomes: dict[int, int] = field(default_factory=dict)  # issue -> exit code of its run


def poll(repo: Repo, gh: GitHub, config: Config, *, parent_env: dict,
         run_issue: Callable[[int], int], out: Callable[[str], None] | None = None) -> PollResult:
    """`run_issue(issue) -> int` is stages.run_issue bound by cli.py (tests inject). Never raises for per-issue
    failures; raises FactoryError only for preflight problems."""
    raise NotImplementedError


def classify(repo: Repo, config: Config, issue: int, journal: dict) -> tuple[bool, str]:
    """(eligible, reason) per step 3. Uses state.is_parked with repo.head/parent/changed_paths_of_commit."""
    raise NotImplementedError


def acquire_poll_lock(factory_dir: Path) -> bool:
    raise NotImplementedError


def release_poll_lock(factory_dir: Path) -> None:
    raise NotImplementedError
