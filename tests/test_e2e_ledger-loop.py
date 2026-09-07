"""End-to-end proofs of the review/fix ledger loop (design §11, §17.2).

Every test drives the factory only through `factory <command>` (the conftest `run_cli` fixture, which
calls `factory.cli.main` in-process with cwd = the target checkout) and asserts only on what a human
or the next command could see afterwards: the exit code, the operator-facing stderr, the fake
harness's call log, the committed worktree, the bare `origin`, and the fake GitHub state.

The proofs, one test each:

1. a re-raised dismissed finding is dropped and counted (design rule 5, §10 ledger merge);
2. the no-progress rule terminates the loop (§11);
3. the round cap terminates the loop (§11);
4. a parked issue re-run unchanged makes no harness call (§11 "parked stays parked");
5. `accept` re-opens the loop after the open-questions gate (§2 step 3);
6. `dismiss` re-opens the loop after `rounds_exhausted`, and the run finalizes (§2 step 5, §9);
7. a review that omits an update for an open finding fails the round and leaves the ledger untouched (§9).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ISSUE = 42
BRANCH = "factory/42"
PR_NUMBER = 117
WORK = f"work/{ISSUE}"

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

EMPTY_NAME_TITLE = "greet returns 'hello ' for an empty name"


def spec_entry(*, open_questions: tuple[str, ...] = ()) -> dict:
    return {"markdown": SPEC_MARKDOWN, "open_questions": list(open_questions)}


def plan_entry() -> dict:
    return {"markdown": PLAN_MARKDOWN}


def build_entry(*, app: str = APP_WITH_GREET) -> dict:
    return {
        "output": {
            "summary": "Added greet to src/app.py; python3 checks.py exits 0.",
            "deviations": [],
        },
        "writes": {"src/app.py": app},
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
        "complete": True,
        "updates": [dict(u) for u in updates],
        "new": [dict(n) for n in new],
    }


def update(finding_id: str, status: str, evidence: str = "src/app.py:9 in the diff") -> dict:
    return {"id": finding_id, "status": status, "evidence": evidence}


def new_finding(
    title: str,
    *,
    severity: str = "important",
    pass_: str = "bugs",
    file: str = "src/app.py",
    line: int | None = 9,
) -> dict:
    return {
        "severity": severity,
        "pass": pass_,
        "file": file,
        "line": line,
        "title": title,
        "detail": f"{title}: the caller gets a wrong result instead of an error.",
        "evidence": '+    return "hello " + name  # no guard on the empty string',
    }


# ---------------------------------------------------------------- assertions helpers


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


def subjects(worktree: Path) -> list[str]:
    return git("log", "--format=%s", cwd=worktree).splitlines()


def origin_sha(origin: Path, branch: str = BRANCH) -> str:
    return git("rev-parse", f"refs/heads/{branch}", cwd=origin)


def read_json(worktree: Path, name: str) -> dict:
    return json.loads((worktree / WORK / name).read_text(encoding="utf-8"))


def state_of(worktree: Path) -> dict:
    return read_json(worktree, "state.json")


def ledger_of(worktree: Path) -> list[dict]:
    return read_json(worktree, "findings.json")["findings"]


def prompt_files(fake_dir) -> list[str]:
    return [call["prompt_file"] for call in fake_dir.calls()]


def flag(argv: list[str], name: str) -> str | None:
    """The value following `name` in a recorded argv, or None when the flag is absent."""
    return argv[argv.index(name) + 1] if name in argv else None


def comments(fake_dir) -> list[str]:
    return [comment["body"] for comment in fake_dir.pr(PR_NUMBER)["comments"]]


def comment_with(fake_dir, marker: str) -> str:
    matches = [body for body in comments(fake_dir) if marker in body]
    assert len(matches) == 1, (
        f"expected exactly one PR comment containing {marker!r}, got {len(matches)}"
    )
    return matches[0]


def assert_clean_and_pushed(worktree: Path, origin: Path) -> None:
    """The worktree carries no leftovers and origin/factory/42 is the commit the operator would see."""
    assert git("status", "--porcelain", cwd=worktree) == ""
    assert origin_sha(origin) == head(worktree)


def set_max_fix_rounds(target: Path, rounds: int) -> None:
    """Config is read from the checkout, never from the worktree (design §14), so editing it here is enough."""
    path = target / "factory.toml"
    text = path.read_text(encoding="utf-8")
    assert "max_fix_rounds = 3" in text
    path.write_text(
        text.replace("max_fix_rounds = 3", f"max_fix_rounds = {rounds}"), encoding="utf-8"
    )


def drive_to_first_review(fake_dir, run_cli, *, new: tuple[dict, ...]) -> None:
    """spec -> plan -> build -> review 1, one command per stage, each expected to exit 0."""
    fake_dir.queue(
        spec_entry(),
        plan_entry(),
        build_entry(),
        review_entry(summary="Round 1: the greeting helper is implemented.", new=new),
    )
    for command in ("spec", "plan", "build", "review"):
        code, _out, err = run_cli(command, ISSUE)
        assert code == 0, f"`factory {command} {ISSUE}` exited {code}\n{err}"


# ---------------------------------------------------------------- 1. re-raised dismissed finding


def test_a_re_raised_dismissed_finding_is_dropped(fake_dir, run_cli, worktree, origin):
    """Design rule 5 and §10: a finding the operator adjudicated cannot come back as new."""
    wt = worktree(ISSUE)
    drive_to_first_review(fake_dir, run_cli, new=(new_finding(EMPTY_NAME_TITLE),))
    assert [f["id"] for f in ledger_of(wt)] == ["F1"]

    code, _out, err = run_cli("dismiss", ISSUE, "F1", "empty names are rejected upstream")
    assert code == 0, err
    assert ledger_of(wt)[0]["status"] == "dismissed"

    # Round 2 re-raises F1's exact pass|file|title, which is the ledger key.
    fake_dir.queue(review_entry(summary="Round 2.", new=(new_finding(EMPTY_NAME_TITLE),)))
    code, _out, err = run_cli("review", ISSUE)
    assert code == 0, err
    # The reviewer saw the dismissal and was not asked to update it.
    ledger_line = fake_dir.calls()[4]["prompt_text"]
    assert f"- F1 [dismissed · important · bugs] src/app.py:9 — {EMPTY_NAME_TITLE}" in ledger_line
    assert "**NEEDS UPDATE**" not in ledger_line

    findings = ledger_of(wt)
    assert [(f["id"], f["status"]) for f in findings] == [("F1", "dismissed")]
    assert findings[0]["dismissed_reason"] == "empty names are rejected upstream"

    reviews = state_of(wt)["reviews"]
    assert len(reviews) == 2
    assert reviews[1]["reraised_dropped"] == 1
    assert reviews[1]["important_open"] == 0

    body = comment_with(fake_dir, f"<!-- factory:review:2:{reviews[1]['sha']} -->")
    assert "| Re-raised after dismissal (dropped) | 1 |" in body
    assert_clean_and_pushed(wt, origin)


# ---------------------------------------------------------------- 2. the no-progress rule


def test_the_no_progress_rule_terminates_the_loop(fake_dir, run_cli, worktree, origin):
    """Design §11: a review following a fix that resolved zero Important findings parks the run."""
    wt = worktree(ISSUE)
    fake_dir.queue(
        spec_entry(),
        plan_entry(),
        build_entry(),
        review_entry(summary="Round 1.", new=(new_finding(EMPTY_NAME_TITLE),)),
        fix_entry(addressed=("F1",)),
        review_entry(
            summary="Round 2: the guard is still missing on the path that matters.",
            updates=(
                update("F1", "unresolved", "src/app.py:9 still concatenates without a guard"),
            ),
        ),
    )
    code, _out, err = run_cli("run", ISSUE)

    assert code == 2, err
    assert "needs human: no_progress" in err
    assert "resolved no Important finding" in err
    assert prompt_files(fake_dir) == [
        f"{WORK}/prompts/spec-1.md",
        f"{WORK}/prompts/plan-1.md",
        f"{WORK}/prompts/build-1.md",
        f"{WORK}/prompts/review-1.md",
        f"{WORK}/prompts/fix-1.md",
        f"{WORK}/prompts/review-2.md",
    ]

    # The two sessions of the loop, as design §8 specifies them for subscription auth.
    fix_call, review_call = fake_dir.calls()[4], fake_dir.calls()[5]
    assert flag(review_call["argv"], "--permission-mode") == "dontAsk"
    assert flag(review_call["argv"], "--allowedTools") == "Read,Grep,Glob"
    assert "Edit,Write" in flag(review_call["argv"], "--disallowedTools")
    assert "Edit,Write" in flag(fix_call["argv"], "--allowedTools")
    assert "--disallowedTools" not in fix_call["argv"]  # a write stage is bounded by its allowlist
    assert "--bare" not in fix_call["argv"] and "--bare" not in review_call["argv"]
    for call in (fix_call, review_call):
        assert call["env"]["GH_TOKEN"] == "absent"  # no agent ever holds a GitHub token
        assert call["env"]["ANTHROPIC_API_KEY"] == "absent"  # subscription auth passes no key
        assert "FACTORY_FAKE_DIR" not in call["env_keys"]

    # What each session was actually told.
    assert f"**F1** — src/app.py:9 — {EMPTY_NAME_TITLE}" in fix_call["prompt_text"]
    assert "**NEEDS UPDATE**" in review_call["prompt_text"]
    assert ".factory/tmp/review-2.diff" in review_call["prompt_text"]
    assert "hello " in (wt / ".factory" / "tmp" / "review-2.diff").read_text(encoding="utf-8")

    state = state_of(wt)
    assert state["outcome"] == "needs_human:no_progress"
    assert state["fix_rounds"] == 1
    assert [r["important_resolved"] for r in state["reviews"]] == [0, 0]
    assert subjects(wt)[0] == f"factory({ISSUE}): park no_progress"
    assert [f["status"] for f in ledger_of(wt)] == ["open"]

    body = comment_with(fake_dir, f"<!-- factory:gate:no_progress:{state['outcome_sha']} -->")
    assert "Factory stopped — needs human (`no_progress`)" in body
    assert "F1 src/app.py:9" in body
    assert (
        git("log", "--format=%s", "-1", BRANCH, cwd=origin) == f"factory({ISSUE}): park no_progress"
    )
    assert_clean_and_pushed(wt, origin)


# ---------------------------------------------------------------- 3. the round cap


def rounds_exhausted_run(fake_dir, run_cli, target) -> tuple[int, str]:
    """One `factory run` under max_fix_rounds = 1: review raises two, the fix resolves one, the cap parks."""
    set_max_fix_rounds(target, 1)
    fake_dir.queue(
        spec_entry(),
        plan_entry(),
        build_entry(),
        review_entry(
            summary="Round 1.",
            new=(
                new_finding(EMPTY_NAME_TITLE),
                new_finding(
                    "greet has no type annotation on the return", severity="important", line=10
                ),
            ),
        ),
        fix_entry(addressed=("F1",)),
        review_entry(
            summary="Round 2: the empty-name guard landed; the annotation is still missing.",
            updates=(
                update("F1", "resolved", "src/app.py:10 now raises ValueError on an empty name"),
                update("F2", "unresolved", "src/app.py:9 still has no return annotation"),
            ),
        ),
    )
    code, _out, err = run_cli("run", ISSUE)
    return code, err


def test_the_round_cap_terminates_the_loop(fake_dir, run_cli, worktree, origin, target):
    """Design §11: fix_rounds == max_fix_rounds with Important findings open parks and lists them."""
    wt = worktree(ISSUE)
    code, err = rounds_exhausted_run(fake_dir, run_cli, target)

    assert code == 2, err
    assert "needs human: rounds_exhausted" in err
    assert "All 1 fix round(s) are spent" in err
    assert "F2 src/app.py:10 greet has no type annotation on the return" in err
    assert len(fake_dir.calls()) == 6  # the cap stops the loop before a second fix round

    state = state_of(wt)
    assert state["outcome"] == "needs_human:rounds_exhausted"
    assert state["fix_rounds"] == 1
    assert [(f["id"], f["status"]) for f in ledger_of(wt)] == [("F1", "resolved"), ("F2", "open")]

    body = comment_with(fake_dir, f"<!-- factory:gate:rounds_exhausted:{state['outcome_sha']} -->")
    assert "F2 src/app.py:10 greet has no type annotation on the return" in body
    assert fake_dir.pr(PR_NUMBER)["isDraft"] is True  # a parked run never flips the draft
    assert_clean_and_pushed(wt, origin)


# ---------------------------------------------------------------- 4. parked stays parked


def test_a_parked_issue_re_run_unchanged_makes_no_harness_call(fake_dir, run_cli, worktree, origin):
    """Design §11: with HEAD == outcome_sha the gate is re-raised before any harness call."""
    wt = worktree(ISSUE)
    fake_dir.queue(spec_entry(open_questions=("Should greet localise the greeting?",)))

    code, _out, err = run_cli("run", ISSUE)
    assert code == 2, err
    assert "needs human: open_questions" in err
    calls_after_first = (fake_dir / "harness_calls.jsonl").read_bytes()
    assert len(fake_dir.calls()) == 1
    parked_head = head(wt)

    code, _out, err = run_cli("run", ISSUE)

    assert code == 2, err
    assert "needs human: open_questions" in err
    assert (fake_dir / "harness_calls.jsonl").read_bytes() == calls_after_first
    assert fake_dir.queue_remaining() == []  # nothing was popped, and nothing was left over
    assert head(wt) == parked_head  # the second run committed nothing either
    assert len(comments(fake_dir)) == 1  # the gate comment is posted once per outcome sha
    assert state_of(wt)["outcome"] == "needs_human:open_questions"
    assert_clean_and_pushed(wt, origin)


# ---------------------------------------------------------------- 5. accept re-opens the loop


def test_accept_re_opens_the_loop_after_open_questions(fake_dir, run_cli, worktree, origin):
    """Design §2 step 3: the operator answers, `accept` records it, and the run continues to plan."""
    wt = worktree(ISSUE)
    question = "Should greet localise the greeting?"
    fake_dir.queue(spec_entry(open_questions=(question,)))

    code, _out, err = run_cli("run", ISSUE)
    assert code == 2, err
    assert question in err
    assert not state_of(wt)["stages"].get("plan")

    code, _out, err = run_cli("accept", ISSUE)
    assert code == 0, err
    accepted = state_of(wt)
    assert accepted["spec_accepted"]["by"] == "operator"
    assert accepted["outcome"] is None

    fake_dir.queue(plan_entry(), build_entry(), review_entry(summary="Nothing blocks this change."))
    code, _out, err = run_cli("run", ISSUE)

    assert code == 0, err
    assert prompt_files(fake_dir) == [
        f"{WORK}/prompts/spec-1.md",
        f"{WORK}/prompts/plan-1.md",
        f"{WORK}/prompts/build-1.md",
        f"{WORK}/prompts/review-1.md",
    ]
    plan_call = fake_dir.calls()[1]
    assert "## Acceptance criteria" in plan_call["prompt_text"]  # the plan prompt carries spec.md
    assert plan_call["env"]["GH_TOKEN"] == "absent"
    assert plan_call["env"]["FACTORY_FAKE_DIR"] == "absent"

    assert (wt / WORK / "plan.md").read_text(encoding="utf-8") == PLAN_MARKDOWN
    assert "greet" in (wt / "src" / "app.py").read_text(encoding="utf-8")
    state = state_of(wt)
    assert state["outcome"] == "done"
    assert sorted(state["stages"]) == ["build", "plan", "spec"]
    assert fake_dir.pr(PR_NUMBER)["isDraft"] is False
    assert_clean_and_pushed(wt, origin)


# ---------------------------------------------------------------- 6. dismiss re-opens the loop


def test_dismiss_re_opens_the_loop_after_rounds_exhausted(
    fake_dir, run_cli, worktree, origin, target
):
    """Design §2 step 5: adjudicating the last open finding moves HEAD, and the next run finalizes."""
    wt = worktree(ISSUE)
    code, err = rounds_exhausted_run(fake_dir, run_cli, target)
    assert code == 2, err

    code, _out, err = run_cli("dismiss", ISSUE, "F2", "annotations are out of scope for this issue")
    assert code == 0, err
    assert [(f["id"], f["status"]) for f in ledger_of(wt)] == [
        ("F1", "resolved"),
        ("F2", "dismissed"),
    ]
    assert state_of(wt)["outcome"] is None  # dismiss un-parks the run

    code, _out, err = run_cli("run", ISSUE)

    assert code == 0, err
    assert len(fake_dir.calls()) == 6  # finalize needs no harness session
    state = state_of(wt)
    assert state["outcome"] == "done"
    assert subjects(wt)[0] == f"factory({ISSUE}): finalize"
    assert (wt / WORK / "checks" / "finalize-1.log").is_file()

    pr = fake_dir.pr(PR_NUMBER)
    assert pr["isDraft"] is False
    assert pr["state"] == "OPEN"  # the factory never merges and never closes a finalized PR
    body = comment_with(fake_dir, f"<!-- factory:summary:{state['outcome_sha']} -->")
    assert "Factory run complete — ready for review" in body
    assert "| F2 | important | bugs | dismissed |" in body
    assert "checks: all green" in body
    assert fake_dir.issue(ISSUE)["labels"] == ["factory"]  # finalize leaves the intake label alone
    assert git("log", "--format=%s", "-1", BRANCH, cwd=origin) == f"factory({ISSUE}): finalize"
    assert_clean_and_pushed(wt, origin)


# ---------------------------------------------------------------- 7. a review must update every finding


def test_a_review_that_omits_an_update_fails_the_round(fake_dir, run_cli, worktree, origin):
    """Design §9: "every open finding has an update" is a deterministic gate, so the round is exit 1."""
    wt = worktree(ISSUE)
    drive_to_first_review(fake_dir, run_cli, new=(new_finding(EMPTY_NAME_TITLE),))
    before_head = head(wt)
    before_ledger = (wt / WORK / "findings.json").read_bytes()
    before_origin = origin_sha(origin)

    fake_dir.queue(review_entry(summary="Round 2 says nothing about F1."))
    code, _out, err = run_cli("review", ISSUE)

    assert code == 1, err
    assert "review 2 gave no update for open finding(s): F1" in err
    assert len(fake_dir.calls()) == 5  # the round ran; only its result was rejected

    assert (wt / WORK / "findings.json").read_bytes() == before_ledger
    assert len(state_of(wt)["reviews"]) == 1
    assert not (wt / WORK / "review-2.json").exists()
    assert head(wt) == before_head
    assert origin_sha(origin) == before_origin
    assert git("status", "--porcelain", cwd=wt) == ""  # the failed round reset the worktree
    assert len(comments(fake_dir)) == 1  # only round 1's comment
