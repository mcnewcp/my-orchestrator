"""`factory` command line: the parser surface and the exit-code mapping (design §6).

Self-contained: a real `git init` in tmp_path plus a minimal factory.toml is everything `cli.main` needs to reach
dispatch, and every stage function is replaced with a recorder, so no test here touches a harness, `gh`, or a remote.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from factory import cli, initcmd, stages
from factory.errors import FactoryError, GateViolation, HarnessError, NeedsHuman
from factory.state import RunLock

MINIMAL_TOML = """\
[factory]
harness = "claude"
auth = "subscription"
base_branch = "main"
checks = [["python3", "checks.py"]]

[poll]
label = "factory"
"""

REJECT_FORCE = ("accept", "review", "fix", "finalize", "run", "status", "abandon")


@pytest.fixture
def checkout(tmp_path, monkeypatch) -> Path:
    """A git checkout with a factory.toml, and cwd pointing at it."""
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=root, check=True)
    (root / "factory.toml").write_text(MINIMAL_TOML, encoding="utf-8")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def prepared(monkeypatch) -> dict:
    """Replace stages.prepare with a recorder; returns the dict it records into."""
    recorded: dict = {}

    def fake_prepare(ctx, **kwargs):
        recorded["kwargs"] = kwargs
        recorded["ctx"] = ctx
        ctx.prepared = True
        return ctx

    monkeypatch.setattr(stages, "prepare", fake_prepare)
    return recorded


def stage_returning(recorded: dict, name: str, monkeypatch, result=None):
    """Replace stages.<name> with a recorder that stores its ctx and returns/raises `result`."""

    def fake_stage(ctx):
        recorded[name] = ctx
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(stages, name, fake_stage)
    return recorded


# ---------------------------------------------------------------- parser surface


def test_version_is_the_only_stdout_of_a_non_status_command(capsys):
    assert cli.main(["version"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "factory 0.0.1"
    assert captured.err == ""


def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0


def test_no_command_prints_usage_and_fails_without_claiming_a_gate(capsys):
    assert cli.main([]) == 1  # never 2: 2 means "needs human"
    assert "usage: factory" in capsys.readouterr().err


def test_an_unknown_command_is_exit_1_not_argparse_exit_2(capsys):
    assert cli.main(["bogus"]) == 1
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["spec", "0"], ["spec", "-3"], ["spec", "forty-two"]])
def test_an_issue_number_must_be_a_positive_integer(argv, capsys):
    assert cli.main(argv) == 1
    assert "error: factory spec:" in capsys.readouterr().err


def test_every_command_carries_the_global_flags_in_either_position():
    parser = cli.build_parser()
    before = parser.parse_args(["--harness", "codex", "--force", "build", "42"])
    after = parser.parse_args(["build", "42", "--harness", "codex", "--force"])
    assert (before.harness, before.force, before.issue) == ("codex", True, 42)
    assert (after.harness, after.force, after.issue) == ("codex", True, 42)


def test_a_flag_before_the_command_survives_a_subparser_that_does_not_repeat_it():
    args = cli.build_parser().parse_args(["--force", "--model", "opus", "plan", "42"])
    assert args.force is True and args.model == "opus"


def test_dismiss_takes_an_issue_a_finding_and_a_reason():
    args = cli.build_parser().parse_args(["dismiss", "42", "F3", "not a real defect"])
    assert (args.issue, args.finding, args.reason) == (42, "F3", "not a real defect")


# ---------------------------------------------------------------- --force policy


@pytest.mark.parametrize("command", REJECT_FORCE)
def test_force_is_refused_for_every_command_but_spec_plan_build(command, capsys):
    assert cli.main(["--force", command, "42"]) == 1
    err = capsys.readouterr().err
    assert "--force is only accepted for spec, plan, build" in err
    assert command in err


@pytest.mark.parametrize("command", cli.FORCE_COMMANDS)
def test_force_reaches_the_context_for_spec_plan_and_build(
    command, checkout, prepared, monkeypatch
):
    stage_returning(prepared, command, monkeypatch)
    assert cli.main([command, "42", "--force"]) == 0
    assert prepared[command].force is True
    assert prepared["ctx"].issue == 42


def test_dismiss_refuses_an_empty_reason(capsys):
    assert cli.main(["dismiss", "42", "F3", "   "]) == 1
    assert "must not be empty" in capsys.readouterr().err


# ---------------------------------------------------------------- poll takes no flags


@pytest.mark.parametrize(
    "argv",
    [
        ["--harness", "codex", "poll"],
        ["poll", "--auth", "api"],
        ["--model", "opus", "poll"],
        ["--force", "poll"],
    ],
)
def test_poll_accepts_none_of_the_global_flags(argv, capsys):
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "poll` takes no --" in err or "--force is only accepted" in err


# ---------------------------------------------------------------- exit-code mapping


def test_needs_human_maps_to_exit_2_and_says_what_clears_it(
    checkout, prepared, monkeypatch, capsys
):
    gate = NeedsHuman("no_progress", "dismiss it or fix it by hand")
    stage_returning(prepared, "run", monkeypatch, result=gate)
    assert cli.main(["run", "42"]) == 2
    err = capsys.readouterr().err
    assert "needs human: no_progress" in err
    assert "dismiss it or fix it by hand" in err


def test_factory_error_maps_to_exit_1_and_prints_the_hint(checkout, prepared, monkeypatch, capsys):
    stage_returning(
        prepared, "plan", monkeypatch, result=FactoryError("plan is missing", "add ## Proof")
    )
    assert cli.main(["plan", "42"]) == 1
    err = capsys.readouterr().err
    assert "error: plan is missing" in err
    assert "add ## Proof" in err


def test_a_gate_violation_is_a_factory_error(checkout, prepared, monkeypatch, capsys):
    violation = GateViolation("build changed protected paths", ["Makefile"])
    stage_returning(prepared, "build", monkeypatch, result=violation)
    assert cli.main(["build", "42"]) == 1
    assert "error: build changed protected paths" in capsys.readouterr().err


def test_a_harness_error_prints_the_transcript_path(checkout, prepared, monkeypatch, capsys):
    failure = HarnessError("claude timed out after 60s", transcript_path=Path("/t/spec-1.json"))
    stage_returning(prepared, "spec", monkeypatch, result=failure)
    assert cli.main(["spec", "42"]) == 1
    err = capsys.readouterr().err
    assert "error: claude timed out after 60s" in err
    assert "transcript: /t/spec-1.json" in err


def test_an_unexpected_exception_maps_to_1_with_a_traceback(
    checkout, prepared, monkeypatch, capsys
):
    stage_returning(prepared, "review", monkeypatch, result=ZeroDivisionError("boom"))
    assert cli.main(["review", "42"]) == 1
    err = capsys.readouterr().err
    assert "ZeroDivisionError" in err and "Traceback" in err


def test_a_missing_factory_toml_is_a_factory_error(checkout, capsys):
    (checkout / "factory.toml").unlink()
    assert cli.main(["run", "42"]) == 1
    err = capsys.readouterr().err
    assert "no factory.toml" in err and "factory init" in err


def test_running_outside_a_checkout_is_a_factory_error(tmp_path, monkeypatch, capsys):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(outside)
    assert cli.main(["status", "42"]) == 1
    assert "is not inside a git checkout" in capsys.readouterr().err


# ---------------------------------------------------------------- dispatch policy


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("spec", {"need_state": False}),
        ("run", {"need_state": False}),
        ("abandon", {"need_state": False, "commit_operator_edits": False}),
        ("plan", {}),
        ("review", {}),
        ("finalize", {}),
    ],
)
def test_each_command_prepares_with_its_own_policy(
    command, expected, checkout, prepared, monkeypatch
):
    stage_returning(prepared, command, monkeypatch)
    assert cli.main([command, "42"]) == 0
    assert prepared["kwargs"] == expected


def test_status_writes_to_stdout_and_never_prepares(checkout, monkeypatch, capsys):
    def explode(*_args, **_kwargs):
        raise AssertionError("status must not call prepare (no fetch, no gh, no commit)")

    monkeypatch.setattr(stages, "prepare", explode)
    monkeypatch.setattr(stages, "status", lambda ctx: f"issue {ctx.issue} report")
    assert cli.main(["status", "42"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "issue 42 report"


def test_dismiss_passes_the_finding_and_reason_through(checkout, prepared, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        stages,
        "dismiss",
        lambda ctx, fid, reason: seen.update(id=fid, reason=reason, issue=ctx.issue),
    )
    assert cli.main(["dismiss", "42", "F3", "duplicate of F1"]) == 0
    assert seen == {"id": "F3", "reason": "duplicate of F1", "issue": 42}


def test_the_run_lock_is_cleared_even_when_the_stage_fails(checkout, monkeypatch):
    factory_dir = checkout / ".factory"

    def fake_prepare(ctx, **_kwargs):
        RunLock(issue=42, stage="build", pid=1234, started_at="now", worktree="wt").write(
            factory_dir
        )
        ctx.prepared = True
        return ctx

    monkeypatch.setattr(stages, "prepare", fake_prepare)
    monkeypatch.setattr(stages, "build", lambda ctx: (_ for _ in ()).throw(FactoryError("red")))
    assert cli.main(["build", "42"]) == 1
    assert not RunLock.path(factory_dir, 42).exists()


def test_the_global_flags_override_factory_toml(checkout, prepared, monkeypatch):
    stage_returning(prepared, "run", monkeypatch)
    assert cli.main(["--harness", "codex", "--auth", "api", "--model", "gpt-5", "run", "42"]) == 0
    config = prepared["run"].config
    assert (config.harness, config.auth, config.model) == ("codex", "api", "gpt-5")
    assert config.harnesses["claude"].model == ""  # --model applies to the selected harness only


def test_init_runs_without_a_factory_toml(checkout, monkeypatch, capsys):
    (checkout / "factory.toml").unlink()
    monkeypatch.setattr(
        initcmd, "init", lambda root, gh, **kw: ["wrote factory.toml", "nothing was committed"]
    )
    assert cli.main(["init"]) == 0
    captured = capsys.readouterr()
    assert (
        "wrote factory.toml" in captured.err
    )  # progress goes to stderr; stdout is for command output
    assert captured.out == ""


def test_doctor_exit_code_follows_the_report(checkout, monkeypatch, capsys):
    class Report:
        def __init__(self, ok):
            self.ok = ok

        def render(self):
            return "FAIL: claude is not on PATH" if not self.ok else "ok: everything"

    monkeypatch.setattr(cli, "run_doctor", lambda *a, **k: Report(False))
    assert cli.main(["doctor"]) == 1
    assert "FAIL: claude is not on PATH" in capsys.readouterr().err
    monkeypatch.setattr(cli, "run_doctor", lambda *a, **k: Report(True))
    assert cli.main(["doctor"]) == 0


def test_poll_gets_a_run_issue_closure_and_returns_its_exit_code(checkout, monkeypatch):
    seen: dict = {}

    def fake_poll(repo, gh, config, *, parent_env, run_issue, out):
        seen["ran"] = run_issue
        seen["auth"] = config.auth
        out("[factory] polling")
        return type("PollResult", (), {"exit_code": 2})()

    monkeypatch.setattr(cli, "run_poll", fake_poll)
    monkeypatch.setattr(stages, "run_issue", lambda *a, **k: 7)
    assert cli.main(["poll"]) == 2
    assert seen["ran"](42) == 7
    assert seen["auth"] == "subscription"  # straight from factory.toml, never a flag
