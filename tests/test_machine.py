"""Transitions, caps, the remediation loop, stuck detection, and a full happy path."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import (FakeRunner, OLD, blocking, commit, head_of, write_audit, write_contract,
                      write_ledger, write_review, write_run)
from orch import machine, shell
from orch.config import Config
from orch.machine import Ctx, run, scratch_dir, step
from orch.state import State, next_audit_round, next_review_round


def make_ctx(repo: Path, fake: FakeRunner, **policy) -> Ctx:
    """A Ctx wired to a fake runner, with policy overrides."""
    config = Config(policy={"review_round_cap": 2, "audit_failure_cap": 3, "max_steps": 40,
                            **policy})
    return Ctx(repo=repo, config=config, runner_factory=lambda role, cfg: fake)


@pytest.fixture
def gh_calls(monkeypatch):
    """Record `gh` invocations instead of running them."""
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args, cwd, check=True):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(shell, "gh", fake_gh)
    return calls


# ----------------------------------------------------------------------- happy path


def test_full_happy_path(repo, gh_calls):
    scratch = scratch_dir(repo, 17)
    fake = FakeRunner()
    fake.handlers = {
        "contractor": lambda: write_contract(scratch),
        "implementer": lambda: (commit(repo, "feature (#17)"),
                                write_run(scratch, branch="main", pr_number=7)),
        "auditor": lambda: write_audit(scratch, next_audit_round(scratch), passed=True,
                                       commit=head_of(repo)),
        "reviewer": lambda: write_review(scratch, next_review_round(scratch), verdict="APPROVE",
                                         commit=head_of(repo)),
    }

    result = run(17, make_ctx(repo, fake))

    assert fake.calls == ["contractor", "implementer", "auditor", "reviewer"]
    assert result.reason == "terminal" and result.state is State.READY and result.ok
    summary = scratch / "summary.md"
    assert summary.exists()
    assert "Add percent_change helper (#17)" in summary.read_text()
    assert gh_calls == [
        ("pr", "edit", "7", "--body-file", str(summary)),
        ("pr", "ready", "7"),
    ]


def test_pause_after_contract_stops_before_implementing(repo):
    scratch = scratch_dir(repo, 17)
    fake = FakeRunner({"contractor": lambda: write_contract(scratch)})

    result = run(17, make_ctx(repo, fake), pause_after_contract=True)

    assert fake.calls == ["contractor"]
    assert result.reason == "paused" and result.state is State.IMPLEMENTING and result.ok
    assert (scratch / "run.json").exists()


def test_run_stops_at_max_steps(repo):
    scratch = scratch_dir(repo, 17)
    fake = FakeRunner({"contractor": lambda: write_contract(scratch)})
    result = run(17, make_ctx(repo, fake, max_steps=1))
    assert result.reason == "max_steps" and not result.ok and result.steps == 1


# ------------------------------------------------------------------- audit failures


def implemented(repo: Path) -> Path:
    """Scratch dir with a contract and an open draft PR on the current branch."""
    scratch = scratch_dir(repo, 17)
    write_contract(scratch)
    write_run(scratch, branch="main", pr_number=7)
    return scratch


def test_audit_failure_cap_writes_escalation(repo):
    scratch = implemented(repo)
    write_audit(scratch, 1, passed=False, commit=OLD)
    write_audit(scratch, 2, passed=False, commit=OLD)
    fake = FakeRunner({"auditor": lambda: write_audit(scratch, 3, passed=False,
                                                      commit=head_of(repo),
                                                      failures=["typecheck failed: x.py:42"])})

    result = step(17, make_ctx(repo, fake))

    assert fake.calls == ["auditor"]
    assert result.state is State.AUDITING and result.next_state is State.ESCALATED
    text = (scratch / "escalation.md").read_text()
    assert "failed 3 times in a row (cap 3)" in text
    assert "typecheck failed: x.py:42" in text


def test_audit_failure_below_the_cap_does_not_escalate(repo):
    scratch = implemented(repo)
    write_audit(scratch, 1, passed=False, commit=OLD)
    fake = FakeRunner({"auditor": lambda: write_audit(scratch, 2, passed=False,
                                                      commit=head_of(repo))})

    result = step(17, make_ctx(repo, fake))

    assert not (scratch / "escalation.md").exists()
    assert result.next_state is State.IMPLEMENTING


def test_audit_failure_cap_is_configurable(repo):
    scratch = implemented(repo)
    fake = FakeRunner({"auditor": lambda: write_audit(scratch, 1, passed=False,
                                                      commit=head_of(repo))})
    step(17, make_ctx(repo, fake, audit_failure_cap=1))
    assert (scratch / "escalation.md").exists()


# ------------------------------------------------------------------ review round cap


def test_review_round_cap_guard_escalates_without_running_the_reviewer(repo):
    scratch = implemented(repo)
    write_audit(scratch, 1, passed=True, commit=head_of(repo))
    write_review(scratch, 1, verdict="REQUEST_CHANGES", commit=OLD)
    write_review(scratch, 2, verdict="REQUEST_CHANGES", commit=OLD)
    fake = FakeRunner()

    result = step(17, make_ctx(repo, fake))

    assert fake.calls == []
    assert result.state is State.REVIEWING and result.next_state is State.ESCALATED
    assert "round cap (2)" in (scratch / "escalation.md").read_text()


def judged(repo: Path, *, rounds_completed: int = 1) -> Path:
    """Scratch dir sitting in JUDGING: a passing audit and an unjudged REQUEST_CHANGES review."""
    scratch = implemented(repo)
    write_audit(scratch, 1, passed=True, commit=head_of(repo))
    write_review(scratch, 1, verdict="REQUEST_CHANGES", commit=OLD)
    write_review(scratch, 2, verdict="REQUEST_CHANGES", commit=head_of(repo))
    write_ledger(scratch, rounds_completed=rounds_completed, findings=[blocking("F-1-1",
                                                                                resolved=True)])
    return scratch


def judge_leaves_a_blocker(scratch: Path):
    """A judge that adjudicates round 2 and leaves one blocking finding open."""
    return lambda: write_ledger(scratch, rounds_completed=2,
                                findings=[blocking("F-1-1", resolved=True), blocking("F-2-1")])


def test_judging_escalates_at_the_cap_with_a_blocking_finding_open(repo):
    scratch = judged(repo)
    fake = FakeRunner({"judge": judge_leaves_a_blocker(scratch)})

    result = step(17, make_ctx(repo, fake, review_round_cap=2))

    assert fake.calls == ["judge"]
    assert result.state is State.JUDGING and result.next_state is State.ESCALATED
    text = (scratch / "escalation.md").read_text()
    assert "Review round cap reached with blocking findings open" in text
    assert "F-2-1" in text


def test_judging_below_the_cap_keeps_remediating(repo):
    scratch = judged(repo)
    fake = FakeRunner({"judge": judge_leaves_a_blocker(scratch)})

    result = step(17, make_ctx(repo, fake, review_round_cap=3))

    assert not (scratch / "escalation.md").exists()
    assert result.next_state is State.REMEDIATING


def test_judging_at_the_cap_without_open_blockers_does_not_escalate(repo):
    scratch = judged(repo)
    fake = FakeRunner({"judge": lambda: write_ledger(
        scratch, rounds_completed=2, findings=[blocking("F-1-1", resolved=True)])})

    result = step(17, make_ctx(repo, fake, review_round_cap=2))

    assert not (scratch / "escalation.md").exists()
    assert result.next_state is State.FINALIZING


def test_review_within_the_cap_runs_the_reviewer(repo):
    scratch = implemented(repo)
    write_audit(scratch, 1, passed=True, commit=head_of(repo))
    write_review(scratch, 1, verdict="REQUEST_CHANGES", commit=OLD)
    fake = FakeRunner({"reviewer": lambda: write_review(scratch, 2, verdict="APPROVE",
                                                        commit=head_of(repo))})

    result = step(17, make_ctx(repo, fake))

    assert fake.calls == ["reviewer"]
    assert result.next_state is State.FINALIZING


# ------------------------------------------------------------------ remediation loop


def test_remediating_runs_once_per_open_blocking_finding(repo):
    scratch = implemented(repo)
    write_audit(scratch, 1, passed=False, commit=head_of(repo))
    write_ledger(scratch, findings=[blocking("F-1-1"), blocking("F-1-2"),
                                    {"id": "F-1-3", "class": "style",
                                     "disposition": "deferred", "resolved": False}])

    def resolve_first():
        import json
        data = json.loads((scratch / "ledger.json").read_text())
        for finding in data["findings"]:
            if finding["disposition"] == "blocking" and not finding["resolved"]:
                finding["resolved"] = True
                finding["resolved_commit"] = commit(repo, "fix (#17)")
                break
        (scratch / "ledger.json").write_text(json.dumps(data))

    fake = FakeRunner({"remediator": resolve_first})
    result = step(17, make_ctx(repo, fake))

    assert fake.calls == ["remediator", "remediator"]
    assert result.state is State.REMEDIATING and result.next_state is State.AUDITING


def test_remediation_count_is_taken_at_entry(repo):
    scratch = implemented(repo)
    write_audit(scratch, 1, passed=False, commit=head_of(repo))
    write_ledger(scratch, findings=[blocking("F-1-1"), blocking("F-1-2")])
    fake = FakeRunner()  # a remediator that resolves nothing must not loop forever
    step(17, make_ctx(repo, fake))
    assert fake.calls == ["remediator", "remediator"]


# ---------------------------------------------------------------- stuck / branching


def test_run_escalates_when_a_step_changes_nothing(repo):
    fake = FakeRunner()  # every role is a no-op
    fake.last_message = "I could not proceed because the criterion depends on a sibling PR."
    result = run(17, make_ctx(repo, fake))
    assert result.reason == "terminal" and result.ok
    assert result.state is State.ESCALATED
    assert "contractor" in result.detail
    assert fake.calls == ["contractor", "contractor"]  # first step created run.json
    escalation = (repo / ".scratch" / "17" / "escalation.md").read_text()
    assert "CONTRACTING did not advance after running contractor" in escalation
    assert "> I could not proceed because the criterion depends on a sibling PR." in escalation


def test_step_checks_out_the_branch_from_run_json(repo):
    subprocess.run(["git", "branch", "issue-17/x"], cwd=repo, check=True, capture_output=True)
    scratch = scratch_dir(repo, 17)
    write_contract(scratch)
    write_run(scratch, branch="issue-17/x", pr_number=7)
    fake = FakeRunner({"auditor": lambda: write_audit(scratch, 1, passed=True,
                                                      commit=head_of(repo))})

    step(17, make_ctx(repo, fake))

    assert shell.current_branch(repo) == "issue-17/x"


def test_terminal_states_do_nothing(repo):
    scratch = scratch_dir(repo, 17)
    scratch.mkdir(parents=True)
    (scratch / "clarification.md").write_text("what does 'fast' mean?\n")
    fake = FakeRunner()
    result = step(17, make_ctx(repo, fake))
    assert fake.calls == []
    assert result.state is State.NEEDS_CLARIFICATION and result.terminal
