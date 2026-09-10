"""Unattended selection and retry limits, without live harness or GitHub calls."""

import contextlib
import copy
import io
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from factory.config import DEFAULTS
from factory.harness import doctor_key
from factory.locks import issue_lock
from factory.poll import poll
from factory.state import atomic_json, load_json


VERSION = "2.1.263 (Claude Code)"


class FakeRepo:
    def __init__(self, root):
        self.root = Path(root)
        self.local_dir = self.root / ".factory"
        self.local_dir.mkdir(exist_ok=True)
        self.entries = {}
        self.events = []
        self.fetch_error = None
        self.head_error = False
        self.push_error = None

    def add(self, issue, *, head=None, outcome=None, outcome_sha=None, remote=False, local=True):
        self.entries[issue] = {
            "head": head or f"head-{issue}", "remote": remote, "local": local,
            "state": {"issue": {"number": issue}, "stages": {}, "outcome": outcome, "outcome_sha": outcome_sha},
        }
        if local:
            self.worktree(issue)

    def fetch(self):
        self.events.append(("fetch",))
        if self.fetch_error:
            raise RuntimeError(self.fetch_error)

    def branch_exists(self, issue, remote=False):
        return self.entries.get(issue, {}).get("remote" if remote else "local", False)

    def worktree(self, issue, create=True):
        path = self.local_dir / "worktrees" / str(issue)
        if not create:
            return path if path.exists() else None
        if issue not in self.entries:
            self.entries[issue] = {"head": "base-head", "local": True, "remote": False, "state": {}}
        entry = self.entries[issue]
        entry["local"] = True
        if not path.exists():
            path.mkdir(parents=True)
            atomic_json(path / "work" / str(issue) / "state.json", entry["state"])
            self.events.append(("create", issue))
        return path

    def validate_clean(self, cwd, issue):
        self.events.append(("validate", issue))
        if (cwd / "partial.md").exists():
            self.events.append(("commit-partial", issue))
            self.entries[issue]["head"] = "committed-partial"

    def sync(self, cwd, issue):
        self.events.append(("sync", issue))

    def head(self, cwd):
        if self.head_error:
            raise RuntimeError("cannot read HEAD")
        return self.entries[int(cwd.name)]["head"]

    def git(self, *args):
        entry = self.entries[int(args[-1].rsplit("/", 1)[-1])]
        return entry.get("remote_head", entry["head"]) if "refs/remotes/" in args[-1] else entry["head"]

    def push(self, issue):
        self.events.append(("push", issue))
        if self.push_error:
            raise RuntimeError(self.push_error)
        self.entries[issue].update(remote=True, remote_head=self.entries[issue]["head"])

    def resolve(self, cwd, ref):
        if ref == "unreachable":
            raise RuntimeError("recorded commit is not reachable from HEAD")
        return ref

    def reset(self, cwd):
        self.events.append(("reset", int(cwd.name)))
        (cwd / "partial.md").unlink(missing_ok=True)


class FakeGitHub:
    def __init__(self, numbers):
        self.numbers = numbers
        self.calls = []

    def issues(self, label):
        self.calls.append(label)
        return [{"number": number} for number in self.numbers]


def _blocking_poll(root, started, release, results):
    """A second process keeps the real poll lock while discovery is underway."""
    repo = FakeRepo(root)
    github = FakeGitHub([])

    def discover(label):
        started.set()
        if not release.wait(10):
            raise RuntimeError("test release timed out")
        return []

    github.issues = discover
    with patch("factory.poll.make_harness") as make, contextlib.redirect_stdout(io.StringIO()):
        make.return_value.version.return_value = VERSION
        try:
            results.put(poll(repo, github, copy.deepcopy(DEFAULTS)))
        except Exception as exc:
            results.put(str(exc))


class PollTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = FakeRepo(self.temporary.name)
        self.github = FakeGitHub([])
        self.config = copy.deepcopy(DEFAULTS)
        self.key = doctor_key("claude", VERSION, "api")
        atomic_json(self.repo.local_dir / "doctor.json", {"records": {self.key: {"passed": True}}})
        env = patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-test-key"})
        env.start()
        self.addCleanup(env.stop)
        harness = patch("factory.poll.make_harness")
        self.harness = harness.start()
        self.harness.return_value.version.return_value = VERSION
        self.addCleanup(harness.stop)
        doctor_mock = patch("factory.poll.doctor", return_value={"passed": True})
        self.doctor = doctor_mock.start()
        self.addCleanup(doctor_mock.stop)
        execute = patch("factory.cli.execute_issue", return_value=0)
        self.execute = execute.start()
        self.addCleanup(execute.stop)
        self.output = io.StringIO()

    def run_poll(self):
        with contextlib.redirect_stdout(self.output):
            return poll(self.repo, self.github, self.config)

    def failures(self):
        return load_json(self.repo.local_dir / "poll.json", {})

    def write_failures(self, values):
        atomic_json(self.repo.local_dir / "poll.json", values)

    def test_skips_done_parked_and_capped_but_runs_fresh_and_changed_in_order(self):
        self.github.numbers = [6, 5, 4, 3, 2, 1]
        self.repo.add(1, outcome="done")
        self.repo.add(2, remote=True, outcome="needs_human:open_questions", outcome_sha="head-2")
        self.repo.add(3)
        self.repo.add(4, outcome="needs_human:open_questions", outcome_sha="old-head")
        self.repo.add(6, head="new-head")
        self.write_failures({"3": {"head": "head-3", "count": 3}, "6": {"head": "old-head", "count": 3}})
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual([call.args[3] for call in self.execute.call_args_list], [4, 5, 6])
        self.assertEqual(self.github.calls, ["factory"])
        self.assertIn("skip done", self.output.getvalue())
        self.assertIn("skip needs_human", self.output.getvalue())
        self.assertIn("skip consecutive failure cap", self.output.getvalue())
        self.assertEqual(self.failures(), {"3": {"head": "head-3", "count": 3}})
        self.doctor.assert_not_called()

    def test_subscription_and_missing_key_stop_before_github_or_harness(self):
        self.config["factory"]["auth"] = "subscription"
        with self.assertRaisesRegex(ValueError, "requires auth=api"):
            self.run_poll()
        self.config["factory"]["auth"] = "api"
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
            self.run_poll()
        self.assertEqual(self.github.calls, [])
        self.harness.assert_not_called()
        self.doctor.assert_not_called()

    def test_done_commit_with_failed_push_is_published_before_skipping(self):
        self.github.numbers = [1]
        self.repo.add(1, remote=True, outcome="done")
        self.repo.entries[1]["remote_head"] = "before-finalize"
        self.repo.push_error = "offline"
        self.assertEqual(self.run_poll(), 1)
        self.assertEqual(self.failures(), {"1": {"head": "head-1", "count": 1}})
        self.repo.push_error = None
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.failures(), {})
        self.assertEqual(self.repo.entries[1]["remote_head"], "head-1")
        self.repo.events.clear()
        self.assertEqual(self.run_poll(), 0)
        self.assertNotIn(("push", 1), self.repo.events)
        self.execute.assert_not_called()

    def test_done_publication_failure_is_bounded_at_same_head(self):
        self.github.numbers = [1]
        self.repo.add(1, outcome="done")
        self.repo.push_error = "offline"
        for _ in range(3):
            self.assertEqual(self.run_poll(), 1)
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.repo.events.count(("push", 1)), 3)
        self.execute.assert_not_called()

    def test_parked_gate_retries_recorded_publication_failure_then_skips(self):
        self.github.numbers = [1]
        self.repo.add(1, remote=True, outcome="needs_human:open_questions", outcome_sha="head-1")
        self.write_failures({"1": {"head": "head-1", "count": 1}})
        self.execute.return_value = 2
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.execute.call_count, 1)
        self.assertEqual(self.failures(), {})
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.execute.call_count, 1)
        self.assertIn("retry pending gate publication", self.output.getvalue())
        self.assertIn("skip needs_human:open_questions", self.output.getvalue())

    def test_parked_gate_with_unpublished_head_retries_without_failure_record(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                self.github.numbers = [1]
                self.repo.add(1, remote=remote, outcome="needs_human:open_questions", outcome_sha="head-1")
                self.repo.entries[1]["remote_head"] = "before-gate"

                def publish(repo, github, config, issue):
                    with issue_lock(repo, issue, "run"):
                        repo.push(issue)
                    return 2

                self.execute.side_effect = publish
                self.execute.reset_mock()
                self.assertEqual(self.run_poll(), 0)
                self.assertEqual(self.execute.call_count, 1)
                self.assertEqual(self.repo.entries[1]["remote_head"], "head-1")
                self.assertEqual(self.run_poll(), 0)
                self.assertEqual(self.execute.call_count, 1)

    def test_parked_gate_publication_retries_obey_failure_cap(self):
        self.github.numbers = [1]
        self.repo.add(1, remote=True, outcome="needs_human:open_questions", outcome_sha="head-1")
        self.write_failures({"1": {"head": "head-1", "count": 1}})
        self.execute.side_effect = RuntimeError("gate comment failed")
        for count in (2, 3):
            self.assertEqual(self.run_poll(), 1)
            self.assertEqual(self.failures()["1"]["count"], count)
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.execute.call_count, 2)

    def test_parked_gate_ignores_failure_record_from_different_head(self):
        self.github.numbers = [1]
        self.repo.add(1, remote=True, outcome="needs_human:open_questions", outcome_sha="head-1")
        self.write_failures({"1": {"head": "older-head", "count": 1}})
        self.assertEqual(self.run_poll(), 0)
        self.execute.assert_not_called()

    def test_parked_gate_replays_persisted_pending_notice_after_host_rebuild(self):
        self.github.numbers = [1]
        self.repo.add(1, remote=True, outcome="needs_human:open_questions", outcome_sha="head-1")
        path = self.repo.worktree(1) / "work/1/state.json"
        state = load_json(path)
        state["gate_notice"] = {"ref": "head-1", "sent": False}
        atomic_json(path, state)
        self.execute.return_value = 2
        self.assertEqual(self.failures(), {})
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.execute.call_count, 1)
        state["gate_notice"]["sent"] = True
        atomic_json(path, state)
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.execute.call_count, 1)

    def test_doctor_requires_matching_factory_harness_version_and_auth(self):
        for wrong_key in (doctor_key("claude", "old-version", "api"), doctor_key("claude", VERSION, "subscription"),
                          doctor_key("codex", VERSION, "api"), "0.0.1:claude:" + VERSION + ":api"):
            with self.subTest(key=wrong_key):
                atomic_json(self.repo.local_dir / "doctor.json", {"records": {wrong_key: {"passed": True}}})
                self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.doctor.call_count, 4)

    def test_failed_doctor_refuses_discovery(self):
        atomic_json(self.repo.local_dir / "doctor.json", {"records": {self.key: {"passed": False}}})
        self.doctor.return_value = {"passed": False}
        with self.assertRaisesRegex(RuntimeError, "doctor failed"):
            self.run_poll()
        self.assertEqual(self.github.calls, [])
        self.execute.assert_not_called()

    def test_retry_counts_use_final_head_and_success_or_human_gate_clears(self):
        self.github.numbers = [1, 2, 3]
        for issue in self.github.numbers:
            self.repo.add(issue)
        self.write_failures({"1": {"head": "head-1", "count": 1},
                             "2": {"head": "head-2", "count": 2},
                             "3": {"head": "head-3", "count": 2}})

        def execute(repo, github, config, issue):
            if issue == 1:
                repo.entries[issue]["head"] = "committed-before-push-failure"
                raise RuntimeError("push failed")
            return 0 if issue == 2 else 2

        self.execute.side_effect = execute
        self.assertEqual(self.run_poll(), 1)
        self.assertEqual(self.failures(), {"1": {"head": "committed-before-push-failure", "count": 1}})
        self.assertEqual(self.run_poll(), 1)
        self.assertEqual(self.failures()["1"]["count"], 2)

    def test_repeated_first_stage_failure_caps_at_created_branch_head(self):
        self.github.numbers = [7]

        def fail_after_branch(repo, github, config, issue):
            repo.worktree(issue)
            raise RuntimeError("spec harness failed")

        self.execute.side_effect = fail_after_branch
        for count in range(1, 4):
            self.assertEqual(self.run_poll(), 1)
            self.assertEqual(self.failures(), {"7": {"head": "base-head", "count": count}})
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.execute.call_count, 3)

    def test_failure_before_branch_creation_caps_at_null_head(self):
        self.github.numbers = [7]
        self.execute.side_effect = RuntimeError("cannot create branch")
        for _ in range(3):
            self.assertEqual(self.run_poll(), 1)
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.execute.call_count, 3)
        self.assertEqual(self.failures(), {"7": {"head": None, "count": 3}})

    def test_remote_only_run_is_recovered_before_classification(self):
        self.github.numbers = [9]
        self.repo.add(9, remote=True, local=False, outcome="needs_human:open_questions", outcome_sha="head-9")
        self.repo.events.clear()
        self.assertEqual(self.run_poll(), 0)
        self.assertEqual(self.repo.events[0], ("fetch",))
        self.assertIn(("create", 9), self.repo.events)
        self.assertTrue(self.repo.worktree(9, create=False).is_dir())
        self.execute.assert_not_called()

    def test_interrupted_stage_resets_before_partial_artifacts_can_be_committed(self):
        self.github.numbers = [1]
        self.repo.add(1)
        cwd = self.repo.worktree(1)
        (cwd / "partial.md").write_text("unfinished generated prompt")
        atomic_json(self.repo.local_dir / "run/1.json", {"pid": 0, "stage": "spec"})
        self.repo.events.clear()

        def execute(repo, github, config, issue):
            # Classification must release its locks before execute's normal lock.
            with issue_lock(repo, issue, "run"):
                self.assertFalse((cwd / "partial.md").exists())
            return 0

        self.execute.side_effect = execute
        self.assertEqual(self.run_poll(), 0)
        self.assertLess(self.repo.events.index(("reset", 1)), self.repo.events.index(("validate", 1)))
        self.assertNotIn(("commit-partial", 1), self.repo.events)
        self.assertFalse((self.repo.local_dir / "run/1.json").exists())

    def test_preparation_failures_and_head_lookup_errors_do_not_abort_queue(self):
        self.github.numbers = [1, 2]
        self.repo.add(1)
        self.repo.add(2)
        self.repo.fetch_error = "offline"
        self.repo.head_error = True
        self.assertEqual(self.run_poll(), 1)
        self.assertEqual(self.failures(), {"1": {"head": None, "count": 1}, "2": {"head": None, "count": 1}})
        self.execute.assert_not_called()

    def test_unreachable_recorded_commit_fails_before_execution(self):
        self.github.numbers = [1]
        self.repo.add(1)
        path = self.repo.worktree(1) / "work/1/state.json"
        state = load_json(path)
        state["stages"] = {"spec": {"commit": "unreachable"}}
        atomic_json(path, state)
        self.assertEqual(self.run_poll(), 1)
        self.assertIn("not reachable", self.output.getvalue())
        self.execute.assert_not_called()

    def test_two_poll_processes_cannot_overlap(self):
        context = multiprocessing.get_context("fork")
        started, release, results = context.Event(), context.Event(), context.Queue()
        process = context.Process(target=_blocking_poll, args=(str(self.repo.root), started, release, results))
        process.start()
        try:
            self.assertTrue(started.wait(5), "first poll did not reach discovery")
            self.assertEqual(self.run_poll(), 0)
            self.assertIn("Another poll is running", self.output.getvalue())
            self.assertEqual(self.github.calls, [])
            self.harness.assert_not_called()
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(2)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(results.get(timeout=2), 0)


if __name__ == "__main__":
    unittest.main()
