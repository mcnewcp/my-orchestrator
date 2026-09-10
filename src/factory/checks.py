"""Deterministic edit gates and check execution with a restricted environment."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess


PROTECTED_PATHS = (
    "Makefile", "factory.toml", "AGENTS.md", "CLAUDE.md", "REVIEW.md",
    ".devcontainer/", ".claude/", ".mcp.json", ".codex/", ".github/",
)
_ENV = {"PATH", "HOME", "LANG", "LANGUAGE", "TMPDIR", "TMP", "TEMP"}
_CONFIG_DIR = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
_API_KEY = {"claude": "ANTHROPIC_API_KEY", "codex": "CODEX_API_KEY"}


def filtered_env(harness: str | None = None, auth: str = "subscription",
                 source: dict | None = None) -> dict:
    """Pass only documented runtime variables and the selected explicit API key."""
    if harness is not None and harness not in _API_KEY:
        raise ValueError(f"unknown harness: {harness}")
    if auth not in ("api", "subscription"):
        raise ValueError(f"unknown auth mode: {auth}")
    source = os.environ if source is None else source
    allowed = _ENV | ({_CONFIG_DIR[harness]} if harness else set())
    env = {key: value for key, value in source.items()
           if key in allowed or re.fullmatch(r"LC_[A-Z_]+", key)}
    if harness is not None and auth == "api":
        key = _API_KEY[harness]
        if not isinstance(source.get(key), str) or not source[key].strip():
            raise ValueError(f"auth=api requires {key}")
        env[key] = source[key]
    return env


def changed_paths(cwd: Path | str) -> list[str]:
    """Include both sides of renames and all tracked and untracked changes."""
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise ValueError("git status failed: " + os.fsdecode(result.stderr).strip())
    entries = result.stdout.split(b"\0")
    paths = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4 or entry[2:3] != b" ":
            raise ValueError("invalid git status output")
        paths.add(os.fsdecode(entry[3:]))
        if b"R" in entry[:2] or b"C" in entry[:2]:
            if index >= len(entries) or not entries[index]:
                raise ValueError("incomplete git rename status")
            paths.add(os.fsdecode(entries[index]))
            index += 1
    return sorted(paths)


def _path(value: str, *, directory: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"invalid repository path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise ValueError(f"invalid repository path: {value!r}")
    return str(path) + ("/" if directory and value.endswith("/") else "")


def _matches(path: str, rule: str) -> bool:
    return path == rule.rstrip("/") or (rule.endswith("/") and path.startswith(rule))


def _planned_paths(plan: str) -> list[str]:
    section = re.search(r"(?m)^## Files that change\s*$", plan)
    if section is None:
        raise ValueError("plan must contain ## Files that change")
    contents = plan[section.end():]
    next_section = re.search(r"(?m)^#{1,2}\s+", contents)
    if next_section:
        contents = contents[:next_section.start()]
    return [_path(value, directory=True) for value in re.findall(r"`([^`\n]+)`", contents)]


def validate_edits(paths: list[str], stage: str, issue: int, config: dict,
                   plan: str = "") -> None:
    if stage not in ("build", "fix"):
        raise ValueError(f"allowed-edit rules require build or fix, got {stage}")
    settings = config.get("factory", config)
    protected = [_path(path, directory=True)
                 for path in (*PROTECTED_PATHS, *settings.get("protected_paths", []))]
    tests = [_path(path, directory=True) for path in settings.get("test_paths", ["tests/"])]
    normalized = [_path(path) for path in paths]
    violations = [path for path in normalized
                  if any(_matches(path, rule) for rule in protected)
                  or (_matches(path, "work/")
                      and not (stage == "build" and path == f"work/{issue}/plan.md"))
                  or (stage == "fix" and any(_matches(path, rule) for rule in tests))]
    if violations:
        raise ValueError(f"{stage} modified forbidden paths: " + ", ".join(sorted(set(violations))))
    if stage == "build":
        planned = _planned_paths(plan)
        unplanned = [path for path in normalized
                     if not any(_matches(path, rule) for rule in planned)]
        if unplanned:
            raise ValueError("build modified paths absent from plan: " + ", ".join(sorted(set(unplanned))))


def run_checks(cwd: Path | str, commands: list[list[str]], log_path: Path | str,
               timeout_s: int) -> bool:
    """Run argv commands sequentially; preserve combined output and stop on failure."""
    if not isinstance(commands, list) or any(
        not isinstance(command, list) or not command
        or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
        or not command[0] for command in commands
    ):
        raise ValueError("checks must be a list of nonempty argv arrays")
    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or timeout_s <= 0:
        raise ValueError("check timeout must be positive")
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        for command in commands:
            log.write("$ " + shlex.join(command) + "\n")
            log.flush()
            try:
                process = subprocess.Popen(command, cwd=cwd, env=filtered_env(),
                                           stdout=log, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
                try:
                    code = process.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    _stop_check(process)
                    log.write(f"\nTIMEOUT after {timeout_s}s\n")
                    return False
                except BaseException:
                    _stop_check(process)
                    raise
            except OSError as exc:
                log.write(f"\nFAILED: {exc}\n")
                return False
            log.write(f"\nexit {code}\n")
            log.flush()
            if code:
                return False
    return True


def _stop_check(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()
