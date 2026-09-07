"""Controller-owned Git operations, including conservative crash recovery."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from factory.errors import Blocked

_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class GitOps:
    def __init__(self, checkout: Path, runner: Any = None, timeout: float = 120):
        self.checkout = Path(checkout).resolve()
        self.runner = runner
        self.timeout = timeout

    def _run(self, args: list[str], path: Path | None = None, *, check: bool = True):
        argv = ["git", "-c", "core.hooksPath=/dev/null", *args]
        kwargs = {
            "cwd": Path(path) if path is not None else self.checkout,
            "timeout": self.timeout,
            "check": False,
            "env": {**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        }
        try:
            if self.runner is not None:
                result = self.runner.run(argv, **kwargs)
            else:
                result = subprocess.run(
                    argv, **kwargs, capture_output=True, text=True, errors="surrogateescape"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Blocked(f"Git command could not finish: {exc}") from exc
        if check and result.returncode:
            raise Blocked(f"Git {' '.join(args[:2])} failed: {result.stderr.strip()}")
        return result

    @staticmethod
    def _sha(sha: str) -> str:
        if not _SHA.fullmatch(sha):
            raise Blocked(f"Expected a full commit SHA, got {sha!r}")
        return sha

    def _branch(self, branch: str) -> str:
        if (
            branch.startswith("-")
            or self._run(["check-ref-format", "--branch", branch], check=False).returncode
        ):
            raise Blocked(f"Invalid Git branch: {branch!r}")
        return branch

    def validate(self, base_branch: str, repo: str | None = None) -> None:
        self._branch(base_branch)
        top = self._run(["rev-parse", "--show-toplevel"]).stdout.strip()
        if Path(top).resolve() != self.checkout:
            raise Blocked("The configured checkout must be the repository root")
        if self.status(self.checkout):
            raise Blocked("The target checkout has uncommitted changes")
        if repo is not None:
            remote = self._run(["remote", "get-url", "origin"]).stdout.strip()
            matches = re.fullmatch(
                r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                r"([^/]+/[^/]+?)(?:\.git)?/?",
                remote,
            )
            if not matches or matches.group(1).lower() != repo.lower():
                raise Blocked(f"origin must point to the configured GitHub repository {repo}")

    def fetch_base(self, base_branch: str) -> str:
        branch = self._branch(base_branch)
        self._run(["fetch", "--no-tags", "origin", f"refs/heads/{branch}"])
        return self._sha(self._run(["rev-parse", "FETCH_HEAD^{commit}"]).stdout.strip())

    def create_worktree(self, path: Path, branch: str, base_sha: str) -> None:
        path = Path(path).resolve()
        self._branch(branch)
        self._sha(base_sha)
        if path.exists() and any(path.iterdir()):
            top = self._run(["rev-parse", "--show-toplevel"], path).stdout.strip()
            current = self._run(["symbolic-ref", "--short", "HEAD"], path).stdout.strip()
            common = self._run(["rev-parse", "--path-format=absolute", "--git-common-dir"])
            other = self._run(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
            if (
                Path(top).resolve() != path
                or current != branch
                or Path(common.stdout.strip()).resolve() != Path(other.stdout.strip()).resolve()
                or self._run(
                    ["merge-base", "--is-ancestor", base_sha, "HEAD"], path, check=False
                ).returncode
            ):
                raise Blocked("Existing worktree does not match this run's branch and base")
            return
        existing = self._run(["show-ref", "--verify", f"refs/heads/{branch}"], check=False)
        if existing.returncode == 0:
            raise Blocked("Run branch already exists without its expected worktree")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(["worktree", "add", "-b", branch, str(path), base_sha])

    def head(self, path: Path) -> str:
        return self._sha(self._run(["rev-parse", "HEAD^{commit}"], path).stdout.strip())

    def parent(self, path: Path, sha: str) -> str:
        return self._sha(
            self._run(["rev-parse", f"{self._sha(sha)}^{{commit}}^"], path).stdout.strip()
        )

    def is_ancestor(self, path: Path, ancestor: str, descendant: str) -> bool:
        result = self._run(
            ["merge-base", "--is-ancestor", self._sha(ancestor), self._sha(descendant)],
            path,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise Blocked("Could not verify commit ancestry")
        return result.returncode == 0

    def status(self, path: Path) -> list[str]:
        raw = self._run(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path).stdout
        entries = iter(raw.split("\0"))
        paths = set()
        for entry in entries:
            if not entry:
                continue
            paths.add(entry[3:])
            if "R" in entry[:2] or "C" in entry[:2]:
                paths.add(next(entries))
        return sorted(paths)

    def untracked_paths(self, path: Path, *, include_ignored: bool = True) -> set[str]:
        """List repository-relative untracked files, including ignored output by default."""
        args = ["ls-files", "--others", "--full-name", "-z"]
        if not include_ignored:
            args.append("--exclude-standard")
        raw = self._run(args, path).stdout
        return set(filter(None, raw.split("\0")))

    def changed_paths(self, path: Path, base_sha: str) -> set[str]:
        self._sha(base_sha)
        raw = self._run(["diff", "--name-only", "--no-renames", "-z", base_sha, "--"], path)
        return set(filter(None, raw.stdout.split("\0"))) | set(self.status(path))

    def commit(
        self, path: Path, message: str, *, run_id: str | None = None, stage: str | None = None
    ) -> str:
        if (run_id is None) != (stage is None):
            raise ValueError("Controller checkpoints require both run_id and stage")
        if run_id is not None:
            if not run_id or not stage or "\n" in run_id or "\n" in stage:
                raise ValueError("Checkpoint identifiers must be nonempty single lines")
            existing = self.checkpoint(path, run_id, stage)
            if existing:
                if self.status(path):
                    raise Blocked("A completed controller checkpoint has unexpected later edits")
                return existing
            message = message.rstrip() + f"\n\nFactory-Run: {run_id}\nFactory-Stage: {stage}\n"
        if not self.status(path) and run_id is None:
            return self.head(path)
        self._run(["add", "--all", "--", "."], path)
        self._run(
            [
                "-c",
                "user.name=Software Factory",
                "-c",
                "user.email=factory@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-m",
                message,
            ],
            path,
        )
        return self.head(path)

    def checkpoint(self, path: Path, run_id: str, stage: str) -> str | None:
        message = self._run(["log", "-1", "--format=%B"], path).stdout
        trailers = self._trailers(message)
        if trailers.get("Factory-Run") == [run_id] and trailers.get("Factory-Stage") == [stage]:
            return self.head(path)
        return None

    @staticmethod
    def _trailers(message: str) -> dict[str, list[str]]:
        # Only the final paragraph can be a trailer block; text in a body is not provenance.
        trailers: dict[str, list[str]] = {}
        for line in message.rstrip().split("\n\n")[-1].splitlines():
            if ": " not in line:
                return {}
            key, value = line.split(": ", 1)
            trailers.setdefault(key, []).append(value)
        return trailers

    def show(self, path: Path, sha: str, relative_path: str) -> bytes:
        self._sha(sha)
        result = self._run(["show", f"{sha}:{relative_path}"], path)
        if getattr(result, "stdout_bytes", None) is not None:
            return result.stdout_bytes
        return result.stdout.encode("utf-8", errors="surrogateescape")

    def diff(self, path: Path, base_sha: str, head_sha: str) -> str:
        return self._run(
            [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                self._sha(base_sha),
                self._sha(head_sha),
                "--",
            ],
            path,
        ).stdout

    def push(self, path: Path, branch: str) -> None:
        self._branch(branch)
        if self._run(["symbolic-ref", "--short", "HEAD"], path).stdout.strip() != branch:
            raise Blocked("The worktree is no longer on the run branch")
        sha = self.head(path)
        ref = f"refs/heads/{branch}"
        remote = self._run(["ls-remote", "--heads", "origin", ref], path).stdout.split()
        if remote:
            if remote[0] == sha:
                return
            ancestor = self._run(["merge-base", "--is-ancestor", remote[0], sha], path, check=False)
            if ancestor.returncode:
                raise Blocked("Remote run branch differs from the candidate; refusing overwrite")
        self._run(["push", "origin", f"{sha}:{ref}"], path)
        published = self._run(["ls-remote", "--heads", "origin", ref], path).stdout.split()
        if not published or published[0] != sha:
            raise Blocked("Remote branch changed while publishing the candidate")

    def preserve_and_restore(
        self, path: Path, checkpoint_sha: str, evidence_dir: Path, *, run_id: str | None = None
    ) -> Path:
        """Preserve interrupted edits before restoring a verified controller checkpoint."""
        path, evidence_dir = Path(path).resolve(), Path(evidence_dir).resolve()
        self._sha(checkpoint_sha)
        if evidence_dir.is_relative_to(path):
            raise Blocked("Recovery evidence must be stored outside the worktree")
        current = self.head(path)
        if current != checkpoint_sha:
            if (
                not run_id
                or self._run(
                    ["merge-base", "--is-ancestor", checkpoint_sha, current], path, check=False
                ).returncode
            ):
                raise Blocked("Unexpected commit history prevents automatic recovery")
            commits = self._run(
                ["log", "--format=%H", f"{checkpoint_sha}..{current}"], path
            ).stdout.splitlines()
            for sha in commits:
                message = self._run(["log", "-1", "--format=%B", sha], path).stdout
                if self._trailers(message).get("Factory-Run") != [run_id]:
                    raise Blocked("Unexpected commit history prevents automatic recovery")
        recovery_id = uuid.uuid4().hex
        destination = evidence_dir / f"recovery-{recovery_id}"
        destination.mkdir(parents=True)
        dirty = sorted(set(self.status(path)) | self.untracked_paths(path))
        (destination / "metadata.json").write_text(
            json.dumps({"head": current, "checkpoint": checkpoint_sha, "paths": dirty}, indent=2)
        )
        for name, args in (
            ("working.patch", ["diff", "--binary", "--no-ext-diff", "--no-textconv"]),
            ("staged.patch", ["diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv"]),
        ):
            result = self._run(args, path)
            raw = getattr(result, "stdout_bytes", None)
            if raw is None:
                raw = result.stdout.encode("utf-8", errors="surrogateescape")
            (destination / name).write_bytes(raw)
        for relative in dirty:
            source, target = path / relative, destination / "files" / relative
            if not source.exists() and not source.is_symlink():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
            elif source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
        self._run(["update-ref", f"refs/factory-recovery/{recovery_id}", current], path)
        self._run(["reset", "--hard", checkpoint_sha], path)
        self._run(["clean", "-fdx", "--"], path)
        if self.status(path) or self.untracked_paths(path):
            raise Blocked(f"Recovery saved evidence at {destination}, but worktree is still dirty")
        return destination
