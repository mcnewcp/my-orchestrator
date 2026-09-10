"""Local publication state survives GitHub failures without metadata commits."""

import unittest
from unittest.mock import patch

from factory.cli import execute_issue
from factory.stages import Engine, NeedsHuman
from factory.state import load_json
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

    def test_failed_gate_comment_retries_from_local_state_without_model_or_commit(self):
        self.through_build()
        self.config["factory"]["max_fix_rounds"] = 0
        self.adapter.reviews = [fixtures.review(fixtures.finding())]
        head = self.engine.head()
        original_comment = self.github.comment

        def fail_gate(number, body, marker):
            if marker.startswith("factory:gate:"):
                raise RuntimeError("comment unavailable")
            original_comment(number, body, marker)

        with patch.object(self.github, "comment", side_effect=fail_gate):
            with self.assertRaisesRegex(RuntimeError, "comment unavailable"):
                execute_issue(self.repo, self.github, self.config, 42)
        pending = self.resume()
        self.assertFalse(pending.state["gate_notice"]["sent"])
        self.assertEqual(pending.state["outcome_sha"], head)
        calls = list(self.adapter.calls)
        with self.assertRaisesRegex(NeedsHuman, "rounds_exhausted"):
            pending.run()
        self.assertEqual(load_json(pending.work / "state.json")["gate_notice"], {"sha": head, "sent": True})
        self.assertEqual(pending.head(), head)
        self.assertEqual(self.remote_head(), head)
        marker = f"factory:gate:rounds_exhausted:{head}"
        self.assertIn(marker, self.github.comments)
        with self.assertRaisesRegex(NeedsHuman, "rounds_exhausted"):
            self.resume().run()
        self.assertEqual(self.adapter.calls, calls)
        self.assertEqual(self.github.pr_creates, 1)

    def test_saved_review_with_failed_comment_finishes_without_new_review(self):
        self.through_build()
        head = self.engine.head()
        with patch.object(self.github, "comment", side_effect=RuntimeError("review comment unavailable")):
            with self.assertRaisesRegex(RuntimeError, "review comment unavailable"):
                execute_issue(self.repo, self.github, self.config, 42, "review")
        resumed = self.resume()
        self.assertEqual(len(resumed.state["reviews"]), 1)
        self.assertEqual(resumed.state["reviews"][0]["sha"], head)
        self.assertEqual(self.github.comments, {})
        resumed.run()
        self.assertEqual(resumed.state["outcome"], "done")
        self.assertEqual(self.github.ready_calls, [142])
        self.assertIn(f"factory:review:{head}:1", self.github.comments)
        self.assertEqual(self.adapter.calls, ["spec", "plan", "build", "review"])
        self.assertEqual(resumed.head(), head)
        self.assertEqual(self.remote_head(), head)

    def test_failed_pr_creation_retries_from_saved_build_without_new_commit(self):
        self.through_plan()
        with patch.object(self.github, "ensure_pr", side_effect=RuntimeError("PR unavailable")):
            with self.assertRaisesRegex(RuntimeError, "PR unavailable"):
                execute_issue(self.repo, self.github, self.config, 42, "build")
        head = self.engine.head()
        resumed = self.resume()
        self.assertIsNone(resumed.state["pr"])
        self.assertEqual(resumed.state["stages"]["build"]["commit"], head)
        resumed.run()
        self.assertEqual(self.adapter.calls, ["spec", "plan", "build", "review"])
        self.assertEqual(resumed.head(), head)
        self.assertEqual(resumed.state["outcome"], "done")


if __name__ == "__main__":
    unittest.main()
