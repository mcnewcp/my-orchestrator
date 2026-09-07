"""`factory poll` (design §12): one-shot, sequential, timer-driven unattended mode.

0. Preflight: config.auth must be "api" and the selected key present (build_env raises) — else exit 1 before touching
   GitHub. doctor.json must hold a current record for (harness, auth, factory version, harness CLI version), else run
   doctor.doctor(); FAIL -> exit 1.
1. Lock .factory/run/poll.lock with flock(LOCK_EX | LOCK_NB), held open for the whole tick; already held -> exit 0.
2. Discover: gh.list_open_issues_with_label(config.poll_label), ascending.
3. Classify each from local state after repo.fetch() and repo.sync_with_remote() (worktree created from
   origin/factory/<n> where needed; a strictly-ahead remote is fast-forwarded, a diverged one is a per-issue error):
   no branch anywhere -> eligible ("new"); outcome None -> eligible ("resume"); needs_human and not
   state.is_parked(...) -> eligible ("operator acted"); parked -> skip; done -> skip;
   journal[n].sha == HEAD and journal[n].failures >= max_consecutive_failures -> skip ("capped").
   The first time an issue is skipped as capped, post one idempotent PR comment (gh.render_capped_comment, CAPPED_MARKER)
   and record capped_comment_sha.
4. Run run_issue(n) for each eligible issue in turn. Exit 0 or 2 -> delete journal[n]. Exit 1: if journal[n].sha is
   the HEAD the run STARTED from then failures += 1, else journal[n] = {failures: 1}; the entry records HEAD after the
   run and the last_error. The counter is consecutive failures over ONE stretch of history, so an operator's commit
   between two ticks resets it while a commit the failing run made itself does not.
5. Exit 1 if any run exited 1, else 0.

Journal keys are strings (`.factory/poll.json` round-trips through JSON): journal[str(issue)]. An issue with no branch
anywhere has no HEAD; its journal entry records the sha as "" so repeated failures before the branch exists still cap.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from . import __version__
from . import doctor as doctor_module
from . import harness as harness_module
from . import state as state_module
from .config import Config
from .errors import FactoryError
from .gh import CAPPED_MARKER, GitHub, render_capped_comment
from .repo import Repo, branch_name

POLL_LOCK_NAME = "poll.lock"

# Classification reasons: the values of PollResult.skipped and the words the log lines use.
NEW = "new"
RESUME = "resume"
OPERATOR_ACTED = "operator acted"
PARKED = "parked"
DONE = "done"
CAPPED = "capped"
ERROR = "error"

NO_COMMIT = ""  # journal sha for an issue whose branch does not exist yet


@dataclass
class PollResult:
    exit_code: int
    ran: list[int] = field(default_factory=list)
    skipped: dict[int, str] = field(default_factory=dict)  # issue -> reason
    outcomes: dict[int, int] = field(default_factory=dict)  # issue -> exit code of its run


def poll(
    repo: Repo,
    gh: GitHub,
    config: Config,
    *,
    parent_env: dict,
    run_issue: Callable[[int], int],
    out: Callable[[str], None] | None = None,
) -> PollResult:
    """`run_issue(issue) -> int` is stages.run_issue bound by cli.py (tests inject). Never raises for per-issue
    failures; raises FactoryError only for preflight problems."""
    log = out if out is not None else _discard
    _preflight(repo, config, parent_env=parent_env, log=log)
    handle = acquire_poll_lock(repo.factory_dir)
    if handle is None:
        log(f"another poll holds {_poll_lock_path(repo.factory_dir)}; nothing to do")
        return PollResult(exit_code=0)
    try:
        return _poll_locked(repo, gh, config, run_issue=run_issue, log=log)
    finally:
        release_poll_lock(handle)


def classify(repo: Repo, config: Config, issue: int, journal: dict) -> tuple[bool, str]:
    """(eligible, reason) per step 3. Uses state.is_parked with repo.head/parent/changed_paths_of_commit.

    The worktree is synced with origin BEFORE HEAD is read (design §12 "from local state after git fetch", §7's
    start-of-command validation): a host that fetched a commit pushed from elsewhere — an operator's hand fix, a
    `factory` run on another machine — must classify the issue on that commit, not on the stale local one it
    would otherwise call parked. A diverged branch raises FactoryError, which the tick reports as this issue's
    error and no other's."""
    branch = branch_name(issue)
    if not repo.local_branch_exists(branch) and not repo.remote_branch_exists(branch):
        # Nothing to read: the issue starts at `spec`, which creates the branch from the base.
        return _unless_capped(config, journal, issue, NO_COMMIT, NEW)

    worktree = repo.ensure_worktree(issue)
    head = repo.sync_with_remote(worktree, branch)
    state = state_module.State.load_or_none(worktree, issue)
    if state is None:
        # The branch exists but no state.json is committed on it (an interrupted first `spec`): resume.
        return _unless_capped(config, journal, issue, head, RESUME)
    if state.outcome == "done":
        return False, DONE
    if state.is_needs_human():
        parked = state_module.is_parked(
            state,
            head,
            repo.parent(head, worktree),
            repo.changed_paths_of_commit(worktree, head),
        )
        if parked:
            return False, PARKED
        return _unless_capped(config, journal, issue, head, OPERATOR_ACTED)
    return _unless_capped(config, journal, issue, head, RESUME)


def acquire_poll_lock(factory_dir: Path) -> TextIO | None:
    """Take the per-repo poll lock and return the open file that HOLDS it, or None when another poll already
    does (design §12 step 1: a held lock is exit 0, not an error). Pass what you get to release_poll_lock.

    The lock is an `flock(LOCK_EX | LOCK_NB)` on `.factory/run/poll.lock`, not the file's existence, because
    neither of the two facts a pid file needs is available on this host:
      * the kernel drops an flock when the holder dies — killed container, OOM, hard reboot — so a crash cannot
        wedge the timer, and there is no stale lock to detect;
      * pids are not evidence of a holder. Every tick runs in a fresh container (design §13) where pid 7 is a
        different process each time, so a leftover file naming a live-but-unrelated pid would block every
        future tick, and one naming a pid this container cannot see would be discarded while its owner runs.
    The pid and started_at written inside the file are for a human reading it over SSH; nothing reads them back.
    The file is deliberately never unlinked (see release_poll_lock)."""
    path = _poll_lock_path(factory_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately not a context manager: the open file is what holds the lock.
        handle = open(path, "a+", encoding="utf-8")
    except OSError as exc:
        raise FactoryError(f"could not open the poll lock {path}: {exc}") from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except OSError as exc:
        handle.close()
        raise FactoryError(
            f"could not lock {path}: {exc}",
            hint="`.factory/` must be on a filesystem that supports flock (a local volume, not a network share)",
        ) from exc
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "started_at": state_module.now_iso()}) + "\n")
        handle.flush()
    except OSError as exc:
        release_poll_lock(handle)
        raise FactoryError(f"could not write the poll lock {path}: {exc}") from exc
    return handle


def release_poll_lock(handle: TextIO | None) -> None:
    """Release the lock acquire_poll_lock returned (None is accepted: nothing was held). Closing the file drops
    the flock. The file itself stays: unlinking it while another poll waits on that inode would let the next
    poll create and lock a NEW file, and two ticks would run at once."""
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass  # already closed, or never locked: the close below is what actually releases it
    try:
        handle.close()
    except OSError as exc:
        raise FactoryError(f"could not release the poll lock: {exc}") from exc


# ---------------------------------------------------------------- preflight (step 0)


def _preflight(repo: Repo, config: Config, *, parent_env: dict, log: Callable[[str], None]) -> None:
    """Design §12 step 0. Raises FactoryError (exit 1) before any GitHub read or git write."""
    if config.auth != "api":
        raise FactoryError(
            f'poll requires auth = "api"; factory.toml selects {config.auth!r}',
            hint="subscription auth is the attended mode (design §8); poll accepts no --auth flag",
        )
    env = harness_module.build_env(
        config.harness, config.auth, parent_env, passthrough=config.env_passthrough
    )
    cli_version = harness_module.get_harness(config.harness).version(env)
    combination = f"{config.harness}:{config.auth}"
    records = state_module.read_doctor_records(repo.factory_dir)
    if doctor_module.doctor_record_is_current(
        records,
        harness=config.harness,
        auth=config.auth,
        cli_version=cli_version,
        factory_version=__version__,
    ):
        log(f"doctor: current record for {combination} ({cli_version})")
        return
    log(f"doctor: no current record for {combination} ({cli_version}); running it")
    report = doctor_module.doctor(
        repo,
        config,
        harness=config.harness,
        auth=config.auth,
        model=config.model,
        parent_env=parent_env,
    )
    if not report.ok:
        raise FactoryError(
            f"doctor failed for {combination}; poll will not run", hint=report.render()
        )
    log(f"doctor: {combination} passed")


# ---------------------------------------------------------------- the tick (steps 2-5)


def _poll_locked(
    repo: Repo,
    gh: GitHub,
    config: Config,
    *,
    run_issue: Callable[[int], int],
    log: Callable[[str], None],
) -> PollResult:
    result = PollResult(exit_code=0)
    issues = gh.list_open_issues_with_label(config.poll_label)
    log(f"{len(issues)} open issue(s) labelled {config.poll_label!r}{_listing(issues)}")
    if not issues:
        return result

    repo.fetch()
    journal = state_module.read_poll_journal(repo.factory_dir)
    failed = False
    for issue in issues:
        try:
            eligible, reason = classify(repo, config, issue, journal)
        except FactoryError as exc:
            # One unreadable issue must not cost the others their tick; the exit code still reports it.
            log(f"issue {issue}: cannot classify: {exc.message}")
            result.skipped[issue] = ERROR
            failed = True
            continue
        if not eligible:
            log(f"issue {issue}: skipped ({reason})")
            result.skipped[issue] = reason
            if reason == CAPPED:
                _comment_capped(gh, journal, issue, log=log)
                state_module.write_poll_journal(repo.factory_dir, journal)
            continue
        log(f"issue {issue}: {reason}; running")
        result.ran.append(issue)
        # Read BEFORE the run: a failing stage may commit, and the cap must survive that (_record_outcome).
        start_head = _issue_head(repo, issue)
        code = _run_one(run_issue, issue, log=log)
        result.outcomes[issue] = code
        log(f"issue {issue}: exit {code}")
        _record_outcome(repo, journal, issue, code, start_head)
        state_module.write_poll_journal(repo.factory_dir, journal)
        failed = failed or code == 1
    result.exit_code = 1 if failed else 0
    return result


def _run_one(run_issue: Callable[[int], int], issue: int, *, log: Callable[[str], None]) -> int:
    """run_issue's contract is that it never raises; a tick survives one that does anyway."""
    try:
        return int(run_issue(issue))
    except Exception as exc:  # an injected callable must not end the tick
        log(f"issue {issue}: run raised {type(exc).__name__}: {exc}")
        return _exit_code_of(exc)


def _record_outcome(repo: Repo, journal: dict, issue: int, code: int, start_head: str) -> None:
    """Step 4. The counter is consecutive exit-1s over one unbroken stretch of history: an operator's commit
    replaces the whole entry (deviation 18), which is what clears both the counter and its capped comment.

    `start_head` is HEAD when this attempt STARTED and is what the recorded sha is compared against; the sha
    stored is HEAD after the attempt, which is what the next tick's `classify` will see. Comparing the two ends
    of the same attempt instead would let a stage that commits before failing reset its own counter every tick
    — each attempt would end at a new commit, no two entries would ever match, and the cap could never fire."""
    key = str(issue)
    if code != 1:
        journal.pop(key, None)
        return
    head = _issue_head(repo, issue)
    previous = journal.get(key)
    if not isinstance(previous, dict) or previous.get("sha") != start_head:
        previous = {}
    failures = _count(previous.get("failures")) + 1
    journal[key] = {
        "failures": failures,
        "sha": head,
        "last_error": _last_error(issue, head, failures),
        "at": state_module.now_iso(),
        "capped_comment_sha": previous.get("capped_comment_sha"),
    }


def _comment_capped(gh: GitHub, journal: dict, issue: int, *, log: Callable[[str], None]) -> None:
    """One idempotent PR comment the first time an issue is skipped as capped at this commit (deviation 18:
    §12 gives exit 1 no channel, so an unattended issue would otherwise go dark). A gh failure is logged only —
    the tick's remaining issues matter more than the comment."""
    entry = journal.get(str(issue))
    if not isinstance(entry, dict):
        return
    sha = str(entry.get("sha") or NO_COMMIT)
    if entry.get("capped_comment_sha") == sha:
        return
    branch = branch_name(issue)
    try:
        pull_request = gh.find_pr_for_branch(branch)
        if pull_request is None:
            log(f"issue {issue}: capped, but no pull request for {branch} to comment on")
            return
        body = render_capped_comment(
            issue, sha, _count(entry.get("failures")), str(entry.get("last_error") or "")
        )
        posted = gh.pr_comment(pull_request.number, body, marker=CAPPED_MARKER.format(sha=sha))
    except FactoryError as exc:
        log(f"issue {issue}: could not post the capped comment: {exc.message}")
        return
    entry["capped_comment_sha"] = sha
    verb = "posted" if posted else "already present on"
    log(f"issue {issue}: capped comment {verb} #{pull_request.number}")


# ---------------------------------------------------------------- helpers


def _unless_capped(
    config: Config, journal: dict, issue: int, head: str, reason: str
) -> tuple[bool, str]:
    """An otherwise-eligible issue that has failed max_consecutive_failures times at this exact commit is skipped."""
    entry = journal.get(str(issue))
    if not isinstance(entry, dict) or entry.get("sha") != head:
        return True, reason
    if _count(entry.get("failures")) >= config.poll_max_consecutive_failures:
        return False, CAPPED
    return True, reason


def _issue_head(repo: Repo, issue: int) -> str:
    """HEAD of the issue's branch after a run: the worktree's, else the local branch, else origin's, else ""
    (the run failed before `spec` created the branch)."""
    branch = branch_name(issue)
    if repo.worktree_exists(issue):
        return repo.head(repo.worktree_path(issue))
    if repo.local_branch_exists(branch):
        return repo.rev_parse(branch)
    if repo.remote_branch_exists(branch):
        return repo.rev_parse(f"origin/{branch}")
    return NO_COMMIT


def _last_error(issue: int, head: str, failures: int) -> str:
    """What the capped comment quotes. run_issue reports its failure through `out` and returns only an exit code,
    so the text names the run and where the message went rather than inventing one."""
    where = f"commit {head[:12]}" if head else "no commit yet"
    return (
        f"`factory run {issue}` exited 1 ({failures} time(s) in a row at {where}). "
        "The stage that failed logged the reason; see the host journal for that tick."
    )


def _listing(issues: list[int]) -> str:
    return (": " + ", ".join(f"#{n}" for n in issues)) if issues else ""


def _count(value: object) -> int:
    """A journal counter read back from JSON: anything that is not a positive integer counts as zero."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _exit_code_of(exc: BaseException) -> int:
    code = getattr(exc, "exit_code", 1)
    return code if isinstance(code, int) and not isinstance(code, bool) else 1


def _discard(_message: str) -> None:
    """`out=None`: modules never write to a stream (only cli.py and Context.log do)."""


def _poll_lock_path(factory_dir: Path) -> Path:
    return Path(factory_dir) / "run" / POLL_LOCK_NAME
