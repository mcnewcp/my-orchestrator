"""One-shot unattended discovery; all workflow decisions remain local."""

from . import present
from .checks import filtered_env
from .config import harness_settings
from .harness import doctor, doctor_key, make_harness
from .locks import file_lock, issue_lock
from .stages import Engine
from .state import atomic_json, load_json


def _current_head(repo, issue, fallback=None):
    """Best-effort read for retry accounting; never create or modify a worktree."""
    try:
        cwd = repo.worktree(issue, create=False)
        if cwd:
            return repo.head(cwd)
        if repo.branch_exists(issue):
            return repo.git("rev-parse", f"refs/heads/factory/{issue}")
    except (RuntimeError, ValueError, OSError):
        pass
    return fallback


def _capped(failures, issue, head, limit):
    previous = failures.get(str(issue), {})
    return previous.get("head") == head and previous.get("count", 0) >= limit


def poll(repo, github, config):
    from .cli import execute_issue

    options = config["factory"]
    if options["auth"] != "api":
        raise ValueError("poll requires auth=api; subscription is attended only")
    harnesses = harness_settings(config)
    for name in harnesses:
        filtered_env(harness=name, auth="api")  # Before any GitHub read or write.
    with file_lock(repo.local_dir / "run" / "poll.lock", skip=True) as acquired:
        if not acquired:
            present.poll_busy()
            return 0
        # Include a first-time doctor in the poll lock. Two fresh poll processes
        # must not race the probe or contend for the shared harness lock.
        cache = load_json(repo.local_dir / "doctor.json", {})
        missing = []
        for name in harnesses:
            version = make_harness(name, repo.local_dir / "transcripts").version()
            key = doctor_key(name, version, "api")
            if not cache.get("records", {}).get(key, {}).get("passed"):
                missing.append(name)
        if missing:
            with file_lock(repo.local_dir / "run" / "harness.lock"):
                if not doctor(repo.root, config)["passed"]:
                    raise RuntimeError("doctor failed; refusing unattended work")
        failures_path = repo.local_dir / "poll.json"
        failures = load_json(failures_path, {})
        limit = config["poll"]["max_consecutive_failures"]
        failed = False
        for item in sorted(github.issues(config["poll"]["label"]), key=lambda item: item["number"]):
            issue = item["number"]
            issue_key = str(issue)
            head = None
            try:
                # Classification can fast-forward and recover interrupted work;
                # it needs the same process exclusion as run.
                with issue_lock(repo, issue, "poll-classify"):
                    repo.fetch()
                    exists = repo.branch_exists(issue) or repo.branch_exists(issue, remote=True)
                    state = {}
                    if exists:
                        prepared = Engine(repo, github, config, issue).prepare(create=True)
                        cwd, head, state = prepared.cwd, prepared.head(), prepared.state
                    outcome = state.get("outcome") or ""
                    if outcome == "done":
                        # Retry any pending code push before parking the run.
                        remote_head = (repo.git("rev-parse", f"refs/remotes/origin/factory/{issue}")
                                       if repo.branch_exists(issue, remote=True) else None)
                        if remote_head != head:
                            if _capped(failures, issue, head, limit):
                                present.poll_skip_capped(issue, head)
                                continue
                            repo.push(issue)
                            if failures.pop(issue_key, None) is not None:
                                atomic_json(failures_path, failures)
                        present.poll_skip_done(issue)
                        continue
                    if _capped(failures, issue, head, limit):
                        present.poll_skip_capped(issue, head)
                        continue
                    if outcome.startswith("needs_human:") and head == repo.resolve(cwd, state["outcome_sha"]):
                        previous = failures.get(issue_key, {})
                        publication_failed = previous.get("head") == head and previous.get("count", 0) > 0
                        notice_pending = bool(state.get("pr")) and state.get("gate_notice", {}).get("sent", True) is False
                        remote_head = (repo.git("rev-parse", f"refs/remotes/origin/factory/{issue}")
                                       if repo.branch_exists(issue, remote=True) else None)
                        if not publication_failed and not notice_pending and remote_head == head:
                            present.poll_skip_parked(issue, outcome)
                            continue
                        # A gate is saved before its push/comment. Replaying
                        # run at that same SHA retries idempotent publication
                        # and exits 2 without launching a model session.
                        present.poll_retry_gate(issue)
                code = execute_issue(repo, github, config, issue)
            except (RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
                head = _current_head(repo, issue, head)
                # A persistent preparation failure (for example divergence)
                # is bounded at the same local HEAD as a failed stage.
                if _capped(failures, issue, head, limit):
                    present.poll_skip_capped(issue, head)
                    continue
                present.poll_failed(issue, exc)
                code = 1
            if code == 1:
                failed = True
                head = _current_head(repo, issue, head)
                previous = failures.get(issue_key, {})
                count = previous.get("count", 0) + 1 if previous.get("head") == head else 1
                failures[issue_key] = {"head": head, "count": count}
            else:
                failures.pop(issue_key, None)
            atomic_json(failures_path, failures)
        return 1 if failed else 0
