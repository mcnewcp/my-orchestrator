"""End-to-end CLI behaviour with no harness: status, exit codes, setup tolerance."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_contract, write_run
from orch import SKILL_NAMES, runners
from orch.cli import main


@pytest.fixture
def orchestrator(tmp_path: Path) -> Path:
    """A fake orchestrator repo carrying the six role skills."""
    root = tmp_path / "orchestrator"
    for name in SKILL_NAMES.values():
        (root / ".agents" / "skills" / name).mkdir(parents=True)
    return root


def test_status_on_a_fresh_repo_reports_contracting(repo, orchestrator, monkeypatch, capsys):
    monkeypatch.setattr(runners, "repo_root", lambda: orchestrator)
    monkeypatch.chdir(repo)

    assert main(["status", "17"]) == 0

    out = capsys.readouterr().out
    assert "state    CONTRACTING" in out
    assert "branch   main" in out
    assert "(none)" in out
    assert (repo / ".claude/skills/orch-audit").is_symlink()
    assert (repo / ".agents/skills/orch-review").is_symlink()
    assert ".scratch/" in (repo / ".gitignore").read_text()


def test_status_warns_but_works_when_the_role_skills_are_missing(repo, tmp_path, monkeypatch,
                                                                 capsys):
    monkeypatch.setattr(runners, "repo_root", lambda: tmp_path / "empty")
    monkeypatch.chdir(repo)

    assert main(["status", "17"]) == 0

    captured = capsys.readouterr()
    assert "role skills are missing" in captured.err
    assert "state    CONTRACTING" in captured.out


def test_step_fails_loudly_when_the_role_skills_are_missing(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runners, "repo_root", lambda: tmp_path / "empty")
    monkeypatch.chdir(repo)

    assert main(["step", "17"]) == 1
    assert "orch: role skills are missing" in capsys.readouterr().err


def test_status_lists_the_inventory(repo, orchestrator, monkeypatch, capsys):
    monkeypatch.setattr(runners, "repo_root", lambda: orchestrator)
    monkeypatch.chdir(repo)
    scratch = repo / ".scratch" / "17"
    write_contract(scratch)
    write_run(scratch, branch="main", pr_number=7)

    assert main(["status", "17"]) == 0

    out = capsys.readouterr().out
    assert "state    AUDITING" in out
    assert "contract.md        test_budget=12" in out
    assert "run.json           branch=main pr=7" in out


def test_run_escalates_and_exits_zero_when_stuck(repo, orchestrator, monkeypatch, capsys):
    monkeypatch.setattr(runners, "repo_root", lambda: orchestrator)
    monkeypatch.setattr(runners, "get_runner", lambda role, config: _NoopRunner())
    monkeypatch.chdir(repo)

    assert main(["run", "17"]) == 0
    captured = capsys.readouterr()
    assert "ESCALATED:" in captured.out
    assert "did not advance after running contractor" in captured.err
    assert (repo / ".scratch" / "17" / "escalation.md").exists()


class _NoopRunner:
    """A runner that launches nothing, so the state never advances."""

    last_log_path = Path("logs/000-noop.jsonl")

    def run(self, role, issue, cwd):
        """Do nothing and report success."""
        return 0


def test_outside_a_git_repo_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["status", "17"]) == 1
    assert "not inside a git repository" in capsys.readouterr().err


def test_status_reads_the_issue_branch_without_checking_it_out(repo, orchestrator, monkeypatch,
                                                               capsys):
    from orch import shell

    monkeypatch.setattr(runners, "repo_root", lambda: orchestrator)
    monkeypatch.chdir(repo)
    scratch = repo / ".scratch" / "17"
    write_contract(scratch)
    shell.git("checkout", "-q", "-b", "issue-17/topic", cwd=repo)
    (repo / "topic.txt").write_text("x\n")
    shell.git("add", "topic.txt", cwd=repo)
    shell.git("commit", "-q", "-m", "topic", cwd=repo)
    topic_sha = shell.head_sha(repo)
    shell.git("checkout", "-q", "main", cwd=repo)
    write_run(scratch, branch="issue-17/topic", pr_number=7)

    assert main(["status", "17"]) == 0

    out = capsys.readouterr().out
    assert f"head     {topic_sha}" in out
    assert "branch   issue-17/topic" in out
    assert shell.current_branch(repo) == "main"
