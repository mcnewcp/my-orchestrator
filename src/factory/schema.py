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

from .errors import FactoryError

SCHEMAS_DIR = Path(__file__).parent / "schemas"
SCHEMA_NAMES = ("spec", "plan", "build", "review", "fix", "probe")

#: The JSON type names the validator understands.
KNOWN_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")

#: The only keywords a shipped schema may use. Everything else — minLength, minItems, pattern, format,
#: oneOf/allOf/anyOf, const, default, $ref — is outside the intersection both CLIs accept.
STRICT_KEYWORDS = frozenset(
    {
        "type",
        "description",
        "title",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
    }
)


def load_schema(name: str) -> dict:
    """Load `schemas/<name>.json` (name in SCHEMA_NAMES); FactoryError for an unknown name."""
    if name not in SCHEMA_NAMES:
        raise FactoryError(f"unknown schema {name!r}; expected one of {', '.join(SCHEMA_NAMES)}")
    path = schema_path(name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FactoryError(f"cannot read schema {path}: {exc}") from exc
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FactoryError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise FactoryError(f"{path}: schema must be a JSON object, got {_typename(loaded)}")
    return loaded


def schema_path(name: str) -> Path:
    return SCHEMAS_DIR / f"{name}.json"


def validate(instance, schema: dict, path: str = "$") -> list[str]:
    """Return a list of human-readable violations (empty list == valid). Never raises on bad instances."""
    if not isinstance(schema, dict):
        return []
    violations: list[str] = []

    declared = _declared_types(schema)
    if declared and not any(_matches_type(instance, name) for name in declared):
        return [f"{path}: expected {' or '.join(declared)}, got {_typename(instance)}"]

    if "enum" in schema and not _enum_contains(schema["enum"], instance):
        options = ", ".join(repr(option) for option in schema["enum"])
        violations.append(f"{path}: {instance!r} is not one of [{options}]")

    if isinstance(instance, dict):
        violations.extend(_object_violations(instance, schema, path))
    elif isinstance(instance, list):
        violations.extend(_array_violations(instance, schema, path))
    elif isinstance(instance, str):
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(instance) < minimum:
            violations.append(
                f"{path}: string of length {len(instance)} is shorter than minLength {minimum}"
            )
    return violations


def validate_or_raise(instance, schema: dict, what: str) -> None:
    """Raise FactoryError(f"{what}: schema-invalid output") listing the violations."""
    violations = validate(instance, schema)
    if violations:
        raise FactoryError(
            f"{what}: schema-invalid output",
            hint="\n".join(f"  {violation}" for violation in violations),
        )


def strict_intersection_violations(schema: dict, path: str = "$") -> list[str]:
    """Return violations of the strict-structured-output intersection described in the module docstring
    (used by tests over every file in schemas/)."""
    if not isinstance(schema, dict):
        return [f"{path}: schema node must be a JSON object, got {_typename(schema)}"]

    violations = [
        f"{path}: keyword {key!r} is outside the strict structured-output intersection"
        for key in schema
        if key not in STRICT_KEYWORDS
    ]

    declared = schema.get("type")
    names: list[str] = []
    if declared is None:
        violations.append(f"{path}: every schema node must declare a type")
    elif isinstance(declared, str):
        names = [declared]
    elif isinstance(declared, list) and declared and all(isinstance(n, str) for n in declared):
        names = list(declared)
    else:
        violations.append(
            f"{path}: type must be a string or a non-empty array of strings, got {declared!r}"
        )
    violations.extend(f"{path}: unknown type {n!r}" for n in names if n not in KNOWN_TYPES)

    if path == "$" and names != ["object"]:
        violations.append('$: the root schema must be a plain {"type": "object"}')

    if "object" in names:
        violations.extend(_object_strict_violations(schema, path))
    if "array" in names:
        items = schema.get("items")
        if not isinstance(items, dict):
            violations.append(f"{path}: an array must declare 'items' as a schema object")
        else:
            violations.extend(strict_intersection_violations(items, f"{path}.items"))
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum:
            violations.append(f"{path}: 'enum' must be a non-empty array")
    return violations


def dumps_compact(obj) -> str:
    """json.dumps with separators=(",", ":") — used to pass a schema on the Claude Code command line."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


# --- internals ---------------------------------------------------------------------------------


def _declared_types(schema: dict) -> list[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list):
        return [name for name in declared if isinstance(name, str)]
    return []


def _matches_type(value, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return False


def _typename(value) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _matches_type(value, name):
            return name
    return type(value).__name__


def _enum_contains(options, value) -> bool:
    """Membership that does not treat True as 1 (JSON enums in schemas/ are strings; stay exact anyway)."""
    if not isinstance(options, list):
        return True
    return any(
        option is value or (type(option) is type(value) and option == value) for option in options
    )


def _object_violations(instance: dict, schema: dict, path: str) -> list[str]:
    violations: list[str] = []
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}

    required = schema.get("required")
    if isinstance(required, list):
        violations.extend(
            f"{path}: missing required key {name!r}" for name in required if name not in instance
        )
    if schema.get("additionalProperties") is False:
        violations.extend(
            f"{path}: unexpected key {key!r} (additionalProperties is false)"
            for key in instance
            if key not in properties
        )
    for name, subschema in properties.items():
        if name in instance:
            violations.extend(validate(instance[name], subschema, f"{path}.{name}"))
    return violations


def _array_violations(instance: list, schema: dict, path: str) -> list[str]:
    violations: list[str] = []
    minimum = schema.get("minItems")
    if isinstance(minimum, int) and len(instance) < minimum:
        violations.append(
            f"{path}: array of {len(instance)} items is shorter than minItems {minimum}"
        )
    items = schema.get("items")
    if isinstance(items, dict):
        for i, element in enumerate(instance):
            violations.extend(validate(element, items, f"{path}[{i}]"))
    return violations


def _object_strict_violations(schema: dict, path: str) -> list[str]:
    violations: list[str] = []
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        violations.append(f"{path}: an object must declare a non-empty 'properties' map")
        properties = {}

    if schema.get("additionalProperties") is not False:
        violations.append(f'{path}: an object must set "additionalProperties": false')

    required = schema.get("required")
    if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
        violations.append(f"{path}: an object must list every property name in 'required'")
    else:
        omitted = [name for name in properties if name not in required]
        if omitted:
            violations.append(f"{path}: 'required' omits {', '.join(sorted(omitted))}")
        undeclared = [name for name in required if name not in properties]
        if undeclared:
            violations.append(
                f"{path}: 'required' names undeclared {', '.join(sorted(undeclared))}"
            )

    for name, subschema in properties.items():
        violations.extend(strict_intersection_violations(subschema, f"{path}.{name}"))
    return violations
