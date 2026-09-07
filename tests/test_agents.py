import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from factory.agents import (
    PINNED_VERSIONS,
    AgentRunner,
    ReviewOutput,
    agent_environment,
    check_environment,
    prompt_text,
)
from factory.errors import Blocked
from factory.process import ProcessResult


def test_subscription_drops_api_overrides_and_controller_credentials():
    source = {
        "HOME": "/home/factory",
        "PATH": "/bin",
        "CODEX_HOME": "/auth/codex",
        "CLAUDE_CONFIG_DIR": "/auth/claude",
        "CODEX_API_KEY": "codex-secret",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "OPENAI_API_KEY": "other-secret",
        "GH_TOKEN": "github-secret",
        "FACTORY_DATABASE_URL": "postgresql://secret",
        "PGPASSWORD": "db-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
    }
    env = agent_environment("codex", "subscription", source)
    assert env["CODEX_HOME"] == "/auth/codex"
    assert set(env) == {"HOME", "PATH", "CODEX_HOME", "CI", "DISABLE_AUTOUPDATER"}


@pytest.mark.parametrize(
    "engine,key",
    [("codex", "CODEX_API_KEY"), ("claude", "ANTHROPIC_API_KEY")],
)
def test_api_requires_selected_key_without_fallback(engine, key):
    with pytest.raises(Blocked, match=key):
        agent_environment(engine, "api", {})
    env = agent_environment(engine, "api", {key: "selected", "GH_TOKEN": "not-for-agent"})
    assert env[key] == "selected"
    assert "GH_TOKEN" not in env


def test_provider_override_is_rejected_without_exposing_value():
    with pytest.raises(Blocked, match="ANTHROPIC_BASE_URL") as error:
        agent_environment("claude", "subscription", {"ANTHROPIC_BASE_URL": "secret-endpoint"})
    assert "secret-endpoint" not in str(error.value)


def test_checks_drop_provider_and_controller_secrets():
    env = check_environment(
        {
            "PATH": "/bin",
            "CODEX_API_KEY": "one",
            "ANTHROPIC_API_KEY": "two",
            "CLAUDE_CONFIG_DIR": "/auth/claude",
            "GH_TOKEN": "three",
            "PGPASSWORD": "four",
            "FACTORY_DATABASE_URL": "five",
            "PYTHONPATH": "/trusted/repo",
        }
    )
    assert env == {
        "PATH": "/bin",
        "PYTHONPATH": "/trusted/repo",
        "CI": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def finding(severity="important"):
    return {
        "severity": severity,
        "file": "src/app.py",
        "line": 12,
        "evidence": "The empty input reaches indexing without a guard.",
        "message": "Return the specified empty result before indexing.",
    }


@pytest.mark.parametrize(
    "data",
    [
        {"decision": "accept", "summary": "Fine", "findings": [finding()]},
        {"decision": "repair", "summary": "Polish", "findings": [finding("nit")]},
        {"decision": "accept", "summary": "Fine", "findings": [finding("nit")] * 6},
        {
            "decision": "accept",
            "summary": "Fine",
            "findings": [],
            "candidate_sha": "chosen-by-agent",
        },
        {"decision": "repair", "summary": "Fix", "findings": [finding() | {"line": "12"}]},
        {"decision": "repair", "summary": "Fix", "findings": [finding() | {"file": "../secret"}]},
    ],
)
def test_review_rejects_inconsistent_and_untrusted_evidence(data):
    with pytest.raises(ValidationError):
        ReviewOutput.model_validate(data)


class NativeStub:
    def __init__(self, output=None, envelope_error=False):
        self.calls = []
        self.output = output or {
            "spec": "## Unresolved decisions\nNone.\n",
            "plan": "Plan",
            "unresolved_decisions": [],
        }
        self.envelope_error = envelope_error

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        engine = argv[0]
        if argv[1:] == ["--version"]:
            return ProcessResult(0, f"{engine} {PINNED_VERSIONS[engine]}", "")
        if argv[1:3] == ["login", "status"]:
            return ProcessResult(0, "", "Logged in using ChatGPT")
        if argv[1:3] == ["auth", "status"]:
            return ProcessResult(0, '{"loggedIn":true,"authMethod":"claude.ai"}', "")
        if engine == "codex":
            path = Path(argv[argv.index("--output-last-message") + 1])
            path.write_text(json.dumps(self.output))
            return ProcessResult(0, '{"type":"turn.completed"}\n', "")
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": self.envelope_error,
            "structured_output": self.output,
        }
        return ProcessResult(0, '{"type":"system","subtype":"init"}\n' + json.dumps(envelope), "")


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_native_session_uses_fresh_readonly_schema_and_persists_exact_prompt(tmp_path, engine):
    process = NativeStub()
    runner = AgentRunner(process)
    prompt = prompt_text("prepare") + "\nFrozen issue body"
    evidence = tmp_path / "evidence"
    output = runner.run("prepare", engine, "subscription", tmp_path, evidence, prompt, 120)
    assert output.unresolved_decisions == []
    assert (evidence / "prompt.txt").read_text() == prompt
    assert (evidence / "validated.json").exists()
    command, kwargs = process.calls[-1]
    assert kwargs["input_text"] == prompt
    assert "--dangerously-skip-permissions" not in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    if engine == "codex":
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ephemeral" in command
        assert "features.multi_agent=false" in command
    else:
        assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
        assert "--restricted" in command
        assert "--bare" not in command


def test_claude_error_envelope_blocks_even_with_valid_structured_output(tmp_path):
    runner = AgentRunner(NativeStub(envelope_error=True))
    with pytest.raises(Blocked, match="Invalid claude prepare output"):
        runner.run("prepare", "claude", "subscription", tmp_path, tmp_path / "out", "task", 30)


def test_agent_cannot_assign_the_review_sha(tmp_path):
    runner = AgentRunner(
        NativeStub(
            {
                "decision": "accept",
                "summary": "Okay",
                "findings": [],
                "candidate_sha": "fake",
            }
        )
    )
    with pytest.raises(Blocked, match="Invalid codex review output"):
        runner.run("review", "codex", "subscription", tmp_path, tmp_path / "out", "task", 30)


def test_recovered_stage_keeps_prior_prompt_output_and_transcript(tmp_path):
    evidence = tmp_path / "prepare"
    evidence.mkdir()
    (evidence / "prompt.txt").write_text("interrupted prompt")
    (evidence / "response.json").write_text("incomplete output")
    (evidence / "transcript.log").write_text("interrupted transcript")
    runner = AgentRunner(NativeStub())
    runner.run("prepare", "codex", "subscription", tmp_path, evidence, "fresh prompt", 30)
    (archived,) = tmp_path.glob("prepare.prior-*")
    assert (archived / "prompt.txt").read_text() == "interrupted prompt"
    assert (archived / "response.json").read_text() == "incomplete output"
    assert (archived / "transcript.log").read_text() == "interrupted transcript"
    assert (evidence / "prompt.txt").read_text() == "fresh prompt"
