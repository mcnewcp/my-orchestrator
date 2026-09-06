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
                 truncated, and the instruction to read it completely with offset/limit  (review)
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

from pathlib import Path

ROLES_DIR = Path(__file__).parent / "roles"
TEMPLATES_DIR = Path(__file__).parent / "templates"
PLACEHOLDERS = ("intent", "spec", "plan", "diff", "checks", "review_policy", "ledger", "findings", "issue", "stage_note")


def load_role(stage: str) -> str:
    """roles/<stage>.md text; stage in spec|plan|build|review|fix."""
    raise NotImplementedError


def load_template(name: str) -> str:
    """templates/<name> text (REVIEW.md, intent.md, factory.toml). Used by initcmd and by review's fallback policy."""
    raise NotImplementedError


def render(template: str, values: dict[str, str]) -> str:
    raise NotImplementedError


def render_role(stage: str, values: dict[str, str]) -> str:
    return render(load_role(stage), values)


def prompt_path(worktree: Path, issue: int, stage: str, round: int) -> Path:
    return worktree / "work" / str(issue) / "prompts" / f"{stage}-{round}.md"


def write_prompt(worktree: Path, issue: int, stage: str, round: int, text: str) -> Path:
    raise NotImplementedError


def format_findings_for_fix(findings: list[dict]) -> str:
    """Numbered list of open Important findings with id, file:line, title, detail, evidence — the fixer's input."""
    raise NotImplementedError


def format_ledger_for_review(findings: list[dict]) -> str:
    """Every finding with id, status, severity, pass, file:line, title; open ones flagged 'NEEDS UPDATE'.
    Empty ledger -> "(empty — first round)"."""
    raise NotImplementedError


def describe_diff(path_rel: str, nbytes: int, nlines: int, truncated: bool, omitted: list[str]) -> str:
    """The {diff} sentence(s) described in the module docstring."""
    raise NotImplementedError
