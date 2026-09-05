"""Fixtures: tmp scratch directories, tiny git repos, and a fake runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orch.config import Config

HEAD = "a" * 40
OLD = "b" * 40


# ------------------------------------------------------------------ artifact writers


def write_contract(scratch: Path, *, issue: int = 17, title: str = "Add percent_change helper",
                   test_budget: int = 12, commands: str | None = None) -> Path:
    """Write a well-formed contract.md into the scratch directory."""
    commands = commands or '  test: "uv run pytest -q"\n  lint: "uv run ruff check ."'
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / "contract.md"
    path.write_text(
        "---\n"
        f"issue: {issue}\n"
        f'title: "{title}"\n'
        f"test_budget: {test_budget}\n"
        'scope_paths: ["src/**", "tests/**"]      # glob patterns relative to repo root\n'
        "commands:                                # omit keys the repo lacks\n"
        f"{commands}\n"
        "---\n"
        "## Summary\nAdds a percent_change helper.\n\n"
        "## Acceptance Criteria\n"
        "- **AC-1** — returns the signed percent change. Verified by: `test_pct_basic`\n\n"
        "## Test Plan\nUnit tests.\n\n"
        "## Non-Goals\nNo formatting changes.\n",
        encoding="utf-8",
    )
    return path


def write_run(scratch: Path, *, issue: int = 17, branch: str | None = "issue-17/x",
              pr_number: int | None = 7) -> Path:
    """Write run.json into the scratch directory."""
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / "run.json"
    path.write_text(
        json.dumps(
            {
                "issue": issue,
                "branch": branch,
                "pr_number": pr_number,
                "pr_url": None if pr_number is None else f"https://github.com/o/r/pull/{pr_number}",
                "created_at": "2026-09-03T00:00:00Z",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def write_audit(scratch: Path, n: int, *, passed: bool, commit: str = HEAD,
                failures: list[str] | None = None) -> Path:
    """Write an audit-<n>.json into the scratch directory."""
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / f"audit-{n}.json"
    path.write_text(
        json.dumps(
            {
                "pass": passed,
                "commit": commit,
                "checks": {
                    "commands": {"test": "pass" if passed else "fail"},
                    "criteria_coverage": [{"id": "AC-1", "tests": ["test_pct_basic"],
                                           "covered": True}],
                    "scope": {"pass": True, "out_of_scope_files": []},
                    "test_budget": {"budget": 12, "added": 2, "pass": True},
                },
                "failures": failures or ([] if passed else ["test failed: test_pct_basic"]),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def write_review(scratch: Path, n: int, *, verdict: str, commit: str = HEAD) -> Path:
    """Write a review-<n>.md into the scratch directory."""
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / f"review-{n}.md"
    findings = (
        ""
        if verdict == "APPROVE"
        else f"### F-{n}-1\n- class: correctness\n- location: src/x.py:4\n"
        "- evidence: repro in the review\n- statement: off-by-one.\n"
    )
    path.write_text(
        f"---\nverdict: {verdict}        # or APPROVE\ncommit: {commit}\n"
        f"round: {n}\nbase: pr\n---\n## Findings\n{findings}",
        encoding="utf-8",
    )
    return path


def write_ledger(scratch: Path, *, rounds_completed: int = 1, findings: list[dict] | None = None) -> Path:
    """Write ledger.json into the scratch directory."""
    scratch.mkdir(parents=True, exist_ok=True)
    path = scratch / "ledger.json"
    path.write_text(
        json.dumps({"rounds_completed": rounds_completed, "findings": findings or []}, indent=2),
        encoding="utf-8",
    )
    return path


def blocking(fid: str = "F-1-1", *, resolved: bool = False) -> dict:
    """A ledger finding with disposition 'blocking'."""
    return {
        "id": fid, "round": 1, "class": "correctness", "location": "src/x.py:4",
        "summary": "off-by-one", "disposition": "blocking", "rationale": "violates AC-1",
        "followup_issue": None, "resolved": resolved, "resolved_commit": None,
    }


# ---------------------------------------------------------------------- fixtures


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """An empty scratch directory for issue 17."""
    path = tmp_path / ".scratch" / "17"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with one commit on branch `main`."""
    root = tmp_path / "target"
    root.mkdir()
    run = lambda *args: subprocess.run(args, cwd=root, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "Test")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "init")
    return root


def commit(repo: Path, message: str = "work") -> str:
    """Make an empty commit in `repo` and return the new HEAD sha."""
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", message], cwd=repo,
                   check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def head_of(repo: Path) -> str:
    """HEAD sha of `repo`."""
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


class FakeRunner:
    """Runner stub: records invocations and lets each role mutate the scratch dir."""

    def __init__(self, handlers: dict | None = None):
        self.handlers = handlers or {}
        self.calls: list[str] = []
        self.last_log_path = Path("logs/fake.jsonl")

    def run(self, role: str, issue: int, cwd: Path) -> int:
        """Record the call and run this role's handler, if any."""
        self.calls.append(role)
        handler = self.handlers.get(role)
        if handler is not None:
            handler()
        return 0


@pytest.fixture
def config() -> Config:
    """Default configuration."""
    return Config()


@pytest.fixture(autouse=True)
def no_real_harness(monkeypatch):
    """Hermeticity guard: no test may launch a real claude/codex process."""
    from orch import runners

    def forbidden(*args, **kwargs):
        raise AssertionError(f"a test tried to launch a harness: {args[0]}")

    monkeypatch.setattr(runners, "Popen", forbidden)
