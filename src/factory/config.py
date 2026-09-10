"""Read the invoking checkout's configuration once."""

import copy
import tomllib
from pathlib import Path


DEFAULTS = {
    "factory": {
        "harness": "claude", "auth": "api", "base_branch": "main",
        "max_fix_rounds": 3, "stage_timeout_min": 45,
        "checks": [["make", "test"], ["make", "lint"]],
        "test_paths": ["tests/"], "protected_paths": [],
    },
    "poll": {"label": "factory", "max_consecutive_failures": 3},
    "harness": {
        "claude": {"model": ""},
        "codex": {"model": ""},
    },
}

TEMPLATE = '''[factory]
harness = "claude"
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

[harness.claude]
model = ""

[harness.codex]
model = ""
'''


def load_config(root: Path, *, optional=False) -> dict:
    path = root / "factory.toml"
    if not path.exists() and not optional:
        raise ValueError("factory.toml is missing; run factory init first")
    raw = tomllib.loads(path.read_text()) if path.exists() else {}
    config = copy.deepcopy(DEFAULTS)

    def merge(target, incoming, prefix=""):
        for key, value in incoming.items():
            if key not in target:
                raise ValueError(f"unknown configuration key: {prefix}{key}")
            if isinstance(target[key], dict):
                if not isinstance(value, dict):
                    raise ValueError(f"expected table: {prefix}{key}")
                merge(target[key], value, f"{prefix}{key}.")
            else:
                target[key] = value

    merge(config, raw)
    f = config["factory"]
    if f["harness"] not in ("claude", "codex"):
        raise ValueError("factory.harness must be claude or codex")
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
    for h in config["harness"].values():
        if any(not isinstance(v, str) for v in h.values()):
            raise ValueError("harness model must be a string")
    return config
