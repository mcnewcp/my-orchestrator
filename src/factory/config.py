"""Read the invoking checkout's configuration once."""

import copy
import tomllib
from pathlib import Path


ROLES = ("spec", "plan", "build", "review", "fix")
RUN_FIELDS = ("harness", "model", "effort")
EFFORTS = ("", "low", "medium", "high", "max")


DEFAULTS = {
    "factory": {
        "harness": "claude", "auth": "api", "base_branch": "main",
        "model": "", "effort": "",
        "max_fix_rounds": 3, "stage_timeout_min": 45,
        "checks": [["make", "test"], ["make", "lint"]],
        "test_paths": ["tests/"], "protected_paths": [],
    },
    "poll": {"label": "factory", "max_consecutive_failures": 3},
    "roles": {role: {} for role in ROLES},
}

TEMPLATE = '''[factory]
harness = "claude"
model = "" # empty model/effort uses the harness CLI default
effort = ""
auth = "api" # use "subscription" for attended runs with a saved login
base_branch = "main"
max_fix_rounds = 3
stage_timeout_min = 45
checks = [["make", "test"], ["make", "lint"]]
test_paths = ["tests/"]
protected_paths = []

[poll]
label = "factory"
max_consecutive_failures = 3

[roles.review]
# model = "opus"
# effort = "high"

[roles.build]
# harness = "codex"
'''


def resolve_role(config: dict, role: str | None = None, *, harness=None,
                 model=None, effort=None) -> dict:
    """Resolve each field independently; explicit empty strings stop inheritance."""
    if role is not None and role not in ROLES:
        raise ValueError(f"unknown configuration key: roles.{role}")
    defaults = config.get("factory", {})
    settings = config.get("roles", {}).get(role, {}) if role else {}
    overrides = dict(harness=harness, model=model, effort=effort)
    resolved, keys = {}, {}
    for field in RUN_FIELDS:
        if overrides[field] is not None:
            value, key = overrides[field], f"--{field}"
        elif field in settings:
            value, key = settings[field], f"roles.{role}.{field}"
        else:
            value, key = defaults.get(field, DEFAULTS["factory"][field]), f"factory.{field}"
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        resolved[field], keys[field] = value, key
    if resolved["harness"] not in ("claude", "codex"):
        raise ValueError(f"{keys['harness']} must be claude or codex")
    if resolved["effort"] not in EFFORTS:
        raise ValueError(f"{keys['effort']} must be empty, low, medium, high, or max (Claude only)")
    if resolved["effort"] == "max" and resolved["harness"] != "claude":
        context = f" for roles.{role}" if role else ""
        raise ValueError(f"{keys['effort']}{context}: max is supported only by claude, not {resolved['harness']}")
    return resolved


def harness_settings(config: dict, **overrides) -> dict:
    """One probe per harness: factory settings first, then the first using role."""
    selected = {}
    for role in (None, *ROLES):
        settings = resolve_role(config, role, **overrides)
        selected.setdefault(settings["harness"], settings)
    return selected


def load_config(root: Path, *, optional=False) -> dict:
    path = root / "factory.toml"
    if not path.exists() and not optional:
        raise ValueError("factory.toml is missing; run factory init first")
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    config = copy.deepcopy(DEFAULTS)

    def merge(target, incoming, prefix=""):
        for key, value in incoming.items():
            role_field = prefix in (f"roles.{role}." for role in ROLES) and key in RUN_FIELDS
            if key not in target and not role_field:
                raise ValueError(f"unknown configuration key: {prefix}{key}")
            if isinstance(target.get(key), dict):
                if not isinstance(value, dict):
                    raise ValueError(f"expected table: {prefix}{key}")
                merge(target[key], value, f"{prefix}{key}.")
            else:
                target[key] = value

    merge(config, raw)
    f = config["factory"]
    resolve_role(config)
    config["roles"] = {role: resolve_role(config, role) for role in ROLES}
    if f["auth"] not in ("api", "subscription"):
        raise ValueError("factory.auth must be api or subscription")
    for name in ("max_fix_rounds", "stage_timeout_min"):
        if type(f[name]) is not int or f[name] < (0 if name == "max_fix_rounds" else 1):
            raise ValueError(f"factory.{name} must be a valid nonnegative integer")
    if not isinstance(f["base_branch"], str) or not f["base_branch"] or f["base_branch"].startswith("-"):
        raise ValueError("factory.base_branch must name a branch")
    commands = f["checks"]
    if not isinstance(commands, list) or not commands:
        raise ValueError("factory.checks must contain at least one argv array")
    for command in commands:
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError("each check must be a nonempty argv array of strings")
    for key in ("test_paths", "protected_paths"):
        if not isinstance(f[key], list) or any(not isinstance(p, str) or not p or p.startswith("/") or ".." in Path(p).parts for p in f[key]):
            raise ValueError(f"factory.{key} must contain relative paths")
    poll = config["poll"]
    if type(poll["max_consecutive_failures"]) is not int or poll["max_consecutive_failures"] < 1:
        raise ValueError("poll.max_consecutive_failures must be positive")
    if not isinstance(poll["label"], str) or not poll["label"].strip():
        raise ValueError("poll.label must be nonempty")
    return config
