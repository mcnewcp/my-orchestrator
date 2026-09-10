"""Exercise real local Git history and deterministic GitHub command responses."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.gh import GitHub
from factory.repo import Repo


def git(cwd, *args):
    return subprocess.run(
        ["git", *map(str, args)], cwd=cwd, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


class RepoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        git(self.root, "init", "--bare", "--initial-branch=main", self.remote)
        self.checkout = self.root / "checkout"
        git(self.root, "init", "--initial-branch=main", self.checkout)
        self.identity(self.checkout)
        (self.checkout / ".gitignore").write_text(".factory/\n")
        (self.checkout / "source.txt").write_text("baseline\n")
        git(self.checkout, "add", ".")
        git(self.checkout, "commit", "-m", "baseline")
        git(self.checkout, "remote", "add", "origin", self.remote)
        git(self.checkout, "push", "-u", "origin", "main")
        self.repo = Repo(self.checkout)
        self.repo.fetch()

    @staticmethod
    def identity(path):
        git(path, "config", "user.email", "factory-test@example.invalid")
        git(path, "config", "user.name", "Factory Test")

    def clone(self, name="other"):
        path = self.root / name
        git(self.root, "clone", self.remote, path)
        self.identity(path)
        return path

    def test_worktree_creation_and_missing_read_only_lookup(self):
        self.assertIsNone(self.repo.worktree(7, create=False))
        worktree = self.repo.worktree(7)
        self.assertEqual(self.repo.git("branch", "--show-current", cwd=worktree), "factory/7")
        self.assertEqual(worktree, self.repo.worktree(7))
        self.assertEqual(worktree, self.repo.worktree(7, create=False))
        self.assertEqual(self.repo.head(worktree), self.repo.head(self.checkout))

    def test_rebuild_recovers_code_and_keeps_artifacts_on_the_same_machine(self):
        worktree = self.repo.worktree(7)
        artifact = self.repo.issue_dir(7) / "state.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text('{"stage": "spec"}\n')
        (worktree / "source.txt").write_text("implementation\n")
        expected = self.repo.commit(worktree, "build")
        self.repo.push(7)
        shutil.rmtree(worktree)
        recovered = self.repo.worktree(7)
        self.assertEqual(self.repo.head(recovered), expected)
        self.assertTrue(artifact.is_file())
        rebuilt = Repo(self.clone())
        rebuilt.fetch()
        recovered = rebuilt.worktree(7)
        self.assertEqual(rebuilt.head(recovered), expected)
        self.assertFalse(rebuilt.issue_dir(7).exists())

    def test_commit_is_idempotent_and_resolves_plain_sha(self):
        worktree = self.repo.worktree(7)
        (worktree / "source.txt").write_text("new\n")
        sha = self.repo.commit(worktree, "build")
        self.assertEqual(self.repo.resolve(worktree, sha), sha)
        self.assertEqual(self.repo.commit(worktree, "already committed"), sha)
        for invalid in ("HEAD", "abcd", None):
            with self.subTest(ref=invalid), self.assertRaisesRegex(RuntimeError, "Invalid recorded commit"):
                self.repo.resolve(worktree, invalid)

    def test_resolves_older_sha_and_rejects_unreachable_commit(self):
        worktree = self.repo.worktree(7)
        (worktree / "source.txt").write_text("first\n")
        first = self.repo.commit(worktree, "first")
        (worktree / "source.txt").write_text("second\n")
        self.repo.commit(worktree, "second")
        self.assertEqual(self.repo.resolve(worktree, first), first)
        (self.checkout / "other.txt").write_text("other history\n")
        unreachable = self.repo.commit(self.checkout, "on main")
        with self.assertRaisesRegex(RuntimeError, "not reachable"):
            self.repo.resolve(worktree, unreachable)

    def test_operator_artifacts_stay_local_and_code_dirt_is_refused(self):
        worktree = self.repo.worktree(7)
        before = self.repo.head(worktree)
        artifact = self.repo.issue_dir(7) / "spec.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("operator edit\n")
        self.repo.validate_clean(worktree, 7)
        self.assertEqual(self.repo.head(worktree), before)
        self.assertEqual(self.repo.changed_paths(worktree), [])
        (worktree / "source.txt").write_text("uncommitted code\n")
        with self.assertRaisesRegex(RuntimeError, "source.txt"):
            self.repo.validate_clean(worktree, 7)

    def test_renames_check_both_paths_and_support_unusual_names(self):
        worktree = self.repo.worktree(7)
        (worktree / "work/7").mkdir(parents=True)
        self.repo.git("mv", "source.txt", "work/7/source.txt", cwd=worktree)
        self.assertEqual(self.repo.changed_paths(worktree), ["source.txt", "work/7/source.txt"])
        with self.assertRaisesRegex(RuntimeError, "source.txt"):
            self.repo.validate_clean(worktree, 7)
        self.repo.reset(worktree)
        odd = 'a "quoted"\nfile.txt'
        (worktree / odd).write_text("untracked\n")
        self.assertEqual(self.repo.changed_paths(worktree), [odd])

    def test_reset_discards_changes_and_preserves_transients(self):
        worktree = self.repo.worktree(7)
        (worktree / "source.txt").write_text("changed\n")
        (worktree / "new.txt").write_text("new\n")
        (worktree / ".factory").mkdir()
        transcript = worktree / ".factory/transcript.txt"
        transcript.write_text("keep\n")
        self.repo.reset(worktree)
        self.assertEqual((worktree / "source.txt").read_text(), "baseline\n")
        self.assertFalse((worktree / "new.txt").exists())
        self.assertTrue(transcript.exists())

    def test_commit_accepts_staged_renames_and_deletions(self):
        worktree = self.repo.worktree(7)
        self.repo.git("mv", "source.txt", "renamed.txt", cwd=worktree)
        renamed = self.repo.commit(worktree, "rename source")
        self.assertEqual(self.repo.changed_paths(worktree), [])
        self.repo.git("rm", "renamed.txt", cwd=worktree)
        deleted = self.repo.commit(worktree, "delete source")
        self.assertNotEqual(renamed, deleted)
        self.assertEqual(self.repo.changed_paths(worktree), [])

    def test_review_diff_includes_only_code_changes(self):
        worktree = self.repo.worktree(7)
        base = self.repo.head(worktree)
        (worktree / "source.txt").write_text("implementation\n")
        self.repo.issue_dir(7).mkdir(parents=True)
        (self.repo.issue_dir(7) / "plan.md").write_text("PLAN SECRET\n")
        self.repo.commit(worktree, "build")
        diff = self.repo.diff(worktree, base)
        self.assertIn("implementation", diff)
        self.assertNotIn("PLAN SECRET", diff)
        self.assertNotIn("work/7", diff)

    def test_sync_fast_forwards_and_refuses_divergence(self):
        worktree = self.repo.worktree(7)
        self.repo.push(7)
        other = self.clone()
        git(other, "checkout", "factory/7")
        (other / "remote.txt").write_text("remote\n")
        expected = Repo(other).commit(other, "remote edit")
        git(other, "push", "origin", "factory/7")
        self.repo.fetch()
        self.repo.sync(worktree, 7)
        self.assertEqual(self.repo.head(worktree), expected)
        (worktree / "local.txt").write_text("local\n")
        self.repo.commit(worktree, "local edit")
        (other / "remote.txt").write_text("remote again\n")
        Repo(other).commit(other, "more remote edits")
        git(other, "push", "origin", "factory/7")
        self.repo.fetch()
        with self.assertRaisesRegex(RuntimeError, "diverged"):
            self.repo.sync(worktree, 7)

    def test_force_push_lease_survives_background_fetch(self):
        worktree = self.repo.worktree(7)
        self.repo.push(7)
        self.repo.fetch()
        other = self.clone()
        git(other, "checkout", "factory/7")
        (other / "remote.txt").write_text("operator\n")
        expected = Repo(other).commit(other, "operator edit")
        git(other, "push", "origin", "factory/7")
        # An editor may update remote tracking refs after our approved rewind base.
        self.repo.git("fetch", "origin")
        (worktree / "source.txt").write_text("rewritten\n")
        self.repo.commit(worktree, "forced stage")
        with self.assertRaisesRegex(RuntimeError, "stale info"):
            self.repo.push(7, force=True)
        self.assertEqual(git(other, "ls-remote", "origin", "refs/heads/factory/7").split()[0], expected)

    def test_force_push_succeeds_with_matching_explicit_lease(self):
        worktree = self.repo.worktree(7)
        self.repo.push(7)
        self.repo.fetch()
        (worktree / "source.txt").write_text("rewrite\n")
        expected = self.repo.commit(worktree, "rewrite")
        self.repo.push(7, force=True)
        self.assertEqual(git(worktree, "ls-remote", "origin", "refs/heads/factory/7").split()[0], expected)
        with self.assertRaisesRegex(RuntimeError, "fetch"):
            Repo(self.checkout).push(7, force=True)

    def test_abandon_is_repeatable_and_does_not_delete_main(self):
        worktree = self.repo.worktree(7)
        main = self.repo.head(self.checkout)
        self.repo.push(7)
        self.repo.issue_dir(7).mkdir(parents=True)
        (self.repo.issue_dir(7) / "state.json").write_text("{}\n")
        self.repo.abandon(7)
        self.assertFalse(self.repo.issue_dir(7).exists())
        self.assertFalse(worktree.exists())
        self.assertFalse(self.repo.branch_exists(7))
        self.assertFalse(self.repo.branch_exists(7, remote=True))
        self.repo.abandon(7)
        self.assertEqual(self.repo.head(self.checkout), main)

    def test_invalid_issue_cannot_address_another_ref(self):
        for invalid in [0, -1, True, "main", "7:main", "--delete"]:
            with self.subTest(issue=invalid), self.assertRaises(RuntimeError):
                self.repo.push(invalid)


class RecordingGitHub(GitHub):
    def __init__(self, responses):
        super().__init__(Path("."))
        self.responses = list(responses)
        self.calls = []
        self.bodies = []

    def _run(self, *args):
        self.calls.append(args)
        if "--body-file" in args:
            self.bodies.append(Path(args[args.index("--body-file") + 1]).read_text())
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response)


class GitHubTests(unittest.TestCase):
    def test_existing_pr_is_returned_without_writes(self):
        api = RecordingGitHub([[{"number": 23, "url": "https://example/23", "state": "OPEN", "baseRefName": "main"}]])
        self.assertEqual(api.ensure_pr(7, "factory/7", "main", "title", "body"), {"number": 23, "url": "https://example/23"})
        self.assertEqual(len(api.calls), 1)
        self.assertIn("all", api.calls[0])

    def test_closed_pr_never_creates_replacement(self):
        for state in ["CLOSED", "MERGED"]:
            api = RecordingGitHub([[{"number": 23, "url": "https://example/23", "state": state}]])
            with self.subTest(state=state), self.assertRaisesRegex(RuntimeError, "abandon"):
                api.ensure_pr(7, "factory/7", "main", "title", "body")
            self.assertEqual(len(api.calls), 1)

    def test_new_pr_uses_draft_and_exact_body_file(self):
        api = RecordingGitHub([[], "https://example/23", {"number": 23, "url": "https://example/23"}])
        body = "Closes #7\n\nLiteral `code` and $(not a shell)\n"
        api.ensure_pr(7, "factory/7", "main", "title", body)
        self.assertEqual(api.bodies, [body])
        self.assertIn("--draft", api.calls[1])
        self.assertNotIn("--body", api.calls[1])

    def test_comment_deduplicates_only_own_exact_marker(self):
        for author, expected_calls in [("operator", 2), ("somebody-else", 3)]:
            api = RecordingGitHub([
                {"login": "operator"},
                [[{"user": {"login": author}, "body": "comment\n\n<!-- factory:gate:abc -->\n"}]],
                "https://example/comment",
            ])
            api.comment(23, "gate", "factory:gate:abc")
            self.assertEqual(len(api.calls), expected_calls)
        self.assertEqual(api.bodies, ["gate\n\n<!-- factory:gate:abc -->\n"])

    def test_comment_deduplicates_across_pages_but_not_marker_prefixes(self):
        api = RecordingGitHub([
            {"login": "operator"},
            [[], [{"user": {"login": "operator"}, "body": "<!-- factory:gate:abc -->"}]],
        ])
        api.comment(23, "gate", "factory:gate:abc")
        self.assertEqual(len(api.calls), 2)
        api = RecordingGitHub([
            {"login": "operator"},
            [[{"user": {"login": "operator"}, "body": "prefix <!-- factory:gate:abc --> suffix"}]],
            "created",
        ])
        api.comment(23, "gate", "factory:gate:abc")
        self.assertEqual(len(api.calls), 3)

    def test_ready_is_repeatable_and_refuses_closed_pr(self):
        api = RecordingGitHub([{"state": "OPEN", "isDraft": False}])
        api.ready(23)
        self.assertEqual(len(api.calls), 1)
        api = RecordingGitHub([{"state": "OPEN", "isDraft": True}, "ready"])
        api.ready(23)
        self.assertEqual(api.calls[1], ("pr", "ready", "23"))
        api = RecordingGitHub([{"state": "CLOSED", "isDraft": True}])
        with self.assertRaisesRegex(RuntimeError, "Cannot finalize"):
            api.ready(23)

    def test_close_is_repeatable(self):
        api = RecordingGitHub([{"state": "CLOSED"}])
        api.close(23)
        self.assertEqual(len(api.calls), 1)

    def test_draft_is_repeatable_and_refuses_closed_or_merged_pr(self):
        api = RecordingGitHub([{"state": "OPEN", "isDraft": True}])
        api.draft(23)
        self.assertEqual(len(api.calls), 1)
        api = RecordingGitHub([{"state": "OPEN", "isDraft": False}, "draft"])
        api.draft(23)
        self.assertEqual(api.calls[1], ("pr", "ready", "23", "--undo"))
        for state in ["CLOSED", "MERGED"]:
            api = RecordingGitHub([{"state": state, "isDraft": False}])
            with self.subTest(state=state), self.assertRaisesRegex(RuntimeError, "Cannot rewrite"):
                api.draft(23)

    def test_empty_successful_label_search_creates_label(self):
        api = GitHub(Path("."))
        with patch.object(api, "_run", side_effect=["", "created"]) as run:
            api.ensure_label("factory")
        self.assertEqual(run.call_args_list[1].args[:3], ("label", "create", "factory"))

    def test_labels_preserve_existing_label_and_skip_absent_removal(self):
        api = RecordingGitHub([[{"name": "factory"}], {"labels": []}])
        api.ensure_label("factory")
        api.remove_label(7, "factory")
        self.assertEqual(len(api.calls), 2)

    def test_issue_intake_is_sorted_and_only_reads_numbers(self):
        api = RecordingGitHub([[{"number": 10}, {"number": 2}]])
        self.assertEqual(api.issues("factory"), [{"number": 2}, {"number": 10}])
        self.assertEqual(api.calls[0][-2:], ("--json", "number"))


if __name__ == "__main__":
    unittest.main()
