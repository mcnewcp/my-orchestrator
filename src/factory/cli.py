"""`factory` console script (design §6).

Global flags: --harness claude|codex, --auth subscription|api, --model, --force. poll accepts none of them (it uses
the config). --force is accepted only for spec, plan and build; on any other command it is a FactoryError.
Exit codes: 0 done/continue; 1 FactoryError; 2 NeedsHuman. Unexpected exceptions -> traceback + 1.

Commands: init doctor spec accept plan build review fix finalize run poll status dismiss abandon version

Dispatch: discover Repo from cwd; init needs no config (it writes it); everything else load_config + validate_overrides.
Issue commands build one Context, call stages.prepare(ctx, ...) once with the per-command policy (spec/run
need_state=False; abandon prepares for a teardown — no fetch, no operator-edit commit, no dirty-worktree refusal and
no protected-path check, because it deletes the branch either way; status does not prepare), dispatch, and release the
RunLock in a finally. `run` uses stages.run_issue semantics inline; `poll` passes a closure over stages.run_issue.
NeedsHuman prints "needs human: <gate>\n<what clears it>" to stderr and returns 2.
FactoryError prints "error: <message>" (+ hint, + "transcript: <path>" for HarnessError) and returns 1.
KeyboardInterrupt prints "error: interrupted; the next command will discard the partial stage" and returns 1 — the
lock is deliberately LEFT behind, so the next command sees a dead writer and takes the interrupted-stage path
(design §7, §15) instead of resuming on top of half-written work.
Progress lines go to stderr; only `status` and `version` write to stdout.

A usage error (unknown command, bad flag, missing argument, no command at all) is a FactoryError too: argparse's own
SystemExit(2) would collide with exit 2 = "needs human", so the parser raises instead of exiting. `--help` still
exits 0 through argparse.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

from . import __version__, initcmd, stages
from .config import AUTHS, HARNESSES, Config, load_config, validate_overrides
from .doctor import doctor as run_doctor
from .errors import FactoryError, NeedsHuman
from .gh import GitHub
from .poll import poll as run_poll
from .repo import Repo
from .stages import Context
from .state import RunLock

#: --force rewinds a stage's own commits; only these three have a defined rewind point (design §6).
FORCE_COMMANDS = ("spec", "plan", "build")

#: Commands that take an issue number. All but `status` and `dismiss` name a one-argument function in stages.py,
#: looked up at dispatch time (never bound at import) so stages.py stays the one source of stage behaviour.
_ISSUE_COMMANDS = {
    "spec": "snapshot the issue, write spec.md, open the draft PR",
    "accept": "record that you resolved spec.md's open questions",
    "plan": "write plan.md from spec.md",
    "build": "implement plan.md and run the checks",
    "review": "run one review round against the diff",
    "fix": "address the open Important findings",
    "finalize": "flip the PR to ready and post the summary",
    "run": "run every remaining stage; stops at gates with exit 2",
    "status": "print the local state for one issue (no network)",
    "abandon": "drop the label, close the PR, delete the branch and worktree",
}

#: Per-command prepare() policy (design deviation 9). Everything else uses prepare()'s defaults.
#: `abandon` removes the label, the PR, the branch and the worktree; every start-of-command rule that could
#: refuse — a fetch that finds the remote diverged, a dirty worktree, a protected path changed on the branch —
#: would only strand the very state it exists to clean up, so abandon is exempt from all of them.
_PREPARE_POLICY = {
    "spec": {"need_state": False},
    "run": {"need_state": False},
    "abandon": {
        "need_state": False,
        "commit_operator_edits": False,
        "fetch": False,
        "strict_clean": False,
        "check_protected": False,
    },
}

_ISSUE_HELP = "GitHub issue number"


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error; here 2 means "needs human", so usage errors raise FactoryError (exit 1)."""

    def error(self, message: str):  # argparse's own hook, called for every usage error
        raise FactoryError(f"{self.prog}: {message}", hint=self.format_usage().strip())


def _issue_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an issue number") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"issue numbers are positive, got {number}")
    return number


def _reason(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError(
            "the dismissal reason is recorded in the ledger and must not be empty"
        )
    return value


def _add_global_flags(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """The four global flags, on the top-level parser and on every subparser, so `factory --force spec 42` and
    `factory spec 42 --force` mean the same thing. The subparser copies default to SUPPRESS so an absent flag
    there does not overwrite what the top-level parser already parsed."""
    hidden = {"default": argparse.SUPPRESS} if suppress else {}
    parser.add_argument("--harness", choices=HARNESSES, help="override [factory] harness", **hidden)
    parser.add_argument("--auth", choices=AUTHS, help="override [factory] auth", **hidden)
    parser.add_argument(
        "--model", help='override the selected harness\'s model ("" = CLI default)', **hidden
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=f"rewind and re-run the stage ({', '.join(FORCE_COMMANDS)} only)",
        **(hidden or {"default": False}),
    )


def build_parser() -> argparse.ArgumentParser:
    common = _Parser(add_help=False)
    _add_global_flags(common, suppress=True)

    parser = _Parser(
        prog="factory",
        description="Turn one bounded GitHub issue into one pull request ready for human review.",
    )
    _add_global_flags(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], help=help_text, description=help_text)

    add("init", "write factory.toml, REVIEW.md, the issue template and the label")
    add("doctor", "check binaries, versions, gh auth, and probe the harness")
    for name, help_text in _ISSUE_COMMANDS.items():
        add(name, help_text).add_argument("issue", type=_issue_number, help=_ISSUE_HELP)
    dismiss = add("dismiss", "adjudicate a finding; recorded in the ledger")
    dismiss.add_argument("issue", type=_issue_number, help=_ISSUE_HELP)
    dismiss.add_argument("finding", help="finding id, e.g. F3")
    dismiss.add_argument(
        "reason", type=_reason, help="why it is dismissed (recorded in the ledger)"
    )
    add("poll", "run every eligible labelled issue in turn; one-shot")
    add("version", "print the factory version")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        return _dispatch(args, parser)
    except NeedsHuman as exc:
        print(f"needs human: {exc.gate}", file=sys.stderr)
        print(exc.what_clears_it, file=sys.stderr)
        return exc.exit_code
    except FactoryError as exc:
        _report(exc)
        return exc.exit_code
    except KeyboardInterrupt:
        print(
            "error: interrupted; the next command will discard the partial stage", file=sys.stderr
        )
        return 1
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


def _report(exc: FactoryError) -> None:
    print(f"error: {exc.message}", file=sys.stderr)
    if exc.hint:
        print(exc.hint, file=sys.stderr)
    transcript = getattr(exc, "transcript_path", None)
    if transcript:
        print(f"transcript: {transcript}", file=sys.stderr)


def _err(line: str) -> None:
    print(line, file=sys.stderr)


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    command = args.command
    if not command:
        parser.print_help(sys.stderr)
        raise FactoryError("no command given", hint="`factory --help` lists the commands")
    if command == "version":
        print(f"factory {__version__}")
        return 0
    _check_global_flags(command, args)

    repo = Repo.discover(Path.cwd())
    gh = GitHub(repo.root)
    if command == "init":
        for line in initcmd.init(repo.root, gh):
            _err(line)
        return 0

    config = validate_overrides(
        load_config(repo.root),
        harness=getattr(args, "harness", None),
        auth=getattr(args, "auth", None),
        model=getattr(args, "model", None),
    )
    if command == "doctor":
        return _doctor(repo, config)
    if command == "poll":
        return _poll(repo, gh, config)
    return _issue_command(command, args, repo, gh, config)


def _check_global_flags(command: str, args: argparse.Namespace) -> None:
    if getattr(args, "force", False) and command not in FORCE_COMMANDS:
        raise FactoryError(
            f"--force is only accepted for {', '.join(FORCE_COMMANDS)}, not {command}",
            hint="--force rewinds the branch to the start of a stage; no other command has one",
        )
    if command != "poll":
        return
    given = [name for name in ("harness", "auth", "model") if getattr(args, name, None) is not None]
    given += ["force"] if getattr(args, "force", False) else []
    if given:
        raise FactoryError(
            f"`factory poll` takes no {', '.join('--' + name for name in given)}",
            hint="poll is unattended: it uses factory.toml so every tick makes the same decisions",
        )


def _doctor(repo: Repo, config: Config) -> int:
    report = run_doctor(
        repo,
        config,
        harness=config.harness,
        auth=config.auth,
        model=config.model,
        parent_env=dict(os.environ),
    )
    _err(report.render())
    return 0 if report.ok else 1


def _poll(repo: Repo, gh: GitHub, config: Config) -> int:
    parent_env = dict(os.environ)

    def run_one(issue: int) -> int:
        return stages.run_issue(repo, gh, config, issue, parent_env=parent_env, out=_err)

    return run_poll(repo, gh, config, parent_env=parent_env, run_issue=run_one, out=_err).exit_code


def _issue_command(
    command: str, args: argparse.Namespace, repo: Repo, gh: GitHub, config: Config
) -> int:
    ctx = Context(
        repo=repo,
        gh=gh,
        config=config,
        issue=args.issue,
        force=getattr(args, "force", False),
        parent_env=dict(os.environ),
        out=_err,
    )
    if command == "status":  # local state only: no prepare, no fetch, no gh, no lock
        print(stages.status(ctx))
        return 0
    interrupted = False
    try:
        stages.prepare(ctx, **_PREPARE_POLICY.get(command, {}))
        if command == "dismiss":
            stages.dismiss(ctx, args.finding, args.reason)
        else:
            getattr(stages, command)(ctx)
    except KeyboardInterrupt:
        interrupted = True  # leave the lock: its dead pid is how the next command knows to reset
        raise
    finally:
        # Only the lock this process took, and only if it is still ours: another container may be holding one
        # for the same issue, and clearing it would hand it two writers (state.RunLock.clear_if_owned).
        if ctx.holds_lock and not interrupted:
            RunLock.clear_if_owned(repo.factory_dir, args.issue)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
