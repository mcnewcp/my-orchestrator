"""Harness adapters (claude, codex) plus the skill-linking setup they depend on."""

from __future__ import annotations

import json
import re
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen
from typing import Any, Protocol

from . import SKILL_NAMES, note, repo_root, warn
from .config import Config, ConfigError

SEQ_RE = re.compile(r"^(\d{3})-")


class SetupError(RuntimeError):
    """The target repo cannot be prepared for a run (skill links, ignore files)."""


class Runner(Protocol):
    """Launches one fresh harness session for a role and returns its exit code."""

    def run(self, role: str, issue: int, cwd: Path) -> int:
        """Invoke `role`'s skill with `issue` as its sole argument, with cwd = target repo."""


# ------------------------------------------------------------------ target setup


def ensure_scratch_ignored(target: Path) -> None:
    """Ensure `.scratch/` is a line in the target repo's .gitignore (created if absent)."""
    path = target / ".gitignore"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if ".scratch/" in [line.strip() for line in lines]:
        return
    text = "" if not lines else "\n".join(lines) + "\n"
    path.write_text(f"{text}.scratch/\n", encoding="utf-8")
    note(f"added .scratch/ to {path}")


def _ensure_symlink(link: Path, source: Path) -> bool:
    if link.is_symlink():
        if link.resolve() == source.resolve():  # relative link targets resolve against `link`
            return False
        raise SetupError(f"{link} is a symlink but does not point at {source}")
    if link.exists():
        raise SetupError(f"{link} already exists and is not a symlink to {source}")
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SetupError(f"{link.parent} is not a directory and cannot be created: {exc}") from exc
    link.symlink_to(source)
    return True


def _ensure_excluded(target: Path, rel_paths: list[str]) -> None:
    git_dir = target / ".git"
    if not git_dir.is_dir():
        warn(f"{git_dir} is not a directory; skipping .git/info/exclude entries")
        return
    exclude = git_dir / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    have = {line.strip() for line in existing.splitlines()}
    missing = [p for p in rel_paths if p not in have]
    if not missing:
        return
    prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
    exclude.write_text(prefix + "\n".join(missing) + "\n", encoding="utf-8")


def link_skills(target: Path, orchestrator: Path | None = None) -> list[Path]:
    """Symlink the six role skills into the target repo for both harnesses; return new links."""
    orchestrator = (orchestrator or repo_root()).resolve()
    target = target.resolve()
    if target == orchestrator:
        return []

    sources = {name: orchestrator / ".agents" / "skills" / name for name in SKILL_NAMES.values()}
    missing = [str(p) for p in sources.values() if not p.is_dir()]
    if missing:
        raise SetupError(
            "role skills are missing from the orchestrator repo: "
            + ", ".join(missing)
            + " — create them before running orch"
        )

    created: list[Path] = []
    rel_paths: list[str] = []
    for name, source in sources.items():
        for root in (".agents/skills", ".claude/skills"):
            link = target / root / name
            rel_paths.append(f"{root}/{name}")
            if _ensure_symlink(link, source):
                created.append(link)
    _ensure_excluded(target, rel_paths)
    if created:
        note(f"linked {len(created)} role skill path(s) into {target}")
    return created


def ensure_target_setup(target: Path, orchestrator: Path | None = None) -> list[Path]:
    """Prepare the target repo: .scratch/ ignored and the six role skills linked in."""
    ensure_scratch_ignored(target)
    return link_skills(target, orchestrator)


# ----------------------------------------------------------------------- runners


def _truncate(text: str, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + " ..."


def _next_seq(logs: Path) -> int:
    seen = [int(m.group(1)) for p in logs.iterdir() if (m := SEQ_RE.match(p.name))]
    return max(seen, default=0) + 1


class BaseRunner:
    """Shared plumbing: log files, streaming, and the invocation itself."""

    name = "base"

    def __init__(self, settings: dict[str, Any] | None = None):
        self.settings = settings or {}
        self.last_log_path: Path | None = None
        self.last_message: str = ""

    def command(self, skill: str, issue: int, stem: str) -> list[str]:
        """Build the harness command line."""
        raise NotImplementedError

    def on_line(self, skill: str, event: dict[str, Any]) -> None:
        """Handle one parsed stdout event."""

    def on_finish(self, skill: str, stem: str, scratch: Path) -> None:
        """Report the session's outcome after the stream ends."""

    def run(self, role: str, issue: int, cwd: Path) -> int:
        """Run the role's skill in a fresh harness session; return the process exit code."""
        skill = SKILL_NAMES[role]
        scratch = cwd / ".scratch" / str(issue)
        logs = scratch / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        stem = f"{_next_seq(logs):03d}-{role}"
        log_path = logs / f"{stem}.jsonl"
        err_path = logs / f"{stem}.err"
        self.last_log_path = log_path

        argv = self.command(skill, issue, stem)
        note(f"{self.name}: {skill} {issue} -> {log_path.relative_to(cwd)}")
        with err_path.open("w", encoding="utf-8") as err, log_path.open(
            "w", encoding="utf-8"
        ) as log:
            proc = Popen(
                argv,
                cwd=str(cwd),
                stdin=DEVNULL,
                stdout=PIPE,
                stderr=err,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                log.flush()
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self.on_line(skill, event)
            code = proc.wait()
        self.on_finish(skill, stem, scratch)
        if code != 0:
            warn(f"{self.name} exited {code} (see {err_path})")
        return code


class ClaudeRunner(BaseRunner):
    """Runs a role skill with `claude -p` and stream-json output."""

    name = "claude"

    def __init__(self, settings: dict[str, Any] | None = None):
        super().__init__(settings)
        self._saw_result = False

    def command(self, skill: str, issue: int, stem: str) -> list[str]:
        """Build the `claude -p` command line (conventions.md section 3)."""
        argv = [
            "claude",
            "-p",
            f"/{skill} {issue}",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "--setting-sources",
            "project",
        ]
        model = self.settings.get("model") or ""
        if model:
            argv += ["--model", model]
        return argv + list(self.settings.get("extra_args") or [])

    def on_line(self, skill: str, event: dict[str, Any]) -> None:
        """Report the init line's session/model/skills and the terminal result line."""
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            skills = [
                s.get("name") if isinstance(s, dict) else s for s in (event.get("skills") or [])
            ]
            if skill not in skills:
                warn(f"skill '{skill}' is not in the session's skills ({len(skills)} listed)")
            note(f"session={event.get('session_id')} model={event.get('model')}")
        elif kind == "result":
            self._saw_result = True
            self.last_message = str(event.get("result", "") or "")
            note(
                f"result is_error={event.get('is_error')} subtype={event.get('subtype')} "
                f":: {_truncate(str(event.get('result', '')))}"
            )

    def on_finish(self, skill: str, stem: str, scratch: Path) -> None:
        """Warn when no terminal result line arrived (the process died mid-run)."""
        if not self._saw_result:
            warn("no result line in the stream: the claude session died mid-run")
        self._saw_result = False


class CodexRunner(BaseRunner):
    """Runs a role skill with `codex exec --json`."""

    name = "codex"

    def command(self, skill: str, issue: int, stem: str) -> list[str]:
        """Build the `codex exec` command line (conventions.md section 3)."""
        argv = [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            str(self.settings.get("sandbox") or "danger-full-access"),
            "-o",
            f".scratch/{issue}/logs/{stem}.last.md",
        ]
        model = self.settings.get("model") or ""
        if model:
            argv += ["--model", model]
        return argv + list(self.settings.get("extra_args") or []) + [f"${skill} {issue}"]

    def on_line(self, skill: str, event: dict[str, Any]) -> None:
        """Report each completed command execution."""
        if event.get("type") != "item.completed":
            return
        item = event.get("item") or {}
        if (item.get("item_type") or item.get("type")) == "command_execution":
            note(f"$ {_truncate(str(item.get('command', '')), 160)}")

    def on_finish(self, skill: str, stem: str, scratch: Path) -> None:
        """Print the final agent message that codex wrote to its -o file."""
        last = scratch / "logs" / f"{stem}.last.md"
        if last.exists():
            self.last_message = last.read_text(encoding="utf-8")
            note(f"final message :: {_truncate(self.last_message)}")
        else:
            warn(f"codex wrote no final message file ({last})")


RUNNERS = {"claude": ClaudeRunner, "codex": CodexRunner}


def get_runner(role: str, config: Config) -> Runner:
    """Instantiate the runner configured for a role key."""
    name = config.runner_name(role)
    try:
        cls = RUNNERS[name]
    except KeyError:
        raise ConfigError(
            f"role '{role}' names unknown runner '{name}' (known: {', '.join(RUNNERS)})"
        ) from None
    return cls(config.runner_settings(name))
