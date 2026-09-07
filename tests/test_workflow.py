"""Workflow acceptance against real Git and Postgres, with bounded fake external APIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from factory.agents import ImplementOutput, PrepareOutput, ReviewOutput
from factory.config import Check, Config
from factory.errors import Blocked, Interrupted
from factory.git_ops import GitOps
from factory.process import ProcessRunner
from factory.state import State
from factory.worker import run_worker
from factory.workflow import Workflow, approved_hashes

pytestmark = pytest.mark.postgres


def git(path, *args):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class LocalGit(GitOps):
    def validate(self, base_branch, repo=None):
        # The test transport is a local bare remote. Production GitHub remote identity
        # validation is separately covered by GitOps tests; every Git operation is real.
        super().validate(base_branch)


class CheckCounter(ProcessRunner):
    def __init__(self):
        super().__init__()
        self.check_count = 0

    def run(self, argv, **kwargs):
        if argv[-1] == "check.py":
            self.check_count += 1
        return super().run(argv, **kwargs)


class FakeAgents:
    def __init__(self):
        self.calls = []
        self.value = "2\n"
        self.hook = None

    def run(self, role, engine, auth, cwd, evidence_dir, prompt, timeout):
        self.calls.append(role)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "prompt.txt").write_text(prompt)
        if self.hook:
            self.hook(role, cwd)
        if role == "prepare":
            return PrepareOutput(
                spec="# Specification\nSet value.txt to 2; existing checks must still pass.",
                plan="# Plan\nChange value.txt and run the configured positive-value check.",
                unresolved_decisions=[],
            )
        if role == "implement":
            (cwd / "value.txt").write_text(self.value)
            return ImplementOutput(
                summary="Updated the value", unresolved_decisions=[], plan_deviations=[]
            )
        return ReviewOutput(
            decision="accept", summary="Acceptance verified at candidate", findings=[]
        )


class FakeGitHub:
    def __init__(self, remote):
        self.remote = remote
        self.current = None
        self.created = 0
        self.issue_reads = 0
        self.ci_success = True
        self.after_create = None

    def issue(self, number):
        self.issue_reads += 1
        return {
            "number": number,
            "title": "Set the example value to two",
            "body": "Set value.txt to 2.",
            "url": f"https://github.com/owner/repo/issues/{number}",
            "retrieved_at": "2026-09-06T00:00:00+00:00",
        }

    def find_pr(self, branch):
        if self.current and self.current["headRefName"] == branch:
            return dict(self.current)
        return None

    def create_draft(self, branch, base, title, body, *, run_id=None):
        if not self.current:
            self.created += 1
            self.current = {
                "number": 1,
                "url": "https://github.com/owner/repo/pull/1",
                "title": title,
                "body": body,
                "state": "OPEN",
                "isDraft": True,
                "headRefName": branch,
                "baseRefName": base,
                "headRefOid": git(self.remote, "rev-parse", f"refs/heads/{branch}"),
            }
            if self.after_create:
                self.after_create()
        return dict(self.current)

    def pr(self, number):
        assert self.current and number == self.current["number"]
        return dict(self.current)

    def wait_ci(self, sha, required, timeout, poll_seconds=5):
        if not self.ci_success:
            raise Blocked(f"Required CI timed out for {sha}: unit=missing")
        assert sha == self.current["headRefOid"]
        return [{"name": name, "sha": sha, "state": "success"} for name in required]

    def ready(self, number, *, expected_sha):
        assert number == self.current["number"]
        assert expected_sha == self.current["headRefOid"]
        self.current["isDraft"] = False
        return dict(self.current)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    dsn = os.environ.get("FACTORY_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set FACTORY_TEST_DATABASE_URL for workflow integration tests")
    schema = f"workflow_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    state = State(make_conninfo(dsn, options=f"-csearch_path={schema}"))
    state.migrate()
    monkeypatch.setenv("FACTORY_IMAGE_ID", "dev:workflow-test")
    for key in list(os.environ):
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "OPENAI_", "AZURE_OPENAI_")):
            monkeypatch.delenv(key)
    workspace = tmp_path / "factory"
    checkout = workspace / "checkout"
    checkout.mkdir(parents=True)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(checkout, "init", "-b", "main")
    for filename in ("AGENTS.md", "CLAUDE.md", "REVIEW.md"):
        (checkout / filename).write_text("Trusted repository instructions.\n")
    (checkout / "value.txt").write_text("1\n")
    (checkout / "check.py").write_text(
        "from pathlib import Path\nassert int(Path('value.txt').read_text()) > 0\n"
    )
    git(checkout, "add", ".")
    git(
        checkout,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Initial target",
    )
    git(checkout, "remote", "add", "origin", str(remote))
    git(checkout, "push", "-u", "origin", "main")
    config = Config(
        repo="owner/repo",
        checkout=checkout,
        workspace_root=workspace,
        checks=[Check(name="positive", command=[sys.executable, "check.py"], timeout=10)],
        required_ci=["unit"],
        protected_paths=["check.py"],
        ci_timeout=1,
    )
    runner, agents, github = CheckCounter(), FakeAgents(), FakeGitHub(remote)
    workflow = Workflow(
        state,
        config,
        runner=runner,
        agents=agents,
        git=LocalGit(checkout, runner=runner),
        github=github,
    )
    try:
        yield SimpleNamespace(
            state=state,
            workflow=workflow,
            config=config,
            runner=runner,
            agents=agents,
            github=github,
            remote=remote,
        )
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def prepared(rig, engine="codex"):
    run = rig.state.submit_prepare(
        "owner/repo",
        42,
        engine,
        "subscription",
        "dev:workflow-test",
        rig.config.workspace_root,
    )
    run_worker(rig.state, rig.workflow, once=True)
    result = rig.state.get(run["id"])
    assert result["state"] == "awaiting_approval", result["blocked_reason"]
    return result


def approve(rig, run):
    return rig.state.approve(
        run["id"],
        {
            "hashes": approved_hashes(Path(run["workspace"]), 42),
            "operator": "test-owner",
            "approved_at": datetime.now(UTC).isoformat(),
        },
    )


def execute(rig, run):
    run_worker(rig.state, rig.workflow, once=True)
    return rig.state.get(run["id"])


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_happy_path_requires_approval_and_publishes_exact_reviewed_candidate(rig, engine):
    run = prepared(rig, engine)
    assert rig.agents.calls == ["prepare"]
    assert rig.github.current is None
    approve(rig, run)
    result = execute(rig, run)
    assert result["state"] == "ready", result["blocked_reason"]
    assert result["attempt_count"] == 1
    assert result["candidate_sha"] == rig.github.current["headRefOid"]
    assert rig.state.attempts(run["id"])[0]["review"]["candidate_sha"] == result["candidate_sha"]
    assert rig.github.created == 1 and not rig.github.current["isDraft"]
    assert rig.github.issue_reads == 1
    assert rig.runner.check_count == 2
    assert "Closes #42" in rig.github.current["body"]
    assert git(Path(result["workspace"]), "status", "--porcelain") == ""


def test_failed_checks_never_publish_and_exhaust_three_attempts(rig):
    run = prepared(rig)
    rig.agents.value = "-1\n"
    approve(rig, run)
    result = execute(rig, run)
    assert result["state"] == "blocked"
    assert result["attempt_count"] == 3
    assert "attempt" in result["blocked_reason"]
    assert rig.github.created == 0
    assert rig.runner.check_count == 4
    rig.state.resume(run["id"])
    resumed = execute(rig, run)
    assert resumed["attempt_count"] == 3
    assert rig.agents.calls.count("implement") == 3


def test_protected_edits_block_before_candidate_checks(rig):
    run = prepared(rig)

    def weaken_check(role, cwd):
        if role == "implement":
            (cwd / "check.py").write_text("pass\n")

    rig.agents.hook = weaken_check
    approve(rig, run)
    result = execute(rig, run)
    assert result["state"] == "blocked"
    assert "protected_paths_changed" in result["blocked_reason"]
    assert rig.runner.check_count == 1
    assert rig.agents.calls.count("review") == 0
    assert rig.github.created == 0


def test_changed_queued_approval_blocks_before_implementation(rig):
    run = prepared(rig)
    approve(rig, run)
    spec = Path(run["workspace"]) / "work/42/spec.md"
    spec.write_text(spec.read_text().replace("Set value.txt to 2", "Set value.txt to 99"))
    result = execute(rig, run)
    assert result["state"] == "blocked"
    assert "approval_changed" in result["blocked_reason"]
    assert result["attempt_count"] == 0
    assert rig.github.created == 0


def test_review_cannot_change_candidate_and_publish_stale_evidence(rig):
    run = prepared(rig)

    def mutate_reviewed_code(role, cwd):
        if role == "review":
            (cwd / "value.txt").write_text("3\n")

    rig.agents.hook = mutate_reviewed_code
    approve(rig, run)
    result = execute(rig, run)
    assert result["state"] == "blocked"
    assert "candidate_changed" in result["blocked_reason"]
    assert rig.github.created == 0


def test_blocked_review_resume_repeats_review_without_another_implementation(rig, monkeypatch):
    run = prepared(rig)
    original = rig.agents.run

    def blocked_review(role, *args, **kwargs):
        if role == "review":
            rig.agents.calls.append(role)
            return ReviewOutput(
                decision="blocked", summary="Evidence service unavailable", findings=[]
            )
        return original(role, *args, **kwargs)

    monkeypatch.setattr(rig.agents, "run", blocked_review)
    approve(rig, run)
    result = execute(rig, run)
    assert result["state"] == "blocked"
    assert "review_blocked" in result["blocked_reason"]
    assert rig.github.created == 0
    candidate = result["candidate_sha"]
    monkeypatch.setattr(rig.agents, "run", original)
    rig.state.resume(run["id"])
    resumed = execute(rig, run)
    assert resumed["state"] == "ready", resumed["blocked_reason"]
    assert resumed["candidate_sha"] == candidate
    assert resumed["attempt_count"] == 1
    assert rig.agents.calls.count("implement") == 1
    assert rig.agents.calls.count("review") == 2


def test_stored_review_for_different_sha_cannot_publish(rig, monkeypatch):
    run = prepared(rig)
    publish = rig.workflow._publish

    def stale_review(current):
        attempt = rig.state.attempts(current["id"])[-1]
        rig.state.finish_attempt(
            current["id"],
            attempt["number"],
            review={**attempt["review"], "candidate_sha": "0" * 40},
        )
        publish(current)

    monkeypatch.setattr(rig.workflow, "_publish", stale_review)
    approve(rig, run)
    result = execute(rig, run)
    assert result["state"] == "blocked"
    assert "stale_or_failed_evidence" in result["blocked_reason"]
    assert rig.github.created == 0


def test_missing_ci_leaves_draft_and_resume_reuses_accepted_candidate(rig):
    run = prepared(rig)
    rig.github.ci_success = False
    approve(rig, run)
    result = execute(rig, run)
    assert result["state"] == "blocked"
    assert "missing" in result["blocked_reason"]
    assert rig.github.current["isDraft"]
    candidate = result["candidate_sha"]
    rig.github.ci_success = True
    rig.state.resume(run["id"])
    resumed = execute(rig, run)
    assert resumed["state"] == "ready", resumed["blocked_reason"]
    assert resumed["candidate_sha"] == candidate and resumed["attempt_count"] == 1
    assert rig.github.created == 1
    assert rig.runner.check_count == 2


def test_interruption_after_pr_creation_adopts_existing_pr_on_resume(rig):
    run = prepared(rig)

    def interrupted_after_creation():
        raise Interrupted("Simulated process stop after GitHub created PR")

    rig.github.after_create = interrupted_after_creation
    approve(rig, run)
    with pytest.raises(Interrupted):
        execute(rig, run)
    result = rig.state.get(run["id"])
    assert result["state"] == "blocked" and not result["pr"]
    assert rig.github.created == 1
    rig.state.resume(run["id"])
    resumed = execute(rig, run)
    assert resumed["state"] == "ready", resumed["blocked_reason"]
    assert rig.github.created == 1 and resumed["attempt_count"] == 1


def test_restart_mid_implementation_preserves_partial_changes_and_attempt_limit(rig):
    run = prepared(rig)

    def interrupted_implementation(role, cwd):
        if role == "implement":
            (cwd / "value.txt").write_text("unfinished partial implementation\n")
            raise Interrupted("Simulated killed controller and agent")

    rig.agents.hook = interrupted_implementation
    approve(rig, run)
    # Bypass the worker exception handler to model abrupt process death before it
    # could mark the record blocked. A newly started worker must reconcile it.
    with pytest.raises(Interrupted):
        rig.workflow.process(rig.state.claim())
    assert rig.state.get(run["id"])["state"] == "running"
    run_worker(rig.state, rig.workflow, once=True)
    blocked = rig.state.get(run["id"])
    assert blocked["blocked_reason"] == "interrupted" and blocked["attempt_count"] == 1
    rig.agents.hook = None
    rig.state.resume(run["id"])
    resumed = execute(rig, run)
    assert resumed["state"] == "ready", resumed["blocked_reason"]
    assert resumed["attempt_count"] == 2
    archive = Path(resumed["metadata"]["recovery_archive"])
    assert archive.exists()
    assert rig.state.attempts(run["id"])[0]["status"] == "interrupted"
    assert rig.github.created == 1


def test_controller_candidate_commit_is_adopted_without_spending_another_attempt(rig, monkeypatch):
    run = prepared(rig)
    original = rig.state.finish_attempt

    def crash_before_recording_candidate(run_id, number, **fields):
        if fields.get("candidate_sha"):
            raise Interrupted("Stopped after candidate commit, before candidate DB records")
        return original(run_id, number, **fields)

    monkeypatch.setattr(rig.state, "finish_attempt", crash_before_recording_candidate)
    approve(rig, run)
    with pytest.raises(Interrupted):
        rig.workflow.process(rig.state.claim())
    candidate = git(Path(run["workspace"]), "rev-parse", "HEAD")
    assert rig.state.get(run["id"])["candidate_sha"] is None
    monkeypatch.setattr(rig.state, "finish_attempt", original)
    run_worker(rig.state, rig.workflow, once=True)
    rig.state.resume(run["id"])
    resumed = execute(rig, run)
    assert resumed["state"] == "ready", resumed["blocked_reason"]
    assert resumed["candidate_sha"] == candidate and resumed["attempt_count"] == 1
    assert rig.agents.calls.count("implement") == 1


def test_restart_during_candidate_adoption_cannot_lose_attempt_candidate(rig, monkeypatch):
    run = prepared(rig)
    original = rig.state.finish_attempt

    def crash_before_recording_candidate(run_id, number, **fields):
        if fields.get("candidate_sha"):
            raise Interrupted("Stopped before attempt candidate write")
        return original(run_id, number, **fields)

    monkeypatch.setattr(rig.state, "finish_attempt", crash_before_recording_candidate)
    approve(rig, run)
    with pytest.raises(Interrupted):
        rig.workflow.process(rig.state.claim())
    run_worker(rig.state, rig.workflow, once=True)
    rig.state.resume(run["id"])
    with pytest.raises(Interrupted):
        # A second process death occurs while adopting the previously committed candidate.
        rig.workflow.process(rig.state.claim())
    monkeypatch.setattr(rig.state, "finish_attempt", original)
    run_worker(rig.state, rig.workflow, once=True)
    rig.state.resume(run["id"])
    resumed = execute(rig, run)
    assert resumed["state"] == "ready", resumed["blocked_reason"]
    assert resumed["attempt_count"] == 1
    assert rig.state.attempts(run["id"])[0]["candidate_sha"] == resumed["candidate_sha"]


def test_unexpected_commit_during_interrupted_implementation_blocks_recovery(rig):
    run = prepared(rig)

    def stop_implementation(role, cwd):
        if role == "implement":
            raise Interrupted("Stopped during implementation")

    rig.agents.hook = stop_implementation
    approve(rig, run)
    with pytest.raises(Interrupted):
        rig.workflow.process(rig.state.claim())
    work = Path(run["workspace"])
    (work / "value.txt").write_text("99\n")
    git(work, "add", "value.txt")
    git(
        work,
        "-c",
        "user.name=Operator",
        "-c",
        "user.email=operator@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Unrecorded outside commit",
    )
    run_worker(rig.state, rig.workflow, once=True)
    rig.state.resume(run["id"])
    result = execute(rig, run)
    assert result["state"] == "blocked"
    assert "unexpected_commit" in result["blocked_reason"]
    assert rig.github.created == 0


def test_frozen_input_file_edit_invalidates_queued_approval(rig):
    run = prepared(rig)
    approve(rig, run)
    path = Path(run["artifacts"]) / "inputs.json"
    inputs = json.loads(path.read_text())
    inputs["config"]["protected_paths"] = ["irrelevant"]
    path.write_text(json.dumps(inputs))
    result = execute(rig, run)
    assert result["state"] == "blocked"
    assert "frozen_inputs_changed" in result["blocked_reason"]
    assert result["attempt_count"] == 0


def test_protected_ignored_file_creation_blocks_before_checks(rig):
    checkout = rig.config.checkout
    (checkout / ".gitignore").write_text("hidden-policy.toml\n")
    git(checkout, "add", ".gitignore")
    git(
        checkout,
        "-c",
        "user.name=Owner",
        "-c",
        "user.email=owner@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Ignore local check policy",
    )
    git(checkout, "push", "origin", "main")
    rig.config.protected_paths.append("hidden-policy.toml")
    run = prepared(rig)

    def add_ignored_policy(role, cwd):
        if role == "implement":
            (cwd / "hidden-policy.toml").write_text("skip_checks = true\n")

    rig.agents.hook = add_ignored_policy
    approve(rig, run)
    result = execute(rig, run)
    assert result["state"] == "blocked", "Ignored protected content must not bypass policy"
    assert rig.runner.check_count == 1
    assert rig.github.created == 0


def ignore_preparation_file(rig, *, protected=False):
    checkout = rig.config.checkout
    (checkout / ".gitignore").write_text("partial.ignored\n")
    git(checkout, "add", ".gitignore")
    git(
        checkout,
        "-c",
        "user.name=Owner",
        "-c",
        "user.email=owner@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Ignore preparation output",
    )
    git(checkout, "push", "origin", "main")
    if protected:
        rig.config.protected_paths.append("partial.ignored")
        # A broad output allowance must never override a protected path.
        rig.config.allowed_untracked.append("*.ignored")


@pytest.mark.parametrize("protected", [False, True])
def test_interrupted_preparation_archives_ignored_files_before_retry(rig, protected):
    ignore_preparation_file(rig, protected=protected)
    run = rig.state.submit_prepare(
        "owner/repo", 42, "codex", "subscription", "dev:workflow-test", rig.config.workspace_root
    )

    def interrupt_prepare(role, cwd):
        if role == "prepare":
            (cwd / "partial.ignored").write_text("unfinished preparation\n")
            raise Interrupted("Stopped with ignored preparation output")

    rig.agents.hook = interrupt_prepare
    with pytest.raises(Interrupted):
        rig.workflow.process(rig.state.claim())
    run_worker(rig.state, rig.workflow, once=True)
    rig.agents.hook = None
    rig.state.resume(run["id"])
    resumed = execute(rig, run)
    assert resumed["state"] == "awaiting_approval", resumed["blocked_reason"]
    assert resumed["attempt_count"] == 0
    archive = Path(resumed["metadata"]["recovery_archive"])
    assert (archive / "files/partial.ignored").read_text() == "unfinished preparation\n"
    assert not (Path(run["workspace"]) / "partial.ignored").exists()
    assert rig.github.issue_reads == 1


@pytest.mark.parametrize("protected", [False, True])
def test_preparation_adoption_rejects_later_ignored_edits(rig, monkeypatch, protected):
    ignore_preparation_file(rig, protected=protected)
    original = rig.state.update

    def crash_after_commit(run_id, event, **fields):
        if event == "prepared":
            raise Interrupted("Stopped after preparation commit before database update")
        return original(run_id, event, **fields)

    monkeypatch.setattr(rig.state, "update", crash_after_commit)
    run = rig.state.submit_prepare(
        "owner/repo", 42, "codex", "subscription", "dev:workflow-test", rig.config.workspace_root
    )
    with pytest.raises(Interrupted):
        rig.workflow.process(rig.state.claim())
    monkeypatch.setattr(rig.state, "update", original)
    work = Path(run["workspace"])
    checkpoint = git(work, "rev-parse", "HEAD")
    (work / "partial.ignored").write_text("outside checkpoint\n")
    run_worker(rig.state, rig.workflow, once=True)
    rig.state.resume(run["id"])
    result = execute(rig, run)
    assert result["state"] == "blocked", "Unrecorded ignored edits cannot be adopted"
    assert git(work, "rev-parse", "HEAD") == checkpoint
    assert (work / "partial.ignored").read_text() == "outside checkpoint\n"
    assert rig.agents.calls == ["prepare"]


def test_preparation_adoption_requires_recorded_controller_contents(rig):
    run = rig.state.submit_prepare(
        "owner/repo", 42, "codex", "subscription", "dev:workflow-test", rig.config.workspace_root
    )

    def interrupt_prepare(role, cwd):
        if role == "prepare":
            raise Interrupted("Stopped before generating preparation documents")

    rig.agents.hook = interrupt_prepare
    with pytest.raises(Interrupted):
        rig.workflow.process(rig.state.claim())
    work = Path(run["workspace"])
    (work / "value.txt").write_text("99\n")
    rig.workflow.git.commit(
        work, "Unrecorded change bearing controller trailers", run_id=run["id"], stage="prepare"
    )
    checkpoint = git(work, "rev-parse", "HEAD")
    run_worker(rig.state, rig.workflow, once=True)
    rig.state.resume(run["id"])
    result = execute(rig, run)
    assert result["state"] == "blocked", "Trailers alone cannot prove controller provenance"
    assert "unexpected_commit" in result["blocked_reason"]
    assert git(work, "rev-parse", "HEAD") == checkpoint
    assert (work / "value.txt").read_text() == "99\n"


@pytest.mark.parametrize("changed_path", [None, "work/42/spec.md", "value.txt"])
def test_preparation_adoption_verifies_exact_recorded_commit(rig, monkeypatch, changed_path):
    original = rig.state.update

    def crash_after_commit(run_id, event, **fields):
        if event == "prepared":
            raise Interrupted("Stopped after preparation commit before database update")
        return original(run_id, event, **fields)

    monkeypatch.setattr(rig.state, "update", crash_after_commit)
    run = rig.state.submit_prepare(
        "owner/repo", 42, "codex", "subscription", "dev:workflow-test", rig.config.workspace_root
    )
    with pytest.raises(Interrupted):
        rig.workflow.process(rig.state.claim())
    monkeypatch.setattr(rig.state, "update", original)
    work = Path(run["workspace"])
    if changed_path:
        (work / changed_path).write_text("Unrecorded replacement\n")
        git(work, "add", changed_path)
        git(
            work,
            "-c",
            "user.name=Operator",
            "-c",
            "user.email=operator@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--amend",
            "--no-edit",
        )
    checkpoint = git(work, "rev-parse", "HEAD")
    run_worker(rig.state, rig.workflow, once=True)
    rig.state.resume(run["id"])
    result = execute(rig, run)
    assert result["state"] == ("blocked" if changed_path else "awaiting_approval")
    assert git(work, "rev-parse", "HEAD") == checkpoint
    assert rig.agents.calls == ["prepare"]
    assert result["attempt_count"] == 0
    if changed_path:
        assert "unexpected_commit" in result["blocked_reason"]
    else:
        assert result["prepared_sha"] == checkpoint


def test_preparation_cannot_create_protected_ignored_files_allowed_as_output(rig):
    ignore_preparation_file(rig, protected=True)

    def write_protected_output(role, cwd):
        if role == "prepare":
            (cwd / "partial.ignored").write_text("unexpected policy\n")

    rig.agents.hook = write_protected_output
    run = rig.state.submit_prepare(
        "owner/repo", 42, "codex", "subscription", "dev:workflow-test", rig.config.workspace_root
    )
    result = execute(rig, run)
    assert result["state"] == "blocked"
    assert result["prepared_sha"] is None
    assert git(Path(run["workspace"]), "rev-parse", "HEAD") == result["base_sha"]
