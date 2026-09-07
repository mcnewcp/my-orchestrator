"""Unit tests for the load-bearing helpers in stages.py (design §7, §9, §11).

Self-contained: real git against a bare "origin" in tmp_path, a stub `gh` object and a stub harness. No fixtures from
conftest.py, no fake binaries, no network, no model. The end-to-end flows through cli.main live elsewhere.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from factory import stages
from factory import state as state_module
from factory.config import Config
from factory.errors import FactoryError, GateViolation, NeedsHuman
from factory.gh import PullRequest
from factory.harness import HarnessResult
from factory.repo import Repo
from factory.stages import Context
from factory.state import Finding, Ledger, ReviewRecord, RunLock, StageRecord, State

ISSUE = 42
BRANCH = "factory/42"


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True, stdin=subprocess.DEVNULL
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc.stdout.strip()


class StubGitHub:
    """Records every write the stages make; returns a fixed draft PR."""

    def __init__(self):
        self.comments: list[tuple[int, str, str | None]] = []
        self.ready: list[int] = []
        self.closed: list[int] = []
        self.labels_removed: list[str] = []
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

    def pr_close(self, number, comment=None):
        self.closed.append(number)

    def remove_label(self, number, label):
        self.labels_removed.append(label)


class StubHarness:
    """One canned HarnessResult; optionally writes files into the worktree first (a read stage must not)."""

    name = "stub"

    def __init__(self, output: dict, *, writes: dict[str, str] | None = None, num_turns: int = 3):
        self.output = output
        self.writes = writes or {}
        self.num_turns = num_turns
        self.calls: list[dict] = []

    def run(self, **kwargs) -> HarnessResult:
        self.calls.append(kwargs)
        transcript = kwargs["transcript_path"]
        transcript.write_text(json.dumps({"stub": True}), encoding="utf-8")
        for rel, text in self.writes.items():
            path = kwargs["cwd"] / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return HarnessResult(
            output=self.output,
            transcript_path=transcript,
            exit_code=0,
            cli_version="9.9.9",
            model="stub-model-1",
            duration_s=0.5,
            num_turns=self.num_turns,
        )

    def version(self, env):
        return "9.9.9"


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    """A bare origin plus a clone, seeded like a small target repository. Returns the clone."""
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
    seed.mkdir()
    git("init", "--quiet", "--initial-branch=main", cwd=seed)
    (seed / "src").mkdir()
    (seed / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (seed / "Makefile").write_text("test:\n\t@true\n", encoding="utf-8")
    (seed / "AGENTS.md").write_text("# Agent instructions\n", encoding="utf-8")
    git("add", "-A", cwd=seed)
    git("commit", "--quiet", "-m", "seed", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "--quiet", "-u", "origin", "main", cwd=seed)

    clone = tmp_path / "checkout"
    git("clone", "--quiet", str(origin), str(clone), cwd=tmp_path)
    return clone


def make_context(workspace: Path, **config_kwargs) -> Context:
    repo = Repo(workspace)
    config = Config(
        base_branch="main",
        auth="subscription",
        checks=[["true"]],
        **config_kwargs,
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
    ctx.prepared = True
    return ctx


def work_file(ctx: Context, name: str, text: str) -> Path:
    path = ctx.worktree / "work" / str(ISSUE) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def base_state(ctx: Context) -> State:
    return State(
        issue={"number": ISSUE, "snapshot_sha256": "abc", "snapshot_at": "2026-09-06T00:00:00Z"},
        base={"branch": "main", "sha": ctx.repo.base_sha("main")},
        branch=BRANCH,
        pr={"number": 117, "url": "https://example.test/pr/117"},
    )


def spec_and_plan(ctx: Context) -> State:
    """Commit a spec and a plan the way the stages do, and return the loaded state."""
    state = base_state(ctx)
    work_file(ctx, "intent.md", "# Intent: add greet\n")
    work_file(ctx, "spec.md", "## Problem\nno greet\n\n## Open questions\n\nNone.\n")
    state.stages["spec"] = StageRecord(
        start_commit=state.base["sha"],
        at="2026-09-06T00:00:00Z",
        harness="claude",
        model="m",
        cli_version="9.9.9",
        auth="subscription",
    )
    state.save(ctx.worktree)
    ctx.repo.commit_all(ctx.worktree, "factory(42): spec")

    plan_start = ctx.repo.head(ctx.worktree)
    work_file(ctx, "plan.md", "## Files that change\n\n- src/app.py\n\n## Proof\n\nmake test\n")
    state.stages["plan"] = StageRecord(
        start_commit=plan_start,
        at="2026-09-06T00:01:00Z",
        harness="claude",
        model="m",
        cli_version="9.9.9",
        auth="subscription",
    )
    state.reviews.append(
        ReviewRecord(
            round=1,
            sha=plan_start,
            important_open=1,
            important_resolved=0,
            nits=0,
            reraised_dropped=0,
        )
    )
    state.fix_rounds = 2
    state.set_outcome("needs_human:rounds_exhausted", plan_start)
    state.save(ctx.worktree)
    ctx.ledger = Ledger(
        findings=[
            Finding(
                id="F1",
                key="k1",
                pass_="bugs",
                severity="important",
                file="src/app.py",
                line=2,
                title="off by one",
                detail="d",
                evidence="e",
                opened_round=1,
            )
        ]
    )
    ctx.ledger.save(ctx.worktree, ISSUE)
    ctx.repo.commit_all(ctx.worktree, "factory(42): plan")
    ctx.state = State.load(ctx.worktree, ISSUE)
    return ctx.state


def ready_for_review(ctx: Context) -> State:
    """spec, plan and build recorded and committed, no outcome and no review yet: what `review` expects."""
    state = base_state(ctx)
    work_file(ctx, "intent.md", "# Intent: add greet\n")
    work_file(ctx, "spec.md", "## Problem\nno greet\n")
    work_file(ctx, "plan.md", "## Files that change\n\n- src/app.py\n\n## Proof\n\nmake test\n")
    for name in stages.STAGE_ORDER:
        state.stages[name] = StageRecord(
            start_commit=state.base["sha"],
            at="2026-09-06T00:00:00Z",
            harness="claude",
            model="m",
            cli_version="9.9.9",
            auth="subscription",
        )
    state.save(ctx.worktree)
    ctx.repo.commit_all(ctx.worktree, "factory(42): build")
    ctx.state = State.load(ctx.worktree, ISSUE)
    return ctx.state


# ---------------------------------------------------------------- what_clears


@pytest.mark.parametrize("gate", stages.GATES)
def test_what_clears_names_an_action_for_every_gate(workspace, gate):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    sentence = stages.what_clears(gate, ctx)
    assert sentence.strip()
    assert f"factory run {ISSUE}" in sentence or f"factory accept {ISSUE}" in sentence


def test_what_clears_open_questions_lists_the_questions(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    ctx.state.spec_open_questions = ["Which datastore holds the counter?"]
    sentence = stages.what_clears("open_questions", ctx)
    assert "Which datastore holds the counter?" in sentence
    assert f"factory accept {ISSUE}" in sentence


def test_what_clears_rounds_exhausted_lists_the_open_important_findings(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    ctx.ledger = Ledger(
        findings=[
            Finding("F3", "k", "bugs", "important", "svc/x.py", 41, "unchecked index", "d", "e", 1),
            Finding("F4", "k2", "bugs", "nit", "svc/y.py", None, "spelling", "d", "e", 1),
        ]
    )
    sentence = stages.what_clears("rounds_exhausted", ctx)
    assert "F3 svc/x.py:41 unchecked index" in sentence
    assert "F4" not in sentence  # nits never block


def test_what_clears_falls_back_to_a_generic_sentence(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    assert f"factory run {ISSUE}" in stages.what_clears("something_new", ctx)


# ---------------------------------------------------------------- is_parked / check_parked


class ParkStubRepo:
    """Only what stages.is_parked reads: head, its parent, and the paths that commit changed."""

    def __init__(self, head, parent, changed):
        self._head, self._parent, self._changed = head, parent, changed

    def head(self, _wt):
        return self._head

    def parent(self, _sha, _wt=None):
        return self._parent

    def changed_paths_of_commit(self, _wt, _sha):
        return self._changed


def parked_ctx(
    head, parent, changed, *, outcome="needs_human:no_progress", outcome_sha="A"
) -> Context:
    ctx = Context(
        repo=ParkStubRepo(head, parent, changed),
        gh=StubGitHub(),
        config=Config(),
        issue=ISSUE,
    )
    ctx.worktree = Path("/nowhere")
    ctx.state = State(issue={"number": ISSUE}, base={"branch": "main", "sha": "B"}, branch=BRANCH)
    ctx.state.set_outcome(outcome, outcome_sha)
    return ctx


def test_is_parked_when_head_is_the_outcome_commit():
    assert stages.is_parked(parked_ctx("A", "Z", ["src/app.py"])) is True


def test_is_parked_when_head_is_parks_own_state_only_child():
    assert stages.is_parked(parked_ctx("B", "A", [f"work/{ISSUE}/state.json"])) is True


def test_not_parked_when_the_child_commit_touched_anything_else():
    assert (
        stages.is_parked(parked_ctx("B", "A", [f"work/{ISSUE}/state.json", "src/app.py"])) is False
    )


def test_not_parked_after_a_hand_fix_moved_head():
    assert stages.is_parked(parked_ctx("C", "B", ["src/app.py"])) is False


def test_not_parked_without_an_outcome():
    assert stages.is_parked(parked_ctx("A", "Z", [], outcome=None, outcome_sha=None)) is False


def test_is_parked_is_false_without_a_worktree():
    ctx = parked_ctx("A", "Z", [])
    ctx.worktree = None
    assert stages.is_parked(ctx) is False


def test_check_parked_raises_needs_human_with_the_recorded_gate():
    ctx = parked_ctx("A", "Z", [])
    with pytest.raises(NeedsHuman) as exc:
        stages.check_parked(ctx)
    assert exc.value.gate == "no_progress"
    assert exc.value.what_clears_it.strip()


def test_check_parked_is_silent_when_head_moved():
    stages.check_parked(parked_ctx("C", "B", ["src/app.py"]))


# ---------------------------------------------------------------- rewind_for_force


def test_rewind_for_force_rebuilds_state_and_keeps_the_operator_inputs(workspace):
    ctx = make_context(workspace)
    state = spec_and_plan(ctx)
    plan_start = state.stages["plan"].start_commit

    work_file(ctx, "spec.md", "## Problem\nno greet, and it must be case-insensitive\n")
    ctx.repo.commit_all(ctx.worktree, "operator: sharpen the spec")
    edited = (ctx.worktree / "work" / str(ISSUE) / "spec.md").read_text(encoding="utf-8")
    ctx.state = State.load(ctx.worktree, ISSUE)
    ctx.force = True

    stages.rewind_for_force(ctx, "plan")

    assert ctx.repo.is_ancestor(plan_start, "HEAD", ctx.worktree)
    assert not (ctx.worktree / "work" / str(ISSUE) / "plan.md").exists()
    assert (ctx.worktree / "work" / str(ISSUE) / "spec.md").read_text(encoding="utf-8") == edited
    assert set(ctx.state.stages) == {"spec"}
    assert ctx.state.reviews == [] and ctx.state.fix_rounds == 0
    assert ctx.state.outcome is None and ctx.state.outcome_sha is None
    assert ctx.state.pr == {"number": 117, "url": "https://example.test/pr/117"}
    assert ctx.ledger.findings == []
    assert not Ledger.path(ctx.worktree, ISSUE).exists()
    assert ctx.force_push is True
    # the rebuilt state is on disk and committed, and the reset was not pushed
    assert State.load(ctx.worktree, ISSUE).to_dict() == ctx.state.to_dict()
    assert ctx.repo.is_clean(ctx.worktree)
    assert not ctx.repo.remote_branch_exists(BRANCH)


def test_rewind_for_force_on_spec_drops_the_acceptance_and_returns_to_base(workspace):
    ctx = make_context(workspace)
    state = spec_and_plan(ctx)
    state.spec_open_questions = ["blocked on X"]
    state.spec_accepted = {"by": "operator", "at": "2026-09-06T00:02:00Z"}
    state.save(ctx.worktree)
    ctx.repo.commit_all(ctx.worktree, "factory(42): accept spec")
    ctx.state = State.load(ctx.worktree, ISSUE)

    stages.rewind_for_force(ctx, "spec")

    assert (
        ctx.repo.head(ctx.worktree) != state.base["sha"]
    )  # the rebuilt state.json is a new commit
    assert ctx.repo.parent(ctx.repo.head(ctx.worktree), ctx.worktree) == state.base["sha"]
    assert ctx.state.stages == {}
    assert ctx.state.spec_open_questions == [] and ctx.state.spec_accepted is None
    assert not (ctx.worktree / "work" / str(ISSUE) / "spec.md").exists()


def test_rewind_for_force_rejects_a_stage_with_no_rewind_point(workspace):
    ctx = make_context(workspace)
    spec_and_plan(ctx)
    with pytest.raises(FactoryError, match="only spec, plan, build"):
        stages.rewind_for_force(ctx, "review")


# ---------------------------------------------------------------- commit_and_push


def test_commit_and_push_skips_the_commit_when_nothing_changed(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    lines: list[str] = []
    ctx.out = lines.append

    head_after_first = stages.commit_and_push(ctx, "factory(42): spec")
    commits = int(git("rev-list", "--count", "HEAD", cwd=ctx.worktree))

    head_after_second = stages.commit_and_push(ctx, "factory(42): spec")

    assert head_after_second == head_after_first
    assert int(git("rev-list", "--count", "HEAD", cwd=ctx.worktree)) == commits
    assert any("nothing to commit" in line for line in lines)
    assert ctx.repo.remote_branch_exists(BRANCH)  # pushed both times


def test_commit_and_push_commits_only_the_given_paths(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    work_file(ctx, "notes.md", "operator scratch\n")
    ctx.state.fix_rounds = 1

    stages.commit_and_push(ctx, "factory(42): park", paths=[f"work/{ISSUE}/state.json"])

    changed = ctx.repo.changed_paths_of_commit(ctx.worktree, "HEAD")
    assert changed == [f"work/{ISSUE}/state.json"]
    assert ctx.repo.has_changes(ctx.worktree)  # notes.md is still uncommitted


def test_commit_and_push_reports_that_the_commit_is_local_when_the_push_fails(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    git("remote", "set-url", "origin", str(workspace / "gone.git"), cwd=workspace)

    with pytest.raises(FactoryError) as exc:
        stages.commit_and_push(ctx, "factory(42): spec")

    assert "re-running the same command pushes" in (exc.value.hint or "")
    assert int(git("rev-list", "--count", "HEAD", cwd=ctx.worktree)) == 2  # the commit was made


def test_commit_and_push_spends_the_force_push_flag_once(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    ctx.force_push = True
    stages.commit_and_push(ctx, "factory(42): spec")
    assert ctx.force_push is False


# ---------------------------------------------------------------- write_review_diff


def commit_code(ctx: Context, sizes: dict[str, int]) -> None:
    for name, lines in sizes.items():
        path = ctx.worktree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"# {name} line {i}\n" for i in range(lines)), encoding="utf-8")
    work_file(ctx, "state.json", "{}\n")
    ctx.repo.commit_all(ctx.worktree, "build")


def test_write_review_diff_writes_the_whole_diff_and_excludes_work(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    commit_code(ctx, {"src/big.py": 40, "src/small.py": 3})

    rel, nbytes, nlines, truncated, omitted = stages.write_review_diff(ctx, 1)

    assert rel == ".factory/tmp/review-1.diff"
    text = (ctx.worktree / rel).read_text(encoding="utf-8")
    assert truncated is False and omitted == []
    assert nbytes == len(text.encode("utf-8")) and nlines == len(text.splitlines())
    assert "src/big.py" in text and "src/small.py" in text
    assert "work/42/state.json" not in text  # design §9: the review diff excludes work/


def test_write_review_diff_truncates_largest_first_and_names_the_omitted_files(workspace):
    ctx = make_context(workspace, max_diff_bytes=1500)
    ctx.state = base_state(ctx)
    commit_code(ctx, {"src/big.py": 200, "src/medium.py": 60, "src/tiny.py": 2})

    rel, nbytes, _nlines, truncated, omitted = stages.write_review_diff(ctx, 2)

    text = (ctx.worktree / rel).read_text(encoding="utf-8")
    assert truncated is True
    assert omitted  # the files that did not fit are named, not silently dropped
    assert "src/big.py" in text  # largest first
    assert all(name in text for name in omitted)  # the banner names them
    assert "TRUNCATED" in text and "Diffstat" in text
    assert nbytes <= ctx.config.max_diff_bytes


def test_the_review_diff_never_dirties_the_worktree(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    commit_code(ctx, {"src/app2.py": 5})
    stages.write_review_diff(ctx, 1)
    assert ctx.repo.is_clean(ctx.worktree)  # .factory/ is excluded (deviation 12)


# ---------------------------------------------------------------- park


def test_park_commits_only_state_json_and_leaves_the_issue_parked(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    evidence = work_file(ctx, "checks/build-1-baseline.log", "$ true\n[exit 1]\n")

    with pytest.raises(NeedsHuman) as exc:
        stages.park(ctx, "baseline_failing", "fix the checks", evidence_paths=[str(evidence.name)])

    assert exc.value.gate == "baseline_failing"
    head = ctx.repo.head(ctx.worktree)
    assert ctx.repo.changed_paths_of_commit(ctx.worktree, head) == [f"work/{ISSUE}/state.json"]
    assert ctx.state.outcome == "needs_human:baseline_failing"
    assert ctx.state.outcome_sha == ctx.repo.parent(head, ctx.worktree)
    assert stages.is_parked(ctx) is True
    assert ctx.repo.is_clean(ctx.worktree)  # the evidence went into its own commit first
    assert "checks/build-1-baseline.log" in git(
        "show", "--name-only", "--format=", "HEAD~1", cwd=ctx.worktree
    )


def test_park_posts_one_gate_comment_per_outcome_sha(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")

    with pytest.raises(NeedsHuman):
        stages.park(ctx, "no_progress", "fix them by hand")
    assert len(ctx.gh.comments) == 1
    number, body, marker = ctx.gh.comments[0]
    assert number == 117 and marker in body
    assert "no_progress" in body and "fix them by hand" in body

    with pytest.raises(NeedsHuman):  # re-park at the same HEAD: no new commit, no second comment
        stages.park(ctx, "no_progress", "fix them by hand")
    assert len(ctx.gh.comments) == 1


def test_a_re_park_at_the_same_head_adds_no_commit(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    with pytest.raises(NeedsHuman):
        stages.park(ctx, "no_progress", "fix them")
    commits = int(git("rev-list", "--count", "HEAD", cwd=ctx.worktree))
    with pytest.raises(NeedsHuman):
        stages.park(ctx, "no_progress", "fix them")
    assert int(git("rev-list", "--count", "HEAD", cwd=ctx.worktree)) == commits


# ---------------------------------------------------------------- protected paths before a session


def test_branch_protected_path_violations_sees_commits_from_anyone(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    assert stages.branch_protected_path_violations(ctx) == []

    (ctx.worktree / "Makefile").write_text("test:\n\t@echo pwned\n", encoding="utf-8")
    (ctx.worktree / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    ctx.repo.commit_all(ctx.worktree, "operator: change the Makefile")

    assert stages.branch_protected_path_violations(ctx) == ["Makefile"]


def test_run_harness_stage_refuses_to_launch_over_a_protected_change(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    ctx.harness = StubHarness({"markdown": "x", "open_questions": []})
    ctx.harness_env = {"PATH": os.environ["PATH"]}
    (ctx.worktree / ".claude").mkdir()
    (ctx.worktree / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    ctx.repo.commit_all(ctx.worktree, "operator: add a hook")

    with pytest.raises(FactoryError, match="protected paths changed"):
        stages.run_harness_stage(
            ctx, stage="spec", round=1, mode="read", prompt_text="do it", schema_name="spec"
        )
    assert ctx.harness.calls == []


# ---------------------------------------------------------------- run_harness_stage


def prepared_for_harness(ctx: Context, harness: StubHarness) -> None:
    ctx.state = base_state(ctx)
    ctx.harness = harness
    ctx.harness_env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    RunLock.create(ISSUE, "", ctx.worktree).write(ctx.repo.factory_dir)
    ctx.holds_lock = True


def test_run_harness_stage_writes_the_prompt_and_returns_the_validated_output(workspace):
    ctx = make_context(workspace)
    harness = StubHarness({"markdown": "# Spec", "open_questions": []})
    prepared_for_harness(ctx, harness)

    out, result, prompt_rel = stages.run_harness_stage(
        ctx, stage="spec", round=1, mode="read", prompt_text="follow this", schema_name="spec"
    )

    assert out == {"markdown": "# Spec", "open_questions": []}
    assert result.cli_version == "9.9.9"
    assert prompt_rel.as_posix() == f"work/{ISSUE}/prompts/spec-1.md"
    assert (ctx.worktree / prompt_rel).read_text(encoding="utf-8") == "follow this\n"
    call = harness.calls[0]
    assert call["cwd"] == ctx.worktree and call["mode"] == "read"
    assert call["schema_file"].name == "spec.json"
    assert call["max_turns"] == ctx.config.harness_config("claude").max_turns_read
    assert call["agents_md"] == ctx.worktree / "AGENTS.md"
    transcript = call["transcript_path"]
    assert transcript.parent == ctx.repo.factory_dir / "transcripts" / str(ISSUE)
    assert transcript.name.count(".") == 1  # codex derives its -o file with with_suffix()
    assert transcript.with_name(f"{transcript.stem}.prompt.md").exists()


def test_a_read_stage_that_wrote_files_fails_and_the_worktree_is_reset(workspace):
    ctx = make_context(workspace)
    harness = StubHarness(
        {"markdown": "# Spec", "open_questions": []}, writes={"src/sneaky.py": "x = 1\n"}
    )
    prepared_for_harness(ctx, harness)

    with pytest.raises(GateViolation) as exc:
        stages.run_harness_stage(
            ctx, stage="spec", round=1, mode="read", prompt_text="p", schema_name="spec"
        )

    assert "read-only stage" in exc.value.message
    assert exc.value.paths == ["src/sneaky.py"]
    assert not (ctx.worktree / "src" / "sneaky.py").exists()  # reset_hard ran before the raise
    assert ctx.repo.is_clean(ctx.worktree)
    assert harness.calls[0]["transcript_path"].exists()  # the transcript is kept
    assert RunLock.read(ctx.repo.factory_dir, ISSUE).last_error == exc.value.message


def test_schema_invalid_output_fails_the_stage_and_resets(workspace):
    ctx = make_context(workspace)
    harness = StubHarness({"markdown": "# Spec"}, writes={"src/half.py": "x = 1\n"})
    prepared_for_harness(ctx, harness)

    with pytest.raises(FactoryError) as exc:
        stages.run_harness_stage(
            ctx, stage="spec", round=1, mode="read", prompt_text="p", schema_name="spec"
        )

    assert "schema-invalid output" in exc.value.message
    assert "open_questions" in (exc.value.hint or "")
    assert ctx.repo.is_clean(ctx.worktree)


def test_a_write_stage_keeps_what_the_harness_wrote(workspace):
    ctx = make_context(workspace)
    harness = StubHarness(
        {"summary": "did it", "deviations": []},
        writes={"src/app.py": "def add(a, b):\n    return b + a\n"},
    )
    prepared_for_harness(ctx, harness)

    out, _result, prompt_rel = stages.run_harness_stage(
        ctx, stage="build", round=1, mode="write", prompt_text="p", schema_name="build"
    )

    assert out["summary"] == "did it"
    assert "return b + a" in (ctx.worktree / "src" / "app.py").read_text(encoding="utf-8")
    assert harness.calls[0]["max_turns"] == ctx.config.harness_config("claude").max_turns_write
    assert prompt_rel.as_posix() in ctx.repo.changed_paths_in_worktree(ctx.worktree)


# ---------------------------------------------------------------- prepare


def test_prepare_commits_operator_edits_under_work(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    work_file(ctx, "spec.md", "## Problem\nedited by hand\n")
    fresh = make_context(workspace)
    fresh.prepared = False

    stages.prepare(fresh, need_state=True, fetch=False)

    assert fresh.repo.is_clean(fresh.worktree)
    assert f"work/{ISSUE}/spec.md" in fresh.repo.changed_paths_of_commit(fresh.worktree, "HEAD")
    assert fresh.state is not None and fresh.ledger is not None


def test_prepare_refuses_dirty_paths_outside_work(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    (ctx.worktree / "src" / "app.py").write_text("half-finished\n", encoding="utf-8")
    fresh = make_context(workspace)
    fresh.prepared = False

    with pytest.raises(FactoryError, match="uncommitted changes outside"):
        stages.prepare(fresh, need_state=True, fetch=False)


def test_prepare_ignores_transient_droppings(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    cache = ctx.worktree / "src" / "__pycache__"
    cache.mkdir()
    (cache / "app.cpython-312.pyc").write_bytes(b"\x00")
    fresh = make_context(workspace)
    fresh.prepared = False

    stages.prepare(fresh, need_state=True, fetch=False)

    assert (cache / "app.cpython-312.pyc").exists()  # untouched, and not an error


def test_prepare_refuses_to_run_beside_a_live_stage(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    RunLock(issue=ISSUE, stage="build", pid=1, started_at="now", worktree=str(ctx.worktree)).write(
        ctx.repo.factory_dir
    )  # pid 1 is alive and not ours
    fresh = make_context(workspace)
    fresh.prepared = False

    with pytest.raises(FactoryError, match="build in progress"):
        stages.prepare(fresh, need_state=True, fetch=False)

    assert fresh.holds_lock is False  # so cli.py's finally leaves the other writer's lock alone
    assert RunLock.read(fresh.repo.factory_dir, ISSUE).pid == 1


def test_prepare_discards_an_interrupted_stage_and_carries_its_last_error(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    dead = subprocess.Popen(["true"])  # reaped below, so its pid answers "no such process"
    dead.wait()
    RunLock(
        issue=ISSUE,
        stage="build",
        pid=dead.pid,
        started_at="now",
        worktree=str(ctx.worktree),
        last_error="checks failed after build: make test",
    ).write(ctx.repo.factory_dir)
    (ctx.worktree / "src" / "app.py").write_text("partial work\n", encoding="utf-8")

    fresh = make_context(workspace)
    fresh.prepared = False
    lines: list[str] = []
    fresh.out = lines.append

    stages.prepare(fresh, need_state=True, fetch=False)

    assert fresh.last_error == "checks failed after build: make test"
    assert "partial work" not in (fresh.worktree / "src" / "app.py").read_text(encoding="utf-8")
    assert any("interrupted build discarded" in line for line in lines)
    assert RunLock.read(fresh.repo.factory_dir, ISSUE).pid == os.getpid()


def test_prepare_returns_without_a_worktree_when_no_branch_exists_yet(workspace):
    repo = Repo(workspace)
    repo.remove_worktree(7)
    ctx = Context(
        repo=repo,
        gh=StubGitHub(),
        config=Config(base_branch="main", auth="subscription"),
        issue=7,
        parent_env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]},
        out=lambda line: None,
    )

    stages.prepare(ctx, need_state=False, fetch=False)

    assert ctx.worktree is None and ctx.state is None
    assert ctx.harness is not None and ctx.harness_env is not None
    assert RunLock.read(repo.factory_dir, 7) is not None  # the lock still spans the command


def test_prepare_is_a_no_op_the_second_time(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    fresh = make_context(workspace)
    fresh.prepared = False
    stages.prepare(fresh, need_state=True, fetch=False)
    marker = object()
    fresh.harness = marker
    stages.prepare(fresh, need_state=True, fetch=False)
    assert fresh.harness is marker


def test_prepare_rejects_a_state_json_that_points_at_an_unreachable_commit(workspace):
    ctx = make_context(workspace)
    state = base_state(ctx)
    state.stages["spec"] = StageRecord(
        start_commit="0" * 40,
        at="now",
        harness="claude",
        model="m",
        cli_version="9",
        auth="subscription",
    )
    ctx.state = state
    stages.commit_and_push(ctx, "factory(42): spec")
    fresh = make_context(workspace)
    fresh.prepared = False

    with pytest.raises(FactoryError, match="not reachable from HEAD"):
        stages.prepare(fresh, need_state=True, fetch=False)


# ---------------------------------------------------------------- ensure_pr


def test_ensure_pr_returns_the_recorded_pr_without_asking_github(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)

    class Explode:
        def find_pr_for_branch(self, branch):
            raise AssertionError("state.pr is authoritative once recorded")

    ctx.gh = Explode()
    assert stages.ensure_pr(ctx).number == 117


def test_ensure_pr_is_not_fatal_when_gh_fails(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    ctx.state.pr = None
    ctx.state.stages["spec"] = StageRecord(
        start_commit=ctx.state.base["sha"],
        at="now",
        harness="claude",
        model="m",
        cli_version="9",
        auth="subscription",
    )

    class Failing:
        def find_pr_for_branch(self, branch):
            raise FactoryError("gh is not on PATH")

        def create_draft_pr(self, **kwargs):
            raise FactoryError("gh is not on PATH")

    ctx.gh = Failing()
    lines: list[str] = []
    ctx.out = lines.append

    assert stages.ensure_pr(ctx) is None
    assert any("could not reach the pull request" in line for line in lines)
    with pytest.raises(FactoryError, match="gh is not on PATH"):
        stages.ensure_pr(ctx, required=True)


# ---------------------------------------------------------------- the lock and the last-error channel


def test_prepare_takes_a_lock_this_process_owns(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    fresh = make_context(workspace)
    fresh.prepared = False

    stages.prepare(fresh, need_state=True, fetch=False)

    lock = RunLock.read(fresh.repo.factory_dir, ISSUE)
    assert fresh.holds_lock is True
    assert lock.pid == os.getpid() and lock.is_mine()


def test_prepare_carries_a_previous_exit_1_into_the_stage_note(workspace):
    """The lock cannot carry an ordinary exit 1 (cli.py clears it at the end of every command), so the
    message travels in .factory/run/<issue>.last-error until an attempt reads it."""
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    message = "checks failed after build: python3 checks.py"
    state_module.write_last_error(ctx.repo.factory_dir, ISSUE, message)
    fresh = make_context(workspace)
    fresh.prepared = False

    stages.prepare(fresh, need_state=True, fetch=False)

    assert fresh.last_error == message
    assert message in stages._stage_note(fresh, notes=[])
    # The file is consumed, but the lock now carries it: a kill here still reaches the next attempt.
    assert state_module.read_last_error(fresh.repo.factory_dir, ISSUE) is None
    assert RunLock.read(fresh.repo.factory_dir, ISSUE).last_error == message


def test_an_interrupted_stage_outranks_an_older_recorded_exit_1(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    state_module.write_last_error(ctx.repo.factory_dir, ISSUE, "an older exit 1")
    dead = subprocess.Popen(["true"])  # reaped below, so its pid answers "no such process"
    dead.wait()
    RunLock(
        issue=ISSUE,
        stage="build",
        pid=dead.pid,
        started_at="now",
        worktree=str(ctx.worktree),
        last_error="killed mid-build",
    ).write(ctx.repo.factory_dir)
    fresh = make_context(workspace)
    fresh.prepared = False

    stages.prepare(fresh, need_state=True, fetch=False)

    assert fresh.last_error == "killed mid-build"  # the more recent failure wins


def test_commit_and_push_retires_the_previous_failure(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    ctx.last_error = "checks failed after build: python3 checks.py"
    state_module.write_last_error(ctx.repo.factory_dir, ISSUE, ctx.last_error)

    stages.commit_and_push(ctx, "factory(42): spec")

    assert state_module.read_last_error(ctx.repo.factory_dir, ISSUE) is None
    assert ctx.last_error is None  # and the stages that follow in this command say nothing about it


# ---------------------------------------------------------------- failures after the session


def test_a_factory_error_after_the_session_resets_and_records_why(workspace):
    """Design §15: not only the gates. A ledger merge that rejects the reviewer's output, an unwritable
    artifact or a failed push must leave the worktree as clean as a gate does."""
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    (ctx.worktree / "src" / "app.py").write_text("half-written by the session\n", encoding="utf-8")

    with pytest.raises(GateViolation) as exc:
        with stages._after_the_session(ctx):
            raise FactoryError("the ledger merge rejected the round", "look at review-2.json")

    assert exc.value.message == "the ledger merge rejected the round"
    assert exc.value.hint == "look at review-2.json"
    assert ctx.repo.is_clean(ctx.worktree)
    assert "half-written" not in (ctx.worktree / "src" / "app.py").read_text(encoding="utf-8")
    assert state_module.read_last_error(ctx.repo.factory_dir, ISSUE) == exc.value.message


def test_a_gate_violation_after_the_session_is_not_reset_twice(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    already_handled = GateViolation("build changed paths it may not touch: Makefile", ["Makefile"])

    class NoReset:
        factory_dir = ctx.repo.factory_dir

        def reset_hard(self, *args, **kwargs):
            raise AssertionError("_gate_failure has already reset the worktree")

    ctx.repo = NoReset()
    with pytest.raises(GateViolation) as exc:
        with stages._after_the_session(ctx):
            raise already_handled

    assert exc.value is already_handled


def test_a_review_the_ledger_merge_rejects_resets_and_keeps_the_ledger(workspace):
    ctx = make_context(workspace)
    ready_for_review(ctx)
    harness = StubHarness(
        {
            "summary": "one finding",
            "updates": [],
            "new": [
                {
                    "severity": "important",
                    "pass": "bugs",
                    "file": "src/app.py",
                    "line": 1,
                    "title": "   ",  # schema-valid, ledger-invalid: a finding with no title
                    "detail": "d",
                    "evidence": "e",
                }
            ],
        }
    )
    ctx.harness = harness
    ctx.harness_env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    RunLock.create(ISSUE, "", ctx.worktree).write(ctx.repo.factory_dir)

    with pytest.raises(FactoryError, match="is missing 'title'"):
        stages.review(ctx)

    assert ctx.repo.is_clean(ctx.worktree)
    assert ctx.state.reviews == []
    assert not (ctx.worktree / "work" / str(ISSUE) / "review-1.json").exists()
    assert "missing 'title'" in state_module.read_last_error(ctx.repo.factory_dir, ISSUE)
    assert (
        ctx.worktree / ".factory" / "tmp" / "review-1.diff"
    ).exists()  # .factory/ survives a reset


# ---------------------------------------------------------------- the checks run on a pristine tree


def test_the_checks_run_with_the_ignored_droppings_removed(workspace):
    """A write stage's `Bash(uv *)`/`Bash(make *)` can leave (or edit) files git never reports. The checks
    must not run against them; `.factory/` and the stage's own work stay."""
    probe = "import os, sys; sys.exit(1 if os.path.exists('.venv/bin/pytest') else 0)"
    ctx = make_context(workspace)
    ctx.config.checks = [["python3", "-c", probe]]
    ctx.harness_env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    ctx.state = base_state(ctx)
    tampered = ctx.worktree / ".venv" / "bin" / "pytest"
    tampered.parent.mkdir(parents=True)
    tampered.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    keep = ctx.worktree / ".factory" / "tmp" / "review-1.diff"
    keep.parent.mkdir(parents=True)
    keep.write_text("diff --git a/src/app.py b/src/app.py\n", encoding="utf-8")
    (ctx.worktree / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")

    verdict = stages._run_checks(ctx)

    assert verdict.ok, verdict.log
    assert not tampered.exists()  # the toolchain rebuilds it; the gate cannot be undermined
    assert keep.exists()  # the review diff is still being read when a gate fails (deviation 11)
    assert (ctx.worktree / "src" / "new.py").exists()  # only IGNORED files go
    assert "src/new.py" in ctx.repo.changed_paths_in_worktree(ctx.worktree)


# ---------------------------------------------------------------- spec's base sha


def test_spec_reads_origin_only_when_it_is_about_to_create_the_branch(workspace):
    ctx = make_context(workspace)
    ctx.worktree = None
    ctx.state = None

    assert stages._spec_base_sha(ctx, rewound=False) == ctx.repo.base_sha("main")


def test_spec_on_an_existing_branch_bases_on_what_that_branch_was_cut_from(workspace):
    """An interrupted first `spec` leaves a branch with no state.json. Reading origin/main again would
    anchor the run on a commit the branch never contained, and every later diff would be wrong."""
    ctx = make_context(workspace)
    tip = ctx.repo.head(ctx.worktree)
    git("commit", "--quiet", "--allow-empty", "-m", "someone else's commit on main", cwd=workspace)
    git("push", "--quiet", "origin", "main", cwd=workspace)
    ctx.repo.fetch()
    assert ctx.repo.base_sha("main") != tip
    ctx.state = None

    assert stages._spec_base_sha(ctx, rewound=False) == tip


# ---------------------------------------------------------------- the progress line


@pytest.mark.parametrize(("num_turns", "reported"), [(3, True), (0, False)])
def test_the_progress_line_reports_turns_only_when_the_harness_counted_them(
    workspace, num_turns, reported
):
    """Codex reports no turn count; "0 turns" would be a fact the transcript contradicts."""
    ctx = make_context(workspace)
    lines: list[str] = []
    ctx.out = lines.append
    prepared_for_harness(
        ctx, StubHarness({"markdown": "# Spec", "open_questions": []}, num_turns=num_turns)
    )

    stages.run_harness_stage(
        ctx, stage="spec", round=1, mode="read", prompt_text="p", schema_name="spec"
    )

    line = next(line for line in lines if "finished in" in line)
    assert ("3 turns, transcript" in line) is reported
    assert ("turns" in line) is reported
    assert "transcript" in line


# ---------------------------------------------------------------- prepare's other start-of-command rules


def test_prepare_refuses_when_a_protected_path_changed_on_the_branch(workspace):
    """Design §19 / deviation 13: build's baseline checks and finalize's checks run whatever the Makefile
    says, so the whole-branch check belongs at the start of every command, not only before a session."""
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    (ctx.worktree / "Makefile").write_text("test:\n\t@echo nope\n", encoding="utf-8")
    ctx.repo.commit_all(ctx.worktree, "operator rewrites the Makefile")
    fresh = make_context(workspace)
    fresh.prepared = False

    with pytest.raises(FactoryError, match="protected paths changed on factory/42: Makefile"):
        stages.prepare(fresh, need_state=True, fetch=False)


def test_abandons_policy_starts_where_every_other_command_refuses(workspace):
    """`abandon` deletes the branch; a dirty worktree or a protected path it is about to throw away must
    not stop it (cli._PREPARE_POLICY)."""
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    stages.commit_and_push(ctx, "factory(42): spec")
    (ctx.worktree / "Makefile").write_text("test:\n\t@echo nope\n", encoding="utf-8")
    ctx.repo.commit_all(ctx.worktree, "operator rewrites the Makefile")
    (ctx.worktree / "src" / "app.py").write_text("half-finished\n", encoding="utf-8")
    fresh = make_context(workspace)
    fresh.prepared = False

    stages.prepare(
        fresh,
        need_state=False,
        fetch=False,
        commit_operator_edits=False,
        strict_clean=False,
        check_protected=False,
    )

    assert fresh.state is not None and fresh.worktree is not None
    assert not fresh.repo.is_clean(fresh.worktree)  # nothing was committed and nothing discarded


def test_prepare_does_not_publish_a_branch_that_has_no_state_yet(workspace):
    """An interrupted first `spec` leaves a branch with nothing on it. Publishing it would leave an empty
    factory/42 on the remote for someone to clean up, and would make `poll` see work where there is none."""
    ctx = make_context(workspace)
    ctx.prepared = False
    ctx.state = None
    assert not ctx.repo.remote_branch_exists(BRANCH)

    stages.prepare(ctx, need_state=False, fetch=True)

    assert ctx.state is None
    assert not ctx.repo.remote_branch_exists(BRANCH)


def test_prepare_publishes_a_local_only_commit_once_there_is_state(workspace):
    ctx = make_context(workspace)
    ctx.state = base_state(ctx)
    ctx.state.save(ctx.worktree)
    ctx.repo.commit_all(ctx.worktree, "factory(42): spec")  # local only: nothing pushed yet
    fresh = make_context(workspace)
    fresh.prepared = False

    stages.prepare(fresh, need_state=True, fetch=True)

    assert fresh.repo.remote_branch_exists(BRANCH)
    assert fresh.repo.rev_parse(f"origin/{BRANCH}") == fresh.repo.head(fresh.worktree)
