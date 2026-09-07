"""Unit tests for factory.poll (design §12, deviation 18).

Self-contained on purpose: real git against a bare "origin" in tmp_path, a stub `claude` that answers only
`--version`, a `GitHub` subclass whose subprocess layer is a hard error, and an injected `run_issue`. No
conftest fixtures, no tests/fakes, no network, and no model is ever launched.

The proofs design §17.2 asks of poll live here: parked, done and capped issues are skipped; two polls cannot
overlap; `subscription` under poll is refused; a deleted `.factory/` resumes the run from the remote branch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from factory import __version__
from factory import poll as poll_module
from factory.config import Config
from factory.errors import FactoryError
from factory.gh import CAPPED_MARKER, GitHub, PullRequest
from factory.repo import Repo, branch_name
from factory.state import (
    State,
    now_iso,
    read_poll_journal,
    state_only_paths,
    write_doctor_record,
    write_poll_journal,
)

STUB_VERSION = "9.9.9 (Claude Code)"
CLAUDE_STUB = f"""\
#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "{STUB_VERSION}"
  exit 0
fi
echo "poll tests never launch a model (argv: $*)" >&2
exit 99
"""

IDENTITY = {
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
}


# ---------------------------------------------------------------- rig


def run_git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def dead_pid() -> int:
    """A pid that certainly no longer exists: a child we started and reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class StubGitHub(GitHub):
    """A real GitHub whose subprocess layer refuses to run: any call the tests do not stub is a bug."""

    def __init__(self, root: Path, *, issues=(), prs=None):
        super().__init__(root)
        self.issues = sorted(issues)
        self.prs: dict[str, PullRequest] = dict(prs or {})
        self.labels_asked: list[str] = []
        self.pr_lookups: list[str] = []
        self.posted: list[tuple[int, str | None, str]] = []
        self.comments: dict[int, list[str]] = {}
        self.comment_fails = False

    def _attempt(self, args):  # pragma: no cover - the assertion is the point
        raise AssertionError(f"gh must not be launched by these tests: {list(args)}")

    def list_open_issues_with_label(self, label: str) -> list[int]:
        self.labels_asked.append(label)
        return list(self.issues)

    def find_pr_for_branch(self, branch: str) -> PullRequest | None:
        self.pr_lookups.append(branch)
        return self.prs.get(branch)

    def pr_comment(self, number: int, body: str, *, marker: str | None = None) -> bool:
        if self.comment_fails:
            raise FactoryError("`gh pr comment` failed (exit 1)")
        existing = self.comments.setdefault(number, [])
        if marker and any(marker in comment for comment in existing):
            return False
        existing.append(body)
        self.posted.append((number, marker, body))
        return True

    @property
    def gh_calls(self) -> int:
        return len(self.labels_asked) + len(self.pr_lookups) + len(self.posted)


@dataclass
class StubRun:
    """The injected `run_issue`: records the issues it was asked to run and returns scripted exit codes."""

    codes: dict[int, int] = field(default_factory=dict)
    default: int = 0
    raises: dict[int, BaseException] = field(default_factory=dict)
    calls: list[int] = field(default_factory=list)

    def __call__(self, issue: int) -> int:
        self.calls.append(issue)
        if issue in self.raises:
            raise self.raises[issue]
        return self.codes.get(issue, self.default)


@dataclass
class World:
    root: Path
    origin: Path
    checkout: Path
    repo: Repo
    base: str
    bindir: Path

    @property
    def factory_dir(self) -> Path:
        return self.repo.factory_dir

    def env(self, **overrides) -> dict:
        """The parent environment poll passes to build_env: PATH (with the stub claude) plus a provider key."""
        env = {
            "PATH": os.pathsep.join([str(self.bindir), os.environ.get("PATH", "")]),
            "HOME": str(self.root / "home"),
            "ANTHROPIC_API_KEY": "test-key",
        }
        env.update(overrides)
        return {name: value for name, value in env.items() if value is not None}

    def worktree(self, issue: int) -> Path:
        return self.repo.worktree_path(issue)

    def head(self, issue: int) -> str:
        return self.repo.head(self.worktree(issue))

    def start(self, issue: int, *, push: bool = False) -> Path:
        """A branch mid-run: one committed state.json with outcome None."""
        wt = self.repo.ensure_worktree(issue, start_point=self.base)
        state = State(
            issue={"number": issue, "snapshot_sha256": "0" * 64, "snapshot_at": now_iso()},
            base={"branch": "main", "sha": self.base},
            branch=branch_name(issue),
        )
        state.save(wt)
        self.repo.commit_all(wt, f"factory({issue}): spec")
        if push:
            self.repo.push(wt, branch_name(issue))
        return wt

    def park(self, issue: int, gate: str = "rounds_exhausted", *, also: str | None = None) -> str:
        """park()'s commit shape: outcome_sha is HEAD, then a commit touching ONLY state.json."""
        wt = self.worktree(issue)
        state = State.load(wt, issue)
        outcome_sha = self.repo.head(wt)
        state.set_outcome(f"needs_human:{gate}", outcome_sha)
        state.save(wt)
        paths = state_only_paths(issue)
        if also is not None:
            (wt / also).write_text("evidence\n", encoding="utf-8")
            paths = [*paths, also]
        self.repo.commit_all(wt, f"factory({issue}): park {gate}", paths=paths)
        return outcome_sha

    def finish(self, issue: int) -> None:
        wt = self.worktree(issue)
        state = State.load(wt, issue)
        state.set_outcome("done", self.repo.head(wt))
        state.save(wt)
        self.repo.commit_all(wt, f"factory({issue}): finalize", paths=state_only_paths(issue))

    def operator_commit(self, issue: int, name: str = "src/app.py") -> str:
        wt = self.worktree(issue)
        path = wt / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# hand fix\n", encoding="utf-8")
        return self.repo.commit_all(wt, f"operator: fix {name}")

    def doctor_record(self, *, cli_version: str = STUB_VERSION) -> None:
        write_doctor_record(
            self.factory_dir,
            "claude",
            "api",
            {
                "ok": True,
                "factory_version": __version__,
                "cli_version": cli_version,
                "at": now_iso(),
                "checks": ["ok: stub record"],
            },
        )

    def journal(self) -> dict:
        return read_poll_journal(self.factory_dir)


@pytest.fixture
def git_env(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))  # deliberately absent
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for name, value in IDENTITY.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def world(tmp_path, git_env) -> World:
    origin = tmp_path / "origin.git"
    run_git(["init", "--bare", "--initial-branch=main", str(origin)], tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_git(["init", "--initial-branch=main"], checkout)
    (checkout / "src").mkdir()
    (checkout / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    run_git(["add", "-A"], checkout)
    run_git(["commit", "-m", "init"], checkout)
    base = run_git(["rev-parse", "HEAD"], checkout).strip()
    run_git(["remote", "add", "origin", str(origin)], checkout)
    run_git(["push", "-u", "origin", "main"], checkout)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "claude"
    stub.write_text(CLAUDE_STUB, encoding="utf-8")
    stub.chmod(0o755)

    return World(
        root=tmp_path,
        origin=origin,
        checkout=checkout,
        repo=Repo(checkout),
        base=base,
        bindir=bindir,
    )


@pytest.fixture
def config() -> Config:
    return Config(harness="claude", auth="api", base_branch="main")


@pytest.fixture
def no_doctor(monkeypatch) -> None:
    """A current doctor record must mean doctor is never run (it costs two live harness sessions)."""

    def boom(*args, **kwargs):  # pragma: no cover - the assertion is the point
        raise AssertionError("doctor must not run when doctor.json holds a current record")

    monkeypatch.setattr(poll_module.doctor_module, "doctor", boom)


def poll(world: World, gh: StubGitHub, config: Config, run: StubRun, **kwargs):
    lines: list[str] = []
    result = poll_module.poll(
        world.repo,
        gh,
        config,
        parent_env=kwargs.pop("parent_env", world.env()),
        run_issue=run,
        out=lines.append,
        **kwargs,
    )
    return result, lines


# ---------------------------------------------------------------- the lock (step 1)


def test_the_poll_lock_is_exclusive_and_release_frees_it(world):
    handle = poll_module.acquire_poll_lock(world.factory_dir)
    assert handle is not None
    lock = world.factory_dir / "run" / "poll.lock"
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()

    # A second open file description conflicts even inside this process: the flock is the lock.
    assert poll_module.acquire_poll_lock(world.factory_dir) is None

    poll_module.release_poll_lock(handle)
    again = poll_module.acquire_poll_lock(world.factory_dir)
    assert again is not None
    assert lock.exists()  # the file is never unlinked; only the flock comes and goes
    poll_module.release_poll_lock(again)


def test_a_lock_file_naming_a_live_foreign_pid_does_not_block_a_tick(world):
    """Pids are not evidence (design §13: every tick runs in a fresh container, where pid 7 is a different
    process each time). A leftover file naming a pid that happens to be alive here must not wedge the timer;
    only a held flock blocks."""
    lock = world.factory_dir / "run" / "poll.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": os.getppid(), "started_at": now_iso()}), encoding="utf-8")

    handle = poll_module.acquire_poll_lock(world.factory_dir)

    assert handle is not None
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    poll_module.release_poll_lock(handle)


def test_a_lock_file_left_by_a_dead_process_is_acquired(world):
    lock = world.factory_dir / "run" / "poll.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps({"pid": dead_pid(), "started_at": now_iso()}), encoding="utf-8")

    handle = poll_module.acquire_poll_lock(world.factory_dir)

    assert handle is not None
    poll_module.release_poll_lock(handle)


def test_a_lock_held_by_another_process_blocks_until_that_process_dies(world):
    """Why the lock is an flock: the kernel drops it when the holder dies, so a killed container — or an OOM,
    or a reboot — cannot leave behind a lock no later tick can clear."""
    lock = world.factory_dir / "run" / "poll.lock"
    lock.parent.mkdir(parents=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys\n"
            "handle = open(sys.argv[1], 'a+')\n"
            "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "sys.stdin.readline()\n",
            str(lock),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert poll_module.acquire_poll_lock(world.factory_dir) is None
    finally:
        holder.kill()
        holder.wait()

    handle = poll_module.acquire_poll_lock(world.factory_dir)
    assert handle is not None
    poll_module.release_poll_lock(handle)


def test_a_held_lock_makes_poll_exit_zero_without_touching_github(world, config, no_doctor):
    world.doctor_record()
    lock = world.factory_dir / "run" / "poll.lock"
    handle = poll_module.acquire_poll_lock(world.factory_dir)
    held = lock.read_text(encoding="utf-8")
    gh, run = StubGitHub(world.checkout, issues=[42]), StubRun()

    try:
        result, lines = poll(world, gh, config, run)
    finally:
        poll_module.release_poll_lock(handle)

    assert result.exit_code == 0
    assert result.ran == [] and result.skipped == {}
    assert gh.gh_calls == 0 and run.calls == []
    assert lock.read_text(encoding="utf-8") == held  # the holder's record is untouched
    assert any("nothing to do" in line for line in lines)


# ---------------------------------------------------------------- preflight (step 0)


def test_subscription_auth_is_refused_before_any_github_call(world, config, no_doctor):
    world.doctor_record()
    config.auth = "subscription"
    gh, run = StubGitHub(world.checkout, issues=[42]), StubRun()

    with pytest.raises(FactoryError) as excinfo:
        poll(world, gh, config, run)

    assert 'auth = "api"' in excinfo.value.message
    assert "subscription" in excinfo.value.message
    assert gh.gh_calls == 0 and run.calls == []
    assert not (world.factory_dir / "run" / "poll.lock").exists()


def test_a_missing_provider_key_is_refused_before_any_github_call(world, config, no_doctor):
    world.doctor_record()
    gh, run = StubGitHub(world.checkout, issues=[42]), StubRun()

    with pytest.raises(FactoryError) as excinfo:
        poll(world, gh, config, run, parent_env=world.env(ANTHROPIC_API_KEY=None))

    assert "ANTHROPIC_API_KEY is not set" in excinfo.value.message
    assert gh.gh_calls == 0 and run.calls == []


def test_doctor_runs_when_no_current_record_exists(world, config, monkeypatch):
    calls = []

    def fake_doctor(repo, cfg, **kwargs):
        calls.append(kwargs)
        world.doctor_record()  # the real doctor writes its record on ok
        return poll_module.doctor_module.DoctorReport(ok=True, lines=["ok: probed"])

    monkeypatch.setattr(poll_module.doctor_module, "doctor", fake_doctor)
    gh, run = StubGitHub(world.checkout, issues=[]), StubRun()

    result, lines = poll(world, gh, config, run)

    assert result.exit_code == 0
    assert [call["harness"] for call in calls] == ["claude"]
    assert calls[0]["auth"] == "api"
    assert any("running it" in line for line in lines)


def test_a_stale_cli_version_makes_doctor_run_again(world, config, monkeypatch):
    world.doctor_record(cli_version="1.0.0 (Claude Code)")  # the pinned CLI was upgraded since
    ran = []
    monkeypatch.setattr(
        poll_module.doctor_module,
        "doctor",
        lambda repo, cfg, **kw: (
            ran.append(kw) or poll_module.doctor_module.DoctorReport(ok=True, lines=["ok"])
        ),
    )

    poll(world, StubGitHub(world.checkout), config, StubRun())

    assert len(ran) == 1


def test_a_failing_doctor_stops_the_tick(world, config, monkeypatch):
    report = poll_module.doctor_module.DoctorReport(
        ok=False, lines=["ok: python 3.12.3", "FAIL: claude is not on PATH"]
    )
    monkeypatch.setattr(poll_module.doctor_module, "doctor", lambda *a, **k: report)
    gh, run = StubGitHub(world.checkout, issues=[42]), StubRun()

    with pytest.raises(FactoryError) as excinfo:
        poll(world, gh, config, run)

    assert "doctor failed for claude:api" in excinfo.value.message
    assert "FAIL: claude is not on PATH" in (excinfo.value.hint or "")
    assert gh.gh_calls == 0 and run.calls == []


def test_a_current_record_skips_doctor_and_names_the_cli_version(world, config, no_doctor):
    world.doctor_record()
    gh = StubGitHub(world.checkout, issues=[])

    result, lines = poll(world, gh, config, StubRun())

    assert result.exit_code == 0
    assert any(STUB_VERSION in line and "current record" in line for line in lines)


# ---------------------------------------------------------------- classification (step 3)


def test_an_issue_with_no_branch_anywhere_is_new(world, config, no_doctor):
    world.doctor_record()
    gh, run = StubGitHub(world.checkout, issues=[42]), StubRun()

    result, _ = poll(world, gh, config, run)

    assert result.ran == [42] and result.skipped == {}
    assert run.calls == [42]
    assert gh.labels_asked == ["factory"]


def test_an_issue_with_outcome_null_resumes(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    gh, run = StubGitHub(world.checkout, issues=[7]), StubRun()

    result, lines = poll(world, gh, config, run)

    assert result.ran == [7] and run.calls == [7]
    assert any("issue 7: resume; running" in line for line in lines)


def test_a_parked_issue_is_skipped_and_never_runs(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    world.park(7, "rounds_exhausted")
    gh, run = StubGitHub(world.checkout, issues=[7]), StubRun()

    result, _ = poll(world, gh, config, run)

    assert result.exit_code == 0
    assert result.skipped == {7: "parked"} and result.ran == []
    assert run.calls == []


def test_a_park_commit_that_touched_more_than_state_json_is_not_parked(world, config, no_doctor):
    """is_parked accepts a state-ONLY child commit; anything else means someone changed the branch."""
    world.doctor_record()
    world.start(7)
    world.park(7, "no_progress", also="notes.txt")
    gh, run = StubGitHub(world.checkout, issues=[7]), StubRun()

    result, _ = poll(world, gh, config, run)

    assert result.skipped == {} and result.ran == [7]


def test_an_operator_commit_reopens_a_parked_issue(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    world.park(7, "no_progress")
    world.operator_commit(7)
    gh, run = StubGitHub(world.checkout, issues=[7]), StubRun()

    result, lines = poll(world, gh, config, run)

    assert result.ran == [7]
    assert any("operator acted" in line for line in lines)


def test_a_done_issue_is_skipped(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    world.finish(7)
    gh, run = StubGitHub(world.checkout, issues=[7]), StubRun()

    result, _ = poll(world, gh, config, run)

    assert result.skipped == {7: "done"} and run.calls == []


def test_a_worktree_is_rebuilt_from_the_remote_branch(world, config, no_doctor):
    """Rule 6: a wiped .factory/ (a rebuilt host) resumes every run from origin/factory/<n>."""
    world.doctor_record()
    world.start(7, push=True)
    head = world.head(7)
    world.repo.remove_worktree(7)
    world.repo.delete_branch(branch_name(7), remote=False)
    shutil.rmtree(world.factory_dir)
    world.doctor_record()
    assert not world.repo.local_branch_exists(branch_name(7))
    gh, run = StubGitHub(world.checkout, issues=[7]), StubRun()

    result, _ = poll(world, gh, config, run)

    assert result.ran == [7]
    assert world.repo.worktree_exists(7)
    assert world.head(7) == head


def test_classify_syncs_the_worktree_with_origin_before_reading_head(world, config, no_doctor):
    """Design §12 "classify from local state after git fetch" + §7's fast-forward: a commit pushed from
    elsewhere (an operator on another host, a `factory` run on the workstation) decides the classification.
    Without the sync this host would read its stale HEAD and re-run an issue that is already parked."""
    world.doctor_record()
    wt = world.start(7, push=True)
    behind = world.head(7)
    world.park(7, "no_progress")
    parked_head = world.head(7)
    world.repo.push(wt, branch_name(7))
    run_git(["reset", "--hard", behind], wt)  # this host is a tick behind the branch
    gh, run = StubGitHub(world.checkout, issues=[7]), StubRun()

    result, _ = poll(world, gh, config, run)

    assert result.skipped == {7: "parked"} and run.calls == []
    assert world.head(7) == parked_head  # fast-forwarded before HEAD was read


def test_a_diverged_branch_is_reported_as_that_issue_s_error(world, config, no_doctor):
    world.doctor_record()
    world.start(7, push=True)
    world.operator_commit(7, "src/local.py")  # one commit here ...
    other = world.root / "other"
    run_git(["clone", str(world.origin), str(other)], world.root)
    run_git(["checkout", "-b", branch_name(7), f"origin/{branch_name(7)}"], other)
    (other / "src" / "remote.py").write_text("# pushed from elsewhere\n", encoding="utf-8")
    run_git(["add", "-A"], other)
    run_git(["commit", "-m", "operator: fix from another clone"], other)
    run_git(["push", "origin", branch_name(7)], other)  # ... and a different one on origin
    world.start(8)
    gh, run = StubGitHub(world.checkout, issues=[7, 8]), StubRun()

    result, lines = poll(world, gh, config, run)

    assert result.exit_code == 1
    assert result.skipped == {7: "error"} and result.ran == [8]
    assert any("issue 7: cannot classify" in line and "diverged" in line for line in lines)
    assert "7" not in world.journal()  # a classification failure is not a run failure


def test_unreadable_state_reports_exit_one_without_stopping_the_other_issues(
    world, config, no_doctor
):
    world.doctor_record()
    wt = world.start(7)
    (wt / "work" / "7" / "state.json").write_text("{not json", encoding="utf-8")
    world.repo.commit_all(wt, "corrupt state.json")
    world.start(8)
    gh, run = StubGitHub(world.checkout, issues=[7, 8]), StubRun()

    result, lines = poll(world, gh, config, run)

    assert result.exit_code == 1
    assert result.skipped == {7: "error"} and result.ran == [8]
    assert run.calls == [8]
    assert any("issue 7: cannot classify" in line for line in lines)
    assert "7" not in world.journal()  # a classification failure is not a run failure


# ---------------------------------------------------------------- the journal (step 4)


def test_exit_one_records_a_failure_and_a_second_tick_increments_it(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    head = world.head(7)
    gh, run = StubGitHub(world.checkout, issues=[7]), StubRun(default=1)

    first, _ = poll(world, gh, config, run)
    assert first.exit_code == 1
    entry = world.journal()["7"]
    assert entry["failures"] == 1 and entry["sha"] == head
    assert "`factory run 7` exited 1" in entry["last_error"]

    second, _ = poll(world, gh, config, run)

    assert second.exit_code == 1
    assert world.journal()["7"]["failures"] == 2
    assert run.calls == [7, 7]


def test_the_counter_resets_when_head_moves(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    write_poll_journal(
        world.factory_dir,
        {"7": {"failures": 2, "sha": world.head(7), "last_error": "boom", "at": now_iso()}},
    )
    moved = world.operator_commit(7)
    gh, run = StubGitHub(world.checkout, issues=[7]), StubRun(default=1)

    result, _ = poll(world, gh, config, run)

    assert result.ran == [7]  # the stale counter did not cap it
    entry = world.journal()["7"]
    assert entry["failures"] == 1 and entry["sha"] == moved


def test_a_commit_the_failing_run_made_itself_does_not_reset_the_cap(world, config, no_doctor):
    """The counter measures consecutive failures over one stretch of history, not consecutive HEADs. A stage
    that commits before exiting 1 (build's baseline log, a park's evidence) would otherwise end every attempt
    at a new commit, replace its own journal entry each tick, and never reach the cap."""
    world.doctor_record()
    world.start(7)
    gh = StubGitHub(world.checkout, issues=[7])
    attempts = []

    def leaky(issue: int) -> int:
        attempts.append(issue)
        world.operator_commit(issue, f"src/leak{len(attempts)}.py")  # the run moved HEAD itself
        return 1

    for expected in (1, 2, 3):
        result, _ = poll(world, gh, config, leaky)
        assert result.ran == [7]
        entry = world.journal()["7"]
        assert entry["failures"] == expected
        assert entry["sha"] == world.head(7)  # where the next tick will find the branch

    result, _ = poll(world, gh, config, StubRun())

    assert result.skipped == {7: "capped"} and attempts == [7, 7, 7]


def test_an_operator_commit_between_two_ticks_still_resets_the_counter(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    gh, run = StubGitHub(world.checkout, issues=[7]), StubRun(default=1)

    poll(world, gh, config, run)
    assert world.journal()["7"]["failures"] == 1
    moved = world.operator_commit(7)

    result, _ = poll(world, gh, config, run)

    assert result.ran == [7]
    entry = world.journal()["7"]
    assert entry["failures"] == 1 and entry["sha"] == moved


def test_exit_two_clears_the_failure_counter(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    write_poll_journal(
        world.factory_dir,
        {"7": {"failures": 2, "sha": world.head(7), "last_error": "boom", "at": now_iso()}},
    )
    gh, run = StubGitHub(world.checkout, issues=[7]), StubRun(default=2)

    result, _ = poll(world, gh, config, run)

    assert result.exit_code == 0 and result.outcomes == {7: 2}
    assert world.journal() == {}


def test_a_run_issue_that_raises_counts_as_a_failure(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    gh = StubGitHub(world.checkout, issues=[7])
    run = StubRun(raises={7: RuntimeError("run_issue exploded")})

    result, lines = poll(world, gh, config, run)

    assert result.exit_code == 1 and result.outcomes == {7: 1}
    assert world.journal()["7"]["failures"] == 1
    assert any("run raised RuntimeError" in line for line in lines)


def test_an_issue_with_no_branch_records_its_failure_against_no_commit(world, config, no_doctor):
    world.doctor_record()
    gh, run = StubGitHub(world.checkout, issues=[42]), StubRun(default=1)

    result, _ = poll(world, gh, config, run)

    assert result.exit_code == 1
    entry = world.journal()["42"]
    assert entry["sha"] == "" and entry["failures"] == 1
    assert "no commit yet" in entry["last_error"]


# ---------------------------------------------------------------- the cap (deviation 18)


def test_a_capped_issue_is_skipped_with_one_pull_request_comment(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    head = world.head(7)
    write_poll_journal(
        world.factory_dir,
        {"7": {"failures": 3, "sha": head, "last_error": "checks failed", "at": now_iso()}},
    )
    pr = PullRequest(
        number=117, url="https://example/pull/117", is_draft=True, state="OPEN", head=branch_name(7)
    )
    gh = StubGitHub(world.checkout, issues=[7], prs={branch_name(7): pr})
    run = StubRun()

    result, _ = poll(world, gh, config, run)

    assert result.skipped == {7: "capped"} and run.calls == []
    assert len(gh.posted) == 1
    number, marker, body = gh.posted[0]
    assert number == 117 and marker == CAPPED_MARKER.format(sha=head)
    assert "checks failed" in body and head[:7] in body
    assert world.journal()["7"]["capped_comment_sha"] == head


def test_the_capped_comment_is_posted_once_per_commit(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    head = world.head(7)
    write_poll_journal(
        world.factory_dir,
        {"7": {"failures": 3, "sha": head, "last_error": "checks failed", "at": now_iso()}},
    )
    pr = PullRequest(
        number=117, url="https://example/pull/117", is_draft=True, state="OPEN", head=branch_name(7)
    )
    gh = StubGitHub(world.checkout, issues=[7], prs={branch_name(7): pr})

    poll(world, gh, config, StubRun())
    lookups_after_first = len(gh.pr_lookups)
    poll(world, gh, config, StubRun())

    assert len(gh.posted) == 1
    assert len(gh.pr_lookups) == lookups_after_first  # the recorded sha stops even the read


def test_a_capped_issue_without_a_pull_request_is_still_skipped(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    write_poll_journal(
        world.factory_dir,
        {"7": {"failures": 5, "sha": world.head(7), "last_error": "boom", "at": now_iso()}},
    )
    gh = StubGitHub(world.checkout, issues=[7])

    result, lines = poll(world, gh, config, StubRun())

    assert result.skipped == {7: "capped"}
    assert gh.posted == []
    assert "capped_comment_sha" not in world.journal()["7"]
    assert any("no pull request" in line for line in lines)


def test_a_failing_capped_comment_does_not_fail_the_tick(world, config, no_doctor):
    world.doctor_record()
    world.start(7)
    world.start(8)
    write_poll_journal(
        world.factory_dir,
        {"7": {"failures": 3, "sha": world.head(7), "last_error": "boom", "at": now_iso()}},
    )
    pr = PullRequest(
        number=117, url="https://example/pull/117", is_draft=True, state="OPEN", head=branch_name(7)
    )
    gh = StubGitHub(world.checkout, issues=[7, 8], prs={branch_name(7): pr})
    gh.comment_fails = True
    run = StubRun()

    result, lines = poll(world, gh, config, run)

    assert result.exit_code == 0
    assert result.skipped == {7: "capped"} and result.ran == [8]
    assert any("could not post the capped comment" in line for line in lines)


def test_the_cap_is_configurable(world, config, no_doctor):
    world.doctor_record()
    config.poll_max_consecutive_failures = 1
    world.start(7)
    write_poll_journal(
        world.factory_dir,
        {"7": {"failures": 1, "sha": world.head(7), "last_error": "boom", "at": now_iso()}},
    )
    gh = StubGitHub(world.checkout, issues=[7])

    result, _ = poll(world, gh, config, StubRun())

    assert result.skipped == {7: "capped"}


# ---------------------------------------------------------------- aggregation (step 5)


def test_every_eligible_issue_runs_in_ascending_order_and_the_worst_exit_code_wins(
    world, config, no_doctor
):
    world.doctor_record()
    for issue in (5, 9, 11):
        world.start(issue)
    world.park(9)
    gh = StubGitHub(world.checkout, issues=[11, 5, 9])
    run = StubRun(codes={5: 0, 11: 1})

    result, _ = poll(world, gh, config, run)

    assert run.calls == [5, 11]
    assert result.ran == [5, 11]
    assert result.skipped == {9: "parked"}
    assert result.outcomes == {5: 0, 11: 1}
    assert result.exit_code == 1
    assert set(world.journal()) == {"11"}


def test_a_gate_alone_is_not_a_failure(world, config, no_doctor):
    world.doctor_record()
    world.start(5)
    world.start(6)
    gh = StubGitHub(world.checkout, issues=[5, 6])
    run = StubRun(codes={5: 2, 6: 0})

    result, _ = poll(world, gh, config, run)

    assert result.exit_code == 0 and result.outcomes == {5: 2, 6: 0}


def test_no_labelled_issue_is_a_quiet_zero(world, config, no_doctor):
    world.doctor_record()
    gh, run = StubGitHub(world.checkout, issues=[]), StubRun()

    result, lines = poll(world, gh, config, run)

    assert result == poll_module.PollResult(exit_code=0)
    assert run.calls == []
    assert any("0 open issue(s)" in line for line in lines)


def test_the_lock_is_released_even_when_the_tick_raises(world, config, no_doctor, monkeypatch):
    world.doctor_record()
    gh = StubGitHub(world.checkout, issues=[42])
    monkeypatch.setattr(
        world.repo, "fetch", lambda: (_ for _ in ()).throw(FactoryError("git fetch failed"))
    )

    with pytest.raises(FactoryError):
        poll(world, gh, config, StubRun())

    handle = poll_module.acquire_poll_lock(world.factory_dir)  # the next tick is not locked out
    assert handle is not None
    poll_module.release_poll_lock(handle)
