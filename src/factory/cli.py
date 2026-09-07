"""Server-side commands: durable submissions and readable evidence paths."""

import functools
import getpass
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import psycopg
import typer

from factory.agents import AgentRunner, prompt_text
from factory.config import database_url, image_identity, load_config
from factory.errors import FactoryError
from factory.git_ops import GitOps
from factory.process import ProcessRunner
from factory.state import State
from factory.worker import run_worker
from factory.workflow import Workflow, approved_hashes

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Turn one bounded GitHub issue into a checked, reviewed PR.",
)


def guarded(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (FactoryError, OSError, ValueError, psycopg.Error) as exc:
            # Database errors can embed connection details; never render their raw message.
            message = (
                "Database unavailable or migration required"
                if isinstance(exc, psycopg.Error)
                else str(exc)
            )
            typer.echo(f"Error: {message}", err=True)
            raise typer.Exit(1) from None

    return call


def _state() -> State:
    return State(database_url())


def _submitted(run: dict) -> None:
    typer.echo(f"{run['id']}  {run['state']}  next={run['next_stage']}")


@app.command()
@guarded
def migrate():
    """Apply the bundled, versioned Postgres migrations."""
    _state().migrate()
    typer.echo("Database migrations applied.")


@app.command()
@guarded
def prepare(
    issue: Annotated[int, typer.Argument(min=1)],
    agent: Annotated[str | None, typer.Option(help="claude or codex")] = None,
    auth: Annotated[str | None, typer.Option(help="subscription or api")] = None,
    supersede: Annotated[str | None, typer.Option(help="Replace this stopped run")] = None,
):
    """Reserve the issue and enqueue preparation; exit zero means accepted."""
    cfg = load_config()
    run = _state().submit_prepare(
        cfg.repo,
        issue,
        agent or cfg.agent,
        auth or cfg.auth,
        image_identity(),
        cfg.workspace_root,
        supersede=supersede,
    )
    _submitted(run)


@app.command(name="run")
@guarded
def approve(run_id: str):
    """Approve the current spec/plan contents and enqueue implementation."""
    state = _state()
    run = state.get(run_id)
    if run["state"] != "awaiting_approval":
        _submitted(state.approve(run_id, run.get("approval") or {}))
        return
    approval = {
        "hashes": approved_hashes(Path(run["workspace"]), run["issue"]),
        "operator": getpass.getuser(),
        "approved_at": datetime.now(UTC).isoformat(),
    }
    _submitted(state.approve(run_id, approval))


@app.command()
@guarded
def resume(run_id: str):
    """Enqueue a blocked run; preserve scope, approval, and consumed attempts."""
    state = _state()
    if state.get(run_id)["image"] != image_identity():
        raise FactoryError("Resume requires the recorded FACTORY_IMAGE_ID")
    _submitted(state.resume(run_id))


@app.command()
@guarded
def status(run_id: str | None = None, as_json: Annotated[bool, typer.Option("--json")] = False):
    """Show durable state, artifact locations, attempts, and GitHub PR identity."""
    cfg, state = load_config(), _state()
    runs = [state.get(run_id)] if run_id else state.list_runs()
    output = []
    for run in runs:
        row = dict(run)
        row["workspace"] = cfg.host_path(run["workspace"])
        row["artifacts"] = cfg.host_path(run["artifacts"])
        row["documents"] = str(Path(row["workspace"]) / "work" / str(run["issue"]))
        row["attempts"] = state.attempts(run["id"])
        output.append(row)
    if as_json:
        typer.echo(json.dumps(output[0] if run_id else output, indent=2, default=str))
        return
    for run in output:
        _submitted(run)
        typer.echo(f"  Issue: {run['repo']}#{run['issue']}  Agent: {run['engine']}/{run['auth']}")
        typer.echo(
            f"  Attempts: {run['attempt_count']}/3  Candidate: {run['candidate_sha'] or '—'}"
        )
        typer.echo(f"  Documents: {run['documents']}\n  Evidence: {run['artifacts']}")
        if run.get("blocked_reason"):
            typer.echo(f"  Blocked: {run['blocked_reason']}")
        if run.get("pr"):
            typer.echo(f"  PR: {run['pr']['url']}")
        if run.get("superseded_by"):
            typer.echo(f"  Superseded by: {run['superseded_by']}")


@app.command()
@guarded
def logs(run_id: str, stage: Annotated[str | None, typer.Option()] = None):
    """Print persisted stage transcripts and check logs (pipe to less)."""
    run = _state().get(run_id)
    root = Path(run["artifacts"])
    if not root.exists():
        typer.echo("No stage output yet.")
        return
    for path in sorted(root.rglob("*.log")):
        if Path(run["workspace"]) in path.parents or (
            stage and stage not in str(path.relative_to(root))
        ):
            continue
        typer.echo(f"\n--- {path.relative_to(root)} ---")
        typer.echo(path.read_text(errors="replace"))


@app.command()
@guarded
def worker(once: Annotated[bool, typer.Option(help="Process at most one queued request")] = False):
    """Run the single supervised worker (Compose's main process)."""
    cfg, state = load_config(), _state()
    image_identity()
    run_worker(state, Workflow(state, cfg), poll_seconds=cfg.poll_seconds, once=once)


@app.command()
@guarded
def doctor():
    """Check tools, native authentication, configuration, mounts, and database."""
    cfg, state, runner = load_config(), _state(), ProcessRunner()
    typer.echo(f"Image: {image_identity()}")
    for executable in ("git", "gh", "claude", "codex"):
        if not shutil.which(executable):
            raise FactoryError(f"Missing executable: {executable}")
    with state.connect() as conn:
        conn.execute("SELECT id FROM runs LIMIT 1")
    if not cfg.workspace_root.is_dir() or not os.access(cfg.workspace_root, os.W_OK):
        raise FactoryError("Workspace mount must exist and be writable")
    GitOps(cfg.checkout, runner).validate(cfg.base_branch, cfg.repo)
    for name in ("AGENTS.md", "CLAUDE.md", "REVIEW.md"):
        if not (cfg.checkout / name).is_file():
            raise FactoryError(f"Owner must review and commit {name}; see examples/target/")
    result = runner.run(["gh", "auth", "status"], cwd=cfg.checkout, timeout=30)
    if result.returncode:
        raise FactoryError("GitHub login required: gh auth login")
    AgentRunner(runner).validate(cfg.agent, cfg.auth, cfg.checkout)
    typer.echo(f"OK: Postgres, workspace, target guidance, GitHub, {cfg.agent}/{cfg.auth}.")
    typer.echo(f"Host workspace: {cfg.host_path(cfg.workspace_root)}")


@app.command()
@guarded
def smoke(
    agent: Annotated[str, typer.Option(help="claude or codex")],
    auth: Annotated[str, typer.Option(help="subscription or api")],
    output: Annotated[Path, typer.Option(help="Persist smoke evidence here")] = Path(
        "/tmp/factory-smoke"
    ),
):
    """Exercise a fresh native CLI session and structured output; no database needed."""
    root = output.absolute() / f"{agent}-{auth}-{uuid4()}"
    work = root / "worktree"
    work.mkdir(parents=True)
    runner = ProcessRunner()
    runner.run(["git", "init", "--quiet", str(work)], cwd=root, timeout=30, check=True)
    (work / "smoke.txt").write_text("The factory smoke-test marker is ORBIT.\n")
    prompt = (
        prompt_text("prepare")
        + "\nThis is an authentication/tool smoke test. Read smoke.txt in the working directory. "
        + "Return a short spec containing the marker from that file, a one-sentence plan, "
        + "and no unresolved decisions. Do not edit files. Read-only shell commands are allowed."
    )
    output_model = AgentRunner(runner).run(
        "prepare", agent, auth, work, root / "evidence", prompt, 180
    )
    if (
        "ORBIT" not in output_model.spec
        or (work / "smoke.txt").read_text() != "The factory smoke-test marker is ORBIT.\n"
    ):
        raise FactoryError("Smoke did not demonstrate read-only file access and valid output")
    typer.echo(f"PASS {agent}/{auth}; evidence: {root}")


if __name__ == "__main__":
    app()
