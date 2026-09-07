"""Fresh official CLI sessions; credentials and output are explicit contracts."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from factory.errors import Blocked
from factory.process import ProcessRunner

PINNED_VERSIONS = {"codex": "0.153.4", "claude": "2.1.263"}
Role = Literal["prepare", "implement", "review"]
Engine = Literal["claude", "codex"]
Auth = Literal["subscription", "api"]


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PrepareOutput(StrictOutput):
    spec: str = Field(min_length=1)
    plan: str = Field(min_length=1)
    unresolved_decisions: list[str]


class ImplementOutput(StrictOutput):
    summary: str = Field(min_length=1)
    unresolved_decisions: list[str]
    plan_deviations: list[str]


class Finding(StrictOutput):
    severity: Literal["important", "nit"]
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    evidence: str = Field(min_length=1)
    message: str = Field(min_length=1)

    @model_validator(mode="after")
    def repository_path(self) -> Self:
        if Path(self.file).is_absolute() or ".." in Path(self.file).parts:
            raise ValueError("Finding file must be a repository-relative path")
        return self


class ReviewOutput(StrictOutput):
    decision: Literal["accept", "repair", "blocked"]
    summary: str = Field(min_length=1)
    findings: list[Finding]

    @model_validator(mode="after")
    def consistent_verdict(self) -> Self:
        important = any(f.severity == "important" for f in self.findings)
        if self.decision == "accept" and important:
            raise ValueError("Important findings cannot accompany acceptance")
        if self.decision == "repair" and not important:
            raise ValueError("Repair requires an important finding")
        if sum(f.severity == "nit" for f in self.findings) > 5:
            raise ValueError("At most five nits are allowed")
        return self


OUTPUT_SCHEMAS = {"prepare": PrepareOutput, "implement": ImplementOutput, "review": ReviewOutput}


def prompt_text(role: str) -> str:
    if role not in OUTPUT_SCHEMAS:
        raise ValueError(f"Unknown role: {role}")
    return files("factory").joinpath("prompts", f"{role}.md").read_text(encoding="utf-8")


_SAFE_ENV = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "TZ",
    "TERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "no_proxy",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
}
_PROVIDER_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "CODEX_", "OPENAI_", "AZURE_OPENAI_")
_CONTROLLER_PREFIXES = ("GH_", "GITHUB_", "PG", "FACTORY_", "DATABASE_")
_AMBIGUOUS_PROVIDER = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "AZURE_OPENAI_ENDPOINT",
    "CODEX_ACCESS_TOKEN",
}


def agent_environment(
    engine: str,
    auth: str,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if engine not in PINNED_VERSIONS or auth not in {"subscription", "api"}:
        raise Blocked("Select agent claude|codex and auth subscription|api")
    source = os.environ if source is None else source
    ambiguous = sorted(k for k in _AMBIGUOUS_PROVIDER if source.get(k))
    if ambiguous:
        raise Blocked("Ambiguous provider configuration: " + ", ".join(ambiguous))
    env = {k: v for k, v in source.items() if k in _SAFE_ENV or k.startswith("LC_")}
    config_key = "CODEX_HOME" if engine == "codex" else "CLAUDE_CONFIG_DIR"
    if source.get(config_key):
        env[config_key] = source[config_key]
    if auth == "api":
        key = "CODEX_API_KEY" if engine == "codex" else "ANTHROPIC_API_KEY"
        if not source.get(key):
            raise Blocked(f"API mode requires {key}")
        env[key] = source[key]
    env.update({"CI": "1", "DISABLE_AUTOUPDATER": "1"})
    return env


def check_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    return {
        k: v
        for k, v in source.items()
        if not k.startswith(_PROVIDER_PREFIXES + _CONTROLLER_PREFIXES)
        and k not in {"DB_PASSWORD", "DB_URL", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
    } | {"CI": "1", "GIT_TERMINAL_PROMPT": "0"}


class AgentRunner:
    def __init__(self, process: ProcessRunner | None = None):
        self.process = process or ProcessRunner()
        self._verified: set[str] = set()

    def validate(self, engine: str, auth: str, cwd: Path) -> dict[str, str]:
        env = agent_environment(engine, auth)
        if engine not in self._verified:
            version = self.process.run([engine, "--version"], cwd=cwd, env=env, timeout=30)
            actual = re.search(r"\b\d+\.\d+\.\d+\b", version.stdout)
            if version.returncode or not actual or actual.group() != PINNED_VERSIONS[engine]:
                raise Blocked(f"{engine} must be pinned to {PINNED_VERSIONS[engine]}")
            self._verified.add(engine)
        if auth == "subscription":
            args = (
                ["codex", "login", "status"]
                if engine == "codex"
                else [
                    "claude",
                    "auth",
                    "status",
                    "--json",
                ]
            )
            status = self.process.run(args, cwd=cwd, env=env, timeout=30)
            if status.returncode:
                raise Blocked(f"{engine} subscription login is required; run native login")
            if engine == "codex":
                if "chatgpt" not in (status.stdout + status.stderr).lower():
                    raise Blocked("Codex subscription mode requires native ChatGPT login")
            else:
                try:
                    account = json.loads(status.stdout)
                except ValueError as exc:
                    raise Blocked("Invalid Claude authentication status") from exc
                if not account.get("loggedIn") or account.get("authMethod") != "claude.ai":
                    raise Blocked("Claude subscription mode requires native claude.ai login")
        return env

    def run(
        self,
        role: str,
        engine: str,
        auth: str,
        cwd: Path,
        evidence_dir: Path,
        prompt: str,
        timeout: int,
    ) -> PrepareOutput | ImplementOutput | ReviewOutput:
        if role not in OUTPUT_SCHEMAS:
            raise Blocked(f"Unknown agent role: {role}")
        env = self.validate(engine, auth, cwd)
        # A recovered stage starts fresh while retaining every interrupted transcript.
        if evidence_dir.exists() and any(evidence_dir.iterdir()):
            evidence_dir.rename(evidence_dir.with_name(f"{evidence_dir.name}.prior-{uuid4().hex}"))
        evidence_dir.mkdir(parents=True, exist_ok=True)
        schema = OUTPUT_SCHEMAS[role]
        schema_path = evidence_dir / "schema.json"
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        schema_path.write_text(schema_json + "\n", encoding="utf-8")
        (evidence_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        output_path = evidence_dir / "response.json"
        if engine == "codex":
            argv = [
                "codex",
                "--ask-for-approval",
                "never",
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--json",
                "--color",
                "never",
                "--sandbox",
                "workspace-write" if role == "implement" else "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-c",
                'web_search="disabled"',
                "-c",
                "sandbox_workspace_write.network_access=false",
                "-c",
                "features.multi_agent=false",
                "-c",
                "features.multi_agent_v2=false",
                "-c",
                "features.apps=false",
                "-c",
                "features.plugins=false",
                "-c",
                "features.hooks=false",
                "-c",
                "features.shell_snapshot=false",
                "-",
            ]
        else:
            allowed = "Read,Glob,Grep" + (",Edit,Write" if role == "implement" else "")
            argv = [
                "claude",
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--json-schema",
                schema_json,
                "--no-session-persistence",
                "--permission-mode",
                "dontAsk",
                "--permission-prompts",
                "none",
                "--restricted",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--setting-sources",
                "",
                "--settings",
                '{"disableAllHooks":true}',
                "--disable-slash-commands",
                "--tools",
                allowed,
                "--allowedTools",
                allowed,
            ]
        result = self.process.run(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            log_path=evidence_dir / "transcript.log",
            input_text=prompt,
        )
        if result.returncode:
            raise Blocked(f"{engine} {role} failed ({result.returncode}); see transcript.log")
        try:
            if engine == "codex":
                data = json.loads(output_path.read_text(encoding="utf-8"))
            else:
                events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                if any(not isinstance(event, dict) for event in events):
                    raise ValueError("Claude events must be JSON objects")
                results = [event for event in events if event.get("type") == "result"]
                if len(results) != 1:
                    raise ValueError("Claude must emit exactly one final result")
                envelope = results[0]
                (evidence_dir / "envelope.json").write_text(
                    json.dumps(envelope, indent=2) + "\n",
                    encoding="utf-8",
                )
                if envelope.get("is_error") or envelope.get("subtype") != "success":
                    raise ValueError("Claude did not report success")
                data = envelope["structured_output"]
                output_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            output = schema.model_validate(data)
        except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
            raise Blocked(f"Invalid {engine} {role} output; see {evidence_dir}") from exc
        (evidence_dir / "validated.json").write_text(output.model_dump_json(indent=2) + "\n")
        return output
