import json
import subprocess
from pathlib import Path

import pytest

from factory.errors import Blocked, Interrupted
from factory.github import GitHub

SHA = "a" * 40
OTHER_SHA = "b" * 40
BRANCH = "factory/42/run-1"


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.guard = None

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        response = self.responses.pop(0)
        if callable(response):
            response = response(argv, kwargs)
        if isinstance(response, subprocess.CompletedProcess):
            return response
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")


def pull_request(**overrides):
    return {
        "number": 17,
        "url": "https://github.com/owner/repo/pull/17",
        "title": "A change",
        "body": "Closes #42\n\n<!-- factory-run:run-1 -->\n",
        "state": "OPEN",
        "isDraft": True,
        "headRefOid": SHA,
        "headRefName": BRANCH,
        "baseRefName": "main",
        "headRepository": {"name": "repo"},
        "headRepositoryOwner": {"login": "owner"},
        **overrides,
    }


def check_run(name="tests", conclusion="success", status="completed", **overrides):
    return {
        "id": 10,
        "name": name,
        "conclusion": conclusion,
        "status": status,
        "head_sha": SHA,
        "app": {"id": 1},
        **overrides,
    }


def pages(*responses):
    # gh api --paginate writes each JSON response consecutively, without a separator.
    return subprocess.CompletedProcess([], 0, "".join(json.dumps(p) for p in responses), "")


def ci_runner(checks=(), statuses=()):
    return FakeRunner(
        [
            pages({"check_runs": list(checks)}),
            pages({"sha": SHA, "statuses": list(statuses)}),
        ]
    )


def test_issue_snapshot_records_retrieval_time():
    runner = FakeRunner([{"title": "Example", "body": "Frozen", "url": "https://example/42"}])
    issue = GitHub("owner/repo", runner).issue(42)
    assert issue["body"] == "Frozen"
    assert issue["retrieved_at"].endswith("+00:00")
    assert len(runner.calls) == 1


def test_create_adopts_existing_run_pr_without_external_write():
    runner = FakeRunner([[pull_request()]])
    result = GitHub("owner/repo", runner).create_draft(BRANCH, "main", "Title", "Body")
    assert result["number"] == 17
    assert len(runner.calls) == 1
    assert runner.calls[0][0][1:3] == ["pr", "list"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"body": "Someone else's PR"},
        {"baseRefName": "release"},
        {"headRepositoryOwner": {"login": "someone-else"}},
        {"state": "CLOSED"},
    ],
)
def test_existing_wrong_or_closed_pr_cannot_be_reused(overrides):
    runner = FakeRunner([[pull_request(**overrides)]])
    with pytest.raises(Blocked):
        GitHub("owner/repo", runner).create_draft(BRANCH, "main", "Title", "Body")
    assert len(runner.calls) == 1


def test_uncertain_creation_reconciles_one_pr_and_preserves_multiline_body():
    def create(argv, kwargs):
        assert argv[1:3] == ["pr", "create"]
        assert "--draft" in argv and "--no-maintainer-edit" in argv
        body_file = Path(argv[argv.index("--body-file") + 1])
        assert body_file.read_text() == "Literal `echo no`\n$(do-not-execute)\n\n" + (
            "<!-- factory-run:run-1 -->\n"
        )
        return subprocess.CompletedProcess(argv, 1, "", "response disconnected")

    runner = FakeRunner([[], create, [pull_request()]])
    result = GitHub("owner/repo", runner).create_draft(
        BRANCH, "main", "Title", "Literal `echo no`\n$(do-not-execute)"
    )
    assert result["number"] == 17
    assert len([argv for argv, _ in runner.calls if argv[1:3] == ["pr", "create"]]) == 1


def test_multiple_prs_block_ambiguous_reconciliation():
    runner = FakeRunner([[pull_request(), pull_request(number=18)]])
    with pytest.raises(Blocked, match="Multiple PRs"):
        GitHub("owner/repo", runner).find_pr(BRANCH)


@pytest.mark.parametrize(
    "conclusion",
    [
        "failure",
        "skipped",
        "cancelled",
        "neutral",
        "timed_out",
        "action_required",
        None,
    ],
)
def test_every_non_success_completed_check_blocks(conclusion):
    runner = ci_runner([check_run(conclusion=conclusion)])
    with pytest.raises(Blocked, match="Required CI failed"):
        GitHub("owner/repo", runner).wait_ci(SHA, ["tests"], timeout=0)


@pytest.mark.parametrize(
    "checks, reason",
    [
        ([], "missing"),
        ([check_run(conclusion=None, status="in_progress")], "pending"),
    ],
)
def test_missing_or_pending_required_ci_never_succeeds(checks, reason):
    runner = ci_runner(checks)
    with pytest.raises(Blocked, match=reason):
        GitHub("owner/repo", runner).wait_ci(SHA, ["tests"], timeout=0)


def test_ci_uses_exact_sha_and_accepts_all_required_successes():
    runner = ci_runner([check_run()], [{"id": 20, "context": "lint", "state": "success"}])
    checks = GitHub("owner/repo", runner).wait_ci(SHA, ["tests", "lint"], timeout=0)
    assert [(c["name"], c["state"]) for c in checks] == [
        ("tests", "success"),
        ("lint", "success"),
    ]
    assert all(SHA in argv[2] for argv, _ in runner.calls)
    assert all("--paginate" in argv and "--slurp" not in argv for argv, _ in runner.calls)


def test_required_checks_and_statuses_may_appear_on_later_pages():
    runner = FakeRunner(
        [
            pages({"check_runs": []}, {"check_runs": [check_run()]}),
            pages(
                {"sha": SHA, "statuses": []},
                {"sha": SHA, "statuses": [{"id": 20, "context": "lint", "state": "success"}]},
            ),
        ]
    )
    checks = GitHub("owner/repo", runner).checks(SHA, ["tests", "lint"])
    assert [check["state"] for check in checks] == ["success", "success"]


@pytest.mark.parametrize("suffix", ["garbage", '{"check_runs":', "NaN", "\x00"])
@pytest.mark.parametrize("endpoint", ["check_runs", "statuses"])
def test_malformed_later_ci_page_cannot_accept_earlier_success(endpoint, suffix):
    checks = pages({"check_runs": [check_run()]})
    statuses = pages({"sha": SHA, "statuses": []})
    response = checks if endpoint == "check_runs" else statuses
    response.stdout += suffix
    runner = FakeRunner([checks, statuses])
    with pytest.raises(Blocked, match="invalid JSON"):
        GitHub("owner/repo", runner).checks(SHA, ["tests"])


def test_later_failed_check_invalidates_earlier_success():
    runner = FakeRunner(
        [
            pages(
                {"check_runs": [check_run()]},
                {"check_runs": [check_run(id=20, conclusion="failure")]},
            ),
            pages({"sha": SHA, "statuses": []}),
        ]
    )
    assert GitHub("owner/repo", runner).checks(SHA, ["tests"])[0]["state"] == "failure"


def test_stale_check_run_revision_blocks():
    runner = ci_runner([check_run(head_sha=OTHER_SHA)])
    with pytest.raises(Blocked, match="different candidate SHA"):
        GitHub("owner/repo", runner).checks(SHA, ["tests"])


def test_stale_status_revision_blocks():
    runner = FakeRunner(
        [
            pages({"check_runs": [check_run()]}),
            pages({"sha": SHA, "statuses": []}, {"sha": OTHER_SHA, "statuses": []}),
        ]
    )
    with pytest.raises(Blocked, match="different candidate SHA"):
        GitHub("owner/repo", runner).checks(SHA, ["tests"])


def test_successful_status_cannot_hide_a_failed_check_with_same_name():
    runner = ci_runner(
        [check_run(conclusion="failure")],
        [
            {"id": 20, "context": "tests", "state": "success"},
        ],
    )
    with pytest.raises(Blocked, match="Required CI failed"):
        GitHub("owner/repo", runner).wait_ci(SHA, ["tests"], timeout=0)


def test_latest_pending_status_invalidates_old_success():
    runner = ci_runner(
        [],
        [
            {"id": 20, "context": "tests", "state": "pending"},
            {"id": 10, "context": "tests", "state": "success"},
        ],
    )
    assert GitHub("owner/repo", runner).checks(SHA, ["tests"])[0]["state"] == "pending"


def test_same_name_in_two_apps_requires_both_to_succeed():
    runner = ci_runner(
        [
            check_run(),
            check_run(id=20, conclusion="failure", app={"id": 2}),
        ]
    )
    assert GitHub("owner/repo", runner).checks(SHA, ["tests"])[0]["state"] == "failure"


def test_ci_wait_stops_when_worker_lifetime_guard_fails():
    runner = ci_runner([])
    invocations = 0

    def guard():
        nonlocal invocations
        invocations += 1
        if invocations == 2:
            raise Interrupted("database lock lost")

    runner.guard = guard
    with pytest.raises(Interrupted, match="database lock lost"):
        GitHub("owner/repo", runner).wait_ci(SHA, ["tests"], timeout=60, poll_seconds=30)
    assert invocations == 2


def test_ready_is_idempotent_but_detects_concurrent_head_change():
    runner = FakeRunner([pull_request(), {}, pull_request(isDraft=False, headRefOid=OTHER_SHA)])
    with pytest.raises(Blocked, match="head changed"):
        GitHub("owner/repo", runner).ready(17)
    runner = FakeRunner([pull_request(isDraft=False), pull_request(isDraft=False)])
    assert GitHub("owner/repo", runner).ready(17)["isDraft"] is False
    assert all(argv[1:3] == ["pr", "view"] for argv, _ in runner.calls)


def test_ready_refuses_changed_candidate_before_external_write():
    runner = FakeRunner([pull_request(headRefOid=OTHER_SHA)])
    with pytest.raises(Blocked, match="differs from the accepted candidate"):
        GitHub("owner/repo", runner).ready(17, expected_sha=SHA)
    assert len(runner.calls) == 1


def test_ci_transport_calls_share_the_overall_deadline():
    runner = ci_runner([check_run()])
    GitHub("owner/repo", runner, timeout=60).wait_ci(SHA, ["tests"], timeout=10)
    timeouts = [kwargs["timeout"] for _, kwargs in runner.calls]
    assert 0 < timeouts[1] <= timeouts[0] <= 10
