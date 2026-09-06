"""Git plumbing (design §5 repo.py): worktree, branch, commit, diff, reset, force-with-lease push.

All commands run via subprocess.run(argv, shell=False, capture_output=True, text=True, stdin=DEVNULL) with
GIT_TERMINAL_PROMPT=0 in the environment. A non-zero exit raises FactoryError with the stderr tail.
Real git only — §17.2 says "fake git"; this build uses real git against a local bare "origin" instead
(tests/FAKES.md): none of the §17.2 proofs depends on faking git, and real worktree/push semantics are what
start-of-command validation actually asserts about.

Layout (design §5):
  checkout_root/                      the clone the command is run in (stays on its own branch)
  checkout_root/.factory/worktrees/<issue>   worktree on branch factory/<issue>
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


def branch_name(issue: int) -> str:
    return f"factory/{issue}"


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def git(args: list[str], cwd: Path, check: bool = True, env: dict | None = None) -> GitResult:
    """Run `git <args>` in cwd. check=True raises FactoryError on non-zero exit."""
    raise NotImplementedError


class Repo:
    def __init__(self, checkout_root: Path):
        self.root = checkout_root
        self.factory_dir = checkout_root / ".factory"

    @classmethod
    def discover(cls, cwd: Path) -> Repo:
        """`git rev-parse --show-toplevel` from cwd; FactoryError if not in a git checkout.
        Refuses to run from inside a factory worktree (path under .factory/worktrees)."""
        raise NotImplementedError

    # --- refs
    def fetch(self) -> None:
        """`git fetch origin --prune`."""
        raise NotImplementedError

    def rev_parse(self, ref: str, cwd: Path | None = None) -> str:
        raise NotImplementedError

    def head(self, cwd: Path) -> str:
        return self.rev_parse("HEAD", cwd)

    def parent(self, sha: str, cwd: Path | None = None) -> str | None:
        """First parent, or None for a root commit."""
        raise NotImplementedError

    def local_branch_exists(self, branch: str) -> bool:
        raise NotImplementedError

    def remote_branch_exists(self, branch: str) -> bool:
        """True if refs/remotes/origin/<branch> exists (after fetch)."""
        raise NotImplementedError

    def is_ancestor(self, a: str, b: str, cwd: Path | None = None) -> bool:
        """`git merge-base --is-ancestor a b`."""
        raise NotImplementedError

    # --- worktrees
    def worktree_path(self, issue: int) -> Path:
        return self.factory_dir / "worktrees" / str(issue)

    def worktree_exists(self, issue: int) -> bool:
        raise NotImplementedError

    def ensure_worktree(self, issue: int, *, start_point: str | None = None) -> Path:
        """Return a worktree for branch factory/<issue>, creating what is missing (design §7 worktree recovery):
        - worktree exists -> return it
        - local branch exists -> add worktree on it
        - origin/factory/<issue> exists -> create local branch tracking it, add worktree
        - none of the above and start_point given -> create branch at start_point, add worktree
        - otherwise FactoryError("no branch for issue N; run `factory spec N`")
        Always calls ensure_excludes() afterwards (idempotent).
        """
        raise NotImplementedError

    def ensure_excludes(self, patterns: list[str]) -> None:
        """Append missing patterns to `<checkout>/.git/info/exclude` (the common dir, so every worktree inherits them).
        The factory's own tooling droppings (checks.TRANSIENT_PATHS + config.transient_paths) therefore never make a
        worktree dirty and never depend on the target repo's committed .gitignore. Idempotent."""
        raise NotImplementedError

    def remove_worktree(self, issue: int) -> None:
        """`git worktree remove --force` (ignore if absent) + `git worktree prune`."""
        raise NotImplementedError

    def delete_branch(self, branch: str, *, remote: bool) -> None:
        """Local `git branch -D` (ignore if absent); remote `git push origin --delete` (ignore if absent)."""
        raise NotImplementedError

    # --- working tree state
    def status_porcelain(self, wt: Path) -> list[str]:
        """`git status --porcelain=v1 --untracked-files=all` lines (paths relative to wt)."""
        raise NotImplementedError

    def changed_paths_in_worktree(self, wt: Path) -> list[str]:
        """Paths from status_porcelain (staged, unstaged, untracked); renames report the new path."""
        raise NotImplementedError

    def is_clean(self, wt: Path) -> bool:
        return not self.status_porcelain(wt)

    def commit_all(self, wt: Path, message: str, *, paths: list[str] | None = None) -> str:
        """`git add -A [-- paths]` then `git commit -m message`; returns the new HEAD sha.
        Commits with the checkout's identity; falls back to default_identity_env() when git has none configured.
        FactoryError if there is nothing to commit (callers that may legitimately have nothing use has_changes first)."""
        raise NotImplementedError

    def has_changes(self, wt: Path, paths: list[str] | None = None) -> bool:
        raise NotImplementedError

    def reset_hard(self, wt: Path, ref: str = "HEAD") -> None:
        """`git reset --hard ref` + `git clean -fd` (untracked files and dirs, respecting .gitignore)."""
        raise NotImplementedError

    def checkout_paths(self, wt: Path, ref: str, paths: list[str]) -> None:
        """`git checkout ref -- paths` (restore files from a commit); missing paths are ignored."""
        raise NotImplementedError

    # --- diffs
    def diff(self, wt: Path, base: str, head: str = "HEAD", *, exclude: tuple[str, ...] = ("work",)) -> str:
        """`git diff <base>...<head> -- . ':!<exclude>'...` (design §9 review input)."""
        raise NotImplementedError

    def diff_stat(self, wt: Path, base: str, head: str = "HEAD", *, exclude: tuple[str, ...] = ("work",)) -> str:
        """`git diff --stat <base>...<head> -- . ':!<exclude>'`."""
        raise NotImplementedError

    def diff_numstat(self, wt: Path, base: str, head: str = "HEAD", *, exclude: tuple[str, ...] = ("work",)) -> list[tuple[int, int, str]]:
        """[(added, deleted, path)] from `git diff --numstat`; binary files report (0, 0, path)."""
        raise NotImplementedError

    def diff_paths(self, wt: Path, base: str, head: str, paths: list[str]) -> str:
        """`git diff <base>...<head> -- <paths>` for a subset of files (used to build a truncated review diff)."""
        raise NotImplementedError

    def changed_paths_between(self, wt: Path, base: str, head: str = "HEAD") -> list[str]:
        """`git diff --name-only base head`."""
        raise NotImplementedError

    def changed_paths_of_commit(self, wt: Path, sha: str) -> list[str]:
        """`git diff-tree --no-commit-id --name-only -r sha`."""
        raise NotImplementedError

    def code_changed_between(self, wt: Path, a: str, b: str = "HEAD") -> bool:
        """True if anything outside work/ differs between a and b (`git diff --quiet a b -- . ':!work'`)."""
        raise NotImplementedError

    # --- remote
    def push(self, wt: Path, branch: str, *, force_with_lease: bool = False) -> None:
        """`git push -u origin branch` (with --force-with-lease when asked)."""
        raise NotImplementedError

    def push_if_ahead(self, wt: Path, branch: str) -> bool:
        """Push when HEAD is strictly ahead of origin/<branch> (or the remote branch is absent); returns whether it
        pushed. Used by prepare() so a local-only accept/dismiss commit survives the host (rule 6)."""
        raise NotImplementedError

    def remote_ahead_state(self, wt: Path, branch: str) -> str:
        """One of 'absent' | 'equal' | 'local_ahead' | 'remote_ahead' | 'diverged' for HEAD vs origin/<branch>."""
        raise NotImplementedError

    def sync_with_remote(self, wt: Path, branch: str) -> str:
        """Start-of-command validation (design §7): after fetch, if origin/<branch> is strictly ahead of HEAD,
        fast-forward (`git merge --ff-only`); if diverged, FactoryError; if absent or equal/behind, no-op.
        Returns HEAD after the sync."""
        raise NotImplementedError

    def base_sha(self, base_branch: str) -> str:
        """`origin/<base_branch>` after fetch."""
        raise NotImplementedError

    def show_file(self, ref: str, path: str, cwd: Path | None = None) -> str | None:
        """`git show ref:path`; None if absent."""
        raise NotImplementedError

    def log_touching(self, wt: Path, path: str, n: int = 1) -> list[str]:
        """Most recent commit shas touching `path`."""
        raise NotImplementedError

    def worktree_for_issue_is_on_branch(self, wt: Path, branch: str) -> bool:
        raise NotImplementedError


def default_identity_env() -> dict[str, str]:
    """GIT_AUTHOR_*/GIT_COMMITTER_* fallback ("factory", "factory@localhost") used only when git has no identity."""
    raise NotImplementedError


_ = subprocess  # keep import for implementers
