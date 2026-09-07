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

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .checks import TRANSIENT_PATHS
from .errors import FactoryError

# --no-pager: git must never try to page into a captured pipe.
# core.quotepath=false: non-ASCII paths come back verbatim instead of as \nnn escapes.
_GIT_BASE = ("git", "--no-pager", "-c", "core.quotepath=false")

_IDENTITY_KEYS = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
)
_DEFAULT_NAME = "factory"
_DEFAULT_EMAIL = "factory@localhost"
_EXCLUDE_HEADER = "# factory: transient paths (repo.ensure_excludes)"

# `git worktree` names its worktrees by directory; these two components identify one of ours.
_WORKTREES_MARKER = (".factory", "worktrees")


def branch_name(issue: int) -> str:
    return f"factory/{issue}"


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def git(args: list[str], cwd: Path, check: bool = True, env: dict | None = None) -> GitResult:
    """Run `git <args>` in cwd. check=True raises FactoryError on non-zero exit.

    stdin is /dev/null and GIT_TERMINAL_PROMPT=0, so git can never block waiting for a human.
    `env` is overlaid on the parent environment (commit_all uses it for the identity fallback).
    """
    proc_env = dict(os.environ)
    proc_env["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        proc_env.update(env)
    try:
        proc = subprocess.run(
            [*_GIT_BASE, *args],
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            env=proc_env,
        )
    except OSError as exc:
        raise FactoryError(f"could not run `git {' '.join(args)}` in {cwd}: {exc}") from exc
    result = GitResult(proc.returncode, proc.stdout, proc.stderr)
    if check and result.returncode != 0:
        raise FactoryError(
            f"git {' '.join(args)} failed in {cwd} (exit {result.returncode}): {_tail(result.stderr)}"
        )
    return result


def _tail(text: str, lines: int = 8) -> str:
    """The last non-empty lines of git's stderr, flattened onto one line for an error message."""
    kept = [ln.strip() for ln in text.splitlines() if ln.strip()][-lines:]
    return " | ".join(kept) if kept else "(no output)"


def _split_z(text: str) -> list[str]:
    return [tok for tok in text.split("\0") if tok]


def _is_worktree_path(path: Path) -> bool:
    parts = path.parts
    return any(parts[i : i + 2] == _WORKTREES_MARKER for i in range(len(parts) - 1))


class Repo:
    def __init__(self, checkout_root: Path):
        self.root = checkout_root
        self.factory_dir = checkout_root / ".factory"

    @classmethod
    def discover(cls, cwd: Path) -> Repo:
        """`git rev-parse --show-toplevel` from cwd; FactoryError if not in a git checkout.
        Refuses to run from inside a factory worktree (path under .factory/worktrees)."""
        here = Path(cwd).resolve()
        res = git(["rev-parse", "--show-toplevel"], cwd, check=False)
        if res.returncode != 0:
            raise FactoryError(
                f"{here} is not inside a git checkout: {_tail(res.stderr)}",
                hint="run factory from the checkout of the repository you want it to work on",
            )
        top = Path(res.stdout.strip())
        offender = here if _is_worktree_path(here) else (top if _is_worktree_path(top) else None)
        if offender is not None:
            raise FactoryError(
                f"refusing to run inside a factory worktree: {offender}",
                hint="run factory from the checkout that owns .factory/, not from .factory/worktrees/<issue>",
            )
        return cls(top)

    # --- refs
    def fetch(self) -> None:
        """`git fetch origin --prune`."""
        git(["fetch", "origin", "--prune"], self.root)

    def rev_parse(self, ref: str, cwd: Path | None = None) -> str:
        return git(["rev-parse", "--verify", ref], cwd or self.root).stdout.strip()

    def head(self, cwd: Path) -> str:
        return self.rev_parse("HEAD", cwd)

    def parent(self, sha: str, cwd: Path | None = None) -> str | None:
        """First parent, or None for a root commit."""
        fields = git(["rev-list", "--parents", "-n", "1", sha], cwd or self.root).stdout.split()
        return fields[1] if len(fields) > 1 else None

    def local_branch_exists(self, branch: str) -> bool:
        return self._ref_exists(f"refs/heads/{branch}")

    def remote_branch_exists(self, branch: str) -> bool:
        """True if refs/remotes/origin/<branch> exists (after fetch)."""
        return self._ref_exists(f"refs/remotes/origin/{branch}")

    def _ref_exists(self, ref: str) -> bool:
        return git(["show-ref", "--verify", "--quiet", ref], self.root, check=False).returncode == 0

    def is_ancestor(self, a: str, b: str, cwd: Path | None = None) -> bool:
        """`git merge-base --is-ancestor a b`. A commit this repository does not have is reported as
        not an ancestor (that is exactly what the caller — a reachability check over state.json — asks)."""
        res = git(["merge-base", "--is-ancestor", a, b], cwd or self.root, check=False)
        if res.returncode in (0, 1):
            return res.returncode == 0
        lowered = res.stderr.lower()
        if "not a valid" in lowered or "bad object" in lowered or "unknown revision" in lowered:
            return False
        raise FactoryError(
            f"git merge-base --is-ancestor {a} {b} failed in {cwd or self.root}: {_tail(res.stderr)}"
        )

    # --- worktrees
    def worktree_path(self, issue: int) -> Path:
        return self.factory_dir / "worktrees" / str(issue)

    def worktree_exists(self, issue: int) -> bool:
        wt = self.worktree_path(issue)
        return (wt / ".git").exists() and wt.resolve() in self._registered_worktrees()

    def _registered_worktrees(self) -> set[Path]:
        out = git(["worktree", "list", "--porcelain"], self.root).stdout
        return {
            Path(line[len("worktree ") :]).resolve()
            for line in out.splitlines()
            if line.startswith("worktree ")
        }

    def ensure_worktree(self, issue: int, *, start_point: str | None = None) -> Path:
        """Return a worktree for branch factory/<issue>, creating what is missing (design §7 worktree recovery):
        - worktree exists -> return it
        - local branch exists -> add worktree on it
        - origin/factory/<issue> exists -> create local branch tracking it, add worktree
        - none of the above and start_point given -> create branch at start_point, add worktree
        - otherwise FactoryError("no branch for issue N; run `factory spec N`")
        Always calls ensure_excludes() afterwards (idempotent).
        """
        wt = self.worktree_path(issue)
        branch = branch_name(issue)
        if self.worktree_exists(issue):
            self.ensure_excludes()
            return wt
        # A half-there worktree (directory wiped with .factory/, or registration without a checkout)
        # would make `git worktree add` refuse; the host is disposable, so discard what is left.
        self._discard_worktree(issue)
        wt.parent.mkdir(parents=True, exist_ok=True)
        if self.local_branch_exists(branch):
            git(["worktree", "add", str(wt), branch], self.root)
        elif self.remote_branch_exists(branch):
            git(
                ["worktree", "add", "--track", "-b", branch, str(wt), f"origin/{branch}"], self.root
            )
        elif start_point:
            git(["worktree", "add", "-b", branch, str(wt), start_point], self.root)
        else:
            raise FactoryError(
                f"no branch for issue {issue}; run `factory spec {issue}`",
                hint=f"neither {branch} nor origin/{branch} exists in {self.root}",
            )
        self.ensure_excludes()
        return wt

    def ensure_excludes(self, patterns: list[str] | None = None) -> None:
        """Append missing patterns to `<checkout>/.git/info/exclude` (the common dir, so every worktree inherits them).
        The factory's own tooling droppings (checks.TRANSIENT_PATHS + config.transient_paths) therefore never make a
        worktree dirty and never depend on the target repo's committed .gitignore. Idempotent.

        `patterns` defaults to checks.TRANSIENT_PATHS: ensure_worktree has no config, and a caller that also has
        config.transient_paths simply calls again with the longer list."""
        wanted = list(TRANSIENT_PATHS) if patterns is None else list(patterns)
        path = self._common_git_dir() / "info" / "exclude"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        present = {line.strip() for line in text.splitlines()}
        missing = [p for p in dict.fromkeys(wanted) if p.strip() and p.strip() not in present]
        if not missing:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        separator = "" if text == "" or text.endswith("\n") else "\n"
        header = "" if _EXCLUDE_HEADER in present else f"{_EXCLUDE_HEADER}\n"
        path.write_text(text + separator + header + "\n".join(missing) + "\n", encoding="utf-8")

    def _common_git_dir(self) -> Path:
        """The shared `.git` directory: `info/` lives there, so linked worktrees read the same excludes."""
        out = git(["rev-parse", "--git-common-dir"], self.root).stdout.strip()
        path = Path(out)
        return path if path.is_absolute() else self.root / path

    def remove_worktree(self, issue: int) -> None:
        """`git worktree remove --force` (ignore if absent) + `git worktree prune`."""
        self._discard_worktree(issue)

    def _discard_worktree(self, issue: int) -> None:
        wt = self.worktree_path(issue)
        git(["worktree", "remove", "--force", str(wt)], self.root, check=False)
        git(["worktree", "prune"], self.root, check=False)
        if wt.exists():
            # `worktree remove` refuses a directory it does not recognise; everything under
            # .factory/ is host-local and expendable (design §7, rule 6).
            shutil.rmtree(wt, ignore_errors=True)

    def delete_branch(self, branch: str, *, remote: bool) -> None:
        """Local `git branch -D` (ignore if absent); remote `git push origin --delete` (ignore if absent)."""
        if remote:
            git(["push", "origin", "--delete", branch], self.root, check=False)
        git(["branch", "-D", branch], self.root, check=False)

    # --- working tree state
    def status_porcelain(self, wt: Path) -> list[str]:
        """`git status --porcelain=v1 --untracked-files=all` lines (paths relative to wt)."""
        return git(["status", "--porcelain=v1", "--untracked-files=all"], wt).stdout.splitlines()

    def changed_paths_in_worktree(self, wt: Path) -> list[str]:
        """Paths from status_porcelain (staged, unstaged, untracked); renames report the new path."""
        return self._status_paths(wt, None)

    def _status_paths(self, wt: Path, paths: list[str] | None) -> list[str]:
        """Same statuses as status_porcelain, parsed from the NUL-separated form so paths are exact
        (no quoting) and a rename's two paths cannot be confused. Optionally limited to a pathspec."""
        args = ["status", "--porcelain=v1", "--untracked-files=all", "-z"]
        if paths:
            args += ["--", *paths]
        tokens = git(args, wt).stdout.split("\0")
        out: list[str] = []
        i = 0
        while i < len(tokens):
            entry = tokens[i]
            i += 1
            if len(entry) < 4:
                continue
            out.append(entry[3:])
            if entry[0] in ("R", "C"):
                i += 1  # the pre-image path follows as its own token
        return out

    def is_clean(self, wt: Path) -> bool:
        return not self.status_porcelain(wt)

    def commit_all(self, wt: Path, message: str, *, paths: list[str] | None = None) -> str:
        """`git add -A [-- paths]` then `git commit -m message`; returns the new HEAD sha.
        Commits with the checkout's identity; falls back to default_identity_env() when git has none configured.
        FactoryError if there is nothing to commit (callers that may legitimately have nothing use has_changes first)."""
        pathspec = ["--", *paths] if paths else []
        git(["add", "-A", *pathspec], wt)
        if git(["diff", "--cached", "--quiet", *pathspec], wt, check=False).returncode == 0:
            scope = f" for {', '.join(paths)}" if paths else ""
            raise FactoryError(f"nothing to commit in {wt}{scope}")
        git(
            ["commit", "--no-gpg-sign", "-m", message, *pathspec],
            wt,
            env=self._identity_env(wt),
        )
        return self.head(wt)

    def _identity_env(self, cwd: Path) -> dict[str, str] | None:
        if all(os.environ.get(key) for key in _IDENTITY_KEYS):
            return None
        configured = all(
            git(["config", "--get", key], cwd, check=False).returncode == 0
            for key in ("user.name", "user.email")
        )
        return None if configured else default_identity_env()

    def has_changes(self, wt: Path, paths: list[str] | None = None) -> bool:
        return bool(self._status_paths(wt, paths))

    def reset_hard(self, wt: Path, ref: str = "HEAD") -> None:
        """`git reset --hard ref` + `git clean -fd` (untracked files and dirs, respecting .gitignore)."""
        git(["reset", "--hard", ref], wt)
        git(["clean", "-fd"], wt)

    def checkout_paths(self, wt: Path, ref: str, paths: list[str]) -> None:
        """`git checkout ref -- paths` (restore files from a commit); missing paths are ignored."""
        if not paths:
            return
        present = _split_z(
            git(["ls-tree", "-r", "-z", "--name-only", ref, "--", *paths], wt).stdout
        )
        if not present:
            return
        git(["checkout", ref, "--", *present], wt)

    # --- diffs
    def diff(
        self, wt: Path, base: str, head: str = "HEAD", *, exclude: tuple[str, ...] = ("work",)
    ) -> str:
        """`git diff <base>...<head> -- . ':!<exclude>'...` (design §9 review input)."""
        args = ["diff", "--no-color", f"{base}...{head}", "--", *_pathspec(exclude)]
        return git(args, wt).stdout

    def diff_stat(
        self, wt: Path, base: str, head: str = "HEAD", *, exclude: tuple[str, ...] = ("work",)
    ) -> str:
        """`git diff --stat <base>...<head> -- . ':!<exclude>'`."""
        args = ["diff", "--no-color", "--stat", f"{base}...{head}", "--", *_pathspec(exclude)]
        return git(args, wt).stdout

    def diff_numstat(
        self, wt: Path, base: str, head: str = "HEAD", *, exclude: tuple[str, ...] = ("work",)
    ) -> list[tuple[int, int, str]]:
        """[(added, deleted, path)] from `git diff --numstat`; binary files report (0, 0, path)."""
        args = ["diff", "--numstat", "-z", f"{base}...{head}", "--", *_pathspec(exclude)]
        return _parse_numstat_z(git(args, wt).stdout)

    def diff_paths(self, wt: Path, base: str, head: str, paths: list[str]) -> str:
        """`git diff <base>...<head> -- <paths>` for a subset of files (used to build a truncated review diff)."""
        if not paths:
            return ""
        return git(["diff", "--no-color", f"{base}...{head}", "--", *paths], wt).stdout

    def changed_paths_between(self, wt: Path, base: str, head: str = "HEAD") -> list[str]:
        """`git diff --name-only base head`. Rename detection is off, so a move away from a protected path
        reports both the deletion and the addition (the protected-path gate must see the deletion)."""
        out = git(["diff", "--name-only", "--no-renames", "-z", base, head], wt).stdout
        return _split_z(out)

    def changed_paths_of_commit(self, wt: Path, sha: str) -> list[str]:
        """`git diff-tree --no-commit-id --name-only -r sha`."""
        args = [
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "--no-renames",
            "-r",
            "--root",
            "-z",
            sha,
        ]
        return _split_z(git(args, wt).stdout)

    def code_changed_between(self, wt: Path, a: str, b: str = "HEAD") -> bool:
        """True if anything outside work/ differs between a and b (`git diff --quiet a b -- . ':!work'`)."""
        res = git(["diff", "--quiet", a, b, "--", *_pathspec(("work",))], wt, check=False)
        if res.returncode in (0, 1):
            return res.returncode == 1
        raise FactoryError(f"git diff --quiet {a} {b} failed in {wt}: {_tail(res.stderr)}")

    # --- remote
    def push(self, wt: Path, branch: str, *, force_with_lease: bool = False) -> None:
        """`git push -u origin branch` (with --force-with-lease when asked)."""
        args = ["push"]
        if force_with_lease:
            args.append("--force-with-lease")
        git([*args, "-u", "origin", branch], wt)

    def push_if_ahead(self, wt: Path, branch: str) -> bool:
        """Push when HEAD is strictly ahead of origin/<branch> (or the remote branch is absent); returns whether it
        pushed. Used by prepare() so a local-only accept/dismiss commit survives the host (rule 6)."""
        if self.remote_ahead_state(wt, branch) not in ("absent", "local_ahead"):
            return False
        self.push(wt, branch)
        return True

    def remote_ahead_state(self, wt: Path, branch: str) -> str:
        """One of 'absent' | 'equal' | 'local_ahead' | 'remote_ahead' | 'diverged' for HEAD vs origin/<branch>."""
        if not self.remote_branch_exists(branch):
            return "absent"
        counts = git(
            ["rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"], wt
        ).stdout.split()
        ahead, behind = int(counts[0]), int(counts[1])
        if ahead and behind:
            return "diverged"
        if ahead:
            return "local_ahead"
        if behind:
            return "remote_ahead"
        return "equal"

    def sync_with_remote(self, wt: Path, branch: str) -> str:
        """Start-of-command validation (design §7): after fetch, if origin/<branch> is strictly ahead of HEAD,
        fast-forward (`git merge --ff-only`); if diverged, FactoryError; if absent or equal/behind, no-op.
        Returns HEAD after the sync."""
        state = self.remote_ahead_state(wt, branch)
        if state == "remote_ahead":
            git(["merge", "--ff-only", f"origin/{branch}"], wt)
        elif state == "diverged":
            raise FactoryError(
                f"{branch} and origin/{branch} have diverged in {wt} "
                f"(local {self.head(wt)[:12]}, remote {self.rev_parse(f'origin/{branch}', wt)[:12]})",
                hint="reconcile the two by hand, or `factory abandon` the issue and start again",
            )
        return self.head(wt)

    def base_sha(self, base_branch: str) -> str:
        """`origin/<base_branch>` after fetch."""
        return self.rev_parse(f"origin/{base_branch}")

    def show_file(self, ref: str, path: str, cwd: Path | None = None) -> str | None:
        """`git show ref:path`; None if absent."""
        res = git(["show", f"{ref}:{path}"], cwd or self.root, check=False)
        return res.stdout if res.returncode == 0 else None

    def log_touching(self, wt: Path, path: str, n: int = 1) -> list[str]:
        """Most recent commit shas touching `path`."""
        return git(["log", "-n", str(n), "--format=%H", "--", path], wt).stdout.split()

    def worktree_for_issue_is_on_branch(self, wt: Path, branch: str) -> bool:
        res = git(["symbolic-ref", "--quiet", "--short", "HEAD"], wt, check=False)
        return res.returncode == 0 and res.stdout.strip() == branch


def _pathspec(exclude: tuple[str, ...]) -> list[str]:
    """`. ':!work' ...` — everything under the worktree root minus the excluded prefixes."""
    return [".", *[f":!{item}" for item in exclude]]


def _parse_numstat_z(out: str) -> list[tuple[int, int, str]]:
    """`--numstat -z` records: "<add>\t<del>\t<path>\0", or "<add>\t<del>\t\0<old>\0<new>\0" for a rename/copy.
    Binary files report "-" for both counts."""
    tokens = out.split("\0")
    rows: list[tuple[int, int, str]] = []
    i = 0
    while i < len(tokens):
        record = tokens[i]
        i += 1
        if "\t" not in record:
            continue
        added, deleted, path = record.split("\t", 2)
        if path == "":
            if i + 1 >= len(tokens):
                break
            path = tokens[i + 1]  # tokens[i] is the pre-image path
            i += 2
        rows.append((_count(added), _count(deleted), path))
    return rows


def _count(field: str) -> int:
    return 0 if field.strip() == "-" else int(field)


def default_identity_env() -> dict[str, str]:
    """GIT_AUTHOR_*/GIT_COMMITTER_* fallback ("factory", "factory@localhost") used only when git has no identity."""
    return {
        "GIT_AUTHOR_NAME": _DEFAULT_NAME,
        "GIT_AUTHOR_EMAIL": _DEFAULT_EMAIL,
        "GIT_COMMITTER_NAME": _DEFAULT_NAME,
        "GIT_COMMITTER_EMAIL": _DEFAULT_EMAIL,
    }
