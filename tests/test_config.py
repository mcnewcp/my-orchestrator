"""Configuration and override behavior at the TOML and CLI boundaries."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from factory.cli import main, parser
from factory.config import ROLES, TEMPLATE, load_config, resolve_role


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def load(self, text):
        (self.root / "factory.toml").write_text(text)
        return load_config(self.root)

    def test_template_and_missing_optional_config_use_cli_defaults(self):
        optional = load_config(self.root, optional=True)
        template = self.load(TEMPLATE)
        for config in (optional, template):
            self.assertNotIn("harness", config)
            self.assertEqual(set(config["roles"]), set(ROLES))
            for settings in config["roles"].values():
                self.assertEqual(settings, {"harness": "claude", "model": "", "effort": ""})
        template["roles"]["build"]["harness"] = "codex"
        self.assertEqual(self.load("")["roles"]["build"]["harness"], "claude")

    def test_factory_role_and_cli_resolution_preserves_explicit_empty_values(self):
        config = self.load('''[factory]
harness = "claude"
model = "factory-model"
effort = "medium"
[roles.plan]
model = ""
effort = ""
[roles.build]
harness = "codex"
[roles.review]
model = "review-model"
effort = "max"
[roles.fix]
harness = "codex"
model = "fix-model"
effort = "low"
''')
        self.assertEqual(config["roles"], {
            "spec": {"harness": "claude", "model": "factory-model", "effort": "medium"},
            "plan": {"harness": "claude", "model": "", "effort": ""},
            "build": {"harness": "codex", "model": "factory-model", "effort": "medium"},
            "review": {"harness": "claude", "model": "review-model", "effort": "max"},
            "fix": {"harness": "codex", "model": "fix-model", "effort": "low"},
        })
        for role in ROLES:
            self.assertEqual(resolve_role(config, role, harness="codex", model="cli-model", effort="high"),
                             {"harness": "codex", "model": "cli-model", "effort": "high"})
            self.assertEqual(resolve_role(config, role, model="", effort="")["model"], "")
            self.assertEqual(resolve_role(config, role, model="", effort="")["effort"], "")
        self.assertEqual(resolve_role(config, "review", effort="low")["model"], "review-model")
        self.assertEqual(config["roles"]["review"]["effort"], "max")

    def test_unknown_keys_values_and_types_fail_at_load_with_key(self):
        cases = [
            ('[roles.typo]\nmodel = "x"', "roles.typo"),
            ('[roles.build]\nmodle = "x"', "roles.build.modle"),
            ('[roles]\nbuild = "codex"', "roles.build"),
            ('[harness.claude]\nmodel = "x"', "harness"),
            ('[factory]\nharness = "codex"\neffort = "max"', "factory.effort"),
            ('[factory]\neffort = "max"\n[roles.build]\nharness = "codex"', "factory.effort"),
        ]
        for table in ("factory", *(f"roles.{role}" for role in ROLES)):
            for key, value in (("harness", '"unknown"'), ("harness", '""'), ("harness", "42"),
                               ("model", "false"), ("effort", "42"), ("effort", '"xhigh"')):
                cases.append((f"[{table}]\n{key} = {value}", f"{table}.{key}"))
        for source, key in cases:
            with self.subTest(source=source), self.assertRaises(ValueError) as error:
                self.load(source)
            self.assertIn(key, str(error.exception))

    def test_effort_support_is_checked_against_effective_harness(self):
        config = self.load('[factory]\neffort = "max"\n[roles.build]\nharness = "codex"\neffort = ""')
        self.assertEqual(config["roles"]["build"]["effort"], "")
        with self.assertRaisesRegex(ValueError, "max is supported only by claude"):
            resolve_role(config, "review", harness="codex")
        with self.assertRaisesRegex(ValueError, "--effort"):
            resolve_role(config, "build", effort="max")
        for name in ("claude", "codex"):
            for effort in ("", "low", "medium", "high"):
                config = self.load(f'[factory]\nharness = "{name}"\neffort = "{effort}"')
                self.assertEqual(config["roles"]["fix"]["effort"], effort)

    def test_effort_overrides_parse_on_issue_commands_and_doctor_but_poll_refuses(self):
        for command in ("doctor", "spec", "accept", "plan", "build", "review", "fix",
                        "finalize", "run", "status", "dismiss", "abandon"):
            args = [command] + ([] if command == "doctor" else ["42"])
            if command == "dismiss":
                args += ["F1", "reason"]
            for flags in (["--effort", "high"], ["--effort", ""]):
                self.assertEqual(parser().parse_args(flags + args).effort, flags[1])
                self.assertEqual(parser().parse_args(args + flags).effort, flags[1])
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["--effort", "high", "poll"]), 1)
            self.assertIn("poll accepts no overrides", errors.getvalue())
            with self.assertRaises(SystemExit) as error:
                main(["poll", "--effort", "high"])
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
