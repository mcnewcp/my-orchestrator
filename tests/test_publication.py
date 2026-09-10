"""Committed publication intent survives GitHub, acknowledgement, and push failures."""

import os
import unittest
from unittest.mock import patch

from factory.cli import execute_issue
from factory.harness import doctor_key
from factory.poll import poll
from factory.stages import Engine, NeedsHuman
from factory.state import atomic_json, load_json
import test_stages as fixtures


class PublicationTests(unittest.TestCase):
    # Reuse the local bare remote and fake adapters without inheriting its tests.
    setUp = fixtures.StageTests.setUp
    git = fixtures.StageTests.git
    remote_head = fixtures.StageTests.remote_head
    through_plan = fixtures.StageTests.through_plan
    through_build = fixtures.StageTests.through_build

    def resume(self):
        return Engine(self.repo, self.github, self.config, 42).prepare()

    def assert_parked_at_published_head(self, engine):
        self.assertEqual(engine.state["outcome"], "needs_human:open_questions")
        self.assertEqual(self.repo.resolve(engine.cwd, engine.state["outcome_sha"]), engine.head())
        self.assertEqual(engine.head(), self.remote_head())
        self.assertEqual(self.repo.changed_paths(engine.cwd), [])

    def test_failed_gate_comment_leaves_pending_notice_and_retries_without_model(self):
        self.adapter.open_questions = ["Which value is required?"]
        with patch.object(self.github, "comment", side_effect=RuntimeError("comment unavailable")):
            with self.assertRaisesRegex(RuntimeError, "comment unavailable"):
                execute_issue(self.repo, self.github, self.config, 42)
        pending = self.resume()
        notice_ref = pending.state["gate_notice"]["ref"]
        self.assertFalse(pending.state["gate_notice"]["sent"])
        self.assert_parked_at_published_head(pending)
        self.assertEqual(self.adapter.calls, ["spec"])
        self.assertEqual(self.github.comments, {})

        with self.assertRaisesRegex(NeedsHuman, "open_questions"):
            pending.run()
        acknowledged = load_json(pending.work / "state.json")
        self.assertEqual(acknowledged["gate_notice"], {"ref": notice_ref, "sent": True})
        self.assert_parked_at_published_head(pending)
        marker = f"factory:gate:open_questions:{self.repo.resolve(pending.cwd, notice_ref)}"
        self.assertEqual(list(self.github.comments), [marker])
        head = pending.head()
        with self.assertRaisesRegex(NeedsHuman, "open_questions"):
            self.resume().run()
        self.assertEqual(pending.head(), head)
        self.assertEqual(self.adapter.calls, ["spec"])
        self.assertEqual(self.github.pr_creates, 1)

    def test_delivered_gate_comment_with_failed_ack_commit_reuses_same_marker(self):
        self.adapter.open_questions = ["Which value is required?"]
        real_commit = self.repo.commit

        def fail_ack(cwd, message):
            if "record open_questions notification" in message:
                raise RuntimeError("acknowledgement commit failed")
            return real_commit(cwd, message)

        # The fixture's comment store uses setdefault by marker, like the real
        # adapter's idempotency check. Track attempts separately from deliveries.
        with patch.object(self.github, "comment", wraps=self.github.comment) as comment:
            with patch.object(self.repo, "commit", side_effect=fail_ack):
                with self.assertRaisesRegex(RuntimeError, "acknowledgement commit failed"):
                    execute_issue(self.repo, self.github, self.config, 42)
            pending = self.resume()
            self.assertFalse(pending.state["gate_notice"]["sent"])
            self.assert_parked_at_published_head(pending)
            notice_ref = pending.state["gate_notice"]["ref"]
            self.assertEqual(len(self.github.comments), 1)
            self.assertEqual(comment.call_count, 1)

            with self.assertRaisesRegex(NeedsHuman, "open_questions"):
                pending.run()
            self.assertEqual(comment.call_count, 2)
            self.assertEqual(comment.call_args_list[0].args[2], comment.call_args_list[1].args[2])
        self.assertEqual(len(self.github.comments), 1)
        self.assertEqual(pending.state["gate_notice"], {"ref": notice_ref, "sent": True})
        self.assert_parked_at_published_head(pending)
        self.assertEqual(self.adapter.calls, ["spec"])

    def test_failed_ack_push_retries_publish_without_redelivering_notice(self):
        self.adapter.open_questions = ["Which value is required?"]
        real_push = self.repo.push

        def fail_ack_push(issue, force=False):
            state = load_json(self.engine.work / "state.json", {})
            if state.get("gate_notice", {}).get("sent"):
                raise RuntimeError("acknowledgement push failed")
            return real_push(issue, force=force)

        with patch.object(self.repo, "push", side_effect=fail_ack_push):
            with self.assertRaisesRegex(RuntimeError, "acknowledgement push failed"):
                execute_issue(self.repo, self.github, self.config, 42)
        resumed = self.resume()
        self.assertTrue(resumed.state["gate_notice"]["sent"])
        self.assertNotEqual(resumed.head(), self.remote_head())
        with patch.object(self.github, "comment", wraps=self.github.comment) as comment:
            with self.assertRaisesRegex(NeedsHuman, "open_questions"):
                resumed.run()
            comment.assert_not_called()
        self.assert_parked_at_published_head(resumed)
        self.assertEqual(len(self.github.comments), 1)
        self.assertEqual(self.adapter.calls, ["spec"])

    def test_committed_review_with_failed_comment_replays_and_finishes_without_new_review(self):
        self.through_build()
        with patch.object(self.github, "comment", side_effect=RuntimeError("review comment unavailable")):
            with self.assertRaisesRegex(RuntimeError, "review comment unavailable"):
                execute_issue(self.repo, self.github, self.config, 42, "review")
        resumed = self.resume()
        self.assertEqual(len(resumed.state["reviews"]), 1)
        review_ref = resumed.state["reviews"][0]["sha"]
        self.assertEqual(self.repo.resolve(resumed.cwd, review_ref), resumed.head())
        self.assertEqual(resumed.head(), self.remote_head())
        self.assertEqual(self.adapter.calls, ["spec", "plan", "build", "review"])
        self.assertEqual(self.github.comments, {})

        resumed.run()
        self.assertEqual(resumed.state["outcome"], "done")
        self.assertEqual(self.github.ready_calls, [142])
        marker = f"factory:review:{self.repo.resolve(resumed.cwd, review_ref)}"
        self.assertIn(marker, self.github.comments)
        self.assertEqual(len(resumed.state["reviews"]), 1)
        self.assertEqual(self.adapter.calls, ["spec", "plan", "build", "review"])
        self.assertEqual(resumed.head(), self.remote_head())
        self.assertEqual(self.repo.changed_paths(resumed.cwd), [])

    def test_poll_recovers_failed_force_push_and_resumes_without_repeating_build(self):
        self.through_build()
        original_remote = self.remote_head()
        with patch.object(self.repo, "push", side_effect=RuntimeError("forced push unavailable")):
            with self.assertRaisesRegex(RuntimeError, "forced push unavailable"):
                execute_issue(self.repo, self.github, self.config, 42, "build", force=True)
        pending_head = self.engine.head()
        pending_state = load_json(self.engine.work / "state.json")
        self.assertNotEqual(pending_head, original_remote)
        self.assertEqual(self.remote_head(), original_remote)
        self.assertEqual(pending_state["rewrite_lease"]["expected_sha"], original_remote)
        builds = self.adapter.calls.count("build")
        self.config["factory"]["auth"] = "api"
        version = "fake installed version"
        key = doctor_key("claude", version, "api")
        atomic_json(self.repo.local_dir / "doctor.json", {"records": {key: {"passed": True}}})

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-test-key"}), \
                patch("factory.poll.make_harness") as harness, \
                patch.object(self.github, "issues", return_value=[{"number": 42}], create=True):
            harness.return_value.version.return_value = version
            self.assertEqual(poll(self.repo, self.github, self.config), 0)

        completed = self.resume()
        self.assertEqual(completed.state["outcome"], "done")
        self.assertEqual(self.adapter.calls.count("build"), builds)
        self.assertEqual(self.adapter.calls.count("review"), 1)
        self.assertEqual(self.repo.resolve(completed.cwd, completed.state["stages"]["build"]["commit"]), pending_head)
        self.assertEqual(completed.head(), self.remote_head())
        self.assertEqual(self.github.ready_calls, [142])


if __name__ == "__main__":
    unittest.main()
