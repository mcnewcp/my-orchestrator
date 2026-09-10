"""Stage integration tests with real local Git and fake harness/GitHub adapters."""

import copy
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from factory.cli import execute_issue, status
from factory.config import DEFAULTS
from factory.harness import HarnessResult
from factory.repo import Repo
from factory.stages import Engine, NeedsHuman
from factory.state import atomic_json, load_json, open_important


def finding(title="Missing input guard"):
    return {"severity": "important", "pass": "bugs", "file": "src/value.py", "line": 1,
            "title": title, "detail": "Input requires a guard.",
            "evidence": "src/value.py:1 accepts an invalid input without a guard."}


def review(*new, updates=()):
    return {"summary": "Reviewed the candidate.", "updates": list(updates), "new": list(new)}


def update(finding_id, status="resolved"):
    return {"id": finding_id, "status": status, "evidence": "Checked src/value.py and its guard."}


class FakeGitHub:
    def __init__(self):
        self.issue_reads = 0
        self.pr_creates = 0
        self.comments = {}
        self.ready_calls = []
        self.draft_calls = []

    def issue(self, number):
        self.issue_reads += 1
        return {"number": number, "title": "Implement value", "body": "Return a useful value.",
                "url": f"https://example.invalid/issues/{number}", "labels": [{"name": "factory"}]}

    def ensure_pr(self, issue, branch, base, title, body):
        self.pr_creates += 1
        self.pr_body = body
        return {"number": issue + 100, "url": f"https://example.invalid/pull/{issue + 100}"}

    def comment(self, number, body, key):
        self.comments.setdefault(key, (number, body))

    def update_pr(self, number, body):
        self.pr_body = body

    def ready(self, number):
        self.ready_calls.append(number)

    def draft(self, number):
        self.draft_calls.append(number)


class FakeHarness:
    def __init__(self):
        self.calls = []
        self.open_questions = []
        self.reviews = []
        self.mutations = {}

    def run(self, **kwargs):
        stage = kwargs["schema_file"].stem
        self.calls.append(stage)
        cwd = kwargs["cwd"]
        if stage in self.mutations:
            self.mutations[stage](cwd)
        elif stage == "build":
            (cwd / "src/value.py").write_text("VALUE = 1\n")
        elif stage == "fix":
            with (cwd / "src/value.py").open("a") as stream:
                stream.write("# Add the requested input guard.\n")
        if stage == "spec":
            output = {"markdown": "# Value\nImplement a useful value with passing checks.",
                      "open_questions": self.open_questions}
        elif stage == "plan":
            output = {"markdown": "# Plan\n## Files that change\n- `src/value.py`\n- `tests/`\n## Proof\nRun configured checks.\n"}
        elif stage == "build":
            output = {"summary": "Implemented the value.", "deviations": []}
        elif stage == "review":
            output = self.reviews.pop(0) if self.reviews else review()
        else:
            ledger = load_json(kwargs["prompt_file"].parent.parent / "findings.json")
            output = {"addressed": [{"id": f["id"], "how": "Added the guard."}
                                    for f in open_important(ledger)], "not_addressed": []}
        transcript = cwd.parents[1] / "transcripts/fake-transcript.json"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("{}\n")
        return HarnessResult(output, transcript, 0, "fake 1.0.0")


class StageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        parent = Path(self.temporary.name)
        self.root, self.remote = parent / "repo", parent / "origin.git"
        self.git("init", "--bare", "-q", str(self.remote), cwd=parent)
        self.git("init", "-q", "-b", "main", str(self.root), cwd=parent)
        self.git("config", "user.name", "Factory tests")
        self.git("config", "user.email", "factory@example.invalid")
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src/value.py").write_text("VALUE = 0\n")
        (self.root / "tests/test_value.py").write_text("# Preserved test assertion\n")
        (self.root / ".gitignore").write_text(".factory/\n__pycache__/\n")
        (self.root / "AGENTS.md").write_text("Implement bounded issues; Python owns commits.\n")
        (self.root / "CLAUDE.md").write_text("@AGENTS.md\n")
        (self.root / "REVIEW.md").write_text("Review bugs, security, compliance. Nit cap: 5\n")
        (self.root / "Makefile").write_text("test:\n\t@true\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial fixture")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-qu", "origin", "main")
        self.config = copy.deepcopy(DEFAULTS)
        self.config["factory"].update(auth="subscription", checks=[[
            sys.executable, "-c", "from pathlib import Path; assert 'VALUE = -1' not in Path('src/value.py').read_text(); print('checks green')"
        ]])
        self.repo = Repo(self.root)
        self.github, self.adapter = FakeGitHub(), FakeHarness()
        harness_patch = patch("factory.stages.make_harness", return_value=self.adapter)
        harness_patch.start()
        self.addCleanup(harness_patch.stop)
        self.engine = Engine(self.repo, self.github, self.config, 42).prepare()

    def git(self, *args, cwd=None):
        result = subprocess.run(["git", *args], cwd=cwd or self.root, check=True,
                                capture_output=True, text=True)
        return result.stdout.strip()

    def remote_head(self):
        return self.git("rev-parse", "refs/heads/factory/42", cwd=self.remote)

    def through_plan(self):
        self.engine.spec()
        self.engine.plan()

    def through_build(self):
        self.through_plan()
        self.engine.build()

    def through_review(self, *new):
        self.adapter.reviews = [review(*new)]
        self.through_build()
        self.engine.review()

    def operator_commit(self, path="src/value.py", text="# Operator correction\n"):
        with (self.engine.cwd / path).open("a") as stream:
            stream.write(text)
        self.repo.commit(self.engine.cwd, "operator correction")

    def operator_edit_plan(self, text):
        with (self.engine.work / "plan.md").open("a") as stream:
            stream.write(text)

    def assert_candidate_not_published(self, action, exception, message):
        local_before, remote_before = self.engine.head(), self.remote_head()
        with self.assertRaisesRegex(exception, message):
            action()
        self.assertEqual(self.engine.head(), local_before)
        self.assertEqual(self.remote_head(), remote_before)
        # The CLI's transaction wrapper performs this reset for failed stages.
        self.repo.reset(self.engine.cwd, local_before)
        self.engine.reload()
        self.assertEqual(self.repo.changed_paths(self.engine.cwd), [])

    def test_end_to_end_run_keeps_artifacts_local_and_records_plain_shas(self):
        self.engine.run()
        self.assertEqual(self.adapter.calls, ["spec", "plan", "build", "review"])
        self.assertEqual(self.github.ready_calls, [142])
        self.assertEqual(self.github.issue_reads, 1)
        self.assertEqual(self.github.pr_creates, 1)
        self.assertEqual(self.engine.state["outcome"], "done")
        self.assertEqual(self.engine.head(), self.remote_head())
        self.assertEqual(self.repo.changed_paths(self.engine.cwd), [])
        self.assertEqual(self.engine.work, self.root / ".factory/issues/42")
        for name in ("intent.md", "spec.md", "plan.md", "state.json", "findings.json",
                     "prompts/spec-1.md", "prompts/plan-1.md", "prompts/build-1.md",
                     "prompts/review-1.md", "checks/baseline-1.log", "checks/build-1.log",
                     "checks/finalize-1.log", "review-1.json", "review-1.diff"):
            self.assertTrue((self.engine.work / name).is_file(), name)
        self.assertTrue((self.repo.local_dir / "transcripts/fake-transcript.json").is_file())
        self.assertFalse((self.engine.cwd / "work").exists())
        self.assertEqual(self.git("log", "--format=%s", "main..factory/42"), "factory(42): build")
        self.assertEqual(self.git("diff", "--name-only", "main...factory/42"), "src/value.py")
        self.assertEqual(self.git("ls-tree", "--name-only", "factory/42", "work", ".factory"), "")
        for stage in self.engine.state["stages"].values():
            self.assertRegex(stage["commit"], r"^[0-9a-f]{40}$")
            self.assertEqual(self.repo.resolve(self.engine.cwd, stage["commit"]), stage["commit"])
        final_body = self.github.comments[f"factory:finalize:{self.engine.head()}"][1]
        for body in (self.github.pr_body, final_body):
            self.assertIn("<summary>Specification</summary>", body)
            self.assertIn("<summary>Plan</summary>", body)
            self.assertIn((self.engine.work / "spec.md").read_text().strip(), body)
            self.assertIn((self.engine.work / "plan.md").read_text().strip(), body)
            self.assertEqual(body.count("<details>"), 2)
        self.assertEqual(self.repo.resolve(self.engine.cwd, self.engine.state["outcome_sha"]), self.engine.head())
        rebuilt = Engine(self.repo, self.github, self.config, 42).prepare()
        rebuilt.run()
        self.assertEqual(self.github.ready_calls, [142])
        self.assertEqual(self.adapter.calls, ["spec", "plan", "build", "review"])

    def test_open_questions_park_unchanged_without_new_harness_calls(self):
        self.adapter.open_questions = ["What value is required?"]
        with self.assertRaisesRegex(NeedsHuman, "open_questions"):
            self.engine.run()
        head, comment_count = self.engine.head(), len(self.github.comments)
        for _ in range(2):
            engine = Engine(self.repo, self.github, self.config, 42).prepare()
            with self.assertRaisesRegex(NeedsHuman, "open_questions"):
                engine.run()
        self.assertEqual(self.adapter.calls, ["spec"])
        self.assertEqual(self.engine.head(), head)
        self.assertEqual(len(self.github.comments), comment_count)
        self.assertEqual(self.github.pr_creates, 0)
        spec = self.engine.work / "spec.md"
        spec.write_text(spec.read_text() + "\nOperator answer: return one.\n")
        self.assertEqual(execute_issue(self.repo, self.github, self.config, 42, "accept"), 0)
        self.assertEqual(self.engine.head(), head)
        resumed = Engine(self.repo, self.github, self.config, 42).prepare()
        resumed.run()
        self.assertEqual(resumed.state["outcome"], "done")
        self.assertIn("Operator answer: return one.", (resumed.work / "prompts/plan-1.md").read_text())
        self.assertEqual(self.github.issue_reads, 1)

    def test_red_baseline_parks_without_running_build_or_pushing_candidate(self):
        self.through_plan()
        candidate_before = (self.engine.cwd / "src/value.py").read_text()
        self.config["factory"]["checks"] = [[sys.executable, "-c", "raise SystemExit(1)"]]
        with self.assertRaisesRegex(NeedsHuman, "baseline_failing"):
            self.engine.build()
        self.assertEqual(self.adapter.calls, ["spec", "plan"])
        self.assertNotIn("build", self.engine.state["stages"])
        self.assertEqual((self.engine.cwd / "src/value.py").read_text(), candidate_before)
        self.assertEqual(self.git("show", "factory/42:src/value.py", cwd=self.remote) + "\n", candidate_before)

    def test_red_baseline_cannot_publish_files_changed_by_checks(self):
        self.through_plan()
        self.config["factory"]["checks"] = [[sys.executable, "-c",
            "from pathlib import Path; Path('src/value.py').write_text('VALUE = 999\\n'); raise SystemExit(1)"]]
        self.assert_candidate_not_published(self.engine.build, RuntimeError, "baseline checks modified")
        self.assertEqual((self.engine.cwd / "src/value.py").read_text(), "VALUE = 0\n")

    def test_red_build_checks_cannot_push_candidate(self):
        self.through_plan()
        self.adapter.mutations["build"] = lambda cwd: (cwd / "src/value.py").write_text("VALUE = -1\n")
        self.assert_candidate_not_published(self.engine.build, RuntimeError, "build checks failed")
        self.assertNotIn("build", self.engine.state["stages"])

    def test_green_baseline_cannot_rewrite_source(self):
        self.through_plan()
        self.config["factory"]["checks"] = [[sys.executable, "-c",
            "from pathlib import Path; Path('src/value.py').write_text('VALUE = 999\\n')"]]
        self.assert_candidate_not_published(self.engine.build, RuntimeError, "baseline checks modified")
        self.assertNotIn("build", self.adapter.calls)

    def test_green_candidate_check_cannot_rewrite_source(self):
        self.through_plan()
        self.config["factory"]["checks"] = [[sys.executable, "-c",
            "from pathlib import Path; p = Path('src/value.py'); "
            "p.write_text('VALUE = 999\\n') if 'VALUE = 1' in p.read_text() else None"]]
        self.assert_candidate_not_published(self.engine.build, RuntimeError, "checks modified candidate")

    def test_build_cannot_change_its_factory_prompt(self):
        self.through_plan()
        self.adapter.mutations["build"] = lambda cwd: (self.engine.work / "prompts/build-1.md").write_text("altered audit trail\n")
        self.assert_candidate_not_published(self.engine.build, ValueError, "forbidden paths under .factory/")

    def test_protected_build_edit_fails_before_candidate_checks(self):
        self.through_plan()
        self.adapter.mutations["build"] = lambda cwd: (cwd / "Makefile").write_text("weakened checks\n")
        self.assert_candidate_not_published(self.engine.build, ValueError, "Makefile")
        self.assertFalse((self.engine.work / "checks/build-1.log").exists())
        self.assertEqual((self.engine.cwd / "Makefile").read_text(), "test:\n\t@true\n")

    def test_unplanned_build_edit_fails(self):
        self.through_plan()
        self.adapter.mutations["build"] = lambda cwd: (cwd / "src/extra.py").write_text("VALUE = 1\n")
        self.assert_candidate_not_published(self.engine.build, ValueError, "absent from plan")
        self.assertFalse((self.engine.cwd / "src/extra.py").exists())

    def test_fix_cannot_modify_tests(self):
        self.through_review(finding())
        self.adapter.mutations["fix"] = lambda cwd: (cwd / "tests/test_value.py").write_text("# Weakened\n")
        self.assert_candidate_not_published(self.engine.fix, ValueError, "tests/test_value.py")
        self.assertEqual(self.engine.state["fix_rounds"], 0)
        self.assertEqual((self.engine.cwd / "tests/test_value.py").read_text(), "# Preserved test assertion\n")

    def test_stale_fix_requires_a_review_without_calling_harness(self):
        self.through_review(finding())
        self.operator_commit()
        calls = list(self.adapter.calls)
        with self.assertRaisesRegex(RuntimeError, "not the reviewed commit"):
            self.engine.fix()
        self.assertEqual(self.adapter.calls, calls)

    def test_finalize_refuses_open_findings_and_stale_head(self):
        self.through_review(finding())
        with self.assertRaisesRegex(RuntimeError, "open Important"):
            self.engine.finalize()
        self.adapter.reviews = [review(updates=[update("F1")])]
        self.engine.review()
        self.operator_commit()
        with self.assertRaisesRegex(RuntimeError, "not the last reviewed commit"):
            self.engine.finalize()
        self.assertEqual(self.github.ready_calls, [])

    def test_review_fix_review_uses_evidence_to_resolve_and_finishes(self):
        self.adapter.reviews = [review(finding()), review(updates=[update("F1")])]
        self.engine.run()
        self.assertEqual(self.adapter.calls, ["spec", "plan", "build", "review", "fix", "review"])
        self.assertEqual(self.engine.state["fix_rounds"], 1)
        self.assertEqual(self.engine.state["reviews"][-1]["important_resolved"], 1)
        self.assertEqual(self.engine.ledger["findings"][0]["status"], "resolved")
        self.assertEqual(self.engine.state["outcome"], "done")
        self.assertEqual(self.git("log", "--format=%s", "--reverse", "main..factory/42").splitlines(),
                         ["factory(42): build", "factory(42): fix 1 (claims await review)"])
        self.assertTrue((self.engine.work / "fix-1.json").is_file())
        self.assertTrue((self.engine.work / "checks/fix-1.log").is_file())

    def test_fix_claim_does_not_change_ledger_status(self):
        self.through_review(finding())
        self.engine.fix()
        self.assertEqual(self.engine.ledger["findings"][0]["status"], "open")
        self.assertEqual(load_json(self.engine.work / "fix-1.json")["addressed"][0]["id"], "F1")

    def test_no_progress_parks_after_one_fix_and_stays_parked(self):
        self.adapter.reviews = [review(finding()), review(updates=[update("F1", "unresolved")])]
        with self.assertRaisesRegex(NeedsHuman, "no_progress"):
            self.engine.run()
        calls = list(self.adapter.calls)
        with self.assertRaisesRegex(NeedsHuman, "no_progress"):
            self.engine.run()
        self.assertEqual(self.adapter.calls, calls)
        self.assertEqual(self.adapter.calls.count("fix"), 1)
        self.assertEqual(self.engine.state["outcome"], "needs_human:no_progress")

    def test_round_cap_parks_even_when_one_finding_was_resolved(self):
        self.config["factory"]["max_fix_rounds"] = 1
        self.adapter.reviews = [review(finding(), finding("Second defect")),
                                review(updates=[update("F1"), update("F2", "unresolved")])]
        with self.assertRaisesRegex(NeedsHuman, "rounds_exhausted"):
            self.engine.run()
        self.assertEqual(self.engine.state["fix_rounds"], 1)
        self.assertEqual(self.engine.state["reviews"][-1]["important_resolved"], 1)
        self.assertEqual([f["id"] for f in open_important(self.engine.ledger)], ["F2"])

    def test_zero_fix_cap_does_not_invoke_fixer(self):
        self.config["factory"]["max_fix_rounds"] = 0
        self.adapter.reviews = [review(finding())]
        with self.assertRaisesRegex(NeedsHuman, "rounds_exhausted"):
            self.engine.run()
        self.assertNotIn("fix", self.adapter.calls)

    def test_dismissed_finding_reraise_is_dropped_and_run_finishes(self):
        self.through_review(finding())
        self.engine.dismiss("F1", "Accepted bounded prototype constraint.")
        self.adapter.reviews = [review(finding("  MISSING input guard! "))]
        self.engine.review()
        self.engine.run()
        self.assertNotIn("fix", self.adapter.calls)
        self.assertEqual(self.engine.state["reviews"][-1]["reraised_dropped"], 1)
        self.assertEqual(self.engine.ledger["findings"][0]["status"], "dismissed")
        self.assertEqual(self.engine.state["outcome"], "done")

    def test_read_mode_edit_is_rejected(self):
        self.engine.spec()
        self.adapter.mutations["plan"] = lambda cwd: (cwd / "src/value.py").write_text("VALUE = 99\n")
        self.assert_candidate_not_published(self.engine.plan, RuntimeError, "read-mode harness modified")

    def test_agent_commit_is_reset_and_rejected(self):
        self.through_plan()
        def commit_from_agent(cwd):
            (cwd / "src/value.py").write_text("VALUE = 99\n")
            self.git("add", ".", cwd=cwd)
            self.git("commit", "-qm", "unauthorized agent commit", cwd=cwd)
        self.adapter.mutations["build"] = commit_from_agent
        self.assert_candidate_not_published(self.engine.build, RuntimeError, "harness changed Git HEAD")

    def test_agent_commit_followed_by_harness_failure_is_reset(self):
        self.through_plan()
        local_before, remote_before = self.engine.head(), self.remote_head()
        def commit_then_fail(cwd):
            (cwd / "src/value.py").write_text("VALUE = 99\n")
            self.git("add", ".", cwd=cwd)
            self.git("commit", "-qm", "unauthorized failed agent commit", cwd=cwd)
            raise RuntimeError("invalid harness response")
        self.adapter.mutations["build"] = commit_then_fail
        with self.assertRaisesRegex(RuntimeError, "harness changed Git HEAD"):
            execute_issue(self.repo, self.github, self.config, 42, "build")
        self.assertEqual(self.engine.head(), local_before)
        self.assertEqual(self.remote_head(), remote_before)
        self.assertEqual((self.engine.cwd / "src/value.py").read_text(), "VALUE = 0\n")
        self.assertEqual(self.repo.changed_paths(self.engine.cwd), [])
        self.assertIn("plan", load_json(self.engine.work / "state.json")["stages"])

    def test_baseline_check_cannot_commit_changes_even_when_it_passes(self):
        self.through_plan()
        self.config["factory"]["checks"] = [[sys.executable, "-c",
            "from pathlib import Path; import subprocess; "
            "Path('src/value.py').write_text('VALUE = 999\\n'); "
            "subprocess.run(['git', 'add', 'src/value.py'], check=True); "
            "subprocess.run(['git', 'commit', '-qm', 'unauthorized check commit'], check=True)"]]
        self.assert_candidate_not_published(self.engine.build, RuntimeError, "checks changed Git HEAD")
        self.assertEqual((self.engine.cwd / "src/value.py").read_text(), "VALUE = 0\n")
        self.assertNotIn("build", self.adapter.calls)
        self.assertTrue((self.engine.work / "checks/baseline-1.log").is_file())

    def test_force_build_preserves_operator_plan_edits_and_rewrites_safely(self):
        self.through_build()
        self.operator_edit_plan("\nOperator clarification: keep the public interface.\n")
        self.engine = Engine(self.repo, self.github, self.config, 42).prepare()
        self.engine.rewind("build")
        self.engine.build()
        self.assertIn("Operator clarification", (self.engine.work / "plan.md").read_text())
        self.assertIn("Operator clarification", self.github.pr_body)
        self.assertEqual(self.adapter.calls.count("build"), 2)
        self.assertEqual(self.engine.head(), self.remote_head())
        self.assertEqual(self.repo.changed_paths(self.engine.cwd), [])

    def test_failed_force_build_restores_original_head_operator_plan_and_ledger(self):
        self.through_review(finding())
        self.engine.dismiss("F1", "Accepted prototype limitation.")
        self.operator_edit_plan("\nOperator clarification must survive a failed build.\n")
        before, remote_before = self.engine.head(), self.remote_head()
        original_plan = (self.engine.work / "plan.md").read_text()
        original_ledger = load_json(self.engine.work / "findings.json")
        self.adapter.mutations["build"] = lambda cwd: (cwd / "src/value.py").write_text("VALUE = -1\n")
        with self.assertRaisesRegex(RuntimeError, "build checks failed"):
            execute_issue(self.repo, self.github, self.config, 42, "build", force=True)
        self.assertEqual(self.engine.head(), before)
        self.assertEqual(self.remote_head(), remote_before)
        self.assertEqual((self.engine.work / "plan.md").read_text(), original_plan)
        self.assertEqual(load_json(self.engine.work / "findings.json"), original_ledger)
        self.assertEqual(self.github.draft_calls, [])
        self.assertEqual(self.repo.changed_paths(self.engine.cwd), [])

    def test_github_failure_after_commit_keeps_local_stage_for_retry(self):
        self.through_plan()
        with patch.object(self.repo, "push", side_effect=RuntimeError("temporary GitHub failure")):
            with self.assertRaisesRegex(RuntimeError, "temporary GitHub failure"):
                self.engine.build()
        self.assertIn("build", load_json(self.engine.work / "state.json")["stages"])
        self.assertNotEqual(self.engine.head(), self.remote_head())
        calls = list(self.adapter.calls)
        resumed = Engine(self.repo, self.github, self.config, 42).prepare()
        resumed.build()
        self.assertEqual(self.adapter.calls, calls)
        self.assertEqual(resumed.head(), self.remote_head())

    def test_interrupted_marker_discards_partial_stage_and_resumes_without_duplicate_pr(self):
        self.engine.spec()
        (self.engine.cwd / "src/value.py").write_text("VALUE = -1\n# interrupted candidate\n")
        (self.engine.work / "plan.md").write_text("partial invalid plan\n")
        transcript = self.repo.local_dir / "transcripts/interrupted.log"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("partial harness output\n")
        stopped = subprocess.Popen([sys.executable, "-c", "pass"])
        stopped.wait()
        marker = self.repo.local_dir / "run/42.json"
        atomic_json(marker, {"stage": "plan", "pid": stopped.pid, "worktree": str(self.engine.cwd)})
        self.assertEqual(execute_issue(self.repo, self.github, self.config, 42), 0)
        self.assertFalse(marker.exists())
        self.assertTrue(transcript.exists())
        self.assertEqual(self.github.pr_creates, 1)
        self.assertEqual(self.github.issue_reads, 1)
        self.assertEqual(self.adapter.calls, ["spec", "plan", "build", "review"])
        self.assertEqual((self.engine.cwd / "src/value.py").read_text(), "VALUE = 1\n")
        self.assertEqual(self.repo.changed_paths(self.engine.cwd), [])
        self.assertEqual(execute_issue(self.repo, self.github, self.config, 42), 0)
        self.assertEqual(self.github.pr_creates, 1)
        self.assertEqual(self.adapter.calls, ["spec", "plan", "build", "review"])

    def test_interrupted_force_restores_last_safe_head_and_operator_edits(self):
        self.through_build()
        self.operator_edit_plan("\nOperator clarification survives process death.\n")
        self.engine = Engine(self.repo, self.github, self.config, 42).prepare()
        safe = self.engine.head()
        stopped = subprocess.Popen([sys.executable, "-c", "pass"])
        stopped.wait()
        marker = self.repo.local_dir / "run/42.json"
        atomic_json(marker, {"stage": "build", "pid": stopped.pid, "last_safe_head": safe,
                             "state": self.engine.state, "ledger": self.engine.ledger})
        self.engine.rewind("build")
        self.engine.save()
        (self.engine.cwd / "src/value.py").write_text("VALUE = -1\n# interrupted forced candidate\n")
        self.assertNotEqual(self.engine.head(), safe)
        self.assertEqual(execute_issue(self.repo, self.github, self.config, 42), 0)
        self.assertTrue(self.repo._ancestor(safe, self.engine.head(), self.engine.cwd))
        self.assertIn("Operator clarification survives", (self.engine.work / "plan.md").read_text())
        self.assertEqual((self.engine.cwd / "src/value.py").read_text(), "VALUE = 1\n")
        self.assertEqual(self.adapter.calls.count("build"), 1)
        self.assertFalse(marker.exists())

    def test_interrupted_unauthorized_commit_is_reset_to_last_safe_head(self):
        self.through_plan()
        safe = self.engine.head()
        stopped = subprocess.Popen([sys.executable, "-c", "pass"])
        stopped.wait()
        marker = self.repo.local_dir / "run/42.json"
        atomic_json(marker, {"stage": "build", "pid": stopped.pid, "last_safe_head": safe})
        (self.engine.cwd / "src/value.py").write_text("VALUE = 999\n")
        unauthorized = self.repo.commit(self.engine.cwd, "unauthorized agent commit before process death")
        self.assertNotEqual(unauthorized, safe)
        self.assertEqual(execute_issue(self.repo, self.github, self.config, 42), 0)
        self.assertFalse(self.repo._ancestor(unauthorized, self.engine.head(), self.engine.cwd))
        self.assertTrue(self.repo._ancestor(safe, self.engine.head(), self.engine.cwd))
        self.assertEqual((self.engine.cwd / "src/value.py").read_text(), "VALUE = 1\n")
        self.assertEqual(self.github.pr_creates, 1)
        self.assertFalse(marker.exists())

    def test_read_only_stages_never_commit(self):
        base = self.engine.head()
        with patch.object(self.repo, "commit", wraps=self.repo.commit) as commit:
            self.through_plan()
            commit.assert_not_called()
        self.assertEqual(self.engine.head(), base)
        self.assertEqual(self.github.pr_creates, 0)
        self.engine.build()
        head = self.engine.head()
        with patch.object(self.repo, "commit", wraps=self.repo.commit) as commit:
            self.engine.review()
            self.engine.finalize()
            commit.assert_not_called()
        self.assertEqual(self.engine.head(), head)

    def test_checks_cannot_rewrite_local_artifacts(self):
        self.through_plan()
        spec = self.engine.work / "spec.md"
        original = spec.read_bytes()
        self.config["factory"]["checks"] = [[sys.executable, "-c",
            f"from pathlib import Path; Path({str(spec)!r}).write_text('Altered requirements')"]]
        self.assert_candidate_not_published(self.engine.build, RuntimeError, "checks modified factory files")
        self.assertEqual(spec.read_bytes(), original)
        self.assertTrue((self.engine.work / "checks/baseline-1.log").is_file())
        self.assertNotIn("build", self.adapter.calls)

    def test_noop_fix_still_requires_another_review(self):
        self.adapter.mutations["fix"] = lambda cwd: None
        self.adapter.reviews = [review(finding()), review(updates=[update("F1", "unresolved")])]
        with self.assertRaisesRegex(NeedsHuman, "no_progress"):
            self.engine.run()
        self.assertEqual(self.adapter.calls, ["spec", "plan", "build", "review", "fix", "review"])
        self.assertEqual(self.git("log", "--format=%s", "main..factory/42"), "factory(42): build")
        for number in (1, 2):
            self.assertIn(f"factory:review:{self.engine.head()}:{number}", self.github.comments)

    def test_build_and_fix_cannot_change_ignored_factory_files(self):
        self.through_review(finding())
        original = (self.engine.work / "state.json").read_bytes()
        for stage in ("build", "fix"):
            for local in (False, True):
                with self.subTest(stage=stage, local=local):
                    target = (self.engine.work / "state.json" if local
                              else self.engine.cwd / ".factory/forbidden.txt")
                    def mutate(cwd):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text("corrupted factory data\n")
                    self.adapter.mutations[stage] = mutate
                    with self.assertRaisesRegex(ValueError, "forbidden paths under .factory/"):
                        self.engine.call(stage)
                    self.assertEqual((self.engine.work / "state.json").read_bytes(), original)
                    if not local:
                        self.assertFalse(target.exists())
                    self.assertEqual(self.repo.changed_paths(self.engine.cwd), [])

    def test_read_mode_cannot_delete_saved_artifacts(self):
        self.through_plan()
        spec = self.engine.work / "spec.md"
        original = spec.read_bytes()
        self.adapter.mutations["plan"] = lambda cwd: spec.unlink()
        with self.assertRaisesRegex(ValueError, "forbidden paths under .factory/"):
            self.engine.call("plan")
        self.assertEqual(spec.read_bytes(), original)

    def test_force_spec_plan_and_build_rewind_and_retain_document_inputs(self):
        self.through_review()
        for stage in ("build", "plan", "spec"):
            with self.subTest(stage=stage):
                self.engine = Engine(self.repo, self.github, self.config, 42).prepare()
                spec, plan = self.engine.work / "spec.md", self.engine.work / "plan.md"
                spec.write_text(spec.read_text() + "\nOperator spec clarification.\n")
                plan.write_text(plan.read_text() + "\nOperator plan clarification.\n")
                before_spec, before_plan = spec.read_bytes(), plan.read_bytes()
                previous = {"spec": None, "plan": "spec", "build": "plan"}[stage]
                target = (self.engine.state["stages"][previous]["commit"] if previous
                          else self.engine.state["base"]["sha"])
                self.engine.rewind(stage)
                self.assertEqual(self.engine.head(), target)
                self.assertNotIn(stage, self.engine.state["stages"])
                self.assertEqual(self.engine.state["reviews"], [])
                self.assertEqual(self.engine.state["fix_rounds"], 0)
                self.assertEqual(spec.read_bytes(), before_spec)
                self.assertEqual(plan.read_bytes(), before_plan)
                getattr(self.engine, stage)()
                prompt = (self.engine.work / "prompts" / f"{stage}-1.md").read_text()
                if stage != "spec":
                    self.assertIn("Operator spec clarification.", prompt)
                self.assertEqual(self.engine.head(), self.remote_head())
                self.engine = Engine(self.repo, self.github, self.config, 42).prepare()
        self.engine.run()
        self.assertEqual(self.engine.state["outcome"], "done")

    def test_status_reads_local_state_even_without_worktree_or_branch(self):
        self.engine.spec()
        saved = load_json(self.engine.work / "state.json")
        for remove in (None, "worktree", "branch"):
            with self.subTest(remove=remove):
                if remove == "worktree":
                    self.repo.git("worktree", "remove", str(self.engine.cwd))
                elif remove == "branch":
                    self.repo.git("branch", "-D", "factory/42")
                output = io.StringIO()
                with patch.object(self.repo, "fetch", side_effect=AssertionError("status must not fetch")), \
                        contextlib.redirect_stdout(output):
                    self.assertEqual(status(self.repo, 42), 0)
                report = json.loads(output.getvalue())
                self.assertEqual(report["state"], saved)
                self.assertEqual(report["worktree"], str(self.engine.cwd) if remove is None else None)
                self.assertEqual(report["head"] is None, remove == "branch")

    def test_dismiss_uses_saved_ledger_without_committing(self):
        self.through_review(finding())
        head = self.engine.head()
        self.assertEqual(execute_issue(self.repo, self.github, self.config, 42, "dismiss",
                                       finding="F1", reason="Accepted limitation."), 0)
        resumed = Engine(self.repo, self.github, self.config, 42).prepare()
        self.assertEqual(resumed.ledger["findings"][0]["status"], "dismissed")
        self.assertEqual(resumed.head(), head)

    def test_abandon_removes_issue_artifacts_branch_worktree_and_pr(self):
        self.through_build()
        other = self.repo.issue_dir(43)
        other.mkdir(parents=True)
        (other / "spec.md").write_text("Another issue\n")
        with patch.object(self.github, "remove_label", create=True) as label, \
                patch.object(self.github, "close", create=True) as close:
            self.assertEqual(execute_issue(self.repo, self.github, self.config, 42, "abandon"), 0)
        label.assert_called_once_with(42, "factory")
        close.assert_called_once_with(142)
        self.assertFalse(self.engine.work.exists())
        self.assertFalse(self.engine.cwd.exists())
        self.assertFalse(self.repo.branch_exists(42))
        self.assertFalse(self.repo.branch_exists(42, remote=True))
        self.assertTrue((other / "spec.md").exists())
        self.assertTrue((self.repo.local_dir / "transcripts/fake-transcript.json").exists())

    def test_cli_rolls_back_a_failed_protected_edit_and_removes_marker(self):
        self.through_plan()
        local_before, remote_before = self.engine.head(), self.remote_head()
        self.adapter.mutations["build"] = lambda cwd: (cwd / "Makefile").write_text("weakened checks\n")
        with self.assertRaisesRegex(ValueError, "Makefile"):
            execute_issue(self.repo, self.github, self.config, 42, "build")
        self.assertEqual(self.engine.head(), local_before)
        self.assertEqual(self.remote_head(), remote_before)
        self.assertEqual(self.repo.changed_paths(self.engine.cwd), [])
        self.assertFalse((self.repo.local_dir / "run/42.json").exists())
        self.assertEqual((self.engine.cwd / "Makefile").read_text(), "test:\n\t@true\n")


if __name__ == "__main__":
    unittest.main()
