"""Loading of orch.toml (conventions.md section 7)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import repo_root

DEFAULTS: dict[str, Any] = {
    "roles": {
        "contractor": "claude",
        "implementer": "claude",
        "auditor": "claude",
        "reviewer": "codex",
        "judge": "codex",
        "remediator": "claude",
    },
    "policy": {"review_round_cap": 2, "audit_failure_cap": 3, "max_steps": 40},
    "runners": {
        "claude": {"model": "opus", "extra_args": []},
        "codex": {"model": "", "sandbox": "danger-full-access", "extra_args": []},
    },
}


class ConfigError(ValueError):
    """The config file is missing a required value or is malformed."""


@dataclass(frozen=True)
class Config:
    """Effective configuration: role -> runner, loop policy, per-runner flags."""

    roles: dict[str, str] = field(default_factory=lambda: dict(DEFAULTS["roles"]))
    policy: dict[str, int] = field(default_factory=lambda: dict(DEFAULTS["policy"]))
    runners: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULTS["runners"].items()}
    )

    @property
    def review_round_cap(self) -> int:
        """Maximum number of review rounds before escalation."""
        return int(self.policy["review_round_cap"])

    @property
    def audit_failure_cap(self) -> int:
        """Consecutive failed audits that trigger escalation."""
        return int(self.policy["audit_failure_cap"])

    @property
    def max_steps(self) -> int:
        """Maximum transitions a single `orch run` will perform."""
        return int(self.policy["max_steps"])

    def runner_name(self, role: str) -> str:
        """Runner name configured for a role key."""
        try:
            return self.roles[role]
        except KeyError:
            raise ConfigError(f"no runner configured for role '{role}'") from None

    def runner_settings(self, name: str) -> dict[str, Any]:
        """Settings block for a runner name."""
        return self.runners.get(name, {})


def config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the config path: --config, then $ORCH_CONFIG, then the orchestrator's orch.toml."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("ORCH_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return repo_root() / "orch.toml"


def load_config(explicit: str | Path | None = None) -> Config:
    """Load configuration, layering the file's sections over the built-in defaults."""
    path = config_path(explicit)
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    merged["runners"] = {k: dict(v) for k, v in DEFAULTS["runners"].items()}
    if not path.exists():
        if explicit or os.environ.get("ORCH_CONFIG"):
            raise ConfigError(f"config file not found: {path}")
        return Config(merged["roles"], merged["policy"], merged["runners"])
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    merged["roles"].update(data.get("roles", {}))
    merged["policy"].update(data.get("policy", {}))
    for name, settings in data.get("runners", {}).items():
        merged["runners"].setdefault(name, {}).update(settings)
    return Config(merged["roles"], merged["policy"], merged["runners"])
