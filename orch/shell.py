"""Thin subprocess wrappers for the two external tools the CLI itself uses."""

from __future__ import annotations

import subprocess
from pathlib import Path


class ShellError(RuntimeError):
    """A `git` or `gh` invocation failed."""


def _run(exe: str, args: tuple[str, ...], cwd: Path, check: bool) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [exe, *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise ShellError(
            f"{exe} {' '.join(args)} failed ({proc.returncode}) in {cwd}: "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return proc


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run `git` in `cwd`, capturing output; raise ShellError on failure unless check=False."""
    return _run("git", args, cwd, check)


def gh(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run `gh` in `cwd`, capturing output; raise ShellError on failure."""
    return _run("gh", args, cwd, True)


def head_sha(cwd: Path) -> str:
    """Current HEAD sha of the checkout, or "" when the repo has no commits."""
    proc = git("rev-parse", "HEAD", cwd=cwd, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def ref_sha(cwd: Path, ref: str) -> str:
    """Sha of a ref (e.g. a branch name) without touching the checkout, or "" when unknown."""
    proc = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def current_branch(cwd: Path) -> str:
    """Current branch name, or "" when detached or unborn."""
    proc = git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd, check=False)
    name = proc.stdout.strip() if proc.returncode == 0 else ""
    return "" if name in ("HEAD", "") else name
