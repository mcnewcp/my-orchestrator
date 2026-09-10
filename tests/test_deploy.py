"""Run the host wrapper and entrypoint with fake executables, never Docker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.jsonl"
        for command in ["docker", "gh", "git", "factory"]:
            program = self.bin / command
            program.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, sys\n"
                "with open(os.environ['FACTORY_TEST_LOG'], 'a') as handle:\n"
                "    handle.write(json.dumps([pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')\n"
            )
            program.chmod(0o755)
        self.env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.root / "ephemeral-home"),
            "FACTORY_TEST_LOG": str(self.log),
        }
        self.env_dir = self.root / "etc/factory"
        self.env_dir.mkdir(parents=True)
        self.mount = self.root / "srv/factory/sample"
        (self.mount / "repo/.git").mkdir(parents=True)
        # Keep production paths fixed. Relocate a copy to temporary fixtures.
        self.wrapper = self.root / "factory-host"
        self.wrapper.write_text(
            (DEPLOY / "factory-host").read_text()
            .replace("/etc/factory/", f"{self.env_dir}/")
            .replace("/srv/factory/", f"{self.root}/srv/factory/")
        )
        self.wrapper.chmod(0o755)
        self.env_file = self.env_dir / "sample.env"
        self.write_env("FACTORY_IMAGE=factory-target:v0.1.0\nGH_TOKEN=test-secret\n")

    def write_env(self, content):
        self.env_file.write_text(content)
        self.env_file.chmod(0o600)

    def invoke(self, *args):
        return subprocess.run(
            [str(self.wrapper), *args], env=self.env,
            capture_output=True, text=True, check=False,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    @unittest.skipIf(os.getuid() == 0, "host wrapper requires a non-root account")
    def test_wrapper_preserves_arguments_and_keeps_secrets_out_of_argv(self):
        reason = 'operator reason; $(literal) "quoted"'
        result = self.invoke("sample", "dismiss", "42", "F3", reason)
        self.assertEqual(result.returncode, 0, result.stderr)
        call, = self.calls()
        self.assertEqual(call[-6:], ["factory-target:v0.1.0", "factory", "dismiss", "42", "F3", reason])
        self.assertIn(str(self.env_file), call)
        self.assertNotIn("test-secret", " ".join(call))
        self.assertIn(f"{os.getuid()}:{os.getgid()}", call)
        self.assertIn("HOME=/tmp/factory-home", call)
        self.assertIn("/tmp:rw,nosuid,nodev,mode=1777", call)
        self.assertIn(f"type=bind,src={self.mount},dst=/work", call)
        self.assertNotIn("-it", call)

    @unittest.skipIf(os.getuid() == 0, "host wrapper requires a non-root account")
    def test_wrapper_never_executes_env_file_contents(self):
        sentinel = self.root / "must-not-exist"
        self.write_env(
            "# literal Docker env-file values\n"
            "FACTORY_IMAGE=factory-target:v0.1.0\n"
            f"GH_TOKEN=$(touch {sentinel})\n"
        )
        result = self.invoke("sample", "poll")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists())
        self.assertNotIn("touch", " ".join(self.calls()[0]))

    @unittest.skipIf(os.getuid() == 0, "host wrapper requires a non-root account")
    def test_wrapper_refuses_unsafe_or_ambiguous_image_values(self):
        examples = [
            "FACTORY_IMAGE=bad;command\n",
            "FACTORY_IMAGE=$(whoami)\n",
            "FACTORY_IMAGE=--privileged\n",
            'FACTORY_IMAGE="factory:v0"\n',
            "FACTORY_IMAGE=factory:v0\nFACTORY_IMAGE=other:v0\n",
            "GH_TOKEN=token\n",
            "FACTORY_IMAGE=factory:v0\nGH_TOKEN\n",
        ]
        for content in examples:
            with self.subTest(content=content):
                self.write_env(content)
                result = self.invoke("sample", "poll")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(self.calls(), [])

    def test_wrapper_refuses_path_traversal(self):
        for name in ["../sample", "/sample", ".", "..", "a/b", "--mount", "a" * 65]:
            with self.subTest(name=name):
                result = self.invoke(name, "poll")
                self.assertEqual(result.returncode, 1)
                self.assertIn("simple name", result.stderr)
                self.assertEqual(self.calls(), [])

    @unittest.skipIf(os.getuid() == 0, "host wrapper requires a non-root account")
    def test_wrapper_refuses_readable_secret_file_permissions(self):
        self.env_file.chmod(0o644)
        result = self.invoke("sample", "poll")
        self.assertEqual(result.returncode, 1)
        self.assertIn("mode 600", result.stderr)
        self.assertNotIn("test-secret", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_wrapper_refuses_root(self):
        program = self.bin / "id"
        program.write_text("#!/bin/sh\nprintf '0\\n'\n")
        program.chmod(0o755)
        result = self.invoke("sample", "poll")
        self.assertEqual(result.returncode, 1)
        self.assertIn("non-root", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_entrypoint_sets_ephemeral_identity_and_token_helper(self):
        self.env.update({"GH_TOKEN": "test-secret", "FACTORY_GIT_NAME": "Factory Operator"})
        result = subprocess.run(
            [str(DEPLOY / "entrypoint"), "factory", "status", "42"],
            env=self.env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(Path(self.env["HOME"]).is_dir())
        self.assertEqual(self.calls(), [
            ["git", "config", "--global", "user.name", "Factory Operator"],
            ["git", "config", "--global", "user.email", "factory@localhost"],
            ["gh", "auth", "setup-git", "--hostname", "github.com"],
            ["factory", "status", "42"],
        ])
        self.assertNotIn("test-secret", self.log.read_text())

    def test_entrypoint_help_does_not_require_authentication(self):
        result = subprocess.run(
            [str(DEPLOY / "entrypoint"), "factory", "--help"],
            env=self.env, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "gh" for call in self.calls()))
        self.assertEqual(self.calls()[-1], ["factory", "--help"])


if __name__ == "__main__":
    unittest.main()
