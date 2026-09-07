"""factory.schema — the strict-intersection guard over schemas/ and the factory's own output re-check.

Design §8 ("the factory re-checks required keys and enums after the CLI's own schema enforcement") and §10
(the five role schemas). The strict-intersection test is the one that matters: codex --output-schema rejects
constraint keywords, so a bad schema would otherwise only surface under --harness codex.
"""

from __future__ import annotations

import json

import pytest

from factory.errors import FactoryError
from factory.schema import (
    SCHEMA_NAMES,
    SCHEMAS_DIR,
    dumps_compact,
    load_schema,
    schema_path,
    strict_intersection_violations,
    validate,
    validate_or_raise,
)

# One sample output per §10 role schema, shaped exactly as the design specifies.
SAMPLES: dict[str, dict] = {
    "spec": {
        "markdown": "## Problem\nThe importer drops rows.\n\n## Acceptance criteria\n- no dropped rows",
        "open_questions": [],
    },
    "plan": {
        "markdown": "## Files that change\n- src/importer.py\n\n## Proof\n- `make test`: the new case passes",
    },
    "build": {
        "summary": "Added a row counter and a regression test; make test and make lint are green.",
        "deviations": ["The plan named importer.py only; the fix also needed a change in io.py."],
    },
    "review": {
        "summary": "The diff adds a counter and a test. Bugs: one important finding. Security: none.",
        "complete": True,
        "updates": [
            {"id": "F1", "status": "resolved", "evidence": "src/importer.py:41 now guards None"}
        ],
        "new": [
            {
                "severity": "important",
                "pass": "bugs",
                "file": "src/importer.py",
                "line": 41,
                "title": "Counter is not reset between batches",
                "detail": "A second batch inherits the first batch's count, so the guard never fires.",
                "evidence": "+ self.count += 1  (no reset in start_batch)",
            },
            {
                "severity": "nit",
                "pass": "compliance",
                "file": "src/io.py",
                "line": None,
                "title": "Docstring does not mention the new flag",
                "detail": "plan.md says the flag is documented.",
                "evidence": "src/io.py docstring unchanged in the diff",
            },
        ],
    },
    "fix": {
        "addressed": [
            {"id": "F2", "how": "Reset the counter in start_batch (src/importer.py:33)."}
        ],
        "not_addressed": [
            {"id": "F3", "why": "The fix needs a schema decision a human must make."}
        ],
    },
    "probe": {"ok": True, "note": "wrote probe.txt"},
}


def test_schema_names_cover_the_directory():
    on_disk = sorted(path.stem for path in SCHEMAS_DIR.glob("*.json"))
    assert on_disk == sorted(SCHEMA_NAMES)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_shipped_schema_is_inside_the_strict_intersection(name):
    """Every schemas/*.json must stay inside what BOTH CLIs accept (module docstring)."""
    schema = json.loads(schema_path(name).read_text(encoding="utf-8"))
    assert strict_intersection_violations(schema) == []


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_load_schema_returns_the_file(name):
    assert load_schema(name) == json.loads(schema_path(name).read_text(encoding="utf-8"))


def test_load_schema_rejects_an_unknown_name():
    with pytest.raises(FactoryError) as excinfo:
        load_schema("intent")
    assert "unknown schema 'intent'" in excinfo.value.message
    assert "spec" in excinfo.value.message


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_sample_output_validates(name):
    assert validate(SAMPLES[name], load_schema(name)) == []


# --- malformed outputs: readable violations ------------------------------------------------------


def _review(**overrides) -> dict:
    sample = json.loads(json.dumps(SAMPLES["review"]))
    sample.update(overrides)
    return sample


def test_missing_key_is_reported_by_name():
    broken = _review()
    del broken["updates"]
    assert validate(broken, load_schema("review")) == ["$: missing required key 'updates'"]


def test_complete_is_required_and_must_be_boolean():
    """The reviewer must state whether it actually reviewed: Python, not prose, owns the verdict,
    and `stages.review` gates on `complete` (deviations 69-70)."""
    broken = _review()
    del broken["complete"]
    assert validate(broken, load_schema("review")) == ["$: missing required key 'complete'"]
    assert validate(_review(complete="true"), load_schema("review")) == [
        "$.complete: expected boolean, got string"
    ]
    assert validate(_review(complete=False), load_schema("review")) == []


def test_missing_nested_key_is_reported_with_its_path():
    broken = _review(updates=[{"id": "F1", "status": "resolved"}])
    assert validate(broken, load_schema("review")) == [
        "$.updates[0]: missing required key 'evidence'"
    ]


def test_bad_enum_names_the_value_and_the_options():
    broken = _review(updates=[{"id": "F1", "status": "fixed", "evidence": "e"}])
    violations = validate(broken, load_schema("review"))
    assert violations == ["$.updates[0].status: 'fixed' is not one of ['resolved', 'unresolved']"]


def test_bad_enum_in_a_new_finding():
    finding = {**SAMPLES["review"]["new"][0], "severity": "blocker", "pass": "style"}
    violations = validate(_review(new=[finding]), load_schema("review"))
    assert "$.new[0].severity: 'blocker' is not one of ['important', 'nit']" in violations
    assert "$.new[0].pass: 'style' is not one of ['bugs', 'security', 'compliance']" in violations


def test_wrong_type_names_what_was_expected_and_what_arrived():
    assert validate({"markdown": 12, "open_questions": []}, load_schema("spec")) == [
        "$.markdown: expected string, got integer"
    ]
    assert validate({"markdown": "x", "open_questions": "none"}, load_schema("spec")) == [
        "$.open_questions: expected array, got string"
    ]
    assert validate({"markdown": "x", "open_questions": [1]}, load_schema("spec")) == [
        "$.open_questions[0]: expected string, got integer"
    ]
    assert validate([], load_schema("spec")) == ["$: expected object, got array"]


def test_extra_key_is_rejected_because_additional_properties_is_false():
    broken = _review(unexpected="x")
    assert validate(broken, load_schema("review")) == [
        "$: unexpected key 'unexpected' (additionalProperties is false)"
    ]


def test_nullable_line_accepts_a_type_union_and_rejects_anything_else():
    schema = load_schema("review")
    for line, expected in ((7, []), (None, [])):
        finding = dict(SAMPLES["review"]["new"][0], line=line)
        assert validate(_review(new=[finding]), schema) == expected
    finding = dict(SAMPLES["review"]["new"][0], line="41")
    assert validate(_review(new=[finding]), schema) == [
        "$.new[0].line: expected integer or null, got string"
    ]


def test_booleans_are_not_integers_and_integers_are_not_booleans():
    finding = dict(SAMPLES["review"]["new"][0], line=True)
    assert validate(_review(new=[finding]), load_schema("review")) == [
        "$.new[0].line: expected integer or null, got boolean"
    ]
    assert validate({"ok": 1, "note": "n"}, load_schema("probe")) == [
        "$.ok: expected boolean, got integer"
    ]


def test_several_violations_are_all_reported():
    broken = {"summary": 1, "updates": [{"id": "F1", "status": "fixed", "evidence": "e"}]}
    violations = validate(broken, load_schema("review"))
    assert "$: missing required key 'new'" in violations
    assert "$.summary: expected string, got integer" in violations
    assert "$.updates[0].status: 'fixed' is not one of ['resolved', 'unresolved']" in violations


@pytest.mark.parametrize("instance", [None, "text", 3, [], {"a": {"b": [1, None]}}, True])
def test_validate_never_raises_on_a_hostile_instance(instance):
    assert isinstance(validate(instance, load_schema("review")), list)


def test_validate_or_raise_is_silent_on_a_valid_output():
    validate_or_raise(SAMPLES["fix"], load_schema("fix"), "fix round 1")


def test_validate_or_raise_names_the_stage_and_lists_the_violations():
    with pytest.raises(FactoryError) as excinfo:
        validate_or_raise({"addressed": []}, load_schema("fix"), "fix round 2")
    assert excinfo.value.message == "fix round 2: schema-invalid output"
    assert "missing required key 'not_addressed'" in (excinfo.value.hint or "")


# --- keywords the factory may use after the fact, but never in a shipped schema ------------------


def test_min_length_and_min_items_are_understood_by_the_validator():
    assert validate("", {"type": "string", "minLength": 1}) == [
        "$: string of length 0 is shorter than minLength 1"
    ]
    assert validate("x", {"type": "string", "minLength": 1}) == []
    assert validate([], {"type": "array", "minItems": 1, "items": {"type": "string"}}) == [
        "$: array of 0 items is shorter than minItems 1"
    ]
    assert validate(["x"], {"type": "array", "minItems": 1, "items": {"type": "string"}}) == []


@pytest.mark.parametrize(
    ("schema", "needle"),
    [
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {"type": "string", "minLength": 1}},
                "required": ["a"],
            },
            "keyword 'minLength' is outside the strict structured-output intersection",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {"oneOf": [{"type": "string"}]}},
                "required": ["a"],
            },
            "keyword 'oneOf' is outside the strict structured-output intersection",
        ),
        (
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            'an object must set "additionalProperties": false',
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                "required": ["a"],
            },
            "$: 'required' omits b",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {"type": "string"}},
                "required": ["a", "z"],
            },
            "$: 'required' names undeclared z",
        ),
        (
            {"type": "array", "items": {"type": "string"}},
            'the root schema must be a plain {"type": "object"}',
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {"description": "no type"}},
                "required": ["a"],
            },
            "$.a: every schema node must declare a type",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {"type": "strng"}},
                "required": ["a"],
            },
            "$.a: unknown type 'strng'",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {"type": "array"}},
                "required": ["a"],
            },
            "$.a: an array must declare 'items' as a schema object",
        ),
        (
            {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
            "an object must declare a non-empty 'properties' map",
        ),
        (
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"a": {"type": "string", "enum": []}},
                "required": ["a"],
            },
            "$.a: 'enum' must be a non-empty array",
        ),
    ],
)
def test_strict_intersection_catches_what_codex_would_reject(schema, needle):
    violations = strict_intersection_violations(schema)
    assert any(needle in violation for violation in violations), violations


def test_strict_intersection_reports_a_non_object_node():
    assert strict_intersection_violations(["nope"]) == [
        "$: schema node must be a JSON object, got array"
    ]


def test_nested_objects_and_arrays_are_walked():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"id": {"type": "string", "pattern": "^F"}},
                    "required": ["id"],
                },
            }
        },
        "required": ["items"],
    }
    violations = strict_intersection_violations(schema)
    assert violations == [
        "$.items.items.id: keyword 'pattern' is outside the strict structured-output intersection"
    ]


def test_dumps_compact_is_stable_and_command_line_safe():
    schema = load_schema("probe")
    rendered = dumps_compact(schema)
    assert json.loads(rendered) == schema
    assert rendered == dumps_compact(json.loads(rendered))  # key order is stable across calls
    assert '","' in rendered and '":{' in rendered  # no whitespace between JSON tokens
    assert "\n" not in rendered
