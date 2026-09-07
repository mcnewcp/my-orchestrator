"""`factory doctor` (design §6, §8, §13): binaries, versions vs pins, gh auth, per-harness auth probe + write-mode probe.

Each probe runs in a throwaway WORKTREE — `git init` a scratch repo under .factory/tmp/doctor/<harness>/, commit one
file, `git worktree add wt` — so it reproduces what every stage sees: .git is a FILE pointing at a git dir outside the
sandbox's writable root, and the path has never been trusted by the harness before. Probes use schemas/probe.json
({"ok": bool, "note": str}) and a prompt written by doctor. The read probe exercises every flag the adapter depends on
(--max-turns, --setting-sources, --strict-mcp-config, --json-schema ...) because `claude --version` exits 0 for unknown
flags and cannot detect a removal. The write probe must exit 0, leave probe.txt in place, and then one configured check
command (config.checks[0]) must run in that worktree via checks.run_checks with the checks env (catches a sandbox that
cannot write its cache, or a missing `make`).

The scratch worktree holds no repository source, so the check command is smoke-tested rather than run for real: when
config.checks[0] invokes `make`, doctor seeds a Makefile whose targets are no-ops (_seed_probe_check), and the report
line says so. What the step proves is that the command executes inside a fresh worktree under the harness's sandbox
leftovers, which is what design §13 asks of it — never that the repository's checks pass. The scratch repo also
carries an AGENTS.md stand-in, so an api-mode probe exercises --append-system-prompt-file exactly as an api-mode
stage does.

Every probe goes through harness.get_harness(...).run(...) — the same adapter, and therefore the same argv builder
(ClaudeCode.argv / Codex.argv), that every stage uses. That is the point of `doctor`: a flag that disappeared from a
CLI must fail here, on a two-turn errand, rather than 40 minutes into a build.

.factory/doctor.json = {"<harness>:<auth>": {"factory_version", "cli_version", "at", "checks": [...]}}; records for
different combinations coexist. cli.py runs doctor once for the configured (harness, auth) pair, or for the pair given by
--harness/--auth.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from . import checks as checks_module
from . import harness as harness_module
from . import repo as repo_module
from . import state as state_module
from .config import Config
from .errors import FactoryError
from .gh import GitHub
from .repo import Repo
from .schema import schema_path

# A probe is a two-turn errand; the 45-minute stage timeout would only hide a hang.
PROBE_TIMEOUT_S = 300
PROBE_FILE = "probe.txt"
AGENTS_FILE = "AGENTS.md"
PROBE_CONTENT = "factory-probe"
PROBE_PROMPTS = {
    "read": (
        "# factory doctor probe (read)\n\n"
        "Do not create, edit or delete any file. Return the structured result the output schema requires:\n"
        "`ok` = true and `note` = one line saying that you ran here.\n"
    ),
    "write": (
        "# factory doctor probe (write)\n\n"
        f"1. Create a file named `{PROBE_FILE}` in the current directory containing exactly "
        f"`{PROBE_CONTENT}`.\n"
        "2. Return the structured result the output schema requires: `ok` = true and `note` = one line "
        "saying what you did.\n"
    ),
}


@dataclass
class DoctorReport:
    ok: bool
    lines: list[str] = field(default_factory=list)  # "ok: ..." / "warn: ..." / "FAIL: ..."
    harness: str = ""
    cli_version: str = ""
    auth: str = ""

    def render(self) -> str:
        return "\n".join(self.lines)

    def _ok(self, message: str) -> None:
        self.lines.append(f"ok: {message}")

    def _warn(self, message: str) -> None:
        self.lines.append(f"warn: {message}")

    def _fail(self, message: str) -> None:
        self.lines.append(f"FAIL: {message}")
        self.ok = False


@dataclass
class _Probe:
    """Everything the two probes share, so their helpers keep short signatures."""

    repo: Repo
    config: Config
    harness_name: str
    auth: str
    model: str | None
    env: dict
    harness: object  # harness.Harness
    worktree: Path

    @property
    def timeout_s(self) -> int:
        return min(self.config.stage_timeout_s, PROBE_TIMEOUT_S)


def doctor(
    repo: Repo,
    config: Config,
    *,
    harness: str,
    auth: str,
    model: str | None,
    parent_env: dict,
    write_probe: bool = True,
) -> DoctorReport:
    """Order (stop at the first FAIL that makes later steps meaningless):
    python >= 3.12; git present, `git status` in the checkout works (a "dubious ownership" error FAILs with the
    safe.directory hint), identity: `git var GIT_AUTHOR_IDENT` or "ok: no git identity; commits use factory <factory@localhost>";
    every distinct config.checks[i][0] resolves on PATH (else FAIL naming it); gh present + gh.auth_ok; harness binary
    present + version (claude >= CLAUDE_MIN_VERSION; warn on pinned_version mismatch); build_env (api key presence;
    reports which NETWORK_ALLOWLIST vars were forwarded); read probe; write probe (+ one check command).
    For codex, best effort: `codex login status` under the built env, WARN if it mentions multiple auth env vars.
    Writes the doctor record on ok."""
    report = DoctorReport(ok=True, harness=harness, auth=auth)
    if not _check_python(report):
        return report
    if not _check_git(report, repo, parent_env):
        return report
    base_env = _base_env(report, harness, config, parent_env)
    if base_env is None:
        return report
    check_binaries_ok = _check_check_binaries(report, config, base_env)
    _check_gh(report, repo, base_env)

    found = _check_harness_binary(report, config, harness, base_env)
    if found is None:
        return report
    harness_obj, cli_version = found
    report.cli_version = cli_version
    probe_env = _probe_env(report, harness, auth, config, parent_env)
    if probe_env is None:
        return report
    if harness == "codex":
        _codex_login_status(report, repo, probe_env)

    worktree = _probe_worktree(report, repo, config, harness)
    if worktree is None:
        return report
    probe = _Probe(
        repo=repo,
        config=config,
        harness_name=harness,
        auth=auth,
        model=model,
        env=probe_env,
        harness=harness_obj,
        worktree=worktree,
    )
    if not _run_probe(report, probe, "read"):
        return report
    if write_probe:
        _write_probe(report, probe, run_check=check_binaries_ok)
    if report.ok:
        _write_record(repo, harness, auth, report)
    return report


def doctor_record_is_current(
    records: dict, *, harness: str, auth: str, cli_version: str, factory_version: str
) -> bool:
    """True when doctor.json holds a passing record for exactly this (harness, auth) pair, taken with this factory
    version and this harness CLI version (design §12 preflight). Anything else -> run doctor again. "Passing" is
    read from the record's own `ok` field rather than assumed: only `doctor` writes this file today, and only on a
    pass, but a preflight that skips itself on a rescued or hand-written record would be a silent hole."""
    record = records.get(f"{harness}:{auth}") if records else None
    if not isinstance(record, dict):
        return False
    return (
        record.get("ok") is True
        and record.get("factory_version") == factory_version
        and record.get("cli_version") == cli_version
    )


# ---------------------------------------------------------------- environment and binaries


def _check_python(report: DoctorReport) -> bool:
    version = sys.version_info
    reported = f"{version.major}.{version.minor}.{version.micro}"
    if version < (3, 12):
        report._fail(f"python {reported} is below the required 3.12")
        return False
    report._ok(f"python {reported}")
    return True


def _check_git(report: DoctorReport, repo: Repo, parent_env: dict) -> bool:
    if harness_module.which("git", parent_env) is None:
        report._fail("git is not on PATH")
        return False
    result = repo_module.git(["status", "--porcelain"], repo.root, check=False)
    if result.returncode != 0:
        detail = _tail(result.stderr) or f"exit {result.returncode}"
        hint = ""
        if "dubious ownership" in result.stderr:
            hint = f"; run `git config --global --add safe.directory {repo.root}`"
        report._fail(f"`git status` failed in {repo.root}: {detail}{hint}")
        return False
    report._ok(f"git works in {repo.root}")
    _check_git_identity(report, repo)
    return True


def _check_git_identity(report: DoctorReport, repo: Repo) -> None:
    result = repo_module.git(["var", "GIT_AUTHOR_IDENT"], repo.root, check=False)
    fields = result.stdout.split()
    if result.returncode != 0 or len(fields) < 3:
        report._ok("no git identity; commits use factory <factory@localhost>")
        return
    report._ok(f"git identity {' '.join(fields[:-2])}")


def _check_check_binaries(report: DoctorReport, config: Config, env: dict) -> bool:
    binaries = list(dict.fromkeys(check[0] for check in config.checks if check))
    if not binaries:
        report._warn(
            "no check commands are configured; the build and fix gates would pass on nothing"
        )
        return False
    ok = True
    for binary in binaries:
        found = harness_module.which(binary, env)
        if found is None:
            report._fail(f"check command `{binary}` is not on PATH")
            ok = False
        else:
            report._ok(f"check command `{binary}` -> {found}")
    return ok


def _check_gh(report: DoctorReport, repo: Repo, env: dict) -> None:
    if harness_module.which("gh", env) is None:
        report._fail("gh is not on PATH")
        return
    try:
        authenticated, detail = GitHub(repo.root).auth_ok()
    except FactoryError as exc:
        report._fail(f"`gh auth status` failed: {exc.message}")
        return
    if authenticated:
        report._ok(f"gh authenticated: {detail}")
    else:
        report._fail(f"gh is not authenticated: {detail}")


def _base_env(report: DoctorReport, harness: str, config: Config, parent_env: dict) -> dict | None:
    """PATH lookups and `--version` run under the subscription-flavoured environment: a missing API key must FAIL
    on its own line (_probe_env), not disguise itself as a missing binary."""
    try:
        return harness_module.build_env(
            harness, "subscription", parent_env, passthrough=config.env_passthrough
        )
    except FactoryError as exc:
        report._fail(f"environment: {exc.message}")
        return None


def _check_harness_binary(
    report: DoctorReport, config: Config, harness: str, env: dict
) -> tuple[object, str] | None:
    if harness_module.which(harness, env) is None:
        report._fail(f"harness binary `{harness}` is not on PATH")
        return None
    try:
        harness_obj = harness_module.get_harness(harness)
        version = harness_obj.version(env).strip()
    except FactoryError as exc:
        report._fail(f"`{harness} --version` failed: {exc.message}")
        return None
    parsed = _version_tuple(version)
    minimum = harness_module.CLAUDE_MIN_VERSION
    if harness == "claude" and parsed and parsed < minimum:
        report._fail(
            f"claude {version} is below the required {_format_version(minimum)} "
            "(--permission-prompts none, design §19)"
        )
        return None
    report._ok(f"{harness} {version}")
    pinned = config.harness_config(harness).pinned_version
    if pinned and _version_tuple(pinned) != parsed:
        report._warn(
            f"{harness} {version} does not match [harness.{harness}] pinned_version {pinned}"
        )
    return harness_obj, version


def _probe_env(
    report: DoctorReport, harness: str, auth: str, config: Config, parent_env: dict
) -> dict | None:
    try:
        env = harness_module.build_env(
            harness, auth, parent_env, passthrough=config.env_passthrough
        )
    except FactoryError as exc:
        report._fail(exc.message)
        return None
    key = harness_module.PROVIDER_KEYS.get(harness, "the provider key")
    if auth == "api":
        report._ok(f"auth api: {key} is set and is the only provider key the harness receives")
    else:
        report._ok(
            f"auth subscription: no provider key is forwarded; {harness}'s saved login is used"
        )
    forwarded = [name for name in harness_module.NETWORK_ALLOWLIST if name in env]
    report._ok("network variables forwarded: " + (", ".join(forwarded) if forwarded else "(none)"))
    return env


def _codex_login_status(report: DoctorReport, repo: Repo, env: dict) -> None:
    """Best effort (design §8 "doctor ... reports what authenticated"): codex warns when several auth env vars are
    present and then picks one, which silently changes who is paying for a run."""
    try:
        proc = subprocess.run(
            ["codex", "login", "status"],
            cwd=repo.root,
            env=env,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report._warn(f"`codex login status` did not run: {exc}")
        return
    output = f"{proc.stdout}\n{proc.stderr}".strip()
    first_line = _tail(output, first=True) or f"exit {proc.returncode}"
    if "multiple" in output.lower() and "auth" in output.lower():
        report._warn(f"codex reports more than one auth source: {first_line}")
        return
    report._ok(f"codex login status: {first_line}")


# ---------------------------------------------------------------- probes


def _probe_worktree(report: DoctorReport, repo: Repo, config: Config, harness: str) -> Path | None:
    root = repo.factory_dir / "tmp" / "doctor" / harness
    worktree = root / "wt"
    try:
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        (root / "README.md").write_text("factory doctor probe repository\n", encoding="utf-8")
        # A stand-in for the target repo's AGENTS.md, so an api-mode probe exercises
        # --append-system-prompt-file the way every api-mode stage does (design §8).
        (root / AGENTS_FILE).write_text(
            "Repository instructions for the factory doctor probe: answer the prompt file and stop.\n",
            encoding="utf-8",
        )
        repo_module.git(["init", "--quiet"], root)
        repo_module.git(["add", "-A"], root)
        repo_module.git(
            [
                "-c",
                "user.name=factory",
                "-c",
                "user.email=factory@localhost",
                "commit",
                "--quiet",
                "-m",
                "doctor probe base",
            ],
            root,
        )
        repo_module.git(["worktree", "add", "--quiet", str(worktree)], root)
    except (FactoryError, OSError) as exc:
        report._fail(f"could not build the probe worktree at {worktree}: {exc}")
        return None
    _seed_probe_check(worktree, config)
    report._ok(f"probe worktree {worktree}")
    return worktree


def _seeds_a_makefile(config: Config) -> bool:
    """True when the probed check is a `make` invocation, and therefore runs against targets doctor wrote
    rather than the repository's own. The report says so; nothing else depends on it."""
    first = config.checks[0] if config.checks else []
    return bool(first) and Path(first[0]).name == "make"


def _seed_probe_check(worktree: Path, config: Config) -> None:
    """`make test` in a scratch worktree fails for want of a Makefile, which says nothing about the toolchain, so a
    `make` check gets no-op targets to run against. Any other command is run exactly as configured."""
    if not _seeds_a_makefile(config):
        return
    first = config.checks[0]
    targets = [arg for arg in first[1:] if not arg.startswith("-")] or ["all"]
    recipes = "".join(f"{target}:\n\t@true\n" for target in targets)
    (worktree / "Makefile").write_text(f".PHONY: {' '.join(targets)}\n{recipes}", encoding="utf-8")


def _run_probe(report: DoctorReport, probe: _Probe, mode: str) -> bool:
    prompt_file = probe.worktree / f"doctor-{mode}-prompt.md"
    prompt_file.write_text(PROBE_PROMPTS[mode], encoding="utf-8")
    transcript_path = (
        probe.repo.factory_dir
        / "transcripts"
        / "doctor"
        / f"{probe.harness_name}-{probe.auth}-{mode}-{_stamp()}.json"
    )
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    harness_config = probe.config.harness_config(probe.harness_name)
    try:
        result = probe.harness.run(
            cwd=probe.worktree,
            prompt_file=prompt_file,
            schema_file=schema_path("probe"),
            mode=mode,
            model=probe.model,
            auth=probe.auth,
            env=probe.env,
            timeout_s=probe.timeout_s,
            transcript_path=transcript_path,
            max_turns=(
                harness_config.max_turns_read if mode == "read" else harness_config.max_turns_write
            ),
            max_budget_usd=harness_config.max_budget_usd,
            agents_md=probe.worktree / AGENTS_FILE,
            writable_dirs=harness_config.writable_dirs if mode == "write" else None,
        )
    except FactoryError as exc:
        where = getattr(exc, "transcript_path", None) or transcript_path
        report._fail(f"{mode} probe: {exc.message}; transcript {where}")
        return False
    output = result.output if isinstance(result.output, dict) else {}
    note = str(output.get("note", "")).strip()
    if output.get("ok") is not True:
        report._fail(
            f"{mode} probe: {probe.harness_name} returned ok={output.get('ok')!r} "
            f"note={note!r}; transcript {result.transcript_path}"
        )
        return False
    report._ok(f"{mode} probe: {note or 'ok'}")
    return True


def _write_probe(report: DoctorReport, probe: _Probe, *, run_check: bool) -> bool:
    probe_file = probe.worktree / PROBE_FILE
    probe_file.unlink(missing_ok=True)
    if not _run_probe(report, probe, "write"):
        return False
    if not probe_file.exists():
        report._fail(
            f"write probe: {probe.harness_name} exited 0 but did not create {probe_file}; "
            "its sandbox could not write the worktree (design §13: fix the container, not the flags)"
        )
        return False
    content = probe_file.read_text(encoding="utf-8", errors="replace").strip()
    if content != PROBE_CONTENT:
        report._warn(f"write probe: {PROBE_FILE} holds {content!r}, expected {PROBE_CONTENT!r}")
    report._ok(f"write probe: {PROBE_FILE} written in the probe worktree")
    if not probe.config.checks:
        return True
    if not run_check:
        report._warn(
            f"write probe: check `{' '.join(probe.config.checks[0])}` was not run "
            "(its command is not on PATH)"
        )
        return False
    return _probe_check(report, probe)


def _probe_check(report: DoctorReport, probe: _Probe) -> bool:
    command = probe.config.checks[0]
    name = " ".join(command)
    run = checks_module.run_checks(
        probe.worktree,
        replace(probe.config, checks=[command]),
        harness_module.checks_env(probe.env),
        probe.timeout_s,
    )
    if not run.ok:
        report._fail(f"write probe: check `{name}` failed in the probe worktree: {_tail(run.log)}")
        return False
    report._ok(f"write probe: check `{name}` {_probe_check_scope(probe.config)}")
    return True


def _probe_check_scope(config: Config) -> str:
    """What passing the probe check does and does not prove. The probe worktree holds no repository source: a
    `make` check runs against the no-op targets doctor seeded, so a green line here says the sandbox can find
    and execute `make` after the harness has written in that worktree — never that the repository's checks pass."""
    if _seeds_a_makefile(config):
        return (
            "ran against a seeded no-op Makefile — proves the sandbox can exec make, "
            "not the repo's checks"
        )
    return (
        "ran in the probe worktree, which holds no repository source — proves the command executes, "
        "not that the repo's checks pass"
    )


# ---------------------------------------------------------------- record


def _write_record(repo: Repo, harness: str, auth: str, report: DoctorReport) -> None:
    state_module.write_doctor_record(
        repo.factory_dir,
        harness,
        auth,
        {
            "ok": True,  # written only on a pass; doctor_record_is_current requires it
            "factory_version": __version__,
            "cli_version": report.cli_version,
            "at": state_module.now_iso(),
            "checks": list(report.lines),
        },
    )


# ---------------------------------------------------------------- small helpers


def _version_tuple(text: str) -> tuple[int, ...]:
    """harness.parse_version, tolerant: an unparseable version is compared as "unknown" ( () ) instead of raising."""
    try:
        return harness_module.parse_version(text)
    except (FactoryError, ValueError, IndexError):
        return ()


def _format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _tail(text: str, limit: int = 200, *, first: bool = False) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[0] if first else lines[-1]
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
