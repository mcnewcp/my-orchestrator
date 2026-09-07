"""End-to-end gate proofs (design §17.2), driven only through the public CLI.

Every test here runs `factory <command> 42` through the `run_cli` fixture — no stage function is
called directly and nothing is monkeypatched — and then asserts against the four records the
factory actually leaves behind:

* the exit code and stderr (design §6: 0 continue, 1 failed, 2 needs human),
* the worktree and its git history (what was committed, what was thrown away),
* the bare `origin` (design rule 3: a red stage must not push),
* the fakes' journals — `harness_calls.jsonl` (was a model launched at all, with which flags and
  which prompt) and `gh_state.json` (PR draft/ready, comments and their idempotency markers).

The six proofs, in order: red checks cannot push · a protected-path edit fails `build` · a
test-file edit fails `fix` · `fix` refuses when code moved since the reviewed sha and `run` picks
review instead · `finalize` refuses with open Important findings · a red baseline parks with one
gate comment and re-parks silently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.gh import GATE_MARKER, SUMMARY_MARKER

ISSUE = 42
BRANCH = f"factory/{ISSUE}"
WORK = f"work/{ISSUE}"
PR_NUMBER = 117

FEATURE = "src/feature.py"
FEATURE_CODE = "def feature() -> int:\n    return 1\n"
TEST_FILE = "tests/test_app.py"
RED = "RED"  # the target repo's check (conftest CHECKS_PY) is red exactly while this file exists

SPEC_MARKDOWN = """\
## Problem
`src/app.py` exposes `add` and nothing else, so no caller can greet anyone.

## Proposed outcome
A new `src/feature.py` exposes `feature()`.

## Acceptance criteria
1. `python3 checks.py` exits 0.
"""

FINDING_TITLE = "feature() returns a constant instead of the greeting"


# ---------------------------------------------------------------- harness payloads


def spec_output(open_questions: tuple[str, ...] = ()) -> dict:
    """A schema-valid `spec` result (src/factory/schemas/spec.json)."""
    return {"markdown": SPEC_MARKDOWN, "open_questions": list(open_questions)}


def plan_output(*paths: str) -> dict:
    """A schema-valid `plan` result whose "## Files that change" lists `paths` verbatim, so the
    build's plan/diff-sync gate passes for exactly those paths and no others."""
    listed = "\n".join(f"- `{path}` — written by the build." for path in paths) or "- (nothing)"
    markdown = (
        "## Files that change\n"
        f"{listed}\n\n"
        "## Order of work\n"
        "1. Write the file.\n\n"
        "## Risks\n"
        "- None worth naming for a fixture.\n\n"
        "## Proof\n"
        "- `python3 checks.py` exits 0, which proves the repository's one check is green.\n"
    )
    return {"markdown": markdown}


def build_output(summary: str = "Wrote the feature and ran the checks.") -> dict:
    return {"summary": summary, "deviations": []}


def important_finding(
    *, file: str = FEATURE, line: int | None = 2, title: str = FINDING_TITLE
) -> dict:
    return {
        "severity": "important",
        "pass": "bugs",
        "file": file,
        "line": line,
        "title": title,
        "detail": "feature() ignores its inputs, so every caller gets 1.",
        "evidence": f"+    return 1  ({file}:{line})",
    }


def review_output(*, new: tuple[dict, ...] = (), updates: tuple[dict, ...] = ()) -> dict:
    return {
        "summary": "Read the diff; one pass per REVIEW.md section.",
        "updates": list(updates),
        "new": list(new),
    }


def resolved(finding_id: str) -> dict:
    return {
        "id": finding_id,
        "status": "resolved",
        "evidence": f"the diff now returns the greeting; {finding_id} is fixed.",
    }


def fix_output(*addressed: str) -> dict:
    return {
        "addressed": [{"id": fid, "how": "rewrote the return value"} for fid in addressed],
        "not_addressed": [],
    }


# ---------------------------------------------------------------- the rig


class Drive:
    """One issue driven through the CLI, plus read-only views of everything it wrote."""

    def __init__(self, run_cli, fakes, worktree, git_target):
        self._run_cli = run_cli
        self.fakes = fakes
        self.wt: Path = worktree(ISSUE)
        self._git = git_target

    # --- running commands
    def cli(self, *args) -> tuple[int, str, str]:
        return self._run_cli(*args)

    def ok(self, *args) -> str:
        """Run a command that must succeed; returns its stderr (the factory's progress log)."""
        code, _out, err = self.cli(*args)
        assert code == 0, f"`factory {' '.join(str(a) for a in args)}` exited {code}\n{err}"
        return err

    # --- stages, each scripting exactly the harness calls it makes
    def spec(self, open_questions: tuple[str, ...] = ()) -> str:
        self.fakes.queue(spec_output(open_questions))
        return self.ok("spec", ISSUE)

    def plan(self, *paths: str) -> str:
        self.fakes.queue(plan_output(*paths))
        return self.ok("plan", ISSUE)

    def build(self, writes: dict[str, str]) -> tuple[int, str, str]:
        self.fakes.queue({"output": build_output(), "writes": writes})
        return self.cli("build", ISSUE)

    def review(self, **kwargs) -> tuple[int, str, str]:
        self.fakes.queue(review_output(**kwargs))
        return self.cli("review", ISSUE)

    # --- git views
    def git(self, *args: str, cwd: Path | None = None) -> str:
        return self._git(*args, cwd=cwd or self.wt)

    @property
    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def origin_head(self, branch: str = BRANCH) -> str:
        """The sha `origin` holds for `branch` ("" when the branch was never pushed)."""
        line = self.git("ls-remote", "origin", f"refs/heads/{branch}")
        return line.split()[0] if line else ""

    @property
    def subjects(self) -> list[str]:
        return self.git("log", "--format=%s").splitlines()

    @property
    def dirty(self) -> list[str]:
        return self.git("status", "--porcelain=v1", "--untracked-files=all").splitlines()

    def commit(self, path: str, text: str, message: str) -> str:
        """A hand commit on the branch — the operator's input channel (design §7)."""
        target = self.wt / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.git("add", "--", path)
        self.git("commit", "--no-gpg-sign", "-m", message)
        return self.head

    # --- artifact views
    def read(self, rel: str) -> str:
        return (self.wt / rel).read_text(encoding="utf-8")

    @property
    def state(self) -> dict:
        return json.loads(self.read(f"{WORK}/state.json"))

    @property
    def findings(self) -> list[dict]:
        return json.loads(self.read(f"{WORK}/findings.json"))["findings"]

    # --- fake views
    @property
    def calls(self) -> list[dict]:
        return self.fakes.calls()

    @property
    def last_call(self) -> dict:
        calls = self.calls
        assert calls, "no harness invocation was recorded"
        return calls[-1]

    @property
    def pr(self) -> dict:
        pull = self.fakes.pr(PR_NUMBER)
        assert pull is not None, f"no PR #{PR_NUMBER} in gh_state.json"
        return pull

    @property
    def comments(self) -> list[str]:
        return [comment["body"] for comment in self.pr.get("comments", [])]

    def comments_matching(self, needle: str) -> list[str]:
        return [body for body in self.comments if needle in body]


@pytest.fixture
def drive(run_cli, fake_dir, worktree, git_target) -> Drive:
    return Drive(run_cli, fake_dir, worktree, git_target)


def flag_values(argv: list[str], name: str) -> list[str]:
    """Every value passed to `name` in a command line ("--allowedTools" is repeated per mode)."""
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == name and i + 1 < len(argv)]


def assert_env_is_the_allowlist(call: dict) -> None:
    """Design §8: harness subprocesses get no GitHub token, and (FAKES.md) not even the variable
    that tells the fakes where their instruction directory is."""
    assert call["env"]["GH_TOKEN"] == "absent"
    assert call["env"]["FACTORY_FAKE_DIR"] == "absent"
    assert call["env"]["ANTHROPIC_API_KEY"] == "absent"  # subscription mode inherits no key
    assert not [key for key in call["env_keys"] if key.startswith(("GH_", "GITHUB_"))]


def reach_review(drive: Drive) -> str:
    """spec → plan → build → review 1, leaving exactly one open Important finding (F1).

    Returns the reviewed sha, i.e. `state.reviews[-1].sha`."""
    drive.spec()
    drive.plan(FEATURE)
    code, _out, err = drive.build({FEATURE: FEATURE_CODE})
    assert code == 0, err
    reviewed = drive.head
    code, _out, err = drive.review(new=(important_finding(),))
    assert code == 0, err
    assert [f["id"] for f in drive.findings if f["status"] == "open"] == ["F1"]
    assert drive.state["reviews"][-1]["sha"] == reviewed
    assert len(drive.calls) == 4, "spec, plan, build and review are one harness session each"
    return reviewed


# ---------------------------------------------------------------- proof 1


def test_red_checks_cannot_push(drive: Drive):
    """Design rule 3 and §9's build row: the factory runs the checks itself and a red build is
    thrown away — no commit, no push, and the worktree back at HEAD."""
    drive.spec()
    drive.plan(FEATURE, RED)
    green_head = drive.head
    assert drive.origin_head() == green_head, (
        "plan must have pushed before we test that build did not"
    )

    code, _out, err = drive.build({FEATURE: FEATURE_CODE, RED: ""})

    assert code == 1
    assert "checks failed after build: python3 checks.py" in err

    # nothing reached the branch, locally or on the remote
    assert drive.head == green_head
    assert drive.origin_head() == green_head
    assert not any("build" in subject for subject in drive.subjects)
    assert "build" not in drive.state["stages"]

    # the worktree was reset to HEAD: the harness's writes are gone and nothing is dirty
    assert drive.dirty == []
    assert not (drive.wt / RED).exists()
    assert not (drive.wt / FEATURE).exists()

    # the session that produced them really was a write-mode build carrying the plan
    call = drive.last_call
    assert call["prompt_file"] == f"{WORK}/prompts/build-1.md"
    assert "## Files that change" in call["prompt_text"]
    assert FEATURE in call["prompt_text"]
    assert "Edit,Write" in "".join(flag_values(call["argv"], "--allowedTools"))
    assert "--bare" not in call["argv"]  # subscription auth (conftest factory.toml)
    assert_env_is_the_allowlist(call)


# ---------------------------------------------------------------- proof 2


def test_a_protected_path_edit_fails_the_build_stage(drive: Drive):
    """Design §9: protected paths never change, because the next session runs whatever they
    configure. The gate names the path and the stage keeps nothing."""
    drive.spec()
    drive.plan(FEATURE, "Makefile")  # even a plan that licenses it cannot license it
    before = drive.head
    makefile = drive.read("Makefile")

    code, _out, err = drive.build({FEATURE: FEATURE_CODE, "Makefile": "test:\n\techo nope\n"})

    assert code == 1
    assert "build changed paths it may not touch: Makefile" in err
    assert "protected paths" in err  # the hint

    assert drive.head == before
    assert drive.origin_head() == before
    assert drive.dirty == []
    assert drive.read("Makefile") == makefile
    assert not (drive.wt / FEATURE).exists()
    assert "build" not in drive.state["stages"]

    # the checks never ran: the allowed-edit rules are checked before anything else runs
    assert not (drive.wt / f"{WORK}/checks/build-1.log").exists()


# ---------------------------------------------------------------- proof 3


def test_a_test_file_edit_fails_the_fix_stage(drive: Drive):
    """Design §9: `fix` may not edit `test_paths`. A fixer must not be able to weaken the check on
    the code it is fixing."""
    reach_review(drive)
    before = drive.head
    original_test = drive.read(TEST_FILE)

    drive.fakes.queue(
        {
            "output": fix_output("F1"),
            "writes": {
                FEATURE: "def feature() -> int:\n    return 2\n",
                TEST_FILE: "def test_add():\n    assert True\n",
            },
        }
    )
    code, _out, err = drive.cli("fix", ISSUE)

    assert code == 1
    assert f"fix 1 changed paths it may not touch: {TEST_FILE}" in err
    assert "may not edit tests" in err  # the hint

    assert drive.head == before
    assert drive.origin_head() == before
    assert drive.dirty == []
    assert drive.read(TEST_FILE) == original_test
    assert drive.read(FEATURE) == FEATURE_CODE  # the code change went with it
    assert drive.state["fix_rounds"] == 0
    assert not (drive.wt / f"{WORK}/fix-1.json").exists()

    # the fixer was given the finding, and only the finding
    call = drive.last_call
    assert call["prompt_file"] == f"{WORK}/prompts/fix-1.md"
    assert "**F1**" in call["prompt_text"]
    assert FINDING_TITLE in call["prompt_text"]


# ---------------------------------------------------------------- proof 4


def test_fix_refuses_when_code_changed_since_the_reviewed_sha(drive: Drive):
    """Design §6: `fix` runs only while HEAD is the reviewed commit — findings against code that
    moved are stale. `run` resolves the same situation by reviewing again instead of fixing."""
    reviewed = reach_review(drive)
    calls_before = len(drive.calls)

    hand = drive.commit("src/app.py", "def add(a, b):\n    return a + b + 0\n", "hand fix")
    assert hand != reviewed

    code, _out, err = drive.cli("fix", ISSUE)

    assert code == 1
    assert f"code changed since review 1 ({reviewed[:12]})" in err
    assert f"run `factory review {ISSUE}`" in err  # the hint names the way out
    assert len(drive.calls) == calls_before, "fix must refuse before launching a model"
    assert drive.state["fix_rounds"] == 0

    # `run` faced with the same state reviews the operator's commit rather than burning a fix round
    drive.fakes.queue(review_output(updates=(resolved("F1"),)))
    err = drive.ok("run", ISSUE)

    assert "code changed since the last review; reviewing it before fixing" in err
    new_calls = drive.calls[calls_before:]
    assert [call["prompt_file"] for call in new_calls] == [f"{WORK}/prompts/review-2.md"]

    # and it reviewed in read mode, over the diff file, with the ledger's open finding in front of it
    review_call = new_calls[0]
    assert flag_values(review_call["argv"], "--allowedTools") == ["Read,Grep,Glob"]
    assert flag_values(review_call["argv"], "--disallowedTools") == [
        "Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch"
    ]
    assert review_call["schema"]["required"] == ["summary", "updates", "new"]
    assert ".factory/tmp/review-2.diff" in review_call["prompt_text"]
    assert "F1 [open · important · bugs]" in review_call["prompt_text"]
    assert "**NEEDS UPDATE**" in review_call["prompt_text"]

    assert len(drive.state["reviews"]) == 2
    assert drive.state["reviews"][-1]["sha"] == hand
    assert drive.state["fix_rounds"] == 0
    assert [f["status"] for f in drive.findings] == ["resolved"]

    # with nothing open, the same `run` finalized: the draft PR is ready and pushed
    assert drive.state["outcome"] == "done"
    assert drive.pr["isDraft"] is False
    assert drive.comments_matching(SUMMARY_MARKER.format(sha=drive.state["outcome_sha"]))
    assert drive.origin_head() == drive.head


# ---------------------------------------------------------------- proof 5


def test_finalize_refuses_with_open_important_findings(drive: Drive):
    """Design §9's finalize row: open Important == 0 is a gate, and the PR stays a draft."""
    reach_review(drive)
    before = drive.head
    calls_before = len(drive.calls)

    code, _out, err = drive.cli("finalize", ISSUE)

    assert code == 1
    assert "1 Important finding(s) are still open: F1" in err
    assert f'factory dismiss {ISSUE} <id> "reason"' in err  # the hint

    assert len(drive.calls) == calls_before, "finalize launches no model at all"
    assert drive.head == before
    assert drive.state["outcome"] is None
    assert drive.pr["isDraft"] is True
    assert not drive.comments_matching(SUMMARY_MARKER.format(sha=before))
    assert not (drive.wt / f"{WORK}/checks/finalize-1.log").exists()


# ---------------------------------------------------------------- proof 6


def test_a_red_baseline_parks_once_and_stays_parked(drive: Drive):
    """Design §11: baseline red before build is exit 2 with one PR comment naming the gate, and
    "parked stays parked" — a second `build` at the same HEAD re-parks with no new comment, no new
    commit and no harness call."""
    drive.spec()
    drive.plan(FEATURE)
    drive.commit(RED, "", "operator: break the checks")  # baseline is now red

    code, _out, err = drive.cli("build", ISSUE)

    assert code == 2
    assert "needs human: baseline_failing" in err
    assert f"{WORK}/checks/build-1-baseline.log" in err

    state = drive.state
    assert state["outcome"] == "needs_human:baseline_failing"
    outcome_sha = state["outcome_sha"]
    assert sorted(state["stages"]) == ["plan", "spec"]  # build never ran
    assert len(drive.calls) == 2, "the baseline runs before the session, so build launched nothing"

    # the evidence is committed and the park commit touched state.json alone
    baseline_log = drive.read(f"{WORK}/checks/build-1-baseline.log")
    assert "checks: RED present, failing" in baseline_log
    assert drive.subjects[0] == f"factory({ISSUE}): park baseline_failing"
    assert drive.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines() == [
        f"{WORK}/state.json"
    ]
    assert drive.git("rev-parse", "HEAD^") == outcome_sha
    assert drive.origin_head() == drive.head  # a gate is pushed, not kept local

    gate_marker = GATE_MARKER.format(gate="baseline_failing", sha=outcome_sha)
    assert len(drive.comments_matching(gate_marker)) == 1
    assert "baseline_failing" in drive.comments_matching(gate_marker)[0]

    parked_head = drive.head
    calls_before = len(drive.calls)
    comments_before = len(drive.comments)

    code, _out, err = drive.cli("build", ISSUE)

    assert code == 2
    assert "needs human: baseline_failing" in err
    assert drive.head == parked_head, "a re-park must not commit again"
    assert len(drive.calls) == calls_before, "a parked issue makes no harness call"
    assert len(drive.comments) == comments_before
    assert len(drive.comments_matching(gate_marker)) == 1
    assert drive.dirty == []
