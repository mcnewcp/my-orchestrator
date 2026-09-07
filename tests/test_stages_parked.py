"""Design §11 "parked stays parked", per stage command (regression tests for the round-1 fixes).

`run` has always re-raised a recorded gate before doing anything. The single-stage commands did not: `factory
build 42` on an issue parked at `baseline_failing` re-ran the whole configured check suite and rewrote
`work/42/checks/build-1-baseline.log` before `park()`'s no-op branch short-circuited it, and that rewritten log
stayed uncommitted — the next command's `prepare()` committed it as an operator edit, which moved HEAD and
un-parked the issue with no human action.

Self-contained: real git against a bare origin in tmp_path, a stub `gh` object, a harness that fails the test if
it is ever launched, and check commands that leave a marker file so "the checks did not run" is observable. No
conftest fixtures, no fake binaries, no model.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from factory import stages
from factory.config import Config
from factory.errors import NeedsHuman
from factory.gh import PullRequest
from factory.repo import Repo
from factory.stages import Context
from factory.state import Ledger, ReviewRecord, StageRecord, State

ISSUE = 42
BRANCH = f"factory/{ISSUE}"
MARKER = "checks-ran"


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True, stdin=subprocess.DEVNULL
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout.strip()


class StubGitHub:
    """Enough of the GitHub surface for park(); every write is recorded so a re-park can be checked."""

    def __init__(self):
        self.comments: list[tuple[int, str, str | None]] = []
        self.ready: list[int] = []
        self.pr = PullRequest(
            number=117, url="https://example.test/pr/117", is_draft=True, state="OPEN", head=BRANCH
        )

    def find_pr_for_branch(self, branch):
        return self.pr

    def create_draft_pr(self, *, branch, base, title, body):
        return self.pr

    def pr_comment(self, number, body, *, marker=None):
        if marker and any(marker in existing for _, existing, _ in self.comments):
            return False
        self.comments.append((number, body, marker))
        return True

    def pr_ready(self, number):
        self.ready.append(number)


class ExplodingHarness:
    """A parked stage must not launch a session; being called at all is the failure."""

    name = "exploding"

    def run(self, **kwargs):
        raise AssertionError("a parked stage launched a harness session")

    def version(self, env):
        return "9.9.9"


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """A bare origin plus a clone seeded like a small target repo. Returns the clone."""
    for name, value in {
        "GIT_AUTHOR_NAME": "Factory Test",
        "GIT_AUTHOR_EMAIL": "factory-test@localhost",
        "GIT_COMMITTER_NAME": "Factory Test",
        "GIT_COMMITTER_EMAIL": "factory-test@localhost",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(tmp_path / "home"),
    }.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "home").mkdir(exist_ok=True)

    origin = tmp_path / "origin.git"
    git("init", "--bare", "--quiet", "--initial-branch=main", str(origin), cwd=tmp_path)
    seed = tmp_path / "seed"
    (seed / "src").mkdir(parents=True)
    (seed / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (seed / "AGENTS.md").write_text("# Agent instructions\n", encoding="utf-8")
    git("init", "--quiet", "--initial-branch=main", cwd=seed)
    git("add", "-A", cwd=seed)
    git("commit", "--quiet", "-m", "seed", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "--quiet", "-u", "origin", "main", cwd=seed)

    clone = tmp_path / "checkout"
    git("clone", "--quiet", str(origin), str(clone), cwd=tmp_path)
    return clone


def marker_path(workspace: Path) -> Path:
    """Outside the worktree: park() resets and cleans the worktree, so a marker inside it proves nothing."""
    return workspace.parent / MARKER


def make_context(workspace: Path, *, red: bool) -> Context:
    """A prepared context whose one check appends to the marker file and then passes or fails."""
    verdict = "1" if red else "0"
    marker = marker_path(workspace)
    repo = Repo(workspace)
    config = Config(
        base_branch="main",
        auth="subscription",
        max_fix_rounds=1,
        checks=[
            [
                "python3",
                "-c",
                f"open({str(marker)!r}, 'a').write('ran\\n'); raise SystemExit({verdict})",
            ]
        ],
    )
    ctx = Context(
        repo=repo,
        gh=StubGitHub(),
        config=config,
        issue=ISSUE,
        parent_env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
        out=lambda line: None,
    )
    ctx.worktree = repo.ensure_worktree(ISSUE, start_point=repo.base_sha("main"))
    ctx.ledger = Ledger()
    ctx.harness = ExplodingHarness()
    ctx.harness_env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    ctx.prepared = True
    return ctx


def work_file(ctx: Context, name: str, text: str) -> Path:
    path = ctx.worktree / "work" / str(ISSUE) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def stage_record(start_commit: str) -> StageRecord:
    return StageRecord(
        start_commit=start_commit,
        at="2026-09-06T00:00:00Z",
        harness="claude",
        model="m",
        cli_version="9.9.9",
        auth="subscription",
    )


def spec_plan_build(ctx: Context) -> State:
    """Commit spec.md, plan.md and a build the way the stages do; return the loaded state."""
    state = State(
        issue={"number": ISSUE, "snapshot_sha256": "abc", "snapshot_at": "2026-09-06T00:00:00Z"},
        base={"branch": "main", "sha": ctx.repo.base_sha("main")},
        branch=BRANCH,
        pr={"number": 117, "url": "https://example.test/pr/117"},
    )
    work_file(ctx, "spec.md", "## Problem\n\nno greet\n")
    state.stages["spec"] = stage_record(state.base["sha"])
    state.save(ctx.worktree)
    ctx.repo.commit_all(ctx.worktree, f"factory({ISSUE}): spec")

    work_file(ctx, "plan.md", "## Files that change\n\n- src/app.py\n\n## Proof\n\nmake test\n")
    state.stages["plan"] = stage_record(ctx.repo.head(ctx.worktree))
    state.save(ctx.worktree)
    ctx.repo.commit_all(ctx.worktree, f"factory({ISSUE}): plan")

    state.stages["build"] = stage_record(ctx.repo.head(ctx.worktree))
    state.save(ctx.worktree)
    ctx.repo.commit_all(ctx.worktree, f"factory({ISSUE}): build")
    ctx.state = state
    return state


def park_now(ctx: Context, gate: str) -> str:
    """Park the issue at the current HEAD and return the parked HEAD."""
    with pytest.raises(NeedsHuman):
        stages.park(ctx, gate, stages.what_clears(gate, ctx))
    assert stages.is_parked(ctx) is True
    return ctx.repo.head(ctx.worktree)


def marker_ran(workspace: Path) -> bool:
    return marker_path(workspace).exists()


# ---------------------------------------------------------------- park's no-op branch


def test_a_re_park_at_the_same_head_restores_the_worktree(workspace):
    """The stage wrote its evidence before calling park(); a no-op re-park must not leave it behind for the
    next command's prepare() to commit as an operator edit (which would un-park the issue)."""
    ctx = make_context(workspace, red=True)
    spec_plan_build(ctx)
    parked_head = park_now(ctx, "baseline_failing")

    work_file(
        ctx, "checks/build-1-baseline.log", "$ python3 -c ...\nnot byte-identical\n[exit 1]\n"
    )

    with pytest.raises(NeedsHuman) as exc:
        stages.park(ctx, "baseline_failing", "fix the checks")

    assert exc.value.gate == "baseline_failing"
    assert ctx.repo.head(ctx.worktree) == parked_head
    assert ctx.repo.is_clean(ctx.worktree)
    assert not (ctx.worktree / "work" / str(ISSUE) / "checks" / "build-1-baseline.log").exists()
    assert stages.is_parked(ctx) is True
    assert len(ctx.gh.comments) == 1


# ---------------------------------------------------------------- the stage commands


def test_build_on_a_parked_issue_runs_no_checks(workspace):
    ctx = make_context(workspace, red=True)
    spec_plan_build(ctx)
    del ctx.state.stages["build"]  # build has not run: only the park is in the way
    parked_head = park_now(ctx, "baseline_failing")
    comments = len(ctx.gh.comments)

    with pytest.raises(NeedsHuman) as exc:
        stages.build(ctx)

    assert exc.value.gate == "baseline_failing"
    assert not marker_ran(workspace), "the baseline ran again behind the gate"
    assert ctx.repo.head(ctx.worktree) == parked_head
    assert ctx.repo.is_clean(ctx.worktree)
    assert len(ctx.gh.comments) == comments


def test_review_on_a_parked_issue_writes_no_diff_and_launches_nothing(workspace):
    ctx = make_context(workspace, red=False)
    state = spec_plan_build(ctx)
    state.reviews.append(
        ReviewRecord(
            round=1,
            sha=ctx.repo.head(ctx.worktree),
            important_open=1,
            important_resolved=0,
            nits=0,
            reraised_dropped=0,
        )
    )
    parked_head = park_now(ctx, "no_progress")

    with pytest.raises(NeedsHuman) as exc:
        stages.review(ctx)

    assert exc.value.gate == "no_progress"
    assert len(ctx.state.reviews) == 1
    assert not (ctx.worktree / ".factory").exists(), "no review diff was written"
    assert ctx.repo.head(ctx.worktree) == parked_head


def test_fix_on_a_parked_issue_re_raises_the_recorded_gate(workspace):
    ctx = make_context(workspace, red=False)
    state = spec_plan_build(ctx)
    state.reviews.append(
        ReviewRecord(
            round=1,
            sha=ctx.repo.head(ctx.worktree),
            important_open=1,
            important_resolved=0,
            nits=0,
            reraised_dropped=0,
        )
    )
    state.fix_rounds = 1  # at the cap, so fix's own park would raise the same gate
    parked_head = park_now(ctx, "rounds_exhausted")

    with pytest.raises(NeedsHuman) as exc:
        stages.fix(ctx)

    assert exc.value.gate == "rounds_exhausted"
    assert ctx.state.fix_rounds == 1
    assert ctx.repo.head(ctx.worktree) == parked_head
    assert ctx.repo.is_clean(ctx.worktree)


def test_finalize_on_a_parked_issue_runs_no_checks_and_leaves_the_pr_a_draft(workspace):
    ctx = make_context(workspace, red=False)
    state = spec_plan_build(ctx)
    state.reviews.append(
        ReviewRecord(
            round=1,
            sha=ctx.repo.head(ctx.worktree),
            important_open=0,
            important_resolved=0,
            nits=0,
            reraised_dropped=0,
        )
    )
    parked_head = park_now(ctx, "no_progress")

    with pytest.raises(NeedsHuman) as exc:
        stages.finalize(ctx)

    assert exc.value.gate == "no_progress"
    assert not marker_ran(workspace)
    assert ctx.gh.ready == [], "a parked issue must not flip its PR out of draft"
    assert ctx.state.outcome == "needs_human:no_progress"
    assert ctx.repo.head(ctx.worktree) == parked_head


# ---------------------------------------------------------------- --force is an operator action


def test_force_runs_build_on_a_parked_issue(workspace):
    """§11 lists `--force` among the actions that re-open the loop, so it must not be refused by the gate.
    The baseline runs again; here it is still red, so the issue re-parks — but on evidence, not on the record."""
    ctx = make_context(workspace, red=True)
    spec_plan_build(ctx)
    del ctx.state.stages["build"]
    park_now(ctx, "baseline_failing")
    ctx.force = True

    with pytest.raises(NeedsHuman) as exc:
        stages.build(ctx)

    assert exc.value.gate == "baseline_failing"
    assert marker_ran(workspace), "--force must reach the stage, gate and all"


# ---------------------------------------------------------------- the gated review flag


def review_round(ctx: Context, *, round: int, important_open: int, fix_rounds_at: int) -> None:
    """Record and commit a review the way stages.review does, so park() has nothing else pending."""
    ctx.state.reviews.append(
        ReviewRecord(
            round=round,
            sha=ctx.repo.head(ctx.worktree),
            important_open=important_open,
            important_resolved=0,
            nits=0,
            reraised_dropped=0,
            fix_rounds_at=fix_rounds_at,
        )
    )
    ctx.state.save(ctx.worktree)
    ctx.repo.commit_all(ctx.worktree, f"factory({ISSUE}): review {round}")


@pytest.mark.parametrize("gate", stages.REVIEW_LOOP_GATES)
def test_park_marks_the_review_that_raised_a_review_loop_gate(workspace, gate):
    """`run` reads the flag back. It is the only thing that tells "the operator dismissed a finding" from
    "nothing has happened": a `dismiss` moves HEAD without changing one line of code, so the gate would
    otherwise be re-raised over the very review the operator answered."""
    ctx = make_context(workspace, red=False)
    spec_plan_build(ctx)
    review_round(ctx, round=1, important_open=1, fix_rounds_at=1)

    park_now(ctx, gate)

    committed = State.load(ctx.worktree, ISSUE)
    assert committed.reviews[-1].gated is True
    assert stages.is_parked(ctx) is True  # the flag travelled in park's state-only commit


@pytest.mark.parametrize("gate", ["open_questions", "baseline_failing"])
def test_park_leaves_the_review_alone_for_a_gate_the_loop_did_not_raise(workspace, gate):
    ctx = make_context(workspace, red=False)
    spec_plan_build(ctx)
    review_round(ctx, round=1, important_open=0, fix_rounds_at=0)

    park_now(ctx, gate)

    assert State.load(ctx.worktree, ISSUE).reviews[-1].gated is False
