"""The state machine: one transition per step, plus the run loop (conventions.md section 6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import artifacts, finalize, note, runners, shell, state as state_mod
from .artifacts import ESCALATION_MD
from .config import Config
from .runners import Runner
from .state import State, TERMINAL, derive_state


def scratch_dir(repo: Path, issue: int) -> Path:
    """Path of the scratch directory for an issue inside the target repo."""
    return repo / ".scratch" / str(issue)


@dataclass
class Ctx:
    """Everything a transition needs: the target checkout, config, and a runner factory."""

    repo: Path
    config: Config
    runner_factory: Callable[[str, Config], Runner] | None = None

    def runner(self, role: str) -> Runner:
        """Runner instance for a role key (runners.get_runner unless one was injected)."""
        factory = self.runner_factory or runners.get_runner
        return factory(role, self.config)


@dataclass
class StepResult:
    """What one transition did and where it left the run."""

    state: State
    next_state: State
    head_before: str
    head_after: str
    actions: list[str] = field(default_factory=list)
    #: (role, log path, the role's final message) per harness invocation, in order.
    invocations: list[tuple[str, Path | None, str]] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        """True when the run has reached a terminal state."""
        return self.next_state in TERMINAL


@dataclass
class RunResult:
    """Outcome of `orch run`: where it stopped and why."""

    state: State
    reason: str  # terminal | paused | stuck | max_steps
    detail: str = ""
    steps: int = 0

    @property
    def ok(self) -> bool:
        """True for a clean stop (terminal state or a requested pause)."""
        return self.reason in ("terminal", "paused")


def ensure_branch(repo: Path, scratch: Path) -> None:
    """Check out run.json's branch when it is set and the checkout is elsewhere."""
    run = artifacts.read_run(scratch) or {}
    branch = run.get("branch")
    if not branch or shell.current_branch(repo) == branch:
        return
    note(f"checking out {branch}")
    shell.git("checkout", str(branch), cwd=repo)


def _escalate(scratch: Path, issue: int, reason: str, detail: str) -> Path:
    """Write escalation.md for a cap the CLI enforces."""
    open_blocking = state_mod.open_blocking_findings(scratch)
    blockers = (
        "\n".join(
            f"- {f.get('id')} ({f.get('class')}) {f.get('location')} — {f.get('summary')}"
            for f in open_blocking
        )
        or "None open in the ledger."
    )
    run = artifacts.read_run(scratch) or {}
    audit = state_mod.latest_audit(scratch)
    failures = "\n".join(f"- {line}" for line in (audit[1].get("failures") or [])) if audit else ""
    text = "\n".join(
        [
            f"# Escalation — issue {issue}",
            "",
            reason,
            "",
            "## What converged",
            f"- contract: `.scratch/{issue}/contract.md`",
            f"- branch: `{run.get('branch')}`",
            f"- PR: {run.get('pr_url') or run.get('pr_number')} (still a draft)",
            "",
            "## What did not converge",
            detail,
            failures,
            "",
            "## Open blocking findings",
            blockers,
            "",
            "## Recommendation",
            "Read the artifacts above, then either fix the blocker by hand, "
            "amend the contract's acceptance criteria, or close the PR.",
            "",
        ]
    )
    path = artifacts.write_text(scratch / ESCALATION_MD, text)
    note(f"wrote {path}")
    return path


def step(issue: int, ctx: Ctx) -> StepResult:
    """Perform exactly one transition and report the state on both sides of it."""
    repo = ctx.repo
    scratch = scratch_dir(repo, issue)
    scratch.mkdir(parents=True, exist_ok=True)
    ensure_branch(repo, scratch)
    head_before = shell.head_sha(repo)
    current = derive_state(scratch, head_before)

    actions: list[str] = []
    invocations: list[tuple[str, Path | None]] = []

    def invoke(role: str) -> int:
        """Run one role in a fresh session and record the invocation."""
        runner = ctx.runner(role)
        code = runner.run(role, issue, repo)
        invocations.append(
            (role, getattr(runner, "last_log_path", None), getattr(runner, "last_message", ""))
        )
        actions.append(f"ran {role} (exit {code})")
        return code

    if current in TERMINAL:
        pass
    elif current is State.CONTRACTING:
        artifacts.ensure_run(scratch, issue)
        invoke("contractor")
    elif current is State.IMPLEMENTING:
        invoke("implementer")
    elif current is State.AUDITING:
        invoke("auditor")
        failures = state_mod.consecutive_audit_failures(scratch)
        cap = ctx.config.audit_failure_cap
        if failures >= cap:
            _escalate(
                scratch,
                issue,
                f"The audit gate failed {failures} times in a row (cap {cap}).",
                f"Latest audit: `audit-{state_mod.next_audit_round(scratch) - 1}.json`.",
            )
            actions.append("wrote escalation.md (audit failure cap)")
    elif current is State.REVIEWING:
        round_n = state_mod.next_review_round(scratch)
        cap = ctx.config.review_round_cap
        if round_n > cap:
            _escalate(
                scratch,
                issue,
                f"Review round {round_n} would exceed the round cap ({cap}).",
                "Backstop: the cap is normally enforced right after JUDGING.",
            )
            actions.append("wrote escalation.md (review round cap)")
        else:
            invoke("reviewer")
    elif current is State.JUDGING:
        invoke("judge")
        rounds = (artifacts.read_ledger(scratch) or {}).get("rounds_completed", 0)
        cap = ctx.config.review_round_cap
        if rounds >= cap and state_mod.open_blocking_findings(scratch):
            _escalate(
                scratch,
                issue,
                f"Review round cap reached with blocking findings open "
                f"({rounds} round(s) adjudicated, cap {cap}).",
                f"Round {rounds} was adjudicated and at least one blocking finding is still open.",
            )
            actions.append("wrote escalation.md (review round cap)")
    elif current is State.REMEDIATING:
        count = len(state_mod.open_blocking_findings(scratch))
        note(f"remediating {count} open blocking finding(s)")
        for _ in range(count):
            invoke("remediator")
    elif current is State.FINALIZING:
        path = finalize.finalize(issue, repo, scratch)
        actions.append(f"wrote {path.name} and marked the PR ready")

    ensure_branch(repo, scratch)
    head_after = shell.head_sha(repo)
    return StepResult(
        state=current,
        next_state=derive_state(scratch, head_after),
        head_before=head_before,
        head_after=head_after,
        actions=actions,
        invocations=invocations,
    )


def run(issue: int, ctx: Ctx, pause_after_contract: bool = False) -> RunResult:
    """Step until a terminal state, a pause, a stuck step, or max_steps."""
    scratch = scratch_dir(ctx.repo, issue)
    steps = 0
    current = derive_state(scratch, shell.head_sha(ctx.repo))
    for _ in range(ctx.config.max_steps):
        scratch.mkdir(parents=True, exist_ok=True)
        files_before = state_mod.artifact_names(scratch)
        current = derive_state(scratch, shell.head_sha(ctx.repo))
        if current in TERMINAL:
            return RunResult(current, "terminal", steps=steps)

        result = step(issue, ctx)
        steps += 1
        current = result.next_state
        files_after = state_mod.artifact_names(scratch)

        if (
            result.next_state is result.state
            and result.head_after == result.head_before
            and files_after <= files_before
        ):
            # A role ran and changed nothing: the loop cannot converge, so hand it to the
            # operator through the designed exit, carrying the role's own explanation.
            role, log, message = (
                result.invocations[-1] if result.invocations else ("(none)", None, "")
            )
            quoted = "\n".join(f"> {line}" for line in message.strip().splitlines()) or "> (none)"
            _escalate(
                scratch,
                issue,
                f"{result.state} did not advance after running {role}: no commit, no new "
                "artifact, no state change.",
                f"Log: `{log}`\n\nFinal message from {role}:\n\n{quoted}",
            )
            current = derive_state(scratch, shell.head_sha(ctx.repo))
            return RunResult(
                current,
                "terminal",
                detail=f"escalated: {result.state} did not advance after running {role}",
                steps=steps,
            )
        if current in TERMINAL:
            return RunResult(current, "terminal", steps=steps)
        if pause_after_contract and result.state is State.CONTRACTING:
            return RunResult(current, "paused", detail="stopped after CONTRACTING", steps=steps)
    return RunResult(
        current, "max_steps", detail=f"stopped after {ctx.config.max_steps} steps", steps=steps
    )
