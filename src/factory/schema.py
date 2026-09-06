"""Minimal JSON-Schema validator (stdlib only) for the factory's own output schemas (design §8, §10).

schemas/*.json MUST stay inside the strict-structured-output intersection BOTH CLIs accept:
object types only, "additionalProperties": false on every object, "required" listing every property,
nullability written as a type union ("type": ["integer", "null"]), and NO constraint keywords —
no minLength, minItems, pattern, format, oneOf/allOf. codex --output-schema rejects them; claude --json-schema
does not, so a violation would only show up under --harness codex. tests/test_schema.py asserts this.

validate() understands type (incl. type unions), properties, required, additionalProperties (bool), items, enum,
minLength, minItems — the last two only so the FACTORY can enforce extra shape after the fact (the §9 "non-empty
markdown" gate is a Python check in stages.spec, never a keyword in a file handed to a CLI).
Both CLIs enforce the schema themselves; this is the factory's independent re-check ("re-checks required keys
and enums", §8).
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMAS_DIR = Path(__file__).parent / "schemas"
SCHEMA_NAMES = ("spec", "plan", "build", "review", "fix", "probe")


def load_schema(name: str) -> dict:
    """Load `schemas/<name>.json` (name in SCHEMA_NAMES); FactoryError for an unknown name."""
    raise NotImplementedError


def schema_path(name: str) -> Path:
    return SCHEMAS_DIR / f"{name}.json"


def validate(instance, schema: dict, path: str = "$") -> list[str]:
    """Return a list of human-readable violations (empty list == valid). Never raises on bad instances."""
    raise NotImplementedError


def validate_or_raise(instance, schema: dict, what: str) -> None:
    """Raise FactoryError(f"{what}: schema-invalid output") listing the violations."""
    raise NotImplementedError


def strict_intersection_violations(schema: dict, path: str = "$") -> list[str]:
    """Return violations of the strict-structured-output intersection described in the module docstring
    (used by tests over every file in schemas/)."""
    raise NotImplementedError


def dumps_compact(obj) -> str:
    """json.dumps with separators=(",", ":") — used to pass a schema on the Claude Code command line."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)
