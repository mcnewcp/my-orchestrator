"""End-to-end plumbing: the whole factory driven only through the public CLI (design §17.2).

Every test here calls `run_cli(...)` — never a stage function — and then asserts against what a
real operator can see: the process exit code and stderr, the fake `gh`'s state and call log, the
git worktree, the bare `origin`, and what the fake harnesses recorded in `harness_calls.jsonl`.
Nothing is monkeypatched: the harnesses, `gh` and the target repository are the fakes and the real
git of `tests/FAKES.md`.

Each test proves one line of the definition of done:

1. an interrupted stage re-runs without a duplicate PR (§15)
2. `api` auth with no key fails instead of falling back to a saved login (§8)
3. `subscription` passes no provider key and no `GH_TOKEN`; `--bare` iff `api` (§8, deviations)
4. a deleted `.factory/` (a rebuilt host) resumes from `origin/factory/42` (§7 rule 6)
5. `status` reads local state only: no `gh`, no fetch (§6)
6. the happy path with claude: issue -> ready PR, every stage commit on origin (§1)
7. the same happy path with codex, recorded as such in `state.json` (§8)
8. `abandon` unwinds label, PR, branch and worktree, twice over (§6)
9. `poll` runs one issue to done, parks another, and skips both next tick (§12)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from factory import __version__
from factory.harness import get_harness

ISSUE = 42
SECOND_ISSUE = 43
BRANCH = f"factory/{ISSUE}"
SECOND_BRANCH = f"factory/{SECOND_ISSUE}"

# The artifacts a completed run must leave under work/<issue>/ on the branch (design §5).
REQUIRED_WORK_FILES = ("intent.md", "spec.md", "plan.md", "state.json", "findings.json")

SPEC_MD = """\
## Problem

`src/app.py` has no way to greet anyone.

## Proposed outcome

`greet(name)` returns `"hello <name>"`.

## Affected users and systems

Callers of `src/app.py` only.

## Constraints

Standard library only.

## Acceptance criteria

- `greet("world") == "hello world"`.

## Flagged concerns

None.
"""

PLAN_MD = """\
## Files that change

- `src/app.py` — add `greet(name)` next to `add`.

## Order of work

1. Add `greet` to `src/app.py`.

## Risks

None: the function is additive.

## Proof

- `python3 checks.py` exits 0 with `checks: all green`.
"""

GREET_APP = """\
# The target repository's only module.


def add(a: int, b: int) -> int:
    return a + b


def greet(name: str) -> str:
    return f"hello {name}"
"""

BUILD_ENTRY = {
    "output": {"summary": "Added greet(name) to src/app.py and ran the checks.", "deviations": []},
    "writes": {"src/app.py": GREET_APP},
}

CLEAN_REVIEW = {
    "summary": "The diff adds one pure function covered by the acceptance criterion. Fit to merge.",
    "complete": True,
    "updates": [],
    "new": [],
}


def spec_output(*open_questions: str) -> dict:
    return {"markdown": SPEC_MD, "open_questions": list(open_questions)}


def queue_happy_path(fake_dir) -> None:
    """spec -> plan -> build -> review, the four harness calls a clean `factory run` makes."""
    fake_dir.queue(spec_output(), {"markdown": PLAN_MD}, BUILD_ENTRY, CLEAN_REVIEW)


# ---------------------------------------------------------------- small helpers


def ok(result: tuple[int, str, str], expected: int = 0) -> tuple[int, str, str]:
    """Assert a CLI exit code, quoting the command's own stderr when it is not the expected one."""
    code, _out, err = result
    assert code == expected, f"expected exit {expected}, got {code}; stderr:\n{err}"
    return result


def git(*args: str, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True, stdin=subprocess.DEVNULL
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd} (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def work_dir(worktree: Path, issue: int = ISSUE) -> Path:
    return worktree / "work" / str(issue)


def state_json(worktree: Path, issue: int = ISSUE) -> dict:
    return read_json(work_dir(worktree, issue) / "state.json")


def subjects(repo: Path, ref: str) -> list[str]:
    return git("log", "--format=%s", ref, cwd=repo).splitlines()


def flag_value(argv: list[str], name: str) -> str | None:
    for index, arg in enumerate(argv):
        if arg == name:
            return argv[index + 1] if index + 1 < len(argv) else None
    return None


def stage_prompts(calls: list[dict]) -> list[str]:
    """The `work/<issue>/prompts/<stage>-<round>.md` each call was pointed at, in order."""
    return [str(call.get("prompt_file") or "") for call in calls]


def pr_for(fake_dir, branch: str) -> dict:
    prs = [pr for pr in fake_dir.gh_state()["prs"].values() if pr["headRefName"] == branch]
    assert len(prs) == 1, f"expected exactly one pull request for {branch}, got {prs}"
    return prs[0]


def comment_bodies(fake_dir, branch: str) -> list[str]:
    return [comment["body"] for comment in pr_for(fake_dir, branch).get("comments", [])]


def set_config(target: Path, key: str, value: str) -> None:
    """Rewrite one `[factory]` scalar in the checkout's factory.toml (config is read from the
    checkout, never from the worktree — design §14)."""
    path = target / "factory.toml"
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{key} = "):
            lines[index] = f'{key} = "{value}"'
            break
    else:
        raise AssertionError(f"{path} has no `{key} = ` line to rewrite")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def dead_pid() -> int:
    """A pid that has certainly exited and been reaped, so `RunLock.pid_alive()` is False."""
    proc = subprocess.Popen(["python3", "-c", ""])
    proc.wait()
    return proc.pid


def write_interrupted_lock(target: Path, stage: str, worktree: Path, issue: int = ISSUE) -> None:
    """`.factory/run/<issue>.json` as a killed stage leaves it: a pid nobody is running (§7)."""
    path = target / ".factory" / "run" / f"{issue}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "issue": issue,
                "stage": stage,
                "pid": dead_pid(),
                "started_at": "2026-09-06T00:00:00Z",
                "worktree": str(worktree),
                "last_error": None,
            }
        ),
        encoding="utf-8",
    )


def dirty_the_worktree(worktree: Path) -> None:
    """Half-finished work of the kind a killed harness leaves behind: one edit, one new file."""
    (worktree / "src" / "app.py").write_text("# half-written\n", encoding="utf-8")
    (worktree / "src" / "scratch.py").write_text("raise SystemExit(1)\n", encoding="utf-8")


def add_issue(fake_dir, number: int, title: str, body: str) -> None:
    state = fake_dir.gh_state()
    state["issues"][str(number)] = {
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/{state['repo']}/issues/{number}",
        "labels": ["factory"],
        "state": "OPEN",
    }
    fake_dir.set_gh_state(state)


def record_passing_doctor(target: Path, harness: str = "claude", auth: str = "api") -> None:
    """`poll`'s preflight runs `doctor` unless `.factory/doctor.json` already holds a passing record
    for this factory version and this harness CLI version (design §12 step 0). Seeding it keeps the
    tick's harness calls to the ones the stages make."""
    cli_version = get_harness(harness).version(dict(os.environ))
    path = target / ".factory" / "doctor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                f"{harness}:{auth}": {
                    "ok": True,
                    "factory_version": __version__,
                    "cli_version": cli_version,
                    "at": "2026-09-06T00:00:00Z",
                    "checks": [],
                }
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- 1. interrupted stage


def test_an_interrupted_stage_re_runs_without_a_duplicate_pr(fake_dir, target, run_cli, worktree):
    """§15: the transient run file with a dead pid means the stage was killed. The next command
    discards the partial work, resets the worktree, and re-runs — and because `state.pr` is on the
    branch, no second pull request can be created."""
    fake_dir.queue(spec_output(), {"markdown": PLAN_MD})
    ok(run_cli("spec", ISSUE))
    wt = worktree()
    first_pr = pr_for(fake_dir, BRANCH)
    head_after_spec = git("rev-parse", "HEAD", cwd=wt)

    # A `spec` killed after it had already committed and opened the PR.
    write_interrupted_lock(target, "spec", wt)
    dirty_the_worktree(wt)

    code, _out, err = run_cli("spec", ISSUE)

    assert code == 0, err
    assert "interrupted spec discarded" in err
    assert len(fake_dir.gh_state()["prs"]) == 1
    assert pr_for(fake_dir, BRANCH)["number"] == first_pr["number"]
    assert git("status", "--porcelain", cwd=wt) == ""
    assert not (wt / "src" / "scratch.py").exists()
    assert "half-written" not in (wt / "src" / "app.py").read_text(encoding="utf-8")
    assert git("rev-parse", "HEAD", cwd=wt) == head_after_spec
    assert len(fake_dir.calls()) == 1  # `spec already done`: no second session

    # A `plan` killed mid-session: the same discard, and this time the stage really does re-run.
    write_interrupted_lock(target, "plan", wt)
    dirty_the_worktree(wt)

    code, _out, err = run_cli("plan", ISSUE)

    assert code == 0, err
    assert "interrupted plan discarded" in err
    assert len(fake_dir.calls()) == 2
    assert (wt / "work" / str(ISSUE) / "plan.md").read_text(encoding="utf-8") == PLAN_MD
    assert git("status", "--porcelain", cwd=wt) == ""
    assert not (wt / "src" / "scratch.py").exists()
    assert len(fake_dir.gh_state()["prs"]) == 1
    assert "plan" in state_json(wt)["stages"]


# ---------------------------------------------------------------- 2. api auth with no key


def test_api_auth_with_no_key_fails_instead_of_using_the_saved_login(fake_dir, target, run_cli):
    """§8: "a missing key is a preflight failure, never a silent switch to a saved login". The
    factory must refuse before it launches anything — the harness's own fallback is never reached."""
    set_config(target, "auth", "api")
    fake_dir.queue(spec_output())  # never consumed: nothing may launch

    code, out, err = run_cli("spec", ISSUE, env={"ANTHROPIC_API_KEY": None})

    assert code == 1
    assert out == ""
    assert "ANTHROPIC_API_KEY is not set" in err
    assert "--auth subscription" in err
    assert not (Path(fake_dir) / "harness_calls.jsonl").exists()
    assert fake_dir.calls() == []
    assert fake_dir.gh_calls() == []  # it failed before it read GitHub, too
    assert fake_dir.queue_remaining() == [{"output": spec_output()}]
    assert git("branch", "--list", BRANCH, cwd=target) == ""


# ---------------------------------------------------------------- 3. what reaches the harness


def test_subscription_leaks_no_credential_and_bare_tracks_the_auth_mode(fake_dir, target, run_cli):
    """§8 "Environment" and the flag table: the harness environment is allowlist-built, so a
    provider key and `GH_TOKEN` present in the factory's own environment do not reach it in
    `subscription` mode; `--bare` appears exactly in `api` mode; and the two settings flags that
    keep the repository from configuring its own reviewer are always there."""
    secrets = {"GH_TOKEN": "ghp-not-for-agents", "ANTHROPIC_API_KEY": "sk-not-for-agents"}
    fake_dir.queue(spec_output(), {"markdown": PLAN_MD})

    ok(run_cli("spec", ISSUE, env=secrets))
    ok(run_cli("plan", ISSUE, "--auth", "api", env=secrets))

    subscription, api = fake_dir.calls()

    # subscription: no provider key, no GitHub token, and no way back to the test harness.
    assert subscription["env"]["ANTHROPIC_API_KEY"] == "absent"
    assert subscription["env"]["GH_TOKEN"] == "absent"
    assert subscription["env"]["CODEX_API_KEY"] == "absent"
    assert subscription["env"]["FACTORY_FAKE_DIR"] == "absent"
    assert subscription["env"]["CLAUDECODE"] == "absent"
    assert "--bare" not in subscription["argv"]

    # api: exactly the selected key, still no GitHub token.
    assert api["env"]["ANTHROPIC_API_KEY"] == "present"
    assert api["env"]["GH_TOKEN"] == "absent"
    assert api["env"]["CODEX_API_KEY"] == "absent"
    assert "--bare" in api["argv"]

    for call in (subscription, api):
        argv = call["argv"]
        assert flag_value(argv, "--setting-sources") == "user"
        assert "--strict-mcp-config" in argv
        assert flag_value(argv, "--permission-mode") == "dontAsk"
        assert flag_value(argv, "--permission-prompts") == "none"
        leaked = [
            name
            for name in call["env_keys"]
            if name.startswith(("GH_", "GITHUB_", "CODEX", "OPENAI", "CLAUDECODE", "FACTORY_"))
        ]
        assert leaked == []


# ---------------------------------------------------------------- 4. worktree recovery


def test_a_rebuilt_host_resumes_the_run_from_the_remote_branch(
    fake_dir, target, run_cli, worktree, origin
):
    """§7 rule 6: "nothing needed to continue a run exists only on the machine that started it".
    Wipe every host-local trace of issue 42 — `.factory/` and the local branch — and the next
    command rebuilds the worktree from `origin/factory/42`, runs the stage, and pushes it back so
    the host after this one can carry on in turn."""
    fake_dir.queue(spec_output(), {"markdown": PLAN_MD})
    ok(run_cli("spec", ISSUE))
    spec_sha = git("rev-parse", BRANCH, cwd=target)

    factory_dir = target / ".factory"
    shutil.rmtree(factory_dir)
    git("worktree", "prune", cwd=target)
    git("branch", "-D", BRANCH, cwd=target)
    assert not factory_dir.exists()
    assert git("branch", "--list", BRANCH, cwd=target) == ""

    code, _out, err = run_cli("plan", ISSUE)

    assert code == 0, err
    wt = worktree()
    assert wt.is_dir()
    assert git("config", f"branch.{BRANCH}.remote", cwd=target) == "origin"
    assert spec_sha in git("log", "--format=%H", "HEAD", cwd=wt).splitlines()
    # The artifacts came back from the remote, and the plan built on them.
    assert (work_dir(wt) / "intent.md").is_file()
    assert (work_dir(wt) / "spec.md").is_file()
    assert (work_dir(wt) / "plan.md").read_text(encoding="utf-8") == PLAN_MD
    assert git("status", "--porcelain", cwd=wt) == ""
    assert "plan" in state_json(wt)["stages"]
    # And the recovered stage is back on the remote for the next host.
    assert git("show", f"{BRANCH}:work/{ISSUE}/plan.md", cwd=origin) == PLAN_MD.rstrip("\n")
    assert f"factory({ISSUE}): plan" in subjects(origin, BRANCH)


# ---------------------------------------------------------------- 5. status is local only


def test_status_reads_local_state_with_no_network_of_any_kind(fake_dir, target, run_cli, origin):
    """§6: "from local state only; no network". Proved by taking the network away — `origin` is
    moved aside, so any fetch would fail — and by the fake `gh` recording no call at all."""
    fake_dir.queue(spec_output())
    ok(run_cli("spec", ISSUE))
    gh_calls_before = len(fake_dir.gh_calls())

    moved = origin.with_name("origin.gone")
    origin.rename(moved)
    try:
        code, out, err = run_cli("status", ISSUE)
    finally:
        moved.rename(origin)

    assert code == 0, err
    assert err == ""
    assert len(fake_dir.gh_calls()) == gh_calls_before
    assert f"issue {ISSUE} on {BRANCH}" in out
    assert f"https://github.com/owner/name/pull/{pr_for(fake_dir, BRANCH)['number']}" in out
    assert "spec   " in out and "plan   (not run)" in out
    assert "outcome: null" in out
    assert "parked: no" in out


# ---------------------------------------------------------------- 6. the happy path


def test_the_happy_path_with_claude_takes_an_issue_all_the_way_to_a_ready_pr(
    fake_dir, target, run_cli, worktree, origin
):
    """§1 end to end: one `factory run 42` from nothing produces spec, plan, build, a clean review
    and finalize — a ready pull request with a summary comment, the full artifact chain committed on
    the branch, and every stage commit on the remote."""
    queue_happy_path(fake_dir)

    code, _out, err = run_cli("run", ISSUE)

    assert code == 0, err
    wt = worktree()

    # One fresh session per stage, in order, each pointed at its committed prompt.
    calls = fake_dir.calls()
    assert stage_prompts(calls) == [
        f"work/{ISSUE}/prompts/spec-1.md",
        f"work/{ISSUE}/prompts/plan-1.md",
        f"work/{ISSUE}/prompts/build-1.md",
        f"work/{ISSUE}/prompts/review-1.md",
    ]
    assert {call["binary"] for call in calls} == {"claude"}
    # Each stage really was handed the previous stage's committed artifact.
    assert '`greet(name)` returns `"hello <name>"`.' in calls[1]["prompt_text"]
    assert "## Files that change" in calls[2]["prompt_text"]
    assert ".factory/tmp/review-1.diff" in calls[3]["prompt_text"]
    assert "--sandbox" not in calls[2]["argv"]  # claude, not codex
    assert "Edit,Write" in " ".join(calls[2]["argv"])  # build ran in write mode

    # The pull request is ready, with the review and summary comments on it.
    pull_request = pr_for(fake_dir, BRANCH)
    assert pull_request["isDraft"] is False
    assert pull_request["state"] == "OPEN"
    bodies = comment_bodies(fake_dir, BRANCH)
    assert any(body.startswith("<!-- factory:review:1:") for body in bodies)
    assert any(body.startswith("<!-- factory:summary:") for body in bodies)
    assert any("ready for review" in body for body in bodies)

    # The artifact chain is on the branch (design §5 layout).
    work = work_dir(wt)
    for name in REQUIRED_WORK_FILES:
        assert (work / name).is_file(), f"{work / name} is missing"
    assert (work / "prompts").is_dir()
    assert (work / "checks").is_dir()
    assert {path.name for path in (work / "prompts").iterdir()} == {
        "spec-1.md",
        "plan-1.md",
        "build-1.md",
        "review-1.md",
    }
    # A green baseline leaves no `-baseline` log: that one is only committed as a park's evidence.
    assert {path.name for path in (work / "checks").iterdir()} == {
        "build-1.log",
        "finalize-1.log",
    }
    assert "checks: all green" in (work / "checks" / "build-1.log").read_text(encoding="utf-8")
    assert (work / "build-1.json").is_file()
    assert (work / "review-1.json").is_file()

    state = state_json(wt)
    assert state["outcome"] == "done"
    assert sorted(state["stages"]) == ["build", "plan", "spec"]
    assert {record["harness"] for record in state["stages"].values()} == {"claude"}
    assert {record["auth"] for record in state["stages"].values()} == {"subscription"}
    assert len(state["reviews"]) == 1
    assert state["reviews"][0]["important_open"] == 0
    assert read_json(work / "findings.json")["findings"] == []

    # Nothing is left uncommitted and every stage commit reached the remote.
    assert git("status", "--porcelain", cwd=wt) == ""
    assert "greet" in (wt / "src" / "app.py").read_text(encoding="utf-8")
    assert git("rev-parse", "HEAD", cwd=wt) == git("rev-parse", BRANCH, cwd=origin)
    pushed = subjects(origin, BRANCH)
    for subject in (
        f"factory({ISSUE}): spec",
        f"factory({ISSUE}): plan",
        f"factory({ISSUE}): build",
        f"factory({ISSUE}): review 1",
        f"factory({ISSUE}): finalize",
    ):
        assert subject in pushed, f"{subject!r} never reached origin: {pushed}"
    assert "greet" in git("show", f"{BRANCH}:src/app.py", cwd=origin)


# ---------------------------------------------------------------- 7. the same, with codex


def test_the_happy_path_with_codex_records_the_harness_it_used(
    fake_dir, target, run_cli, worktree, origin
):
    """§8: the two harnesses are interchangeable behind one protocol, and `state.json` records which
    one produced each stage (so a cross-model review is legible after the fact)."""
    queue_happy_path(fake_dir)

    code, _out, err = run_cli("run", ISSUE, "--harness", "codex")

    assert code == 0, err
    wt = worktree()
    calls = fake_dir.calls()
    assert {call["binary"] for call in calls} == {"codex"}
    assert stage_prompts(calls) == [
        f"work/{ISSUE}/prompts/spec-1.md",
        f"work/{ISSUE}/prompts/plan-1.md",
        f"work/{ISSUE}/prompts/build-1.md",
        f"work/{ISSUE}/prompts/review-1.md",
    ]
    assert flag_value(calls[0]["argv"], "--sandbox") == "read-only"
    assert flag_value(calls[2]["argv"], "--sandbox") == "workspace-write"

    state = state_json(wt)
    assert state["outcome"] == "done"
    assert {record["harness"] for record in state["stages"].values()} == {"codex"}
    assert {record["cli_version"] for record in state["stages"].values()} == {"codex-cli 9.9.9"}
    assert pr_for(fake_dir, BRANCH)["isDraft"] is False
    assert git("rev-parse", "HEAD", cwd=wt) == git("rev-parse", BRANCH, cwd=origin)


# ---------------------------------------------------------------- 8. abandon


def test_abandon_unwinds_everything_and_is_safe_to_repeat(
    fake_dir, target, run_cli, worktree, origin
):
    """§6: "remove the label, close the draft PR, delete branch and worktree — in that order", and
    §6 again: "every command is idempotent and re-runnable"."""
    fake_dir.queue(spec_output())
    ok(run_cli("spec", ISSUE))
    number = pr_for(fake_dir, BRANCH)["number"]
    assert worktree().is_dir()
    assert git("ls-remote", "--heads", str(origin), BRANCH, cwd=target) != ""

    code, _out, err = run_cli("abandon", ISSUE)

    assert code == 0, err
    assert fake_dir.issue(ISSUE)["labels"] == []
    assert fake_dir.pr(number)["state"] == "CLOSED"
    assert any(f"factory abandon {ISSUE}" in body for body in comment_bodies(fake_dir, BRANCH))
    assert not worktree().exists()
    assert git("branch", "--list", BRANCH, cwd=target) == ""
    assert git("ls-remote", "--heads", str(origin), BRANCH, cwd=target) == ""
    assert not (target / ".factory" / "run" / f"{ISSUE}.json").exists()

    # Nothing left to undo, and undoing it again is not an error.
    code, _out, err = run_cli("abandon", ISSUE)

    assert code == 0, err
    assert fake_dir.issue(ISSUE)["labels"] == []
    assert fake_dir.pr(number)["state"] == "CLOSED"
    assert not worktree().exists()
    assert len(fake_dir.calls()) == 1  # neither abandon started a harness session


# ---------------------------------------------------------------- 9. poll


def test_poll_runs_one_issue_to_done_parks_another_and_skips_both_next_tick(
    fake_dir, target, run_cli, worktree
):
    """§12: one tick, sequential, ascending. 42 runs to a ready PR (exit 0); 43 stops on the
    open-questions gate (exit 2); neither is an exit 1, so the tick exits 0. The second tick
    reads the same local state and makes no model call at all (§11 "parked stays parked")."""
    set_config(target, "auth", "api")
    record_passing_doctor(target)
    add_issue(
        fake_dir,
        SECOND_ISSUE,
        "Decide the retry policy",
        "## Problem\nRetries are unspecified.\n\n## Open questions\nHow many?\n",
    )
    queue_happy_path(fake_dir)  # issue 42, ascending order
    fake_dir.queue(spec_output("How many retries should the client make before giving up?"))
    api_key = {"ANTHROPIC_API_KEY": "sk-test-key"}

    code, _out, err = run_cli("poll", env=api_key)

    assert code == 0, err
    assert f"issue {ISSUE}: new; running" in err
    assert f"issue {SECOND_ISSUE}: new; running" in err
    # 0 and 2 aggregate to 0; only an exit 1 would make the tick itself fail (§12 step 5).
    assert f"issue {ISSUE}: exit 0" in err
    assert f"issue {SECOND_ISSUE}: exit 2" in err
    assert len(fake_dir.calls()) == 5
    assert {call["env"]["ANTHROPIC_API_KEY"] for call in fake_dir.calls()} == {"present"}

    done = state_json(worktree(ISSUE))
    assert done["outcome"] == "done"
    assert pr_for(fake_dir, BRANCH)["isDraft"] is False

    parked = state_json(worktree(SECOND_ISSUE), SECOND_ISSUE)
    assert parked["outcome"] == "needs_human:open_questions"
    assert parked["spec_open_questions"] == [
        "How many retries should the client make before giving up?"
    ]
    assert pr_for(fake_dir, SECOND_BRANCH)["isDraft"] is True  # a parked run never goes ready
    gate = comment_bodies(fake_dir, SECOND_BRANCH)
    assert any(body.startswith("<!-- factory:gate:open_questions:") for body in gate)
    assert any("needs human (`open_questions`)" in body for body in gate)

    # Second tick: both issues are decided from local state, so nothing runs.
    code, _out, err = run_cli("poll", env=api_key)

    assert code == 0, err
    assert f"issue {ISSUE}: skipped (done)" in err
    assert f"issue {SECOND_ISSUE}: skipped (parked)" in err
    assert len(fake_dir.calls()) == 5
    assert fake_dir.queue_remaining() == []


def test_poll_refuses_the_attended_auth_mode_before_it_reads_github(fake_dir, target, run_cli):
    """The other end of proof 9's preflight (§12 step 0, §8): `subscription` is the attended mode
    and a timer must never reach for a saved login. The refusal happens before the label query, so
    a misconfigured tick costs nothing."""
    set_config(target, "auth", "subscription")  # the fixture's default, made explicit

    code, _out, err = run_cli("poll")

    assert code == 1
    assert 'poll requires auth = "api"' in err
    assert fake_dir.gh_calls() == []
    assert fake_dir.calls() == []
