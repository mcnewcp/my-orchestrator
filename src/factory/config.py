"""factory.toml (design §14). Read once, from the checkout the command runs in, never from the worktree.

Extensions beyond §14, all with defaults so a §14-only file is valid (recorded in docs/design/deviations.md):
  [factory] max_nits            nit cap enforced as the review gate (§9 "nits ≤ cap"); default 10
  [factory] max_diff_bytes      review diff larger than this is truncated (stat + largest files first + banner)
  [factory] transient_paths     extra tooling droppings ignored by the worktree-clean rule (checks.TRANSIENT_PATHS)
  [factory] env_passthrough     extra environment variable NAMES forwarded to harness and check subprocesses
  [harness.claude] max_turns_read / max_turns_write / max_budget_usd   Claude Code runaway bounds (§8)
  [harness.codex] writable_dirs  extra --add-dir roots for the workspace-write sandbox (e.g. "~/.cache/uv")
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import FactoryError

HARNESSES = ("claude", "codex")
AUTHS = ("api", "subscription")


@dataclass
class HarnessConfig:
    model: str = ""  # empty = CLI default
    pinned_version: str = ""  # doctor warns on mismatch
    max_turns_read: int = 30  # claude only; binds before the 45-minute stage timeout
    max_turns_write: int = 120  # claude only
    max_budget_usd: float = 0.0  # claude only; 0 = no cap (--max-budget-usd)
    writable_dirs: list[str] = field(default_factory=list)  # codex only; --add-dir, "~" expanded


@dataclass
class Config:
    harness: str = "claude"
    auth: str = "api"
    base_branch: str = "main"
    max_fix_rounds: int = 3
    stage_timeout_min: int = 45
    checks: list[list[str]] = field(default_factory=lambda: [["make", "test"], ["make", "lint"]])
    test_paths: list[str] = field(default_factory=lambda: ["tests/"])
    protected_paths: list[str] = field(default_factory=list)
    max_nits: int = 10
    max_diff_bytes: int = 300_000
    transient_paths: list[str] = field(default_factory=list)
    env_passthrough: list[str] = field(default_factory=list)
    poll_label: str = "factory"
    poll_max_consecutive_failures: int = 3
    harnesses: dict[str, HarnessConfig] = field(
        default_factory=lambda: {"claude": HarnessConfig(), "codex": HarnessConfig()}
    )

    @property
    def stage_timeout_s(self) -> int:
        return self.stage_timeout_min * 60

    def harness_config(self, name: str) -> HarnessConfig:
        return self.harnesses.get(name, HarnessConfig())

    @property
    def model(self) -> str | None:
        """The configured model for the selected harness, or None for the CLI default."""
        return self.harness_config(self.harness).model or None


DEFAULT_TOML = """\
[factory]
harness = "claude"                 # claude | codex
auth = "api"                       # api | subscription (attended only; poll refuses it)
base_branch = "main"
max_fix_rounds = 3
stage_timeout_min = 45
checks = [["make", "test"], ["make", "lint"]]   # argv arrays, run without a shell
test_paths = ["tests/"]
protected_paths = []               # added to the built-in list (design §9)
max_nits = 10                      # review gate: at most this many new nits per round
max_diff_bytes = 300000            # review diff above this is truncated (stat + largest files first)
transient_paths = []               # extra tooling droppings the worktree-clean rule ignores
env_passthrough = []               # extra env var NAMES forwarded to harness + check subprocesses

[poll]
label = "factory"                  # the one label read from GitHub
max_consecutive_failures = 3

[harness.claude]
model = ""                         # empty = CLI default
pinned_version = ""                # doctor warns on mismatch; actual version recorded in state.json
max_turns_read = 30                # the turn cap binds before the stage timeout; raise after watching num_turns
max_turns_write = 120
max_budget_usd = 0.0               # 0 = no cap

[harness.codex]
model = ""
pinned_version = ""
writable_dirs = []                 # extra --add-dir roots for workspace-write, e.g. ["~/.cache/uv"]
"""


def load_config(checkout_root: Path) -> Config:
    """Parse `<checkout_root>/factory.toml`. Missing file -> FactoryError telling the user to run `factory init`.

    Unknown keys are ignored. Invalid enum values (harness, auth) or wrong types -> FactoryError.
    """
    raise NotImplementedError


def parse_config(text: str) -> Config:
    """Parse TOML text into Config (used by load_config and tests). tomllib.TOMLDecodeError -> FactoryError."""
    raise NotImplementedError


def validate_overrides(config: Config, *, harness: str | None, auth: str | None, model: str | None) -> Config:
    """Return a copy of `config` with CLI global-flag overrides applied (design §6). Enum-checked.
    `model` overrides harnesses[<selected harness>].model. This is the ONLY place CLI flags are applied;
    everything downstream reads config.harness / config.auth / config.model."""
    raise NotImplementedError


_ = (tomllib, replace, FactoryError)
