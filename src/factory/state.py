"""Transactional workflow records and reservations; external work stays outside SQL."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from factory.errors import FactoryError

Run = dict[str, Any]

# Stable, distinct session locks for schema changes and the single worker.
MIGRATION_LOCK = 0x464143544D4947
WORKER_LOCK = 0x46414354574F52

_RUN_JSON = {"frozen", "approval", "pr", "metadata"}
_RUN_FIELDS = {
    "state",
    "next_stage",
    "frozen",
    "base_sha",
    "prepared_sha",
    "checkpoint_sha",
    "approval_sha",
    "candidate_sha",
    "approval",
    "blocked_reason",
    "pr",
    "superseded_by",
    "metadata",
}
_ATTEMPT_JSON = {"checks", "review", "findings", "evidence"}
_ATTEMPT_FIELDS = _ATTEMPT_JSON | {
    "stage",
    "status",
    "candidate_sha",
    "finished_at",
}


def _event_json(value: Any) -> str:
    def encode(item: Any) -> str:
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        raise TypeError(f"Unsupported event value: {type(item).__name__}")

    return json.dumps(value, default=encode)


class State:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self, *, autocommit: bool = False) -> Connection:
        return psycopg.connect(
            self.dsn,
            autocommit=autocommit,
            row_factory=dict_row,
            connect_timeout=5,
            application_name="software-factory",
        )

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.connect() as connection:
            yield connection

    def migrate(self) -> None:
        """Apply packaged SQL files once, with serialized, checksum-checked migrations."""
        with self.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name text PRIMARY KEY, sha256 text NOT NULL, "
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            migrations = files("factory").joinpath("migrations")
            for migration in sorted(migrations.iterdir(), key=lambda item: item.name):
                if not migration.name.endswith(".sql"):
                    continue
                body = migration.read_text(encoding="utf-8")
                digest = hashlib.sha256(body.encode()).hexdigest()
                existing = connection.execute(
                    "SELECT sha256 FROM schema_migrations WHERE name = %s", (migration.name,)
                ).fetchone()
                if existing:
                    if existing["sha256"] != digest:
                        raise FactoryError(f"Applied migration changed: {migration.name}")
                    continue
                connection.execute(body)
                connection.execute(
                    "INSERT INTO schema_migrations(name, sha256) VALUES (%s, %s)",
                    (migration.name, digest),
                )

    @staticmethod
    def _get(connection: Connection, run_id: str, *, lock: bool = False) -> Run:
        row = connection.execute(
            "SELECT * FROM runs WHERE id = %s" + (" FOR UPDATE" if lock else ""),
            (run_id,),
        ).fetchone()
        if row is None:
            raise FactoryError(f"Unknown run: {run_id}")
        return row

    @staticmethod
    def _event(connection: Connection, run_id: str, event: str, details: Any = None) -> None:
        connection.execute(
            "INSERT INTO events(run_id, event, details) VALUES (%s, %s, %s)",
            (run_id, event, Jsonb(details or {}, dumps=_event_json)),
        )

    def get(self, run_id: str) -> Run:
        with self.transaction() as connection:
            return self._get(connection, run_id)

    def list_runs(self) -> list[Run]:
        with self.transaction() as connection:
            return connection.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()

    def submit_prepare(
        self,
        repo: str,
        issue: int,
        engine: str,
        auth: str,
        image: str,
        workspace_root: str | Path,
        supersede: str | None = None,
    ) -> Run:
        """Reserve an issue and repository atomically; retries never duplicate work."""
        if issue <= 0 or engine not in {"claude", "codex"} or auth not in {"subscription", "api"}:
            raise FactoryError("Expected a positive issue, claude|codex, and subscription|api")
        with self.transaction() as connection:
            # A transaction lock protects even the no-existing-row case. Unique indexes
            # also enforce the invariants for all writers, independent of this method.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (repo,))
            if supersede:
                old = self._get(connection, supersede, lock=True)
                if old["repo"] != repo or old["issue"] != issue:
                    raise FactoryError("Supersession must name a run for this repository and issue")
                if old["superseded_by"]:
                    return self._get(connection, old["superseded_by"])
                if old["state"] not in {"blocked", "awaiting_approval", "ready", "closed"}:
                    raise FactoryError("Supersession requires a stopped run")
            else:
                existing = connection.execute(
                    "SELECT * FROM runs WHERE repo = %s AND issue = %s "
                    "AND state NOT IN ('closed', 'superseded') AND superseded_by IS NULL",
                    (repo, issue),
                ).fetchone()
                if existing:
                    if (existing["engine"], existing["auth"], existing["image"]) != (
                        engine,
                        auth,
                        image,
                    ):
                        raise FactoryError("Issue already reserved with different frozen settings")
                    return existing

            active = connection.execute(
                "SELECT id FROM runs WHERE repo = %s "
                "AND state IN ('queued', 'running', 'awaiting_approval', 'blocked') "
                "AND superseded_by IS NULL",
                (repo,),
            ).fetchone()
            if active and active["id"] != supersede:
                raise FactoryError(f"Repository already reserved by unfinished run {active['id']}")

            run_id = str(uuid4())
            artifacts = Path(workspace_root).absolute() / "runs" / run_id
            if supersede:
                connection.execute(
                    "UPDATE runs SET superseded_by = %s, updated_at = now() WHERE id = %s",
                    (run_id, supersede),
                )
                self._event(
                    connection, supersede, "supersession_requested", {"replacement": run_id}
                )
            row = connection.execute(
                "INSERT INTO runs (id, repo, issue, branch, workspace, artifacts, state, "
                "next_stage, engine, auth, image, supersedes) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s) RETURNING *",
                (
                    run_id,
                    repo,
                    issue,
                    f"factory/{issue}/{run_id}",
                    str(artifacts / "worktree"),
                    str(artifacts),
                    "supersede" if supersede else "prepare",
                    engine,
                    auth,
                    image,
                    supersede,
                ),
            ).fetchone()
            self._event(connection, run_id, "prepare_requested", {"supersedes": supersede})
            return row

    def approve(self, run_id: str, approval: dict) -> Run:
        with self.transaction() as connection:
            run = self._get(connection, run_id, lock=True)
            if run["superseded_by"] or run["state"] in {"closed", "superseded"}:
                raise FactoryError("This run is closed or superseded")
            if run["approval"] and run["state"] != "awaiting_approval":
                return run
            if run["state"] != "awaiting_approval":
                raise FactoryError("Approval requires awaiting_approval")
            hashes = approval.get("hashes", {})
            if (
                set(approval) != {"hashes", "operator", "approved_at"}
                or not isinstance(hashes, dict)
                or set(hashes) != {"spec.md", "plan.md"}
                or any(
                    not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h)
                    for h in hashes.values()
                )
                or not isinstance(approval.get("operator"), str)
                or not approval["operator"].strip()
                or not isinstance(approval.get("approved_at"), str)
            ):
                raise FactoryError("Approval must record exact document hashes, operator, and time")
            try:
                approved_at = datetime.fromisoformat(approval["approved_at"])
            except ValueError as error:
                raise FactoryError(
                    "Approval time must be an ISO timestamp with timezone"
                ) from error
            if approved_at.tzinfo is None:
                raise FactoryError("Approval time must be an ISO timestamp with timezone")
            row = connection.execute(
                "UPDATE runs SET approval = %s, state = 'queued', next_stage = 'approve', "
                "blocked_reason = NULL, updated_at = now() WHERE id = %s RETURNING *",
                (Jsonb(approval), run_id),
            ).fetchone()
            self._event(connection, run_id, "approval_requested", approval)
            return row

    def resume(self, run_id: str) -> Run:
        with self.transaction() as connection:
            run = self._get(connection, run_id, lock=True)
            if run["superseded_by"] or run["state"] == "superseded":
                raise FactoryError("Superseded runs can never resume")
            if run["state"] in {"queued", "running"}:
                # Only replay an actual resume; prepare/run are separate requests.
                resumed = connection.execute(
                    "SELECT 1 FROM events WHERE run_id = %s AND event = 'resume_requested' LIMIT 1",
                    (run_id,),
                ).fetchone()
                if resumed:
                    return run
            if run["state"] != "blocked":
                raise FactoryError("Resume requires blocked")
            row = connection.execute(
                "UPDATE runs SET state = 'queued', blocked_reason = NULL, "
                "updated_at = now() WHERE id = %s RETURNING *",
                (run_id,),
            ).fetchone()
            self._event(
                connection,
                run_id,
                "resume_requested",
                {
                    "next_stage": run["next_stage"],
                    "previous_reason": run["blocked_reason"],
                },
            )
            return row

    def claim(self) -> Run | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE state = 'queued' AND superseded_by IS NULL "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            run = connection.execute(
                "UPDATE runs SET state = 'running', updated_at = now() WHERE id = %s RETURNING *",
                (row["id"],),
            ).fetchone()
            self._event(connection, row["id"], "claimed", {"next_stage": row["next_stage"]})
            return run

    def update(self, run_id: str, event: str, **fields: Any) -> Run:
        unknown = fields.keys() - _RUN_FIELDS
        if unknown:
            raise FactoryError(f"Unknown run fields: {', '.join(sorted(unknown))}")
        with self.transaction() as connection:
            self._get(connection, run_id, lock=True)
            assignments = [sql.SQL("{} = %s").format(sql.Identifier(key)) for key in fields]
            assignments.append(sql.SQL("updated_at = now()"))
            parameters = [
                Jsonb(value) if key in _RUN_JSON else value for key, value in fields.items()
            ]
            row = connection.execute(
                sql.SQL("UPDATE runs SET {} WHERE id = %s RETURNING *").format(
                    sql.SQL(", ").join(assignments)
                ),
                [*parameters, run_id],
            ).fetchone()
            self._event(connection, run_id, event, fields)
            return row

    def reserve_attempt(self, run_id: str) -> dict:
        with self.transaction() as connection:
            run = self._get(connection, run_id, lock=True)
            if run["state"] != "running" or run["superseded_by"]:
                raise FactoryError("Only a running, current run can reserve an attempt")
            if run["attempt_count"] >= 3:
                raise FactoryError("Implementation attempt budget exhausted (3 total)")
            number = run["attempt_count"] + 1
            connection.execute(
                "UPDATE runs SET attempt_count = %s, updated_at = now() WHERE id = %s",
                (number, run_id),
            )
            attempt = connection.execute(
                "INSERT INTO attempts(run_id, number) VALUES (%s, %s) RETURNING *",
                (run_id, number),
            ).fetchone()
            self._event(connection, run_id, "attempt_reserved", {"number": number})
            return attempt

    def finish_attempt(self, run_id: str, number: int, **fields: Any) -> dict:
        unknown = fields.keys() - _ATTEMPT_FIELDS
        if unknown:
            raise FactoryError(f"Unknown attempt fields: {', '.join(sorted(unknown))}")
        with self.transaction() as connection:
            assignments = [sql.SQL("{} = %s").format(sql.Identifier(key)) for key in fields]
            if "finished_at" not in fields:
                if fields.get("status") in {"reserved", "running"}:
                    assignments.append(sql.SQL("finished_at = NULL"))
                elif (
                    fields.get("status")
                    in {
                        "accepted",
                        "rejected",
                        "blocked",
                        "interrupted",
                        "failed",
                        "complete",
                    }
                    or not fields
                ):
                    assignments.append(sql.SQL("finished_at = now()"))
            parameters = [
                Jsonb(value) if key in _ATTEMPT_JSON else value for key, value in fields.items()
            ]
            row = connection.execute(
                sql.SQL(
                    "UPDATE attempts SET {} WHERE run_id = %s AND number = %s RETURNING *"
                ).format(sql.SQL(", ").join(assignments)),
                [*parameters, run_id, number],
            ).fetchone()
            if row is None:
                raise FactoryError(f"Unknown attempt {number} for run {run_id}")
            self._event(connection, run_id, "attempt_updated", {"number": number, **fields})
            return row

    def attempts(self, run_id: str) -> list[dict]:
        with self.transaction() as connection:
            self._get(connection, run_id)
            return connection.execute(
                "SELECT * FROM attempts WHERE run_id = %s ORDER BY number",
                (run_id,),
            ).fetchall()

    def events(self, run_id: str) -> list[dict]:
        with self.transaction() as connection:
            self._get(connection, run_id)
            return connection.execute(
                "SELECT * FROM events WHERE run_id = %s ORDER BY id",
                (run_id,),
            ).fetchall()

    def reconcile_interrupted(self) -> list[Run]:
        """Only call after acquiring the worker lock; preserve checkpoints and budgets."""
        with self.transaction() as connection:
            runs = connection.execute(
                "UPDATE runs SET state = 'blocked', blocked_reason = 'interrupted', "
                "updated_at = now() WHERE state = 'running' RETURNING *"
            ).fetchall()
            for run in runs:
                connection.execute(
                    "UPDATE attempts SET status = 'interrupted', finished_at = now() "
                    "WHERE run_id = %s AND finished_at IS NULL",
                    (run["id"],),
                )
                self._event(connection, run["id"], "interrupted", {"next_stage": run["next_stage"]})
            return runs
