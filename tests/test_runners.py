"""Runner command lines, stream parsing, and skill linking — no harness is ever launched."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from orch import SKILL_NAMES, runners
from orch.config import Config
from orch.runners import ClaudeRunner, CodexRunner, SetupError, get_runner, link_skills


class FakePopen:
    """Stands in for subprocess.Popen: records the call, replays canned stdout."""

    calls: list[dict] = []

    def __init__(self, argv, cwd=None, stdin=None, stdout=None, stderr=None, text=None,
                 bufsize=None):
        FakePopen.calls.append({"argv": argv, "cwd": cwd, "stdin": stdin})
        self.stdout = io.StringIO(self.lines)
        if "-o" in argv:  # codex writes its final message to the -o path
            out = Path(cwd) / argv[argv.index("-o") + 1]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("all done\n", encoding="utf-8")

    def wait(self):
        return 0


@pytest.fixture
def popen(monkeypatch):
    """Install FakePopen and return it (set `.lines` to the stdout to replay)."""
    FakePopen.calls = []
    FakePopen.lines = ""
    monkeypatch.setattr(runners, "Popen", FakePopen)
    return FakePopen


def test_claude_command_line(popen, repo):
    popen.lines = json.dumps(
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "opus",
         "skills": ["orch-audit"]}
    ) + "\n" + json.dumps(
        {"type": "result", "is_error": False, "subtype": "success", "result": "wrote audit-1"}
    ) + "\n"

    code = ClaudeRunner({"model": "opus", "extra_args": []}).run("auditor", 17, repo)

    assert code == 0
    assert popen.calls[0]["argv"] == [
        "claude", "-p", "/orch-audit 17",
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "bypassPermissions", "--dangerously-skip-permissions",
        "--setting-sources", "project",
        "--model", "opus",
    ]
    assert popen.calls[0]["cwd"] == str(repo)
    assert popen.calls[0]["stdin"] is subprocess.DEVNULL
    log = repo / ".scratch/17/logs/001-auditor.jsonl"
    assert log.exists() and "orch-audit" in log.read_text()
    assert (repo / ".scratch/17/logs/001-auditor.err").exists()


def test_claude_omits_the_model_when_blank_and_appends_extra_args(popen, repo):
    ClaudeRunner({"model": "", "extra_args": ["--debug"]}).run("auditor", 17, repo)
    argv = popen.calls[0]["argv"]
    assert "--model" not in argv
    assert argv[-1] == "--debug"


def test_claude_warns_when_the_role_skill_is_absent_and_when_no_result_arrives(popen, repo, capsys):
    popen.lines = json.dumps(
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "opus",
         "skills": [{"name": "something-else"}]}
    ) + "\n"
    ClaudeRunner().run("auditor", 17, repo)
    err = capsys.readouterr().err
    assert "orch-audit' is not in the session's skills" in err
    assert "died mid-run" in err


def test_codex_command_line_and_final_message(popen, repo, capsys):
    popen.lines = json.dumps(
        {"type": "item.completed",
         "item": {"item_type": "command_execution", "command": "gh pr diff 7"}}
    ) + "\n"

    CodexRunner({"model": "", "sandbox": "danger-full-access"}).run("reviewer", 17, repo)

    assert popen.calls[0]["argv"] == [
        "codex", "exec", "--json", "--sandbox", "danger-full-access",
        "-o", ".scratch/17/logs/001-reviewer.last.md",
        "$orch-review 17",
    ]
    err = capsys.readouterr().err
    assert "$ gh pr diff 7" in err
    assert "final message :: all done" in err


def test_codex_includes_the_model_when_configured(popen, repo):
    CodexRunner({"model": "gpt-5.6-sol"}).run("judge", 17, repo)
    argv = popen.calls[0]["argv"]
    assert argv[-3:] == ["--model", "gpt-5.6-sol", "$orch-judge 17"]


def test_log_sequence_increments_across_roles(popen, repo):
    ClaudeRunner().run("contractor", 17, repo)
    CodexRunner().run("reviewer", 17, repo)
    ClaudeRunner().run("remediator", 17, repo)
    names = sorted(p.name for p in (repo / ".scratch/17/logs").glob("*.jsonl"))
    assert names == ["001-contractor.jsonl", "002-reviewer.jsonl", "003-remediator.jsonl"]


def test_get_runner_maps_roles_to_runners():
    config = Config()
    assert isinstance(get_runner("auditor", config), ClaudeRunner)
    assert isinstance(get_runner("judge", config), CodexRunner)


# ------------------------------------------------------------------ skill linking


@pytest.fixture
def orchestrator(tmp_path: Path) -> Path:
    """A fake orchestrator repo carrying the six role skills."""
    root = tmp_path / "orchestrator"
    for name in SKILL_NAMES.values():
        skill = root / ".agents" / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return root


def test_link_skills_creates_both_trees_and_excludes_them(repo, orchestrator):
    created = link_skills(repo, orchestrator)

    assert len(created) == 12
    for name in SKILL_NAMES.values():
        for root in (".agents/skills", ".claude/skills"):
            link = repo / root / name
            assert link.is_symlink()
            assert link.resolve() == (orchestrator / ".agents" / "skills" / name).resolve()
    exclude = (repo / ".git/info/exclude").read_text()
    assert ".claude/skills/orch-audit" in exclude
    assert exclude.count(".agents/skills/orch-audit") == 1

    assert link_skills(repo, orchestrator) == []  # idempotent
    assert (repo / ".git/info/exclude").read_text().count(".agents/skills/orch-audit") == 1


def test_link_skills_reports_a_missing_source_directory(repo, tmp_path):
    empty = tmp_path / "no-skills"
    empty.mkdir()
    with pytest.raises(SetupError, match="role skills are missing"):
        link_skills(repo, empty)


def test_link_skills_refuses_to_clobber_an_existing_path(repo, orchestrator):
    (repo / ".claude/skills/orch-audit").mkdir(parents=True)
    with pytest.raises(SetupError, match="already exists and is not a symlink"):
        link_skills(repo, orchestrator)


def test_link_skills_accepts_an_existing_relative_symlink(repo, orchestrator, monkeypatch):
    link = repo / ".agents/skills/orch-audit"
    source = orchestrator / ".agents" / "skills" / "orch-audit"
    link.parent.mkdir(parents=True)
    link.symlink_to(os.path.relpath(source, link.parent))
    monkeypatch.chdir(repo)  # a relative target must resolve against the link, not the cwd

    created = link_skills(repo, orchestrator)

    assert link not in created and len(created) == 11
    assert link.is_symlink() and link.resolve() == source.resolve()


def test_link_skills_reports_a_link_parent_that_is_not_a_directory(repo, orchestrator):
    (repo / ".claude").mkdir()
    (repo / ".claude/skills").write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(SetupError, match="is not a directory and cannot be created"):
        link_skills(repo, orchestrator)


def test_link_skills_skips_the_orchestrator_itself(orchestrator):
    assert link_skills(orchestrator, orchestrator) == []


def test_ensure_target_setup_adds_scratch_to_gitignore(repo, orchestrator):
    (repo / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    runners.ensure_target_setup(repo, orchestrator)
    assert (repo / ".gitignore").read_text() == "node_modules\n.scratch/\n"
    runners.ensure_target_setup(repo, orchestrator)
    assert (repo / ".gitignore").read_text().count(".scratch/") == 1
