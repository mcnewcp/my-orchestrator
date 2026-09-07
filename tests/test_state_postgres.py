"""Real PostgreSQL invariants, concurrency and restart behavior.

Set FACTORY_TEST_DATABASE_URL to an expendable database. Each test creates and
drops only its own unique schema, so other suites can share the database.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from factory.errors import FactoryError, Interrupted
from factory.state import State
from factory.worker import WorkerLease, run_worker

pytestmark = pytest.mark.postgres


@pytest.fixture
def state():
    dsn = os.environ.get("FACTORY_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set FACTORY_TEST_DATABASE_URL for real PostgreSQL integration tests")
    schema = f"state_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = State(make_conninfo(dsn, options=f"-csearch_path={schema}"))
    scoped.migrate()
    try:
        yield scoped
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def prepare(state, issue=42, **kwargs):
    return state.submit_prepare(
        "owner/repo",
        issue,
        "codex",
        "subscription",
        "image@sha256:abc",
        "/tmp/factory-test",
        **kwargs,
    )


def test_duplicate_preparation_is_atomic_under_concurrent_submissions(state):
    barrier = threading.Barrier(8)

    def submit(_):
        barrier.wait()
        return prepare(state)

    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(submit, range(8)))
    assert len({run["id"] for run in runs}) == 1
    assert len(state.list_runs()) == 1
    assert len(state.events(runs[0]["id"])) == 1


def test_repository_and_issue_reservations_have_different_lifetimes(state):
    run = prepare(state)
    with pytest.raises(FactoryError, match="Repository already reserved"):
        prepare(state, issue=43)
    state.update(run["id"], "ready", state="ready")
    assert prepare(state)["id"] == run["id"]
    next_run = prepare(state, issue=43)
    assert next_run["id"] != run["id"]
    state.update(next_run["id"], "closed", state="closed")
    state.update(run["id"], "closed", state="closed")
    assert prepare(state)["id"] != run["id"]


def test_reservation_constraint_applies_to_direct_writers(state):
    run = prepare(state)
    with pytest.raises(psycopg.errors.UniqueViolation), state.transaction() as connection:
        connection.execute(
            "INSERT INTO runs(id, repo, issue, branch, workspace, artifacts, state, next_stage, "
            "engine, auth, image) SELECT 'other', repo, issue + 1, 'other', workspace, artifacts, "
            "state, next_stage, engine, auth, image FROM runs WHERE id = %s",
            (run["id"],),
        )


def test_concurrent_claim_executes_once(state):
    prepare(state)
    barrier = threading.Barrier(6)

    def claim(_):
        barrier.wait()
        return state.claim()

    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(claim, range(6)))
    assert sum(run is not None for run in claims) == 1
    assert state.list_runs()[0]["state"] == "running"


def test_approval_is_frozen_and_repeat_does_not_queue_twice(state):
    run = prepare(state)
    with pytest.raises(FactoryError, match="awaiting_approval"):
        state.approve(run["id"], {"spec": "x"})
    state.update(run["id"], "prepared", state="awaiting_approval")
    approval = {
        "hashes": {"spec.md": "a" * 64, "plan.md": "b" * 64},
        "operator": "owner",
        "approved_at": "2026-09-06T00:00:00+00:00",
    }
    accepted = state.approve(run["id"], approval)
    assert accepted["next_stage"] == "approve"
    duplicate = state.approve(run["id"], {**approval, "approved_at": "later"})
    assert duplicate["approval"] == approval
    assert [event["event"] for event in state.events(run["id"])].count("approval_requested") == 1


def test_attempt_reservation_survives_restart_and_resume_keeps_budget(state):
    run = prepare(state)
    state.claim()
    state.update(run["id"], "approved", next_stage="implement", approval={"spec": "aaa"})
    first = state.reserve_attempt(run["id"])
    assert first["number"] == 1
    restarted = State(state.dsn)
    interrupted = restarted.reconcile_interrupted()
    assert interrupted[0]["blocked_reason"] == "interrupted"
    assert restarted.attempts(run["id"])[0]["status"] == "interrupted"
    resumed = restarted.resume(run["id"])
    assert resumed["approval"] == {"spec": "aaa"}
    assert resumed["attempt_count"] == 1
    assert restarted.resume(run["id"])["id"] == run["id"]
    restarted.claim()
    assert restarted.reserve_attempt(run["id"])["number"] == 2
    assert restarted.reserve_attempt(run["id"])["number"] == 3
    with pytest.raises(FactoryError, match="budget exhausted"):
        restarted.reserve_attempt(run["id"])
    assert restarted.get(run["id"])["attempt_count"] == 3


def test_attempt_results_and_events_are_committed_together(state):
    run = prepare(state)
    state.claim()
    attempt = state.reserve_attempt(run["id"])
    result = state.finish_attempt(
        run["id"],
        attempt["number"],
        status="accepted",
        stage="review",
        candidate_sha="abc",
        checks=[{"name": "unit", "exit_code": 0}],
        review={"decision": "accept"},
        evidence={"review": "/tmp/review.json"},
        finished_at=datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert result["finished_at"] is not None
    assert state.attempts(run["id"])[0]["review"]["decision"] == "accept"
    assert state.events(run["id"])[-1]["details"]["candidate_sha"] == "abc"
    assert state.events(run["id"])[-1]["details"]["finished_at"] == "2026-09-06T00:00:00+00:00"


def test_attempt_in_progress_remains_interruptible_through_checks_and_review(state):
    run = prepare(state)
    state.claim()
    attempt = state.reserve_attempt(run["id"])
    state.finish_attempt(run["id"], attempt["number"], stage="check", status="running")
    state.finish_attempt(run["id"], attempt["number"], stage="review", status="running")
    assert state.attempts(run["id"])[0]["finished_at"] is None
    state.reconcile_interrupted()
    result = state.attempts(run["id"])[0]
    assert result["status"] == "interrupted"
    assert result["finished_at"] is not None


def test_supersession_transfers_reservation_and_permanently_disables_resume(state):
    old = prepare(state)
    with pytest.raises(FactoryError, match="stopped"):
        prepare(state, supersede=old["id"])
    state.update(old["id"], "blocked", state="blocked", pr={"number": 7})
    replacement = prepare(state, supersede=old["id"])
    assert replacement["next_stage"] == "supersede"
    assert replacement["supersedes"] == old["id"]
    assert state.get(old["id"])["state"] == "blocked"
    assert prepare(state, supersede=old["id"])["id"] == replacement["id"]
    assert prepare(state)["id"] == replacement["id"]
    with pytest.raises(FactoryError, match="never resume"):
        state.resume(old["id"])


def test_migrations_are_idempotent_and_detect_changed_applied_migration(state):
    state.migrate()
    with state.transaction() as connection:
        connection.execute("UPDATE schema_migrations SET sha256 = 'tampered'")
    with pytest.raises(FactoryError, match="Applied migration changed"):
        state.migrate()


def test_second_worker_is_rejected(state):
    with WorkerLease(state):
        with pytest.raises(FactoryError, match="Another factory worker"), WorkerLease(state):
            pytest.fail("Duplicate worker acquired advisory lock")


def test_loss_of_lock_connection_interrupts_worker_guard(state):
    with WorkerLease(state, heartbeat_seconds=0.05) as lease:
        pid = lease.connection.info.backend_pid
        with state.transaction() as connection:
            connection.execute("SELECT pg_terminate_backend(%s)", (pid,))
        deadline = time.monotonic() + 3
        while not lease.lost.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        with pytest.raises(Interrupted, match="connection lost"):
            lease.check_alive()


def test_worker_blocks_failed_stage_and_preserves_attempt(state):
    run = prepare(state)

    def process(claimed):
        state.reserve_attempt(claimed["id"])
        raise FactoryError("invalid agent output")

    workflow = SimpleNamespace(runner=SimpleNamespace(guard=None), process=process)
    run_worker(state, workflow, once=True)
    result = state.get(run["id"])
    assert result["state"] == "blocked"
    assert result["blocked_reason"] == "invalid agent output"
    assert result["attempt_count"] == 1
    assert workflow.runner.guard is None


def test_worker_reconciles_running_records_before_claiming(state):
    run = prepare(state)
    state.claim()
    workflow = SimpleNamespace(
        runner=SimpleNamespace(guard=None),
        process=lambda _: pytest.fail("Interrupted stage must await explicit resume"),
    )
    run_worker(state, workflow, once=True)
    assert state.get(run["id"])["state"] == "blocked"
