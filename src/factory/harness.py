"""Harness adapter — the interchangeability boundary (design §8).

Verified on this machine 2026-09-06 (see docs/design/harness-smoke-2026-09-06.md):
  claude 2.1.263: `claude -p "<msg>" --output-format json --permission-mode dontAsk --permission-prompts none
                   --json-schema '<schema>' [--allowedTools ...] [--disallowedTools ...] [--max-turns N] [--model M] [--bare]`
                  stdout = one JSON object; the artifact is its `structured_output` field; branch on `is_error`
                  BEFORE `subtype` (an auth failure returns exit 1, is_error=true, subtype="success").
                  --bare with no ANTHROPIC_API_KEY exits 1 in <1s with result "Not logged in".
  codex 0.153.4:  `codex exec --json --sandbox read-only|workspace-write --output-schema <file> -o <last.json>
                   [-m M] -C <cwd> "<msg>"`
                  stdout = JSONL events (thread.started, turn.started, item.*, turn.completed); the artifact
                  is the JSON in <last.json>. Exit code reports harness health, not task success.
  BOTH: stdin MUST be subprocess.DEVNULL — codex blocks forever reading a non-tty stdin; claude stalls 3s.

The command-line prompt is always the fixed sentence PROMPT_SENTENCE. Role, inputs and policy live in the
committed prompt file; the output shape lives in the schema file.

Subprocess contract (both harnesses, and checks.run_checks): STREAM, DON'T BUFFER. Open transcript_path ("wb") and
a sibling ".stderr" file, Popen(argv, cwd=cwd, env=env, stdin=DEVNULL, stdout=tf, stderr=ef, start_new_session=True),
proc.wait(timeout=timeout_s). On TimeoutExpired: os.killpg(os.getpgid(proc.pid), SIGTERM); wait 10 s; SIGKILL the group;
raise HarnessError("<harness> timed out after Ns", transcript_path=...). The transcript is complete on disk on every
path including timeout. Never capture_output=True: a 45-minute JSONL stream does not belong in memory, and a surviving
grandchild holding the pipe would block communicate() past the timeout.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from .errors import FactoryError, HarnessError
from .schema import dumps_compact

Mode = Literal["read", "write"]
Auth = Literal["subscription", "api"]

PROMPT_SENTENCE = "Follow the instructions in {prompt_file} exactly."

PROVIDER_KEYS: dict[str, str] = {"claude": "ANTHROPIC_API_KEY", "codex": "CODEX_API_KEY"}
# Every credential either CLI honours. codex 0.153.4 accepts OPENAI_API_KEY, CODEX_API_KEY and CODEX_ACCESS_TOKEN and
# warns "multiple auth env vars are present" when more than one is set; claude honours ANTHROPIC_API_KEY,
# ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN. None of these is ever forwarded except the single selected key.
ALL_PROVIDER_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "OPENAI_API_KEY",
)
CONFIG_DIR_VARS: dict[str, str] = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "TERM",
    "USER",
    "LOGNAME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
)
# Both binaries read these; without them egress breaks behind a proxy/MITM CA.
NETWORK_ALLOWLIST: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)
STRIPPED_ALWAYS: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN")
# Postcondition of build_env, asserted in tests: no key matches this except the one selected provider key in api mode.
FORBIDDEN_KEY_PATTERN = r"^(CLAUDE|CLAUDECODE|ANTHROPIC|CODEX|OPENAI|AI_AGENT|GH_|GITHUB_|FACTORY_)"

CLAUDE_READ_ALLOWED = "Read,Grep,Glob"
CLAUDE_READ_DISALLOWED = "Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch"
CLAUDE_WRITE_ALLOWED = "Read,Grep,Glob,Edit,Write,Bash(make *),Bash(pytest *),Bash(uv *),Bash(python *),Bash(python3 *)"
# Every claude invocation adds these: the worktree's .claude/ settings, hooks and .mcp.json are repository-controlled
# input and must not configure the session that reviews that same repository. --bare's documented skip list covers
# hooks and CLAUDE.md but not project settings files (whose `env` block injects environment into every tool call),
# so api mode passes them too. Verified present in claude 2.1.263; doctor's read probe exercises them.
CLAUDE_ALWAYS = ("--setting-sources", "user", "--strict-mcp-config")
CLAUDE_MIN_VERSION = (2, 1, 259)  # --permission-prompts none (design §19)

# Claude Code spends a cheap helper model on titles and summaries; it is never the model that did the work.
_HELPER_MODEL_PREFIX = "claude-haiku"
_TERM_GRACE_S = 10.0  # SIGTERM -> SIGKILL window for a timed-out process group (module docstring)
_KILL_GRACE_S = 5.0
_VERSION_TIMEOUT_S = 60
_ERROR_TAIL_LINES = 20


@dataclass
class HarnessResult:
    output: dict
    transcript_path: Path
    exit_code: int
    cli_version: str
    # actual model when the CLI reports it (claude: modelUsage keys minus haiku helpers; codex: None)
    model: str | None = None
    session_id: str | None = None
    duration_s: float = 0.0
    # claude: result["permission_denials"]; logged, never a gate
    permission_denials: list[dict] = field(default_factory=list)
    # claude: result["num_turns"]; == max_turns is logged as "hit the turn bound"
    num_turns: int = 0


class Harness(Protocol):
    name: str

    def run(
        self,
        *,
        cwd: Path,
        prompt_file: Path,
        schema_file: Path,
        mode: Mode,
        model: str | None,
        auth: Auth,
        env: dict[str, str],
        timeout_s: int,
        transcript_path: Path,
        max_turns: int | None = None,
        max_budget_usd: float = 0.0,
        agents_md: Path | None = None,
        writable_dirs: list[str] | None = None,
    ) -> HarnessResult: ...

    def version(self, env: dict[str, str]) -> str: ...


def build_env(
    harness: str,
    auth: Auth,
    parent: dict[str, str] | None = None,
    *,
    passthrough: list[str] | None = None,
) -> dict[str, str]:
    """Allowlisted environment for a harness subprocess (design §8 "Environment").

    Constructed from the allowlists ONLY (never by copying `parent` by pattern): ENV_ALLOWLIST + NETWORK_ALLOWLIST +
    CONFIG_DIR_VARS[harness] + config.env_passthrough names (`passthrough`), each copied when present in parent
    (parent defaults to os.environ). Then:
      auth == "api":          add PROVIDER_KEYS[harness] from parent, or FactoryError("api auth: <KEY> is not set; export
                              it or use --auth subscription") — never a silent switch to a saved login.
      auth == "subscription": no provider key at all.
    Postcondition (tested): no key matches FORBIDDEN_KEY_PATTERN except the single selected key and the harness's own
    CONFIG_DIR_VARS entry (CLAUDE_CONFIG_DIR / CODEX_HOME, which the pattern also matches); STRIPPED_ALWAYS and
    every ALL_PROVIDER_KEYS entry other than the selected one are absent even if named in `passthrough`. A
    `passthrough` name matching FORBIDDEN_KEY_PATTERN is dropped outright, so a repo's env_passthrough cannot smuggle
    a credential (or a nesting marker) into the session. This workstation's environment carries CLAUDECODE, AI_AGENT,
    CLAUDE_EFFORT, CLAUDE_PID and CLAUDE_CODE_MESSAGING_*; a nested Claude Code session must not be detected and must
    not be able to message its parent.
    Always sets GIT_TERMINAL_PROMPT=0 and, when TERM is absent, TERM=dumb.
    """
    _require_harness(harness)
    _require_auth(auth)
    source = dict(os.environ) if parent is None else dict(parent)

    names: list[str] = [*ENV_ALLOWLIST, *NETWORK_ALLOWLIST, CONFIG_DIR_VARS[harness]]
    for raw in passthrough or []:
        name = raw.strip()
        if name and name not in names and _is_forwardable(name):
            names.append(name)

    env = {name: source[name] for name in names if name in source}

    if auth == "api":
        key = PROVIDER_KEYS[harness]
        value = source.get(key)
        if not value:
            raise FactoryError(f"api auth: {key} is not set; export it or use --auth subscription")
        env[key] = value

    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("TERM", "dumb")
    return env


def checks_env(harness_env: dict[str, str]) -> dict[str, str]:
    """Design §8: check commands get the same (allowlisted) environment minus ALL_PROVIDER_KEYS, so repository-controlled
    test code never sees a credential. Repos whose toolchain needs more (JAVA_HOME, GOPATH, ...) list the names in
    config.env_passthrough, which reaches both the harness and the checks. Also sets CI=1."""
    env = {name: value for name, value in harness_env.items() if name not in ALL_PROVIDER_KEYS}
    env["CI"] = "1"
    return env


def prompt_sentence(cwd: Path, prompt_file: Path) -> str:
    """PROMPT_SENTENCE with prompt_file rendered relative to cwd (posix)."""
    relative = os.path.relpath(os.path.abspath(prompt_file), os.path.abspath(cwd))
    return PROMPT_SENTENCE.format(prompt_file=Path(relative).as_posix())


def parse_version(text: str) -> tuple[int, ...]:
    """'2.1.263 (Claude Code)' -> (2,1,263); 'codex-cli 0.153.4' -> (0,153,4).

    FactoryError when `text` holds no dotted version number: a CLI whose --version output stopped being parseable
    is exactly the "both CLIs change fast" case doctor exists to surface (design §19).
    """
    match = re.search(r"\d+(?:\.\d+)+", text)
    if match is None:
        raise FactoryError(f"cannot read a version number from {text.strip()!r}")
    return tuple(int(part) for part in match.group(0).split("."))


def run_streaming(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: int,
    stdout_path: Path,
    stderr_path: Path,
    what: str,
) -> tuple[int, float]:
    """The shared subprocess contract from the module docstring. Returns (exit_code, duration_s); raises HarnessError on
    timeout (after killing the process group) with transcript_path=stdout_path. Also used by checks.run_checks.

    A binary that cannot be executed raises FactoryError naming it (its message is also written to stderr_path, so a
    caller that keeps the log rather than the exception still sees why).
    """
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with open(stdout_path, "wb") as out_file, open(stderr_path, "wb") as err_file:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out_file,
                stderr=err_file,
                start_new_session=True,
            )
        except OSError as exc:
            err_file.write(f"{argv[0]}: {exc}\n".encode())
            raise FactoryError(f"{what}: cannot run {argv[0]}: {exc.strerror or exc}") from exc
        try:
            exit_code = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            raise HarnessError(
                f"{what} timed out after {timeout_s}s and its process group was killed; "
                f"partial output: {stdout_path}",
                transcript_path=stdout_path,
            ) from None
    return exit_code, time.monotonic() - started


class ClaudeCode:
    name = "claude"

    def version(self, env: dict[str, str]) -> str:
        """`claude --version` -> e.g. '2.1.263 (Claude Code)'. FactoryError if the binary is missing."""
        return _cli_version_line(self.name, env)

    def argv(
        self,
        *,
        cwd: Path,
        prompt_file: Path,
        schema: dict,
        mode: Mode,
        model: str | None,
        auth: Auth,
        max_turns: int | None,
        max_budget_usd: float,
        agents_md: Path | None,
    ) -> list[str]:
        """Build the command line (design §8 table):
          claude -p <sentence> --output-format json --permission-mode dontAsk --permission-prompts none
                 --setting-sources user --strict-mcp-config --json-schema <compact schema>
                 [--max-turns N] [--max-budget-usd X (when > 0)] [--model M]
          mode=read  adds --allowedTools CLAUDE_READ_ALLOWED --disallowedTools CLAUDE_READ_DISALLOWED
          mode=write adds --allowedTools CLAUDE_WRITE_ALLOWED
          auth=api   adds --bare and, when agents_md exists, --append-system-prompt-file <agents_md>
        The prompt sentence is the FIRST positional, immediately after -p, and never the last element of argv:
        --allowedTools/--disallowedTools are variadic in claude 2.1.263 and a trailing sentence would be parsed as
        another tool name. Pass each tool list as ONE comma-separated argument."""
        _require_mode(mode)
        _require_auth(auth)
        argv = [
            self.name,
            "-p",
            prompt_sentence(cwd, prompt_file),
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            *CLAUDE_ALWAYS,
            "--json-schema",
            dumps_compact(schema),
        ]
        if max_turns is not None:
            argv += ["--max-turns", str(max_turns)]
        if max_budget_usd > 0:
            argv += ["--max-budget-usd", f"{max_budget_usd:g}"]
        if model:
            argv += ["--model", model]
        if mode == "read":
            argv += [
                "--allowedTools",
                CLAUDE_READ_ALLOWED,
                "--disallowedTools",
                CLAUDE_READ_DISALLOWED,
            ]
        else:
            argv += ["--allowedTools", CLAUDE_WRITE_ALLOWED]
        if auth == "api":
            argv.append("--bare")
            if agents_md is not None and Path(agents_md).exists():
                argv += ["--append-system-prompt-file", str(agents_md)]
        return argv

    def run(
        self,
        *,
        cwd,
        prompt_file,
        schema_file,
        mode,
        model,
        auth,
        env,
        timeout_s,
        transcript_path,
        max_turns=None,
        max_budget_usd=0.0,
        agents_md=None,
        writable_dirs=None,
    ) -> HarnessResult:
        """run_streaming(...) then parse transcript_path as one JSON object.
        exit != 0 or is_error -> HarnessError(result text or stderr tail, transcript_path); missing/invalid JSON ->
        HarnessError; missing structured_output -> HarnessError. Returns HarnessResult(output=structured_output,
        model=<the non-haiku modelUsage key if any>, session_id, permission_denials, num_turns, cli_version).
        `writable_dirs` is codex-only and ignored here."""
        cli_version = self.version(env)
        schema = _read_schema(schema_file)
        argv = self.argv(
            cwd=cwd,
            prompt_file=prompt_file,
            schema=schema,
            mode=mode,
            model=model,
            auth=auth,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            agents_md=agents_md,
        )
        stderr_path = _stderr_path(transcript_path)
        exit_code, duration_s = run_streaming(
            argv,
            cwd=cwd,
            env=env,
            timeout_s=timeout_s,
            stdout_path=transcript_path,
            stderr_path=stderr_path,
            what=self.name,
        )

        result = self._result_or_raise(transcript_path, stderr_path, exit_code)
        output = result.get("structured_output")
        if not isinstance(output, dict):
            raise HarnessError(
                f"claude returned no structured_output object in {transcript_path}; "
                "the --json-schema contract was not honoured",
                transcript_path=transcript_path,
            )
        return HarnessResult(
            output=output,
            transcript_path=transcript_path,
            exit_code=exit_code,
            cli_version=cli_version,
            model=_claude_model(result),
            session_id=_as_str(result.get("session_id")),
            duration_s=duration_s,
            permission_denials=_as_dict_list(result.get("permission_denials")),
            num_turns=_as_int(result.get("num_turns")),
        )

    def _result_or_raise(self, transcript_path: Path, stderr_path: Path, exit_code: int) -> dict:
        """The result object from the transcript. Design §8 / smoke note: branch on is_error BEFORE subtype
        (an auth failure reports exit 1, is_error true and subtype "success")."""
        result = _read_json_object(transcript_path)
        if exit_code != 0 or (result is not None and result.get("is_error")):
            detail = _as_str(result.get("result")) if result is not None else None
            detail = detail or _tail_file(stderr_path) or "(no output)"
            reason = f"exited {exit_code}" if exit_code != 0 else "reported is_error"
            raise HarnessError(f"claude {reason}: {detail}", transcript_path=transcript_path)
        if result is None:
            raise HarnessError(
                f"claude exited 0 but wrote no parseable JSON result to {transcript_path}",
                transcript_path=transcript_path,
            )
        return result


class Codex:
    name = "codex"

    def version(self, env: dict[str, str]) -> str:
        """`codex --version` -> e.g. 'codex-cli 0.153.4'."""
        return _cli_version_line(self.name, env)

    def argv(
        self,
        *,
        cwd: Path,
        prompt_file: Path,
        schema_file: Path,
        last_file: Path,
        mode: Mode,
        model: str | None,
        auth: Auth,
        writable_dirs: list[str] | None,
    ) -> list[str]:
        """codex exec --json --sandbox <read-only|workspace-write> --skip-git-repo-check --output-schema <schema_file>
                      -o <last_file> [--ignore-user-config --ignore-rules] [-m model] [--add-dir D ...] -C <cwd> <sentence>
          --skip-git-repo-check: each issue gets a fresh worktree path, which codex rejects with "Not inside a trusted
              directory" unless an ancestor is trusted in $CODEX_HOME/config.toml (this workstation only passes because
              $HOME is trusted there).
          auth == "api" adds --ignore-user-config --ignore-rules: the codex analogue of claude's --bare — auth still
              resolves from CODEX_HOME (CODEX_API_KEY in env), but the host's model/MCP/plugins/hooks config and the
              repo's execpolicy .rules do not load. subscription omits them (attended, §8).
          writable_dirs (config [harness.codex].writable_dirs, "~" expanded) -> one --add-dir each, write mode only;
              lets a sandboxed builder write uv's cache so `make test` works offline inside the sandbox.
        Approvals are already off under `exec` (non-interactive, stdin=/dev/null)."""
        _require_mode(mode)
        _require_auth(auth)
        argv = [
            self.name,
            "exec",
            "--json",
            "--sandbox",
            "read-only" if mode == "read" else "workspace-write",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_file),
            "-o",
            str(last_file),
        ]
        if auth == "api":
            argv += ["--ignore-user-config", "--ignore-rules"]
        if model:
            argv += ["-m", model]
        if mode == "write":
            for directory in writable_dirs or []:
                argv += ["--add-dir", str(Path(directory).expanduser())]
        argv += ["-C", str(cwd), prompt_sentence(cwd, prompt_file)]
        return argv

    def run(
        self,
        *,
        cwd,
        prompt_file,
        schema_file,
        mode,
        model,
        auth,
        env,
        timeout_s,
        transcript_path,
        max_turns=None,
        max_budget_usd=0.0,
        agents_md=None,
        writable_dirs=None,
    ) -> HarnessResult:
        """run_streaming(...) with stdout JSONL -> transcript_path; last_file = transcript_path.with_suffix('.last.json').
        exit != 0 -> HarnessError(stderr tail); missing/invalid last_file JSON -> HarnessError. Returns the parsed JSON
        as output; session_id from the thread.started event when present; model=None.
        `max_turns`, `max_budget_usd` and `agents_md` are claude-only and ignored here (codex reads AGENTS.md itself)."""
        cli_version = self.version(env)
        _require_readable(schema_file, "codex output schema")
        last_file = transcript_path.with_suffix(".last.json")
        # never report a previous attempt's artifact as this run's output
        last_file.unlink(missing_ok=True)
        argv = self.argv(
            cwd=cwd,
            prompt_file=prompt_file,
            schema_file=schema_file,
            last_file=last_file,
            mode=mode,
            model=model,
            auth=auth,
            writable_dirs=writable_dirs,
        )
        stderr_path = _stderr_path(transcript_path)
        exit_code, duration_s = run_streaming(
            argv,
            cwd=cwd,
            env=env,
            timeout_s=timeout_s,
            stdout_path=transcript_path,
            stderr_path=stderr_path,
            what=self.name,
        )
        if exit_code != 0:
            raise HarnessError(
                f"codex exited {exit_code}: {_tail_file(stderr_path) or '(no stderr)'}",
                transcript_path=transcript_path,
            )
        return HarnessResult(
            output=_read_codex_output(last_file, transcript_path),
            transcript_path=transcript_path,
            exit_code=exit_code,
            cli_version=cli_version,
            model=None,
            session_id=_codex_thread_id(transcript_path),
            duration_s=duration_s,
        )


def get_harness(name: str) -> Harness:
    """'claude' -> ClaudeCode(), 'codex' -> Codex(); else FactoryError."""
    _require_harness(name)
    return ClaudeCode() if name == "claude" else Codex()


def which(binary: str, env: dict[str, str]) -> str | None:
    """shutil.which against env['PATH']."""
    return shutil.which(binary, path=env.get("PATH", ""))


# --- internals


def _require_harness(name: str) -> None:
    if name not in PROVIDER_KEYS:
        raise FactoryError(
            f"unknown harness {name!r}; expected one of: {', '.join(sorted(PROVIDER_KEYS))}"
        )


def _require_auth(auth: str) -> None:
    if auth not in ("api", "subscription"):
        raise FactoryError(f"unknown auth {auth!r}; expected one of: api, subscription")


def _require_mode(mode: str) -> None:
    if mode not in ("read", "write"):
        raise FactoryError(f"unknown harness mode {mode!r}; expected one of: read, write")


def _is_forwardable(name: str) -> bool:
    """A config env_passthrough name the factory will copy: never a credential, a GitHub token, a factory variable,
    or a marker that makes a harness think it is nested."""
    return (
        re.match(FORBIDDEN_KEY_PATTERN, name) is None
        and name not in STRIPPED_ALWAYS
        and name not in ALL_PROVIDER_KEYS
    )


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole group, then SIGKILL it. start_new_session=True made the child a group leader, so a harness
    that spawned `make` -> `pytest` -> a server dies with it instead of holding the worktree."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        proc.kill()
        return
    for sig, grace_s in ((signal.SIGTERM, _TERM_GRACE_S), (signal.SIGKILL, _KILL_GRACE_S)):
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass
        try:
            proc.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            continue


def _stderr_path(transcript_path: Path) -> Path:
    return transcript_path.with_name(transcript_path.name + ".stderr")


def _tail_file(path: Path, lines: int = _ERROR_TAIL_LINES) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def _read_schema(schema_file: Path) -> dict:
    _require_readable(schema_file, "output schema")
    try:
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FactoryError(f"output schema {schema_file} is not valid JSON: {exc}") from exc
    if not isinstance(schema, dict):
        raise FactoryError(f"output schema {schema_file} must be a JSON object")
    return schema


def _require_readable(path: Path, what: str) -> None:
    if not Path(path).is_file():
        raise FactoryError(f"{what} {path} does not exist")


def _read_json_object(path: Path) -> dict | None:
    """The claude transcript is exactly one JSON object (smoke 2026-09-06). None when it is anything else, so the
    caller can report the process failure that usually explains it."""
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _read_codex_output(last_file: Path, transcript_path: Path) -> dict:
    if not last_file.is_file():
        raise HarnessError(
            f"codex wrote no structured output to {last_file}; the --output-schema turn did not finish",
            transcript_path=transcript_path,
        )
    try:
        output = json.loads(last_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        raise HarnessError(
            f"codex wrote unparseable JSON to {last_file}: {exc}", transcript_path=transcript_path
        ) from exc
    if not isinstance(output, dict):
        raise HarnessError(
            f"codex structured output in {last_file} is {type(output).__name__}, expected an object",
            transcript_path=transcript_path,
        )
    return output


def _codex_thread_id(transcript_path: Path) -> str | None:
    """The `thread.started` event's thread_id — codex's equivalent of claude's session_id."""
    try:
        text = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            return _as_str(event.get("thread_id"))
    return None


def _claude_model(result: dict) -> str | None:
    """modelUsage keys minus the haiku helper models Claude Code bills for titles and summaries."""
    usage = result.get("modelUsage")
    if not isinstance(usage, dict):
        return None
    names = sorted(str(name) for name in usage if not str(name).startswith(_HELPER_MODEL_PREFIX))
    if not names:
        return None
    return ",".join(names)


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_dict_list(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _cli_version_line(binary: str, env: dict[str, str]) -> str:
    """`<binary> --version`, one short line — the one place buffering is right (the streaming rule in the module
    docstring is about session transcripts)."""
    executable = which(binary, env)
    if executable is None:
        raise FactoryError(
            f"{binary} is not on PATH; install it or fix PATH before running the factory"
        )
    try:
        proc = subprocess.run(
            [executable, "--version"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
        )
    except OSError as exc:
        raise FactoryError(f"cannot run {executable} --version: {exc.strerror or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FactoryError(
            f"{executable} --version did not finish within {_VERSION_TIMEOUT_S}s"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise FactoryError(
            f"{executable} --version exited {proc.returncode}: {detail[-1] if detail else '(no output)'}"
        )
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise FactoryError(f"{executable} --version printed nothing")
    return lines[0]
