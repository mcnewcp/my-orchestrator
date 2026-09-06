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

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from .errors import FactoryError, HarnessError

Mode = Literal["read", "write"]
Auth = Literal["subscription", "api"]

PROMPT_SENTENCE = "Follow the instructions in {prompt_file} exactly."

PROVIDER_KEYS: dict[str, str] = {"claude": "ANTHROPIC_API_KEY", "codex": "CODEX_API_KEY"}
# Every credential either CLI honours. codex 0.153.4 accepts OPENAI_API_KEY, CODEX_API_KEY and CODEX_ACCESS_TOKEN and
# warns "multiple auth env vars are present" when more than one is set; claude honours ANTHROPIC_API_KEY,
# ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN. None of these is ever forwarded except the single selected key.
ALL_PROVIDER_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY",
)
CONFIG_DIR_VARS: dict[str, str] = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH", "HOME", "SHELL", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP", "TERM", "USER", "LOGNAME",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
)
NETWORK_ALLOWLIST: tuple[str, ...] = (  # both binaries read these; without them egress breaks behind a proxy/MITM CA
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
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


@dataclass
class HarnessResult:
    output: dict
    transcript_path: Path
    exit_code: int
    cli_version: str
    model: str | None = None  # actual model when the CLI reports it (claude: modelUsage keys minus haiku helpers; codex: None)
    session_id: str | None = None
    duration_s: float = 0.0
    permission_denials: list[dict] = field(default_factory=list)  # claude: result["permission_denials"]; logged, never a gate
    num_turns: int = 0  # claude: result["num_turns"]; == max_turns is logged as "hit the turn bound"


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


def build_env(harness: str, auth: Auth, parent: dict[str, str] | None = None, *,
              passthrough: list[str] | None = None) -> dict[str, str]:
    """Allowlisted environment for a harness subprocess (design §8 "Environment").

    Constructed from the allowlists ONLY (never by copying `parent` by pattern): ENV_ALLOWLIST + NETWORK_ALLOWLIST +
    CONFIG_DIR_VARS[harness] + config.env_passthrough names (`passthrough`), each copied when present in parent
    (parent defaults to os.environ). Then:
      auth == "api":          add PROVIDER_KEYS[harness] from parent, or FactoryError("api auth: <KEY> is not set; export
                              it or use --auth subscription") — never a silent switch to a saved login.
      auth == "subscription": no provider key at all.
    Postcondition (tested): no key matches FORBIDDEN_KEY_PATTERN except the single selected key; STRIPPED_ALWAYS and
    every ALL_PROVIDER_KEYS entry other than the selected one are absent even if named in `passthrough`. This
    workstation's environment carries CLAUDECODE, AI_AGENT, CLAUDE_EFFORT, CLAUDE_PID and CLAUDE_CODE_MESSAGING_*; a
    nested Claude Code session must not be detected and must not be able to message its parent.
    Always sets GIT_TERMINAL_PROMPT=0 and, when TERM is absent, TERM=dumb.
    """
    raise NotImplementedError


def checks_env(harness_env: dict[str, str]) -> dict[str, str]:
    """Design §8: check commands get the same (allowlisted) environment minus ALL_PROVIDER_KEYS, so repository-controlled
    test code never sees a credential. Repos whose toolchain needs more (JAVA_HOME, GOPATH, ...) list the names in
    config.env_passthrough, which reaches both the harness and the checks. Also sets CI=1."""
    raise NotImplementedError


def prompt_sentence(cwd: Path, prompt_file: Path) -> str:
    """PROMPT_SENTENCE with prompt_file rendered relative to cwd (posix)."""
    raise NotImplementedError


def parse_version(text: str) -> tuple[int, ...]:
    """'2.1.263 (Claude Code)' -> (2,1,263); 'codex-cli 0.153.4' -> (0,153,4)."""
    raise NotImplementedError


def run_streaming(argv: list[str], *, cwd: Path, env: dict[str, str], timeout_s: int, stdout_path: Path,
                  stderr_path: Path, what: str) -> tuple[int, float]:
    """The shared subprocess contract from the module docstring. Returns (exit_code, duration_s); raises HarnessError on
    timeout (after killing the process group) with transcript_path=stdout_path. Also used by checks.run_checks."""
    raise NotImplementedError


class ClaudeCode:
    name = "claude"

    def version(self, env: dict[str, str]) -> str:
        """`claude --version` -> e.g. '2.1.263 (Claude Code)'. FactoryError if the binary is missing."""
        raise NotImplementedError

    def argv(self, *, cwd: Path, prompt_file: Path, schema: dict, mode: Mode, model: str | None, auth: Auth,
             max_turns: int | None, max_budget_usd: float, agents_md: Path | None) -> list[str]:
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
        raise NotImplementedError

    def run(self, *, cwd, prompt_file, schema_file, mode, model, auth, env, timeout_s, transcript_path,
            max_turns=None, max_budget_usd=0.0, agents_md=None, writable_dirs=None) -> HarnessResult:
        """run_streaming(...) then parse transcript_path as one JSON object.
        exit != 0 or is_error -> HarnessError(result text or stderr tail, transcript_path); missing/invalid JSON ->
        HarnessError; missing structured_output -> HarnessError. Returns HarnessResult(output=structured_output,
        model=<the non-haiku modelUsage key if any>, session_id, permission_denials, num_turns, cli_version)."""
        raise NotImplementedError


class Codex:
    name = "codex"

    def version(self, env: dict[str, str]) -> str:
        """`codex --version` -> e.g. 'codex-cli 0.153.4'."""
        raise NotImplementedError

    def argv(self, *, cwd: Path, prompt_file: Path, schema_file: Path, last_file: Path, mode: Mode,
             model: str | None, auth: Auth, writable_dirs: list[str] | None) -> list[str]:
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
        raise NotImplementedError

    def run(self, *, cwd, prompt_file, schema_file, mode, model, auth, env, timeout_s, transcript_path,
            max_turns=None, max_budget_usd=0.0, agents_md=None, writable_dirs=None) -> HarnessResult:
        """run_streaming(...) with stdout JSONL -> transcript_path; last_file = transcript_path.with_suffix('.last.json').
        exit != 0 -> HarnessError(stderr tail); missing/invalid last_file JSON -> HarnessError. Returns the parsed JSON
        as output; session_id from the thread.started event when present; model=None."""
        raise NotImplementedError


def get_harness(name: str) -> Harness:
    """'claude' -> ClaudeCode(), 'codex' -> Codex(); else FactoryError."""
    raise NotImplementedError


def which(binary: str, env: dict[str, str]) -> str | None:
    """shutil.which against env['PATH']."""
    raise NotImplementedError


_ = (os, subprocess, FactoryError, HarnessError)
