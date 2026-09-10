"""Tests for deterministic gates; no provider or GitHub credentials are needed."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from factory.checks import changed_paths, filtered_env, run_checks, validate_edits
from factory.state import atomic_json, load_json, merge_ledger, open_important


def finding(**changes):
    result = {"severity": "important", "pass": "bugs", "file": "src/app.py",
              "line": 12, "title": "Reject empty input", "detail": "An empty input crashes.",
              "evidence": "src/app.py:12 indexes input[0] without checking length."}
    result.update(changes)
    return result


def review(new=None, updates=None):
    return {"summary": "Review complete.", "new": new or [], "updates": updates or []}


def update(finding_id="F1", status="resolved", evidence="A length check now precedes the index."):
    return {"id": finding_id, "status": status, "evidence": evidence}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger, _ = merge_ledger({"findings": []}, review([finding()]), 1)

    def test_new_ids_and_merge_leave_inputs_untouched(self):
        before = copy.deepcopy(self.ledger)
        output = review([finding(title="Separate failure", line=20)], [update()])
        original_output = copy.deepcopy(output)
        result, stats = merge_ledger(self.ledger, output, 2)
        self.assertEqual(self.ledger, before)
        self.assertEqual(output, original_output)
        self.assertEqual([item["id"] for item in result["findings"]], ["F1", "F2"])
        self.assertEqual(stats["important_resolved"], 1)
        self.assertEqual([item["id"] for item in open_important(result)], ["F2"])

    def test_every_open_finding_requires_exactly_one_evidenced_update(self):
        variants = [[], [update(), update()], [update("F99")], [update(evidence=" \n")],
                    [update(status="dismissed")]]
        for updates in variants:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                merge_ledger(self.ledger, review(updates=updates), 2)
        result, _ = merge_ledger(self.ledger, review(updates=[update(status="unresolved")]), 2)
        self.assertEqual(result["findings"][0]["status"], "open")
        self.assertEqual(result["findings"][0]["status_round"], 2)

    def test_nits_also_require_updates(self):
        ledger, _ = merge_ledger({}, review([finding(severity="nit")]), 1)
        with self.assertRaisesRegex(ValueError, "omitted updates"):
            merge_ledger(ledger, review(), 2)

    def test_normalized_duplicates_merge_and_keep_identity(self):
        duplicate = finding(title="  REJECT   empty input! ", file="./src/app.py", line=99)
        result, stats = merge_ledger(self.ledger,
                                     review([duplicate], [update(status="unresolved")]), 2)
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["id"], "F1")
        self.assertEqual(result["findings"][0]["line"], 99)
        self.assertEqual(result["findings"][0]["opened_round"], 1)
        self.assertEqual(stats["important_open"], 1)

    def test_dismissed_reraise_drops_and_resolved_reraise_reopens(self):
        for status in ("dismissed", "resolved"):
            ledger = copy.deepcopy(self.ledger)
            ledger["findings"][0].update(status=status, dismissed_reason="Accepted constraint")
            result, stats = merge_ledger(ledger, review([finding()]), 3)
            item = result["findings"][0]
            self.assertEqual(item["id"], "F1")
            self.assertEqual(stats["reraised_dropped"], int(status == "dismissed"))
            self.assertEqual(item["status"], "dismissed" if status == "dismissed" else "open")
            if status == "resolved":
                self.assertEqual(item["status_round"], 3)

    def test_same_review_resolve_and_reraise_is_not_progress(self):
        result, stats = merge_ledger(self.ledger, review([finding()], [update()]), 2)
        self.assertEqual(stats["important_resolved"], 0)
        self.assertEqual(len(open_important(result)), 1)

    def test_closed_findings_cannot_receive_updates(self):
        ledger, _ = merge_ledger(self.ledger, review(updates=[update()]), 2)
        with self.assertRaisesRegex(ValueError, "not for an open finding"):
            merge_ledger(ledger, review(updates=[update()]), 3)

    def test_new_finding_validation_and_nit_cap(self):
        for item in (finding(evidence=""), finding(severity="critical"), finding(line=True),
                     finding(**{"pass": "style"}), finding(title=" ")):
            with self.subTest(item=item), self.assertRaises(ValueError):
                merge_ledger({}, review([item]), 1)
        with self.assertRaisesRegex(ValueError, "cap is 1"):
            merge_ledger({}, review([finding(severity="nit"), finding(severity="nit")]), 1, nit_cap=1)

    def test_atomic_json_and_missing_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            default = {"stages": []}
            loaded = load_json(path, default)
            loaded["stages"].append("spec")
            self.assertEqual(default, {"stages": []})
            atomic_json(path, {"a": "é"})
            self.assertEqual(load_json(path), {"a": "é"})
            with patch("factory.state.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    atomic_json(path, {"a": "replacement"})
            self.assertEqual(load_json(path), {"a": "é"})
            self.assertEqual(list(path.parent.iterdir()), [path])
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                load_json(path, {})


class EditRulesTests(unittest.TestCase):
    def setUp(self):
        self.config = {"test_paths": ["tests/", "unit.spec.js"], "protected_paths": ["ci/"]}
        self.plan = "## Files that change\n- `src/app.py`\n- `tests/`\n## Proof\n`make test`\n"

    def validate(self, paths, stage="build", plan=None):
        validate_edits(paths, stage, 42, self.config, self.plan if plan is None else plan)

    def test_build_exact_files_and_directories(self):
        self.validate(["src/app.py", "tests/nested/test_app.py"])
        for path in ("src/app.py/child", "src/unplanned.py", "make test"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "absent from plan"):
                self.validate([path])

    def test_directory_permission_requires_explicit_slash(self):
        with self.assertRaisesRegex(ValueError, "src/app.py"):
            self.validate(["src/app.py"], plan="## Files that change\n- `src`\n## Proof\n")
        self.validate(["src/app.py"], plan="## Files that change\n- `src/`\n## Proof\n")

    def test_protected_paths_are_forbidden_even_if_planned(self):
        paths = ["Makefile", "factory.toml", "AGENTS.md", "CLAUDE.md", "REVIEW.md",
                 ".devcontainer/Dockerfile", ".claude/settings.json", ".mcp.json",
                 ".codex/config.toml", ".github/workflows/test.yml", "ci/check"]
        for stage in ("build", "fix"):
            for path in paths:
                with self.subTest(stage=stage, path=path), self.assertRaisesRegex(ValueError, "forbidden paths"):
                    self.validate([path], stage, "## Files that change\n`" + path + "`\n")

    def test_all_factory_artifacts_protected(self):
        for path in (".factory/issues/42/state.json", ".factory/issues/7/plan.md", ".factory/issues/42/prompts/build-1.md"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "forbidden paths"):
                self.validate([path])
        with self.assertRaisesRegex(ValueError, "forbidden paths"):
            self.validate([".factory/issues/42/plan.md"], "fix")

    def test_fix_test_paths_forbidden_and_unplanned_source_allowed(self):
        self.validate(["src/unplanned.py"], "fix")
        for path in ("tests/new.py", "tests", "unit.spec.js"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "forbidden paths"):
                self.validate([path], "fix")

    def test_invalid_and_missing_plan_paths(self):
        for path in ("../escape", "/tmp/absolute", "src/../../escape", "."):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate([path], "fix")
        with self.assertRaisesRegex(ValueError, "Files that change"):
            self.validate(["src/app.py"], plan="## Proof\n`src/app.py`\n")

    def test_git_status_includes_rename_source_staged_unstaged_and_untracked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, check=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            git("init", "-q")
            git("config", "user.name", "Factory tests")
            git("config", "user.email", "factory@example.invalid")
            for name in ("Makefile", "source.py", "staged.py"):
                (root / name).write_text("initial\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "fixture")
            git("mv", "Makefile", "renamed file")
            (root / "source.py").write_text("unstaged\n", encoding="utf-8")
            (root / "staged.py").write_text("staged\n", encoding="utf-8")
            git("add", "staged.py")
            (root / "new\nfile.py").write_text("new\n", encoding="utf-8")
            self.assertEqual(changed_paths(root),
                             ["Makefile", "new\nfile.py", "renamed file", "source.py", "staged.py"])
            with self.assertRaisesRegex(ValueError, "Makefile"):
                self.validate(changed_paths(root), "fix")


class EnvironmentAndChecksTests(unittest.TestCase):
    def setUp(self):
        self.source = {"PATH": os.environ.get("PATH", ""), "HOME": "/tmp/home",
                       "LANG": "C", "LC_ALL": "C", "TMPDIR": "/tmp",
                       "GH_TOKEN": "gh-secret", "GITHUB_TOKEN": "github-secret",
                       "ANTHROPIC_API_KEY": "anthropic-secret", "CODEX_API_KEY": "codex-secret",
                       "OPENAI_API_KEY": "openai-secret", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
                       "CLAUDE_CONFIG_DIR": "/tmp/claude", "CODEX_HOME": "/tmp/codex",
                       "AWS_SECRET_ACCESS_KEY": "aws-secret", "PYTHONPATH": "/tmp/untrusted"}

    def test_api_requires_selected_key_and_only_passes_it(self):
        for harness, key, config_key in (("claude", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR"),
                                          ("codex", "CODEX_API_KEY", "CODEX_HOME")):
            env = filtered_env(harness, "api", self.source)
            self.assertEqual(env[key], self.source[key])
            self.assertIn(config_key, env)
            self.assertEqual(set(env), {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", key, config_key})
            source = self.source.copy()
            del source[key]
            with self.assertRaisesRegex(ValueError, key):
                filtered_env(harness, "api", source)

    def test_subscription_and_checks_have_no_provider_keys(self):
        for harness in (None, "claude", "codex"):
            env = filtered_env(harness, "subscription", self.source)
            self.assertFalse(any("KEY" in key or "TOKEN" in key for key in env))
            self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(set(filtered_env(None, "api", self.source)),
                         {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"})

    def test_checks_strip_secrets_and_do_not_interpret_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "checks.log"
            script = ("import os, sys; "
                      "assert not any('KEY' in k or 'TOKEN' in k for k in os.environ); "
                      "print(sys.argv[1])")
            with patch.dict(os.environ, self.source):
                passed = run_checks(directory, [[sys.executable, "-c", script, "$(touch injected); literal"]], log, 5)
            self.assertTrue(passed)
            self.assertFalse((Path(directory) / "injected").exists())
            self.assertIn("$(touch injected); literal", log.read_text())

    def test_check_failure_stops_sequence_and_logs_output(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "checks.log"
            commands = [[sys.executable, "-c", "print('failure evidence'); raise SystemExit(7)"],
                        [sys.executable, "-c", "print('must not execute')"]]
            self.assertFalse(run_checks(directory, commands, log, 5))
            self.assertIn("failure evidence", log.read_text())
            self.assertIn("exit 7", log.read_text())
            self.assertNotIn("must not execute", log.read_text())

    def test_missing_executable_and_timeout_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "checks.log"
            self.assertFalse(run_checks(directory, [["/nonexistent-factory-command"]], log, 5))
            self.assertIn("FAILED", log.read_text())
            self.assertFalse(run_checks(directory, [[sys.executable, "-c", "import time; time.sleep(30)"]], log, .1))
            self.assertIn("TIMEOUT", log.read_text())

    def test_rejects_shell_string_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            for commands in ("make test", ["make test"], [[]]):
                with self.subTest(commands=commands), self.assertRaises(ValueError):
                    run_checks(directory, commands, Path(directory) / "log", 5)

    def test_keyboard_interrupt_kills_the_running_check(self):
        original_popen = subprocess.Popen
        processes = []
        def interrupt_first_wait(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            original_wait = process.wait
            first_wait = True
            def wait(*wait_args, **wait_kwargs):
                nonlocal first_wait
                if first_wait:
                    first_wait = False
                    raise KeyboardInterrupt
                return original_wait(*wait_args, **wait_kwargs)
            process.wait = wait
            return process
        with tempfile.TemporaryDirectory() as directory:
            with patch("factory.checks.subprocess.Popen", side_effect=interrupt_first_wait):
                with self.assertRaises(KeyboardInterrupt):
                    run_checks(directory, [[sys.executable, "-c", "import time; time.sleep(30)"]],
                               Path(directory) / "log", 5)
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll())


if __name__ == "__main__":
    unittest.main()
