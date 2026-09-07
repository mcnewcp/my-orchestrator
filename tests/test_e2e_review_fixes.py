"""End-to-end proofs that an operator's action always re-opens the loop (design §11, §15, §17.2).

Every test drives the factory only through `factory <command>` (the conftest `run_cli` fixture, which calls
`factory.cli.main` in-process with cwd = the target checkout) and asserts only on what a human or the next
command could see: the exit code, the operator-facing stderr, the fake harness's call log, the committed
worktree, the bare `origin`, the host-local `.factory/` files and the fake GitHub state.

The proofs, one test each:

1. a hand commit after `no_progress` is REVIEWED, not re-parked (§11 "only your action re-opens the loop");
2. a `dismiss` after `rounds_exhausted` is reviewed too — it moves HEAD without changing a line of code, so
   only `ReviewRecord.gated` distinguishes it from the state the gate was raised in;
3. a hand commit after a finished run is reviewed and finalized again, instead of exiting 1 for ever;
4. `build --force` publishes the rewound branch before the session, so a stage that then fails a gate leaves
   `origin` == the local tip and the next plain `factory build` works (§6, §15);
5. a command beside a lock another live process holds exits 1 and leaves that lock alone (§7);
6. the next attempt's prompt carries why the previous one was discarded (§15's "re-run the same command");
7. `abandon` tears down a branch that has diverged from its remote (§6) — the command that exists to clean up
   cannot be the one that refuses to start.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from factory.state import RunLock, current_boot_id, now_iso, pid_start_time

ISSUE = 42
BRANCH = "factory/42"
PR_NUMBER = 117
WORK = f"work/{ISSUE}"
LAST_ERROR = Path(".factory") / "run" / f"{ISSUE}.last-error"

# ---------------------------------------------------------------- harness payloads (schema-valid)

SPEC_MARKDOWN = """\
## Problem

`src/app.py` exposes `add` and nothing that greets, so no caller can produce a greeting.

## Proposed outcome

`src/app.py` gains `greet(name)` returning `"hello <name>"`.

## Affected users and systems

`src/app.py` and its callers only.

## Constraints

Standard library only; the configured check `python3 checks.py` must stay green.

## Acceptance criteria

- `greet("world")` returns `"hello world"`.

## Flagged concerns

None.
"""

PLAN_MARKDOWN = """\
# Plan for issue 42

## Files that change

- `src/app.py` — add the `greet` helper next to `add`.

## Order of work

1. Add `greet` to `src/app.py`.
2. Run the proof command.

## Risks

- None worth naming: one pure function with no dependencies.

## Proof

- `python3 checks.py` exits 0, which is the configured check for this repository.
"""

APP_BASE = """\
# The target repository's only module.


def add(a: int, b: int) -> int:
    return a + b
"""

APP_WITH_GREET = (
    APP_BASE
    + """

def greet(name: str) -> str:
    return "hello " + name
"""
)

APP_WITH_GUARD = (
    APP_BASE
    + """

def greet(name: str) -> str:
    if not name:
        raise ValueError("greet needs a name")
    return "hello " + name
"""
)

#: What an operator writes by hand — deliberately different from every payload above, so a hand commit
#: really is a commit.
APP_HAND_FIXED = (
    APP_BASE
    + '''

def greet(name: str) -> str:
    """Greet `name`; an empty one is a caller error."""
    if not name:
        raise ValueError("greet needs a name")
    return "hello " + name
'''
)

EMPTY_NAME_TITLE = "greet returns 'hello ' for an empty name"
ANNOTATION_TITLE = "greet has no type annotation on the return"
DOCSTRING_TITLE = "greet has no docstring"


def spec_entry(*, open_questions: tuple[str, ...] = ()) -> dict:
    return {"markdown": SPEC_MARKDOWN, "open_questions": list(open_questions)}


def plan_entry() -> dict:
    return {"markdown": PLAN_MARKDOWN}


def build_entry(*, writes: dict[str, str] | None = None) -> dict:
    return {
        "output": {
            "summary": "Added greet to src/app.py; python3 checks.py exits 0.",
            "deviations": [],
        },
        "writes": {"src/app.py": APP_WITH_GREET} if writes is None else dict(writes),
    }


def fix_entry(*, addressed: tuple[str, ...] = (), app: str = APP_WITH_GUARD) -> dict:
    return {
        "output": {
            "addressed": [
                {"id": fid, "how": "guarded the empty name in src/app.py"} for fid in addressed
            ],
            "not_addressed": [],
        },
        "writes": {"src/app.py": app},
    }


def review_entry(
    *, summary: str, updates: tuple[dict, ...] = (), new: tuple[dict, ...] = ()
) -> dict:
    return {
        "summary": summary,
        "updates": [dict(u) for u in updates],
        "new": [dict(n) for n in new],
    }


def update(finding_id: str, status: str, evidence: str = "src/app.py:9 in the diff") -> dict:
    return {"id": finding_id, "status": status, "evidence": evidence}


def new_finding(title: str, *, severity: str = "important", line: int | None = 9) -> dict:
    return {
        "severity": severity,
        "pass": "bugs",
        "file": "src/app.py",
        "line": line,
        "title": title,
        "detail": f"{title}: the caller gets a wrong result instead of an error.",
        "evidence": '+    return "hello " + name  # no guard on the empty string',
    }


# ---------------------------------------------------------------- helpers


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True, stdin=subprocess.DEVNULL
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd} (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def head(worktree: Path) -> str:
    return git("rev-parse", "HEAD", cwd=worktree)


def origin_sha(origin: Path, branch: str = BRANCH) -> str:
    return git("rev-parse", f"refs/heads/{branch}", cwd=origin)


def state_of(worktree: Path) -> dict:
    return json.loads((worktree / WORK / "state.json").read_text(encoding="utf-8"))


def ledger_of(worktree: Path) -> list[dict]:
    return json.loads((worktree / WORK / "findings.json").read_text(encoding="utf-8"))["findings"]


def prompt_files(fake_dir) -> list[str]:
    return [call["prompt_file"] for call in fake_dir.calls()]


def comments(fake_dir) -> list[str]:
    return [comment["body"] for comment in fake_dir.pr(PR_NUMBER)["comments"]]


def hand_commit(worktree: Path, text: str, message: str) -> str:
    """The operator's own edit, committed on the branch — the action §11 says re-opens the loop."""
    (worktree / "src" / "app.py").write_text(text, encoding="utf-8")
    git("commit", "-am", message, cwd=worktree)
    return head(worktree)


def set_max_fix_rounds(target: Path, rounds: int) -> None:
    """Config is read from the checkout, never from the worktree (design §14)."""
    path = target / "factory.toml"
    text = path.read_text(encoding="utf-8")
    assert "max_fix_rounds = 3" in text
    path.write_text(
        text.replace("max_fix_rounds = 3", f"max_fix_rounds = {rounds}"), encoding="utf-8"
    )


def assert_clean_and_pushed(worktree: Path, origin: Path) -> None:
    assert git("status", "--porcelain", cwd=worktree) == ""
    assert origin_sha(origin) == head(worktree)


def drive(fake_dir, run_cli, *commands: str) -> None:
    """Run one command per stage, each expected to exit 0."""
    for command in commands:
        code, _out, err = run_cli(command, ISSUE)
        assert code == 0, f"`factory {command} {ISSUE}` exited {code}\n{err}"


# ---------------------------------------------------------------- 1. a hand commit after no_progress


def test_a_hand_commit_after_no_progress_is_reviewed_not_re_parked(
    fake_dir, run_cli, worktree, origin
):
    """Design §11: the gate stopped a fixer that was not converging. The operator's own fix must be
    adjudicated by a review — re-parking on the review that raised the gate would strand the run."""
    wt = worktree(ISSUE)
    fake_dir.queue(
        spec_entry(),
        plan_entry(),
        build_entry(),
        review_entry(summary="Round 1.", new=(new_finding(EMPTY_NAME_TITLE),)),
        fix_entry(addressed=("F1",)),
        review_entry(
            summary="Round 2: the guard is still missing.",
            updates=(
                update("F1", "unresolved", "src/app.py:9 still concatenates without a guard"),
            ),
        ),
    )
    code, _out, err = run_cli("run", ISSUE)
    assert code == 2, err
    assert "needs human: no_progress" in err
    parked = state_of(wt)
    assert [r["gated"] for r in parked["reviews"]] == [False, True]  # the review the gate was about

    hand = hand_commit(wt, APP_HAND_FIXED, "hand fix: guard the empty name")

    fake_dir.queue(
        review_entry(
            summary="Round 3: the operator's guard resolves it.",
            updates=(update("F1", "resolved", "src/app.py:10 raises ValueError on an empty name"),),
        )
    )
    code, _out, err = run_cli("run", ISSUE)

    assert code == 0, err
    assert len(fake_dir.calls()) == 7  # exactly one more session, and it is a review
    assert prompt_files(fake_dir)[-1] == f"{WORK}/prompts/review-3.md"
    assert fake_dir.calls()[-1]["prompt_text"].count("**NEEDS UPDATE**") == 1

    state = state_of(wt)
    assert state["fix_rounds"] == 1  # no second fix round was spent on the operator's own work
    assert state["outcome"] == "done"
    assert [r["gated"] for r in state["reviews"]] == [False, True, False]
    assert state["reviews"][2]["sha"] == hand  # round 3 reviewed the commit the operator made
    assert [f["status"] for f in ledger_of(wt)] == ["resolved"]
    assert fake_dir.pr(PR_NUMBER)["isDraft"] is False
    assert_clean_and_pushed(wt, origin)


# ---------------------------------------------------------------- 2. a dismissal after the round cap


def test_a_dismissal_after_rounds_exhausted_is_reviewed_not_re_parked(
    fake_dir, run_cli, worktree, origin, target
):
    """`dismiss` commits under work/ only, so no code changed and the cap is still spent: without
    ReviewRecord.gated the run would re-raise the very gate the dismissal answered, for ever."""
    wt = worktree(ISSUE)
    set_max_fix_rounds(target, 1)
    fake_dir.queue(
        spec_entry(),
        plan_entry(),
        build_entry(),
        review_entry(
            summary="Round 1.",
            new=(
                new_finding(EMPTY_NAME_TITLE),
                new_finding(ANNOTATION_TITLE, line=10),
                new_finding(DOCSTRING_TITLE, line=11),
            ),
        ),
        fix_entry(addressed=("F1",)),
        review_entry(
            summary="Round 2: the guard landed; the other two stand.",
            updates=(
                update("F1", "resolved", "src/app.py:10 raises ValueError on an empty name"),
                update("F2", "unresolved", "src/app.py:9 still has no return annotation"),
                update("F3", "unresolved", "src/app.py:9 still has no docstring"),
            ),
        ),
    )
    code, _out, err = run_cli("run", ISSUE)
    assert code == 2, err
    assert "needs human: rounds_exhausted" in err
    parked = state_of(wt)
    assert parked["reviews"][-1]["gated"] is True
    parked_head = head(wt)

    code, _out, err = run_cli("dismiss", ISSUE, "F2", "annotations are out of scope for this issue")
    assert code == 0, err
    assert state_of(wt)["outcome"] is None
    assert head(wt) != parked_head  # the dismissal moved HEAD ...
    assert git("diff", "--name-only", parked_head, "HEAD", cwd=wt).splitlines() == [
        f"{WORK}/findings.json",
        f"{WORK}/state.json",
    ]  # ... without touching a line of code

    fake_dir.queue(
        review_entry(
            summary="Round 3: F3 was addressed by the docstring the operator left.",
            updates=(update("F3", "resolved", "src/app.py:9 now documents the contract"),),
        )
    )
    code, _out, err = run_cli("run", ISSUE)

    assert code == 0, err
    assert "raised the gate you answered" in err
    assert len(fake_dir.calls()) == 7  # one review, no second fix round, no re-park
    assert prompt_files(fake_dir)[-1] == f"{WORK}/prompts/review-3.md"

    state = state_of(wt)
    assert state["outcome"] == "done"
    assert state["fix_rounds"] == 1
    assert [(f["id"], f["status"]) for f in ledger_of(wt)] == [
        ("F1", "resolved"),
        ("F2", "dismissed"),
        ("F3", "resolved"),
    ]
    gate_comments = [body for body in comments(fake_dir) if "`rounds_exhausted`" in body]
    assert len(gate_comments) == 1  # the gate was raised once and never again
    assert_clean_and_pushed(wt, origin)


# ---------------------------------------------------------------- 3. a hand commit after finalize


def test_a_hand_commit_after_a_finished_run_is_reviewed_and_finalized_again(
    fake_dir, run_cli, worktree, origin
):
    """Before this, `finalize` refused a HEAD the last review had not seen and `run` had nothing else to
    offer: the issue exited 1 on every tick. Now the commit is reviewed, checked and finalized."""
    wt = worktree(ISSUE)
    fake_dir.queue(
        spec_entry(),
        plan_entry(),
        build_entry(),
        review_entry(summary="Round 1: nothing blocks it."),
    )
    code, _out, err = run_cli("run", ISSUE)
    assert code == 0, err
    first = state_of(wt)
    assert first["outcome"] == "done"

    hand_commit(wt, APP_WITH_GUARD, "hand fix after the run finished")

    fake_dir.queue(review_entry(summary="Round 2: the added guard is sound."))
    code, _out, err = run_cli("run", ISSUE)

    assert code == 0, err
    assert "reviewing it before finalizing" in err
    assert len(fake_dir.calls()) == 5  # one review; finalize needs no session
    assert prompt_files(fake_dir)[-1] == f"{WORK}/prompts/review-2.md"

    state = state_of(wt)
    assert len(state["reviews"]) == 2
    assert state["outcome"] == "done"
    assert (
        state["outcome_sha"] != first["outcome_sha"]
    )  # re-checked and re-finalized at the new HEAD
    finalize_log = (wt / WORK / "checks" / "finalize-1.log").read_text(encoding="utf-8")
    assert "checks: all green" in finalize_log  # the checks ran again, over the operator's commit
    assert f"{WORK}/state.json" in git(
        "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", cwd=wt
    )
    summaries = [body for body in comments(fake_dir) if "Factory run complete" in body]
    assert len(summaries) == 2  # one per finalized sha, both idempotent by marker
    assert fake_dir.pr(PR_NUMBER)["isDraft"] is False
    assert_clean_and_pushed(wt, origin)


# ---------------------------------------------------------------- 4. --force publishes the rewind


def test_a_forced_build_that_fails_a_gate_leaves_origin_at_the_rewound_tip(
    fake_dir, run_cli, worktree, origin
):
    """Design §6: `--force` rewinds and pushes with --force-with-lease. Pushing the rewind BEFORE the
    session means a rejected attempt leaves the remote exactly where the local branch is, so the next
    plain `factory build` is an ordinary fast-forward instead of a second forced push."""
    wt = worktree(ISSUE)
    fake_dir.queue(spec_entry(), plan_entry(), build_entry())
    drive(fake_dir, run_cli, "spec", "plan", "build")
    built = head(wt)
    plan_commit = state_of(wt)["stages"]["build"]["start_commit"]
    assert origin_sha(origin) == built

    fake_dir.queue(
        build_entry(writes={"src/app.py": APP_WITH_GREET, "src/extra.py": "# unplanned\n"})
    )
    code, _out, err = run_cli("build", ISSUE, "--force")

    assert code == 1, err
    assert "plan.md does not list: src/extra.py" in err
    rewound = head(wt)
    history = git("log", "--format=%H", cwd=wt).splitlines()
    assert built not in history  # the build commit was rewound away ...
    assert plan_commit in history  # ... back to where build started
    assert origin_sha(origin) == rewound  # and the remote says exactly the same
    assert not (wt / "src" / "extra.py").exists()
    assert git("status", "--porcelain", cwd=wt) == ""
    assert "build" not in state_of(wt)["stages"]

    fake_dir.queue(build_entry())
    code, _out, err = run_cli("build", ISSUE)

    assert code == 0, err
    assert state_of(wt)["stages"]["build"]["start_commit"] == rewound  # ran on the rewound branch
    assert head(wt) != built  # a new build commit, not the one the rewind discarded
    assert "greet" in (wt / "src" / "app.py").read_text(encoding="utf-8")
    assert_clean_and_pushed(wt, origin)


# ---------------------------------------------------------------- 5. a lock somebody else holds


def test_a_command_beside_a_live_lock_exits_1_and_leaves_the_lock_alone(
    fake_dir, run_cli, worktree, target
):
    """Design §7: the lock's presence with a LIVE pid is the per-issue lock. The refused command must not
    clear it on its way out — that is another writer's lock, and it is still working."""
    wt = worktree(ISSUE)
    fake_dir.queue(spec_entry(), plan_entry())
    drive(fake_dir, run_cli, "spec")
    factory_dir = target / ".factory"

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        RunLock(
            issue=ISSUE,
            stage="build",
            pid=holder.pid,
            started_at=now_iso(),
            worktree=str(wt),
            boot_id=current_boot_id(),
            pid_start=pid_start_time(holder.pid),
        ).write(factory_dir)
        calls_before = len(fake_dir.calls())

        code, _out, err = run_cli("plan", ISSUE)

        assert code == 1, err
        assert f"build in progress (pid {holder.pid})" in err
        assert len(fake_dir.calls()) == calls_before  # refused before any session
        still_there = RunLock.read(factory_dir, ISSUE)
        assert still_there is not None
        assert (still_there.pid, still_there.stage) == (holder.pid, "build")
    finally:
        holder.kill()
        holder.wait()

    # With the holder gone the same lock reads as an interrupted stage, and the command runs.
    code, _out, err = run_cli("plan", ISSUE)

    assert code == 0, err
    assert "interrupted build discarded" in err
    assert not RunLock.path(
        factory_dir, ISSUE
    ).exists()  # this command owned the lock and cleared it


# ---------------------------------------------------------------- 6. the previous rejection carries


def test_the_next_attempt_is_told_why_the_previous_one_was_discarded(
    fake_dir, run_cli, worktree, target
):
    """Design §15: "re-run the same command" is only useful if the re-run knows what went wrong. The
    RunLock cannot carry it (a clean exit 1 clears the lock), so the message lives in
    .factory/run/<issue>.last-error until an attempt gets past it."""
    wt = worktree(ISSUE)
    fake_dir.queue(spec_entry(), plan_entry())
    drive(fake_dir, run_cli, "spec", "plan")

    fake_dir.queue(
        build_entry(writes={"src/app.py": APP_WITH_GREET, "src/extra.py": "# unplanned\n"})
    )
    code, _out, err = run_cli("build", ISSUE)
    assert code == 1, err
    recorded = (target / LAST_ERROR).read_text(encoding="utf-8")
    assert "src/extra.py" in recorded

    fake_dir.queue(build_entry())
    code, _out, err = run_cli("build", ISSUE)

    assert code == 0, err
    prompt = fake_dir.calls()[-1]["prompt_text"]
    assert prompt_files(fake_dir)[-1] == f"{WORK}/prompts/build-1.md"
    assert "The previous attempt was discarded because" in prompt
    assert "src/extra.py" in prompt
    assert not (target / LAST_ERROR).exists()  # a stage that committed has nothing to apologise for
    assert (wt / WORK / "prompts" / "build-1.md").read_text(encoding="utf-8") == prompt


# ---------------------------------------------------------------- 7. abandon on a diverged branch


def test_abandon_tears_down_a_branch_that_has_diverged_from_its_remote(
    fake_dir, run_cli, worktree, origin, target, tmp_path
):
    """Design §6: `abandon` is the way to close a run. It runs no session and no check and deletes the
    branch, so none of the start-of-command rules — fetch, dirty worktree, protected paths — may refuse
    it. A diverged branch is exactly the state an operator most wants to abandon."""
    wt = worktree(ISSUE)
    fake_dir.queue(spec_entry())
    drive(fake_dir, run_cli, "spec")

    other = tmp_path / "other-clone"
    git("clone", str(origin), str(other), cwd=tmp_path)
    git("checkout", BRANCH, cwd=other)
    (other / "src" / "app.py").write_text(APP_WITH_GREET, encoding="utf-8")
    git("commit", "-am", "a commit pushed from somewhere else", cwd=other)
    git("push", "origin", BRANCH, cwd=other)

    hand_commit(wt, APP_WITH_GUARD, "a commit made here")
    code, _out, err = run_cli("plan", ISSUE)
    assert code == 1 and "diverged" in err  # every other command stops on this branch

    code, _out, err = run_cli("abandon", ISSUE)

    assert code == 0, err
    assert git("ls-remote", "--heads", str(origin), BRANCH, cwd=target) == ""
    assert BRANCH not in git("branch", "--list", cwd=target)
    assert not wt.exists()
    assert fake_dir.pr(PR_NUMBER)["state"] == "CLOSED"
    assert fake_dir.issue(ISSUE)["labels"] == []
    assert not RunLock.path(target / ".factory", ISSUE).exists()
