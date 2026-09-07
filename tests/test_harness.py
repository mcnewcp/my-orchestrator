"""Unit tests for the harness adapter (design §8).

Self-contained by construction: the result parsing is asserted against the REAL transcripts captured from
claude 2.1.263 and codex 0.153.4 on this machine (tests/fixtures/real-transcripts/), and every subprocess is a
stub script written into tmp_path. No conftest fixtures, no fakes, no network, no model.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from factory import harness
from factory.errors import FactoryError, HarnessError

FIXTURES = Path(__file__).parent / "fixtures" / "real-transcripts"

# A parent environment shaped like this workstation's: nesting markers, three providers' credentials, GitHub
# tokens, factory variables and unrelated junk. build_env must copy from its allowlists, not filter this.
DIRTY_PARENT = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/dev",
    "SHELL": "/bin/bash",
    "LANG": "en_US.UTF-8",
    "TERM": "xterm-256color",
    "TMPDIR": "/tmp",
    "HTTPS_PROXY": "http://proxy.corp:3128",
    "NODE_EXTRA_CA_CERTS": "/etc/ssl/corp.pem",
    "CLAUDE_CONFIG_DIR": "/home/dev/.claude",
    "CODEX_HOME": "/home/dev/.codex",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_MESSAGING_SOCKET": "/run/user/1000/claude.sock",
    "CLAUDE_EFFORT": "high",
    "CLAUDE_PID": "4242",
    "AI_AGENT": "1",
    "ANTHROPIC_API_KEY": "sk-ant-parent",
    "ANTHROPIC_AUTH_TOKEN": "auth-token",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
    "CODEX_API_KEY": "sk-codex-parent",
    "CODEX_ACCESS_TOKEN": "codex-access",
    "OPENAI_API_KEY": "sk-openai-parent",
    "GH_TOKEN": "ghp_factory",
    "GITHUB_TOKEN": "ghp_actions",
    "FACTORY_FAKE_DIR": "/tmp/fake",
    "JAVA_HOME": "/opt/java",
    "RANDOM_JUNK": "keep-me-out",
}

# A stub CLI. It answers --version, records the invocation, optionally writes the file named by -o, then
# replays a canned stdout/stderr and exit code — everything the adapter observes about a real harness.
STUB = """\
#!/usr/bin/env python3
import json
import os
import pathlib
import sys

plan = json.loads(pathlib.Path({plan!r}).read_text())
argv = sys.argv[1:]
if "--version" in argv:
    sys.stdout.write(plan["version"] + "\\n")
    raise SystemExit(0)
pathlib.Path(plan["calls"]).write_text(
    json.dumps({{"argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ)}})
)
if plan.get("o_file") is not None:
    pathlib.Path(argv[argv.index("-o") + 1]).write_text(plan["o_file"])
sys.stdout.write(plan.get("stdout", ""))
sys.stderr.write(plan.get("stderr", ""))
raise SystemExit(plan.get("exit_code", 0))
"""


def _install_stub(
    tmp_path: Path,
    name: str,
    *,
    version: str,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    o_file: str | None = None,
) -> Path:
    """Write an executable stub `name` into tmp_path/bin; return the path it records each call to."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    calls = bin_dir / f"{name}.calls.json"
    plan = bin_dir / f"{name}.plan.json"
    plan.write_text(
        json.dumps(
            {
                "version": version,
                "calls": str(calls),
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "o_file": o_file,
            }
        )
    )
    script = bin_dir / name
    script.write_text(STUB.format(plan=str(plan)))
    script.chmod(0o755)
    return calls


def _stub_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path)}
    env.update(extra)
    return env


def _worktree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """(worktree, prompt file inside it, schema file) — the shapes stages.run_harness_stage passes."""
    worktree = tmp_path / "wt"
    prompt = worktree / "work" / "42" / "prompts" / "spec-1.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("Write the spec.\n")
    schema = tmp_path / "spec.json"
    schema.write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            }
        )
    )
    return worktree, prompt, schema


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _forbidden(env: dict[str, str], allowed: set[str] | None = None) -> list[str]:
    allowed = allowed or set()
    return sorted(k for k in env if re.match(harness.FORBIDDEN_KEY_PATTERN, k) and k not in allowed)


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _pid_gone(pid: int, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:  # pragma: no cover - pid reused by another user
            return False
        time.sleep(0.05)
    return False


# --- build_env (design §8 "Environment")


def test_build_env_copies_only_the_allowlists():
    env = harness.build_env("claude", "subscription", DIRTY_PARENT)

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/dev"
    assert env["SHELL"] == "/bin/bash"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["TMPDIR"] == "/tmp"
    assert env["HTTPS_PROXY"] == "http://proxy.corp:3128"
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/corp.pem"
    assert "JAVA_HOME" not in env
    assert "RANDOM_JUNK" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["TERM"] == "xterm-256color"


def test_build_env_subscription_forwards_no_provider_key():
    env = harness.build_env("claude", "subscription", DIRTY_PARENT)

    # Only the harness's own config-dir variable may match the forbidden pattern in subscription mode.
    assert _forbidden(env, allowed={"CLAUDE_CONFIG_DIR"}) == []
    for key in harness.ALL_PROVIDER_KEYS:
        assert key not in env
    for key in harness.STRIPPED_ALWAYS:
        assert key not in env


@pytest.mark.parametrize(
    ("name", "selected"), [("claude", "ANTHROPIC_API_KEY"), ("codex", "CODEX_API_KEY")]
)
def test_build_env_api_forwards_exactly_one_provider_key(name: str, selected: str):
    env = harness.build_env(name, "api", DIRTY_PARENT)

    assert env[selected] == DIRTY_PARENT[selected]
    assert _forbidden(env, allowed={selected, harness.CONFIG_DIR_VARS[name]}) == []
    for key in harness.ALL_PROVIDER_KEYS:
        if key != selected:
            assert key not in env


def test_build_env_api_without_the_key_fails_instead_of_falling_back():
    parent = {k: v for k, v in DIRTY_PARENT.items() if k != "ANTHROPIC_API_KEY"}

    with pytest.raises(FactoryError) as excinfo:
        harness.build_env("claude", "api", parent)

    assert str(excinfo.value) == (
        "api auth: ANTHROPIC_API_KEY is not set; export it or use --auth subscription"
    )
    # ...and the saved-login credentials that were present were never a fallback.
    assert "CLAUDE_CODE_OAUTH_TOKEN" in parent


def test_build_env_api_with_an_empty_key_is_a_missing_key():
    parent = dict(DIRTY_PARENT, CODEX_API_KEY="")

    with pytest.raises(FactoryError, match="CODEX_API_KEY is not set"):
        harness.build_env("codex", "api", parent)


def test_build_env_forwards_only_the_harness_config_dir():
    claude_env = harness.build_env("claude", "subscription", DIRTY_PARENT)
    codex_env = harness.build_env("codex", "subscription", DIRTY_PARENT)

    assert claude_env["CLAUDE_CONFIG_DIR"] == "/home/dev/.claude"
    assert "CODEX_HOME" not in claude_env
    assert codex_env["CODEX_HOME"] == "/home/dev/.codex"
    assert "CLAUDE_CONFIG_DIR" not in codex_env


def test_build_env_passthrough_forwards_toolchain_names_but_no_credential():
    env = harness.build_env(
        "claude",
        "subscription",
        DIRTY_PARENT,
        passthrough=[
            "JAVA_HOME",
            " RANDOM_JUNK ",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "OPENAI_API_KEY",
            "FACTORY_FAKE_DIR",
            "",
        ],
    )

    assert env["JAVA_HOME"] == "/opt/java"
    assert env["RANDOM_JUNK"] == "keep-me-out"
    assert _forbidden(env, allowed={"CLAUDE_CONFIG_DIR"}) == []
    for key in (*harness.ALL_PROVIDER_KEYS, *harness.STRIPPED_ALWAYS, "FACTORY_FAKE_DIR"):
        assert key not in env


def test_build_env_passthrough_cannot_reintroduce_an_unselected_provider_key():
    env = harness.build_env(
        "claude", "api", DIRTY_PARENT, passthrough=["CODEX_API_KEY", "OPENAI_API_KEY"]
    )

    assert env["ANTHROPIC_API_KEY"] == "sk-ant-parent"
    assert "CODEX_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_build_env_defaults_term_when_absent():
    parent = {k: v for k, v in DIRTY_PARENT.items() if k != "TERM"}

    assert harness.build_env("codex", "subscription", parent)["TERM"] == "dumb"


def test_build_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/dev/.cache")
    monkeypatch.setenv("GH_TOKEN", "ghp_env")

    env = harness.build_env("claude", "subscription")

    assert env["XDG_CACHE_HOME"] == "/home/dev/.cache"
    assert "GH_TOKEN" not in env


@pytest.mark.parametrize(("name", "auth"), [("gemini", "api"), ("claude", "oauth")])
def test_build_env_rejects_unknown_harness_or_auth(name: str, auth: str):
    with pytest.raises(FactoryError):
        harness.build_env(name, auth, DIRTY_PARENT)


# --- checks_env (design §8: repository-controlled test code never sees a credential)


def test_checks_env_strips_every_provider_key_and_marks_ci():
    harness_env = harness.build_env("claude", "api", DIRTY_PARENT, passthrough=["JAVA_HOME"])

    env = harness.checks_env(harness_env)

    for key in harness.ALL_PROVIDER_KEYS:
        assert key not in env
    assert env["CI"] == "1"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["JAVA_HOME"] == "/opt/java"
    assert env["CLAUDE_CONFIG_DIR"] == "/home/dev/.claude"
    assert (
        harness_env["ANTHROPIC_API_KEY"] == "sk-ant-parent"
    )  # the harness env itself is not mutated


# --- prompt sentence and version parsing


def test_prompt_sentence_is_relative_to_the_worktree():
    sentence = harness.prompt_sentence(Path("/w/wt"), Path("/w/wt/work/42/prompts/review-2.md"))

    assert sentence == "Follow the instructions in work/42/prompts/review-2.md exactly."
    assert (
        harness.PROMPT_SENTENCE.format(prompt_file="x") == "Follow the instructions in x exactly."
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2.1.263 (Claude Code)", (2, 1, 263)),
        ("codex-cli 0.153.4", (0, 153, 4)),
        ("9.9.9 (Claude Code)\n", (9, 9, 9)),
        ("codex-cli 1.0", (1, 0)),
    ],
)
def test_parse_version(text: str, expected: tuple[int, ...]):
    assert harness.parse_version(text) == expected


def test_parse_version_of_the_smoked_versions_clears_the_claude_minimum():
    assert harness.parse_version("2.1.263 (Claude Code)") >= harness.CLAUDE_MIN_VERSION
    assert harness.parse_version("2.1.258 (Claude Code)") < harness.CLAUDE_MIN_VERSION


def test_parse_version_names_the_text_it_could_not_read():
    with pytest.raises(FactoryError, match="cannot read a version number from 'not a version'"):
        harness.parse_version("not a version\n")


# --- run_streaming (the shared subprocess contract)


def test_run_streaming_streams_to_files_and_returns_the_exit_code(tmp_path):
    script = tmp_path / "noisy.sh"
    script.write_text("#!/bin/bash\necho out-line\necho err-line >&2\nexit 7\n")
    script.chmod(0o755)
    stdout_path = tmp_path / "transcripts" / "42" / "build-1.json"

    exit_code, duration_s = harness.run_streaming(
        [str(script)],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        timeout_s=30,
        stdout_path=stdout_path,
        stderr_path=Path(str(stdout_path) + ".stderr"),
        what="claude",
    )

    assert exit_code == 7
    assert duration_s >= 0.0
    assert stdout_path.read_text() == "out-line\n"
    assert Path(str(stdout_path) + ".stderr").read_text() == "err-line\n"


def test_run_streaming_gives_the_child_devnull_stdin_and_the_worktree_cwd(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    script = tmp_path / "reader.sh"
    # `cat` blocks forever on an inherited pipe; with stdin=/dev/null it sees EOF at once (smoke 2026-09-06).
    script.write_text("#!/bin/bash\npwd\ncat\n")
    script.chmod(0o755)
    stdout_path = tmp_path / "out.json"

    exit_code, _ = harness.run_streaming(
        [str(script)],
        cwd=worktree,
        env={"PATH": os.environ["PATH"]},
        timeout_s=20,
        stdout_path=stdout_path,
        stderr_path=tmp_path / "out.json.stderr",
        what="codex",
    )

    assert exit_code == 0
    assert stdout_path.read_text().strip() == str(worktree)


def test_run_streaming_timeout_kills_the_process_group_and_keeps_the_transcript(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    script = tmp_path / "sleeper.sh"
    script.write_text(
        "#!/bin/bash\n"
        "echo partial-transcript\n"
        "sleep 300 &\n"
        f'echo $! > "{child_pid_file}"\n'
        "sleep 300\n"
    )
    script.chmod(0o755)
    stdout_path = tmp_path / "transcripts" / "42" / "build-1.json"

    started = time.monotonic()
    with pytest.raises(HarnessError) as excinfo:
        harness.run_streaming(
            [str(script)],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"]},
            timeout_s=1,
            stdout_path=stdout_path,
            stderr_path=Path(str(stdout_path) + ".stderr"),
            what="claude",
        )

    assert "claude timed out after 1s" in str(excinfo.value)
    assert str(stdout_path) in str(excinfo.value)
    assert excinfo.value.transcript_path == stdout_path
    assert time.monotonic() - started < 20
    assert stdout_path.exists()
    assert "partial-transcript" in stdout_path.read_text()
    assert _pid_gone(int(child_pid_file.read_text().strip())), (
        "the grandchild outlived the killed group"
    )


def test_run_streaming_kills_the_process_group_when_the_wait_is_interrupted(tmp_path, monkeypatch):
    """Ctrl-C (or any other exception out of wait) must not orphan the session: the child leads its own
    process group, so nothing else would ever reap it or the `make`/`pytest` grandchildren it spawned."""
    child_pid_file = tmp_path / "child.pid"
    script = tmp_path / "sleeper.sh"
    script.write_text(
        "#!/bin/bash\n"
        "echo partial-transcript\n"
        "sleep 300 &\n"
        f'echo $! > "{child_pid_file}"\n'
        "sleep 300\n"
    )
    script.chmod(0o755)
    stdout_path = tmp_path / "transcripts" / "42" / "build-1.json"

    class InterruptedPopen(subprocess.Popen):
        """A real child, but the first wait() — run_streaming's own — raises the way Ctrl-C does. The waits
        inside the kill helper must still work, so only the first call is interrupted."""

        interrupted = False

        def wait(self, timeout=None):
            if not InterruptedPopen.interrupted:
                InterruptedPopen.interrupted = True
                # wait a moment first, as a real Ctrl-C would arrive mid-session with output on disk
                with contextlib.suppress(subprocess.TimeoutExpired):
                    super().wait(timeout=1)
                raise KeyboardInterrupt
            return super().wait(timeout=timeout)

    monkeypatch.setattr(harness.subprocess, "Popen", InterruptedPopen)

    with pytest.raises(KeyboardInterrupt):
        harness.run_streaming(
            [str(script)],
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"]},
            timeout_s=300,
            stdout_path=stdout_path,
            stderr_path=Path(str(stdout_path) + ".stderr"),
            what="claude",
        )

    assert InterruptedPopen.interrupted
    assert "partial-transcript" in stdout_path.read_text(), "the transcript survives an interrupt"
    assert _pid_gone(int(child_pid_file.read_text().strip())), (
        "the grandchild outlived an interrupted wait"
    )


def test_run_streaming_names_a_binary_it_cannot_execute(tmp_path):
    stdout_path = tmp_path / "out.log"

    with pytest.raises(FactoryError) as excinfo:
        harness.run_streaming(
            [str(tmp_path / "not-installed")],
            cwd=tmp_path,
            env={"PATH": ""},
            timeout_s=5,
            stdout_path=stdout_path,
            stderr_path=tmp_path / "out.log.stderr",
            what="check `make test`",
        )

    assert "check `make test`: cannot run" in str(excinfo.value)
    assert "not-installed" in str(excinfo.value)
    assert "not-installed" in (tmp_path / "out.log.stderr").read_text()


# --- ClaudeCode.argv (design §8 table)


def _claude_argv(**overrides) -> list[str]:
    kwargs = {
        "cwd": Path("/w/wt"),
        "prompt_file": Path("/w/wt/work/42/prompts/spec-1.md"),
        "schema": {"type": "object", "properties": {"markdown": {"type": "string"}}},
        "mode": "read",
        "model": None,
        "auth": "subscription",
        "max_turns": 30,
        "max_budget_usd": 0.0,
        "agents_md": None,
    }
    kwargs.update(overrides)
    return harness.ClaudeCode().argv(**kwargs)


def test_claude_argv_puts_the_sentence_first_and_never_last():
    for mode in ("read", "write"):
        for auth in ("subscription", "api"):
            argv = _claude_argv(mode=mode, auth=auth)
            assert argv[0] == "claude"
            assert argv[1] == "-p"
            assert argv[2] == "Follow the instructions in work/42/prompts/spec-1.md exactly."
            assert argv[-1] != argv[2], "a trailing sentence is eaten by the variadic tool flags"


def test_claude_argv_always_carries_the_headless_and_setting_source_flags():
    argv = _claude_argv()

    assert _value_after(argv, "--output-format") == "json"
    assert _value_after(argv, "--permission-mode") == "dontAsk"
    assert _value_after(argv, "--permission-prompts") == "none"
    assert _value_after(argv, "--setting-sources") == "user"
    assert "--strict-mcp-config" in argv
    assert list(harness.CLAUDE_ALWAYS) == ["--setting-sources", "user", "--strict-mcp-config"]


def test_claude_argv_passes_a_compact_json_schema():
    schema = {"type": "object", "properties": {"markdown": {"type": "string"}}}

    value = _value_after(_claude_argv(schema=schema), "--json-schema")

    assert json.loads(value) == schema
    assert " " not in value


def test_claude_argv_read_mode_allow_and_disallow_lists_are_single_arguments():
    argv = _claude_argv(mode="read")

    assert argv.count("--allowedTools") == 1
    assert argv.count("--disallowedTools") == 1
    assert _value_after(argv, "--allowedTools") == "Read,Grep,Glob"
    assert (
        _value_after(argv, "--disallowedTools") == "Edit,Write,NotebookEdit,Bash,WebFetch,WebSearch"
    )


def test_claude_argv_write_mode_allows_the_build_tools_and_disallows_nothing():
    argv = _claude_argv(mode="write")

    assert argv.count("--allowedTools") == 1
    assert _value_after(argv, "--allowedTools") == harness.CLAUDE_WRITE_ALLOWED
    assert "Bash(make *)" in _value_after(argv, "--allowedTools")
    assert "--disallowedTools" not in argv


def test_claude_argv_bare_and_agents_md_only_in_api_mode(tmp_path):
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# conventions\n")

    api = _claude_argv(auth="api", agents_md=agents_md)
    subscription = _claude_argv(auth="subscription", agents_md=agents_md)

    assert "--bare" in api
    assert _value_after(api, "--append-system-prompt-file") == str(agents_md)
    assert "--bare" not in subscription
    assert "--append-system-prompt-file" not in subscription


def test_claude_argv_skips_append_system_prompt_when_there_is_no_agents_md(tmp_path):
    argv = _claude_argv(auth="api", agents_md=tmp_path / "AGENTS.md")

    assert "--bare" in argv
    assert "--append-system-prompt-file" not in argv


def test_claude_argv_runaway_bounds_and_model_are_optional():
    assert "--max-turns" not in _claude_argv(max_turns=None)
    assert _value_after(_claude_argv(max_turns=120), "--max-turns") == "120"
    assert "--max-budget-usd" not in _claude_argv(max_budget_usd=0.0)
    assert _value_after(_claude_argv(max_budget_usd=2.5), "--max-budget-usd") == "2.5"
    assert "--model" not in _claude_argv(model=None)
    assert "--model" not in _claude_argv(model="")
    assert _value_after(_claude_argv(model="opus"), "--model") == "opus"


def test_claude_argv_rejects_an_unknown_mode():
    with pytest.raises(FactoryError, match="unknown harness mode"):
        _claude_argv(mode="edit")


# --- Codex.argv (design §8 table + deviations.md flag choices)


def _codex_argv(**overrides) -> list[str]:
    kwargs = {
        "cwd": Path("/w/wt"),
        "prompt_file": Path("/w/wt/work/42/prompts/build-1.md"),
        "schema_file": Path("/pkg/schemas/build.json"),
        "last_file": Path("/w/.factory/transcripts/42/build-1.last.json"),
        "mode": "write",
        "model": None,
        "auth": "subscription",
        "writable_dirs": None,
    }
    kwargs.update(overrides)
    return harness.Codex().argv(**kwargs)


def test_codex_argv_shape_and_sentence_position():
    argv = _codex_argv()

    assert argv[:4] == ["codex", "exec", "--json", "--sandbox"]
    assert argv[-3:] == [
        "-C",
        "/w/wt",
        "Follow the instructions in work/42/prompts/build-1.md exactly.",
    ]
    assert _value_after(argv, "--output-schema") == "/pkg/schemas/build.json"
    assert _value_after(argv, "-o") == "/w/.factory/transcripts/42/build-1.last.json"


def test_codex_argv_always_skips_the_git_repo_check():
    for mode in ("read", "write"):
        for auth in ("subscription", "api"):
            assert "--skip-git-repo-check" in _codex_argv(mode=mode, auth=auth)


def test_codex_argv_sandbox_follows_the_mode():
    assert _value_after(_codex_argv(mode="read"), "--sandbox") == "read-only"
    assert _value_after(_codex_argv(mode="write"), "--sandbox") == "workspace-write"


def test_codex_argv_ignores_host_and_repo_config_only_in_api_mode():
    api = _codex_argv(auth="api")
    subscription = _codex_argv(auth="subscription")

    assert "--ignore-user-config" in api
    assert "--ignore-rules" in api
    assert "--ignore-user-config" not in subscription
    assert "--ignore-rules" not in subscription


def test_codex_argv_model_flag_is_dash_m():
    assert "-m" not in _codex_argv(model=None)
    assert _value_after(_codex_argv(model="gpt-5-codex"), "-m") == "gpt-5-codex"


def test_codex_argv_add_dir_per_writable_dir_with_tilde_expanded():
    argv = _codex_argv(mode="write", writable_dirs=["~/.cache/uv", "/srv/wheels"])

    values = [argv[i + 1] for i, item in enumerate(argv) if item == "--add-dir"]
    assert values == [str(Path.home() / ".cache/uv"), "/srv/wheels"]
    assert "~" not in " ".join(argv)


def test_codex_argv_read_mode_gets_no_add_dir():
    assert "--add-dir" not in _codex_argv(mode="read", writable_dirs=["~/.cache/uv"])


# --- ClaudeCode.run, parsed against the real transcripts


def test_claude_run_parses_the_real_read_result(tmp_path):
    calls = _install_stub(
        tmp_path,
        "claude",
        version="2.1.263 (Claude Code)",
        stdout=_fixture("claude-read-result.json"),
    )
    worktree, prompt, schema = _worktree(tmp_path)
    transcript = tmp_path / "transcripts" / "42" / "spec-1-20260906T181500Z.json"

    result = harness.ClaudeCode().run(
        cwd=worktree,
        prompt_file=prompt,
        schema_file=schema,
        mode="read",
        model=None,
        auth="subscription",
        env=_stub_env(tmp_path),
        timeout_s=30,
        transcript_path=transcript,
        max_turns=30,
    )

    assert result.output == {"ok": True, "first_line": "hello smoke"}
    assert result.model == "claude-fable-5-1", (
        "the claude-haiku-* helper model is not the model that worked"
    )
    assert result.session_id == "eee98d4d-1594-4132-8f71-a1b596db4ffa"
    assert result.num_turns == 4
    assert result.permission_denials == []
    assert result.cli_version == "2.1.263 (Claude Code)"
    assert result.exit_code == 0
    assert result.transcript_path == transcript
    assert transcript.read_text() == _fixture("claude-read-result.json")

    recorded = json.loads(calls.read_text())
    assert Path(recorded["cwd"]).resolve() == worktree.resolve()
    assert recorded["argv"][2] == "Follow the instructions in work/42/prompts/spec-1.md exactly."
    assert _value_after(recorded["argv"], "--max-turns") == "30"
    assert json.loads(_value_after(recorded["argv"], "--json-schema")) == json.loads(
        schema.read_text()
    )


def test_claude_run_parses_the_real_write_result(tmp_path):
    _install_stub(
        tmp_path,
        "claude",
        version="2.1.263 (Claude Code)",
        stdout=_fixture("claude-write-result.json"),
    )
    worktree, prompt, schema = _worktree(tmp_path)

    result = harness.ClaudeCode().run(
        cwd=worktree,
        prompt_file=prompt,
        schema_file=schema,
        mode="write",
        model=None,
        auth="subscription",
        env=_stub_env(tmp_path),
        timeout_s=30,
        transcript_path=tmp_path / "transcripts" / "build-1.json",
        max_turns=120,
    )

    assert result.output["summary"].startswith("Created probe.txt")
    assert result.model == "claude-fable-5-1"
    assert result.num_turns == 5


def test_claude_run_reports_the_api_auth_failure_from_the_real_bare_transcript(tmp_path):
    _install_stub(
        tmp_path,
        "claude",
        version="2.1.263 (Claude Code)",
        stdout=_fixture("claude-bare-nokey-result.json"),
        exit_code=1,
    )
    worktree, prompt, schema = _worktree(tmp_path)
    transcript = tmp_path / "transcripts" / "spec-1.json"

    with pytest.raises(HarnessError) as excinfo:
        harness.ClaudeCode().run(
            cwd=worktree,
            prompt_file=prompt,
            schema_file=schema,
            mode="read",
            model=None,
            auth="api",
            env=_stub_env(tmp_path, ANTHROPIC_API_KEY="sk-stub"),
            timeout_s=30,
            transcript_path=transcript,
        )

    # is_error is read before subtype: this transcript reports subtype "success".
    assert json.loads(_fixture("claude-bare-nokey-result.json"))["subtype"] == "success"
    assert str(excinfo.value) == "claude exited 1: Not logged in · Please run /login"
    assert excinfo.value.transcript_path == transcript


def test_claude_run_treats_is_error_on_a_zero_exit_as_a_failure(tmp_path):
    _install_stub(
        tmp_path,
        "claude",
        version="2.1.263 (Claude Code)",
        exit_code=0,
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "rate limit reached",
            }
        ),
    )
    worktree, prompt, schema = _worktree(tmp_path)

    with pytest.raises(HarnessError, match="claude reported is_error: rate limit reached"):
        harness.ClaudeCode().run(
            cwd=worktree,
            prompt_file=prompt,
            schema_file=schema,
            mode="read",
            model=None,
            auth="subscription",
            env=_stub_env(tmp_path),
            timeout_s=30,
            transcript_path=tmp_path / "t" / "spec-1.json",
        )


def test_claude_run_requires_structured_output(tmp_path):
    _install_stub(
        tmp_path,
        "claude",
        version="2.1.263 (Claude Code)",
        stdout=json.dumps(
            {
                "type": "result",
                "is_error": False,
                "num_turns": 2,
                "result": "I could not produce the JSON.",
            }
        ),
    )
    worktree, prompt, schema = _worktree(tmp_path)
    transcript = tmp_path / "t" / "spec-1.json"

    with pytest.raises(HarnessError) as excinfo:
        harness.ClaudeCode().run(
            cwd=worktree,
            prompt_file=prompt,
            schema_file=schema,
            mode="read",
            model=None,
            auth="subscription",
            env=_stub_env(tmp_path),
            timeout_s=30,
            transcript_path=transcript,
        )

    assert "no structured_output" in str(excinfo.value)
    assert excinfo.value.transcript_path == transcript


def test_claude_run_reports_unparseable_output_and_the_stderr_tail(tmp_path):
    _install_stub(tmp_path, "claude", version="2.1.263 (Claude Code)", stdout="not json at all\n")
    worktree, prompt, schema = _worktree(tmp_path)

    with pytest.raises(HarnessError, match="no parseable JSON result"):
        harness.ClaudeCode().run(
            cwd=worktree,
            prompt_file=prompt,
            schema_file=schema,
            mode="read",
            model=None,
            auth="subscription",
            env=_stub_env(tmp_path),
            timeout_s=30,
            transcript_path=tmp_path / "t" / "spec-1.json",
        )


def test_claude_run_falls_back_to_stderr_when_the_crash_left_no_json(tmp_path):
    _install_stub(
        tmp_path,
        "claude",
        version="2.1.263 (Claude Code)",
        stdout="",
        stderr="node: out of memory\n",
        exit_code=134,
    )
    worktree, prompt, schema = _worktree(tmp_path)

    with pytest.raises(HarnessError, match="claude exited 134: node: out of memory"):
        harness.ClaudeCode().run(
            cwd=worktree,
            prompt_file=prompt,
            schema_file=schema,
            mode="read",
            model=None,
            auth="subscription",
            env=_stub_env(tmp_path),
            timeout_s=30,
            transcript_path=tmp_path / "t" / "spec-1.json",
        )


def test_claude_run_passes_only_the_allowlisted_environment(tmp_path):
    calls = _install_stub(
        tmp_path,
        "claude",
        version="2.1.263 (Claude Code)",
        stdout=_fixture("claude-read-result.json"),
    )
    worktree, prompt, schema = _worktree(tmp_path)
    parent = dict(
        DIRTY_PARENT, PATH=f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}", HOME=str(tmp_path)
    )
    env = harness.build_env("claude", "subscription", parent)

    harness.ClaudeCode().run(
        cwd=worktree,
        prompt_file=prompt,
        schema_file=schema,
        mode="read",
        model=None,
        auth="subscription",
        env=env,
        timeout_s=30,
        transcript_path=tmp_path / "t" / "spec-1.json",
    )

    child_env = json.loads(calls.read_text())["env"]
    assert child_env["GIT_TERMINAL_PROMPT"] == "0"
    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDECODE",
        "AI_AGENT",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "FACTORY_FAKE_DIR",
    ):
        assert key not in child_env


def test_claude_run_rejects_a_missing_schema_file(tmp_path):
    _install_stub(tmp_path, "claude", version="2.1.263 (Claude Code)")
    worktree, prompt, _ = _worktree(tmp_path)

    with pytest.raises(FactoryError, match="output schema .* does not exist"):
        harness.ClaudeCode().run(
            cwd=worktree,
            prompt_file=prompt,
            schema_file=tmp_path / "absent.json",
            mode="read",
            model=None,
            auth="subscription",
            env=_stub_env(tmp_path),
            timeout_s=30,
            transcript_path=tmp_path / "t" / "spec-1.json",
        )


def test_claude_version_requires_the_binary(tmp_path):
    with pytest.raises(FactoryError, match="claude is not on PATH"):
        harness.ClaudeCode().version({"PATH": str(tmp_path)})


# --- Codex.run, parsed against the real transcripts


def test_codex_run_parses_the_real_read_events_and_last_json(tmp_path):
    calls = _install_stub(
        tmp_path,
        "codex",
        version="codex-cli 0.153.4",
        stdout=_fixture("codex-read-events.jsonl"),
        o_file=_fixture("codex-read-last.json"),
    )
    worktree, prompt, schema = _worktree(tmp_path)
    transcript = tmp_path / "transcripts" / "42" / "spec-1-20260906T181500Z.json"

    result = harness.Codex().run(
        cwd=worktree,
        prompt_file=prompt,
        schema_file=schema,
        mode="read",
        model=None,
        auth="subscription",
        env=_stub_env(tmp_path),
        timeout_s=30,
        transcript_path=transcript,
    )

    assert result.output == {"ok": True, "first_line": "hello codex smoke"}
    assert result.session_id == "01a078e9-763c-7b20-aec5-1c0784378f47"
    assert result.model is None
    assert result.cli_version == "codex-cli 0.153.4"
    assert result.exit_code == 0
    last_file = transcript.with_suffix(".last.json")
    assert last_file.exists(), "-o must land next to the transcript"
    assert _value_after(json.loads(calls.read_text())["argv"], "-o") == str(last_file)


def test_codex_run_parses_the_real_write_last_json(tmp_path):
    _install_stub(
        tmp_path,
        "codex",
        version="codex-cli 0.153.4",
        stdout=_fixture("codex-write-events.jsonl"),
        o_file=_fixture("codex-write-last.json"),
    )
    worktree, prompt, schema = _worktree(tmp_path)

    result = harness.Codex().run(
        cwd=worktree,
        prompt_file=prompt,
        schema_file=schema,
        mode="write",
        model=None,
        auth="subscription",
        env=_stub_env(tmp_path),
        timeout_s=30,
        transcript_path=tmp_path / "t" / "build-1.json",
        writable_dirs=["~/.cache/uv"],
    )

    assert result.output["summary"].startswith("Created probe.txt")
    assert result.session_id == "01a078ed-4a77-7891-a5d8-ddd996c07ebf"


def test_codex_run_reports_a_non_zero_exit_with_the_stderr_tail(tmp_path):
    _install_stub(
        tmp_path,
        "codex",
        version="codex-cli 0.153.4",
        exit_code=2,
        stdout='{"type":"thread.started","thread_id":"t1"}\n',
        stderr="stream error: 429 Too Many Requests\n",
    )
    worktree, prompt, schema = _worktree(tmp_path)
    transcript = tmp_path / "t" / "spec-1.json"

    with pytest.raises(HarnessError) as excinfo:
        harness.Codex().run(
            cwd=worktree,
            prompt_file=prompt,
            schema_file=schema,
            mode="read",
            model=None,
            auth="subscription",
            env=_stub_env(tmp_path),
            timeout_s=30,
            transcript_path=transcript,
        )

    assert str(excinfo.value) == "codex exited 2: stream error: 429 Too Many Requests"
    assert excinfo.value.transcript_path == transcript


def test_codex_run_requires_the_output_file(tmp_path):
    _install_stub(
        tmp_path, "codex", version="codex-cli 0.153.4", stdout=_fixture("codex-read-events.jsonl")
    )
    worktree, prompt, schema = _worktree(tmp_path)

    with pytest.raises(HarnessError, match="codex wrote no structured output"):
        harness.Codex().run(
            cwd=worktree,
            prompt_file=prompt,
            schema_file=schema,
            mode="read",
            model=None,
            auth="subscription",
            env=_stub_env(tmp_path),
            timeout_s=30,
            transcript_path=tmp_path / "t" / "spec-1.json",
        )


def test_codex_run_rejects_unparseable_output(tmp_path):
    _install_stub(
        tmp_path,
        "codex",
        version="codex-cli 0.153.4",
        stdout=_fixture("codex-read-events.jsonl"),
        o_file="{not json",
    )
    worktree, prompt, schema = _worktree(tmp_path)

    with pytest.raises(HarnessError, match="unparseable JSON"):
        harness.Codex().run(
            cwd=worktree,
            prompt_file=prompt,
            schema_file=schema,
            mode="read",
            model=None,
            auth="subscription",
            env=_stub_env(tmp_path),
            timeout_s=30,
            transcript_path=tmp_path / "t" / "spec-1.json",
        )


def test_codex_run_never_reports_a_previous_attempts_output(tmp_path):
    _install_stub(
        tmp_path, "codex", version="codex-cli 0.153.4", stdout=_fixture("codex-read-events.jsonl")
    )
    worktree, prompt, schema = _worktree(tmp_path)
    transcript = tmp_path / "t" / "spec-1.json"
    transcript.parent.mkdir(parents=True)
    transcript.with_suffix(".last.json").write_text('{"ok": true, "first_line": "stale"}')

    with pytest.raises(HarnessError, match="codex wrote no structured output"):
        harness.Codex().run(
            cwd=worktree,
            prompt_file=prompt,
            schema_file=schema,
            mode="read",
            model=None,
            auth="subscription",
            env=_stub_env(tmp_path),
            timeout_s=30,
            transcript_path=transcript,
        )


# --- lookup helpers


def test_get_harness_returns_the_two_adapters():
    assert isinstance(harness.get_harness("claude"), harness.ClaudeCode)
    assert isinstance(harness.get_harness("codex"), harness.Codex)
    assert harness.get_harness("claude").name == "claude"
    assert harness.get_harness("codex").name == "codex"


def test_get_harness_rejects_anything_else():
    with pytest.raises(FactoryError, match="unknown harness 'gemini'"):
        harness.get_harness("gemini")


def test_which_searches_only_the_given_env_path(tmp_path):
    _install_stub(tmp_path, "claude", version="2.1.263 (Claude Code)")

    assert harness.which("claude", {"PATH": str(tmp_path / "bin")}) == str(
        tmp_path / "bin" / "claude"
    )
    assert harness.which("claude", {"PATH": str(tmp_path)}) is None
    assert harness.which("claude", {}) is None
