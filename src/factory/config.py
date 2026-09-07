"""factory.toml (design §14). Read once, from the checkout the command runs in, never from the worktree.

DEFAULT_TOML is the packaged `templates/factory.toml` verbatim — the file `factory init` installs — so the
documented default and the installed default cannot drift apart. Every key below must appear there.

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

CONFIG_FILENAME = "factory.toml"


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


TEMPLATE_PATH = Path(__file__).parent / "templates" / "factory.toml"


def _default_toml() -> str:
    """The packaged templates/factory.toml — the ONE copy of the default config.

    `factory init` installs that file and DEFAULT_TOML documents it, so the two must be the same bytes: a
    second copy here is how a repo ends up initialised without the keys the factory has since grown (a live
    run reached codex with no `writable_dirs` line in the installed file, and therefore no --add-dir, because
    the template lagged behind this constant). Read once, at import: the file ships inside the package, so an
    unreadable one means a broken install and every command is about to fail anyway."""
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise FactoryError(
            f"the packaged default config is missing or unreadable at {TEMPLATE_PATH}: {exc}",
            hint="reinstall the factory package; templates/factory.toml ships inside it",
        ) from exc


DEFAULT_TOML = _default_toml()

_TYPE_NAMES = {
    bool: "boolean",
    int: "integer",
    float: "float",
    str: "string",
    list: "array",
    dict: "table",
    type(None): "nothing",
}


def _typename(value: object) -> str:
    return _TYPE_NAMES.get(type(value), type(value).__name__)


def _table(parent: dict, key: str, source: str) -> dict:
    """A TOML sub-table, defaulting to empty. A non-table value is a FactoryError."""
    value = parent.get(key, {})
    if not isinstance(value, dict):
        raise FactoryError(f"{source}: [{key}] must be a table, got {_typename(value)}")
    return value


def _get_str(table: dict, key: str, default: str, prefix: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise FactoryError(f"{prefix} {key} must be a string, got {_typename(value)}")
    return value


def _get_int(table: dict, key: str, default: int, prefix: str, *, minimum: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise FactoryError(f"{prefix} {key} must be an integer, got {_typename(value)}")
    if value < minimum:
        raise FactoryError(f"{prefix} {key} must be >= {minimum}, got {value}")
    return value


def _get_float(table: dict, key: str, default: float, prefix: str, *, minimum: float) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FactoryError(f"{prefix} {key} must be a number, got {_typename(value)}")
    if value < minimum:
        raise FactoryError(f"{prefix} {key} must be >= {minimum}, got {value}")
    return float(value)


def _get_str_list(table: dict, key: str, default: list[str], prefix: str) -> list[str]:
    value = table.get(key, default)
    if not isinstance(value, list):
        raise FactoryError(f"{prefix} {key} must be an array of strings, got {_typename(value)}")
    for i, entry in enumerate(value):
        if not isinstance(entry, str):
            raise FactoryError(f"{prefix} {key}[{i}] must be a string, got {_typename(entry)}")
    return list(value)


def _get_argv_list(table: dict, key: str, default: list[list[str]], prefix: str) -> list[list[str]]:
    """`checks = [["make", "test"], ...]`: argv arrays, run without a shell, so each must be non-empty."""
    value = table.get(key, default)
    if not isinstance(value, list):
        raise FactoryError(
            f"{prefix} {key} must be an array of argv arrays, got {_typename(value)}"
        )
    argvs: list[list[str]] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, list) or not entry:
            raise FactoryError(
                f"{prefix} {key}[{i}] must be a non-empty argv array of strings"
                f' (e.g. ["make", "test"]), got {_typename(entry)}'
            )
        for j, word in enumerate(entry):
            if not isinstance(word, str):
                raise FactoryError(
                    f"{prefix} {key}[{i}][{j}] must be a string, got {_typename(word)}"
                )
        argvs.append(list(entry))
    return argvs


def _check_enum(value: str, allowed: tuple[str, ...], what: str) -> str:
    if value not in allowed:
        raise FactoryError(f"{what}: unknown value {value!r}; expected one of {', '.join(allowed)}")
    return value


def _parse_harness_table(tables: dict, name: str, source: str) -> HarnessConfig:
    prefix = f"{source}: [harness.{name}]"
    table = tables.get(name, {})
    if not isinstance(table, dict):
        raise FactoryError(f"{prefix} must be a table, got {_typename(table)}")
    d = HarnessConfig()
    return HarnessConfig(
        model=_get_str(table, "model", d.model, prefix),
        pinned_version=_get_str(table, "pinned_version", d.pinned_version, prefix),
        max_turns_read=_get_int(table, "max_turns_read", d.max_turns_read, prefix, minimum=1),
        max_turns_write=_get_int(table, "max_turns_write", d.max_turns_write, prefix, minimum=1),
        max_budget_usd=_get_float(table, "max_budget_usd", d.max_budget_usd, prefix, minimum=0.0),
        writable_dirs=_get_str_list(table, "writable_dirs", d.writable_dirs, prefix),
    )


def load_config(checkout_root: Path) -> Config:
    """Parse `<checkout_root>/factory.toml`. Missing file -> FactoryError telling the user to run `factory init`.

    Unknown keys are ignored. Invalid enum values (harness, auth) or wrong types -> FactoryError.
    """
    path = Path(checkout_root) / CONFIG_FILENAME
    if not path.is_file():
        raise FactoryError(
            f"no {CONFIG_FILENAME} in {checkout_root}",
            hint="run `factory init` in the repository root to write one",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FactoryError(f"cannot read {path}: {exc}") from exc
    return parse_config(text, source=str(path))


def parse_config(text: str, *, source: str = CONFIG_FILENAME) -> Config:
    """Parse TOML text into Config (used by load_config and tests). tomllib.TOMLDecodeError -> FactoryError."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise FactoryError(f"{source}: invalid TOML: {exc}") from exc

    factory = _table(data, "factory", source)
    poll = _table(data, "poll", source)
    harness_tables = _table(data, "harness", source)
    fp = f"{source}: [factory]"
    pp = f"{source}: [poll]"
    d = Config()

    return Config(
        harness=_check_enum(
            _get_str(factory, "harness", d.harness, fp), HARNESSES, f"{fp} harness"
        ),
        auth=_check_enum(_get_str(factory, "auth", d.auth, fp), AUTHS, f"{fp} auth"),
        base_branch=_get_str(factory, "base_branch", d.base_branch, fp),
        max_fix_rounds=_get_int(factory, "max_fix_rounds", d.max_fix_rounds, fp, minimum=0),
        stage_timeout_min=_get_int(
            factory, "stage_timeout_min", d.stage_timeout_min, fp, minimum=1
        ),
        checks=_get_argv_list(factory, "checks", d.checks, fp),
        test_paths=_get_str_list(factory, "test_paths", d.test_paths, fp),
        protected_paths=_get_str_list(factory, "protected_paths", d.protected_paths, fp),
        max_nits=_get_int(factory, "max_nits", d.max_nits, fp, minimum=0),
        max_diff_bytes=_get_int(factory, "max_diff_bytes", d.max_diff_bytes, fp, minimum=1),
        transient_paths=_get_str_list(factory, "transient_paths", d.transient_paths, fp),
        env_passthrough=_get_str_list(factory, "env_passthrough", d.env_passthrough, fp),
        poll_label=_get_str(poll, "label", d.poll_label, pp),
        poll_max_consecutive_failures=_get_int(
            poll, "max_consecutive_failures", d.poll_max_consecutive_failures, pp, minimum=1
        ),
        harnesses={name: _parse_harness_table(harness_tables, name, source) for name in HARNESSES},
    )


def validate_overrides(
    config: Config, *, harness: str | None, auth: str | None, model: str | None
) -> Config:
    """Return a copy of `config` with CLI global-flag overrides applied (design §6). Enum-checked.
    `model` overrides harnesses[<selected harness>].model. This is the ONLY place CLI flags are applied;
    everything downstream reads config.harness / config.auth / config.model.

    The copy is deep enough that mutating the result never touches the parsed config: every list is copied
    and every HarnessConfig is replaced.
    """
    selected = config.harness if harness is None else _check_enum(harness, HARNESSES, "--harness")
    resolved_auth = config.auth if auth is None else _check_enum(auth, AUTHS, "--auth")

    harnesses = {
        name: replace(hc, writable_dirs=list(hc.writable_dirs))
        for name, hc in config.harnesses.items()
    }
    if model is not None:
        base = harnesses.get(selected, HarnessConfig())
        harnesses[selected] = replace(base, model=model)

    return replace(
        config,
        harness=selected,
        auth=resolved_auth,
        checks=[list(argv) for argv in config.checks],
        test_paths=list(config.test_paths),
        protected_paths=list(config.protected_paths),
        transient_paths=list(config.transient_paths),
        env_passthrough=list(config.env_passthrough),
        harnesses=harnesses,
    )
