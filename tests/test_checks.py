"""Unit tests for factory.checks — the deterministic gates (design §9).

Self-contained: no conftest fixtures, no fakes directory. Check commands are tiny stub scripts written into
tmp_path and run with sys.executable. run_checks calls harness.run_streaming for real; while that module is
still a skeleton the tests substitute an equivalent implementation of its documented contract
(`_reference_run_streaming`) and one test exercises the real one as soon as it exists.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from factory import checks
from factory.config import Config
from factory.errors import FactoryError, HarnessError

ENV = {"PATH": os.environ.get("PATH", "")}


# ---------------------------------------------------------------- helpers


def _reference_run_streaming(argv, *, cwd, env, timeout_s, stdout_path, stderr_path, what):
    """harness.run_streaming's documented contract (harness.py module docstring), used until it is implemented."""
    started = time.monotonic()
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            raise HarnessError(
                f"{what} timed out after {timeout_s}s", transcript_path=stdout_path
            ) from None
    return code, time.monotonic() - started


@pytest.fixture
def streaming(monkeypatch):
    monkeypatch.setattr(checks, "run_streaming", _reference_run_streaming)


def script(tmp_path: Path, name: str, body: str) -> list[str]:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return [sys.executable, str(path)]


def name_of(argv: list[str]) -> str:
    return " ".join(argv)


# ---------------------------------------------------------------- run_checks


def test_log_format_is_command_output_exit(tmp_path, streaming):
    talker = script(tmp_path, "talk.py", "print('hello')\n")
    silent = script(tmp_path, "quiet.py", "pass\n")
    result = checks.run_checks(tmp_path, Config(checks=[talker, silent]), ENV, 30)

    assert result.ok is True
    assert result.failed == []
    assert result.log == (f"$ {name_of(talker)}\nhello\n[exit 0]\n$ {name_of(silent)}\n[exit 0]\n")


def test_stderr_is_captured_after_stdout(tmp_path, streaming):
    both = script(tmp_path, "both.py", "import sys\nprint('out')\nprint('err', file=sys.stderr)\n")
    result = checks.run_checks(tmp_path, Config(checks=[both]), ENV, 30)

    assert result.ok is True
    assert result.log == f"$ {name_of(both)}\nout\nerr\n[exit 0]\n"


def test_checks_run_in_the_given_cwd(tmp_path, streaming):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    writer = script(tmp_path, "write.py", "open('made-here.txt', 'w').close()\n")
    result = checks.run_checks(worktree, Config(checks=[writer]), ENV, 30)

    assert result.ok is True
    assert (worktree / "made-here.txt").exists()


def test_stops_at_the_first_failure(tmp_path, streaming):
    first = script(tmp_path, "first.py", "print('ran first')\n")
    failing = script(
        tmp_path, "fail.py", "import sys\nprint('boom', file=sys.stderr)\nsys.exit(2)\n"
    )
    never = script(tmp_path, "never.py", "open('third-ran.txt', 'w').close()\n")
    result = checks.run_checks(tmp_path, Config(checks=[first, failing, never]), ENV, 30)

    assert result.ok is False
    assert result.failed == [name_of(failing)]
    assert not (tmp_path / "third-ran.txt").exists()
    assert result.log == (
        f"$ {name_of(first)}\nran first\n[exit 0]\n$ {name_of(failing)}\nboom\n[exit 2]\n"
    )


def test_missing_binary_is_a_failure_naming_the_command(tmp_path, streaming):
    missing = ["factory-no-such-binary-xyz", "--version"]
    after = script(tmp_path, "after.py", "open('after-ran.txt', 'w').close()\n")
    result = checks.run_checks(tmp_path, Config(checks=[missing, after]), ENV, 30)

    assert result.ok is False
    assert result.failed == [name_of(missing)]
    assert "cannot run" in result.log
    assert "factory-no-such-binary-xyz" in result.log
    assert not (tmp_path / "after-ran.txt").exists()


def test_timeout_is_a_failure_and_stops_the_run(tmp_path, streaming):
    sleeper = script(
        tmp_path, "sleep.py", "import time\nprint('starting', flush=True)\ntime.sleep(60)\n"
    )
    after = script(tmp_path, "after.py", "open('after-ran.txt', 'w').close()\n")
    result = checks.run_checks(tmp_path, Config(checks=[sleeper, after]), ENV, 1)

    assert result.ok is False
    assert result.failed == [name_of(sleeper)]
    assert "timed out after 1s" in result.log
    assert "starting" in result.log  # the partial output on disk is still part of the log
    assert not (tmp_path / "after-ran.txt").exists()


def test_no_checks_configured_is_green(tmp_path, streaming):
    result = checks.run_checks(tmp_path, Config(checks=[]), ENV, 30)

    assert result.ok is True
    assert result.failed == []
    assert result.log == "$ (no checks configured)\n[exit 0]\n"


def test_empty_check_entry_is_a_config_error(tmp_path, streaming):
    with pytest.raises(FactoryError) as excinfo:
        checks.run_checks(tmp_path, Config(checks=[[]]), ENV, 30)

    assert "checks[0]" in excinfo.value.message


def test_run_checks_passes_cwd_env_and_a_per_check_timeout(tmp_path, monkeypatch):
    seen = []

    def spy(argv, *, cwd, env, timeout_s, stdout_path, stderr_path, what):
        seen.append({"argv": argv, "cwd": cwd, "env": env, "timeout_s": timeout_s, "what": what})
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return 0, 0.0

    monkeypatch.setattr(checks, "run_streaming", spy)
    config = Config(checks=[["make", "test"], ["make", "lint"]])
    checks.run_checks(tmp_path, config, ENV, 900)

    assert [call["argv"] for call in seen] == [["make", "test"], ["make", "lint"]]
    assert [call["what"] for call in seen] == ["make test", "make lint"]
    assert {call["cwd"] for call in seen} == {tmp_path}
    assert {id(call["env"]) for call in seen} == {id(ENV)}
    assert all(call["timeout_s"] == 900 for call in seen)


def test_run_checks_uses_the_real_harness_run_streaming(tmp_path):
    """The production call path: no monkeypatching. Skips only while harness.py is still a skeleton."""
    probe = tmp_path / "probe.out"
    try:
        checks.run_streaming(
            [sys.executable, "-c", "print('probe')"],
            cwd=tmp_path,
            env=ENV,
            timeout_s=30,
            stdout_path=probe,
            stderr_path=tmp_path / "probe.err",
            what="probe",
        )
    except NotImplementedError:
        pytest.skip("harness.run_streaming is not implemented yet")

    ok = script(tmp_path, "ok.py", "print('green')\n")
    bad = script(tmp_path, "bad.py", "import sys\nsys.exit(1)\n")
    missing = ["factory-no-such-binary-xyz"]
    sleeper = script(tmp_path, "sleep.py", "import time\ntime.sleep(60)\n")

    green = checks.run_checks(tmp_path, Config(checks=[ok]), ENV, 30)
    red = checks.run_checks(tmp_path, Config(checks=[bad]), ENV, 30)
    absent = checks.run_checks(tmp_path, Config(checks=[missing]), ENV, 30)
    slow = checks.run_checks(tmp_path, Config(checks=[sleeper]), ENV, 1)

    assert green.ok is True
    assert "green" in green.log
    assert red.ok is False
    assert red.failed == [name_of(bad)]
    # A binary that cannot be started and a timeout are failures, never exceptions out of run_checks.
    assert absent.ok is False
    assert absent.failed == [name_of(missing)]
    assert "factory-no-such-binary-xyz" in absent.log
    assert slow.ok is False
    assert slow.failed == [name_of(sleeper)]
    assert "timed out after 1s" in slow.log


# ---------------------------------------------------------------- check logs


def test_check_log_path_and_write_check_log(tmp_path):
    path = checks.write_check_log(tmp_path, 42, "build", 1, "$ make test\n[exit 0]")

    assert path == tmp_path / "work" / "42" / "checks" / "build-1.log"
    assert path.read_text(encoding="utf-8") == "$ make test\n[exit 0]\n"

    baseline = checks.write_check_log(tmp_path, 42, "build", 1, "red\n", suffix="-baseline")
    assert baseline == checks.check_log_path(tmp_path, 42, "build", 1, "-baseline")
    assert baseline.name == "build-1-baseline.log"


def test_write_check_log_overwrites_the_same_round(tmp_path):
    checks.write_check_log(tmp_path, 42, "fix", 2, "first\n")
    path = checks.write_check_log(tmp_path, 42, "fix", 2, "second\n")

    assert path.read_text(encoding="utf-8") == "second\n"
    assert sorted(p.name for p in path.parent.iterdir()) == ["fix-2.log"]


def test_latest_check_log_none_when_absent(tmp_path):
    assert checks.latest_check_log(tmp_path, 42) == (None, "(none)")

    checks.write_check_log(tmp_path, 42, "build", 1, "x\n")
    (tmp_path / "work" / "7" / "checks").mkdir(parents=True)
    assert checks.latest_check_log(tmp_path, 7) == (None, "(none)")


def test_latest_check_log_prefers_the_newest_mtime(tmp_path):
    old = checks.write_check_log(tmp_path, 42, "finalize", 1, "old\n")
    new = checks.write_check_log(tmp_path, 42, "build", 1, "new\n")
    os.utime(old, (1_000, 1_000))
    os.utime(new, (2_000, 2_000))

    assert checks.latest_check_log(tmp_path, 42) == (new, "new\n")


def _same_mtime(*paths: Path) -> None:
    for path in paths:
        os.utime(path, (1_700_000_000, 1_700_000_000))


def test_latest_check_log_tiebreak_is_the_later_stage(tmp_path):
    build = checks.write_check_log(tmp_path, 42, "build", 1, "build\n")
    fix = checks.write_check_log(tmp_path, 42, "fix", 1, "fix\n")
    _same_mtime(build, fix)
    assert checks.latest_check_log(tmp_path, 42)[0] == fix

    final = checks.write_check_log(tmp_path, 42, "finalize", 1, "final\n")
    _same_mtime(build, fix, final)
    assert checks.latest_check_log(tmp_path, 42)[0] == final


def test_latest_check_log_tiebreak_is_the_highest_round_as_an_integer(tmp_path):
    two = checks.write_check_log(tmp_path, 42, "fix", 2, "two\n")
    ten = checks.write_check_log(tmp_path, 42, "fix", 10, "ten\n")
    _same_mtime(two, ten)

    assert checks.latest_check_log(tmp_path, 42) == (ten, "ten\n")


def test_latest_check_log_prefers_the_real_run_over_its_baseline(tmp_path):
    baseline = checks.write_check_log(tmp_path, 42, "build", 1, "baseline\n", suffix="-baseline")
    real = checks.write_check_log(tmp_path, 42, "build", 1, "real\n")
    _same_mtime(baseline, real)

    assert checks.latest_check_log(tmp_path, 42) == (real, "real\n")


def test_latest_check_log_ignores_unparseable_names_when_a_real_log_exists(tmp_path):
    good = checks.write_check_log(tmp_path, 42, "build", 1, "good\n")
    stray = good.parent / "notes.log"
    stray.write_text("stray\n", encoding="utf-8")
    _same_mtime(good, stray)

    assert checks.latest_check_log(tmp_path, 42) == (good, "good\n")


# ---------------------------------------------------------------- path predicates


@pytest.mark.parametrize(
    "path",
    [
        "Makefile",
        "GNUmakefile",
        "makefile",
        "MAKEFILE",
        "factory.toml",
        "AGENTS.md",
        "CLAUDE.md",
        "REVIEW.md",
        ".mcp.json",
        ".github",
        ".github/workflows/ci.yml",
        ".claude/settings.json",
        ".CLAUDE/settings.json",
        ".claude",
        ".devcontainer/Dockerfile",
        ".codex/config.toml",
        "./Makefile",
        # both CLIs load the instruction file of every directory they read, so a nested one configures
        # the next session exactly as the root one does
        "docs/AGENTS.md",
        "src/deep/nest/CLAUDE.md",
        "docs/agents.md",
    ],
)
def test_is_protected_true(path):
    assert checks.is_protected(path, Config()) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "Makefile.in",
        "GNUmakefile.in",
        ".githubbing/x",
        "sub/.github/x.yml",
        "sub/Makefile",  # only the root Makefile is the one `make` runs for the checks
        "docs/AGENTS.md.bak",
        "docs/notes/AGENTS.mdx",
        "",
        "work/42/plan.md",
    ],
)
def test_is_protected_false(path):
    assert checks.is_protected(path, Config()) is False


def test_is_protected_includes_config_entries():
    config = Config(protected_paths=["deploy/", "SECURITY.md"])

    assert checks.is_protected("deploy/Dockerfile", config) is True
    assert checks.is_protected("deploy", config) is True
    assert checks.is_protected("SECURITY.md", config) is True
    assert checks.is_protected("SECURITY.md.bak", config) is False
    # config entries are matched case-insensitively too
    assert checks.is_protected("Deploy/Dockerfile", config) is True
    assert checks.is_protected("security.md", config) is True


def test_every_name_gnu_make_looks_for_is_protected():
    """GNU make reads GNUmakefile, then makefile, then Makefile: renaming the gate is changing the gate."""
    assert set(checks.PROTECTED_PATHS) >= {"GNUmakefile", "makefile", "Makefile"}
    for name in ("GNUmakefile", "makefile", "Makefile"):
        assert checks.is_protected(name, Config()) is True


@pytest.mark.parametrize(
    "path",
    [
        ".venv/x",
        ".venv/lib/site-packages/a.py",
        "sub/.venv/x",
        ".factory/tmp/review-1.diff",
        "__pycache__/a.pyc",
        "src/__pycache__/a.pyc",
        "a.pyc",
        "src/deep/a.pyc",
        "software_factory.egg-info/PKG-INFO",
        "node_modules/pkg/index.js",
        "target/debug/x",
        ".pytest_cache/v/cache",
        ".ruff_cache/x",
        ".mypy_cache/x",
        ".tox/py312/x",
        "venv/bin/python",
    ],
)
def test_is_transient_true(path):
    assert checks.is_transient(path, Config()) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "tests/test_app.py",
        "work/42/plan.md",
        "src/pyc",
        "myvenv/x",
        "",
        "docs/target.md",
    ],
)
def test_is_transient_false(path):
    assert checks.is_transient(path, Config()) is False


def test_is_transient_includes_config_entries():
    config = Config(transient_paths=["build/", "*.log", "scratch/notes.txt"])

    assert checks.is_transient("build/lib/x.py", config) is True
    assert checks.is_transient("sub/build/x", config) is True
    assert checks.is_transient("out/run.log", config) is True
    assert checks.is_transient("scratch/notes.txt", config) is True
    assert checks.is_transient("scratch/other.txt", config) is False


@pytest.mark.parametrize(
    ("path", "prefixes", "expected"),
    [
        ("tests/test_app.py", ["tests/"], True),
        ("tests/test_app.py", ["tests"], True),
        ("tests", ["tests/"], True),
        ("testsuite/x.py", ["tests/"], False),
        ("src/tests/x.py", ["tests/"], False),
        ("work/42/state.json", ["work"], True),
        ("src/app.py", ["tests/", "work"], False),
        ("src/app.py", [], False),
        ("", ["work"], False),
    ],
)
def test_is_under(path, prefixes, expected):
    assert checks.is_under(path, prefixes) is expected


# ---------------------------------------------------------------- allowed-edit rules


def test_build_allowed_edits(tmp_path):
    changed = [
        "src/app.py",
        "tests/test_app.py",
        "work/42/plan.md",
        "work/42/prompts/build-1.md",
        ".venv/lib/x.py",
        "src/__pycache__/app.pyc",
    ]

    assert (
        checks.allowed_edit_violations(
            changed, stage="build", issue=42, config=Config(), ignore=["work/42/prompts/build-1.md"]
        )
        == []
    )


def test_build_violations_are_protected_paths_and_other_work_files():
    changed = [
        "src/app.py",
        "Makefile",
        ".github/workflows/ci.yml",
        "work/42/spec.md",
        "work/42/state.json",
        "work/7/plan.md",
        "work/42/plan.md",
    ]

    assert checks.allowed_edit_violations(changed, stage="build", issue=42, config=Config()) == [
        "Makefile",
        ".github/workflows/ci.yml",
        "work/42/spec.md",
        "work/42/state.json",
        "work/7/plan.md",
    ]


def test_fix_violations_are_tests_and_all_of_work():
    changed = ["src/app.py", "tests/test_app.py", "work/42/plan.md", "REVIEW.md", "docs/x.md"]

    assert checks.allowed_edit_violations(changed, stage="fix", issue=42, config=Config()) == [
        "tests/test_app.py",
        "work/42/plan.md",
        "REVIEW.md",
    ]


def test_fix_honours_configured_test_paths_and_ignores_transients():
    config = Config(test_paths=["spec/", "src/app_test.py"], transient_paths=["build/"])
    changed = ["tests/test_app.py", "spec/unit/x.py", "src/app_test.py", "build/x.o", "src/app.py"]

    assert checks.allowed_edit_violations(changed, stage="fix", issue=42, config=config) == [
        "spec/unit/x.py",
        "src/app_test.py",
    ]


def test_ignore_covers_the_factory_own_prompt_in_fix():
    changed = ["work/42/prompts/fix-2.md", "src/app.py"]

    assert (
        checks.allowed_edit_violations(
            changed, stage="fix", issue=42, config=Config(), ignore=["work/42/prompts/fix-2.md"]
        )
        == []
    )
    assert checks.allowed_edit_violations(changed, stage="fix", issue=42, config=Config()) == [
        "work/42/prompts/fix-2.md",
    ]


def test_violations_are_normalised_and_deduplicated():
    changed = ["./Makefile", "Makefile", "work/42/state.json", "work/42/state.json"]

    assert checks.allowed_edit_violations(changed, stage="build", issue=42, config=Config()) == [
        "Makefile",
        "work/42/state.json",
    ]


def test_a_protected_path_is_a_violation_for_every_write_stage():
    for stage in ("build", "fix"):
        assert checks.allowed_edit_violations(
            [".claude/hooks/pre.sh"], stage=stage, issue=42, config=Config()
        ) == [".claude/hooks/pre.sh"]


def test_a_transient_location_cannot_launder_a_protected_path():
    """The rules are evaluated over the path itself: is_transient() says which droppings the clean/dirty
    comparison ignores (deviation 12), not which edits a write stage is allowed to make."""
    config = Config(transient_paths=["vendor/"])
    changed = ["vendor/pkg/AGENTS.md", "vendor/pkg/index.js"]

    for stage in ("build", "fix"):
        assert checks.allowed_edit_violations(changed, stage=stage, issue=42, config=config) == [
            "vendor/pkg/AGENTS.md"
        ]


def test_a_transient_location_cannot_launder_a_test_path_or_a_work_path():
    config = Config(test_paths=["target/tests/"], transient_paths=["target/"])
    changed = ["target/tests/test_app.py", "target/debug/app", "work/42/state.json"]

    assert checks.allowed_edit_violations(changed, stage="fix", issue=42, config=config) == [
        "target/tests/test_app.py",
        "work/42/state.json",
    ]
    # build may write anything outside work/ — including a transient location
    assert checks.allowed_edit_violations(changed, stage="build", issue=42, config=config) == [
        "work/42/state.json"
    ]


def test_the_ignore_list_still_wins_over_every_rule():
    """`ignore` is the factory's own rendered prompt, which it wrote into work/<issue>/prompts/ itself."""
    changed = ["work/42/prompts/fix-2.md"]

    assert (
        checks.allowed_edit_violations(
            changed, stage="fix", issue=42, config=Config(), ignore=["./work/42/prompts/fix-2.md"]
        )
        == []
    )


# ---------------------------------------------------------------- plan gates


PLAN = """# Plan for issue 42

## Files that change

- `src/app.py` — add the flag
- src/new.py — new module

## Order of work

1. write the test

## Proof

- `pytest tests/test_app.py::test_flag`
"""


def test_paths_missing_from_plan():
    changed = ["src/app.py", "src/new.py", "src/sneaky.py", "work/42/plan.md"]

    assert checks.paths_missing_from_plan(changed, PLAN, issue=42) == ["src/sneaky.py"]


def test_paths_missing_from_plan_needs_the_exact_path():
    # A directory or a suffix is not the verbatim relative path.
    assert checks.paths_missing_from_plan(["src/app.py"], "changes under src/", issue=42) == [
        "src/app.py"
    ]
    assert checks.paths_missing_from_plan(["src/app.py"], "app.py", issue=42) == ["src/app.py"]
    assert (
        checks.paths_missing_from_plan(["src/app.py"], "we touch src/app.py here", issue=42) == []
    )
    # Nor is a longer path that merely contains it: the gate matches whole tokens, not substrings.
    assert checks.paths_missing_from_plan(["src/app.py"], "- `src/app.py.bak`", issue=42) == [
        "src/app.py"
    ]
    assert checks.paths_missing_from_plan(["src/app.py"], "- `docs/src/app.py`", issue=42) == [
        "src/app.py"
    ]
    # Markdown around a path is not part of the path.
    assert (
        checks.paths_missing_from_plan(["src/app.py"], "- (`src/app.py`), rewritten.", issue=42)
        == []
    )


def test_paths_missing_from_plan_matches_paths_containing_spaces():
    """The gate searches for the path literally, so nothing about how a path is spelled hides it: a token
    scan split `src/my report.py` at the space and failed every build that touched it."""
    plan = "## Files that change\n\n- `src/my report.py` — rewritten\n- 'docs/read me.md'\n"

    assert checks.paths_missing_from_plan(["src/my report.py"], plan, issue=42) == []
    assert checks.paths_missing_from_plan(["docs/read me.md"], plan, issue=42) == []
    # still bounded: a longer name that merely contains the listed one is a different file
    assert checks.paths_missing_from_plan(["src/my report.py.bak"], plan, issue=42) == [
        "src/my report.py.bak"
    ]
    assert checks.paths_missing_from_plan(["src/my report.p"], plan, issue=42) == [
        "src/my report.p"
    ]


@pytest.mark.parametrize(
    "plan",
    [
        "- `src/app.py`\n",
        "- (src/app.py)\n",
        '- "src/app.py"\n',
        "- [src/app.py](x)\n",
        "- src/app.py, and nothing else\n",
        "- we rewrite src/app.py.\n",  # a sentence stop is not part of the path
        "src/app.py",  # end of text
        "- `./src/app.py`\n",  # a plan may spell it relative to the root
        "- `src\\app.py`\n",  # or the windows way
    ],
)
def test_a_listed_path_is_recognised_through_its_markdown(plan):
    assert checks.paths_missing_from_plan(["src/app.py"], plan, issue=42) == []


@pytest.mark.parametrize(
    "plan",
    [
        "- `src/app.py.bak`\n",
        "- `docs/src/app.py`\n",
        "- `src/app.pyc`\n",
        "- `xsrc/app.py`\n",
        "- `src/app.py-old`\n",
        "",
    ],
)
def test_a_path_that_merely_contains_the_changed_one_does_not_list_it(plan):
    assert checks.paths_missing_from_plan(["src/app.py"], plan, issue=42) == ["src/app.py"]


def test_paths_missing_from_plan_deduplicates_and_skips_work():
    changed = ["work/42/plan.md", "work/9/spec.md", "src/x.py", "./src/x.py"]

    assert checks.paths_missing_from_plan(changed, "", issue=42) == ["src/x.py"]


def test_plan_has_required_sections_ok():
    assert checks.plan_has_required_sections(PLAN) == []


def test_plan_sections_are_case_insensitive_at_any_level_from_two():
    plan = "### files THAT   change\n\ntext\n\n###### Proof ###\n"

    assert checks.plan_has_required_sections(plan) == []


def test_plan_sections_missing():
    assert checks.plan_has_required_sections("") == ["## Files that change", "## Proof"]
    assert checks.plan_has_required_sections("## Proof\n") == ["## Files that change"]
    assert checks.plan_has_required_sections("## Files that change\n") == ["## Proof"]


def test_plan_level_one_heading_is_not_a_section():
    plan = "# Files that change\n# Proof\n"

    assert checks.plan_has_required_sections(plan) == ["## Files that change", "## Proof"]


def test_plan_section_text_must_match_exactly():
    plan = "## Files that change:\n## Proof of correctness\n"

    assert checks.plan_has_required_sections(plan) == ["## Files that change", "## Proof"]


# ---------------------------------------------------------------- tail


def test_tail_returns_short_text_unchanged():
    assert checks.tail("a\nb\n", 60) == "a\nb\n"
    assert checks.tail("", 60) == ""
    assert checks.tail("a\nb\nc\n", 0) == "a\nb\nc\n"


def test_tail_keeps_the_last_lines_and_says_what_it_dropped():
    text = "\n".join(str(i) for i in range(10)) + "\n"

    result = checks.tail(text, 3)

    assert result == "[... 7 earlier lines omitted ...]\n7\n8\n9\n"
