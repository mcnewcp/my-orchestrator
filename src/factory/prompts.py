"""Render role prompts (design §10) into work/<issue>/prompts/<stage>-<n>.md.

Templates live in roles/<stage>.md and use `{name}` placeholders from PLACEHOLDERS only. Rendering
replaces exactly those tokens (regex over the known names) so literal braces elsewhere in the template
(JSON examples) survive. Missing values render as "(none)".

Placeholder producers (stages.py):
  issue          the issue number
  intent         intent.md text                                  (spec)
  spec           spec.md text                                    (plan, build, review)
  plan           plan.md text                                    (build, review)
  diff           NOT the diff text: a sentence giving the worktree-relative path of the diff file written by
                 stages.write_review_diff (".factory/tmp/review-<n>.diff"), its byte and line counts, whether it was
                 truncated, and the instruction to read it completely, spelled for either harness — offset/limit for a
                 file-reading tool, `cat`/`sed -n` slices for a read-only shell  (review)
  checks         checks.tail(latest check log, 200)             (build, fix, review)
  review_policy  REVIEW.md from the worktree or load_template("REVIEW.md"), plus the factory-appended line
                 "Nit cap enforced by the factory this round: N. Exceeding it fails the round."  (review)
  ledger         format_ledger_for_review(...)                  (review)
  findings       format_findings_for_fix(...)                   (fix)
  stage_note     one short factory-authored paragraph about THIS attempt: the round number, whether this is a --force
                 re-run, and the gate that rejected the previous attempt with its offending paths / failing check /
                 not_addressed entries. "(none)" when there is nothing to say. Producers: build (retry after a
                 GateViolation, recorded transiently in .factory/run/<n>.json "last_error"), review (round), fix (round
                 + previous fix's not_addressed).
"""

from __future__ import annotations

import re
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .errors import FactoryError

ROLES_DIR = Path(__file__).parent / "roles"
TEMPLATES_DIR = Path(__file__).parent / "templates"
PLACEHOLDERS = (
    "intent",
    "spec",
    "plan",
    "diff",
    "checks",
    "review_policy",
    "ledger",
    "findings",
    "issue",
    "stage_note",
)

ROLES = ("spec", "plan", "build", "review", "fix")
NONE = "(none)"
EMPTY_LEDGER = "(empty — first round)"
_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")
_ITEM_INDENT = "   "


def load_role(stage: str) -> str:
    """roles/<stage>.md text; stage in spec|plan|build|review|fix."""
    if stage not in ROLES:
        raise FactoryError(f"unknown role {stage!r}; expected one of {', '.join(ROLES)}")
    return _read(ROLES_DIR / f"{stage}.md", f"role prompt for stage {stage!r}")


def load_template(name: str) -> str:
    """templates/<name> text (REVIEW.md, intent.md, factory.toml). Used by initcmd and by review's fallback policy."""
    if name != Path(name).name:
        raise FactoryError(f"template name {name!r} must be a bare file name")
    return _read(TEMPLATES_DIR / name, f"template {name!r}")


def _read(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FactoryError(f"{what} is missing from the factory package: {path}") from None
    except OSError as exc:
        raise FactoryError(f"cannot read {what} at {path}: {exc}") from None


def render(template: str, values: dict[str, str]) -> str:
    """Replace only the `{placeholder}` tokens named in PLACEHOLDERS; every other brace in the template
    (JSON examples, f-string-looking prose) survives verbatim. A missing, empty or blank value renders
    as "(none)"; keys that are not placeholders are ignored."""
    return _PLACEHOLDER_RE.sub(lambda m: _value(values.get(m.group(1))), template)


def _value(value) -> str:
    if value is None:
        return NONE
    text = value if isinstance(value, str) else str(value)
    return text if text.strip() else NONE


def render_role(stage: str, values: dict[str, str]) -> str:
    return render(load_role(stage), values)


def prompt_path(worktree: Path, issue: int, stage: str, round: int) -> Path:
    return worktree / "work" / str(issue) / "prompts" / f"{stage}-{round}.md"


def write_prompt(worktree: Path, issue: int, stage: str, round: int, text: str) -> Path:
    """Write the rendered prompt to prompt_path(...), creating work/<issue>/prompts/. Returns the path."""
    path = prompt_path(worktree, issue, stage, round)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path


def format_findings_for_fix(findings: list[dict]) -> str:
    """Numbered list of open Important findings with id, file:line, title, detail, evidence — the fixer's input."""
    if not findings:
        return NONE
    blocks = []
    for number, finding in enumerate(_as_dicts(findings), 1):
        blocks.append(
            f"{number}. **{_field(finding, 'id', '(no id)')}** — {_location(finding)} — "
            f"{_field(finding, 'title', '(no title)')}\n"
            f"{_ITEM_INDENT}- detail: {_block(_field(finding, 'detail', NONE))}\n"
            f"{_ITEM_INDENT}- evidence: {_block(_field(finding, 'evidence', NONE))}"
        )
    return "\n\n".join(blocks)


def format_ledger_for_review(findings: list[dict]) -> str:
    """Every finding with id, status, severity, pass, file:line, title; open ones flagged 'NEEDS UPDATE'.
    Empty ledger -> "(empty — first round)"."""
    if not findings:
        return EMPTY_LEDGER
    lines = []
    for finding in _as_dicts(findings):
        status = _field(finding, "status", "open")
        flag = "  **NEEDS UPDATE**" if status == "open" else ""
        lines.append(
            f"- {_field(finding, 'id', '(no id)')} [{status} · {_field(finding, 'severity', '?')} · "
            f"{_pass(finding)}] {_location(finding)} — {_field(finding, 'title', '(no title)')}{flag}"
        )
    return "\n".join(lines)


def describe_diff(
    path_rel: str, nbytes: int, nlines: int, truncated: bool, omitted: list[str]
) -> str:
    """The {diff} sentence(s) described in the module docstring."""
    parts = [
        f"The diff under review is not reproduced here. It is the file `{path_rel}`, relative to the root "
        f"of this worktree: {nbytes} bytes, {nlines} lines.",
        f"Read it completely before you judge anything: start at the beginning and keep going until you "
        f"have seen line {nlines} — with a file-reading tool, continue with offset/limit; with a shell, "
        f"`cat` the file, or walk it in slices with `sed -n '1,400p'`, `sed -n '401,800p'` and so on. A "
        f"single read may return only the first part of the file. Do not review from a partial read.",
    ]
    if truncated:
        banner = (
            "This diff was TRUNCATED by the factory to stay under its size limit: it holds a diffstat "
            "followed by the largest per-file diffs that fit."
        )
        if omitted:
            banner += (
                " These files changed but their diffs are NOT in the file, so you cannot see them and must "
                f"not raise findings about them: {', '.join(omitted)}."
            )
        parts.append(banner)
    return "\n\n".join(parts)


def _as_dicts(findings) -> list[dict]:
    """The ledger holds state.Finding dataclasses and serializes them with to_dict(); accept either, so a caller
    that forgets the conversion still renders a prompt instead of dying mid-stage."""
    out = []
    for finding in findings:
        if isinstance(finding, dict):
            out.append(finding)
        elif is_dataclass(finding) and not isinstance(finding, type):
            out.append(asdict(finding))
        else:
            raise FactoryError(
                f"a finding must be a dict or a dataclass, got {type(finding).__name__}"
            )
    return out


def _field(finding: dict, name: str, default: str) -> str:
    value = finding.get(name)
    if value is None:
        return default
    text = str(value)
    return text if text.strip() else default


def _pass(finding: dict) -> str:
    """The finding's review pass. Finding.to_dict serializes the dataclass field `pass_` as "pass"."""
    for name in ("pass", "pass_"):
        if finding.get(name):
            return str(finding[name])
    return "?"


def _location(finding: dict) -> str:
    file = _field(finding, "file", "(no file)")
    line = finding.get("line")
    return f"{file}:{line}" if line is not None else file


def _block(text: str) -> str:
    """Keep a multi-line detail or evidence inside its list item."""
    return text.strip().replace("\n", "\n" + _ITEM_INDENT + "  ")
