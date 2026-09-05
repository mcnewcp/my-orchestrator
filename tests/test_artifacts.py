"""Artifact parsing: the flat YAML front-matter subset and shape validation."""

from __future__ import annotations

import json

import pytest

from conftest import HEAD, write_contract, write_review
from orch.artifacts import (ArtifactError, ensure_run, parse_front_matter, read_audit,
                            read_contract, read_ledger, read_review_front, split_sections)


def test_contract_front_matter_round_trip(scratch):
    write_contract(scratch)
    contract = read_contract(scratch)
    assert contract.issue == 17
    assert contract.title == "Add percent_change helper"
    assert contract.test_budget == 12
    assert contract.scope_paths == ["src/**", "tests/**"]
    assert contract.commands == {"test": "uv run pytest -q", "lint": "uv run ruff check ."}
    assert contract.summary == "Adds a percent_change helper."
    assert "AC-1" in contract.acceptance_criteria


def test_contract_with_omitted_command_keys(scratch):
    write_contract(scratch, commands='  test: "pytest"')
    assert read_contract(scratch).commands == {"test": "pytest"}


def test_contract_tolerates_crlf_and_trailing_whitespace(scratch):
    path = write_contract(scratch)
    path.write_bytes(path.read_text().replace("\n", "   \r\n").encode("utf-8"))
    assert read_contract(scratch).commands["test"] == "uv run pytest -q"


def test_contract_requires_a_test_command(scratch):
    write_contract(scratch, commands='  lint: "ruff check ."')
    with pytest.raises(ArtifactError, match="must include a 'test' command"):
        read_contract(scratch)


@pytest.mark.parametrize(
    "front,message",
    [
        ('issue: 17\ntitle: "t"\ntest_budget: 12\ncommands:\n  test: "x"', "scope_paths"),
        ('title: "t"\ntest_budget: 12\nscope_paths: []\ncommands:\n  test: "x"', "'issue'"),
        ('issue: 17\ntitle: "t"\ntest_budget: many\nscope_paths: []\ncommands:\n  test: "x"',
         "test_budget"),
    ],
)
def test_contract_shape_errors_name_the_file(scratch, front, message):
    (scratch / "contract.md").write_text(f"---\n{front}\n---\n## Summary\n", encoding="utf-8")
    with pytest.raises(ArtifactError) as excinfo:
        read_contract(scratch)
    assert "contract.md" in str(excinfo.value) and message in str(excinfo.value)


def test_unescaped_quote_inside_a_quoted_scalar_is_an_error(scratch):
    write_contract(scratch, title='Fix "off-by-one" in parser')
    with pytest.raises(ArtifactError) as excinfo:
        read_contract(scratch)
    assert "contract.md" in str(excinfo.value) and "escaped" in str(excinfo.value)


def test_escaped_quotes_inside_a_title_round_trip(scratch):
    write_contract(scratch, title='Fix \\"off-by-one\\" in parser')
    assert read_contract(scratch).title == 'Fix "off-by-one" in parser'


def test_missing_front_matter_is_an_error(scratch):
    (scratch / "contract.md").write_text("## Summary\nno front matter\n", encoding="utf-8")
    with pytest.raises(ArtifactError, match="expected YAML front matter"):
        read_contract(scratch)


def test_front_matter_parses_comments_quotes_and_lists():
    front, body = parse_front_matter(
        '---\n'
        'n: 3   # a count\n'
        'flag: true\n'
        'empty: null\n'
        'text: "has: a colon and # a hash"\n'
        'items: ["a", "b"]  # trailing comment\n'
        'commands:\n'
        '  test: "pytest -q"\n'
        '\n'
        '  lint: "ruff check ."\n'
        '---\nbody\n',
        "x.md",
    )
    assert front == {
        "n": 3, "flag": True, "empty": None,
        "text": "has: a colon and # a hash",
        "items": ["a", "b"],
        "commands": {"test": "pytest -q", "lint": "ruff check ."},
    }
    assert body.strip() == "body"


def test_split_sections_keeps_order_and_text():
    sections = split_sections("## A\nfirst\n\n## B\nsecond\n")
    assert list(sections) == ["A", "B"]
    assert sections["B"] == "second"


def test_review_front_matter(scratch):
    path = write_review(scratch, 2, verdict="REQUEST_CHANGES")
    front = read_review_front(path)
    assert front["verdict"] == "REQUEST_CHANGES"
    assert front["commit"] == HEAD
    assert front["round"] == 2


def test_review_rejects_an_unknown_verdict(scratch):
    path = scratch / "review-1.md"
    path.write_text(f"---\nverdict: LGTM\ncommit: {HEAD}\n---\n", encoding="utf-8")
    with pytest.raises(ArtifactError, match="APPROVE or REQUEST_CHANGES"):
        read_review_front(path)


def test_audit_shape(scratch):
    path = scratch / "audit-1.json"
    path.write_text(json.dumps({"pass": "yes", "commit": HEAD}), encoding="utf-8")
    with pytest.raises(ArtifactError, match="'pass' must be a boolean"):
        read_audit(path)


def test_ledger_defaults_and_shape(scratch):
    (scratch / "ledger.json").write_text("{}", encoding="utf-8")
    assert read_ledger(scratch) == {"rounds_completed": 0, "findings": []}
    (scratch / "ledger.json").write_text('{"findings": 3}', encoding="utf-8")
    with pytest.raises(ArtifactError, match="'findings' must be a list"):
        read_ledger(scratch)


def test_ensure_run_is_idempotent(scratch):
    first = ensure_run(scratch, 17)
    assert first["issue"] == 17 and first["pr_number"] is None and first["created_at"]
    assert ensure_run(scratch, 17) == first
