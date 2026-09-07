"""Shared fixtures and test-double wiring. Contract: tests/FAKES.md.

Every test runs with `tests/fakes` first on PATH, a temp HOME, a git identity in the environment,
a fresh FACTORY_FAKE_DIR, and no inherited provider credential — so a fake harness that records
`ANTHROPIC_API_KEY: "present"` says something about the factory, not about this workstation.

Each test gets its OWN copy of the three fakes, in `<FACTORY_FAKE_DIR>/bin`, first on PATH, next to a
`.fake_dir` pointer file. That pointer is how a fake finds its instruction directory once the factory
launches it with the allowlisted environment of design §8, which deliberately drops FACTORY_FAKE_DIR.
Copying (rather than pointing PATH at `tests/fakes`) keeps the pointer per test: nothing is written
inside the repository, and two pytest processes on one checkout cannot pop each other's queue.

Real git is used throughout (design deviation: only `claude`, `codex` and `gh` are faked), against
a bare `origin` in a temp directory, so worktree, push, fast-forward and force-with-lease semantics
are the real ones.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
FAKES_DIR = TESTS_DIR / "fakes"  # the originals; every test runs a private copy (see fake_env)
FAKE_BINARIES = ("claude", "codex", "gh")
POINTER_NAME = ".fake_dir"  # read by the fakes as <dirname(argv[0])>/../.fake_dir

QUEUE_FILE = "harness_queue.jsonl"
HARNESS_CALLS_FILE = "harness_calls.jsonl"
GH_STATE_FILE = "gh_state.json"
GH_CALLS_FILE = "gh_calls.jsonl"

#: Keys of a `harness_queue.jsonl` entry. A dict made only of these is taken as a whole entry;
#: anything else is taken as the harness's structured output and wrapped.
ENTRY_KEYS = frozenset(
    {
        "output",
        "writes",
        "deletes",
        "exit_code",
        "is_error",
        "sleep_s",
        "hang",
        "result",
        "num_turns",
        "permission_denials",
        "model_usage",
    }
)

#: Credentials and agent markers this workstation exports; dropped so every test starts from
#: "absent" and a test that wants one sets it explicitly (harness.FORBIDDEN_KEY_PATTERN).
SCRUBBED_ENV = re.compile(r"^(CLAUDE|CLAUDECODE|ANTHROPIC|CODEX|OPENAI|AI_AGENT|GH_|GITHUB_)")

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Factory Test",
    "GIT_AUTHOR_EMAIL": "factory-test@localhost",
    "GIT_COMMITTER_NAME": "Factory Test",
    "GIT_COMMITTER_EMAIL": "factory-test@localhost",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}

REPO_SLUG = "owner/name"

ISSUE_42_BODY = """\
## Problem
`greet` does not exist yet, so nothing can say hello.

## Proposed outcome
`src/app.py` gains a `greet(name)` returning `"hello <name>"`, covered by a test.

## Affected users and systems
Callers of `src/app.py` only.

## Constraints
Standard library only.

## Open questions
None.
"""

CHECKS_PY = """\
#!/usr/bin/env python3
# The target repository's one-command check. Red exactly when a file named RED exists in cwd,
# so a test can turn the baseline red by committing (or writing) that file.
import os
import sys

red = os.path.exists("RED")
print("checks: RED present, failing" if red else "checks: all green")
sys.exit(1 if red else 0)
"""

FACTORY_TOML = """\
[factory]
harness = "claude"
auth = "subscription"
base_branch = "main"
max_fix_rounds = 3
stage_timeout_min = 1
checks = [["python3", "checks.py"]]
test_paths = ["tests/"]
protected_paths = []

[poll]
label = "factory"
max_consecutive_failures = 3

[harness.claude]
model = ""
pinned_version = ""

[harness.codex]
model = ""
pinned_version = ""
"""

MAKEFILE = ".PHONY: test lint\ntest:\n\tpython3 checks.py\nlint:\n\t@echo lint ok\n"

AGENTS_MD = """\
# Agent instructions

Python 3.12+, standard library only. Keep functions small and name errors precisely.
Run `python3 checks.py` before you claim anything is green.
"""

CLAUDE_MD = "@AGENTS.md\n"

REVIEW_MD = """\
# Review policy

Review the diff, not the repository.

## Passes

1. **bugs** - wrong logic, unhandled error paths, a test that cannot fail.
2. **security** - injection, secrets in code or logs, a weakened check.
3. **compliance** - against `spec.md` and `plan.md`.

## Important vs nit

**Important** blocks the PR: you can name the failing input and its consequence.
**nit** is everything else. Nits never block and are never auto-fixed.

## Nit cap

At most 10 new nits per round.
"""

APP_PY = """\
# The target repository's only module.


def add(a: int, b: int) -> int:
    return a + b
"""

TEST_APP_PY = """\
# The target repository's only test. Never executed by the factory: the configured check is
# `python3 checks.py`. It exists so `test_paths = ["tests/"]` has something to protect.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from app import add  # noqa: E402


def test_add():
    assert add(1, 2) == 3
"""

GITIGNORE = ".factory/\n__pycache__/\n*.pyc\n"

TARGET_FILES = {
    "checks.py": CHECKS_PY,
    "factory.toml": FACTORY_TOML,
    "Makefile": MAKEFILE,
    "AGENTS.md": AGENTS_MD,
    "CLAUDE.md": CLAUDE_MD,
    "REVIEW.md": REVIEW_MD,
    ".gitignore": GITIGNORE,
    "src/app.py": APP_PY,
    "tests/test_app.py": TEST_APP_PY,
}


# ---------------------------------------------------------------- helpers


def atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file + os.replace, so no reader ever sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_jsonl(path: Path) -> list[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def as_queue_entry(obj: dict) -> dict:
    """A dict of only ENTRY_KEYS is a full queue entry; anything else is a structured output."""
    if not isinstance(obj, dict):
        raise TypeError(f"queue entries must be dicts, got {type(obj).__name__}")
    if obj and set(obj) <= ENTRY_KEYS:
        return dict(obj)
    return {"output": obj}


def fake_bin_dir(fake_dir: Path) -> Path:
    """`<fake_dir>/bin`, populated with executable copies of the three fakes. The pointer file sits one level
    up, exactly where a fake looks for it."""
    bin_dir = fake_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in FAKE_BINARIES:
        copy = bin_dir / name
        shutil.copy2(FAKES_DIR / name, copy)
        copy.chmod(0o755)
    return bin_dir


def pointer_path(fake_dir: Path) -> Path:
    """The `.fake_dir` file `<fake_dir>/bin/<fake>` reads to find `<fake_dir>`."""
    return fake_dir / POINTER_NAME


def git(*args: str, cwd: Path, check: bool = True) -> str:
    """Run real git in `cwd` and return its stdout, stripped."""
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True, stdin=subprocess.DEVNULL
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd} (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


class Fakes:
    """Scripts the fake binaries and reads back what they recorded.

    Doubles as the FACTORY_FAKE_DIR path: `Path(fakes)`, `fakes / "gh_state.json"` and
    `str(fakes)` all work.
    """

    def __init__(self, path: Path):
        self.path = path

    def __fspath__(self) -> str:
        return str(self.path)

    def __truediv__(self, other: str) -> Path:
        return self.path / other

    def __str__(self) -> str:
        return str(self.path)

    def __repr__(self) -> str:
        return f"Fakes({self.path})"

    # --- harness queue and calls
    def queue(self, *entries: dict) -> None:
        """Append harness invocations. Each argument is either a full queue entry (FAKES.md) or a
        structured output, which is wrapped as {"output": ...}. claude and codex share the queue."""
        with (self.path / QUEUE_FILE).open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(as_queue_entry(entry)) + "\n")

    def queue_remaining(self) -> list[dict]:
        """Entries no harness call has consumed yet."""
        return read_jsonl(self.path / QUEUE_FILE)

    def calls(self) -> list[dict]:
        """One record per claude/codex invocation (`--version` probes are not recorded)."""
        return read_jsonl(self.path / HARNESS_CALLS_FILE)

    # --- gh
    def gh_calls(self) -> list[dict]:
        return read_jsonl(self.path / GH_CALLS_FILE)

    def gh_state(self) -> dict:
        try:
            return json.loads((self.path / GH_STATE_FILE).read_text(encoding="utf-8"))
        except OSError:
            return {
                "repo": REPO_SLUG,
                "auth_ok": True,
                "issues": {},
                "labels": [],
                "prs": {},
                "next_pr_number": 117,
                "fail_next": [],
            }

    def set_gh_state(self, state: dict) -> None:
        atomic_write(self.path / GH_STATE_FILE, json.dumps(state, indent=2) + "\n")

    def pr(self, number: int) -> dict | None:
        return self.gh_state().get("prs", {}).get(str(number))

    def issue(self, number: int) -> dict | None:
        return self.gh_state().get("issues", {}).get(str(number))

    def fail_next(self, *prefixes: str) -> None:
        """Make the next `gh <prefix> …` call fail once, per prefix ("pr create")."""
        state = self.gh_state()
        state.setdefault("fail_next", []).extend(prefixes)
        self.set_gh_state(state)


# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def fake_env(tmp_path_factory, monkeypatch) -> Path:
    """Per-test environment: a private copy of the fakes first on PATH, temp HOME, git identity, clean
    credentials, and the `.fake_dir` pointer the fakes read when the factory strips FACTORY_FAKE_DIR.

    The fakes resolve their own directory through `Path(sys.argv[0]).resolve()`, so the copies must be real
    files — a symlink would resolve back to `tests/fakes` and share one pointer across processes again."""
    directory = tmp_path_factory.mktemp("fakedir")
    bin_dir = fake_bin_dir(directory)
    home = tmp_path_factory.mktemp("home")
    for name in list(os.environ):
        if SCRUBBED_ENV.match(name):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FACTORY_FAKE_DIR", str(directory))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
    for name, value in GIT_ENV.items():
        monkeypatch.setenv(name, value)
    atomic_write(pointer_path(directory), f"{directory}\n")
    return directory


@pytest.fixture
def fake_dir(fake_env) -> Fakes:
    """FACTORY_FAKE_DIR for this test, wrapped in the `Fakes` scripting helper."""
    return Fakes(fake_env)


@pytest.fixture
def origin(tmp_path_factory, fake_env) -> Path:
    """A bare repository standing in for GitHub's git side."""
    parent = tmp_path_factory.mktemp("remote")
    path = parent / "origin.git"
    git("init", "--bare", "--initial-branch=main", str(path), cwd=parent)
    return path


@pytest.fixture
def target(tmp_path_factory, origin, fake_dir) -> Path:
    """A clone of `origin` on `main`, seeded as FAKES.md lists, with issue 42 open and labelled."""
    parent = tmp_path_factory.mktemp("checkout")
    seed = parent / "seed"
    seed.mkdir()
    for rel, text in TARGET_FILES.items():
        path = seed / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (seed / "checks.py").chmod(0o755)
    git("init", "--initial-branch=main", ".", cwd=seed)
    git("add", "-A", cwd=seed)
    git("commit", "-m", "seed the target repository", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "-u", "origin", "main", cwd=seed)

    clone = parent / "target"
    git("clone", str(origin), str(clone), cwd=parent)

    fake_dir.set_gh_state(
        {
            "repo": REPO_SLUG,
            "auth_ok": True,
            "issues": {
                "42": {
                    "number": 42,
                    "title": "Add a greeting helper",
                    "body": ISSUE_42_BODY,
                    "url": f"https://github.com/{REPO_SLUG}/issues/42",
                    "labels": ["factory"],
                    "state": "OPEN",
                }
            },
            "labels": ["factory"],
            "prs": {},
            "next_pr_number": 117,
            "fail_next": [],
        }
    )
    return clone


@pytest.fixture
def git_target(target):
    """Run real git inside the target checkout (or any `cwd=` under it, e.g. a worktree)."""

    def _git_target(*args: str, cwd: Path | None = None, check: bool = True) -> str:
        return git(*args, cwd=cwd or target, check=check)

    return _git_target


@pytest.fixture
def worktree(target):
    """Where the factory puts issue N's worktree."""

    def _worktree(issue: int = 42) -> Path:
        return target / ".factory" / "worktrees" / str(issue)

    return _worktree


@pytest.fixture
def run_cli(target, monkeypatch):
    """Run `factory.cli.main(argv)` in-process with cwd = target. Returns (exit_code, out, err).

    `env` sets (or, with a None value, unsets) environment variables for the duration of the call.
    """

    def _run_cli(*args, env: dict | None = None) -> tuple[int, str, str]:
        from factory import cli  # imported late: cli.main may not exist when conftest is imported

        argv = [str(arg) for arg in args]
        out, err = io.StringIO(), io.StringIO()
        with monkeypatch.context() as patch:
            patch.chdir(target)
            for name, value in (env or {}).items():
                if value is None:
                    patch.delenv(name, raising=False)
                else:
                    patch.setenv(name, str(value))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(argv)
                except SystemExit as exc:
                    code = exc.code
        return (0 if code is None else int(code)), out.getvalue(), err.getvalue()

    return _run_cli
