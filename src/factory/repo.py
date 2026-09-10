"""Git operations restricted to the factory's issue branches."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def issue_number(issue: int | str) -> int:
    if isinstance(issue, bool) or not re.fullmatch(r"[1-9][0-9]*", str(issue)):
        raise RuntimeError(f"Invalid issue number: {issue!r}")
    return int(issue)


class Repo:
    def __init__(self, root: Path, base_branch: str = "main"):
        self.root = Path(root).resolve()
        self.root = Path(self.git("rev-parse", "--show-toplevel")).resolve()
        self.local_dir = self.root / ".factory"
        self.base_branch = base_branch
        self.git("check-ref-format", f"refs/heads/{base_branch}")
        self._fetched_heads: dict[str, str] | None = None

    def _run(self, *args: str, cwd: Path | None = None, input: str | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["git", *map(str, args)], cwd=cwd or self.root,
                capture_output=True, text=True, errors="surrogateescape", check=False, input=input,
            )
        except OSError as exc:
            raise RuntimeError(f"Cannot run git: {exc}") from exc

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        result = self._run(*args, cwd=cwd)
        if check and result.returncode:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
        return result.stdout.rstrip("\n")

    @staticmethod
    def branch(issue: int | str) -> str:
        return f"factory/{issue_number(issue)}"

    def fetch(self) -> None:
        self.git("fetch", "--prune", "origin")
        refs = self.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes/origin/factory/")
        self._fetched_heads = {
            ref.removeprefix("refs/remotes/origin/"): sha
            for ref, sha in (line.split() for line in refs.splitlines())
        }

    def branch_exists(self, issue: int | str, remote: bool = False) -> bool:
        prefix = "refs/remotes/origin/" if remote else "refs/heads/"
        result = self._run("show-ref", "--verify", "--quiet", prefix + self.branch(issue))
        if result.returncode not in (0, 1):
            raise RuntimeError(f"Cannot inspect branch: {result.stderr.strip()}")
        return result.returncode == 0

    def _worktrees(self) -> dict[str, Path]:
        trees = {}
        for entry in self.git("worktree", "list", "--porcelain", "-z").split("\0\0"):
            fields = dict(line.split(" ", 1) for line in entry.split("\0") if " " in line)
            if "branch" in fields and "worktree" in fields:
                trees[fields["branch"]] = Path(fields["worktree"])
        return trees

    def worktree(self, issue: int | str, create: bool = True) -> Path | None:
        branch = self.branch(issue)
        path = self.local_dir / "worktrees" / str(issue_number(issue))
        if not create:
            registered = self._worktrees().get(f"refs/heads/{branch}")
            if registered != path or not (path / ".git").is_file():
                return None
            return path
        # A discarded .factory directory leaves linked-worktree registration behind.
        self.git("worktree", "prune", "--expire", "now")
        registered = self._worktrees().get(f"refs/heads/{branch}")
        if registered:
            if registered != path:
                raise RuntimeError(f"{branch} is already checked out at {registered}")
            return registered
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.branch_exists(issue):
            self.git("worktree", "add", str(path), branch)
        elif self.branch_exists(issue, remote=True):
            self.git("worktree", "add", "-b", branch, str(path), f"refs/remotes/origin/{branch}")
        else:
            self.git("worktree", "add", "-b", branch, str(path), f"refs/remotes/origin/{self.base_branch}")
        return path

    def head(self, cwd: Path) -> str:
        return self.git("rev-parse", "--verify", "HEAD", cwd=cwd)

    def resolve(self, cwd: Path, ref: str) -> str:
        """Resolve immutable state references and prove that they are in HEAD's history.

        State cannot contain the hash of the commit that contains state itself.
        A checkpoint token instead names an exact commit-message trailer.
        """
        if ref.startswith("checkpoint:"):
            token = ref.removeprefix("checkpoint:")
            if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", token):
                raise RuntimeError(f"Invalid checkpoint reference: {ref!r}")
            trailer = f"Factory-Checkpoint: {token}"
            log = self.git("log", "--format=%H%x00%B%x00", "--fixed-strings", f"--grep={trailer}", "HEAD", cwd=cwd)
            pieces = log.split("\0")
            matches = [
                pieces[index].strip() for index in range(0, len(pieces) - 1, 2)
                if trailer in pieces[index + 1].splitlines()
            ]
            if len(matches) != 1:
                raise RuntimeError(f"Checkpoint {ref} is missing or ambiguous in HEAD history")
            sha = matches[0]
        else:
            if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", ref):
                raise RuntimeError(f"Invalid recorded commit: {ref!r}")
            sha = self.git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=cwd)
        if not self._ancestor(sha, "HEAD", cwd):
            raise RuntimeError(f"Recorded commit {ref} is not reachable from HEAD")
        return sha

    def commit(self, cwd: Path, message: str) -> str:
        if self.git("diff", "--cached", "--name-only", "--", ".factory", cwd=cwd):
            raise RuntimeError("Host-local .factory files must not be staged")
        if self.git("diff", "--name-only", "--diff-filter=U", cwd=cwd):
            raise RuntimeError("Resolve merge conflicts before committing")
        # Already-staged deletions and rename sources no longer exist in the index;
        # passing them to git add would fail. Only stage remaining working changes.
        unstaged = self.git("diff", "--name-only", "--no-renames", "-z", cwd=cwd)
        untracked = self.git("ls-files", "--others", "--exclude-standard", "-z", cwd=cwd)
        paths = sorted({
            path for path in (unstaged + untracked).split("\0")
            if path and path != ".factory" and not path.startswith(".factory/")
        })
        if paths:
            result = self._run(
                "--literal-pathspecs", "add", "--all", "--pathspec-from-file=-", "--pathspec-file-nul",
                cwd=cwd, input="\0".join(paths) + "\0",
            )
            if result.returncode:
                raise RuntimeError(f"Cannot stage changes: {result.stderr.strip()}")
        result = self._run("diff", "--cached", "--quiet", cwd=cwd)
        if result.returncode == 1:
            self.git("commit", "-m", message, cwd=cwd)
        elif result.returncode:
            raise RuntimeError(f"Cannot inspect staged changes: {result.stderr.strip()}")
        return self.head(cwd)

    def push(self, issue: int | str, force: bool = False) -> None:
        branch = self.branch(issue)
        ref = f"refs/heads/{branch}"
        args = ["push"]
        if force:
            if self._fetched_heads is None:
                raise RuntimeError("Force push requires fetch() first to establish an explicit lease")
            args.append(f"--force-with-lease={ref}:{self._fetched_heads.get(branch, '')}")
        self.git(*args, "origin", f"{ref}:{ref}")

    def changed_paths(self, cwd: Path) -> list[str]:
        entries = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=cwd).split("\0")
        paths = set()
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            paths.add(entry[3:])
            if "R" in entry[:2] or "C" in entry[:2]:
                paths.add(entries[index])
                index += 1
        return sorted(path for path in paths if path != ".factory" and not path.startswith(".factory/"))

    def reset(self, cwd: Path, sha: str = "HEAD") -> None:
        self.git("reset", "--hard", sha, cwd=cwd)
        self.git("clean", "-fd", "-e", ".factory/", cwd=cwd)

    def _ancestor(self, older: str, newer: str, cwd: Path) -> bool:
        result = self._run("merge-base", "--is-ancestor", older, newer, cwd=cwd)
        if result.returncode not in (0, 1):
            raise RuntimeError(f"Cannot compare git history: {result.stderr.strip()}")
        return result.returncode == 0

    def sync(self, cwd: Path, issue: int | str) -> None:
        branch = self.branch(issue)
        if self.git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=cwd) != branch:
            raise RuntimeError(f"Expected worktree on {branch}")
        if not self.branch_exists(issue, remote=True):
            return
        remote = f"refs/remotes/origin/{branch}"
        if self._ancestor(remote, "HEAD", cwd):
            return
        if not self._ancestor("HEAD", remote, cwd):
            raise RuntimeError(f"{branch} has diverged from origin; reconcile it manually")
        self.git("merge", "--ff-only", remote, cwd=cwd)

    def validate_clean(self, cwd: Path, issue: int | str) -> None:
        paths = self.changed_paths(cwd)
        prefix = f"work/{issue_number(issue)}/"
        outside = [path for path in paths if not path.startswith(prefix)]
        if outside:
            raise RuntimeError("Uncommitted changes outside issue artifacts: " + ", ".join(outside))
        if paths:
            self.commit(cwd, f"factory: record operator edits for issue #{issue}")

    def diff(self, cwd: Path, base: str) -> str:
        return self.git("diff", f"{base}...HEAD", "--", ".", ":(exclude)work", ":(exclude).factory", cwd=cwd)

    def abandon(self, issue: int | str) -> None:
        branch = self.branch(issue)
        # Delete only our explicit ref; an absent remote branch is already done.
        self.fetch()
        if self.branch_exists(issue, remote=True):
            expected = self._fetched_heads[branch]
            self.git("push", f"--force-with-lease=refs/heads/{branch}:{expected}", "origin", f":refs/heads/{branch}")
        self.git("worktree", "prune", "--expire", "now")
        registered = self._worktrees().get(f"refs/heads/{branch}")
        if registered:
            expected_path = self.local_dir / "worktrees" / str(issue_number(issue))
            if registered != expected_path:
                raise RuntimeError(f"Refusing to remove worktree outside .factory: {registered}")
            self.git("worktree", "remove", "--force", str(registered))
        if self.branch_exists(issue):
            self.git("branch", "-D", branch)
