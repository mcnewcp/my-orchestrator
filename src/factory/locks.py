"""Linux process locks; persisted stage markers also detect interrupted work."""

import fcntl
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from .state import atomic_json, load_json, utcnow


def record_safe_head(repo, issue: int, head: str | None = None, *, stage: str | None = None,
                     state: dict | None = None, ledger: dict | None = None) -> None:
    """Keep interrupted recovery anchored to the last saved state and code."""
    marker = repo.local_dir / "run" / f"{issue}.json"
    if not marker.exists():
        return
    current = load_json(marker)
    if not isinstance(current, dict) or current.get("pid") != os.getpid():
        raise RuntimeError(f"cannot update issue {issue} marker without its process lock")
    if head is not None:
        current["last_safe_head"] = head
    if stage is not None:
        current["stage"] = stage
    if state is not None:
        current["state"] = state
    if ledger is not None:
        current["ledger"] = ledger
    atomic_json(marker, current)


@contextmanager
def file_lock(path: Path, *, skip=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if skip:
                yield False
                return
            raise RuntimeError(f"another factory command holds {path}") from None
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def issue_lock(repo, issue: int, stage: str):
    run = repo.local_dir / "run"
    marker = run / f"{issue}.json"
    with file_lock(run / "harness.lock"), file_lock(run / f"{issue}.lock"):
        if marker.exists():
            previous = {}
            try:
                previous = json.loads(marker.read_text())
                if not isinstance(previous, dict):
                    previous = {}
                pid = previous.get("pid", 0)
                if type(pid) is int and pid > 0:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        raise RuntimeError(f"issue {issue} is locked by live pid {pid}")
            except (json.JSONDecodeError, TypeError):
                pass
            cwd = repo.worktree(issue, create=False)
            if cwd and cwd.exists():
                safe = previous.get("last_safe_head")
                if safe is None:
                    repo.reset(cwd)
                else:
                    if not isinstance(safe, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", safe):
                        raise RuntimeError(f"invalid recovery commit for issue {issue}")
                    # A rewind can make this commit no longer an ancestor of
                    # HEAD. Prove the object is a commit without requiring that.
                    safe = repo.git("rev-parse", "--verify", f"{safe}^{{commit}}", cwd=cwd)
                    repo.reset(cwd, safe)
                if "state" in previous:
                    atomic_json(repo.issue_dir(issue) / "state.json", previous["state"])
                if "ledger" in previous:
                    atomic_json(repo.issue_dir(issue) / "findings.json", previous["ledger"])
        atomic_json(marker, {"stage": stage, "pid": os.getpid(), "started_at": utcnow(),
                             "worktree": str(repo.local_dir / "worktrees" / str(issue))})
        try:
            yield
        finally:
            marker.unlink(missing_ok=True)
