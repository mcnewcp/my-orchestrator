"""One supervised worker, guarded by a lifetime PostgreSQL advisory connection."""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import psycopg

from factory.errors import FactoryError, Interrupted
from factory.state import WORKER_LOCK, State

log = logging.getLogger(__name__)


class WorkerLease:
    """A connection loss stops this worker; this lock never grants automatic failover."""

    def __init__(self, state: State, heartbeat_seconds: float = 0.5):
        self.state = state
        self.heartbeat_seconds = heartbeat_seconds
        self.stopping = threading.Event()
        self.lost = threading.Event()
        self.connection: psycopg.Connection | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> WorkerLease:
        self.connection = psycopg.connect(
            self.state.dsn,
            autocommit=True,
            connect_timeout=5,
            application_name="software-factory-worker-lock",
            keepalives=1,
            keepalives_idle=5,
            keepalives_interval=1,
            keepalives_count=3,
            tcp_user_timeout=5000,
            options="-c statement_timeout=5000",
        )
        locked = self.connection.execute(
            "SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK,)
        ).fetchone()
        if not locked or not locked[0]:
            self.connection.close()
            raise FactoryError("Another factory worker already holds the database lock")
        self.thread = threading.Thread(target=self._heartbeat, name="worker-lock", daemon=True)
        self.thread.start()
        return self

    def _heartbeat(self) -> None:
        while not self.stopping.wait(self.heartbeat_seconds):
            try:
                assert self.connection is not None
                self.connection.execute("SELECT 1").fetchone()
            except Exception:
                self.lost.set()
                return

    def check_alive(self) -> None:
        if self.lost.is_set():
            raise Interrupted("Worker database lock connection lost; child processes stopped")
        if self.stopping.is_set():
            raise Interrupted("Worker shutdown requested; child processes stopped")

    def __exit__(self, *_: Any) -> None:
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=6)
        if self.connection:
            self.connection.close()


@contextmanager
def _shutdown_signals(stop: threading.Event):
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stop.set())
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def run_worker(state: State, workflow: Any, poll_seconds: float = 2, once: bool = False) -> None:
    """Claim one stage at a time, keeping SQL transactions out of external operations."""
    with WorkerLease(state) as lease, _shutdown_signals(lease.stopping):
        previous_guard: Callable | None = getattr(workflow.runner, "guard", None)

        def guard() -> None:
            lease.check_alive()
            if previous_guard:
                previous_guard()

        workflow.runner.guard = guard
        try:
            state.reconcile_interrupted()
            reconciled_at = 0.0
            while True:
                guard()
                if time.monotonic() - reconciled_at >= 30:
                    reconcile_ready = getattr(workflow, "reconcile_ready", None)
                    if reconcile_ready:
                        reconcile_ready()
                    reconciled_at = time.monotonic()
                run = state.claim()
                if run is None:
                    if once:
                        return
                    lease.stopping.wait(poll_seconds)
                    continue
                try:
                    guard()
                    workflow.process(run)
                    guard()
                    latest = state.get(run["id"])
                    if latest["state"] == "running":
                        raise FactoryError("Workflow returned without recording a durable outcome")
                except Exception as error:
                    log.error("Run %s blocked: %s", run["id"], error)
                    try:
                        state.update(
                            run["id"],
                            "interrupted" if isinstance(error, Interrupted) else "blocked",
                            state="blocked",
                            blocked_reason=str(error),
                        )
                    except psycopg.Error:
                        # Startup reconciliation will block this record after DB recovery.
                        log.exception("Could not persist blocked state for %s", run["id"])
                    if isinstance(error, Interrupted) or lease.lost.is_set():
                        raise
                if once:
                    return
        finally:
            workflow.runner.guard = previous_guard
