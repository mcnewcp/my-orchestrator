"""Unit tests for `factory doctor` (design §6, §8, §13).

Everything is real except the model: a real git checkout under tmp_path, a real probe worktree, real stub binaries
on PATH, and the real `harness.build_env`, `repo.git`, `checks.run_checks` and `state.write_doctor_record`. Only
`harness.get_harness` is replaced — by a stub exposing `version()` and `run()` — so no `claude` or `codex` process
is ever launched. `GitHub.auth_ok` is stubbed for the same reason (no network).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory import __version__
from factory import doctor as doctor_module
from factory import harness as harness_module
from factory.config import Config, HarnessConfig
from factory.doctor import doctor, doctor_record_is_current
from factory.errors import HarnessError
from factory.harness import HarnessResult
from factory.repo import Repo

CLAUDE_VERSION = "9.9.9 (Claude Code)"
# `make` records every invocation in the directory it ran from, so tests can prove which check ran where.
MAKE_STUB = (
    '#!/bin/sh\nprintf \'%s|%s|%s|%s\\n\' "$PWD" "$CI" "$*" "$ANTHROPIC_API_KEY" >> make-ran.txt\n'
)
STUB_BINARIES = {
    "make": MAKE_STUB,
    "gh": "#!/bin/sh\nexit 0\n",
    "claude": "#!/bin/sh\nexit 0\n",
    "codex": "#!/bin/sh\nexit 0\n",
    "failing-check": '#!/bin/sh\necho "the suite is red" >&2\nexit 1\n',
}


class StubHarness:
    """What harness.get_harness returns: `version(env)` and `run(**kwargs)`, nothing else.

    `behaviours` is one dict per expected call: {"output": ..., "error": Exception, "write_probe_file": bool}.
    """

    def __init__(self, name: str = "claude", version: str = CLAUDE_VERSION, behaviours=None):
        self.name = name
        self._version = version
        self._behaviours = list(behaviours or [])
        self.calls: list[dict] = []

    def version(self, env: dict) -> str:
        return self._version

    def run(self, **kwargs):
        self.calls.append(kwargs)
        behaviour = self._behaviours.pop(0) if self._behaviours else {}
        if "error" in behaviour:
            raise behaviour["error"]
        if behaviour.get("write_probe_file", kwargs["mode"] == "write"):
            (kwargs["cwd"] / "probe.txt").write_text(
                behaviour.get("probe_content", "factory-probe\n")
            )
        output = behaviour.get("output", {"ok": True, "note": f"{kwargs['mode']} probe ran"})
        return HarnessResult(
            output=output,
            transcript_path=kwargs["transcript_path"],
            exit_code=0,
            cli_version=self._version,
        )


@dataclass
class Bench:
    repo: Repo
    config: Config
    parent_env: dict
    harness: StubHarness
    auth_status: tuple[bool, str] = (True, "github.com as octocat")

    def run(
        self,
        *,
        harness: str = "claude",
        auth: str = "subscription",
        model: str | None = None,
        write_probe: bool = True,
    ):
        return doctor(
            self.repo,
            self.config,
            harness=harness,
            auth=auth,
            model=model,
            parent_env=self.parent_env,
            write_probe=write_probe,
        )

    def records(self) -> dict:
        path = self.repo.factory_dir / "doctor.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def probe_worktree(self, harness: str = "claude") -> Path:
        return self.repo.factory_dir / "tmp" / "doctor" / harness / "wt"

    def make_runs(self, harness: str = "claude") -> list[str]:
        log = self.probe_worktree(harness) / "make-ran.txt"
        return log.read_text().splitlines() if log.exists() else []


def run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, stdin=subprocess.DEVNULL
    )


@pytest.fixture
def bench(tmp_path, monkeypatch) -> Bench:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    run_git(["init", "--quiet"], checkout)
    (checkout / "README.md").write_text("target repo\n")
    run_git(["add", "-A"], checkout)
    run_git(
        ["-c", "user.name=t", "-c", "user.email=t@e.com", "commit", "--quiet", "-m", "base"],
        checkout,
    )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUB_BINARIES.items():
        script = bindir / name
        script.write_text(body)
        script.chmod(0o755)
    (bindir / "git").symlink_to(shutil.which("git"))

    bench = Bench(
        repo=Repo(checkout),
        config=Config(checks=[["make", "test"], ["make", "lint"]], stage_timeout_min=1),
        parent_env={"PATH": str(bindir), "HOME": str(tmp_path / "home")},
        harness=StubHarness(),
    )
    monkeypatch.setattr(harness_module, "get_harness", lambda name: bench.harness)
    monkeypatch.setattr("factory.gh.GitHub.auth_ok", lambda self: bench.auth_status)
    return bench


def fails(report) -> list[str]:
    return [line for line in report.lines if line.startswith("FAIL:")]


def warns(report) -> list[str]:
    return [line for line in report.lines if line.startswith("warn:")]


# ---------------------------------------------------------------- all green


def test_all_green_writes_the_doctor_record(bench):
    report = bench.run()

    assert report.ok, report.render()
    assert fails(report) == []
    assert report.harness == "claude"
    assert report.auth == "subscription"
    assert report.cli_version == CLAUDE_VERSION
    record = bench.records()["claude:subscription"]
    assert record["ok"] is True
    assert record["factory_version"] == __version__
    assert record["cli_version"] == CLAUDE_VERSION
    assert record["checks"] == report.lines
    assert record["at"].endswith("Z")


def test_records_for_other_combinations_coexist(bench):
    bench.run(harness="claude", auth="subscription")
    bench.harness = StubHarness(name="codex", version="codex-cli 9.9.9")
    bench.run(harness="codex", auth="subscription")

    assert sorted(bench.records()) == ["claude:subscription", "codex:subscription"]


def test_probes_run_in_a_real_throwaway_worktree(bench):
    bench.run()

    worktree = bench.probe_worktree()
    assert (worktree / ".git").is_file(), (
        "the probe must see .git as a file, like every stage worktree"
    )
    assert (worktree / "probe.txt").read_text().strip() == "factory-probe"
    subprocess.run(["git", "status"], cwd=worktree, check=True, capture_output=True)


def test_the_probe_worktree_is_rebuilt_from_scratch(bench):
    bench.run()
    stale = bench.probe_worktree() / "left-over.txt"
    stale.write_text("from the previous doctor run\n")

    bench.run()

    assert not stale.exists()


def test_read_probe_then_write_probe_with_the_documented_arguments(bench):
    bench.run(model="claude-sonnet-4-6")

    assert [call["mode"] for call in bench.harness.calls] == ["read", "write"]
    read, write = bench.harness.calls
    assert read["schema_file"].name == "probe.json"
    assert read["model"] == "claude-sonnet-4-6"
    assert read["auth"] == "subscription"
    assert read["cwd"] == bench.probe_worktree()
    assert read["timeout_s"] == 60, "stage_timeout_min=1 binds below PROBE_TIMEOUT_S"
    assert read["max_turns"] == HarnessConfig().max_turns_read
    assert write["max_turns"] == HarnessConfig().max_turns_write
    assert read["prompt_file"].parent == bench.probe_worktree()
    assert "probe.txt" in write["prompt_file"].read_text()
    assert read["transcript_path"].parent == bench.repo.factory_dir / "transcripts" / "doctor"
    assert read["transcript_path"].parent.is_dir(), (
        "the harness must be able to open the transcript"
    )
    assert read["transcript_path"].name.startswith("claude-subscription-read-")


def test_the_probe_repo_supplies_an_agents_md(bench):
    bench.run()

    agents_md = bench.harness.calls[0]["agents_md"]
    assert agents_md == bench.probe_worktree() / "AGENTS.md"
    assert agents_md.is_file(), "api mode appends it with --append-system-prompt-file (design §8)"


def test_subscription_auth_forwards_no_provider_key(bench):
    bench.parent_env["ANTHROPIC_API_KEY"] = "sk-must-not-be-forwarded"

    report = bench.run(auth="subscription")

    assert report.ok, report.render()
    assert "ANTHROPIC_API_KEY" not in bench.harness.calls[0]["env"]


def test_only_the_first_configured_check_runs_and_it_runs_in_the_probe_worktree(bench):
    report = bench.run()

    runs = bench.make_runs()
    assert len(runs) == 1, "only config.checks[0] is probed"
    cwd, ci, args, provider_key = runs[0].split("|")
    assert Path(cwd).resolve() == bench.probe_worktree().resolve()
    assert ci == "1", "checks run with the checks env (design §8)"
    assert args == "test"
    assert provider_key == "", "checks never see a provider key (design §8)"
    assert (bench.probe_worktree() / "Makefile").read_text() == ".PHONY: test\ntest:\n\t@true\n"
    assert (
        "ok: write probe: check `make test` ran against a seeded no-op Makefile — "
        "proves the sandbox can exec make, not the repo's checks"
    ) in report.lines, "the line must not read as if the repository's own checks had passed"


def test_a_non_make_check_line_says_what_it_proves(bench, tmp_path):
    recorder = tmp_path / "bin" / "record-check"
    recorder.write_text("#!/bin/sh\nexit 0\n")
    recorder.chmod(0o755)
    bench.config.checks = [["record-check"]]

    report = bench.run()

    line = next(line for line in report.lines if "check `record-check`" in line)
    assert "no repository source" in line
    assert "seeded" not in line, "nothing was seeded for a check that is not `make`"


def test_a_non_make_check_is_run_exactly_as_configured(bench, tmp_path):
    recorder = tmp_path / "bin" / "record-check"
    recorder.write_text('#!/bin/sh\necho "$*" > ran.txt\n')
    recorder.chmod(0o755)
    bench.config.checks = [["record-check", "--fast"]]

    report = bench.run()

    assert report.ok, report.render()
    assert (bench.probe_worktree() / "ran.txt").read_text().strip() == "--fast"
    assert not (bench.probe_worktree() / "Makefile").exists(), (
        "only a make check needs seeded targets"
    )


def test_the_probes_go_through_the_real_harness_argv_builder(bench):
    """doctor drives the harness through harness.get_harness(...).run(...) — the same adapter, and therefore the
    same argv, a stage gets. A flag the CLI dropped has to fail here, not 40 minutes into a build."""
    argvs: list[list[str]] = []

    class RealArgvHarness(harness_module.ClaudeCode):
        def version(self, env: dict) -> str:
            return CLAUDE_VERSION

        def run(self, **kwargs):
            argvs.append(
                self.argv(
                    cwd=kwargs["cwd"],
                    prompt_file=kwargs["prompt_file"],
                    schema=json.loads(Path(kwargs["schema_file"]).read_text()),
                    mode=kwargs["mode"],
                    model=kwargs["model"],
                    auth=kwargs["auth"],
                    max_turns=kwargs["max_turns"],
                    max_budget_usd=kwargs["max_budget_usd"],
                    agents_md=kwargs["agents_md"],
                )
            )
            (kwargs["cwd"] / "probe.txt").write_text("factory-probe\n")
            return HarnessResult(
                output={"ok": True, "note": "probe ran"},
                transcript_path=kwargs["transcript_path"],
                exit_code=0,
                cli_version=CLAUDE_VERSION,
            )

    bench.harness = RealArgvHarness()
    bench.parent_env["ANTHROPIC_API_KEY"] = "sk-probe"

    report = bench.run(auth="api")

    assert report.ok, report.render()
    read_argv, write_argv = argvs
    assert read_argv[:3] == [
        "claude",
        "-p",
        "Follow the instructions in doctor-read-prompt.md exactly.",
    ]
    assert harness_module.CLAUDE_READ_ALLOWED in read_argv
    assert harness_module.CLAUDE_WRITE_ALLOWED in write_argv
    for argv in argvs:
        assert "--bare" in argv, "api mode, exactly as a stage runs it"
        assert "--append-system-prompt-file" in argv
        assert all(flag in argv for flag in harness_module.CLAUDE_ALWAYS)
    assert write_argv[write_argv.index("--max-turns") + 1] == str(HarnessConfig().max_turns_write)


def test_write_probe_is_skipped_when_asked(bench):
    report = bench.run(write_probe=False)

    assert report.ok
    assert [call["mode"] for call in bench.harness.calls] == ["read"]
    assert bench.make_runs() == []


# ---------------------------------------------------------------- binaries, versions, auth


def test_missing_harness_binary_fails_before_any_probe(bench, tmp_path):
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    bench.parent_env["PATH"] = str(only_git)

    report = bench.run()

    assert not report.ok
    assert fails(report) == [
        "FAIL: check command `make` is not on PATH",
        "FAIL: gh is not on PATH",
        "FAIL: harness binary `claude` is not on PATH",
    ]
    assert bench.harness.calls == []
    assert bench.records() == {}


def test_version_below_the_minimum_fails(bench):
    major, minor, patch = harness_module.CLAUDE_MIN_VERSION
    bench.harness = StubHarness(version=f"{major}.{minor}.{patch - 1} (Claude Code)")

    report = bench.run()

    assert not report.ok
    assert any("below the required" in line for line in fails(report))
    assert bench.harness.calls == [], (
        "no probe runs against a CLI missing --permission-prompts none"
    )
    assert bench.records() == {}


def test_pinned_version_mismatch_only_warns(bench):
    bench.config.harnesses["claude"] = HarnessConfig(pinned_version="2.1.263")

    report = bench.run()

    assert report.ok
    assert warns(report) == [
        f"warn: claude {CLAUDE_VERSION} does not match [harness.claude] pinned_version 2.1.263"
    ]
    assert bench.records()["claude:subscription"]["cli_version"] == CLAUDE_VERSION


def test_a_matching_pin_does_not_warn(bench):
    bench.config.harnesses["claude"] = HarnessConfig(pinned_version="9.9.9")

    assert warns(bench.run()) == []


def test_api_auth_without_a_key_fails_and_launches_nothing(bench):
    report = bench.run(auth="api")

    assert not report.ok
    assert fails(report) == [
        "FAIL: api auth: ANTHROPIC_API_KEY is not set; export it or use --auth subscription"
    ]
    assert bench.harness.calls == []
    assert bench.records() == {}


def test_api_auth_with_a_key_reports_the_forwarded_variables(bench):
    bench.parent_env["ANTHROPIC_API_KEY"] = "sk-test"
    bench.parent_env["HTTPS_PROXY"] = "http://proxy:3128"

    report = bench.run(auth="api")

    assert report.ok, report.render()
    assert (
        "ok: auth api: ANTHROPIC_API_KEY is set and is the only provider key the harness receives"
        in report.lines
    )
    assert "ok: network variables forwarded: HTTPS_PROXY" in report.lines
    assert bench.harness.calls[0]["env"]["ANTHROPIC_API_KEY"] == "sk-test"
    assert "claude:api" in bench.records()
    assert bench.make_runs()[0].endswith("|test|"), "the check never sees ANTHROPIC_API_KEY"


def test_gh_without_a_login_fails(bench):
    bench.auth_status = (False, "not logged in")

    report = bench.run()

    assert not report.ok
    assert "FAIL: gh is not authenticated: not logged in" in report.lines


def test_a_missing_check_binary_fails_and_names_it(bench):
    bench.config.checks = [["cargo", "test"]]

    report = bench.run()

    assert not report.ok
    assert "FAIL: check command `cargo` is not on PATH" in report.lines
    assert any("was not run" in line for line in warns(report))


def test_a_broken_checkout_fails_with_the_safe_directory_hint(bench, monkeypatch):
    from factory import repo as repo_module
    from factory.repo import GitResult

    real_git = repo_module.git

    def dubious(args, cwd, check=True, env=None):
        if args[:1] == ["status"]:
            return GitResult(
                128, "", f"fatal: detected dubious ownership in repository at '{cwd}'\n"
            )
        return real_git(args, cwd, check, env)

    monkeypatch.setattr(repo_module, "git", dubious)

    report = bench.run()

    assert not report.ok
    assert "safe.directory" in report.lines[-1]
    assert bench.harness.calls == []


def test_no_git_identity_is_reported_as_ok(bench, monkeypatch):
    from factory import repo as repo_module
    from factory.repo import GitResult

    real_git = repo_module.git

    def no_identity(args, cwd, check=True, env=None):
        if args == ["var", "GIT_AUTHOR_IDENT"]:
            return GitResult(128, "", "fatal: unable to auto-detect email address\n")
        return real_git(args, cwd, check, env)

    monkeypatch.setattr(repo_module, "git", no_identity)

    report = bench.run()

    assert report.ok, report.render()
    assert "ok: no git identity; commits use factory <factory@localhost>" in report.lines


# ---------------------------------------------------------------- probe failures


def test_a_failing_read_probe_stops_before_the_write_probe(bench):
    bench.harness = StubHarness(
        behaviours=[
            {
                "error": HarnessError(
                    "claude timed out after 60s", transcript_path=Path("/tmp/t.json")
                )
            }
        ]
    )

    report = bench.run()

    assert not report.ok
    assert "FAIL: read probe: claude timed out after 60s; transcript /tmp/t.json" in report.lines
    assert len(bench.harness.calls) == 1
    assert bench.make_runs() == []
    assert bench.records() == {}


def test_a_read_probe_that_returns_not_ok_fails(bench):
    bench.harness = StubHarness(
        behaviours=[{"output": {"ok": False, "note": "could not read the prompt"}}]
    )

    report = bench.run()

    assert not report.ok
    assert any("could not read the prompt" in line for line in fails(report))
    assert len(bench.harness.calls) == 1


def test_a_write_probe_that_leaves_no_file_fails(bench):
    bench.harness = StubHarness(behaviours=[{}, {"write_probe_file": False}])

    report = bench.run()

    assert not report.ok
    failure = fails(report)[0]
    assert "did not create" in failure and "probe.txt" in failure
    assert bench.make_runs() == [], (
        "no point running the check once the sandbox proved it cannot write"
    )
    assert bench.records() == {}


def test_a_stale_probe_file_cannot_pass_the_write_probe(bench):
    bench.run()  # leaves probe.txt in the probe worktree
    bench.harness = StubHarness(behaviours=[{}, {"write_probe_file": False}])

    report = bench.run()

    assert not report.ok
    assert any("did not create" in line for line in fails(report))


def test_a_failing_check_fails_the_write_probe(bench):
    bench.config.checks = [["failing-check"]]

    report = bench.run()

    assert not report.ok
    failure = fails(report)[0]
    assert failure.startswith(
        "FAIL: write probe: check `failing-check` failed in the probe worktree:"
    )
    assert "[exit 1]" in failure
    assert bench.records() == {}


def test_unexpected_probe_content_warns_but_passes(bench):
    bench.harness = StubHarness(behaviours=[{}, {"probe_content": "something else\n"}])

    report = bench.run()

    assert report.ok, report.render()
    assert any("expected 'factory-probe'" in line for line in warns(report))


# ---------------------------------------------------------------- the record predicate


def test_doctor_record_is_current_matches_only_the_same_combination():
    records = {
        "claude:api": {
            "ok": True,
            "factory_version": "0.0.1",
            "cli_version": "2.1.263",
            "at": "now",
            "checks": [],
        }
    }
    same = {"cli_version": "2.1.263", "factory_version": "0.0.1"}

    assert doctor_record_is_current(records, harness="claude", auth="api", **same)
    assert not doctor_record_is_current(records, harness="claude", auth="subscription", **same)
    assert not doctor_record_is_current(records, harness="codex", auth="api", **same)
    assert not doctor_record_is_current(
        records, harness="claude", auth="api", cli_version="2.1.264", factory_version="0.0.1"
    )
    assert not doctor_record_is_current(
        records, harness="claude", auth="api", cli_version="2.1.263", factory_version="0.0.2"
    )
    assert not doctor_record_is_current({}, harness="claude", auth="api", **same)
    assert not doctor_record_is_current(
        {"claude:api": "junk"}, harness="claude", auth="api", **same
    )
    stale = {"claude:api": dict(records["claude:api"], ok=False)}
    assert not doctor_record_is_current(stale, harness="claude", auth="api", **same)


def test_a_record_written_by_doctor_is_current(bench):
    bench.run()

    assert doctor_record_is_current(
        bench.records(),
        harness="claude",
        auth="subscription",
        cli_version=CLAUDE_VERSION,
        factory_version=__version__,
    )
    assert not doctor_record_is_current(
        bench.records(),
        harness="claude",
        auth="subscription",
        cli_version="1.0.0",
        factory_version=__version__,
    )


def test_module_constants_stay_in_step_with_the_probe_prompt():
    assert doctor_module.PROBE_FILE in doctor_module.PROBE_PROMPTS["write"]
    assert doctor_module.PROBE_CONTENT in doctor_module.PROBE_PROMPTS["write"]
    assert doctor_module.PROBE_FILE not in doctor_module.PROBE_PROMPTS["read"]
