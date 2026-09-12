"""The attended CLI and its stable exit codes."""

import argparse
import json
import subprocess
from pathlib import Path

from . import __version__, present
from .config import EFFORTS, TEMPLATE, load_config
from .gh import GitHub
from .harness import doctor
from .locks import file_lock, issue_lock
from .repo import Repo
from .stages import Engine, NeedsHuman
from .state import load_json


REVIEW = """# Review policy

nit_cap: 5

An Important finding is a concrete defect that blocks acceptance: a bug, a security
problem, or a failure to meet spec.md or plan.md. Anything else worth saying is a nit,
and nits never block completion. Skip style preferences, speculative risks, pre-existing
issues outside this change, and requests to expand the agreed scope.
"""


def positive(value):
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("issue must be a positive integer")


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--harness", choices=("claude", "codex"), default=argparse.SUPPRESS)
    common.add_argument("--auth", choices=("subscription", "api"), default=argparse.SUPPRESS)
    common.add_argument("--model", default=argparse.SUPPRESS)
    common.add_argument("--effort", choices=EFFORTS, default=argparse.SUPPRESS)
    common.add_argument("--force", action="store_true", default=argparse.SUPPRESS)
    result = argparse.ArgumentParser(prog="factory", parents=[common], description="Turn one GitHub issue into a PR for human review.")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("init", "doctor", "poll"):
        commands.add_parser(name, parents=[common] if name != "poll" else [])
    for name in ("spec", "accept", "plan", "build", "review", "fix", "finalize", "run", "status", "dismiss", "abandon"):
        command = commands.add_parser(name, parents=[common])
        command.add_argument("issue", type=positive)
        if name == "dismiss":
            command.add_argument("finding")
            command.add_argument("reason")
        if name == "status":
            command.add_argument("--json", action="store_true", dest="as_json",
                                 help="print the raw local state instead of a readable summary")
    return result


def init(repo, github):
    config = load_config(repo.root, optional=True)
    label = config["poll"]["label"]
    files = {
        "factory.toml": TEMPLATE,
        "REVIEW.md": REVIEW,
        "AGENTS.md": "# Repository instructions\n\nRun `make test` and `make lint`. Keep changes bounded to the approved plan.\n",
        "CLAUDE.md": "@AGENTS.md\n",
        ".github/ISSUE_TEMPLATE/intent.md": (
            "---\nname: Factory intent\nabout: A bounded problem for the software factory\n"
            "title: ''\nlabels: " + json.dumps([label]) + "\n---\n\n"
            "## Problem\n\n## Proposed outcome\n\n## Affected users and systems\n\n"
            "## Constraints\n\n## Open questions\n"
        ),
    }
    for name, content in files.items():
        path = repo.root / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            present.created(name)
    ignore = repo.root / ".gitignore"
    contents = ignore.read_text() if ignore.exists() else ""
    if not any(line.strip() in (".factory/", "/.factory/", ".factory", "/.factory") for line in contents.splitlines()):
        ignore.write_text(contents.rstrip("\n") + "\n.factory/\n")
    github.ensure_label(label)
    present.initialized()


def status(repo, issue, *, as_json=False):
    state = load_json(repo.issue_dir(issue) / "state.json", {})
    cwd = repo.worktree(issue, create=False)
    if cwd:
        head = repo.head(cwd)
    elif repo.branch_exists(issue):
        head = repo.git("rev-parse", f"refs/heads/factory/{issue}")
    else:
        head = None
    report = {"issue": issue, "head": head, "worktree": str(cwd) if cwd else None,
              "state": state, "in_flight": load_json(repo.local_dir / "run" / f"{issue}.json"),
              "note": present.LOCAL_ONLY_NOTE}
    if as_json:
        present.json_document(report)
    else:
        present.status_summary(report, load_json(repo.issue_dir(issue) / "findings.json", {"findings": []}),
                               artifacts=repo.issue_dir(issue),
                               transcripts=repo.local_dir / "transcripts")
    return 0


def execute_issue(repo, github, config, issue, command="run", *, harness=None, auth=None,
                  model=None, effort=None, force=False, finding=None, reason=None):
    with issue_lock(repo, issue, command):
        elapsed = present.timer()
        engine = Engine(repo, github, config, issue, harness=harness, auth=auth, model=model, effort=effort)
        engine.prepare(create=True)
        if command == "abandon":
            # Deliberate ordering: discovery stops first, then PR, then Git state.
            github.remove_label(issue, config["poll"]["label"])
            if engine.state.get("pr"):
                github.close(engine.state["pr"]["number"])
            repo.abandon(issue)
            return 0
        try:
            if force:
                engine.rewind(command)
            if command == "dismiss":
                engine.dismiss(finding, reason)
            else:
                getattr(engine, command)()
        except NeedsHuman as exc:
            present.gate(issue, exc.reason, exc.guidance)
            return 2
        except BaseException:
            engine.rollback()
            raise
        present.completed(issue, command, pr_url=(engine.state.get("pr") or {}).get("url"),
                          artifacts=engine.work, transcripts=repo.local_dir / "transcripts",
                          seconds=elapsed())
        return 0


def main(argv=None):
    args = parser().parse_args(argv)
    overrides = {key: getattr(args, key) for key in ("harness", "auth", "model", "effort", "force") if hasattr(args, key)}
    if args.command == "poll" and overrides:
        present.poll_overrides_rejected()
        return 1
    if overrides.get("force") and args.command not in ("spec", "plan", "build"):
        present.force_unsupported()
        return 1
    try:
        repo = Repo(Path.cwd())
        config = load_config(repo.root, optional=args.command == "init")
        repo.base_branch = config["factory"]["base_branch"]
        github = GitHub(repo.root)
        if args.command == "init":
            init(repo, github)
            return 0
        if args.command == "doctor":
            with file_lock(repo.local_dir / "run" / "harness.lock"):
                result = doctor(repo.root, config, harness_override=overrides.get("harness"),
                                auth_override=overrides.get("auth"), model_override=overrides.get("model"),
                                effort_override=overrides.get("effort"))
            present.json_document(result)
            return 0 if result["passed"] else 1
        if args.command == "status":
            return status(repo, args.issue, as_json=args.as_json)
        if args.command == "poll":
            from .poll import poll
            return poll(repo, github, config)
        return execute_issue(repo, github, config, args.issue, args.command, **overrides,
                             finding=getattr(args, "finding", None), reason=getattr(args, "reason", None))
    except (RuntimeError, ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        present.failure(exc)
        return 1
    except KeyboardInterrupt:
        present.interrupted()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
