"""Unit tests for factory.gh — every `gh` call, its idempotency rules, and the rendered comment bodies.

Self-contained: a stub `gh` python script is written into tmp_path and put first on PATH. It records every
invocation (argv, cwd, the /dev/null-ness of stdin, the content of any --body-file) and replays canned
responses driven by a JSON file, so no test depends on tests/fakes/gh or tests/conftest.py.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.errors import FactoryError  # noqa: E402
from factory.gh import (  # noqa: E402
    CAPPED_MARKER,
    GATE_MARKER,
    REVIEW_MARKER,
    SUMMARY_MARKER,
    GitHub,
    Issue,
    PullRequest,
    render_capped_comment,
    render_gate_comment,
    render_review_comment,
    render_summary_comment,
)

STUB_GH = '''#!/usr/bin/env python3
"""Stub `gh`: records argv and replays canned responses from $STUB_GH_STATE."""
import json
import os
import pathlib
import sys

state_path = pathlib.Path(os.environ["STUB_GH_STATE"])
state = json.loads(state_path.read_text())
argv = sys.argv[1:]

try:
    stdin_path = os.readlink("/proc/self/fd/0")
except OSError:
    stdin_path = "?"

record = {"argv": argv, "cwd": os.getcwd(), "stdin_path": stdin_path}
for flag in ("--body-file", "--output-file"):
    if flag in argv:
        target = pathlib.Path(argv[argv.index(flag) + 1])
        record["body_file"] = str(target)
        record["body"] = target.read_text() if target.exists() else None

with open(state["calls"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record) + "\\n")


def matches(tokens, args):
    i = 0
    for token in tokens:
        while i < len(args) and args[i] != token:
            i += 1
        if i == len(args):
            return False
        i += 1
    return True


for index, rule in enumerate(state["rules"]):
    if not matches(rule["match"], argv):
        continue
    if rule.get("once"):
        state["rules"].pop(index)
        state_path.write_text(json.dumps(state))
    sys.stdout.write(rule.get("stdout", ""))
    sys.stderr.write(rule.get("stderr", ""))
    sys.exit(rule.get("exit", 0))

sys.stderr.write("stub gh: unhandled %s\\n" % (argv,))
sys.exit(2)
'''


class Stub:
    """Drives the stub `gh` binary and reads back what it was called with."""

    def __init__(self, root: Path, state_path: Path, calls_path: Path):
        self.root = root
        self.state_path = state_path
        self.calls_path = calls_path

    def rule(
        self, *match: str, stdout: str = "", stderr: str = "", exit: int = 0, once: bool = False
    ) -> None:
        state = json.loads(self.state_path.read_text())
        state["rules"].append(
            {"match": list(match), "stdout": stdout, "stderr": stderr, "exit": exit, "once": once}
        )
        self.state_path.write_text(json.dumps(state))

    def json_rule(self, *match: str, payload, once: bool = False) -> None:
        self.rule(*match, stdout=json.dumps(payload), once=once)

    def calls(self) -> list[dict]:
        if not self.calls_path.exists():
            return []
        return [
            json.loads(line) for line in self.calls_path.read_text().splitlines() if line.strip()
        ]

    def argvs(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls()]

    def called(self, *tokens: str) -> list[dict]:
        return [call for call in self.calls() if _is_subsequence(tokens, call["argv"])]


def _is_subsequence(tokens, argv) -> bool:
    i = 0
    for token in tokens:
        while i < len(argv) and argv[i] != token:
            i += 1
        if i == len(argv):
            return False
        i += 1
    return True


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stub:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "gh"
    script.write_text(STUB_GH)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    root = tmp_path / "checkout"
    root.mkdir()
    calls_path = tmp_path / "gh_calls.jsonl"
    state_path = tmp_path / "gh_stub.json"
    state_path.write_text(json.dumps({"calls": str(calls_path), "rules": []}))

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_GH_STATE", str(state_path))
    return Stub(root, state_path, calls_path)


@pytest.fixture
def gh(stub: Stub) -> GitHub:
    return GitHub(stub.root)


# ---------------------------------------------------------------- plumbing


def test_calls_run_in_the_checkout_root_with_stdin_devnull(stub: Stub, gh: GitHub):
    stub.json_rule("repo", "view", payload={"nameWithOwner": "owner/name"})

    assert gh.repo_slug() == "owner/name"

    (call,) = stub.calls()
    assert call["argv"] == ["repo", "view", "--json", "nameWithOwner"]
    assert call["cwd"] == str(stub.root)
    assert call["stdin_path"] == "/dev/null"


def test_failure_names_the_command_the_root_and_the_stderr_tail(stub: Stub, gh: GitHub):
    stub.rule("repo", "view", stderr="HTTP 404: Not Found\n", exit=1)

    with pytest.raises(FactoryError) as excinfo:
        gh.repo_slug()

    message = excinfo.value.message
    assert "gh repo view" in message
    assert str(stub.root) in message
    assert "HTTP 404" in message


def test_non_json_output_is_a_clear_error(stub: Stub, gh: GitHub):
    stub.rule("repo", "view", stdout="not json at all")

    with pytest.raises(FactoryError, match="did not return JSON"):
        gh.repo_slug()


def test_missing_gh_binary_is_a_factory_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    with pytest.raises(FactoryError, match="not on PATH"):
        GitHub(tmp_path).repo_slug()


def test_auth_ok_reports_status_and_one_line_detail(stub: Stub, gh: GitHub):
    stub.rule("auth", "status", stderr="Logged in to github.com as octocat\n", once=True)
    ok, detail = gh.auth_ok()
    assert ok is True
    assert detail == "Logged in to github.com as octocat"

    stub.rule("auth", "status", stderr="\nYou are not logged into any GitHub hosts.\n", exit=1)
    ok, detail = gh.auth_ok()
    assert ok is False
    assert "not logged into" in detail


# ---------------------------------------------------------------- issues


def test_issue_maps_labels_and_round_trips_to_the_render_intent_shape(stub: Stub, gh: GitHub):
    stub.json_rule(
        "issue",
        "view",
        payload={
            "number": 42,
            "title": "Add a widget",
            "body": "## Problem\nno widget\n",
            "url": "https://github.com/owner/name/issues/42",
            "labels": [{"name": "factory"}, {"name": "bug"}],
            "state": "OPEN",
        },
    )

    issue = gh.issue(42)

    assert issue == Issue(
        number=42,
        title="Add a widget",
        body="## Problem\nno widget\n",
        url="https://github.com/owner/name/issues/42",
        labels=["factory", "bug"],
        state="OPEN",
    )
    assert issue.to_dict()["labels"] == [{"name": "factory"}, {"name": "bug"}]
    (call,) = stub.calls()
    assert call["argv"] == [
        "issue",
        "view",
        "42",
        "--json",
        "number,title,body,url,labels,state",
    ]


def test_issue_tolerates_missing_optional_fields(stub: Stub, gh: GitHub):
    stub.json_rule("issue", "view", payload={"number": 7, "title": None, "labels": None})

    issue = gh.issue(7)

    assert (issue.body, issue.url, issue.labels, issue.state) == ("", "", [], "")


def test_unknown_issue_raises_naming_the_number(stub: Stub, gh: GitHub):
    stub.rule("issue", "view", stderr="no issue found\n", exit=1)

    with pytest.raises(FactoryError, match="issue view 99"):
        gh.issue(99)


def test_list_open_issues_with_label_is_ascending_and_deduplicated(stub: Stub, gh: GitHub):
    stub.json_rule(
        "issue",
        "list",
        payload=[{"number": 51}, {"number": 7}, {"number": 51}, {"number": 42}],
    )

    assert gh.list_open_issues_with_label("factory") == [7, 42, 51]
    (call,) = stub.calls()
    assert call["argv"] == [
        "issue",
        "list",
        "--state",
        "open",
        "--label",
        "factory",
        "--json",
        "number",
        "--limit",
        "200",
    ]


def test_remove_label_absent_label_is_success(stub: Stub, gh: GitHub):
    stub.rule(
        "issue",
        "edit",
        stderr="failed to update issue: 'factory' not found\n",
        exit=1,
    )

    gh.remove_label(42, "factory")  # no raise

    assert stub.argvs() == [["issue", "edit", "42", "--remove-label", "factory"]]


def test_remove_label_unrelated_failure_still_raises(stub: Stub, gh: GitHub):
    stub.rule("issue", "edit", stderr="HTTP 503: service unavailable\n", exit=1)

    with pytest.raises(FactoryError, match="503"):
        gh.remove_label(42, "factory")


def test_ensure_label_existing_label_creates_nothing(stub: Stub, gh: GitHub):
    stub.json_rule("label", "list", payload=[{"name": "Factory"}, {"name": "bug"}])

    assert gh.ensure_label("factory") is False
    assert stub.called("label", "create") == []


def test_ensure_label_creates_with_color_description_and_force(stub: Stub, gh: GitHub):
    stub.json_rule("label", "list", payload=[{"name": "bug"}])
    stub.rule("label", "create", stdout="created\n")

    assert gh.ensure_label("factory", color="ff0000", description="factory intake") is True

    (create,) = stub.called("label", "create")
    assert create["argv"] == [
        "label",
        "create",
        "factory",
        "--color",
        "ff0000",
        "--description",
        "factory intake",
        "--force",
    ]


# ---------------------------------------------------------------- pull requests


def _pr(number: int, state: str = "OPEN", head: str = "factory/42", draft: bool = True) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/owner/name/pull/{number}",
        "isDraft": draft,
        "state": state,
        "headRefName": head,
    }


def test_find_pr_for_branch_prefers_open_and_filters_other_branches(stub: Stub, gh: GitHub):
    stub.json_rule(
        "pr",
        "list",
        payload=[_pr(110, "CLOSED"), _pr(117, "OPEN"), _pr(120, "OPEN", head="factory/9")],
    )

    pr = gh.find_pr_for_branch("factory/42")

    assert pr == PullRequest(
        number=117,
        url="https://github.com/owner/name/pull/117",
        is_draft=True,
        state="OPEN",
        head="factory/42",
    )
    (call,) = stub.calls()
    assert call["argv"] == [
        "pr",
        "list",
        "--head",
        "factory/42",
        "--state",
        "all",
        "--json",
        "number,url,isDraft,state,headRefName",
    ]


def test_find_pr_for_branch_falls_back_to_the_most_recent_closed_pr(stub: Stub, gh: GitHub):
    stub.json_rule("pr", "list", payload=[_pr(110, "CLOSED"), _pr(113, "MERGED")])

    pr = gh.find_pr_for_branch("factory/42")

    assert (pr.number, pr.state) == (113, "MERGED")


def test_find_pr_for_branch_returns_none_when_there_is_none(stub: Stub, gh: GitHub):
    stub.json_rule("pr", "list", payload=[])

    assert gh.find_pr_for_branch("factory/42") is None


def test_create_draft_pr_reuses_an_existing_open_pr(stub: Stub, gh: GitHub):
    stub.json_rule("pr", "list", payload=[_pr(117)])

    pr = gh.create_draft_pr(branch="factory/42", base="main", title="t", body="b")

    assert pr.number == 117
    assert stub.called("pr", "create") == []


def test_create_draft_pr_creates_when_only_a_closed_pr_exists(stub: Stub, gh: GitHub):
    stub.json_rule("pr", "list", payload=[_pr(110, "CLOSED")], once=True)
    stub.rule("pr", "create", stdout="https://github.com/owner/name/pull/118\n")
    stub.json_rule("pr", "list", payload=[_pr(110, "CLOSED"), _pr(118)])

    pr = gh.create_draft_pr(
        branch="factory/42", base="main", title="Add a widget", body="Closes #42\n" + "x" * 5000
    )

    assert (pr.number, pr.state, pr.is_draft) == (118, "OPEN", True)
    (create,) = stub.called("pr", "create")
    assert create["argv"][:8] == [
        "pr",
        "create",
        "--draft",
        "--head",
        "factory/42",
        "--base",
        "main",
        "--title",
    ]
    assert "--body" not in create["argv"]  # multi-KB bodies always go through a file
    assert create["body"].startswith("Closes #42")
    assert len(create["body"]) == len("Closes #42\n") + 5000


def test_create_draft_pr_falls_back_to_the_url_when_the_list_lags(stub: Stub, gh: GitHub):
    stub.json_rule("pr", "list", payload=[])
    stub.rule(
        "pr",
        "create",
        stdout="Creating pull request for factory/42 into main\n"
        "https://github.com/owner/name/pull/118\n",
    )

    pr = gh.create_draft_pr(branch="factory/42", base="main", title="t", body="b")

    assert pr == PullRequest(
        number=118,
        url="https://github.com/owner/name/pull/118",
        is_draft=True,
        state="OPEN",
        head="factory/42",
    )


def test_create_draft_pr_without_a_usable_url_raises(stub: Stub, gh: GitHub):
    stub.json_rule("pr", "list", payload=[])
    stub.rule("pr", "create", stdout="something went sideways\n")

    with pytest.raises(FactoryError, match="no pull request URL"):
        gh.create_draft_pr(branch="factory/42", base="main", title="t", body="b")


def test_pr_comment_skips_a_body_whose_marker_is_already_posted(stub: Stub, gh: GitHub):
    marker = GATE_MARKER.format(gate="open_questions", sha="abc123")
    stub.json_rule("pr", "view", payload={"comments": [{"body": "hi"}, {"body": marker + "\nold"}]})

    assert gh.pr_comment(117, marker + "\nnew", marker=marker) is False
    assert stub.called("pr", "comment") == []


def test_pr_comment_posts_through_a_body_file(stub: Stub, gh: GitHub):
    marker = GATE_MARKER.format(gate="open_questions", sha="abc123")
    stub.json_rule("pr", "view", payload={"comments": [{"body": "unrelated"}]})
    stub.rule("pr", "comment", stdout="https://github.com/owner/name/pull/117#issuecomment-1\n")

    assert gh.pr_comment(117, marker + "\nbody", marker=marker) is True

    (comment,) = stub.called("pr", "comment")
    assert comment["argv"][:3] == ["pr", "comment", "117"]
    assert comment["argv"][3] == "--body-file"
    assert comment["body"] == marker + "\nbody"


def test_pr_comment_without_a_marker_never_reads_comments(stub: Stub, gh: GitHub):
    stub.rule("pr", "comment", stdout="ok\n")

    assert gh.pr_comment(117, "body") is True
    assert stub.called("pr", "view") == []


def test_pr_comments_returns_bodies(stub: Stub, gh: GitHub):
    stub.json_rule("pr", "view", payload={"comments": [{"body": "one"}, {"body": "two"}]})

    assert gh.pr_comments(117) == ["one", "two"]


def test_pr_ready_treats_an_already_ready_pr_as_success(stub: Stub, gh: GitHub):
    stub.rule(
        "pr",
        "ready",
        stderr="! Pull request owner/name#117 is already marked as ready for review\n",
        exit=1,
    )

    gh.pr_ready(117)  # no raise

    assert stub.argvs() == [["pr", "ready", "117"]]


def test_pr_ready_propagates_a_real_failure(stub: Stub, gh: GitHub):
    stub.rule("pr", "ready", stderr="GraphQL: Could not resolve to a node\n", exit=1)

    with pytest.raises(FactoryError, match="Could not resolve"):
        gh.pr_ready(117)


def test_pr_close_passes_the_comment_and_tolerates_an_already_closed_pr(stub: Stub, gh: GitHub):
    stub.rule(
        "pr",
        "close",
        stderr="! Pull request owner/name#117 is already closed\n",
        exit=1,
    )

    gh.pr_close(117, "abandoned by `factory abandon 42`")

    assert stub.argvs() == [
        ["pr", "close", "117", "--comment", "abandoned by `factory abandon 42`"]
    ]


def test_pr_close_propagates_a_real_failure(stub: Stub, gh: GitHub):
    stub.rule("pr", "close", stderr="HTTP 403: Resource not accessible\n", exit=1)

    with pytest.raises(FactoryError, match="403"):
        gh.pr_close(117)


# ---------------------------------------------------------------- rendered comment bodies


def test_gate_comment_leads_with_its_marker_and_names_the_gate():
    body = render_gate_comment(
        "open_questions",
        "Answer them in work/42/spec.md, then `factory accept 42`.",
        "abc1234def",
        42,
    )

    assert body.splitlines()[0] == GATE_MARKER.format(gate="open_questions", sha="abc1234def")
    assert "open_questions" in body
    assert "Answer them in work/42/spec.md" in body
    assert "factory status 42" in body
    assert "abc1234" in body


def test_review_comment_carries_summary_stats_and_ledger():
    body = render_review_comment(
        2,
        "deadbeefcafe",
        "Two important findings remain.",
        "| id | severity |\n|---|---|\n| F3 | important |",
        {"important_open": 2, "important_resolved": 1, "nits": 4, "reraised_dropped": 1},
    )

    lines = body.splitlines()
    assert lines[0] == REVIEW_MARKER.format(round=2, sha="deadbeefcafe")
    assert "round 2" in body
    assert "deadbeef" in body
    assert "Two important findings remain." in body
    assert "Important open" in body and "| 2 |" in body
    assert "Re-raised after dismissal (dropped)" in body
    assert "| F3 | important |" in body


def test_review_comment_handles_an_empty_summary_and_unknown_stat_keys():
    body = render_review_comment(1, "abc", "", "", {"weird_metric": ["F1", "F2"]})

    assert "Weird metric" in body
    assert "F1, F2" in body
    assert "_No findings._" in body


def test_summary_comment_reports_stages_reviews_ledger_and_checks():
    state = {
        "issue": {"number": 42},
        "branch": "factory/42",
        "stages": {
            "spec": {
                "harness": "claude",
                "model": "claude-fake-1",
                "cli_version": "2.1.263",
                "auth": "api",
                "at": "2026-09-06T10:00:00Z",
            },
            "build": {
                "harness": "claude",
                "model": "claude-fake-1",
                "cli_version": "2.1.263",
                "auth": "api",
                "at": "2026-09-06T10:20:00Z",
            },
        },
        "reviews": [
            {
                "round": 1,
                "sha": "1111111111",
                "important_open": 1,
                "important_resolved": 0,
                "nits": 2,
            },
            {
                "round": 2,
                "sha": "2222222222",
                "important_open": 0,
                "important_resolved": 1,
                "nits": 0,
            },
        ],
        "fix_rounds": 1,
        "pr": {"number": 117, "url": "https://github.com/owner/name/pull/117"},
        "outcome": "done",
    }

    body = render_summary_comment(state, "| F1 | resolved |", "2 passed in 0.2s", "2222222222")

    assert body.splitlines()[0] == SUMMARY_MARKER.format(sha="2222222222")
    assert "factory/42" in body
    assert "work/42/spec.md" in body and "work/42/plan.md" in body
    assert "2.1.263" in body
    assert "spec" in body and "build" in body
    assert "Fix rounds: 1" in body
    assert "open Important at the last review: 0" in body
    assert "| F1 | resolved |" in body
    assert "```\n2 passed in 0.2s\n```" in body


def test_summary_comment_survives_a_sparse_state():
    body = render_summary_comment({}, "", "", "abc")

    assert "_(no stages recorded)_" in body
    assert "_(no review rounds)_" in body
    assert "_No findings._" in body
    assert "_(no output)_" in body


def test_summary_comment_truncates_a_huge_check_tail():
    body = render_summary_comment({"issue": {"number": 42}}, "", "x" * 50_000, "abc")

    assert "… (truncated)" in body
    assert len(body) < 20_000


def test_capped_comment_explains_the_counter_and_quotes_the_error():
    body = render_capped_comment(42, "abc1234def", 3, "error: check `make test` failed")

    assert body.splitlines()[0] == CAPPED_MARKER.format(sha="abc1234def")
    assert "3 time(s) in a row" in body
    assert "abc1234" in body
    assert "make test" in body
    assert "factory abandon 42" in body
