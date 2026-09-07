"""Deterministic checks and allowed-edit rules (design §9).

Checks run as argv arrays without a shell, cwd = worktree, env = harness.checks_env(...) (no provider keys), via
harness.run_streaming (process-group kill on timeout; output streamed to a file, then read back).
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from .config import Config
from .errors import FactoryError, HarnessError
from .harness import run_streaming

PROTECTED_PATHS: tuple[str, ...] = (
    # GNU make reads GNUmakefile, then makefile, then Makefile, and stops at the first that exists — all
    # three are "the gate runs whatever the Makefile says" (design §9), so all three are protected.
    "GNUmakefile",
    "makefile",
    "Makefile",
    "factory.toml",
    "AGENTS.md",
    "CLAUDE.md",
    "REVIEW.md",
    ".devcontainer/",
    ".claude/",
    ".mcp.json",
    ".codex/",
    ".github/",
)
# Protected wherever they sit, not only at the root: both CLIs load the instruction file of every directory
# on the way to a file they read, so `docs/AGENTS.md` configures the next session exactly as the root one does.
PROTECTED_BASENAMES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md")
_PROTECTED_BASENAMES_FOLDED = frozenset(name.casefold() for name in PROTECTED_BASENAMES)
# Tooling droppings the factory itself creates by running the checks (and its own .factory/tmp inside the worktree).
# Never the operator's edits, never a stage's output. Written to the checkout's .git/info/exclude by
# Repo.ensure_excludes and ignored by every clean/dirty comparison via is_transient().
TRANSIENT_PATHS: tuple[str, ...] = (
    ".factory/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    ".tox/",
    "node_modules/",
    ".gradle/",
    "target/",
    "*.pyc",
    "*.egg-info/",
)
STAGE_LOG_ORDER = ("build", "fix", "finalize")

NONE = "(none)"
WORK_DIR = "work"
# work/<issue>/checks/<stage>-<round><suffix>.log — suffix is "-baseline" for build's pre-session run.
_LOG_NAME_RE = re.compile(r"^(?P<stage>[A-Za-z]+)-(?P<round>\d+)(?P<suffix>-.*)?\.log$")
# A markdown ATX heading of level 2 or deeper: "## Proof", "   ### proof ###".
_HEADING_RE = re.compile(r"^ {0,3}#{2,6}[ \t]+(?P<text>.*?)[ \t]*#*[ \t]*$")
_REQUIRED_PLAN_SECTIONS = ("## Files that change", "## Proof")
# One character a path can be spelled with: word characters (\w, so non-ASCII names count), separators, dots
# and dashes. Everything else — backticks, quotes, brackets, parentheses, commas, em dashes, whitespace, the
# end of a line — is markdown around the path and therefore a boundary.
_PATH_CHAR_RE = re.compile(r"[\w./\\-]")
_GLOB_CHARS = "*?["


@dataclass
class CheckRun:
    ok: bool
    # full combined output of every check, with a header line per command and its exit code
    log: str
    failed: list[str] = field(default_factory=list)  # "make test" style names of failing commands


def run_checks(cwd: Path, config: Config, env: dict[str, str], timeout_s: int) -> CheckRun:
    """Run every config.checks entry in order; stop at the first failure (its output is still captured).
    A timeout or a missing binary counts as a failure. Log format per check:
        $ make test
        <stdout+stderr>
        [exit 0]
    """
    if not config.checks:
        return CheckRun(ok=True, log=_section("(no checks configured)", "", "exit 0"))
    sections: list[str] = []
    failed: list[str] = []
    remaining = max(1, int(timeout_s))
    with tempfile.TemporaryDirectory(prefix="factory-checks-") as tmp:
        tmpdir = Path(tmp)
        for index, argv in enumerate(config.checks):
            name = _check_name(argv, index)
            stdout_path, stderr_path = tmpdir / f"{index}.out", tmpdir / f"{index}.err"
            status, ok, duration = _run_one(
                list(argv),
                cwd=cwd,
                env=env,
                timeout_s=remaining,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                name=name,
            )
            remaining = max(1, remaining - int(duration))
            sections.append(_section(name, _combined_output(stdout_path, stderr_path), status))
            if not ok:
                failed.append(name)
                break
    return CheckRun(ok=not failed, log="".join(sections), failed=failed)


def _check_name(argv: list[str], index: int) -> str:
    if not argv or not all(isinstance(word, str) and word for word in argv):
        raise FactoryError(
            f"factory.toml: checks[{index}] is not a non-empty list of strings (got {argv!r})",
            'each check is an argv array, e.g. checks = [["make", "test"]]',
        )
    return " ".join(argv)


def _run_one(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: int,
    stdout_path: Path,
    stderr_path: Path,
    name: str,
) -> tuple[str, bool, float]:
    """Run one check. Returns (status text for the log, passed, duration_s). Never raises for a failing check:
    a non-zero exit, a timeout (HarnessError from run_streaming, which killed the process group) and a binary
    that cannot be executed (FactoryError from run_streaming, or OSError straight from Popen) are all just
    failures whose reason goes into the log. A red check is the caller's gate, never an exception."""
    try:
        exit_code, duration = run_streaming(
            argv,
            cwd=cwd,
            env=env,
            timeout_s=timeout_s,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            what=name,
        )
    except HarnessError as exc:  # timeout: the process group is already dead
        return str(exc), False, float(timeout_s)
    except FactoryError as exc:  # run_streaming could not start the binary
        return str(exc), False, 0.0
    except OSError as exc:  # Popen's own error, if run_streaming ever lets it through
        return f"cannot run {name!r}: {exc}", False, 0.0
    return f"exit {exit_code}", exit_code == 0, duration


def _section(name: str, output: str, status: str) -> str:
    lines = [f"$ {name}"]
    body = output.rstrip("\n")
    if body:
        lines.append(body)
    lines.append(f"[{status}]")
    return "\n".join(lines) + "\n"


def _combined_output(stdout_path: Path, stderr_path: Path) -> str:
    """stdout then stderr — run_streaming writes them to separate files (one shared handle would interleave
    two file offsets and lose output), so the log concatenates them in that order."""
    return _read_text(stdout_path) + _read_text(stderr_path)


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


def check_log_path(worktree: Path, issue: int, stage: str, round: int, suffix: str = "") -> Path:
    return worktree / "work" / str(issue) / "checks" / f"{stage}-{round}{suffix}.log"


def write_check_log(
    worktree: Path, issue: int, stage: str, round: int, log: str, *, suffix: str = ""
) -> Path:
    """work/<issue>/checks/<stage>-<round><suffix>.log (committed with the stage). Round convention: build always 1
    (a --force rewind removes the previous attempt's log; a plain re-run overwrites it); fix uses state.fix_rounds + 1;
    review has no log; finalize uses 1. suffix is "-baseline" for build's pre-session run."""
    path = check_log_path(worktree, issue, stage, round, suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(log if log.endswith("\n") else log + "\n", encoding="utf-8")
    return path


def latest_check_log(worktree: Path, issue: int) -> tuple[Path | None, str]:
    """The newest work/<issue>/checks/*.log by (file mtime desc, then STAGE_LOG_ORDER, then integer round desc);
    returns (path, text) or (None, "(none)"). The single accessor used by review's {checks}, build/fix's {checks},
    and finalize's summary comment.

    STAGE_LOG_ORDER is chronological, so a LATER stage wins a tie: finalize > fix > build, matching "round desc"
    (fix-10 over fix-2) and the plain log over its "-baseline" sibling. Ties are real — `git checkout` and a fresh
    worktree stamp every restored file with the same mtime."""
    directory = worktree / "work" / str(issue) / "checks"
    logs = [p for p in directory.glob("*.log") if p.is_file()] if directory.is_dir() else []
    if not logs:
        return None, NONE
    newest = min(logs, key=_log_sort_key)
    return newest, _read_text(newest)


def _log_sort_key(path: Path) -> tuple[float, int, int, int, str]:
    """Ascending sort key whose minimum is the newest log: mtime descending, then the stage order reversed
    (the later stage is the newer log), then the round descending (integer, so fix-10 beats fix-2), then the
    plain log before its "-baseline" sibling, then the name so the result never depends on readdir order."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    match = _LOG_NAME_RE.match(path.name)
    if match is None:
        return (-mtime, 1, 0, 1, path.name)
    stage = match.group("stage")
    suffix = match.group("suffix") or ""
    order = STAGE_LOG_ORDER.index(stage) if stage in STAGE_LOG_ORDER else -1
    return (-mtime, -order, -int(match.group("round")), 1 if suffix else 0, path.name)


def _normalize(path: str) -> str:
    """A worktree-relative posix path with any './' prefix and surrounding slashes removed."""
    text = path.strip()
    if text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _matches_prefix(path: str, entry: str) -> bool:
    """The path IS `entry` or lies under it, anchored at the worktree root."""
    base = _normalize(entry)
    if not base:
        return False
    return path == base or path.startswith(base + "/")


def is_protected(path: str, config: Config) -> bool:
    """PROTECTED_PATHS + PROTECTED_BASENAMES + config.protected_paths. Entries ending in '/' match the
    directory prefix; others match the exact path or the path as a directory prefix (".github" matches
    ".github/x.yml"); PROTECTED_BASENAMES match the last component at any depth.

    Matching is case-insensitive (both sides casefolded): `.CLAUDE/settings.json`, `makefile` and `Makefile`
    are one file on a case-insensitive filesystem and one gate everywhere else, and an agent that renames
    `Makefile` to `MAKEFILE` has still changed what `make` runs."""
    target = _normalize(path)
    if not target:
        return False
    folded = target.casefold()
    if folded.rpartition("/")[2] in _PROTECTED_BASENAMES_FOLDED:
        return True
    return any(
        _matches_prefix(folded, entry.casefold())
        for entry in (*PROTECTED_PATHS, *config.protected_paths)
    )


def is_transient(path: str, config: Config) -> bool:
    """TRANSIENT_PATHS + config.transient_paths: '/'-suffixed entries match any path component prefix
    (".venv/" matches ".venv/x" and "sub/.venv/x"); '*.ext' entries match by suffix."""
    target = _normalize(path)
    if not target:
        return False
    components = target.split("/")
    for entry in (*TRANSIENT_PATHS, *config.transient_paths):
        pattern = _normalize(entry)
        if not pattern:
            continue
        if entry.strip().endswith("/") or "/" not in pattern:
            # A directory name (or a bare glob such as "*.pyc") matches at any depth.
            if any(fnmatchcase(component, pattern) for component in components):
                return True
        elif any(char in pattern for char in _GLOB_CHARS):
            if fnmatchcase(target, pattern):
                return True
        elif _matches_prefix(target, pattern):
            return True
    return False


def is_under(path: str, prefixes: list[str]) -> bool:
    """True when `path` is one of `prefixes` or lies under one of them (root-anchored, trailing '/' optional)."""
    target = _normalize(path)
    if not target:
        return False
    return any(_matches_prefix(target, prefix) for prefix in prefixes)


def allowed_edit_violations(
    changed_paths: list[str],
    *,
    stage: str,
    issue: int,
    config: Config,
    ignore: list[str] | None = None,
) -> list[str]:
    """Return offending paths for a write stage's changes (design §9). Scope: the write stage's OWN diff (the
    branch-level protected-path check before a session launches is stages.branch_protected_path_violations).
      - paths in `ignore` (the factory's own rendered prompt file) are never violations
      - any protected path -> violation (both stages)
      - build: inside work/<issue>/ only plan.md is allowed; any other work/ path is a violation
      - fix:   any path under config.test_paths or anything under work/ is a violation

    Every rule is evaluated over the path itself, with no transient-location escape: a path that breaks a rule
    is a violation wherever it sits. is_transient() decides which droppings the worktree-clean comparison
    ignores (deviation 12), and it must not double as an amnesty here — a `transient_paths` entry naming a
    directory that also holds an AGENTS.md or the test suite would otherwise silently disable the gate for it.
    A transient path that breaks no rule is not a violation, because no rule matches it.

    Paths are worktree-relative posix strings.
    """
    ignored = {_normalize(p) for p in (ignore or [])}
    plan_md = f"{WORK_DIR}/{issue}/plan.md"
    violations: list[str] = []
    for raw in changed_paths:
        path = _normalize(raw)
        if not path or path in ignored or path in violations:
            continue
        if is_protected(path, config):
            violations.append(path)
        elif stage == "build":
            if is_under(path, [WORK_DIR]) and path != plan_md:
                violations.append(path)
        elif stage == "fix":
            if is_under(path, [WORK_DIR]) or is_under(path, config.test_paths):
                violations.append(path)
    return violations


def paths_missing_from_plan(changed_paths: list[str], plan_md: str, *, issue: int) -> list[str]:
    """Design §9 build gate "every changed path listed in plan.md": a changed path (outside work/) is listed
    when the path appears in plan_md literally, bounded on both sides by a character a path cannot contain.
    Returns the unlisted ones.

    Literal search, not a token scan, so a path is matchable whatever it is spelled with: `src/my report.py`
    is listed by "- `src/my report.py` — rewritten", and a space, bracket or quote inside the path no longer
    hides it from the gate (a token scan would split the path and never match it, failing every build that
    touched such a file).

    Bounded, not substring (deviation 23): a plan naming `src/app.py.bak` or `docs/src/app.py` does not
    license a change to `src/app.py`.
    """
    del issue  # the gate is the same for every issue; the parameter keeps the call sites explicit
    haystacks = _plan_haystacks(plan_md)
    missing: list[str] = []
    for raw in changed_paths:
        path = _normalize(raw)
        if not path or is_under(path, [WORK_DIR]) or path in missing:
            continue
        if not _plan_lists(haystacks, path):
            missing.append(path)
    return missing


def _plan_haystacks(plan_md: str) -> tuple[str, ...]:
    """The plan as written, plus a copy with '\\' turned into '/' so a plan spelling a path the Windows way
    (`src\\app.py`) still lists the path git reports (`src/app.py`). The substitution is one character for
    one, so an offset — and therefore the boundary test — means the same thing in either copy."""
    slashed = plan_md.replace("\\", "/")
    return (plan_md,) if slashed == plan_md else (plan_md, slashed)


def _plan_lists(haystacks: tuple[str, ...], path: str) -> bool:
    """`path`, or `./path` as a plan may write it, occurring as a whole path in any of the haystacks."""
    return any(
        _occurs_as_whole_path(text, candidate)
        for text in haystacks
        for candidate in (path, f"./{path}")
    )


def _occurs_as_whole_path(text: str, needle: str) -> bool:
    """`needle` occurs in `text` with a path boundary on both sides. Every occurrence is tried: the first one
    may be inside a longer path (`docs/src/app.py`) while a later one is the real listing."""
    start = text.find(needle)
    while start != -1:
        if _boundary_before(text, start) and _boundary_after(text, start + len(needle)):
            return True
        start = text.find(needle, start + 1)
    return False


def _boundary_before(text: str, index: int) -> bool:
    return index == 0 or not _is_path_char(text[index - 1])


def _boundary_after(text: str, index: int) -> bool:
    """The end of the text, or a character no path contains — backtick, quote, bracket, parenthesis, comma,
    whitespace, end of line. A '.' also ends the path when it is itself followed by one of those: "we rewrite
    src/app.py." names the file, while "src/app.py.bak" is a different one (deviation 23)."""
    if index >= len(text):
        return True
    char = text[index]
    if not _is_path_char(char):
        return True
    return char == "." and (index + 1 >= len(text) or not _is_path_char(text[index + 1]))


def _is_path_char(char: str) -> bool:
    return _PATH_CHAR_RE.match(char) is not None


def plan_has_required_sections(plan_md: str) -> list[str]:
    """Design §9 plan gate: returns the missing headings among "## Files that change" and "## Proof"
    (case-insensitive match on the heading text, any heading level >= 2)."""
    found = set()
    for line in plan_md.splitlines():
        match = _HEADING_RE.match(line)
        if match is not None:
            found.add(" ".join(match.group("text").split()).casefold())
    return [s for s in _REQUIRED_PLAN_SECTIONS if s.lstrip("# ").casefold() not in found]


def tail(text: str, lines: int = 60) -> str:
    """The last `lines` lines of `text`, with a banner naming how many earlier lines were dropped.
    Short text, empty text and a non-positive `lines` come back unchanged."""
    if not text:
        return ""
    all_lines = text.splitlines()
    if lines <= 0 or len(all_lines) <= lines:
        return text
    omitted = len(all_lines) - lines
    kept = all_lines[-lines:]
    return "\n".join([f"[... {omitted} earlier lines omitted ...]", *kept]) + "\n"
