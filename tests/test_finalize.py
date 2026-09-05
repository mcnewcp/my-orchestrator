"""summary.md rendering and the two gh calls that publish it."""

from __future__ import annotations

import subprocess

import pytest

from conftest import blocking, write_contract, write_ledger, write_run
from orch import artifacts, shell
from orch.finalize import FinalizeError, finalize, render_summary
from orch.shell import ShellError


def test_render_summary_includes_contract_ledger_and_followups(scratch):
    write_contract(scratch)
    write_ledger(scratch, findings=[
        blocking("F-1-1", resolved=True),
        {"id": "F-1-2", "class": "style", "disposition": "deferred", "resolved": False,
         "followup_issue": "https://github.com/o/r/issues/44"},
    ])

    text = render_summary(17, artifacts.read_contract(scratch), artifacts.read_ledger(scratch))

    assert text.startswith("Closes #17\n")  # merging the PR must close the issue
    assert "# Add percent_change helper (#17)" in text
    assert "Adds a percent_change helper." in text
    assert "Verified by: `test_pct_basic`" in text
    assert "| F-1-1 | correctness | blocking | yes | — |" in text
    assert "| F-1-2 | style | deferred | no | https://github.com/o/r/issues/44 |" in text
    assert text.rstrip().endswith("_Assembled by `orch` from `.scratch/17/contract.md` "
                                  "and `ledger.json`._")


def test_render_summary_without_a_ledger(scratch):
    write_contract(scratch)
    text = render_summary(17, artifacts.read_contract(scratch), None)
    assert "_No review findings were recorded._" in text
    assert "## Follow-ups filed\nNone." in text


def test_finalize_writes_summary_and_publishes_it(scratch, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(shell, "gh", lambda *a, cwd, check=True: (
        calls.append(a), subprocess.CompletedProcess(a, 0, "", ""))[1])
    write_contract(scratch)
    write_run(scratch, pr_number=7)

    path = finalize(17, tmp_path, scratch)

    assert path == scratch / "summary.md" and path.exists()
    assert calls == [("pr", "edit", "7", "--body-file", str(path)), ("pr", "ready", "7")]


def test_finalize_removes_summary_when_github_rejects_it(scratch, tmp_path, monkeypatch):
    def boom(*args, cwd, check=True):
        raise ShellError("gh pr edit failed")

    monkeypatch.setattr(shell, "gh", boom)
    write_contract(scratch)
    write_run(scratch, pr_number=7)

    with pytest.raises(ShellError):
        finalize(17, tmp_path, scratch)
    assert not (scratch / "summary.md").exists()


def test_finalize_needs_a_pr_number(scratch, tmp_path):
    write_contract(scratch)
    write_run(scratch, pr_number=None)
    with pytest.raises(FinalizeError, match="no pr_number"):
        finalize(17, tmp_path, scratch)
