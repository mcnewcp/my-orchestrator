"""Unit tests for factory.prompts — role rendering and the factory-authored prompt sections (design §10).

Self-contained: reads the real files under src/factory/roles and src/factory/templates, writes only into tmp_path.
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from factory import prompts
from factory.errors import FactoryError

ROLES = ("spec", "plan", "build", "review", "fix")


# ---------------------------------------------------------------- loading the shipped files


@pytest.mark.parametrize("stage", ROLES)
def test_load_role_reads_the_shipped_role(stage):
    text = prompts.load_role(stage)

    assert text.startswith(f"# Role: {stage}")
    assert (prompts.ROLES_DIR / f"{stage}.md").is_file()


def test_load_role_rejects_an_unknown_stage():
    with pytest.raises(FactoryError) as excinfo:
        prompts.load_role("doctor")

    assert "doctor" in excinfo.value.message


@pytest.mark.parametrize("name", ["REVIEW.md", "intent.md", "factory.toml"])
def test_load_template_reads_the_shipped_template(name):
    assert prompts.load_template(name).strip() != ""


def test_load_template_rejects_a_path():
    for name in ("../roles/spec.md", "sub/REVIEW.md"):
        with pytest.raises(FactoryError):
            prompts.load_template(name)


def test_load_template_missing_file_names_the_path():
    with pytest.raises(FactoryError) as excinfo:
        prompts.load_template("nope.md")

    assert "nope.md" in excinfo.value.message


def test_every_role_placeholder_is_a_known_placeholder():
    """A typo in a role file would otherwise render as a literal `{foo}` in the prompt."""
    for stage in ROLES:
        for token in re.findall(r"\{([a-z_]+)\}", prompts.load_role(stage)):
            assert token in prompts.PLACEHOLDERS, (
                f"roles/{stage}.md uses unknown placeholder {token!r}"
            )


# ---------------------------------------------------------------- harness-neutral wording


@pytest.mark.parametrize("stage", ["spec", "plan", "review"])
def test_the_read_only_roles_do_not_assume_a_file_reading_tool(stage):
    """Design §8: the same role text drives both harnesses. "Do not run shell commands" is right for
    Claude Code in read mode (allowedTools Read,Grep,Glob) and wrong for Codex, whose `--sandbox
    read-only` gives it no reader but the shell — which is how a live review returned an empty ledger
    it had never read the diff for (deviation 69)."""
    text = prompts.load_role(stage)

    assert "Do not run shell commands" not in text
    assert "do not run anything that writes" in text
    assert "read-only shell commands such as" in text


def test_the_review_role_says_when_to_report_an_incomplete_review():
    """The `complete` flag is only useful if the reviewer is told what it means (deviation 70)."""
    text = prompts.load_role("review")

    assert "Set `complete` true only when you read the whole diff" in text
    assert "empty findings are not approval" in text


# ---------------------------------------------------------------- render


def test_render_replaces_only_known_placeholders():
    template = "spec:\n{spec}\nunknown:\n{nonsense}\n"

    assert (
        prompts.render(template, {"spec": "S", "nonsense": "N"})
        == "spec:\nS\nunknown:\n{nonsense}\n"
    )


def test_render_leaves_json_examples_intact():
    template = (
        'Answer with {"markdown": str, "open_questions": [str]} and a set {1, 2}.\nSpec: {spec}\n'
    )

    rendered = prompts.render(template, {"spec": "S"})

    assert '{"markdown": str, "open_questions": [str]}' in rendered
    assert "{1, 2}" in rendered
    assert rendered.endswith("Spec: S\n")


def test_render_missing_and_blank_values_are_none():
    template = "{intent}|{spec}|{plan}"

    assert prompts.render(template, {"spec": "", "plan": "   \n"}) == "(none)|(none)|(none)"


def test_render_stringifies_the_issue_number():
    assert prompts.render("issue {issue}", {"issue": 42}) == "issue 42"


def test_render_does_not_reinterpret_braces_or_backslashes_in_values():
    value = r'{"a": 1} \n \g<0> {spec}'

    assert prompts.render("{plan}", {"plan": value}) == value


def test_render_replaces_every_occurrence():
    assert prompts.render("{issue} and {issue}", {"issue": "7"}) == "7 and 7"


def test_render_role_fills_the_real_build_role():
    values = {
        "issue": 42,
        "plan": "## Files that change\n- src/app.py",
        "spec": "the spec body",
        "checks": "$ make test\n[exit 0]",
        "stage_note": "Round 1.",
    }

    rendered = prompts.render_role("build", values)

    assert "issue 42" in rendered
    assert "## Files that change\n- src/app.py" in rendered
    assert "the spec body" in rendered
    assert "$ make test\n[exit 0]" in rendered
    assert "work/42/plan.md" in rendered  # the role's own {issue} interpolation
    for token in ("{issue}", "{plan}", "{spec}", "{checks}", "{stage_note}"):
        assert token not in rendered


def test_render_role_unfilled_placeholders_become_none():
    rendered = prompts.render_role("review", {"issue": 42})

    assert "(none)" in rendered
    for token in (
        "{spec}",
        "{plan}",
        "{diff}",
        "{ledger}",
        "{review_policy}",
        "{checks}",
        "{stage_note}",
    ):
        assert token not in rendered


# ---------------------------------------------------------------- writing the prompt


def test_write_prompt_creates_directories_and_returns_the_path(tmp_path):
    path = prompts.write_prompt(tmp_path, 42, "review", 2, "prompt body")

    assert path == prompts.prompt_path(tmp_path, 42, "review", 2)
    assert path == tmp_path / "work" / "42" / "prompts" / "review-2.md"
    assert path.read_text(encoding="utf-8") == "prompt body\n"


def test_write_prompt_overwrites_and_keeps_one_trailing_newline(tmp_path):
    prompts.write_prompt(tmp_path, 42, "build", 1, "first\n")
    path = prompts.write_prompt(tmp_path, 42, "build", 1, "second\n")

    assert path.read_text(encoding="utf-8") == "second\n"
    assert sorted(p.name for p in path.parent.iterdir()) == ["build-1.md"]


# ---------------------------------------------------------------- findings for fix


def finding(**overrides) -> dict:
    base = {
        "id": "F3",
        "key": "abc",
        "pass": "bugs",
        "severity": "important",
        "file": "svc/x.py",
        "line": 41,
        "title": "Off-by-one in the retry bound",
        "detail": "The loop stops one short.",
        "evidence": "+ for i in range(n - 1):",
        "opened_round": 1,
        "status": "open",
        "status_round": None,
        "status_evidence": None,
        "dismissed_reason": None,
    }
    base.update(overrides)
    return base


def test_format_findings_for_fix_empty():
    assert prompts.format_findings_for_fix([]) == "(none)"


def test_format_findings_for_fix_numbers_every_finding():
    text = prompts.format_findings_for_fix(
        [finding(), finding(id="F5", line=None, file="svc/y.py")]
    )

    assert text.startswith("1. **F3** — svc/x.py:41 — Off-by-one in the retry bound\n")
    assert "2. **F5** — svc/y.py — Off-by-one in the retry bound\n" in text
    assert "- detail: The loop stops one short." in text
    assert "- evidence: + for i in range(n - 1):" in text


def test_format_findings_for_fix_indents_multiline_evidence():
    text = prompts.format_findings_for_fix([finding(evidence="line one\nline two")])

    assert "   - evidence: line one\n     line two" in text


def test_format_findings_for_fix_tolerates_missing_fields():
    text = prompts.format_findings_for_fix([{"id": "F1"}])

    assert "1. **F1** — (no file) — (no title)" in text
    assert "- detail: (none)" in text


# ---------------------------------------------------------------- ledger for review


def test_format_ledger_for_review_empty():
    assert prompts.format_ledger_for_review([]) == "(empty — first round)"


def test_format_ledger_for_review_flags_open_findings():
    ledger = [
        finding(),
        finding(
            id="F4",
            severity="nit",
            status="open",
            title="Log line is noisy",
            **{"pass": "security"},
        ),
        finding(id="F5", status="resolved", title="Null deref"),
        finding(id="F6", status="dismissed", title="Style choice"),
    ]

    text = prompts.format_ledger_for_review(ledger).splitlines()

    assert (
        text[0]
        == "- F3 [open · important · bugs] svc/x.py:41 — Off-by-one in the retry bound  **NEEDS UPDATE**"
    )
    assert text[1].startswith(
        "- F4 [open · nit · security] svc/x.py:41 — Log line is noisy  **NEEDS UPDATE**"
    )
    assert text[2] == "- F5 [resolved · important · bugs] svc/x.py:41 — Null deref"
    assert text[3] == "- F6 [dismissed · important · bugs] svc/x.py:41 — Style choice"
    assert "NEEDS UPDATE" not in text[2] and "NEEDS UPDATE" not in text[3]


def test_format_ledger_for_review_accepts_the_serialized_pass_key():
    """state.Finding stores the pass as `pass_` and serializes it as "pass"; both must render."""
    assert "· bugs]" in prompts.format_ledger_for_review([finding()])
    assert "· bugs]" in prompts.format_ledger_for_review(
        [{"id": "F1", "pass_": "bugs", "status": "open"}]
    )


def test_ledger_and_findings_survive_rendering_into_the_review_role():
    ledger = prompts.format_ledger_for_review([finding()])
    rendered = prompts.render_role("review", {"issue": 42, "ledger": ledger})

    assert ledger in rendered


# ---------------------------------------------------------------- diff description


def test_describe_diff_names_path_bytes_lines_and_the_read_instruction():
    """The instruction must fit either harness (deviation 69): this text sits in the same prompt as the
    review role and is the sentence nearest the diff's path, so a file-tool-only phrasing here would
    re-create the dogfood bug the role wording fixed."""
    text = prompts.describe_diff(".factory/tmp/review-1.diff", 12_345, 678, False, [])

    assert ".factory/tmp/review-1.diff" in text
    assert "12345 bytes" in text
    assert "678 lines" in text
    assert "line 678" in text
    assert "offset/limit" in text  # for a harness whose reader is a file tool
    assert "sed -n" in text  # for one whose only reader is a read-only shell
    assert "TRUNCATED" not in text


def test_describe_diff_truncated_banner_lists_the_omitted_files():
    text = prompts.describe_diff(
        ".factory/tmp/review-2.diff", 300_000, 9_000, True, ["big/a.py", "big/b.py"]
    )

    assert "TRUNCATED" in text
    assert "big/a.py, big/b.py" in text
    assert "must not raise findings about them" in text


def test_describe_diff_truncated_without_named_files():
    text = prompts.describe_diff(".factory/tmp/review-2.diff", 300_000, 9_000, True, [])

    assert "TRUNCATED" in text
    assert "NOT in the file" not in text


def test_describe_diff_is_safe_inside_a_rendered_prompt():
    described = prompts.describe_diff(".factory/tmp/review-1.diff", 10, 2, False, [])
    rendered = prompts.render_role("review", {"issue": 42, "diff": described})

    assert described in rendered


def test_findings_may_be_state_finding_dataclasses():
    """The ledger holds dataclasses; a caller that skips .to_dict() still gets a prompt."""

    @dataclasses.dataclass
    class FakeFinding:
        id: str
        pass_: str
        severity: str
        file: str
        line: int | None
        title: str
        detail: str
        evidence: str
        status: str

    item = FakeFinding(
        "F9",
        "security",
        "important",
        "svc/z.py",
        7,
        "Token in the log",
        "It logs the token.",
        "+ log.info(token)",
        "open",
    )

    assert "1. **F9** — svc/z.py:7 — Token in the log" in prompts.format_findings_for_fix([item])
    assert prompts.format_ledger_for_review([item]) == (
        "- F9 [open · important · security] svc/z.py:7 — Token in the log  **NEEDS UPDATE**"
    )


def test_a_finding_that_is_neither_dict_nor_dataclass_is_an_error():
    with pytest.raises(FactoryError):
        prompts.format_ledger_for_review(["F1"])
