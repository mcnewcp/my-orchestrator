"""Exercise real subprocess boundaries with local fake CLI executables."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from factory.harness import doctor, doctor_key, make_harness, validate_schema


FAKE_CLI = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
import time

fixture = Path(FIXTURE)
settings = json.loads((fixture / 'settings.json').read_text())
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (fixture / 'calls.jsonl').open('a') as log:
    log.write(json.dumps({'name': name, 'args': args, 'env': dict(os.environ)}) + '\n')
if '--version' in args:
    print(settings.get('versions', {}).get(name, {
        'claude': '2.1.263 (Claude Code)', 'codex': 'codex-cli 0.153.4',
        'git': 'git version 2.43.0', 'gh': 'gh version 2.40.0',
    }[name]))
    sys.exit(0)
if name in ('git', 'gh'):
    sys.exit(0)
if settings.get('child'):
    child = subprocess.Popen([sys.executable, '-c',
        'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(90)'])
    (fixture / 'child.pid').write_text(str(child.pid))
if settings.get('sleep'):
    print('partial stdout', flush=True)
    print('partial stderr', file=sys.stderr, flush=True)
    time.sleep(settings['sleep'])
if settings.get('stderr'):
    print(settings['stderr'], file=sys.stderr)
if settings.get('exit_code'):
    print('failed stdout', flush=True)
    sys.exit(settings['exit_code'])
output = settings.get('output', {'markdown': '# A spec', 'open_questions': []})
if settings.get('doctor'):
    nonce = Path('input.txt').read_text().rstrip('\n')
    prompt_arg = next(arg for arg in args if arg.startswith('Follow the instructions in '))
    prompt_path = prompt_arg[len('Follow the instructions in '):-len(' exactly.')]
    prompt = Path(prompt_path).read_text()
    output = {'message': nonce}
    if prompt.startswith('Create ') and not settings.get('skip_write'):
        Path('factory-doctor-probe.txt').write_text(nonce + '\n')
    if prompt.startswith('Read ') and settings.get('dirty_read'):
        Path('unexpected.txt').write_text('oops')
if name == 'claude':
    print(settings.get('raw', json.dumps({'structured_output': output,
                                        'is_error': settings.get('is_error', False)})))
else:
    print('{"type":"thread.started"}')
    if not settings.get('missing_last'):
        Path(args[args.index('-o') + 1]).write_text(settings.get('raw', json.dumps(output)))
'''


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for binary in ("claude", "codex", "git", "gh"):
            path = self.bin / binary
            path.write_text(FAKE_CLI.replace("FIXTURE", repr(str(self.root))))
            path.chmod(0o755)
        self.settings()
        self.source = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "ANTHROPIC_API_KEY": "fake-anthropic", "CODEX_API_KEY": "fake-codex",
            "GH_TOKEN": "fake-gh", "GITHUB_TOKEN": "fake-github", "SECRET": "fake-secret",
            "OPENAI_API_KEY": "fake-openai", "CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth",
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-config"),
            "CODEX_HOME": str(self.root / "codex-config"),
        }
        self.environment = patch.dict(os.environ, self.source, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.cwd = self.root / "repo"
        self.cwd.mkdir()
        (self.cwd / "AGENTS.md").write_text("Use the instructions.\n")
        self.prompt = self.cwd / "instructions with spaces.md"
        self.prompt.write_text("Return the requested JSON.\n")
        self.schema = Path(__file__).parents[1] / "src/factory/schemas/spec.json"
        self.transcripts = self.root / "transcripts"

    def settings(self, **values):
        (self.root / "settings.json").write_text(json.dumps(values))

    def calls(self):
        log = self.root / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def run_harness(self, name="claude", **overrides):
        values = dict(cwd=self.cwd, prompt_file=self.prompt, schema_file=self.schema,
                      mode="read", model="test-model", auth="subscription",
                      env=self.source, timeout_s=5)
        values.update(overrides)
        return make_harness(name, self.transcripts, max_turns=7).run(**values)

    def test_all_harness_auth_and_permission_combinations(self):
        for name in ("claude", "codex"):
            for auth in ("subscription", "api"):
                for mode in ("read", "write"):
                    with self.subTest(name=name, auth=auth, mode=mode):
                        result = self.run_harness(name, auth=auth, mode=mode)
                        self.assertEqual(result.output["markdown"], "# A spec")
                        self.assertTrue(result.transcript_path.is_file())
                        self.assertEqual(result.exit_code, 0)
                        call = self.calls()[-1]
                        args, env = call["args"], call["env"]
                        self.assertIn(f"Follow the instructions in {self.prompt} exactly.", args)
                        for forbidden in ("GH_TOKEN", "GITHUB_TOKEN", "SECRET", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
                            self.assertNotIn(forbidden, env)
                        for key in ("ANTHROPIC_API_KEY", "CODEX_API_KEY"):
                            expected = auth == "api" and key == ("ANTHROPIC_API_KEY" if name == "claude" else "CODEX_API_KEY")
                            self.assertEqual(key in env, expected)
                        if name == "claude":
                            self.assertEqual("--bare" in args, auth == "api")
                            self.assertEqual("--append-system-prompt-file" in args, auth == "api")
                            self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
                            self.assertEqual(args[args.index("--permission-prompts") + 1], "none")
                            self.assertEqual(args[args.index("--max-turns") + 1], "7")
                            self.assertEqual("--disallowedTools" in args, mode == "read")
                            allowed = args[args.index("--allowedTools") + 1]
                            self.assertEqual("Bash(python *)" in allowed, mode == "write")
                            self.assertNotIn("CODEX_HOME", env)
                            self.assertEqual(args[args.index("--model") + 1], "test-model")
                        else:
                            self.assertEqual(args[args.index("--sandbox") + 1], "read-only" if mode == "read" else "workspace-write")
                            self.assertEqual(args[args.index("--output-schema") + 1], str(self.schema.resolve()))
                            self.assertNotIn("CLAUDE_CONFIG_DIR", env)
                            self.assertEqual(args[args.index("-m") + 1], "test-model")
                        for forbidden in ("--full-auto", "--dangerously-bypass-approvals-and-sandbox", "bypassPermissions"):
                            self.assertNotIn(forbidden, args)

    def test_api_missing_key_fails_before_any_cli_call(self):
        for name, key in (("claude", "ANTHROPIC_API_KEY"), ("codex", "CODEX_API_KEY")):
            env = dict(self.source)
            del env[key]
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, key + ".*transcript:"):
                self.run_harness(name, auth="api", env=env)
        self.assertEqual(self.calls(), [])

    def test_failure_retains_both_streams(self):
        self.settings(exit_code=9, stderr="rate limited")
        with self.assertRaisesRegex(RuntimeError, "exited 9.*transcript:"):
            self.run_harness()
        self.assertIn("failed stdout", next(self.transcripts.glob("*.json")).read_text())
        self.assertIn("rate limited", next(self.transcripts.glob("*.stderr.log")).read_text())

    def test_timeout_terminates_descendants_and_keeps_partial_output(self):
        self.settings(sleep=90, child=True)
        with self.assertRaisesRegex(RuntimeError, "timed out.*transcript:"):
            self.run_harness(timeout_s=1)
        self.assertIn("partial stdout", next(self.transcripts.glob("*.json")).read_text())
        self.assertIn("partial stderr", next(self.transcripts.glob("*.stderr.log")).read_text())
        pid = int((self.root / "child.pid").read_text())
        status = Path(f"/proc/{pid}/stat")
        for _ in range(100):
            try:
                state = status.read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            self.fail("child still executing after timeout")

    def test_malformed_and_missing_structured_output_fail(self):
        for name in ("claude", "codex"):
            for settings in ({"raw": "not JSON"}, {"output": {"markdown": "  ", "open_questions": []}},
                             {"output": {"markdown": "text"}}, {"output": []}):
                with self.subTest(name=name, settings=settings):
                    self.settings(**settings)
                    with self.assertRaisesRegex(RuntimeError, "transcript:"):
                        self.run_harness(name)
        self.settings(raw='{"result":"plain text"}')
        with self.assertRaisesRegex(RuntimeError, "no structured_output"):
            self.run_harness()
        self.settings(missing_last=True)
        with self.assertRaisesRegex(RuntimeError, "final output file"):
            self.run_harness("codex")
        self.settings(is_error=True)
        with self.assertRaisesRegex(RuntimeError, "reported an error"):
            self.run_harness()

    def test_doctor_checks_real_read_and_write_and_keeps_cache_per_auth(self):
        self.settings(doctor=True)
        config = {"factory": {"harness": "claude", "auth": "subscription", "stage_timeout_min": 1},
                  "harness": {"claude": {"pinned_version": "2.1.259"}}}
        result = doctor(self.cwd, config)
        key = doctor_key("claude", "2.1.263 (Claude Code)", "subscription")
        self.assertEqual(result["selected"], key)
        self.assertTrue(result["records"][key]["passed"])
        self.assertIn("differs from pinned", result["records"][key]["warnings"][0])
        api_result = doctor(self.cwd, config, "codex", "api")
        self.assertEqual(len(api_result["records"]), 2)
        self.assertTrue(api_result["passed"])
        self.assertEqual(list((self.cwd / ".factory/tmp").iterdir()), [])

    def test_doctor_does_not_trust_claim_of_write(self):
        self.settings(doctor=True, skip_write=True)
        config = {"factory": {"harness": "codex", "auth": "subscription"}}
        with self.assertRaisesRegex(RuntimeError, "did not create.*transcript:"):
            doctor(self.cwd, config)
        cache = json.loads((self.cwd / ".factory/doctor.json").read_text())
        self.assertFalse(cache["passed"])
        self.assertFalse(cache["records"][cache["selected"]]["passed"])

    def test_doctor_read_mode_must_not_mutate_files(self):
        self.settings(doctor=True, dirty_read=True)
        with self.assertRaisesRegex(RuntimeError, "read probe failed"):
            doctor(self.cwd, {"factory": {"auth": "subscription"}})

    def test_doctor_missing_key_fails_before_github(self):
        del os.environ["ANTHROPIC_API_KEY"]
        with self.assertRaisesRegex((RuntimeError, ValueError), "ANTHROPIC_API_KEY"):
            doctor(self.cwd, {})
        self.assertEqual(self.calls(), [])


class SchemaTests(unittest.TestCase):
    def test_review_nested_contract_requires_evidence_and_enums(self):
        schema = json.loads((Path(__file__).parents[1] / "src/factory/schemas/review.json").read_text())
        valid = {"summary": "Two concerns", "updates": [{"id": "F1", "status": "resolved", "evidence": "Guard added at x.py:9"}],
                 "new": [{"severity": "important", "pass": "bugs", "file": "x.py", "line": None,
                          "title": "Input omitted", "detail": "Fails with an empty input", "evidence": "x.py indexes items[0] without checking length"}]}
        validate_schema(valid, schema)
        for field, value in (("evidence", " "), ("severity", "critical"), ("pass", "style"), ("line", True), ("line", 0)):
            invalid = json.loads(json.dumps(valid))
            invalid["new"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_schema(invalid, schema)
        valid["updates"][0]["invented"] = "field"
        with self.assertRaisesRegex(ValueError, "unexpected key"):
            validate_schema(valid, schema)

    def test_roles_render_with_documented_inputs(self):
        directory = Path(__file__).parents[1] / "src/factory/roles"
        context = {key: "fixture" for key in ("intent", "spec", "plan", "diff", "checks", "review_policy", "ledger", "findings")}
        for template in directory.glob("*.md"):
            with self.subTest(role=template.name):
                self.assertIn("fixture", template.read_text().format(**context))


if __name__ == "__main__":
    unittest.main()
