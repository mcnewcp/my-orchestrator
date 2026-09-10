"""The two headless CLI adapters, schema validation, and authenticated probes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any, Literal, Protocol
from dataclasses import dataclass
from uuid import uuid4

from .checks import filtered_env


FACTORY_VERSION = "0.1.0"
Auth = Literal["subscription", "api"]
Mode = Literal["read", "write"]


@dataclass(frozen=True)
class HarnessResult:
    output: dict
    transcript_path: Path
    exit_code: int
    cli_version: str


class Harness(Protocol):
    def version(self) -> str: ...

    def run(
        self, *, cwd: Path, prompt_file: Path, schema_file: Path, mode: Mode,
        model: str | None, auth: Auth, env: dict[str, str], timeout_s: int,
    ) -> HarnessResult: ...


def validate_schema(value: Any, schema: dict, path: str = "$", root: dict | None = None) -> None:
    """Validate the JSON Schema subset used by our bundled, strict schemas.

    This is deliberately a validator for factory schemas, not an implementation
    of the full JSON Schema standard. The CLI and this check both validate output.
    """
    root = schema if root is None else root
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/"):
            raise ValueError(f"{path}: only local schema references are supported")
        target = root
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        validate_schema(value, target, path, root)
    for option in schema.get("allOf", []):
        validate_schema(value, option, path, root)
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            matches = 0
            for option in schema[keyword]:
                try:
                    validate_schema(value, option, path, root)
                    matches += 1
                except ValueError:
                    pass
            if matches == 0 or (keyword == "oneOf" and matches != 1):
                raise ValueError(f"{path}: does not match {keyword}")
    types = schema.get("type")
    if isinstance(types, str):
        types = [types]
    if types:
        predicates = {
            "null": value is None,
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "string": isinstance(value, str),
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
        }
        if not any(predicates.get(kind, False) for kind in types):
            raise ValueError(f"{path}: expected {' or '.join(types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: expected one of {schema['enum']!r}")
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        raise ValueError(f"{path}: unexpected value")
    if isinstance(value, dict):
        missing = set(schema.get("required", [])) - value.keys()
        if missing:
            raise ValueError(f"{path}: missing required keys: {', '.join(sorted(missing))}")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], f"{path}.{key}", root)
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"{path}: unexpected key {key!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                validate_schema(item, schema["additionalProperties"], f"{path}.{key}", root)
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise ValueError(f"{path}: invalid array length")
        for index, item in enumerate(value):
            if "items" in schema:
                validate_schema(item, schema["items"], f"{path}[{index}]", root)
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise ValueError(f"{path}: invalid string length")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValueError(f"{path}: string does not match required pattern")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise ValueError(f"{path}: number outside allowed range")


class _CLI:
    name: str
    executable: str

    def __init__(self, transcripts_dir: Path, max_turns: int = 100):
        self.transcripts_dir = Path(transcripts_dir).resolve()
        self.max_turns = max_turns

    def version(self) -> str:
        try:
            result = subprocess.run(
                [self.executable, "--version"], env=filtered_env(harness=self.name),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Cannot run {self.executable} --version: {exc}") from exc
        version = result.stdout.strip()
        if result.returncode or not version:
            raise RuntimeError(f"Cannot identify {self.executable}: {result.stderr.strip()}")
        return version

    def command(self, *, prompt_file: Path, schema_file: Path, schema: dict,
                mode: Mode, model: str | None, auth: Auth, cwd: Path,
                last_file: Path) -> list[str]:
        raise NotImplementedError

    def parse_output(self, stdout: str, last_file: Path) -> dict:
        raise NotImplementedError

    def run(
        self, *, cwd: Path, prompt_file: Path, schema_file: Path, mode: Mode,
        model: str | None, auth: Auth, env: dict[str, str], timeout_s: int,
    ) -> HarnessResult:
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{time.time_ns()}-{self.name}-{uuid4().hex[:8]}"
        transcript = self.transcripts_dir / f"{stem}.{'jsonl' if self.name == 'codex' else 'json'}"
        errors = self.transcripts_dir / f"{stem}.stderr.log"
        last_file = self.transcripts_dir / f"{stem}.last.json"
        transcript.touch()
        errors.touch()
        try:
            if mode not in ("read", "write") or auth not in ("subscription", "api"):
                raise ValueError("Invalid harness mode or authentication mode")
            if timeout_s <= 0:
                raise ValueError("Harness timeout must be positive")
            clean_env = filtered_env(harness=self.name, auth=auth, source=env)
            key = "ANTHROPIC_API_KEY" if self.name == "claude" else "CODEX_API_KEY"
            if auth == "api" and not clean_env.get(key):
                raise ValueError(f"{auth} authentication requires {key}; no login fallback is allowed")
            prompt_file = Path(prompt_file).resolve()
            schema_file = Path(schema_file).resolve()
            cwd = Path(cwd).resolve()
            if not prompt_file.is_file():
                raise ValueError(f"Prompt does not exist: {prompt_file}")
            schema = json.loads(schema_file.read_text())
            if not isinstance(schema, dict):
                raise ValueError("Output schema must be a JSON object")
            version = self.version()
            command = self.command(
                prompt_file=prompt_file, schema_file=schema_file, schema=schema,
                mode=mode, model=model, auth=auth, cwd=cwd, last_file=last_file,
            )
            with transcript.open("wb") as out, errors.open("wb") as err:
                process = subprocess.Popen(
                    command, cwd=cwd, env=clean_env, stdin=subprocess.DEVNULL,
                    stdout=out, stderr=err, start_new_session=True,
                )
                try:
                    code = process.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    self._stop_group(process)
                    raise RuntimeError(f"{self.name} timed out after {timeout_s}s") from None
                except BaseException:
                    self._stop_group(process)
                    raise
            if code:
                raise RuntimeError(f"{self.name} exited {code}")
            output = self.parse_output(transcript.read_text(errors="replace"), last_file)
            if not isinstance(output, dict):
                raise ValueError("Harness output must be a JSON object")
            validate_schema(output, schema)
            return HarnessResult(output, transcript, code, version)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            raise RuntimeError(f"{exc}; transcript: {transcript}; stderr: {errors}") from exc

    @staticmethod
    def _stop_group(process: subprocess.Popen) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            if sig == signal.SIGTERM:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        process.wait()


class ClaudeCode(_CLI):
    name = "claude"
    executable = "claude"

    def command(self, *, prompt_file: Path, schema_file: Path, schema: dict,
                mode: Mode, model: str | None, auth: Auth, cwd: Path,
                last_file: Path) -> list[str]:
        command = [
            "claude", "-p", f"Follow the instructions in {prompt_file} exactly.",
            "--output-format", "json", "--permission-mode", "dontAsk",
            "--permission-prompts", "none", "--max-turns", str(self.max_turns),
            "--json-schema", json.dumps(schema),
        ]
        if auth == "api":
            agents = cwd / "AGENTS.md"
            if not agents.is_file():
                raise ValueError(f"Claude API mode requires {agents}")
            command += ["--bare", "--append-system-prompt-file", str(agents)]
        if mode == "read":
            command += [
                "--allowedTools", "Read,Grep,Glob", "--disallowedTools",
                "Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch",
            ]
        else:
            command += [
                "--allowedTools",
                "Read,Grep,Glob,Edit,Write,Bash(make *),Bash(pytest *),Bash(uv *),Bash(python *)",
            ]
        if model:
            command += ["--model", model]
        return command

    def parse_output(self, stdout: str, last_file: Path) -> dict:
        envelope = json.loads(stdout)
        if not isinstance(envelope, dict):
            raise ValueError("Claude returned an invalid JSON envelope")
        if envelope.get("is_error"):
            raise ValueError(f"Claude reported an error: {envelope.get('subtype', 'unknown')}")
        if "structured_output" not in envelope:
            raise ValueError("Claude returned no structured_output")
        return envelope["structured_output"]


class Codex(_CLI):
    name = "codex"
    executable = "codex"

    def command(self, *, prompt_file: Path, schema_file: Path, schema: dict,
                mode: Mode, model: str | None, auth: Auth, cwd: Path,
                last_file: Path) -> list[str]:
        command = [
            "codex", "exec", "--json", "-o", str(last_file), "--output-schema",
            str(schema_file), "--sandbox", "read-only" if mode == "read" else "workspace-write",
        ]
        if model:
            command += ["-m", model]
        command.append(f"Follow the instructions in {prompt_file} exactly.")
        return command

    def parse_output(self, stdout: str, last_file: Path) -> dict:
        if not last_file.is_file():
            raise ValueError("Codex did not create its final output file")
        return json.loads(last_file.read_text())


def make_harness(name: str, transcripts_dir: Path, max_turns: int = 100) -> Harness:
    implementations = {"claude": ClaudeCode, "codex": Codex}
    if name not in implementations:
        raise ValueError(f"Unknown harness: {name}")
    return implementations[name](transcripts_dir, max_turns)


def doctor_key(harness: str, cli_version: str, auth: str) -> str:
    return f"{FACTORY_VERSION}:{harness}:{cli_version}:{auth}"


def _doctor_command(argv: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Doctor could not run {argv[0]}: {exc}") from exc
    if result.returncode:
        raise RuntimeError(f"Doctor failed: {' '.join(argv)}: {result.stderr.strip()}")
    return result.stdout.strip()


def doctor(root: Path, config: dict, harness_override: str | None = None,
           auth_override: str | None = None) -> dict:
    """Check tools, GitHub auth, and actual read/write permissions for one harness."""
    root = Path(root).resolve()
    factory = config.get("factory", {})
    name = harness_override or factory.get("harness", "claude")
    auth = auth_override or factory.get("auth", "api")
    # Fail before even calling gh when unattended authentication is unavailable.
    filtered_env(harness=name, auth=auth, source=dict(os.environ))
    provider_key = "ANTHROPIC_API_KEY" if name == "claude" else "CODEX_API_KEY"
    if auth == "api" and not os.environ.get(provider_key):
        raise RuntimeError(f"api authentication requires {provider_key}; no login fallback is allowed")
    for binary in ("git", "gh", name):
        if shutil.which(binary) is None:
            raise RuntimeError(f"Doctor requires {binary} on PATH")
    binary_versions = {tool: _doctor_command([tool, "--version"], root) for tool in ("git", "gh")}
    _doctor_command(["gh", "auth", "status"], root)
    adapter = make_harness(name, root / ".factory" / "transcripts")
    version = adapter.version()
    settings = config.get("harness", {}).get(name, {})
    if name == "claude":
        numeric = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        if numeric and tuple(map(int, numeric.groups())) < (2, 1, 259):
            raise RuntimeError("Claude Code >= 2.1.259 is required for --permission-prompts none")
    key = doctor_key(name, version, auth)
    record = {
        "factory_version": FACTORY_VERSION, "harness": name, "cli_version": version,
        "auth": auth, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "passed": False, "warnings": [], "binaries": binary_versions,
    }
    cache_path = root / ".factory" / "doctor.json"
    try:
        cached = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    except (OSError, ValueError):
        cached = {}
    if not isinstance(cached, dict) or not isinstance(cached.get("records", {}), dict):
        cached = {}
    cached.setdefault("records", {})[key] = record
    scratch_parent = root / ".factory" / "tmp"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="doctor-", dir=scratch_parent) as directory:
            scratch = Path(directory)
            _doctor_command(["git", "init", "--quiet", str(scratch)], root)
            (scratch / "AGENTS.md").write_text("Follow the probe instructions. Never commit, push, or access GitHub.\n")
            (scratch / "CLAUDE.md").write_text("@AGENTS.md\n")
            nonce = f"factory-doctor-{uuid4().hex}"
            (scratch / "input.txt").write_text(nonce + "\n")
            prompt = scratch / "probe.md"
            schema = scratch / "probe.json"
            probe_schema = {
                "type": "object", "additionalProperties": False,
                "properties": {"message": {"type": "string"}}, "required": ["message"],
            }
            schema.write_text(json.dumps(probe_schema))
            prompt.write_text(
                "Read input.txt. Return its exact contents, excluding the final newline, "
                "as the message field. Do not write or modify any files.\n"
            )
            before = {p.relative_to(scratch): p.read_bytes() for p in scratch.rglob("*") if p.is_file()}
            kwargs = dict(cwd=scratch, prompt_file=prompt, schema_file=schema,
                          model=settings.get("model") or None, auth=auth,
                          env=dict(os.environ), timeout_s=int(factory.get("stage_timeout_min", 45) * 60))
            read = adapter.run(mode="read", **kwargs)
            after = {p.relative_to(scratch): p.read_bytes() for p in scratch.rglob("*") if p.is_file()}
            if read.output.get("message") != nonce or before != after:
                raise RuntimeError(f"Doctor read probe failed; transcript: {read.transcript_path}")
            prompt.write_text(
                f"Create factory-doctor-probe.txt containing exactly {nonce!r} followed by one newline. "
                "Do not modify any other files. Do not commit, push, or access GitHub. "
                "Return the file contents excluding the final newline in the message field.\n"
            )
            write = adapter.run(mode="write", **kwargs)
            created = scratch / "factory-doctor-probe.txt"
            if not created.is_file() or created.read_text() != nonce + "\n" or write.output.get("message") != nonce:
                raise RuntimeError(f"Doctor write probe did not create the required file; transcript: {write.transcript_path}")
            record.update(passed=True, read_transcript=str(read.transcript_path), write_transcript=str(write.transcript_path))
    except Exception as exc:
        record["error"] = str(exc)
        raise
    finally:
        cached.update(factory_version=FACTORY_VERSION, selected=key, passed=record["passed"])
        temporary = cache_path.with_name(f"doctor-{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(cached, indent=2) + "\n")
        temporary.replace(cache_path)
    return cached
