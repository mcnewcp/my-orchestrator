"""Command line: `orch run | step | status <issue>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import artifacts, machine, note, runners, shell, state as state_mod, warn
from .artifacts import ArtifactError
from .config import ConfigError, load_config
from .finalize import FinalizeError
from .machine import Ctx, scratch_dir
from .runners import SetupError
from .shell import ShellError
from .state import State, derive_state


def target_repo(start: Path | None = None) -> Path:
    """Root of the target repo the command was run from."""
    start = start or Path.cwd()
    proc = shell.git("rev-parse", "--show-toplevel", cwd=start, check=False)
    if proc.returncode != 0:
        raise SetupError(f"not inside a git repository: {start}")
    return Path(proc.stdout.strip()).resolve()


def _context(args: argparse.Namespace, strict_setup: bool) -> Ctx:
    repo = target_repo()
    config = load_config(args.config)
    try:
        runners.ensure_target_setup(repo)
    except SetupError as exc:
        if strict_setup:
            raise
        warn(str(exc))
    return Ctx(repo=repo, config=config)


def _terminal_report(state: State, repo: Path, issue: int) -> str:
    scratch = scratch_dir(repo, issue)
    if state is State.READY:
        run = artifacts.read_run(scratch) or {}
        return str(run.get("pr_url") or scratch / artifacts.SUMMARY_MD)
    if state is State.NEEDS_CLARIFICATION:
        return str(scratch / artifacts.CLARIFICATION_MD)
    if state is State.ESCALATED:
        return str(scratch / artifacts.ESCALATION_MD)
    return str(scratch)


def cmd_status(args: argparse.Namespace) -> int:
    """Print the derived state, HEAD, branch, and artifact inventory."""
    ctx = _context(args, strict_setup=False)
    scratch = scratch_dir(ctx.repo, args.issue)
    # Read the issue branch's tip instead of checking it out: status never touches the checkout.
    branch = (artifacts.read_run(scratch) or {}).get("branch")
    head = shell.ref_sha(ctx.repo, branch) if branch else shell.head_sha(ctx.repo)
    state = derive_state(scratch, head)
    print(f"issue    {args.issue}")
    print(f"repo     {ctx.repo}")
    print(f"scratch  {scratch}")
    print(f"state    {state}")
    print(f"head     {head or '(no commits)'}")
    print(f"branch   {branch or shell.current_branch(ctx.repo) or '(detached)'}")
    inventory = state_mod.inventory(scratch)
    print("artifacts:")
    for name, detail in inventory or [("(none)", "")]:
        print(f"  {name:<18} {detail}".rstrip())
    return 0


def cmd_step(args: argparse.Namespace) -> int:
    """Perform exactly one transition."""
    ctx = _context(args, strict_setup=True)
    result = machine.step(args.issue, ctx)
    for action in result.actions:
        note(action)
    print(f"{result.state} -> {result.next_state}")
    if result.terminal:
        print(f"{result.next_state}: {_terminal_report(result.next_state, ctx.repo, args.issue)}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Step until a terminal state (a stuck role escalates), a pause, or max_steps."""
    ctx = _context(args, strict_setup=True)
    result = machine.run(args.issue, ctx, pause_after_contract=args.pause_after_contract)
    if result.reason == "terminal":
        if result.detail:
            note(result.detail)
        print(f"{result.state}: {_terminal_report(result.state, ctx.repo, args.issue)}")
        return 0
    if result.reason == "paused":
        print(f"paused after CONTRACTING in state {result.state}; run `orch run` again to resume")
        return 0
    print(f"aborted ({result.reason}) in state {result.state}: {result.detail}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the three commands."""
    parser = argparse.ArgumentParser(prog="orch", description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, func) -> argparse.ArgumentParser:
        """Register one subcommand that takes an issue number."""
        sub = subs.add_parser(name, help=help_text)
        sub.add_argument("issue", type=int, help="GitHub issue number")
        sub.add_argument("--config", default=None, help="path to orch.toml")
        sub.set_defaults(func=func)
        return sub

    run_parser = add("run", "step until a terminal state", cmd_run)
    run_parser.add_argument(
        "--pause-after-contract",
        action="store_true",
        help="stop after the contract is written",
    )
    add("step", "perform exactly one transition", cmd_step)
    add("status", "print derived state and artifact inventory", cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse arguments, dispatch, and map known failures to exit code 1."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ArtifactError, ConfigError, FinalizeError, SetupError, ShellError) as exc:
        print(f"orch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
